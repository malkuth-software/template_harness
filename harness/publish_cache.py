#!/usr/bin/env python3
"""Publish one tested platform without waiting for other matrix jobs."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from ci_reuse import api, file_digest
import project
import windows_toolchain


def ensure_release(repository: str, revision: str) -> str:
    tag: str = 'build-' + revision
    command: list[str] = ['gh', 'release', 'view', tag, '--repo', repository]
    exists: subprocess.CompletedProcess[str] = subprocess.run(command, capture_output=True, text=True)
    if exists.returncode != 0:
        notes: str = ('Platform archives appear independently after their native validation passes. '
                      'An absent platform is not yet published. Source: ' + revision + '. '
                      'Each archive contains its producer and validation provenance. '
                      'These are build inputs, not an application release.')
        created: subprocess.CompletedProcess[str] = subprocess.run(
            ['gh', 'release', 'create', tag, '--repo', repository, '--target', revision,
             '--prerelease', '--title', 'Compiler cache ' + revision[:7], '--notes', notes],
            capture_output=True, text=True)
        if created.returncode != 0:
            # Another platform may have won the creation race. Only an existing
            # release with the exact source tag permits continuing.
            subprocess.run(command, check=True)
    reference: dict[str, Any] = api('repos/' + repository + '/git/ref/tags/' + tag)
    if reference['object']['type'] != 'commit' or reference['object']['sha'] != revision:
        raise ValueError('Build release tag does not name the tested source commit')
    return tag


def publish(repository: str, revision: str, archive: Path) -> None:
    if not archive.is_file() or archive.stat().st_size == 0:
        raise ValueError('Missing platform archive: ' + archive.name)
    tag: str = ensure_release(repository, revision)
    uploaded: subprocess.CompletedProcess[str] = subprocess.run(
        ['gh', 'release', 'upload', tag, str(archive), '--repo', repository], capture_output=True, text=True)
    if uploaded.returncode == 0:
        return
    # Reruns never silently replace a published asset. Accept only equal bytes.
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(['gh', 'release', 'download', tag, '--repo', repository,
                        '--pattern', archive.name, '--dir', directory], check=True)
        existing: Path = Path(directory) / archive.name
        if file_digest(existing) != file_digest(archive):
            raise ValueError('Refusing to replace a published archive with different bytes')


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True)
    args: argparse.Namespace = parser.parse_args()
    identity: dict[str, Any] = json.loads(Path('.ci/inputs.json').read_text())
    if identity['platform'] != args.platform or identity['revision'] != os.environ['GITHUB_SHA']:
        raise ValueError('Publication identity mismatch')
    archives: list[Path] = []
    if args.platform == windows_toolchain.PLATFORM:
        # Publish the toolchain first: a consumer never sees a Windows cache
        # without the exact compiler tree that filled it.
        archives.extend(windows_toolchain.publication_files(Path('.')))
    archives.append(Path(project.current().cache_asset(args.platform)))
    archive: Path
    for archive in archives:
        publish(os.environ['GITHUB_REPOSITORY'], identity['revision'], archive)


if __name__ == '__main__':
    main()
