#!/usr/bin/env python3
"""Restore the pinned toolchain and the newest usable compiler cache into a local workspace.

CI restores both inside its jobs (windows_toolchain.py restore, ci_reuse.py).
This is the local counterpart that harness/bootstrap.py runs, for the
project of HARNESS_PROJECT in the workspace of HARNESS_WORKSPACE:

    python3 harness/restore_local.py toolchain --platform P [--dry-run] [--workspace-toolchain]
    python3 harness/restore_local.py cache --platform P [--dry-run] [--revision SHA]
    python3 harness/restore_local.py env --platform P [--set NAME=VALUE ...]

toolchain
    windows-x64: the published CLANG64 archive of the current specification
    (windows_toolchain.json), from the pinned release or the newest
    matching one, verified and extracted. For a project that publishes
    compiler caches it goes to the producer's msys root that the archive's
    manifest records (D:\\a\\_temp\\msys64 on GitHub's Windows runners):
    the clang resource headers are included by absolute path, outside
    CCACHE_BASEDIR, so a toolchain anywhere else misses every cached
    object (windows_toolchain.py's contract). With --workspace-toolchain,
    for a project without caches, or when that root holds another
    toolchain or cannot be created, it goes to .toolchain/<spec
    digest>/msys64 under the workspace (the last case says the cache will
    miss). An existing extraction is kept.
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
    stable one to build the newer changes from).
env
    The build environment CI gives the project's build, written as
    .ci/local-build-env.json (bootstrap.py runs the prepare commands and the
    smoke build in it) and as .ci/local-build-env.ps1 and .sh to load in a
    shell: the ccache settings CI sets (CCACHE_DIR, CCACHE_BASEDIR,
    CCACHE_COMPILERCHECK, CCACHE_SLOPPINESS and the compiler launchers);
    the locale CI's shell has, since ccache hashes LANG, LC_ALL, LC_CTYPE
    and LC_MESSAGES into every key (the restored cache-manifest.json's
    "environment" when it records one, else CI_LOCALE); the restored
    toolchain first on PATH; the project's [bootstrap.<platform>]
    environment; and each --set NAME=VALUE.

--dry-run lists the lookups' results and prints the downloads instead of
running them. Every GitHub call goes through gh_json or gh_download.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys
import tarfile
from typing import Any, BinaryIO

import macos_runtime_manifest
import project
import windows_toolchain

# Releases are listed RELEASES_PER_PAGE at a time, at most RELEASE_PAGES pages: a project
# publishes a test cache and tagged versions beside each compiler cache, so one page of 30
# reached back only about ten compiler caches.
RELEASES_PER_PAGE: int = 100
RELEASE_PAGES: int = 3
CACHE_RECORD: str = '.ci/local-cache.json'
# The restored Windows toolchain's PATH directories, one per line, first first;
# bootstrap.py puts them before PATH for the smoke build.
TOOLCHAIN_PATH_RECORD: str = '.ci/local-toolchain-path.txt'
# The build environment (env), as JSON for bootstrap.py and as scripts for a shell.
BUILD_ENVIRONMENT_RECORD: str = '.ci/local-build-env.json'
BUILD_ENVIRONMENT_POWERSHELL: str = '.ci/local-build-env.ps1'
BUILD_ENVIRONMENT_SHELL: str = '.ci/local-build-env.sh'
MACOS_RUNTIME: str = '.build/toolchain/llvm-22.1.8-macos14'
# Under the workspace root: the extracted Windows toolchains (one per specification
# digest) and the downloaded archives.
TOOLCHAIN_DIRECTORY: str = '.toolchain'
DOWNLOAD_DIRECTORY: str = '.bootstrap'
PLATFORMS: tuple[str, ...] = ('windows-x64', 'linux-x64', 'linux-arm64', 'macos-arm64')
# The locale variables ccache hashes into every key (ccache's hash_common_info).
LOCALE_VARIABLES: tuple[str, ...] = ('LANG', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES')
# The locale of CI's build shell where a cache manifest records none: GitHub's msys2 shell
# on Windows sets LANG=en_US.UTF-8 and no LC_* variable. The Linux and macOS runners are
# not measured yet, so they get no default.
CI_LOCALE: dict[str, dict[str, str]] = {'windows-x64': {'LANG': 'en_US.UTF-8'}}
# The ccache settings CI's native jobs set (GUI.Forms native-platform.yml, Plan Paint native-build.sh).
CCACHE_SLOPPINESS: str = 'pch_defines,time_macros'
LAUNCHER_VARIABLES: tuple[str, ...] = ('CMAKE_C_COMPILER_LAUNCHER', 'CMAKE_CXX_COMPILER_LAUNCHER',
                                       'CMAKE_OBJCXX_COMPILER_LAUNCHER')
ENVIRONMENT_NAME: re.Pattern[str] = re.compile(r'[A-Z][A-Z0-9_]*')
WORKSPACE_PLACEHOLDER: str = '{workspace}'


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


def restore_cache(product: project.Project, platform: str, dry_run: bool, revision: str = '') -> str | None:
    """Restores the newest usable cache, or with revision the cache of that commit (a full or
    abbreviated SHA, in the checkout's history: an older stable cache to update from); returns its
    tag, or None when none is usable."""
    workspace: Path = product.workspace_root()
    source: Path = product.source_root()
    asset_name: str = product.cache_asset(platform)
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


def producer_root(manifest: dict[str, Any]) -> Path | None:
    """The msys root the toolchain's producer built the caches with, or None when the manifest
    records none or one whose last part is not the archive's root directory (msys64)."""
    producer: Any = manifest.get('producer')
    recorded: str = str(producer.get('msys_root', '')) if isinstance(producer, dict) else ''
    if not recorded:
        return None
    root: Path = Path(recorded)
    if root.name != windows_toolchain.ARCHIVE_ROOT:
        return None
    return root


def holds_compiler(msys_root: Path, compiler_sha256: str) -> bool:
    """Whether msys_root holds the manifest's compiler, byte for byte."""
    compiler: Path = msys_root.parent / windows_toolchain.COMPILER_MEMBER
    return compiler.is_file() and file_digest(compiler) == compiler_sha256


def usable_producer_root(root: Path, manifest: dict[str, Any]) -> tuple[bool, str]:
    """(usable, reason): the producer root is usable when it holds this toolchain already, or does
    not exist yet and its parent can be created."""
    if holds_compiler(root, str(manifest.get('compiler_sha256', ''))):
        return True, 'holds this toolchain'
    if root.exists():
        return False, str(root) + ' holds another toolchain or something else'
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return False, str(root.parent) + ' cannot be created (' + str(error) + ')'
    return True, 'is free'


def restore_windows_toolchain(product: project.Project, dry_run: bool, workspace_toolchain: bool = False) -> Path | None:
    """The extracted msys64 root of the current specification, extracting the published archive when needed."""
    spec: dict[str, Any] = windows_toolchain.load_spec()
    digest: str = windows_toolchain.spec_digest(spec)
    workspace_destination: Path = product.workspace_root() / TOOLCHAIN_DIRECTORY / digest
    workspace_root: Path = workspace_destination / windows_toolchain.ARCHIVE_ROOT
    wants_producer_root: bool = product.cache_asset_prefix is not None and not workspace_toolchain
    if not wants_producer_root and (workspace_destination / windows_toolchain.COMPILER_MEMBER).is_file():
        print('The Windows toolchain ' + digest[:12] + ' is already at ' + str(workspace_root), flush=True)
        record_toolchain_path(product, workspace_root)
        return workspace_root
    repository: str = product.toolchain_repository
    found: tuple[str, dict[str, Any]] | None = windows_toolchain.pinned_release(repository, digest)
    if found is None:
        found = windows_toolchain.find_release(repository, 'spec_sha256', digest)
    if found is None:
        raise RestoreError('No published Windows toolchain for specification ' + digest + ' in ' + repository)
    manifest: dict[str, Any] = found[1]
    msys_root: Path = workspace_root
    if wants_producer_root:
        producer: Path | None = producer_root(manifest)
        if producer is None:
            print('warning: the toolchain manifest records no producer msys root; extracting to the workspace, '
                  'where the compiler cache will miss', flush=True)
        else:
            usable: bool
            reason: str
            usable, reason = usable_producer_root(producer, manifest) if not dry_run else (True, 'dry run')
            if usable:
                msys_root = producer
            else:
                print('warning: ' + reason + '; extracting to the workspace instead, where every cached object '
                      'misses (the clang resource headers are included by absolute path)', flush=True)
    if (msys_root.parent / windows_toolchain.COMPILER_MEMBER).is_file():
        print('The Windows toolchain ' + digest[:12] + ' is already at ' + str(msys_root), flush=True)
        record_toolchain_path(product, msys_root)
        return msys_root
    if dry_run:
        print('would download ' + windows_toolchain.ARCHIVE_NAME + ' from ' + found[0] + ' and extract it to '
              + str(msys_root), flush=True)
        return None
    download: Path = product.workspace_root() / DOWNLOAD_DIRECTORY
    download.mkdir(parents=True, exist_ok=True)
    archive: Path = windows_toolchain.download_release_archive(repository, found[0], manifest, download)
    msys_root.parent.mkdir(parents=True, exist_ok=True)
    root: Path = windows_toolchain.extract(archive, manifest, msys_root.parent)
    record_toolchain_path(product, root)
    print('Restored the Windows toolchain ' + digest[:12] + ' to ' + str(root)
          + '; put msys64/clang64/bin and msys64/usr/bin first on PATH', flush=True)
    return root


def restore_toolchain(product: project.Project, platform: str, dry_run: bool, workspace_toolchain: bool = False) -> None:
    if platform == 'windows-x64':
        restore_windows_toolchain(product, dry_run, workspace_toolchain)
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
    print('Build the macOS runtime yourself: python3 ' + str(harness / 'build_macos_runtimes.py')
          + ' --work <scratch> --prefix ' + str(runtime) + '; then set ' + product.variable('LLVM_RUNTIME')
          + '=' + str(runtime), flush=True)


def recorded_locale(workspace: Path, platform: str) -> dict[str, str]:
    """The locale variables the restored cache-manifest.json records under "environment", else CI_LOCALE's."""
    manifest_path: Path = workspace / 'cache-manifest.json'
    if manifest_path.is_file():
        manifest: Any = json.loads(manifest_path.read_text(encoding='utf-8'))
        recorded: Any = manifest.get('environment') if isinstance(manifest, dict) else None
        if isinstance(recorded, dict):
            locale: dict[str, str] = {name: str(recorded[name]) for name in LOCALE_VARIABLES if name in recorded}
            return locale
    return dict(CI_LOCALE.get(platform, {}))


def build_environment(product: project.Project, platform: str, settings: list[tuple[str, str]]) -> dict[str, str]:
    """The variables of CI's build environment for this workspace (see env in the module docstring).
    A locale variable CI's shell does not set maps to the empty string: the scripts unset it."""
    workspace: Path = product.workspace_root()
    base: str = workspace.as_posix()
    values: dict[str, str] = {'CCACHE_DIR': (workspace / '.ccache').as_posix(), 'CCACHE_BASEDIR': base,
                              'CCACHE_COMPILERCHECK': 'content', 'CCACHE_SLOPPINESS': CCACHE_SLOPPINESS}
    name: str
    for name in LAUNCHER_VARIABLES:
        values[name] = 'ccache'
    locale: dict[str, str] = recorded_locale(workspace, platform)
    for name in LOCALE_VARIABLES:
        values[name] = locale.get(name, '')
    record: Path = workspace / TOOLCHAIN_PATH_RECORD
    if platform == 'windows-x64' and record.is_file():
        directories: list[str] = record.read_text(encoding='utf-8').splitlines()
        values['PATH'] = os.pathsep.join(directories) + os.pathsep + '${PATH}'
    table: project.BootstrapPlatform | None = (None if product.bootstrap is None
                                                else product.bootstrap.platforms.get(platform))
    value: str
    if table is not None:
        for name, value in table.environment:
            values[name] = value.replace(WORKSPACE_PLACEHOLDER, base)
    for name, value in settings:
        if not ENVIRONMENT_NAME.fullmatch(name):
            raise RestoreError('Not an environment variable name: ' + name)
        values[name] = value
    return values


def powershell_line(name: str, value: str) -> str:
    if value == '':
        return 'Remove-Item -ErrorAction SilentlyContinue env:' + name
    quoted: str = "'" + value.replace("'", "''") + "'"
    if name == 'PATH':
        quoted = quoted.replace('${PATH}', "' + $env:PATH + '")
    return '$env:' + name + ' = ' + quoted


def shell_line(name: str, value: str) -> str:
    if value == '':
        return 'unset ' + name
    if name == 'PATH':
        # MSYS2 and Git Bash spell D:\x as /d/x; cygpath converts the list.
        listing: str = value.replace('${PATH}', '').rstrip(os.pathsep)
        return 'export PATH="$(cygpath -p -u ' + shlex.quote(listing) + '):$PATH"'
    return 'export ' + name + '=' + shlex.quote(value)


def write_build_environment(product: project.Project, values: dict[str, str]) -> Path:
    """Writes BUILD_ENVIRONMENT_RECORD and the two scripts; returns the record's path."""
    workspace: Path = product.workspace_root()
    record: Path = workspace / BUILD_ENVIRONMENT_RECORD
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(values, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    names: list[str] = sorted(values)
    header: str = 'CI\'s build environment for this workspace (harness/restore_local.py env); load it before building.'
    powershell: list[str] = ['# ' + header] + [powershell_line(name, values[name]) for name in names]
    shell: list[str] = ['# ' + header, '# . ' + BUILD_ENVIRONMENT_SHELL] + [shell_line(name, values[name])
                                                                           for name in names]
    (workspace / BUILD_ENVIRONMENT_POWERSHELL).write_text('\n'.join(powershell) + '\n', encoding='utf-8')
    (workspace / BUILD_ENVIRONMENT_SHELL).write_text('\n'.join(shell) + '\n', encoding='utf-8')
    print('Wrote the build environment: ' + str(record) + ' (load ' + BUILD_ENVIRONMENT_POWERSHELL + ' in '
          'PowerShell or . ' + BUILD_ENVIRONMENT_SHELL + ' in a shell before building)', flush=True)
    return record


def apply_build_environment(record: Path, environment: dict[str, str]) -> dict[str, str]:
    """environment with the record's variables applied (bootstrap.py's steps): an empty value
    removes the variable, and ${PATH} in PATH stands for environment's PATH."""
    result: dict[str, str] = dict(environment)
    if not record.is_file():
        return result
    values: dict[str, str] = json.loads(record.read_text(encoding='utf-8'))
    name: str
    for name in sorted(values):
        value: str = values[name]
        if value == '':
            result.pop(name, None)
        elif name == 'PATH':
            result['PATH'] = value.replace('${PATH}', environment.get('PATH', ''))
        else:
            result[name] = value
    return result


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
    parser.add_argument('command', choices=('toolchain', 'cache', 'env'))
    parser.add_argument('--platform', required=True, choices=PLATFORMS)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--workspace-toolchain', action='store_true',
                        help='toolchain: extract under the workspace even when the project publishes caches')
    parser.add_argument('--set', dest='settings', action='append', type=parse_setting, default=[],
                        metavar='NAME=VALUE', help='env: one more variable (repeatable)')
    parser.add_argument('--revision', default='', metavar='SHA',
                        help='cache: the cache of this commit instead of the newest (an older stable one)')
    args: argparse.Namespace = parser.parse_args(argv)
    product: project.Project = project.current()
    try:
        if args.command == 'toolchain':
            restore_toolchain(product, args.platform, args.dry_run, args.workspace_toolchain)
        elif args.command == 'cache':
            restore_cache(product, args.platform, args.dry_run, args.revision)
        else:
            values: dict[str, str] = build_environment(product, args.platform, args.settings)
            if args.dry_run:
                print(json.dumps(values, indent=2, sort_keys=True), flush=True)
            else:
                write_build_environment(product, values)
    except (RestoreError, ValueError, OSError, subprocess.CalledProcessError) as error:
        print('restore_local: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
