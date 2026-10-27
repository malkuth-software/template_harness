"""Unit tests for bootstrap_proof.py: the log expectations, and the workspace checks on local repositories."""
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
import bootstrap_proof
import lock

GIT_ENVIRONMENT: dict[str, str] = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
NOTES_BRANCH: str = 'gui_forms'
# What bootstrap.py, lock.py and restore_local.py print, shortened to the lines the proof reads.
FIRST_WINDOWS_LOG: tuple[str, ...] = (
    'Workspace W for gui_forms (windows-x64)',
    'the product: cloning https://github.com/malkuth-software/gui_forms.git',
    'Cloning https://github.com/malkuth-software/template_harness.git into W/template_harness',
    'the notes: cloning https://github.com/malkuth-software/notes.git',
    'notes branch gui_forms: adding the worktree W/notes-gui_forms',
    'Restored the Windows toolchain 0123456789ab to W/.toolchain/x/msys64; put msys64/clang64/bin first',
    'Restored the cache of build-0123 into W',
    'Smoke build skipped (--no-smoke)',
    'Bootstrap finished')
AGAIN_WINDOWS_LOG: tuple[str, ...] = (
    'Workspace W for gui_forms (windows-x64)',
    'the product: updating W/gui_forms',
    'W/template_harness is malkuth-software/template_harness at 0123',
    'the notes: updating W/notes',
    'notes branch gui_forms: updating W/notes-gui_forms',
    'The Windows toolchain 0123456789ab is already at W/.toolchain/x/msys64',
    'The cache of build-0123 is already restored',
    'Bootstrap finished')


def git(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def failures_of(run: str, platform: str, log: tuple[str, ...]) -> list[str]:
    expected: bootstrap_proof.Expectations = bootstrap_proof.expectations(run, platform, NOTES_BRANCH)
    failures: list[str] = bootstrap_proof.log_failures(list(log), expected)
    return failures


class LogTests(unittest.TestCase):
    def test_a_first_and_a_repeated_windows_run_pass(self) -> None:
        self.assertEqual(failures_of('first', 'windows-x64', FIRST_WINDOWS_LOG), [])
        self.assertEqual(failures_of('again', 'windows-x64', AGAIN_WINDOWS_LOG), [])

    def test_a_repeated_run_that_clones_or_downloads_fails(self) -> None:
        recloned: tuple[str, ...] = AGAIN_WINDOWS_LOG + ('Cloning https://example.invalid/h.git into W/h',)
        self.assertEqual(failures_of('again', 'windows-x64', recloned), ["the log has a line with 'Cloning '"])
        downloaded: tuple[str, ...] = AGAIN_WINDOWS_LOG + ('Restored the cache of build-0456 into W',)
        self.assertEqual(failures_of('again', 'windows-x64', downloaded), ["the log has a line with 'Restored the '"])

    def test_the_first_run_log_is_not_a_no_op(self) -> None:
        failures: list[str] = failures_of('again', 'windows-x64', FIRST_WINDOWS_LOG)
        self.assertIn("the log has no line with 'the product: updating '", failures)
        self.assertIn("the log has a line with ': cloning '", failures)

    def test_an_unpublished_cache_satisfies_both_runs(self) -> None:
        first: list[str] = []
        line: str
        for line in FIRST_WINDOWS_LOG:
            first.append(line if not line.startswith('Restored the cache') else 'No published gui-forms-cache-x')
        self.assertEqual(failures_of('first', 'windows-x64', tuple(first)), [])

    def test_a_run_that_did_not_finish_fails(self) -> None:
        unfinished: tuple[str, ...] = AGAIN_WINDOWS_LOG[:-1]
        self.assertEqual(failures_of('again', 'windows-x64', unfinished),
                         ["the log has no line with 'Bootstrap finished'"])

    def test_macos_needs_the_runtime_and_linux_needs_no_toolchain_line(self) -> None:
        unix: list[str] = []
        line: str
        for line in AGAIN_WINDOWS_LOG:
            if 'Windows toolchain' not in line:
                unix.append(line)
        self.assertEqual(failures_of('again', 'linux-x64', tuple(unix)), [])
        self.assertEqual(failures_of('again', 'macos-arm64', tuple(unix)),
                         ["the log has no line with ' matches its manifest'"])
        unix.append('The macOS runtime at W/.build/toolchain/llvm matches its manifest')
        self.assertEqual(failures_of('again', 'macos-arm64', tuple(unix)), [])


class WorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        environment: mock._patch_dict = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace: Path = Path(directory.name)
        harness: Path = self.workspace / bootstrap_proof.HARNESS_DIRECTORY
        harness.mkdir()
        git(harness, 'init', '--quiet', '--initial-branch=main')
        git(harness, 'commit', '--quiet', '--allow-empty', '-m', 'one')
        self.locked: str = git(harness, 'rev-parse', 'HEAD')
        git(harness, 'commit', '--quiet', '--allow-empty', '-m', 'two')
        git(harness, 'checkout', '--quiet', '--detach', self.locked)
        product: Path = self.workspace / 'gui_forms'
        product.mkdir()
        git(product, 'init', '--quiet', '--initial-branch=main')
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
        other: str = 'f' * 40
        self.assertEqual(len(self.failures(other)), 1)
        git(self.workspace / bootstrap_proof.HARNESS_DIRECTORY, 'checkout', '--quiet', 'main')
        failures: list[str] = self.failures(git(self.workspace / bootstrap_proof.HARNESS_DIRECTORY, 'rev-parse', 'HEAD'))
        self.assertEqual(len(failures), 1)
        self.assertIn('is on a branch', failures[0])

    def test_main_reports_each_failure_and_exits_1(self) -> None:
        (self.workspace / 'gui_forms' / lock.LOCK_NAME).write_text(
            '[harness]\nrepository = "owner/template_harness"\ncommit = "' + self.locked + '"\n', encoding='utf-8')
        log: Path = self.workspace / 'again.log'
        log.write_text('\n'.join(FIRST_WINDOWS_LOG) + '\n', encoding='utf-8')
        output: io.StringIO = io.StringIO()
        arguments: list[str] = ['again', '--log', str(log), '--workspace', str(self.workspace),
                                '--checkout', 'gui_forms', '--notes-branch', NOTES_BRANCH, '--platform', 'linux-x64']
        with contextlib.redirect_stdout(output):
            status: int = bootstrap_proof.main(arguments)
        self.assertEqual(status, 1)
        self.assertIn('FAILED: the log has no line with', output.getvalue())
        log.write_text('\n'.join(AGAIN_WINDOWS_LOG) + '\n', encoding='utf-8')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = bootstrap_proof.main(arguments)
        self.assertEqual(status, 0, output.getvalue())


if __name__ == '__main__':
    unittest.main()
