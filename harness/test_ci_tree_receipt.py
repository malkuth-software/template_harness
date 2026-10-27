"""A tree receipt must not turn a stale, partial or untrusted result into a skipped job."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
import zipfile

import harness_testing
import ci_tree_receipt


REPOSITORY: str = 'owner/gui_forms'
TREE: str = 'a' * 40
HEAD: str = 'b' * 40
MERGE: str = 'c' * 40
JOB: str = 'Sanitizers (Linux, ASan + UBSan + LSan)'


def zipped(receipt: dict[str, Any]) -> bytes:
    stream: io.BytesIO = io.BytesIO()
    archive: zipfile.ZipFile
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(ci_tree_receipt.RECEIPT_NAME, json.dumps(receipt))
    data: bytes = stream.getvalue()
    return data


class FakeGitHub(ci_tree_receipt.GitHub):
    """One producer run (id 10) with one receipt artifact; each test changes a field."""

    def __init__(self) -> None:
        super().__init__(REPOSITORY)
        self.receipt: dict[str, Any] = ci_tree_receipt.receipt_for('sanitize', TREE, HEAD, '10', 'everything')
        self.data: bytes = b''
        self.digest: str = ''
        self.run_record: dict[str, Any] = {
            'path': ci_tree_receipt.WORKFLOW, 'event': 'pull_request', 'head_sha': HEAD,
            'repository': {'full_name': REPOSITORY}, 'head_repository': {'full_name': REPOSITORY}}
        self.job_records: list[dict[str, Any]] = [{'name': JOB, 'conclusion': 'success'},
                                                  {'name': 'docs', 'conclusion': 'success'}]
        self.trees: dict[str, str] = {HEAD: TREE}
        self.parents: dict[str, list[str]] = {}
        self.producer: int = 10

    def seal(self) -> None:
        self.data = zipped(self.receipt)
        self.digest = 'sha256:' + hashlib.sha256(self.data).hexdigest()

    def artifacts(self, name: str) -> list[dict[str, Any]]:
        if not self.data:
            self.seal()
        artifact: dict[str, Any] = {'id': 7, 'name': name, 'expired': False, 'size_in_bytes': len(self.data),
                                    'digest': self.digest, 'workflow_run': {'id': self.producer}}
        return [artifact]

    def run(self, run_id: int) -> dict[str, Any]:
        return self.run_record

    def jobs(self, run_id: int) -> list[dict[str, Any]]:
        return self.job_records

    def commit(self, revision: str) -> dict[str, Any]:
        parents: list[dict[str, str]] = []
        parent: str
        for parent in self.parents.get(revision, []):
            parents.append({'sha': parent})
        commit: dict[str, Any] = {'tree': {'sha': self.trees.get(revision, 'd' * 40)}, 'parents': parents}
        return commit

    def download(self, artifact_id: int) -> bytes:
        return self.data


def found(github: FakeGitHub) -> int | None:
    receipt: dict[str, Any] | None = ci_tree_receipt.lookup(github, 'sanitize', JOB, TREE, 20)
    if receipt is None:
        return None
    producer: int = int(receipt['run_id'])
    return producer


class LookupTests(unittest.TestCase):
    def test_takes_an_older_trusted_run_that_passed_this_tree(self) -> None:
        self.assertEqual(found(FakeGitHub()), 10)

    def test_takes_a_merge_group_run_but_no_other_event(self) -> None:
        queued: FakeGitHub = FakeGitHub()
        queued.run_record['event'] = 'merge_group'
        self.assertEqual(found(queued), 10)
        other: FakeGitHub = FakeGitHub()
        other.run_record['event'] = 'pull_request_target'
        self.assertIsNone(found(other))

    def test_never_takes_this_run_or_a_newer_one(self) -> None:
        github: FakeGitHub = FakeGitHub()
        github.producer = 20
        github.receipt['run_id'] = '20'
        self.assertIsNone(found(github))

    def test_never_takes_a_fork_or_another_workflow(self) -> None:
        fork: FakeGitHub = FakeGitHub()
        fork.run_record['head_repository'] = {'full_name': 'someone/gui_forms'}
        self.assertIsNone(found(fork))
        other: FakeGitHub = FakeGitHub()
        other.run_record['path'] = '.github/workflows/release.yml'
        self.assertIsNone(found(other))

    def test_needs_the_named_job_to_have_succeeded(self) -> None:
        github: FakeGitHub = FakeGitHub()
        github.job_records[0]['conclusion'] = 'failure'
        self.assertIsNone(found(github))
        missing: FakeGitHub = FakeGitHub()
        missing.job_records = [{'name': 'docs', 'conclusion': 'success'}]
        self.assertIsNone(found(missing))

    def test_rejects_a_changed_artifact(self) -> None:
        github: FakeGitHub = FakeGitHub()
        github.seal()
        github.digest = 'sha256:' + '0' * 64
        self.assertIsNone(found(github))

    def test_rejects_a_receipt_for_another_tree_kind_or_run(self) -> None:
        field: str
        value: str
        for field, value in (('tree', 'e' * 40), ('kind', 'docs'), ('run_id', '9')):
            github: FakeGitHub = FakeGitHub()
            github.receipt[field] = value
            self.assertIsNone(found(github), field)

    def test_a_narrowed_run_is_taken_and_an_unknown_coverage_is_not(self) -> None:
        github: FakeGitHub = FakeGitHub()
        github.receipt['coverage'] = 'selected'
        receipt: dict[str, Any] | None = ci_tree_receipt.lookup(github, 'sanitize', JOB, TREE, 20)
        self.assertIsNotNone(receipt)
        self.assertEqual((receipt or {}).get('coverage'), 'selected')
        unknown: FakeGitHub = FakeGitHub()
        unknown.receipt['coverage'] = 'most'
        self.assertIsNone(found(unknown))

    def test_a_pull_request_merge_commit_must_merge_the_run_head(self) -> None:
        github: FakeGitHub = FakeGitHub()
        github.trees = {HEAD: 'f' * 40, MERGE: TREE}
        github.receipt['revision'] = MERGE
        github.parents = {MERGE: ['1' * 40, HEAD]}
        self.assertEqual(found(github), 10)
        stranger: FakeGitHub = FakeGitHub()
        stranger.trees = {HEAD: 'f' * 40, MERGE: TREE}
        stranger.receipt['revision'] = MERGE
        stranger.parents = {MERGE: ['1' * 40, '2' * 40]}
        self.assertIsNone(found(stranger))


class RecordTests(unittest.TestCase):
    def test_a_reused_job_records_the_run_it_carried(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            out: Path = Path(directory) / 'receipt'
            environment: dict[str, str] = {'GITHUB_OUTPUT': str(Path(directory) / 'output'), 'GITHUB_RUN_ID': '20'}
            with patch.dict(os.environ, environment), patch.object(ci_tree_receipt, 'git_field', return_value=TREE):
                self.assertEqual(ci_tree_receipt.main(['write', '--kind', 'sanitize', '--out', str(out),
                                                       '--build-record', str(Path(directory) / 'none.json'),
                                                       '--carried-from', '10', '--coverage', 'selected']), 0)
            receipt: dict[str, Any] = json.loads((out / ci_tree_receipt.RECEIPT_NAME).read_text(encoding='utf-8'))
            self.assertEqual(receipt['carried_from'], '10')
            self.assertEqual(receipt['coverage'], 'selected')
            self.assertEqual(receipt['run_id'], '20')
            self.assertEqual((Path(directory) / 'output').read_text(encoding='utf-8'),
                             'artifact=gui-tree-sanitize-' + TREE + '\n')

    def test_coverage_follows_the_build_record(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            record: Path = Path(directory) / 'gui-forms-sanitize.record.json'
            self.assertEqual(ci_tree_receipt.coverage_of_record(record), 'nothing')
            built: str
            for built in ('nothing', 'selected', 'everything'):
                record.write_text(json.dumps({'built': built, 'reason': 'test'}), encoding='utf-8')
                self.assertEqual(ci_tree_receipt.coverage_of_record(record), built)
            record.write_text(json.dumps({'built': 'all of it'}), encoding='utf-8')
            self.assertEqual(ci_tree_receipt.coverage_of_record(record), 'nothing')

    def test_a_failed_lookup_runs_the_job(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            output: Path = Path(directory) / 'output'
            environment: dict[str, str] = {'GITHUB_OUTPUT': str(output), 'GITHUB_RUN_ID': '20',
                                           'GITHUB_REPOSITORY': REPOSITORY}
            error: subprocess.CalledProcessError = subprocess.CalledProcessError(1, ['gh'])
            with patch.dict(os.environ, environment), patch.object(ci_tree_receipt, 'lookup', side_effect=error):
                self.assertEqual(ci_tree_receipt.main(['lookup', '--kind', 'docs', '--job', 'docs']), 0)
            self.assertEqual(output.read_text(encoding='utf-8'), 'reused=false\nproducer-run=\ncoverage=\n')


if __name__ == '__main__':
    unittest.main()
