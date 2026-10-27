"""Unit tests for bootstrap_proof.py: the log rules, the snapshot, and the workspace checks on local repositories."""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import harness_testing
import bootstrap_proof
import lock
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
        self.assertEqual(bootstrap_proof.first_failures(list(FIRST_WINDOWS_LOG), 'windows-x64', NOTES_BRANCH), [])
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
        self.assertEqual(bootstrap_proof.first_failures(list(first), 'windows-x64', NOTES_BRANCH), [])
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
