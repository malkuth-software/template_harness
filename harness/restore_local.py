#!/usr/bin/env python3
"""Restore the pinned toolchain and the newest usable compiler cache into a local workspace.

CI restores both inside its jobs (windows_toolchain.py restore, ci_reuse.py).
This is the local counterpart that harness/bootstrap.py runs, for the
project of HARNESS_PROJECT in the workspace of HARNESS_WORKSPACE:

    python3 harness/restore_local.py toolchain --platform P [--dry-run]
    python3 harness/restore_local.py cache --platform P [--dry-run]

toolchain
    windows-x64: the published CLANG64 archive of the current specification
    (windows_toolchain.json), from the pinned release or the newest
    matching one, verified and extracted to .toolchain/<spec digest>/msys64
    under the workspace. An existing extraction is kept.
    linux-*: prints the package installation (it needs sudo; not run).
    macos-arm64: verifies .build/toolchain/llvm-22.1.8-macos14 when present,
    else prints the command that builds it (not run).
cache
    The newest build-<sha> pre-release of the project's repository (among
    the most recent RELEASE_LIMIT) that has this platform's cache asset and
    whose <sha> is in the checkout's history. The asset is checked against
    the digest GitHub lists for it, extracted into the workspace (it holds
    .ccache/ and cache-manifest.json), and recorded in
    .ci/local-cache.json; a recorded release is not downloaded again.

--dry-run lists the lookups' results and prints the downloads instead of
running them. Every GitHub call goes through gh_json or gh_download.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tarfile
from typing import Any, BinaryIO

import macos_runtime_manifest
import project
import windows_toolchain

RELEASE_LIMIT: int = 30
CACHE_RECORD: str = '.ci/local-cache.json'
# The restored Windows toolchain's PATH directories, one per line, first first;
# bootstrap.py puts them before PATH for the smoke build.
TOOLCHAIN_PATH_RECORD: str = '.ci/local-toolchain-path.txt'
MACOS_RUNTIME: str = '.build/toolchain/llvm-22.1.8-macos14'
# Under the workspace root: the extracted Windows toolchains (one per specification
# digest) and the downloaded archives.
TOOLCHAIN_DIRECTORY: str = '.toolchain'
DOWNLOAD_DIRECTORY: str = '.bootstrap'
PLATFORMS: tuple[str, ...] = ('windows-x64', 'linux-x64', 'linux-arm64', 'macos-arm64')


class RestoreError(Exception):
    """An expected failure; the command prints it and exits 1."""


def gh_json(path: str) -> Any:
    """GET a GitHub API path through gh (tests replace it)."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['gh', 'api', path], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RestoreError('gh api ' + path + ' failed: ' + completed.stderr.strip())
    value: Any = json.loads(completed.stdout)
    return value


def gh_download(repository: str, tag: str, asset: str, directory: Path) -> Path:
    """Download one release asset into directory through gh (tests replace it)."""
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(['gh', 'release', 'download', tag, '--repo', repository, '--pattern', asset,
                    '--dir', str(directory), '--clobber'], check=True)
    path: Path = directory / asset
    return path


def file_digest(path: Path) -> str:
    stream: BinaryIO
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def in_history(source: Path, revision: str) -> bool:
    """Whether revision is HEAD or an ancestor of it in the checkout (an unknown revision is not)."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(source), 'merge-base', '--is-ancestor', revision, 'HEAD'],
        capture_output=True, text=True, check=False)
    return completed.returncode == 0


def cache_candidates(releases: list[dict[str, Any]], asset_name: str) -> list[tuple[str, str, dict[str, Any]]]:
    """(tag, revision, asset) of each build-<sha> pre-release carrying asset_name, newest first."""
    candidates: list[tuple[str, str, dict[str, Any]]] = []
    release: dict[str, Any]
    for release in releases:
        tag: str = str(release.get('tag_name', ''))
        revision: str = tag[len('build-'):]
        if not tag.startswith('build-') or len(revision) != 40 or not release.get('prerelease'):
            continue
        asset: dict[str, Any]
        for asset in release.get('assets', []):
            if asset.get('name') == asset_name:
                candidates.append((tag, revision, asset))
    candidates.sort(key=release_order, reverse=True)
    return candidates


def release_order(candidate: tuple[str, str, dict[str, Any]]) -> str:
    """Sort key: the asset's upload time (ISO 8601 sorts as text)."""
    return str(candidate[2].get('created_at', ''))


def extract_cache(archive: Path, workspace: Path) -> None:
    """Extract a cache archive into the workspace; members must be relative and stay inside it."""
    payload: tarfile.TarFile
    with tarfile.open(archive, 'r:gz') as payload:
        member: tarfile.TarInfo
        for member in payload.getmembers():
            name: PurePosixPath = PurePosixPath(member.name)
            if not name.parts or name.is_absolute() or '..' in name.parts:
                raise RestoreError('Unexpected cache archive member: ' + member.name)
        payload.extractall(workspace, filter='data')


def restore_cache(product: project.Project, platform: str, dry_run: bool) -> str | None:
    """Restores the newest usable cache; returns its tag, or None when none is usable."""
    workspace: Path = product.workspace_root()
    source: Path = product.source_root()
    asset_name: str = product.cache_asset(platform)
    releases: list[dict[str, Any]] = gh_json('repos/' + product.repository + '/releases?per_page='
                                             + str(RELEASE_LIMIT))
    chosen: tuple[str, str, dict[str, Any]] | None = None
    candidate: tuple[str, str, dict[str, Any]]
    for candidate in cache_candidates(releases, asset_name):
        if in_history(source, candidate[1]):
            chosen = candidate
            break
    if chosen is None:
        print('No published ' + asset_name + ' for a revision in ' + str(source) + "'s history; "
              'the first build fills the cache', flush=True)
        return None
    tag: str = chosen[0]
    record_path: Path = workspace / CACHE_RECORD
    if record_path.is_file():
        record: dict[str, Any] = json.loads(record_path.read_text(encoding='utf-8'))
        if record.get('tag') == tag and record.get('asset') == asset_name:
            print('The cache of ' + tag + ' is already restored', flush=True)
            return tag
    download: Path = workspace / DOWNLOAD_DIRECTORY
    if dry_run:
        print('would run: ' + shlex.join(['gh', 'release', 'download', tag, '--repo', product.repository,
                                          '--pattern', asset_name, '--dir', str(download), '--clobber']), flush=True)
        print('would extract ' + asset_name + ' into ' + str(workspace), flush=True)
        return tag
    archive: Path = gh_download(product.repository, tag, asset_name, download)
    expected: str = str(chosen[2].get('digest', ''))
    if expected.startswith('sha256:') and file_digest(archive) != expected[len('sha256:'):]:
        raise RestoreError(asset_name + ' of ' + tag + ' does not match the digest GitHub lists for it')
    extract_cache(archive, workspace)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps({'tag': tag, 'asset': asset_name, 'sha256': file_digest(archive)}, indent=2)
                           + '\n', encoding='utf-8')
    print('Restored the cache of ' + tag + ' into ' + str(workspace), flush=True)
    return tag


def record_toolchain_path(product: project.Project, msys_root: Path) -> None:
    record: Path = product.workspace_root() / TOOLCHAIN_PATH_RECORD
    record.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [str(msys_root / 'clang64' / 'bin'), str(msys_root / 'usr' / 'bin')]
    record.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def restore_windows_toolchain(product: project.Project, dry_run: bool) -> Path | None:
    """The extracted msys64 root of the current specification, extracting the published archive when needed."""
    spec: dict[str, Any] = windows_toolchain.load_spec()
    digest: str = windows_toolchain.spec_digest(spec)
    destination: Path = product.workspace_root() / TOOLCHAIN_DIRECTORY / digest
    msys_root: Path = destination / windows_toolchain.ARCHIVE_ROOT
    if (destination / windows_toolchain.COMPILER_MEMBER).is_file():
        print('The Windows toolchain ' + digest[:12] + ' is already at ' + str(msys_root), flush=True)
        record_toolchain_path(product, msys_root)
        return msys_root
    repository: str = product.toolchain_repository
    found: tuple[str, dict[str, Any]] | None = windows_toolchain.pinned_release(repository, digest)
    if found is None:
        found = windows_toolchain.find_release(repository, 'spec_sha256', digest)
    if found is None:
        raise RestoreError('No published Windows toolchain for specification ' + digest + ' in ' + repository)
    if dry_run:
        print('would download ' + windows_toolchain.ARCHIVE_NAME + ' from ' + found[0] + ' and extract it to '
              + str(msys_root), flush=True)
        return None
    download: Path = product.workspace_root() / DOWNLOAD_DIRECTORY
    download.mkdir(parents=True, exist_ok=True)
    archive: Path = windows_toolchain.download_release_archive(repository, found[0], found[1], download)
    destination.mkdir(parents=True, exist_ok=True)
    root: Path = windows_toolchain.extract(archive, found[1], destination)
    record_toolchain_path(product, root)
    print('Restored the Windows toolchain ' + digest[:12] + ' to ' + str(root)
          + '; put msys64/clang64/bin and msys64/usr/bin first on PATH', flush=True)
    return root


def restore_toolchain(product: project.Project, platform: str, dry_run: bool) -> None:
    if platform == 'windows-x64':
        restore_windows_toolchain(product, dry_run)
        return
    harness: Path = Path(__file__).resolve().parent
    if platform.startswith('linux-'):
        if not product.linux_packages:
            print('The project names no Ubuntu packages (no [linux_packages] table); install its build tools '
                  'yourself', flush=True)
            return
        profile: str = product.workspace_linux_profile()
        # install_linux_ci_packages.sh reads the package set from the project file, so the
        # printed command names it; the line runs as it stands once the directory is filled in.
        print('Install the Ubuntu packages yourself (sudo): '
              + project.PROJECT_VARIABLE + '=' + shlex.quote(str(product.path)) + ' sh '
              + shlex.quote(str(harness / 'install_linux_ci_packages.sh')) + ' ' + profile
              + ' <apt archive directory>', flush=True)
        return
    runtime: Path = product.workspace_root() / MACOS_RUNTIME
    if (runtime / 'runtime-manifest.json').is_file():
        macos_runtime_manifest.verify(runtime)
        print('The macOS runtime at ' + str(runtime) + ' matches its manifest', flush=True)
        return
    print('Build the macOS runtime yourself: python3 ' + str(harness / 'build_macos_runtimes.py')
          + ' --work <scratch> --prefix ' + str(runtime) + '; then set ' + product.variable('LLVM_RUNTIME')
          + '=' + str(runtime), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('toolchain', 'cache'))
    parser.add_argument('--platform', required=True, choices=PLATFORMS)
    parser.add_argument('--dry-run', action='store_true')
    args: argparse.Namespace = parser.parse_args(argv)
    product: project.Project = project.current()
    try:
        if args.command == 'toolchain':
            restore_toolchain(product, args.platform, args.dry_run)
        else:
            restore_cache(product, args.platform, args.dry_run)
    except (RestoreError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print('restore_local: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
