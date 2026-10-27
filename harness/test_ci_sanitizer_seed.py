"""The sanitizer seed comes only from earlier same-repository runs of the nearest branch."""
from __future__ import annotations
import hashlib
import io
import os
from pathlib import Path
import tarfile
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import harness_testing
import ci_sanitizer_seed


def seed_artifact(identifier: int, run: int, branch: str, expired: bool = False, fork: bool = False) -> dict[str, Any]:
    head_repository: int = 2 if fork else 1
    return {'id': identifier, 'name': ci_sanitizer_seed.ARTIFACT_NAME, 'expired': expired,
            'workflow_run': {'id': run, 'head_branch': branch, 'repository_id': 1,
                             'head_repository_id': head_repository}}


class CandidateBranchTests(unittest.TestCase):
    def test_a_pull_request_on_main_seeds_from_its_own_earlier_pushes(self) -> None:
        self.assertEqual(ci_sanitizer_seed.candidate_branches('pull_request', 'claude/a', 'main', []),
                         ['claude/a'])

    def test_a_stacked_pull_request_falls_back_to_its_base(self) -> None:
        self.assertEqual(ci_sanitizer_seed.candidate_branches('pull_request', 'claude/b', 'claude/a', []),
                         ['claude/b', 'claude/a'])

    def test_a_push_to_main_seeds_from_the_merged_heads(self) -> None:
        self.assertEqual(ci_sanitizer_seed.candidate_branches('push', '', '', ['claude/a', 'main', 'claude/a', 'x']),
                         ['claude/a', 'x'])

    def test_a_merge_group_seeds_from_its_queued_pull_requests_head(self) -> None:
        self.assertEqual(ci_sanitizer_seed.candidate_branches('merge_group', '', '', ['claude/a']), ['claude/a'])

    def test_a_merge_groups_ref_names_its_pull_request(self) -> None:
        base: str = '0123456789abcdef0123456789abcdef01234567'
        self.assertEqual(ci_sanitizer_seed.queued_pull_request('refs/heads/gh-readonly-queue/main/pr-361-' + base),
                         '361')
        self.assertEqual(ci_sanitizer_seed.queued_pull_request(
            'refs/heads/gh-readonly-queue/release/1.0/pr-7-' + base), '7')
        self.assertEqual(ci_sanitizer_seed.queued_pull_request('refs/heads/main'), '')
        self.assertEqual(ci_sanitizer_seed.queued_pull_request('refs/heads/gh-readonly-queue/main/pr-7-xyz'), '')

    def test_a_queued_pull_request_from_a_fork_has_no_seed_branch(self) -> None:
        ref: str = 'refs/heads/gh-readonly-queue/main/pr-7-' + 'a' * 40
        own: dict[str, Any] = {'head': {'ref': 'claude/a', 'repo': {'full_name': 'owner/repo'}}}
        with patch.object(ci_sanitizer_seed, 'api', return_value=own):
            self.assertEqual(ci_sanitizer_seed.queued_head_branches('owner/repo', ref), ['claude/a'])
        fork: dict[str, Any] = {'head': {'ref': 'claude/a', 'repo': {'full_name': 'someone/repo'}}}
        with patch.object(ci_sanitizer_seed, 'api', return_value=fork):
            self.assertEqual(ci_sanitizer_seed.queued_head_branches('owner/repo', ref), [])
        self.assertEqual(ci_sanitizer_seed.queued_head_branches('owner/repo', 'refs/heads/main'), [])

    def test_nothing_else_has_candidates(self) -> None:
        self.assertEqual(ci_sanitizer_seed.candidate_branches('workflow_dispatch', '', '', []), [])


class ChooseArtifactTests(unittest.TestCase):
    def test_the_newest_earlier_run_of_the_branch_wins(self) -> None:
        artifacts: list[dict[str, Any]] = [seed_artifact(1, 10, 'claude/a'), seed_artifact(2, 30, 'claude/a'),
                                           seed_artifact(3, 20, 'claude/a'), seed_artifact(4, 40, 'claude/b')]
        chosen: dict[str, Any] | None = ci_sanitizer_seed.choose_artifact(artifacts, 'claude/a', 50)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen['id'], 2)

    def test_expired_forked_current_and_later_runs_are_refused(self) -> None:
        artifacts: list[dict[str, Any]] = [seed_artifact(1, 10, 'claude/a', expired=True),
                                           seed_artifact(2, 11, 'claude/a', fork=True),
                                           seed_artifact(3, 50, 'claude/a'), seed_artifact(4, 60, 'claude/a')]
        self.assertIsNone(ci_sanitizer_seed.choose_artifact(artifacts, 'claude/a', 50))

    def test_other_artifact_names_are_ignored(self) -> None:
        artifact: dict[str, Any] = seed_artifact(1, 10, 'claude/a')
        artifact['name'] = 'gui-forms-cache-linux-x64'
        self.assertIsNone(ci_sanitizer_seed.choose_artifact([artifact], 'claude/a', 50))


def archive_with(names: list[str]) -> bytes:
    buffer: io.BytesIO = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as payload:
        name: str
        for name in names:
            data: bytes = b'object'
            info: tarfile.TarInfo = tarfile.TarInfo(name)
            info.size = len(data)
            payload.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class RestoreTests(unittest.TestCase):
    def restore(self, root: Path, names: list[str], digest: str | None = None, artifact_id: int = 0) -> bool:
        download: Path = root / '.ci' / 'sanitizer-seed'
        download.mkdir(parents=True)
        data: bytes = archive_with(names)
        (download / ci_sanitizer_seed.ARCHIVE_NAME).write_bytes(data)
        if digest is None:
            digest = hashlib.sha256(data).hexdigest()
        (download / ci_sanitizer_seed.DIGEST_NAME).write_text(digest, encoding='utf-8')
        with patch.object(ci_sanitizer_seed, 'ROOT', root), patch.object(ci_sanitizer_seed, 'DOWNLOAD', download):
            restored: bool = ci_sanitizer_seed.restore_seed(artifact_id)
        return restored

    def test_the_seed_replaces_the_cache(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            (root / '.ccache').mkdir()
            (root / '.ccache' / 'stale').write_bytes(b'old')
            self.assertTrue(self.restore(root, ['.ccache/a/b']))
            self.assertTrue((root / '.ccache' / 'a' / 'b').is_file())
            self.assertFalse((root / '.ccache' / 'stale').exists())

    def test_members_outside_the_cache_skip_the_seed_before_anything_is_removed(self) -> None:
        name: str
        for name in ('outside', '.ccache/../escape', '/absolute'):
            directory: str
            with tempfile.TemporaryDirectory() as directory:
                root: Path = Path(directory)
                (root / '.ccache').mkdir()
                (root / '.ccache' / 'kept').write_bytes(b'old')
                self.assertFalse(self.restore(root, ['.ccache/a', name]), name)
                self.assertTrue((root / '.ccache' / 'kept').is_file(), name)

    def test_a_cut_short_seed_is_fetched_again_then_skipped(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            (root / '.ccache').mkdir()
            (root / '.ccache' / 'kept').write_bytes(b'old')
            downloads: list[int] = []

            def download_nothing_better(artifact_id: int) -> None:
                downloads.append(artifact_id)

            with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}),                     patch.object(ci_sanitizer_seed, 'download_seed', side_effect=download_nothing_better):
                self.assertFalse(self.restore(root, ['.ccache/a'], digest='0' * 64, artifact_id=7))
            self.assertEqual(downloads, [7] * ci_sanitizer_seed.DOWNLOADS)
            self.assertTrue((root / '.ccache' / 'kept').is_file())


if __name__ == '__main__':
    unittest.main()
