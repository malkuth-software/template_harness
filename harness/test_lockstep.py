"""Unit tests for lockstep.py: a local repository stands in for GUI.Forms and a script for its build (no network)."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any
import unittest
from unittest import mock

import harness_testing
import lockstep

GIT_ENVIRONMENT: dict[str, str] = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
# The stand-in for tools/build_sdk.py: logs each run (the directory it ran
# in, its --work, CCACHE_BASEDIR and LANG) to FAKE_BUILD_LOG, waits
# FAKE_BUILD_SECONDS, fails when FAKE_BUILD_FAIL is set, and otherwise
# installs a package configuration and a font directory into --prefix.
BUILD_SCRIPT: str = '''import argparse, json, os, pathlib, sys, time
parser = argparse.ArgumentParser()
for name in ('--target', '--prefix', '--jobs', '--work', '--features'):
    parser.add_argument(name, required=True)
args = parser.parse_args()
record = {'cwd': pathlib.Path.cwd().name, 'work': pathlib.Path(args.work).as_posix(), 'features': args.features,
          'basedir': os.environ.get('CCACHE_BASEDIR', ''), 'lang': os.environ.get('LANG', '')}
with open(os.environ['FAKE_BUILD_LOG'], 'a', encoding='utf-8') as log:
    log.write(json.dumps(record) + '\\n')
time.sleep(float(os.environ.get('FAKE_BUILD_SECONDS', '0')))
if os.environ.get('FAKE_BUILD_FAIL'):
    sys.exit(3)
prefix = pathlib.Path(args.prefix)
(prefix / 'lib' / 'cmake' / 'GUIForms').mkdir(parents=True)
(prefix / 'lib' / 'cmake' / 'GUIForms' / 'GUIFormsConfig.cmake').write_text('# config\\n')
(prefix / 'share' / 'GUIForms' / 'fonts').mkdir(parents=True)
'''


def git(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def commit_file(repository: Path, name: str, text: str) -> str:
    """Commit text as name in repository; the new commit."""
    path: Path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    git(repository, 'add', name)
    git(repository, 'commit', '-q', '-m', 'change ' + name)
    result: str = git(repository, 'rev-parse', 'HEAD')
    return result


class Fixture:
    """An upstream repository, a lockstep root and the environment the commands see."""

    def __init__(self, directory: Path) -> None:
        self.upstream: Path = directory / 'upstream'
        self.upstream.mkdir(parents=True)
        git(self.upstream, 'init', '-q', '-b', 'main')
        commit_file(self.upstream, 'cmake/llvm22.cmake', '# toolchain\n')
        self.first: str = commit_file(self.upstream, 'tools/build_sdk.py', BUILD_SCRIPT)
        self.root: Path = directory / 'root'
        self.layout: lockstep.Layout = lockstep.layout_of(self.root)
        self.log: Path = directory / 'build.log'
        self.target: str = lockstep.host_target()
        self.environment: dict[str, str] = {**GIT_ENVIRONMENT, 'FAKE_BUILD_LOG': str(self.log),
                                            'FAKE_BUILD_FAIL': '', 'FAKE_BUILD_SECONDS': '0',
                                            lockstep.IDENTITY_VARIABLE: 'toolchain-a', 'LANG': 'caller'}

    def run(self, command: str, *arguments: str) -> list[str]:
        """Run one command function with the fixture's environment and the compiler and ccache stood in for."""
        with mock.patch.dict('os.environ', self.environment), \
                mock.patch.object(lockstep, 'compiler_path', return_value=Path('/toolchain/bin/clang')), \
                mock.patch.object(lockstep, 'ccache_counters', return_value={}):
            if command == 'init':
                result: list[str] = lockstep.init_command(self.layout, str(self.upstream), None,
                                                          [Path(argument) for argument in arguments])
            elif command == 'advance':
                result = lockstep.advance_command(self.layout, arguments[0], 'origin')
            elif command == 'build':
                result = lockstep.build_command(self.layout, self.target, 2)
            else:
                result = lockstep.prefix_command(self.layout, self.target, bool(arguments))
        return result

    def builds(self) -> list[dict[str, str]]:
        lines: list[str] = self.log.read_text(encoding='utf-8').splitlines() if self.log.exists() else []
        result: list[dict[str, str]] = [json.loads(line) for line in lines]
        return result



class LockstepTests(unittest.TestCase):
    def test_build_once_per_commit_and_prefix_follows_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture: Fixture = Fixture(Path(directory))
            self.assertEqual(fixture.run('init')[1], 'commit=' + fixture.first)
            with self.assertRaisesRegex(lockstep.LockstepError, 'nothing is built'):
                fixture.run('prefix')
            first_prefix: Path = lockstep.install_of(fixture.layout, fixture.first, fixture.target)
            self.assertEqual(fixture.run('build'), ['prefix=' + first_prefix.as_posix(), 'built=' + fixture.first,
                                                    'compiler_cache_hits=0/0'])
            self.assertEqual(fixture.run('build'), ['prefix=' + first_prefix.as_posix(), 'built=already'])
            self.assertEqual(len(fixture.builds()), 1)
            run: dict[str, str] = fixture.builds()[0]
            expected_lang: str = 'en_US.UTF-8' if fixture.target.startswith('windows') else 'caller'
            self.assertEqual(run, {
                'features': 'all', 'cwd': 'gui_forms', 'basedir': fixture.layout.workspace.as_posix(), 'lang': expected_lang,
                'work': (fixture.layout.workspace / '.build' / ('native-' + fixture.target)).as_posix()})
            self.assertEqual(fixture.run('prefix'), [first_prefix.as_posix()])
            record: dict[str, Any] = json.loads(fixture.run('prefix', 'json')[0])
            self.assertEqual(record['commit'], fixture.first)
            self.assertEqual(record['features'], 'all')
            self.assertEqual(record['package_dir'], (first_prefix / 'lib/cmake/GUIForms').as_posix())
            self.assertEqual(record['font_dir'], (first_prefix / 'share/GUIForms/fonts').as_posix())
            self.assertEqual(record['source'], fixture.layout.source.as_posix())
            self.assertEqual(record['toolchain_file'], (fixture.layout.source / 'cmake/llvm22.cmake').as_posix())
            self.assertEqual(sorted(record), sorted([
                'schema', 'commit', 'target', 'toolchain_identity', 'features', 'compiler', 'prefix', 'package_dir', 'source',
                'toolchain_file', 'font_dir', 'llvm_runtime', 'ccache_dir', 'ccache_basedir', 'lang']))
            second: str = commit_file(fixture.upstream, 'src/one.cpp', 'int one();\n')
            self.assertEqual(fixture.run('advance', 'main'), ['from=' + fixture.first, 'commit=' + second])
            with self.assertRaisesRegex(lockstep.LockstepError, 'lockstep clone is at ' + second):
                fixture.run('prefix')
            second_prefix: Path = lockstep.install_of(fixture.layout, second, fixture.target)
            self.assertEqual(fixture.run('build')[0], 'prefix=' + second_prefix.as_posix())
            self.assertEqual(fixture.run('prefix'), [second_prefix.as_posix()])
            # The earlier install stays for products still using it.
            self.assertTrue((first_prefix / lockstep.RECORD_NAME).is_file())
            # Back to a built commit: its install becomes current again without a build.
            self.assertEqual(fixture.run('advance', fixture.first)[1], 'commit=' + fixture.first)
            self.assertEqual(fixture.run('build'), ['prefix=' + first_prefix.as_posix(), 'built=already'])
            self.assertEqual(fixture.run('prefix'), [first_prefix.as_posix()])
            self.assertEqual(len(fixture.builds()), 2)
            # Another toolchain builds the same commit again.
            fixture.environment[lockstep.IDENTITY_VARIABLE] = 'toolchain-b'
            fixture.run('build')
            self.assertEqual(len(fixture.builds()), 3)

    def test_a_failed_build_leaves_current_and_is_built_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture: Fixture = Fixture(Path(directory))
            fixture.run('init')
            fixture.run('build')
            second: str = commit_file(fixture.upstream, 'src/one.cpp', 'int one();\n')
            fixture.run('advance', 'main')
            fixture.environment['FAKE_BUILD_FAIL'] = '1'
            with self.assertRaisesRegex(lockstep.LockstepError, 'unchanged'):
                fixture.run('build')
            current: dict[str, Any] | None = lockstep.read_record(lockstep.current_of(fixture.layout, fixture.target))
            self.assertEqual(current['commit'] if current is not None else '', fixture.first)
            fixture.environment['FAKE_BUILD_FAIL'] = ''
            self.assertEqual(fixture.run('build')[1], 'built=' + second)

    def test_concurrent_builds_build_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture: Fixture = Fixture(Path(directory))
            fixture.run('init')
            fixture.environment['FAKE_BUILD_SECONDS'] = '1'
            results: list[list[str]] = []
            worker: ConcurrentBuild = ConcurrentBuild(fixture, results)
            threads: list[threading.Thread] = [threading.Thread(target=worker.run) for index in range(3)]
            thread: threading.Thread
            with mock.patch.dict('os.environ', fixture.environment), \
                    mock.patch.object(lockstep, 'compiler_path', return_value=Path('/toolchain/bin/clang')), \
                    mock.patch.object(lockstep, 'ccache_counters', return_value={}):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(len(fixture.builds()), 1)
            self.assertEqual(sorted(result[1] for result in results),
                             sorted(['built=' + fixture.first, 'built=already', 'built=already']))

    def test_init_seeds_missing_sources_and_refuses_a_foreign_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            seed: Path = Path(directory) / 'seed'
            (seed / 'third_party' / 'harfbuzz').mkdir(parents=True)
            (seed / 'third_party' / 'harfbuzz' / 'hb.h').write_text('// hb\n', encoding='utf-8')
            fixture: Fixture = Fixture(Path(directory))
            lines: list[str] = fixture.run('init', str(seed))
            self.assertEqual(lines[-1], 'unseeded=freetype,libunibreak,miniaudio')
            self.assertTrue((fixture.layout.source / 'third_party' / 'harfbuzz' / 'hb.h').is_file())
            self.assertEqual(fixture.run('init')[1], 'commit=' + fixture.first)
            other: Fixture = Fixture(Path(directory) / 'other')
            other.layout.source.mkdir(parents=True)
            (other.layout.source / 'file').write_text('x\n', encoding='utf-8')
            with self.assertRaisesRegex(lockstep.LockstepError, 'not a lockstep clone'):
                other.run('init')

    def test_a_commit_without_sdk_features_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture: Fixture = Fixture(Path(directory))
            commit_file(fixture.upstream, 'tools/build_sdk.py', BUILD_SCRIPT.replace(", '--features'", ''))
            fixture.run('init')
            with self.assertRaisesRegex(lockstep.LockstepError, 'has no --features'):
                fixture.run('build')
            self.assertEqual(fixture.builds(), [])

    def test_changes_to_tracked_files_stop_build_and_advance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture: Fixture = Fixture(Path(directory))
            fixture.run('init')
            (fixture.layout.source / 'cmake' / 'llvm22.cmake').write_text('# edited\n', encoding='utf-8')
            with self.assertRaisesRegex(lockstep.LockstepError, 'tracked files'):
                fixture.run('build')
            with self.assertRaisesRegex(lockstep.LockstepError, 'tracked files'):
                fixture.run('advance', 'main')
            self.assertEqual(fixture.builds(), [])


class ConcurrentBuild:
    """One build per call, appending its printed lines to a shared list (the list's append is atomic)."""

    def __init__(self, fixture: Fixture, results: list[list[str]]) -> None:
        self.fixture = fixture
        self.results = results

    def run(self) -> None:
        lines: list[str] = lockstep.build_command(self.fixture.layout, self.fixture.target, 2)
        self.results.append(lines)


if __name__ == '__main__':
    unittest.main()
