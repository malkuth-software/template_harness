"""Publication reuses completed validation, never an unrelated or pending build."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import harness_testing
import ci_promote
import ci_reuse
from test_ci_reuse import FixtureAPI, zipped_receipt


class PromotionAPI(FixtureAPI):
    def __init__(self) -> None:
        super().__init__()
        self.receipt.update(validation_run='10', validation_revision='commit')
        self.data = zipped_receipt(self.receipt)

    def request(self, endpoint: str) -> Any:
        if 'artifacts?per_page=100&page=' in endpoint:
            return {'artifacts': [{'id': 5, 'name': 'gui-validated-linux-x64-tree-environment',
                'expired': self.expired, 'size_in_bytes': len(self.data),
                'digest': 'sha256:' + hashlib.sha256(self.data).hexdigest()}]}
        result: Any = super().request(endpoint)
        return result

    def git_field(self, format_code: str) -> str:
        if format_code == '%T':
            return 'tree'
        return 'merge-commit'


class PromotionTests(unittest.TestCase):
    def discover(self, fixture: PromotionAPI, root: Path, seed: bool = False) -> bool:
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '20'}), \
             patch.object(ci_reuse, 'STATE', root), \
             patch.object(ci_reuse, 'api', side_effect=fixture.request), \
             patch.object(ci_reuse, 'git_field', side_effect=fixture.git_field), \
             patch.object(ci_reuse.subprocess, 'check_output', return_value=fixture.data), \
             patch.object(ci_reuse, 'output'):
            return ci_promote.find_validation('linux-x64', seed)

    def test_seed_keeps_new_tree_and_never_reuses_native_validation(self) -> None:
        fixture: PromotionAPI = PromotionAPI()
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            identity: dict[str, Any] = fixture.identity.copy()
            identity['tree'] = 'new-source-tree'
            ci_reuse.write_json(root / 'inputs.json', identity)
            self.assertTrue(self.discover(fixture, root, seed=True))
            self.assertTrue((root / 'seed.json').is_file())
            self.assertFalse((root / 'reused.json').exists())
            self.assertFalse((root / 'promotion.json').exists())
            self.assertEqual(json.loads((root / 'inputs.json').read_text()), identity)

    def test_seed_requires_matching_compiler(self) -> None:
        fixture: PromotionAPI = PromotionAPI()
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            ci_reuse.write_json(root / 'inputs.json', {'compiler': 'other-compiler'})
            self.assertFalse(self.discover(fixture, root, seed=True))

    def test_legacy_receipts_do_not_trigger_expensive_provenance_lookup(self) -> None:
        fixture: PromotionAPI = PromotionAPI()
        fixture.receipt['schema'] = 1
        fixture.data = zipped_receipt(fixture.receipt)
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            ci_reuse.write_json(root / 'inputs.json', fixture.identity)
            with patch.object(ci_reuse, 'lookup') as lookup:
                self.assertFalse(self.discover(fixture, root, seed=True))
                lookup.assert_not_called()

    def test_promotes_without_publishing_runner_compiler_or_environment(self) -> None:
        fixture: PromotionAPI = PromotionAPI()
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            self.assertTrue(self.discover(fixture, root))
            identity: dict[str, Any] = json.loads((root / 'inputs.json').read_text())
            self.assertEqual(identity['revision'], 'merge-commit')
            self.assertEqual(identity['environment'], 'environment')
            self.assertEqual(identity['compiler'], 'compiler')
            self.assertEqual(identity['run_id'], '20')

    def test_failed_job_wrong_tree_fork_and_expired_are_not_promoted(self) -> None:
        case: str
        for case in ('failed', 'tree', 'fork', 'expired'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                fixture: PromotionAPI = PromotionAPI()
                if case == 'failed':
                    fixture.job['conclusion'] = 'failure'
                if case == 'tree':
                    fixture.tree = 'changed'
                if case == 'fork':
                    fixture.origin['head_repository_id'] = 2
                if case == 'expired':
                    fixture.expired = True
                self.assertFalse(self.discover(fixture, Path(directory)))

    def test_repack_changes_only_manifest_and_preserves_producer_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            state: Path = root / '.ci'
            download: Path = state / 'download'
            download.mkdir(parents=True)
            fixture: PromotionAPI = PromotionAPI()
            receipt: dict[str, Any] = fixture.receipt.copy()
            identity: dict[str, Any] = fixture.identity.copy()
            identity.update(revision='merge-commit', run_id='20')
            manifest: dict[str, Any] = {'revision': 'commit', 'compiler': 'compiler', 'platform': 'linux-x64',
                'runner_image_version': 'producer-image', 'compiler_sha256': 'compiler-bytes'}
            archive: Path = download / 'gui-forms-cache-linux-x64.tar.gz'
            with tarfile.open(archive, 'w:gz') as payload:
                entry: tarfile.TarInfo = tarfile.TarInfo('cache-manifest.json')
                data: bytes = json.dumps(manifest).encode()
                entry.size = len(data)
                payload.addfile(entry, io.BytesIO(data))
                entry = tarfile.TarInfo('.ccache/object')
                entry.size = 5
                payload.addfile(entry, io.BytesIO(b'bytes'))
            receipt['archive_sha256'] = ci_reuse.file_digest(archive)
            ci_reuse.write_json(state / 'inputs.json', identity)
            ci_reuse.write_json(state / 'reused.json', receipt)
            with patch.object(ci_reuse, 'ROOT', root), patch.object(ci_reuse, 'STATE', state):
                ci_promote.promote_archive()
                with tarfile.open(root / archive.name) as promoted:
                    stream: Any = promoted.extractfile('cache-manifest.json')
                    updated: dict[str, Any] = json.load(stream)
                    self.assertEqual(updated['revision'], 'merge-commit')
                    self.assertEqual(updated['runner_image_version'], 'producer-image')
                    self.assertEqual(updated['validation_provenance']['run_id'], '10')
                    self.assertEqual(promoted.extractfile('.ccache/object').read(), b'bytes')
                corrupted: bytes = archive.read_bytes() + b'corruption'
                archive.write_bytes(corrupted)
                downloads: list[int] = []

                def download_corrupted(artifact_id: int, target: Path) -> None:
                    downloads.append(artifact_id)
                    target.write_bytes(corrupted)

                # A mismatch is downloaded again from the receipt's artifact, then refused.
                with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}),                         patch.object(ci_reuse, 'download_archive', side_effect=download_corrupted):
                    with self.assertRaisesRegex(ValueError, 'digest mismatch'):
                        ci_promote.promote_archive()
                self.assertEqual(len(downloads), ci_reuse.ARCHIVE_DOWNLOADS)

    def test_manifest_must_match_receipt(self) -> None:
        fixture: PromotionAPI = PromotionAPI()
        manifest: dict[str, Any] = {'revision': 'wrong', 'compiler': 'compiler', 'platform': 'linux-x64'}
        with self.assertRaisesRegex(ValueError, 'match validated producer'):
            ci_promote.promoted_manifest(manifest, fixture.receipt, fixture.identity)



class SeedRestoreTests(unittest.TestCase):
    def test_an_unverifiable_seed_is_skipped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state: Path = Path(directory)
            ci_reuse.write_json(state / 'inputs.json', {'platform': 'linux-x64'})
            with patch.object(ci_reuse, 'STATE', state),                     patch.object(ci_reuse, 'restore_archive',
                                 side_effect=ValueError('Reused cache archive digest mismatch')):
                ci_promote.restore_seed()


if __name__ == '__main__':
    unittest.main()
