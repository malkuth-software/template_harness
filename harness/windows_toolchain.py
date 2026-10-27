#!/usr/bin/env python3
"""Package, restore and locate the exact MSYS2 CLANG64 tree that fills the Windows cache.

Contract for a consumer: extract the archive, prepend msys64/clang64/bin and
msys64/usr/bin to PATH, and use it without network access. Its clang.exe is the
byte-identical compiler recorded as compiler_sha256 in both this manifest and
the cache manifest. Cache hits additionally require extraction at the
producer's msys_root: system header paths enter preprocessed output, and
CCACHE_BASEDIR covers only the workspace.

Requires Python 3.14 (stdlib zstd) for packing and extraction.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tempfile
from typing import Any, BinaryIO, TextIO

import ci_reuse
import project

PLATFORM: str = 'windows-x64'
# <toolchain.asset_prefix>windows-x64.tar.zst and .json, and the cache asset
# (GUI.Forms: gui-forms-toolchain-windows-x64.tar.zst, gui-forms-cache-windows-x64.tar.gz).
ARCHIVE_NAME: str = project.current().toolchain_asset_prefix + PLATFORM + '.tar.zst'
MANIFEST_NAME: str = project.current().toolchain_asset_prefix + PLATFORM + '.json'
# The compiler-cache archive beside the toolchain, or None for a product that
# publishes no caches: only the cache publication and fetch-recorded read it
# (through project.cache_asset, which names the missing field), so such a
# product still restores the toolchain.
CACHE_NAME: str | None = (None if project.current().cache_asset_prefix is None
                          else project.current().cache_asset(PLATFORM))
SPEC_PATH: Path = Path(__file__).resolve().parent / 'windows_toolchain.json'
# The release that holds the published archive of the current specification,
# kept apart from the specification, whose digest names the toolchain.
PIN_PATH: Path = Path(__file__).resolve().parent / 'windows_toolchain_release.json'
# Set to 1 (native.yml's package_windows_toolchain input) to let a job with
# no published archive install the specification's packages from the MSYS2
# mirrors and package them. Only for a deliberate specification change: the
# mirrors carry only the current LLVM, never an older pinned one.
PACKAGE_VARIABLE: str = project.current().variable('PACKAGE_WINDOWS_TOOLCHAIN')
CLANG_PACKAGE: str = 'mingw-w64-clang-x86_64-clang'
LOCAL_MANIFEST: Path = ci_reuse.STATE / 'windows-toolchain.json'
ARCHIVE_ROOT: str = 'msys64'
COMPILER_MEMBER: str = 'msys64/clang64/bin/clang.exe'
# Changing what the archive omits or how it is laid out requires a new
# toolchain even when the package specification is unchanged.
LAYOUT: int = 2
ZSTD_LEVEL: int = 10
# Bounded discovery: recent releases only, and few manifest downloads.
RELEASE_PAGES: int = 3
RELEASES_PER_PAGE: int = 30
MAXIMUM_MANIFESTS: int = 10
MAXIMUM_MANIFEST_BYTES: int = 1024 * 1024
# Package caches, sync databases, documentation, per-user state and the local
# pacman signing secret are omitted. The public keyring remains for pacman -Q.
EXCLUDED_DIRECTORIES: tuple[str, ...] = (
    'home', 'tmp', 'var/cache', 'var/log', 'var/tmp', 'var/lib/pacman/sync',
    'etc/pacman.d/gnupg/private-keys-v1.d', 'etc/pacman.d/gnupg/openpgp-revocs.d',
    'usr/share/doc', 'usr/share/man', 'usr/share/info', 'usr/share/gtk-doc',
    'clang64/share/doc', 'clang64/share/man', 'clang64/share/info', 'clang64/share/gtk-doc')
EXCLUDED_FILES: tuple[str, ...] = (
    'etc/pacman.d/gnupg/secring.gpg', 'etc/pacman.d/gnupg/random_seed')
GNUPG_DIRECTORY: str = 'etc/pacman.d/gnupg'
# Runtime directories whose contents are omitted but which must exist: the
# MSYS2 login profile points TMP and TEMP at /tmp, and std::filesystem's
# temp_directory_path() rejects a missing directory.
EMPTY_DIRECTORIES: tuple[str, ...] = ('tmp', 'var/tmp', 'home')


def file_digest(path: Path) -> str:
    return ci_reuse.file_digest(path)


def load_spec() -> dict[str, Any]:
    spec: dict[str, Any] = json.loads(SPEC_PATH.read_text(encoding='utf-8'))
    if spec.get('schema') != 1 or spec.get('msystem') != 'CLANG64':
        raise ValueError('Unsupported Windows toolchain specification')
    return spec


class ToolchainUnavailable(RuntimeError):
    """The pinned Windows toolchain cannot be restored, and installing the current one was not requested."""


def check_llvm_version(packages: dict[str, str], spec: dict[str, Any]) -> None:
    """Raise unless the installed clang package is the specification's LLVM release (any package revision)."""
    installed: str = packages.get(CLANG_PACKAGE, 'none')
    if not installed.startswith(spec['llvm'] + '-'):
        raise ToolchainUnavailable('tools/windows_toolchain.json pins LLVM ' + spec['llvm'] + ', but the installed '
                                   + CLANG_PACKAGE + ' is ' + installed + '. The MSYS2 mirrors carry only the '
                                   'current LLVM; packaging a new toolchain needs a specification that names it.')


def spec_digest(spec: dict[str, Any]) -> str:
    identity: dict[str, Any] = {'spec': spec, 'layout': LAYOUT}
    text: str = json.dumps(identity, sort_keys=True, separators=(',', ':'))
    digest: str = hashlib.sha256(text.encode('utf-8')).hexdigest()
    return digest


def installed_packages(msys_root: Path) -> dict[str, str]:
    """Read exact installed versions from pacman's local database without running pacman."""
    packages: dict[str, str] = {}
    database: Path = msys_root / 'var/lib/pacman/local'
    entry: Path
    for entry in sorted(database.iterdir()):
        description: Path = entry / 'desc'
        if not description.is_file():
            continue
        lines: list[str] = description.read_text(encoding='utf-8').splitlines()
        name: str = ''
        version: str = ''
        index: int
        for index in range(len(lines) - 1):
            if lines[index] == '%NAME%':
                name = lines[index + 1]
            if lines[index] == '%VERSION%':
                version = lines[index + 1]
        if name == '' or version == '':
            raise ValueError('Incomplete pacman package record: ' + entry.name)
        packages[name] = version
    return packages


def excluded(relative: PurePosixPath) -> bool:
    text: str = relative.as_posix()
    directory: str
    for directory in EXCLUDED_DIRECTORIES:
        if text == directory or text.startswith(directory + '/'):
            return True
    if text in EXCLUDED_FILES:
        return True
    # gpg-agent sockets are runtime state.
    if relative.parent.as_posix() == GNUPG_DIRECTORY and relative.name.startswith('S.'):
        return True
    return False


def normalized_member(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = 0
    member.gid = 0
    member.uname = ''
    member.gname = ''
    return member


def write_archive(msys_root: Path, archive: Path) -> None:
    """Write msys_root as msys64/ in sorted order, omitting EXCLUDED_* paths."""
    with tarfile.open(archive, 'w:zst', level=ZSTD_LEVEL) as output:
        output.add(msys_root, arcname=ARCHIVE_ROOT, recursive=False, filter=normalized_member)
        current: str
        directories: list[str]
        files: list[str]
        for current, directories, files in os.walk(msys_root):
            directories.sort()
            files.sort()
            base: Path = Path(current)
            kept: list[str] = []
            directory: str
            for directory in directories:
                relative_directory: PurePosixPath = PurePosixPath(
                    (base / directory).relative_to(msys_root).as_posix())
                if excluded(relative_directory):
                    if relative_directory.as_posix() in EMPTY_DIRECTORIES:
                        output.add(base / directory, arcname=ARCHIVE_ROOT + '/' + relative_directory.as_posix(),
                                   recursive=False, filter=normalized_member)
                    continue
                kept.append(directory)
                output.add(base / directory, arcname=ARCHIVE_ROOT + '/' + relative_directory.as_posix(),
                           recursive=False, filter=normalized_member)
            directories[:] = kept
            name: str
            for name in files:
                relative_file: PurePosixPath = PurePosixPath((base / name).relative_to(msys_root).as_posix())
                if excluded(relative_file):
                    continue
                output.add(base / name, arcname=ARCHIVE_ROOT + '/' + relative_file.as_posix(),
                           recursive=False, filter=normalized_member)


def pack(msys_root: Path, destination: Path, spec: dict[str, Any], producer: dict[str, str]) -> dict[str, Any]:
    """Archive an installed tree into destination/ARCHIVE_NAME; return its manifest."""
    packages: dict[str, str] = installed_packages(msys_root)
    requested: str
    for requested in spec['packages']:
        if requested not in packages:
            raise ValueError('Specified toolchain package is not installed: ' + requested)
    check_llvm_version(packages, spec)
    compiler: Path = msys_root / 'clang64/bin/clang.exe'
    compiler_sha256: str = file_digest(compiler)
    archive: Path = destination / ARCHIVE_NAME
    write_archive(msys_root, archive)
    manifest: dict[str, Any] = {
        'schema': 1, 'repository': project.current().toolchain_repository, 'platform': PLATFORM,
        'archive': ARCHIVE_NAME, 'archive_sha256': file_digest(archive),
        'archive_bytes': archive.stat().st_size, 'compression': 'zstd level ' + str(ZSTD_LEVEL),
        'spec_sha256': spec_digest(spec), 'spec': spec, 'layout': LAYOUT,
        'msystem': spec['msystem'], 'root': ARCHIVE_ROOT,
        'path': [ARCHIVE_ROOT + '/clang64/bin', ARCHIVE_ROOT + '/usr/bin'],
        'compiler': COMPILER_MEMBER, 'compiler_sha256': compiler_sha256,
        'packages': packages,
        'omitted': list(EXCLUDED_DIRECTORIES) + list(EXCLUDED_FILES) + [GNUPG_DIRECTORY + '/S.*'],
        'producer': producer,
        'cache_location': ('ccache hits also require extraction so that msys64/ is at '
                           'producer.msys_root; system header paths enter preprocessed output')}
    return manifest


def verify_archive(archive: Path, manifest: dict[str, Any]) -> None:
    if manifest.get('schema') != 1 or manifest.get('platform') != PLATFORM:
        raise ValueError('Unsupported Windows toolchain manifest')
    if file_digest(archive) != manifest['archive_sha256']:
        raise ValueError('Windows toolchain archive digest mismatch')


def extract(archive: Path, manifest: dict[str, Any], destination: Path) -> Path:
    """Extract a verified archive below destination; return the new msys64 root."""
    verify_archive(archive, manifest)
    msys_root: Path = destination / ARCHIVE_ROOT
    if msys_root.exists():
        raise ValueError('Refusing to extract over an existing ' + str(msys_root))
    with tarfile.open(archive, 'r:zst') as payload:
        member: tarfile.TarInfo
        for member in payload.getmembers():
            name: PurePosixPath = PurePosixPath(member.name)
            if (not name.parts or name.is_absolute() or '..' in name.parts
                    or name.parts[0] != ARCHIVE_ROOT
                    or not (member.isfile() or member.isdir() or member.islnk() or member.issym())):
                raise ValueError('Unexpected toolchain archive member: ' + member.name)
        payload.extractall(destination, filter='data')
    # Layout 1 archives omitted these directories entirely.
    empty: str
    for empty in EMPTY_DIRECTORIES:
        (msys_root / empty).mkdir(parents=True, exist_ok=True)
    if file_digest(destination / COMPILER_MEMBER) != manifest['compiler_sha256']:
        raise ValueError('Extracted clang.exe differs from the toolchain manifest')
    return msys_root


def write_wrapper(msys_root: Path, directory: Path, msystem: str) -> Path:
    """Write the same msys2.cmd that msys2/setup-msys2 writes for `shell: msys2 {0}`."""
    directory.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        '@echo off',
        'setlocal',
        'IF NOT DEFINED MSYSTEM set MSYSTEM=' + msystem,
        'IF NOT DEFINED MSYS2_PATH_TYPE set MSYS2_PATH_TYPE=inherit',
        'set CHERE_INVOKING=1',
        str(msys_root) + '\\usr\\bin\\bash.exe -leo pipefail %*']
    wrapper: Path = directory / 'msys2.cmd'
    wrapper.write_bytes('\r\n'.join(lines).encode('utf-8'))
    return wrapper


def append_line(variable: str, line: str) -> None:
    stream: TextIO
    with Path(os.environ[variable]).open('a', encoding='utf-8') as stream:
        stream.write(line + '\n')


def release_manifest(repository: str, tag: str, directory: Path) -> dict[str, Any]:
    subprocess.run(['gh', 'release', 'download', tag, '--repo', repository,
                    '--pattern', MANIFEST_NAME, '--dir', str(directory), '--clobber'], check=True)
    path: Path = directory / MANIFEST_NAME
    if path.stat().st_size > MAXIMUM_MANIFEST_BYTES:
        raise ValueError('Windows toolchain manifest exceeds its bound')
    manifest: dict[str, Any] = json.loads(path.read_text(encoding='utf-8'))
    return manifest


def find_release(repository: str, field: str, value: str) -> tuple[str, dict[str, Any]] | None:
    """Return the newest build-<sha> release whose toolchain manifest has manifest[field] == value.

    Prints what it listed and inspected, so that a miss can be explained
    from the job log.
    """
    inspected: int = 0
    page: int
    for page in range(1, RELEASE_PAGES + 1):
        releases: list[dict[str, Any]] = ci_reuse.api(
            'repos/' + repository + '/releases?per_page=' + str(RELEASES_PER_PAGE) + '&page=' + str(page))
        print('Release page ' + str(page) + ': ' + str(len(releases)) + ' releases', flush=True)
        release: dict[str, Any]
        for release in releases:
            tag: str = release['tag_name']
            if release['draft'] or not tag.startswith('build-'):
                continue
            names: list[str] = []
            asset: dict[str, Any]
            for asset in release['assets']:
                names.append(asset['name'])
            if ARCHIVE_NAME not in names or MANIFEST_NAME not in names:
                continue
            if inspected == MAXIMUM_MANIFESTS:
                return None
            inspected += 1
            manifest: dict[str, Any]
            with tempfile.TemporaryDirectory() as directory:
                manifest = release_manifest(repository, tag, Path(directory))
            print('  ' + tag + ': ' + field + ' ' + str(manifest.get(field)), flush=True)
            if manifest.get('schema') == 1 and manifest.get(field) == value:
                return tag, manifest
        if len(releases) < RELEASES_PER_PAGE:
            break
    print('No release among ' + str(inspected) + ' inspected toolchain manifests has ' + field + ' ' + value,
          flush=True)
    return None


def pinned_release(repository: str, digest: str) -> tuple[str, dict[str, Any]] | None:
    """The release PIN_PATH names for specification digest, read by its tag (no listing).

    None, with the reason printed, when the pin names another specification
    (it must follow every specification change) or its release does not
    hold the pinned archive.
    """
    pin: dict[str, Any] = json.loads(PIN_PATH.read_text(encoding='utf-8'))
    if pin.get('spec_sha256') != digest:
        print(PIN_PATH.name + ' pins specification ' + str(pin.get('spec_sha256')) + ', not the current ' + digest,
              flush=True)
        return None
    tag: str = pin['tag']
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['gh', 'api', 'repos/' + repository + '/releases/tags/' + tag], capture_output=True, text=True)
    if completed.returncode != 0:
        print('Pinned release ' + tag + ' cannot be read: ' + completed.stderr.strip(), flush=True)
        return None
    release: dict[str, Any] = json.loads(completed.stdout)
    names: list[str] = []
    asset: dict[str, Any]
    for asset in release['assets']:
        names.append(asset['name'])
    if ARCHIVE_NAME not in names or MANIFEST_NAME not in names:
        print('Pinned release ' + tag + ' holds no toolchain archive', flush=True)
        return None
    manifest: dict[str, Any]
    with tempfile.TemporaryDirectory() as directory:
        manifest = release_manifest(repository, tag, Path(directory))
    if manifest.get('spec_sha256') != digest or manifest.get('archive_sha256') != pin.get('archive_sha256'):
        print('Pinned release ' + tag + ' holds archive ' + str(manifest.get('archive_sha256')) + ' of specification '
              + str(manifest.get('spec_sha256')) + ', not the pinned one', flush=True)
        return None
    return tag, manifest


def download_release_archive(repository: str, tag: str, manifest: dict[str, Any], destination: Path) -> Path:
    subprocess.run(['gh', 'release', 'download', tag, '--repo', repository,
                    '--pattern', ARCHIVE_NAME, '--dir', str(destination), '--clobber'], check=True)
    archive: Path = destination / ARCHIVE_NAME
    verify_archive(archive, manifest)
    (destination / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return archive


def artifact_name(archive_sha256: str) -> str:
    return project.current().toolchain_asset_prefix + PLATFORM + '-' + archive_sha256


def download_artifact(repository: str, archive_sha256: str, destination: Path) -> dict[str, Any]:
    """Fetch a not-yet-published toolchain uploaded by the validating run of this repository."""
    name: str = artifact_name(archive_sha256)
    listing: dict[str, Any] = ci_reuse.api(
        'repos/' + repository + '/actions/artifacts?per_page=30&name=' + name)
    artifact: dict[str, Any]
    for artifact in listing['artifacts']:
        origin: dict[str, Any] = artifact['workflow_run']
        if artifact['expired'] or origin['head_repository_id'] != origin['repository_id']:
            continue
        subprocess.run(['gh', 'run', 'download', str(origin['id']), '--repo', repository,
                        '--name', name, '--dir', str(destination)], check=True)
        manifest: dict[str, Any] = json.loads((destination / MANIFEST_NAME).read_text(encoding='utf-8'))
        verify_archive(destination / ARCHIVE_NAME, manifest)
        return manifest
    raise ValueError('The toolchain recorded by the promoted cache is neither published nor in an '
                     'unexpired artifact; rerun with force_validation to package it again')


def cache_toolchain(cache_archive: Path) -> dict[str, Any]:
    """Return the toolchain record from a Windows cache archive's cache-manifest.json."""
    with tarfile.open(cache_archive, 'r:gz') as payload:
        member: tarfile.TarInfo = payload.getmember('cache-manifest.json')
        if member.size > MAXIMUM_MANIFEST_BYTES:
            raise ValueError('Cache manifest exceeds its bound')
        stream: BinaryIO | None = payload.extractfile(member)
        if stream is None:
            raise ValueError('Cache manifest is not a file')
        with stream:
            manifest: dict[str, Any] = json.load(stream)
    record: Any = manifest.get('toolchain')
    if not isinstance(record, dict):
        raise ValueError('Windows cache manifest does not record its toolchain')
    if record.get('compiler_sha256') != manifest.get('compiler_sha256'):
        raise ValueError('Cache compiler differs from its recorded toolchain compiler')
    return record


def cache_record(manifest: dict[str, Any]) -> dict[str, Any]:
    """The toolchain fields that a cache manifest carries."""
    record: dict[str, Any] = {
        'archive': manifest['archive'], 'manifest': MANIFEST_NAME,
        'sha256': manifest['archive_sha256'], 'spec_sha256': manifest['spec_sha256'],
        'compiler_sha256': manifest['compiler_sha256'], 'msys_root': manifest['producer']['msys_root']}
    return record


def publication_files(root: Path) -> list[Path]:
    """Check that the toolchain beside the cache archive is the one it records."""
    archive: Path = root / ARCHIVE_NAME
    manifest_path: Path = root / MANIFEST_NAME
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding='utf-8'))
    verify_archive(archive, manifest)
    record: dict[str, Any] = cache_toolchain(root / project.current().cache_asset(PLATFORM))
    if record['sha256'] != manifest['archive_sha256'] or record['compiler_sha256'] != manifest['compiler_sha256']:
        raise ValueError('Toolchain does not match the one recorded by the cache archive')
    files: list[Path] = [archive, manifest_path]
    return files


def packaging_requested(environ: dict[str, str]) -> bool:
    return environ.get(PACKAGE_VARIABLE, '').strip().lower() in ('1', 'true')


def restore_command(repository: str) -> None:
    """Restore the published toolchain of the current specification, or stop the job.

    The pinned release is read first, then the recent releases are listed.
    Without either, the job fails with ToolchainUnavailable, unless
    PACKAGE_VARIABLE asks for a new toolchain: only then does the
    `restored` output stay false, so that setup-msys2 installs one.
    """
    spec: dict[str, Any] = load_spec()
    digest: str = spec_digest(spec)
    ci_reuse.output('packages', ' '.join(spec['packages']))
    ci_reuse.output('spec', digest)
    found: tuple[str, dict[str, Any]] | None = pinned_release(repository, digest)
    if found is None:
        found = find_release(repository, 'spec_sha256', digest)
    if found is None:
        if not packaging_requested(dict(os.environ)):
            raise ToolchainUnavailable(
                'No published Windows toolchain for specification ' + digest + ' (LLVM ' + spec['llvm'] + '). '
                'This job will not install the current MSYS2 packages in its place. Publish the pinned archive '
                'again, or, for a deliberate change of tools/windows_toolchain.json, run native.yml with '
                'package_windows_toolchain.')
        print('No published toolchain for specification ' + digest + '; packaging was requested, so setup-msys2 '
              'installs one', flush=True)
        ci_reuse.output('restored', 'false')
        return
    tag: str = found[0]
    manifest: dict[str, Any] = found[1]
    archive: Path = download_release_archive(repository, tag, manifest, ci_reuse.ROOT)
    temporary: Path = Path(os.environ['RUNNER_TEMP'])
    msys_root: Path = extract(archive, manifest, temporary)
    wrapper: Path = write_wrapper(msys_root, temporary / 'setup-msys2', manifest['msystem'])
    append_line('GITHUB_PATH', str(wrapper.parent))
    append_line('GITHUB_ENV', 'MSYSTEM=' + manifest['msystem'])
    ci_reuse.write_json(LOCAL_MANIFEST, manifest)
    print('Restored toolchain ' + manifest['archive_sha256'] + ' from ' + tag + ' without network package installs',
          flush=True)
    ci_reuse.output('restored', 'true')


def pack_command(msys_root: Path) -> None:
    spec: dict[str, Any] = load_spec()
    producer: dict[str, str] = {
        'run_id': os.environ.get('GITHUB_RUN_ID', ''), 'revision': ci_reuse.git_field('%H'),
        'runner': os.environ.get(project.current().variable('RUNNER'), ''),
        'runner_image_version': os.environ.get('ImageVersion', ''), 'msys_root': str(msys_root)}
    manifest: dict[str, Any] = pack(msys_root, ci_reuse.ROOT, spec, producer)
    (ci_reuse.ROOT / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    ci_reuse.write_json(LOCAL_MANIFEST, manifest)
    ci_reuse.output('artifact-name', artifact_name(manifest['archive_sha256']))
    print('Packaged toolchain ' + manifest['archive_sha256'] + ' (' + str(manifest['archive_bytes']) + ' bytes)',
          flush=True)


def fetch_recorded_command(repository: str) -> None:
    record: dict[str, Any] = cache_toolchain(ci_reuse.ROOT / project.current().cache_asset(PLATFORM))
    archive_sha256: str = record['sha256']
    found: tuple[str, dict[str, Any]] | None = None
    pin: dict[str, Any] = json.loads(PIN_PATH.read_text(encoding='utf-8'))
    if pin.get('archive_sha256') == archive_sha256:
        found = pinned_release(repository, str(pin.get('spec_sha256')))
    if found is None:
        found = find_release(repository, 'archive_sha256', archive_sha256)
    manifest: dict[str, Any]
    if found is None:
        manifest = download_artifact(repository, archive_sha256, ci_reuse.ROOT)
    else:
        manifest = found[1]
        download_release_archive(repository, found[0], manifest, ci_reuse.ROOT)
    if manifest['archive_sha256'] != archive_sha256:
        raise ValueError('Fetched toolchain is not the one recorded by the promoted cache')


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('restore', 'pack', 'fetch-recorded'))
    parser.add_argument('--root', type=Path, help='Installed msys64 directory to package')
    args: argparse.Namespace = parser.parse_args()
    repository: str = os.environ['GITHUB_REPOSITORY']
    if args.command == 'restore':
        restore_command(repository)
    elif args.command == 'pack':
        if args.root is None:
            raise ValueError('pack requires --root')
        pack_command(args.root.resolve())
    else:
        fetch_recorded_command(repository)


if __name__ == '__main__':
    main()
