#!/usr/bin/env python3
"""Promote a successful exact-tree artifact without installing another compiler.

The producer environment remains authoritative. This is publication of tested
bytes, not a claim that tests ran on the publishing runner's image.
"""
from __future__ import annotations
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from typing import Any, BinaryIO
import zipfile

import ci_reuse
import project


def find_validation(platform: str, seed: bool = False) -> bool:
    repository: str = os.environ['GITHUB_REPOSITORY']
    tree: str = ci_reuse.git_field('%T')
    prefix: str = project.current().artifact('validated-' + platform + '-' + tree + '-')
    current: dict[str, Any] = {}
    if seed:
        current = json.loads((ci_reuse.STATE / 'inputs.json').read_text())
        prefix = project.current().artifact('validated-' + platform + '-')
    endpoint: str = 'repos/' + repository + '/actions/artifacts'
    inspected: int = 0
    page: int
    # Bounded discovery; eviction or a busy repository falls back to validation.
    for page in range(1, 6):
        listing: dict[str, Any] = ci_reuse.api(endpoint + '?per_page=100&page=' + str(page))
        artifact: dict[str, Any]
        for artifact in listing['artifacts']:
            if not artifact['name'].startswith(prefix) or artifact['expired']:
                continue
            if artifact['size_in_bytes'] > 256 * 1024:
                continue
            if seed and inspected == 8:
                return False
            inspected += 1
            data: bytes = subprocess.check_output(
                ['gh', 'api', endpoint + '/' + str(artifact['id']) + '/zip', '--allow-escape-sequences'])
            receipt: dict[str, Any] = ci_reuse.receipt_from_zip(data, artifact.get('digest', ''))
            if receipt.get('schema') != 2:
                continue
            if receipt.get('platform') != platform:
                continue
            if not seed and receipt.get('tree') != tree:
                continue
            if seed and receipt.get('compiler') != current['compiler']:
                continue
            # Reuse the full trust gate: repository/workflow, actual Git tree,
            # completed platform job, receipt/payload digests and ownership.
            if not ci_reuse.lookup(receipt, 0):
                continue
            if seed:
                # This is only a ccache seed. Do not leave a reused-validation
                # marker, change current source identity, or skip native tests.
                (ci_reuse.STATE / 'reused.json').replace(ci_reuse.STATE / 'seed.json')
                print('Using verified objects only; current-source native tests remain required', flush=True)
                return True
            identity: dict[str, Any] = {
                'schema': 2, 'tree': tree, 'platform': platform,
                'revision': ci_reuse.git_field('%H'), 'run_id': os.environ['GITHUB_RUN_ID'],
                'compiler': receipt['compiler'], 'environment': receipt['environment']}
            ci_reuse.write_json(ci_reuse.STATE / 'inputs.json', identity)
            ci_reuse.write_json(ci_reuse.STATE / 'promotion.json', {'producer_environment': receipt['environment']})
            return True
        if len(listing['artifacts']) < 100:
            break
    return False


def promoted_manifest(manifest: dict[str, Any], receipt: dict[str, Any], identity: dict[str, Any]) -> bytes:
    if (manifest['revision'] != receipt['revision'] or manifest['compiler'] != receipt['compiler']
            or manifest['platform'] != identity['platform']):
        raise ValueError('Promoted manifest does not match validated producer')
    manifest['revision'] = identity['revision']
    manifest['run_id'] = identity['run_id']
    manifest['validation'] = 'promoted successful provider validation for the identical complete Git tree'
    manifest['validation_provenance'] = {
        'tree': receipt['tree'], 'run_id': receipt['validation_run'],
        'revision': receipt['validation_revision'], 'receipt_run_id': receipt['run_id'],
        'environment': receipt['environment']}
    # Compiler, SDK, runtime and runner fields describe the original producer.
    text: str = json.dumps(manifest, indent=2) + '\n'
    data: bytes = text.encode('utf-8')
    return data


def promote_archive() -> None:
    identity: dict[str, Any] = json.loads((ci_reuse.STATE / 'inputs.json').read_text())
    receipt: dict[str, Any] = json.loads((ci_reuse.STATE / 'reused.json').read_text())
    filename: str = project.current().cache_asset(identity['platform'])
    source: Path = ci_reuse.STATE / 'download' / filename
    ci_reuse.verified_archive(source, receipt)
    destination: Path = ci_reuse.ROOT / filename
    temporary: Path = destination.with_suffix('.partial')
    manifest_count: int = 0
    try:
        with tarfile.open(source, 'r:gz') as original, tarfile.open(temporary, 'w:gz', compresslevel=1) as promoted:
            entry: tarfile.TarInfo
            for entry in original:
                stream: BinaryIO | None = None
                if entry.isfile():
                    stream = original.extractfile(entry)
                if entry.name == 'cache-manifest.json':
                    if stream is None or entry.size > 1024 * 1024:
                        raise ValueError('Invalid promoted manifest')
                    manifest_count += 1
                    manifest: dict[str, Any] = json.load(stream)
                    stream.close()
                    data: bytes = promoted_manifest(manifest, receipt, identity)
                    entry.size = len(data)
                    stream = io.BytesIO(data)
                try:
                    # No extraction; object/runtime bytes and link targets stay
                    # unchanged. Only the commit/provenance manifest is rewritten.
                    promoted.addfile(entry, stream)
                finally:
                    if stream is not None:
                        stream.close()
            if manifest_count != 1:
                raise ValueError('Expected exactly one cache manifest')
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def restore_seed() -> None:
    """Restore a seed's objects, or build without them when its archive cannot be verified.

    A seed only fills ccache, which checks every hit, and every test still
    runs, so an archive that fails its digest even after the downloads
    verified_archive retries is skipped with a warning instead of failing the
    job. Nothing is extracted from an archive that failed.
    """
    identity: dict[str, Any] = json.loads((ci_reuse.STATE / 'inputs.json').read_text())
    try:
        ci_reuse.restore_archive(identity, ci_reuse.STATE / 'seed.json')
    except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        print('::warning::Seed objects not restored (' + str(error) + '); building from the restored cache',
              flush=True)


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('lookup', 'archive', 'seed', 'restore-seed'))
    parser.add_argument('--platform')
    args: argparse.Namespace = parser.parse_args()
    if args.command == 'archive':
        promote_archive()
        return
    if args.command == 'restore-seed':
        restore_seed()
        return
    found: bool = False
    seed: bool = args.command == 'seed'
    try:
        found = find_validation(args.platform, seed)
    except (subprocess.CalledProcessError, ValueError, KeyError, OSError, zipfile.BadZipFile) as error:
        print('Promotion unavailable; run native validation: ' + str(error), flush=True)
    field: str = 'reused'
    if seed:
        field = 'seeded'
    ci_reuse.output(field, str(found).lower())


if __name__ == '__main__':
    main()
