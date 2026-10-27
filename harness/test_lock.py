"""Unit tests for lock.py with a local bare repository standing in for GitHub (no network)."""
from __future__ import annotations
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import harness_testing
import lock

GIT_ENVIRONMENT: dict[str, str] = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
REPOSITORY: str = 'owner/template_harness'


def git(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def lock_text(commit: str) -> str:
    text: str = ('# The harness this product builds with.\n[harness]\nrepository = "' + REPOSITORY + '"\n'
                 'commit = "' + commit + '"  # bumped by lock.py\n')
    return text


class LockTests(unittest.TestCase):
    def setUp(self) -> None:
        environment: mock._patch_dict = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root: Path = Path(directory.name).resolve()
        self.github: Path = self.root / 'github'
        bare: Path = self.github / (REPOSITORY + '.git')
        bare.parent.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)], check=True)
        self.seed: Path = self.root / 'seed'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(self.seed)], check=True)
        self.commits: list[str] = []
        index: int
        for index in range(2):
            self.commit('tool.py', 'version ' + str(index) + '\n')
        git(self.seed, 'tag', '-a', '-m', 'First', 'v1', self.commits[0])
        git(self.seed, 'push', '-q', str(bare), 'main', 'v1')
        self.lock_path: Path = self.root / 'product' / 'harness.lock'
        self.lock_path.parent.mkdir()
        self.lock_path.write_text(lock_text(self.commits[0]), encoding='utf-8')
        self.harness: Path = self.root / 'template_harness'

    def commit(self, name: str, text: str) -> str:
        (self.seed / name).write_text(text, encoding='utf-8')
        git(self.seed, 'add', name)
        git(self.seed, 'commit', '-q', '-m', 'Write ' + text.strip())
        self.commits.append(git(self.seed, 'rev-parse', 'HEAD'))
        return self.commits[-1]

    def run_lock(self, *arguments: str) -> tuple[int, str, str]:
        output: io.StringIO = io.StringIO()
        errors: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status: int = lock.main(['--lock', str(self.lock_path), '--github-base', str(self.github), *arguments])
        return status, output.getvalue(), errors.getvalue()

    def test_show_prints_the_pin(self) -> None:
        status: int
        output: str
        errors: str
        status, output, errors = self.run_lock('show')
        self.assertEqual(status, 0, errors)
        self.assertEqual(output, REPOSITORY + ' ' + self.commits[0] + '\n')

    def test_a_malformed_lock_is_refused(self) -> None:
        with self.assertRaisesRegex(lock.LockError, '40 lowercase'):
            lock.parse(lock_text('ABC'), 'harness.lock')
        with self.assertRaisesRegex(lock.LockError, 'owner/name'):
            lock.parse('[harness]\nrepository = "nameonly"\ncommit = "' + '0' * 40 + '"\n', 'harness.lock')
        with self.assertRaisesRegex(lock.LockError, 'no \\[harness\\]'):
            lock.parse('commit = "' + '0' * 40 + '"\n', 'harness.lock')

    def test_checkout_clones_then_moves_to_a_bumped_commit(self) -> None:
        self.assertEqual(self.run_lock('checkout', str(self.harness))[0], 0)
        self.assertEqual(git(self.harness, 'rev-parse', 'HEAD'), self.commits[0])
        self.assertEqual(git(self.harness, 'branch', '--show-current'), '')
        self.assertEqual((self.harness / 'tool.py').read_text(encoding='utf-8'), 'version 0\n')
        # A commit pushed after the clone: the existing clone fetches it.
        newest: str = self.commit('tool.py', 'version 2\n')
        git(self.seed, 'push', '-q', str(self.github / (REPOSITORY + '.git')), 'main')
        self.lock_path.write_text(lock_text(newest), encoding='utf-8')
        status: int
        output: str
        errors: str
        status, output, errors = self.run_lock('checkout', str(self.harness))
        self.assertEqual(status, 0, errors)
        self.assertIn('Fetching', output)
        self.assertEqual(git(self.harness, 'rev-parse', 'HEAD'), newest)
        # Again: nothing to fetch, still the commit.
        self.assertNotIn('Fetching', self.run_lock('checkout', str(self.harness))[1])

    def test_checkout_refuses_a_dirty_or_foreign_directory_and_an_unknown_commit(self) -> None:
        self.assertEqual(self.run_lock('checkout', str(self.harness))[0], 0)
        (self.harness / 'tool.py').write_text('edited\n', encoding='utf-8')
        status: int
        output: str
        errors: str
        status, output, errors = self.run_lock('checkout', str(self.harness))
        self.assertEqual(status, 1)
        self.assertIn('uncommitted changes', errors)
        foreign: Path = self.root / 'foreign'
        foreign.mkdir()
        (foreign / 'file.txt').write_text('x\n', encoding='utf-8')
        status, output, errors = self.run_lock('checkout', str(foreign))
        self.assertIn('not a git clone', errors)
        self.lock_path.write_text(lock_text('1' * 40), encoding='utf-8')
        status, output, errors = self.run_lock('checkout', str(self.root / 'fresh'))
        self.assertEqual(status, 1)
        self.assertIn('has no commit ' + '1' * 40, errors)

    def test_bump_rewrites_only_the_commit(self) -> None:
        status: int
        output: str
        errors: str
        status, output, errors = self.run_lock('bump')
        self.assertEqual(status, 0, errors)
        self.assertEqual(self.lock_path.read_text(encoding='utf-8'), lock_text(self.commits[1]))
        self.assertEqual(self.run_lock('bump', '--to', 'v1')[0], 0)
        self.assertEqual(lock.load(self.lock_path).commit, self.commits[0])
        self.assertEqual(self.run_lock('bump', '--to', 'f' * 40)[0], 0)
        self.assertEqual(lock.load(self.lock_path).commit, 'f' * 40)
        status, output, errors = self.run_lock('bump', '--to', 'absent')
        self.assertEqual(status, 1)
        self.assertIn('no branch or tag absent', errors)


class ActionLockReaderTests(unittest.TestCase):
    """The composite action's read_lock.sh agrees with lock.parse on what a lock says."""
    READER: Path = harness_testing.REPOSITORY / '.github' / 'actions' / 'harness-checkout' / 'read_lock.sh'

    def read(self, text: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / 'harness.lock'
            path.write_text(text, encoding='utf-8')
            completed: subprocess.CompletedProcess[str] = subprocess.run(
                ['sh', str(self.READER), str(path)], capture_output=True, text=True, check=False)
        return completed

    def test_a_valid_lock_gives_the_two_outputs(self) -> None:
        commit: str = 'a' * 40
        parsed: lock.Lock = lock.parse(lock_text(commit), 'harness.lock')
        completed: subprocess.CompletedProcess[str] = self.read(lock_text(commit))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, 'repository=' + parsed.repository + '\ncommit=' + parsed.commit + '\n')

    def test_a_malformed_or_repeated_commit_is_refused(self) -> None:
        self.assertEqual(self.read(lock_text('ABC')).returncode, 1)
        self.assertEqual(self.read(lock_text('a' * 40) + 'commit = "' + 'b' * 40 + '"\n').returncode, 1)
        self.assertIn('repository', self.read('[harness]\ncommit = "' + 'a' * 40 + '"\n').stderr)


if __name__ == '__main__':
    unittest.main()
