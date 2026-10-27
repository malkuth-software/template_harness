"""Unit tests for bootstrap_proof.py: the log rules, the snapshot, and the workspace checks on local repositories."""
from __future__ import annotations
import argparse
import contextlib
import dataclasses
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import harness_testing
import bootstrap
import bootstrap_proof
import lock
import project
import restore_local

GIT_ENVIRONMENT: dict[str, str] = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
NOTES_BRANCH: str = 'gui_forms'
PINNED: str = '0123456789abcdef0123456789abcdef01234567'
CACHE: str = 'build-e077f9d5'
BEFORE: bootstrap_proof.Before = bootstrap_proof.Before(product='aaaa', harness=PINNED, cache=CACHE)
# What bootstrap.py, lock.py and restore_local.py print, shortened, as the CI logs show them.
FIRST_WINDOWS_LOG: tuple[str, ...] = (
    'Workspace W for gui_forms (windows-x64)',
    'the product: cloning https://github.com/malkuth-software/gui_forms.git',
    'harness: malkuth-software/template_harness at ' + PINNED + ' (harness.lock) in W/template_harness',
    'Cloning https://github.com/malkuth-software/template_harness.git into W/template_harness',
    'the notes: cloning https://github.com/malkuth-software/notes.git',
    'notes branch gui_forms: adding the worktree W/notes-gui_forms',
    'Restored the Windows toolchain 0123456789ab to W/.toolchain/x/msys64; put msys64/clang64/bin first',
    'Restored the cache of ' + CACHE + ' into W',
    'Smoke build skipped (--no-smoke)',
    'Bootstrap finished')
AGAIN_WINDOWS_LOG: tuple[str, ...] = (
    'Workspace W for gui_forms (windows-x64)',
    'the product: updating W/gui_forms',
    'W/template_harness is malkuth-software/template_harness at ' + PINNED,
    'the notes: updating W/notes',
    'notes branch gui_forms: updating W/notes-gui_forms',
    'The Windows toolchain 0123456789ab is already at W/.toolchain/x/msys64',
    'The cache of ' + CACHE + ' is already restored',
    '-- Fetching ninja for the smoke build, which no rule reads',
    'Fetching ninja at version:2@1.12.1.chromium.4 for platform mac-arm64',
    'Bootstrap finished')


def git(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def again(platform: str, log: tuple[str, ...], before: bootstrap_proof.Before = BEFORE) -> tuple[list[str], list[str]]:
    moves: list[str] = []
    failures: list[str] = bootstrap_proof.again_failures(list(log), platform, NOTES_BRANCH, before, PINNED, moves)
    return failures, moves


def replaced(log: tuple[str, ...], prefix: str, line: str) -> tuple[str, ...]:
    """log with each line starting prefix replaced by line."""
    lines: list[str] = []
    entry: str
    for entry in log:
        lines.append(line if entry.startswith(prefix) else entry)
    return tuple(lines)


class LogTests(unittest.TestCase):
    def test_a_first_and_a_repeated_windows_run_pass(self) -> None:
        self.assertEqual(bootstrap_proof.first_failures(list(FIRST_WINDOWS_LOG), 'windows-x64', NOTES_BRANCH, True), [])
        self.assertEqual(again('windows-x64', AGAIN_WINDOWS_LOG), ([], []))

    def test_a_dependency_fetch_in_the_smoke_build_is_not_a_harness_fetch(self) -> None:
        fetched: tuple[str, ...] = AGAIN_WINDOWS_LOG + ('Fetching 0123456789ab into W/template_harness',)
        self.assertEqual(again('windows-x64', fetched)[0],
                         ["the log has a line starting 'Fetching [0-9a-f]{12} into '"])

    def test_a_repeated_run_that_clones_fails(self) -> None:
        recloned: tuple[str, ...] = AGAIN_WINDOWS_LOG + ('Cloning https://example.invalid/h.git into W/h',)
        self.assertEqual(again('windows-x64', recloned)[0], ["the log has a line starting 'Cloning \\\\S+ into '"])

    def test_restoring_the_same_cache_again_fails_and_a_newer_one_is_a_move(self) -> None:
        same: tuple[str, ...] = replaced(AGAIN_WINDOWS_LOG, 'The cache of ', 'Restored the cache of ' + CACHE + ' into W')
        self.assertEqual(again('windows-x64', same)[0], ['the run restored the cache of ' + CACHE + ' again'])
        newer: tuple[str, ...] = replaced(AGAIN_WINDOWS_LOG, 'The cache of ', 'Restored the cache of build-62d8 into W')
        failures: list[str]
        moves: list[str]
        failures, moves = again('windows-x64', newer)
        self.assertEqual(failures, [])
        self.assertEqual(moves, ['the cache moved from ' + CACHE + ' to build-62d8 (the product was at aaaa)'])

    def test_a_moved_harness_may_fetch_and_restore_the_toolchain(self) -> None:
        moved: tuple[str, ...] = replaced(AGAIN_WINDOWS_LOG, 'The Windows toolchain ',
                                          'Restored the Windows toolchain fedcba987654 to W/.toolchain/y/msys64')
        moved = moved + ('Fetching 0123456789ab into W/template_harness',)
        self.assertEqual(len(again('windows-x64', moved)[0]), 3)
        earlier: bootstrap_proof.Before = bootstrap_proof.Before(product='aaaa', harness='f' * 40, cache=CACHE)
        failures: list[str]
        moves: list[str]
        failures, moves = again('windows-x64', moved, earlier)
        self.assertEqual(failures, [])
        self.assertEqual(moves, ['harness.lock moved the harness from ' + 'f' * 40 + ' to ' + PINNED])

    def test_the_first_run_log_is_not_a_no_op(self) -> None:
        failures: list[str] = again('windows-x64', FIRST_WINDOWS_LOG)[0]
        self.assertIn("the log has no line starting 'the product: updating '", failures)
        self.assertIn("the log has a line starting 'the product: cloning '", failures)
        self.assertIn('the run restored the cache of ' + CACHE + ' again', failures)

    def test_an_unpublished_cache_satisfies_both_runs(self) -> None:
        first: tuple[str, ...] = replaced(FIRST_WINDOWS_LOG, 'Restored the cache', 'No published gui-forms-cache-x')
        self.assertEqual(bootstrap_proof.first_failures(list(first), 'windows-x64', NOTES_BRANCH, True), [])
        repeated: tuple[str, ...] = replaced(AGAIN_WINDOWS_LOG, 'The cache of ', 'No published gui-forms-cache-x')
        self.assertEqual(again('windows-x64', repeated), ([], []))

    def test_a_run_that_did_not_finish_fails(self) -> None:
        unfinished: tuple[str, ...] = AGAIN_WINDOWS_LOG[:-1]
        self.assertEqual(again('windows-x64', unfinished)[0], ["the log has no line starting 'Bootstrap finished'"])

    def test_macos_needs_the_runtime_and_linux_needs_no_toolchain_line(self) -> None:
        unix: list[str] = []
        line: str
        for line in AGAIN_WINDOWS_LOG:
            if 'Windows toolchain' not in line:
                unix.append(line)
        self.assertEqual(again('linux-x64', tuple(unix)), ([], []))
        self.assertEqual(again('macos-arm64', tuple(unix))[0],
                         ["the log has no line starting 'The macOS runtime at .* matches its manifest'"])
        unix.append('The macOS runtime at W/.build/toolchain/llvm matches its manifest')
        self.assertEqual(again('macos-arm64', tuple(unix)), ([], []))

    def test_the_cache_record_is_restore_locals(self) -> None:
        self.assertEqual(bootstrap_proof.CACHE_RECORD, restore_local.CACHE_RECORD)
        self.assertEqual(bootstrap_proof.PYTHON_ARGUMENT, bootstrap.PYTHON_ARGUMENT)

    def test_an_unlocked_product_says_so_instead_of_cloning_a_harness(self) -> None:
        unlocked: tuple[str, ...] = replaced(FIRST_WINDOWS_LOG, 'Cloning ',
                                             'harness: the product has no harness.lock; using this harness, H')
        self.assertEqual(bootstrap_proof.first_failures(list(unlocked), 'windows-x64', NOTES_BRANCH, False), [])
        self.assertEqual(bootstrap_proof.first_failures(list(unlocked), 'windows-x64', NOTES_BRANCH, True),
                         ['the log has no line starting ' + repr(bootstrap_proof.HARNESS_CLONED.pattern)])
        unpinned: bootstrap_proof.Before = bootstrap_proof.Before(product='aaaa', harness=None, cache=CACHE)
        moves: list[str] = []
        failures: list[str] = bootstrap_proof.again_failures(list(AGAIN_WINDOWS_LOG), 'windows-x64', NOTES_BRANCH,
                                                             unpinned, None, moves)
        self.assertEqual((failures, moves), ([], []))


class FakeGitHub(bootstrap_proof.GitHub):
    """Answers from fixed sets instead of gh."""

    def __init__(self, paths: set[str], branches: set[str]) -> None:
        self.paths: set[str] = paths
        self.branches: set[str] = branches

    def answer(self, endpoint: str, query: str) -> str | None:
        raise AssertionError('no gh in tests: ' + endpoint)

    def default_branch(self, repository: str) -> str | None:
        branch: str | None = None if repository.endswith('/private') else 'main'
        return branch

    def has_path(self, repository: str, path: str, ref: str) -> bool:
        found: bool = repository + ':' + ref + ':' + path in self.paths
        return found

    def has_branch(self, repository: str, branch: str) -> bool:
        found: bool = repository + ':' + branch in self.branches
        return found


class MatrixTests(unittest.TestCase):
    def test_the_smoke_paths_follow_the_command_form(self) -> None:
        self.assertEqual(bootstrap_proof.smoke_paths(('{python}', 'scripts/smoke-build.py')),
                         ['scripts/smoke-build.py'])
        self.assertEqual(bootstrap_proof.smoke_paths(('{python}', '-B', 'tools/x.py', '--smoke')), ['tools/x.py'])
        self.assertEqual(bootstrap_proof.smoke_paths(
            ('{python}', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_publication.py')),
            ['tests/test_publication.py'])
        self.assertEqual(bootstrap_proof.smoke_paths(('{python}', '-m', 'unittest', 'discover', '-p', 'test_*.py')),
                         [])
        self.assertEqual(bootstrap_proof.smoke_paths(('{python}', '-m', 'compileall', 'src')), [])
        self.assertEqual(bootstrap_proof.smoke_paths(('sh', 'build.sh', 'all')), ['build.sh'])
        self.assertEqual(bootstrap_proof.smoke_paths(()), [])

    def test_a_project_waits_for_its_notes_branch_and_smoke_script(self) -> None:
        reference: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        # The smoke runs gui_forms/tools/ci_build.py from the workspace: tools/ci_build.py in the repository.
        script: str = 'tools/ci_build.py'
        ready: FakeGitHub = FakeGitHub({reference.repository + ':main:' + script},
                                       {'malkuth-software/notes:gui_forms'})
        self.assertIsNone(bootstrap_proof.readiness(reference, ready))
        no_script: FakeGitHub = FakeGitHub(set(), {'malkuth-software/notes:gui_forms'})
        self.assertEqual(bootstrap_proof.readiness(reference, no_script),
                         'its smoke script ' + script + ' is not on ' + reference.repository + ' main yet')
        no_notes: FakeGitHub = FakeGitHub({reference.repository + ':main:' + script}, set())
        self.assertEqual(bootstrap_proof.readiness(reference, no_notes),
                         'malkuth-software/notes has no branch gui_forms yet')
        assert reference.bootstrap is not None
        silent: project.Project = dataclasses.replace(
            reference, bootstrap=dataclasses.replace(reference.bootstrap, smoke=()))
        self.assertEqual(bootstrap_proof.readiness(silent, ready), 'its [bootstrap] table names no smoke command yet')
        hidden: project.Project = dataclasses.replace(reference, repository='owner/private')
        self.assertEqual(bootstrap_proof.readiness(hidden, ready), 'owner/private cannot be read (missing or private)')

    def test_smoke_paths_are_read_relative_to_the_product_checkout(self) -> None:
        reference: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        self.assertEqual(bootstrap_proof.repository_path(reference, 'gui_forms/tools/ci_build.py'), 'tools/ci_build.py')
        self.assertIsNone(bootstrap_proof.repository_path(reference, 'template_harness/harness/x.py'))
        root: project.Project = dataclasses.replace(reference, source_dir='.')
        self.assertEqual(bootstrap_proof.repository_path(root, 'scripts/smoke-build.py'), 'scripts/smoke-build.py')

    def test_the_matrix_has_each_host_of_each_ready_project(self) -> None:
        reference: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        waiting: project.Project = dataclasses.replace(reference, name='waiting', repository='owner/waiting')
        unbooted: project.Project = dataclasses.replace(reference, name='plain', bootstrap=None)
        github: FakeGitHub = FakeGitHub({reference.repository + ':main:tools/ci_build.py'},
                                        {'malkuth-software/notes:gui_forms'})
        entries: list[dict[str, str]]
        skipped: list[str]
        entries, skipped = bootstrap_proof.matrix_entries([reference, waiting, unbooted], github)
        self.assertEqual(len(entries), len(bootstrap_proof.HOSTS))
        self.assertEqual(entries[0], {'project': 'gui_forms', 'runner': 'windows-2022', 'platform': 'windows-x64',
                                      'checkout': 'gui_forms', 'notes_branch': 'gui_forms'})
        self.assertEqual(skipped, ['waiting: skipped, because its smoke script tools/ci_build.py is not on '
                                   'owner/waiting main yet'])

    def test_plan_writes_the_matrix_and_the_summary(self) -> None:
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        output: Path = Path(directory.name) / 'output'
        summary: Path = Path(directory.name) / 'summary'
        projects: Path = harness_testing.REPOSITORY / 'projects'
        github: FakeGitHub = FakeGitHub({'malkuth-software/gui_forms:main:tools/ci_build.py'},
                                        {'malkuth-software/notes:gui_forms'})
        arguments: argparse.Namespace = argparse.Namespace(projects=projects, output=output, summary=summary,
                                                           only=['gui_forms', 'plan-paint'])
        printed: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(printed):
            status: int = bootstrap_proof.plan(arguments, github)
        self.assertEqual(status, 0)
        lines: list[str] = output.read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines[1], 'count=' + str(len(bootstrap_proof.HOSTS)))
        matrix: dict[str, list[dict[str, str]]] = json.loads(lines[0][len('matrix='):])
        self.assertEqual(matrix['include'][0]['project'], 'gui_forms')
        self.assertIn('- plan-paint: skipped, because malkuth-software/notes has no branch plan-paint yet',
                      summary.read_text(encoding='utf-8'))
        self.assertIn('::notice title=Bootstrap proof::plan-paint: skipped', printed.getvalue())


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        environment: mock._patch_dict = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace: Path = Path(directory.name)
        self.harness: Path = self.workspace / bootstrap_proof.HARNESS_DIRECTORY
        self.harness.mkdir()
        git(self.harness, 'init', '--quiet', '--initial-branch=main')
        git(self.harness, 'commit', '--quiet', '--allow-empty', '-m', 'one')
        self.locked: str = git(self.harness, 'rev-parse', 'HEAD')
        git(self.harness, 'commit', '--quiet', '--allow-empty', '-m', 'two')
        git(self.harness, 'checkout', '--quiet', '--detach', self.locked)
        self.product: Path = self.workspace / 'gui_forms'
        self.product.mkdir()
        git(self.product, 'init', '--quiet', '--initial-branch=main')
        git(self.product, 'commit', '--quiet', '--allow-empty', '-m', 'product')
        notes: Path = self.workspace / 'notes'
        notes.mkdir()
        git(notes, 'init', '--quiet', '--initial-branch=main')
        git(notes, 'commit', '--quiet', '--allow-empty', '-m', 'notes')
        git(notes, 'worktree', 'add', '--quiet', '-b', NOTES_BRANCH, str(self.workspace / ('notes-' + NOTES_BRANCH)))

    def failures(self, commit: str) -> list[str]:
        pinned: lock.Lock = lock.Lock('owner/template_harness', commit)
        failures: list[str] = bootstrap_proof.workspace_failures(self.workspace, 'gui_forms', NOTES_BRANCH, pinned)
        return failures

    def test_the_pinned_harness_and_the_notes_worktree_pass(self) -> None:
        self.assertEqual(self.failures(self.locked), [])

    def test_another_commit_or_a_branch_fails(self) -> None:
        self.assertEqual(len(self.failures('f' * 40)), 1)
        git(self.harness, 'checkout', '--quiet', 'main')
        failures: list[str] = self.failures(git(self.harness, 'rev-parse', 'HEAD'))
        self.assertEqual(len(failures), 1)
        self.assertIn('is on a branch', failures[0])

    def test_the_snapshot_records_the_commits_and_the_cache(self) -> None:
        record: Path = self.workspace / bootstrap_proof.CACHE_RECORD
        record.parent.mkdir(parents=True)
        record.write_text(json.dumps({'tag': CACHE, 'asset': 'a'}), encoding='utf-8')
        before: bootstrap_proof.Before = bootstrap_proof.snapshot(self.workspace, 'gui_forms')
        self.assertEqual(before, bootstrap_proof.Before(product=git(self.product, 'rev-parse', 'HEAD'),
                                                        harness=self.locked, cache=CACHE))

    def test_main_snapshots_then_reports_each_failure(self) -> None:
        (self.product / lock.LOCK_NAME).write_text(
            '[harness]\nrepository = "owner/template_harness"\ncommit = "' + self.locked + '"\n', encoding='utf-8')
        snapshot_file: Path = self.workspace / 'before.json'
        output: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(output):
            status: int = bootstrap_proof.main(['snapshot', '--workspace', str(self.workspace), '--checkout',
                                                'gui_forms', '--out', str(snapshot_file)])
        self.assertEqual(status, 0)
        log: Path = self.workspace / 'again.log'
        log.write_text('\n'.join(FIRST_WINDOWS_LOG) + '\n', encoding='utf-8')
        arguments: list[str] = ['again', '--log', str(log), '--workspace', str(self.workspace),
                                '--checkout', 'gui_forms', '--notes-branch', NOTES_BRANCH, '--platform', 'linux-x64',
                                '--before', str(snapshot_file)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = bootstrap_proof.main(arguments)
        self.assertEqual(status, 1)
        self.assertIn('FAILED: the log has no line starting', output.getvalue())
        log.write_text('\n'.join(AGAIN_WINDOWS_LOG).replace(PINNED, self.locked) + '\n', encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = bootstrap_proof.main(arguments)
        self.assertEqual(status, 0, output.getvalue())


if __name__ == '__main__':
    unittest.main()
