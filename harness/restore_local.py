#!/usr/bin/env python3
"""Restore the pinned toolchain and the newest usable compiler cache into a local workspace.

CI restores both inside its jobs (windows_toolchain.py restore, ci_reuse.py).
This is the local counterpart that harness/bootstrap.py runs, for the
project of HARNESS_PROJECT in the workspace of HARNESS_WORKSPACE:

    python3 harness/restore_local.py toolchain --platform P [--dry-run]
    python3 harness/restore_local.py cache --platform P [--dry-run] [--revision SHA]
    python3 harness/restore_local.py runtime --platform P [--dry-run]
    python3 harness/restore_local.py env --platform P [--dry-run] [--set NAME=VALUE ...]

toolchain
    windows-x64: the published CLANG64 archive of the current specification
    (windows_toolchain.json), from the pinned release or the newest
    matching one, verified and extracted to .toolchain/<spec digest>/msys64
    under the workspace, where CI's build extracts it too
    (windows_toolchain.py restore): inside CCACHE_BASEDIR, so the clang
    resource headers hash the same wherever the workspace is. An existing
    extraction is kept.
    linux-*: prints the package installation (it needs sudo; not run).
    macos-arm64: verifies .build/toolchain/llvm-22.1.8-macos14 when present,
    else prints the command that builds it (not run).
cache
    The newest build-<sha> pre-release of the project's repository (among
    the most recent RELEASE_PAGES pages of releases) that has this
    platform's cache asset and whose <sha> is in the checkout's history.
    The asset is checked against the digest GitHub lists for it, extracted
    into the workspace (it holds .ccache/ and cache-manifest.json), and
    recorded in .ci/local-cache.json; a recorded release is not downloaded
    again. --revision SHA takes that commit's cache instead (an older,
    stable one to build the newer changes from). When the project names a
    runtime asset (ci.runtime_asset_prefix) and the release has this
    platform's, it is restored the same way: the C++ runtime CI built
    (GUI.Forms: the own runtime on Windows, the static runtime on Linux),
    which a fresh machine would otherwise build for minutes.
runtime
    macos-arm64 and linux-*: the static C++ runtime the build links
    (<PREFIX>_LLVM_RUNTIME). One the cache or runtime archive restored under
    .build/toolchain/ is verified against its manifest; else it is built
    there with harness/build_macos_runtimes.py or build_linux_runtimes.py,
    when a clang 22.1 is on PATH (on Linux the packages printed by
    toolchain bring it; without it the step says so and changes nothing).
    windows-x64: nothing (a product builds its own runtime in its
    [bootstrap.windows-x64] prepare commands, reusing a restored one).
env
    The build environment CI gives the project's build
    (build_environment.py): the ccache settings and locale the restored
    cache-manifest.json records, the restored toolchain and runtime, the
    project's [bootstrap] environment and each --set NAME=VALUE. Written as
    .ci/local-build-env.json, .sh and .ps1; --dry-run prints it instead. It
    then names what will still make the cache miss on this machine
    (build_environment.compatibility: another compiler build, another macOS
    SDK).

--dry-run lists the lookups' results and prints the downloads instead of
running them. Every GitHub call goes through gh_json or gh_download, which
use gh when it is installed and GitHub's public endpoints otherwise (a
public repository needs no sign-in).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform as host_platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from typing import Any, BinaryIO

import build_environment
import linux_runtime_manifest
import macos_runtime_manifest
import project
import windows_toolchain

# Releases are listed RELEASES_PER_PAGE at a time, at most RELEASE_PAGES pages: a project
# publishes a test cache and tagged versions beside each compiler cache, so one page of 30
# reached back only about ten compiler caches.
RELEASES_PER_PAGE: int = 100
RELEASE_PAGES: int = 3
CACHE_RECORD: str = '.ci/local-cache.json'
# Written by toolchain, read by env (build_environment.TOOLCHAIN_PATH_RECORD).
TOOLCHAIN_PATH_RECORD: str = build_environment.TOOLCHAIN_PATH_RECORD
MACOS_RUNTIME: str = '.build/toolchain/llvm-22.1.8-macos14'
# Under the workspace root: the extracted Windows toolchains (one per specification
# digest) and the downloaded archives.
TOOLCHAIN_DIRECTORY: str = windows_toolchain.TOOLCHAIN_DIRECTORY
DOWNLOAD_DIRECTORY: str = '.bootstrap'
PLATFORMS: tuple[str, ...] = ('windows-x64', 'linux-x64', 'linux-arm64', 'macos-arm64')
GITHUB_API: str = 'https://api.github.com/'
GITHUB_DOWNLOADS: str = 'https://github.com/'


class RestoreError(Exception):
    """An expected failure; the command prints it and exits 1."""


def github_request(url: str) -> urllib.request.Request:
    """A request to GitHub's public endpoints, signed with GH_TOKEN or GITHUB_TOKEN when one is set."""
    request: urllib.request.Request = urllib.request.Request(url, headers={'User-Agent': 'template-harness-restore'})
    token: str = os.environ.get('GH_TOKEN', '') or os.environ.get('GITHUB_TOKEN', '')
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    return request


def gh_json(path: str) -> Any:
    """GET a GitHub API path through gh, or without gh from the public API (tests replace it)."""
    if shutil.which('gh') is None:
        try:
            with urllib.request.urlopen(github_request(GITHUB_API + path), timeout=60) as response:
                value: Any = json.loads(response.read().decode('utf-8'))
        except OSError as error:
            raise RestoreError('GET ' + GITHUB_API + path + ' failed (gh is not installed): ' + str(error)) from error
        return value
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['gh', 'api', path], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RestoreError('gh api ' + path + ' failed: ' + completed.stderr.strip())
    listed: Any = json.loads(completed.stdout)
    return listed


def gh_download(repository: str, tag: str, asset: str, directory: Path) -> Path:
    """Download one release asset into directory through gh, or without gh from the public download
    address (tests replace it)."""
    directory.mkdir(parents=True, exist_ok=True)
    path: Path = directory / asset
    if shutil.which('gh') is None:
        url: str = GITHUB_DOWNLOADS + repository + '/releases/download/' + tag + '/' + asset
        try:
            stream: BinaryIO
            with urllib.request.urlopen(github_request(url), timeout=600) as response, path.open('wb') as stream:
                shutil.copyfileobj(response, stream)
        except OSError as error:
            raise RestoreError('downloading ' + url + ' failed (gh is not installed): ' + str(error)) from error
        return path
    subprocess.run(['gh', 'release', 'download', tag, '--repo', repository, '--pattern', asset,
                    '--dir', str(directory), '--clobber'], check=True)
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


def list_releases(repository: str) -> list[dict[str, Any]]:
    """The newest releases of repository, up to RELEASE_PAGES pages; a short page is the last."""
    releases: list[dict[str, Any]] = []
    page: int
    for page in range(1, RELEASE_PAGES + 1):
        listed: list[dict[str, Any]] = gh_json('repos/' + repository + '/releases?per_page='
                                               + str(RELEASES_PER_PAGE) + '&page=' + str(page))
        releases.extend(listed)
        if len(listed) < RELEASES_PER_PAGE:
            break
    return releases


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


def restore_asset(product: project.Project, tag: str, asset: dict[str, Any], dry_run: bool) -> str:
    """Downloads one release asset of tag, checks it against the digest GitHub lists, extracts it into the
    workspace (extract_cache's rules) and returns its sha256 (empty in a dry run)."""
    workspace: Path = product.workspace_root()
    name: str = str(asset.get('name', ''))
    download: Path = workspace / DOWNLOAD_DIRECTORY
    if dry_run:
        print('would download ' + name + ' of ' + tag + ' from ' + product.repository + ' into ' + str(download)
              + ' and extract it into ' + str(workspace), flush=True)
        return ''
    archive: Path = gh_download(product.repository, tag, name, download)
    digest: str = file_digest(archive)
    expected: str = str(asset.get('digest', ''))
    if expected.startswith('sha256:') and digest != expected[len('sha256:'):]:
        raise RestoreError(name + ' of ' + tag + ' does not match the digest GitHub lists for it')
    extract_cache(archive, workspace)
    return digest


def release_asset(releases: list[dict[str, Any]], tag: str, name: str) -> dict[str, Any] | None:
    """The asset called name in the release tagged tag, or None."""
    release: dict[str, Any]
    for release in releases:
        if release.get('tag_name') != tag:
            continue
        asset: dict[str, Any]
        for asset in release.get('assets', []):
            if asset.get('name') == name:
                return asset
    return None


def restore_cache(product: project.Project, platform: str, dry_run: bool, revision: str = '') -> str | None:
    """Restores the newest usable cache, or with revision the cache of that commit (a full or
    abbreviated SHA, in the checkout's history: an older stable cache to update from), and the
    release's runtime asset when the project names one and the release has it; returns the tag,
    or None when no cache is usable."""
    workspace: Path = product.workspace_root()
    source: Path = product.source_root()
    asset_name: str = product.cache_asset(platform)
    runtime_name: str | None = product.runtime_asset(platform)
    releases: list[dict[str, Any]] = list_releases(product.repository)
    chosen: tuple[str, str, dict[str, Any]] | None = None
    candidate: tuple[str, str, dict[str, Any]]
    for candidate in cache_candidates(releases, asset_name):
        if revision and not candidate[1].startswith(revision.lower()):
            continue
        if in_history(source, candidate[1]):
            chosen = candidate
            break
    if chosen is None and revision:
        print('No published ' + asset_name + ' for revision ' + revision + ' in ' + str(source) + "'s history "
              '(it may have been pruned: packaging/pinned_cache_revisions.json keeps a pinned one)', flush=True)
        return None
    if chosen is None:
        print('No published ' + asset_name + ' for a revision in ' + str(source) + "'s history; "
              'the first build fills the cache', flush=True)
        return None
    tag: str = chosen[0]
    runtime: dict[str, Any] | None = None if runtime_name is None else release_asset(releases, tag, runtime_name)
    record_path: Path = workspace / CACHE_RECORD
    if record_path.is_file():
        record: dict[str, Any] = json.loads(record_path.read_text(encoding='utf-8'))
        if record.get('tag') == tag and record.get('asset') == asset_name:
            print('The cache of ' + tag + ' is already restored', flush=True)
            return tag
    restored: dict[str, str] = {'tag': tag, 'asset': asset_name}
    restored['sha256'] = restore_asset(product, tag, chosen[2], dry_run)
    if runtime is not None:
        restored['runtime'] = str(runtime_name)
        restored['runtime_sha256'] = restore_asset(product, tag, runtime, dry_run)
    elif runtime_name is not None:
        print(tag + ' has no ' + runtime_name + '; the build makes its runtime itself', flush=True)
    if dry_run:
        return tag
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(restored, indent=2) + '\n', encoding='utf-8')
    print('Restored the cache of ' + tag + (' and its runtime' if runtime is not None else '') + ' into '
          + str(workspace), flush=True)
    return tag


def record_toolchain_path(product: project.Project, msys_root: Path) -> None:
    record: Path = product.workspace_root() / TOOLCHAIN_PATH_RECORD
    record.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [str(msys_root / 'clang64' / 'bin'), str(msys_root / 'usr' / 'bin')]
    record.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def restore_windows_toolchain(product: project.Project, dry_run: bool) -> Path | None:
    """The extracted msys64 root of the current specification under the workspace, extracting the
    published archive when needed (windows_toolchain.workspace_destination, CI's place too)."""
    spec: dict[str, Any] = windows_toolchain.load_spec()
    digest: str = windows_toolchain.spec_digest(spec)
    destination: Path = windows_toolchain.workspace_destination(product.workspace_root(), digest)
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
        print('Then build the static C++ runtime with that compiler: python3 '
              + shlex.quote(str(harness / 'build_linux_runtimes.py'))
              + ' --work <scratch> --prefix <workspace>/.build/toolchain/<python3 '
              + shlex.quote(str(harness / 'linux_runtime_manifest.py')) + ' --directory-name>; then set '
              + product.variable('LLVM_RUNTIME') + ' to that prefix', flush=True)
        return
    runtime: Path = product.workspace_root() / MACOS_RUNTIME
    if (runtime / 'runtime-manifest.json').is_file():
        macos_runtime_manifest.verify(runtime)
        print('The macOS runtime at ' + str(runtime) + ' matches its manifest', flush=True)
        return
    print('The macOS runtime comes with the cache, or restore_local.py runtime builds it: python3 '
          + str(harness / 'build_macos_runtimes.py') + ' --work <scratch> --prefix ' + str(runtime) + '; then set '
          + product.variable('LLVM_RUNTIME') + '=' + str(runtime), flush=True)


def runtime_target(workspace: Path, platform: str) -> Path:
    """Where the platform's runtime goes when the bootstrap builds it."""
    if platform == 'macos-arm64':
        return workspace / MACOS_RUNTIME
    name: str = linux_runtime_manifest.runtime_directory_name(linux_runtime_manifest.c_library(),
                                                              host_platform.machine())
    return workspace / build_environment.RUNTIME_DIRECTORY / name


def has_clang_22() -> bool:
    """Whether the clang on PATH is LLVM 22.1 (the runtimes' bootstrap compiler)."""
    if shutil.which('clang') is None:
        return False
    version: str = subprocess.run(['clang', '--version'], capture_output=True, text=True, check=False).stdout
    return 'clang version 22.1.' in version


def restore_runtime(product: project.Project, platform: str, dry_run: bool) -> Path | None:
    """The runtime prefix, verified when restored and built when missing (see runtime above); None
    when the platform needs none here or it could not be built."""
    if platform == 'windows-x64':
        print('The Windows runtime is the product\'s own ([bootstrap.windows-x64] prepare)', flush=True)
        return None
    workspace: Path = product.workspace_root()
    restored: Path | None = build_environment.runtime_prefix(workspace)
    verifier: Any = macos_runtime_manifest if platform == 'macos-arm64' else linux_runtime_manifest
    if restored is not None:
        try:
            verifier.verify(restored)
            print('The C++ runtime at ' + str(restored) + ' matches its manifest', flush=True)
            return restored
        except RuntimeError as error:
            # A runtime from CI's archive records CI's compiler and SDK; on a machine with
            # another one its own check refuses it (and the cached objects miss anyway), so
            # it is set aside and this machine's runtime is built in its place.
            aside: Path = restored.with_name(restored.name + build_environment.UNVERIFIED_SUFFIX)
            print('warning: the restored C++ runtime at ' + str(restored) + ' does not verify here (' + str(error)
                  + '); ' + ('it would be set aside and rebuilt' if dry_run else 'setting it aside as ' + str(aside)
                             + ' and building one'), flush=True)
            if not dry_run:
                if aside.exists():
                    shutil.rmtree(aside)
                restored.rename(aside)
    target: Path = runtime_target(workspace, platform)
    builder: str = 'build_macos_runtimes.py' if platform == 'macos-arm64' else 'build_linux_runtimes.py'
    command: list[str] = [sys.executable, '-B', str(Path(__file__).resolve().parent / builder),
                          '--work', str(workspace / DOWNLOAD_DIRECTORY / 'llvm-runtimes'), '--prefix', str(target),
                          '--jobs', str(os.cpu_count() or 4)]
    if dry_run:
        print('would run: ' + shlex.join(command), flush=True)
        return None
    if not has_clang_22():
        print('The C++ runtime is not built: no clang 22.1 on PATH (install the packages above first); then '
              'run: ' + shlex.join(command), flush=True)
        return None
    print('Building the C++ runtime into ' + str(target) + ' (several minutes, once per workspace)', flush=True)
    subprocess.run(command, check=True)
    verifier.verify(target)
    return target


def write_environment(product: project.Project, platform: str, settings: list[tuple[str, str]],
                      dry_run: bool) -> None:
    """env: writes (or with dry_run prints) the build environment, then names what will still miss."""
    values: dict[str, str] = build_environment.build_environment(product, platform, settings)
    workspace: Path = product.workspace_root()
    if dry_run:
        print(json.dumps(values, indent=2, sort_keys=True), flush=True)
    else:
        record: Path = build_environment.write_build_environment(workspace, values)
        print('Wrote the build environment: ' + str(record) + ' (load ' + build_environment.BUILD_ENVIRONMENT_SHELL
              + ' with . in a shell, or ' + build_environment.BUILD_ENVIRONMENT_POWERSHELL + ' in PowerShell, '
              'before building)', flush=True)
    applied: dict[str, str] = build_environment.apply_build_environment(
        workspace / build_environment.BUILD_ENVIRONMENT_RECORD, dict(os.environ)) if not dry_run else dict(os.environ)
    problem: str
    for problem in build_environment.compatibility(build_environment.read_manifest(workspace), platform, applied):
        print('warning: ' + problem, flush=True)


def parse_setting(text: str) -> tuple[str, str]:
    name: str
    value: str
    name, separator, value = text.partition('=')
    if not separator:
        raise argparse.ArgumentTypeError('expected NAME=VALUE, got ' + text)
    return name, value


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('toolchain', 'cache', 'runtime', 'env'))
    parser.add_argument('--platform', required=True, choices=PLATFORMS)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--set', dest='settings', action='append', type=parse_setting, default=[],
                        metavar='NAME=VALUE', help='env: one more variable (repeatable)')
    parser.add_argument('--revision', default='', metavar='SHA',
                        help='cache: the cache of this commit instead of the newest (an older stable one)')
    args: argparse.Namespace = parser.parse_args(argv)
    product: project.Project = project.current()
    try:
        if args.command == 'toolchain':
            restore_toolchain(product, args.platform, args.dry_run)
        elif args.command == 'cache':
            restore_cache(product, args.platform, args.dry_run, args.revision)
        elif args.command == 'runtime':
            restore_runtime(product, args.platform, args.dry_run)
        else:
            write_environment(product, args.platform, args.settings, args.dry_run)
    except (RestoreError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print('restore_local: ' + str(error), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
