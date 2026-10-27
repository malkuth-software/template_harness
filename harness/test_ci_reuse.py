"""Validation reuse must not turn a stale or untrusted result into a green job."""
from __future__ import annotations
import dataclasses
import hashlib
import io
import json
import os
import subprocess
from pathlib import Path
import tempfile
import tarfile
from typing import Any
import unittest
from unittest.mock import patch
import zipfile

import harness_testing
import ci_reuse
import project


def fixture_identity() -> dict[str, Any]:
    return {'schema': 2, 'tree': 'tree', 'compiler': 'compiler', 'environment': 'environment',
            'platform': 'linux-x64', 'revision': 'commit', 'run_id': '10'}


def zipped_receipt(receipt: dict[str, Any]) -> bytes:
    stream: io.BytesIO = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('receipt.json', json.dumps(receipt))
    return stream.getvalue()


class FixtureAPI:
    def __init__(self) -> None:
        self.identity: dict[str, Any] = fixture_identity()
        self.receipt: dict[str, Any] = self.identity.copy()
        self.receipt.update(artifact_id=9, archive_sha256='a' * 64)
        self.data: bytes = zipped_receipt(self.receipt)
        self.origin: dict[str, Any] = {'id': 10, 'repository_id': 1, 'head_repository_id': 1}
        self.run: dict[str, Any] = {'path': '.github/workflows/native.yml', 'head_sha': 'commit',
            'event': 'pull_request', 'head_repository': {'full_name': 'owner/repo'}}
        self.job: dict[str, Any] = {'name': 'native (ubuntu-24.04, linux-x64, bash)',
            'conclusion': 'success', 'status': 'completed'}
        self.tree: str = 'tree'
        self.expired: bool = False
        self.payload_run: int = 10
        self.calls: list[str] = []
        self.pending: bool = False
        # Commits other than the run's head, by revision: (tree, parent revisions).
        self.commits: dict[str, tuple[str, list[str]]] = {}

    def request(self, endpoint: str) -> Any:
        self.calls.append(endpoint)
        if 'artifacts?per_page=' in endpoint:
            if ('gui-inputs-' in endpoint) != self.pending:
                return {'artifacts': []}
            return {'artifacts': [{'id': 5, 'workflow_run': self.origin,
                'expired': self.expired, 'size_in_bytes': len(self.data),
                'digest': 'sha256:' + hashlib.sha256(self.data).hexdigest()}]}
        if '/git/commits/' in endpoint:
            revision: str = endpoint.rpartition('/')[2]
            if revision in self.commits:
                tree: str
                parents: list[str]
                tree, parents = self.commits[revision]
                parent_records: list[dict[str, str]] = []
                parent: str
                for parent in parents:
                    parent_records.append({'sha': parent})
                return {'tree': {'sha': tree}, 'parents': parent_records}
            return {'tree': {'sha': self.tree}, 'parents': []}
        if '/jobs?' in endpoint:
            return {'jobs': [self.job]}
        if endpoint.endswith('/runs/10'):
            return self.run
        if endpoint.endswith('/artifacts/9'):
            return {'expired': False, 'workflow_run': {'id': self.payload_run},
                    'name': 'gui-forms-cache-linux-x64'}
        raise AssertionError(endpoint)

    def complete(self, seconds: float) -> None:
        self.pending = False
        self.job['conclusion'] = 'success'
        self.job['status'] = 'completed'


class ReuseTests(unittest.TestCase):
    def test_git_fingerprints_match_raw_commit_without_revision_operators(self) -> None:
        commit: str = subprocess.check_output(
            ['git', '-C', str(ci_reuse.SOURCE), 'cat-file', '-p', 'HEAD'], text=True)
        tree: str = commit.splitlines()[0].removeprefix('tree ')
        self.assertEqual(ci_reuse.git_field('%T'), tree)
        revision: str = subprocess.check_output(
            ['git', '-C', str(ci_reuse.SOURCE), 'rev-parse', 'HEAD'], text=True).strip()
        self.assertEqual(ci_reuse.git_field('%H'), revision)

    def lookup(self, fixture: FixtureAPI) -> bool:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '20'}), \
                 patch.object(ci_reuse, 'STATE', Path(directory)), \
                 patch.object(ci_reuse, 'api', side_effect=fixture.request), \
                 patch.object(ci_reuse.subprocess, 'check_output', return_value=fixture.data), \
                 patch.object(ci_reuse, 'output'):
                return ci_reuse.lookup(fixture.identity, 0)

    def test_successful_same_tree_other_commit_can_be_reused(self) -> None:
        fixture: FixtureAPI = FixtureAPI()
        fixture.identity['revision'] = 'merge-commit'
        self.assertTrue(self.lookup(fixture))

    def test_a_job_of_the_per_platform_reusable_workflow_is_found(self) -> None:
        # native.yml calls native-platform.yml once per platform; the job's name
        # is the calling job's, then " / build".
        fixture: FixtureAPI = FixtureAPI()
        fixture.job['name'] = 'native (ubuntu-24.04, linux-x64, bash) / build'
        self.assertTrue(self.lookup(fixture))

    def test_another_platforms_job_is_not_taken(self) -> None:
        fixture: FixtureAPI = FixtureAPI()
        fixture.job['name'] = 'native (ubuntu-24.04-arm, linux-arm64, bash) / build'
        self.assertFalse(self.lookup(fixture))

    def test_a_matrix_job_named_by_its_platform_is_found_through_a_template(self) -> None:
        # A product whose own matrix names each native job by its platform
        # (${{ matrix.label || matrix.platform }}) sets native_job = "{platform}".
        template: project.Project = dataclasses.replace(project.current(), native_job='{platform}')
        fixture: FixtureAPI = FixtureAPI()
        fixture.job['name'] = 'linux-x64'
        with patch.object(ci_reuse.project, 'current', return_value=template):
            self.assertTrue(self.lookup(fixture))
            other: FixtureAPI = FixtureAPI()
            other.job['name'] = 'linux-arm64'
            self.assertFalse(self.lookup(other))
        # The prefix form does not take a bare platform name.
        self.assertFalse(self.lookup(fixture))

    def test_changed_receipt_inputs_are_rejected(self) -> None:
        key: str
        for key in ('tree', 'compiler', 'environment', 'platform'):
            with self.subTest(key=key):
                identity: dict[str, Any] = fixture_identity()
                receipt: dict[str, Any] = identity.copy()
                receipt.update(artifact_id=9, archive_sha256='a' * 64)
                receipt[key] = 'different'
                self.assertFalse(ci_reuse.matching_receipt(receipt, identity, 10))

    def test_changed_actual_tree_is_rejected_even_with_forged_receipt(self) -> None:
        fixture: FixtureAPI = FixtureAPI()
        fixture.tree = 'different'
        self.assertFalse(self.lookup(fixture))

    def test_a_merge_commit_of_the_producer_head_vouches_for_its_tree(self) -> None:
        # A branch behind its base: the producer checked out the test merge
        # commit, whose tree is not the head's.
        fixture: FixtureAPI = FixtureAPI()
        fixture.tree = 'head-tree'
        fixture.receipt['revision'] = 'merge'
        fixture.data = zipped_receipt(fixture.receipt)
        fixture.commits['merge'] = ('tree', ['base', 'commit'])
        self.assertTrue(self.lookup(fixture))

    def test_a_merge_commit_without_the_head_or_with_another_tree_is_rejected(self) -> None:
        case: tuple[str, list[str]]
        for case in (('tree', ['base', 'other-head']), ('other-tree', ['base', 'commit'])):
            with self.subTest(case=case):
                fixture: FixtureAPI = FixtureAPI()
                fixture.tree = 'head-tree'
                fixture.receipt['revision'] = 'merge'
                fixture.data = zipped_receipt(fixture.receipt)
                fixture.commits['merge'] = case
                self.assertFalse(self.lookup(fixture))

    def test_a_merge_group_run_is_trusted_and_another_event_is_not(self) -> None:
        fixture: FixtureAPI = FixtureAPI()
        fixture.run['event'] = 'merge_group'
        self.assertTrue(self.lookup(fixture))
        fixture = FixtureAPI()
        fixture.run['event'] = 'pull_request_target'
        self.assertFalse(self.lookup(fixture))

    def test_foreign_workflow_and_fork_are_rejected(self) -> None:
        fixture: FixtureAPI = FixtureAPI()
        fixture.run['path'] = '.github/workflows/other.yml'
        self.assertFalse(self.lookup(fixture))
        fixture = FixtureAPI()
        fixture.origin['head_repository_id'] = 2
        self.assertFalse(self.lookup(fixture))
        fixture = FixtureAPI()
        fixture.run['head_repository']['full_name'] = 'attacker/fork'
        self.assertFalse(self.lookup(fixture))

    def test_failed_pending_and_expired_results_are_rejected(self) -> None:
        conclusion: str | None
        for conclusion in ('failure', 'cancelled', None):
            fixture: FixtureAPI = FixtureAPI()
            fixture.job['conclusion'] = conclusion
            self.assertFalse(self.lookup(fixture))
        fixture = FixtureAPI()
        fixture.expired = True
        self.assertFalse(self.lookup(fixture))

    def test_payload_must_belong_to_validated_run(self) -> None:
        fixture: FixtureAPI = FixtureAPI()
        fixture.payload_run = 99
        self.assertFalse(self.lookup(fixture))

    def test_no_wait_cycle_or_self_reuse(self) -> None:
        producer: int
        for producer in (20, 21):
            fixture: FixtureAPI = FixtureAPI()
            fixture.origin['id'] = producer
            self.assertFalse(self.lookup(fixture))
            self.assertFalse(any('/runs/' in endpoint for endpoint in fixture.calls))

    def test_corrupted_receipt_zip_is_rejected(self) -> None:
        data: bytes = zipped_receipt(fixture_identity())
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            ci_reuse.receipt_from_zip(data, 'sha256:' + '0' * 64)

    def test_waits_for_older_identical_job_then_reuses_it(self) -> None:
        fixture: FixtureAPI = FixtureAPI()
        fixture.pending = True
        fixture.job.update(status='in_progress', conclusion=None)
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '20'}), \
                 patch.object(ci_reuse, 'STATE', Path(directory)), \
                 patch.object(ci_reuse, 'api', side_effect=fixture.request), \
                 patch.object(ci_reuse.subprocess, 'check_output', return_value=fixture.data), \
                 patch.object(ci_reuse, 'output'), \
                 patch.object(ci_reuse.time, 'sleep', side_effect=fixture.complete) as sleeper:
                self.assertTrue(ci_reuse.lookup(fixture.identity, 60))
                sleeper.assert_called_once()

    def test_bounded_receipt_read(self) -> None:
        data: bytes = zipped_receipt({'oversized': 'x' * (256 * 1024)})
        with self.assertRaisesRegex(ValueError, 'bound'):
            ci_reuse.receipt_from_zip(data, 'sha256:' + hashlib.sha256(data).hexdigest())

    def test_archive_restore_checks_digest_manifest_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            state: Path = root / '.ci'
            (state / 'download').mkdir(parents=True)
            archive: Path = state / 'download/gui-forms-cache-linux-x64.tar.gz'
            manifest: dict[str, str] = {'revision': 'commit', 'compiler': 'compiler', 'platform': 'linux-x64'}
            data: bytes = json.dumps(manifest).encode()
            entry: tarfile.TarInfo = tarfile.TarInfo('cache-manifest.json')
            entry.size = len(data)
            with tarfile.open(archive, 'w:gz') as payload:
                payload.addfile(entry, io.BytesIO(data))
            receipt: dict[str, Any] = fixture_identity()
            receipt.update(archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
            ci_reuse.write_json(state / 'reused.json', receipt)
            with patch.object(ci_reuse, 'ROOT', root), patch.object(ci_reuse, 'STATE', state):
                ci_reuse.restore_archive(fixture_identity())
                self.assertEqual(json.loads((root / 'cache-manifest.json').read_text()), manifest)
                archive.write_bytes(archive.read_bytes() + b'corrupt')
                with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                    ci_reuse.restore_archive(fixture_identity())
                with tarfile.open(archive, 'w:gz') as payload:
                    entry.name = '../escape'
                    payload.addfile(entry, io.BytesIO(data))
                receipt['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
                ci_reuse.write_json(state / 'reused.json', receipt)
                with self.assertRaisesRegex(ValueError, 'Unexpected cache archive member'):
                    ci_reuse.restore_archive(fixture_identity())


    def test_a_cut_short_download_is_fetched_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive: Path = Path(directory) / 'gui-forms-cache-linux-x64.tar.gz'
            whole: bytes = b'complete archive bytes'
            archive.write_bytes(whole[:8])
            receipt: dict[str, Any] = {'artifact_id': 9, 'archive_sha256': hashlib.sha256(whole).hexdigest()}
            downloads: list[int] = []

            def download_whole(artifact_id: int, target: Path) -> None:
                downloads.append(artifact_id)
                target.write_bytes(whole)

            with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}),                     patch.object(ci_reuse, 'download_archive', side_effect=download_whole):
                ci_reuse.verified_archive(archive, receipt)
            self.assertEqual(downloads, [9])
            self.assertEqual(archive.read_bytes(), whole)
            del receipt['artifact_id']
            archive.write_bytes(whole[:8])
            with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                ci_reuse.verified_archive(archive, receipt)


if __name__ == '__main__':
    unittest.main()
