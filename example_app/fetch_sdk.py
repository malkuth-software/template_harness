#!/usr/bin/env python3
"""Fetch the GUI.Forms SDK pinned in gui_forms.lock, or pin a release.

    python3 example_app/fetch_sdk.py fetch [--platform P] [--cache DIR]
    python3 example_app/fetch_sdk.py prefix [--platform P] [--cache DIR]
    python3 example_app/fetch_sdk.py pin TAG

fetch downloads the running platform's SDK archive from the pinned release,
checks its SHA-256 against the lock before unpacking anything, unpacks it
into <cache>/gui-forms-sdk/<tag>/<platform>/ and prints `prefix=<path>` (the
CMAKE_PREFIX_PATH) and, when the SDK carries the macOS LLVM runtime,
`llvm_runtime=<path>`. An SDK already unpacked for the same digest is reused
without a download. With GITHUB_OUTPUT set, the same lines are appended
there. prefix prints the same lines for an SDK already fetched, without the
network.

pin reads a release's asset list (names and the digests GitHub publishes;
no archive is downloaded) and rewrites the lock's [sdk] table with every
platform's archive named by the lock's sdk_asset pattern.

The cache is outside the source tree: --cache, else HARNESS_CACHE, else
%LOCALAPPDATA%/template_harness/cache on Windows and
${XDG_CACHE_HOME:-~/.cache}/template_harness elsewhere. A cache inside the
repository is refused, so a fetched SDK never shows up as a source change.

Unpacking is all or nothing: the archive is extracted into a temporary
directory beside the destination and renamed into place, so an interrupted
fetch leaves no partial SDK (at most a temporary directory, removed by the
next fetch). An archive member that would land outside its directory, or a
link, is refused.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform as host_platform
import shutil
import sys
import tarfile
import tempfile
import tomllib
from typing import Any, BinaryIO
import urllib.request
import zipfile

HERE: Path = Path(__file__).resolve().parent
REPOSITORY_ROOT: Path = HERE.parent
LOCK_PATH: Path = HERE / 'gui_forms.lock'
PLATFORMS: tuple[str, ...] = ('windows-x64', 'macos-arm64', 'linux-x64', 'linux-arm64')
ARCHIVE_SUFFIXES: tuple[str, ...] = ('.tar.gz', '.zip')
# The file that marks a complete unpack, holding the archive's digest.
COMPLETE_MARKER: str = '.fetched-sha256'
CHUNK_BYTES: int = 1 << 20


class FetchError(Exception):
    """A fetch or pin that cannot proceed; the message says why and nothing was changed."""


@dataclass(frozen=True)
class Asset:
    """One platform's pinned SDK archive."""
    platform: str
    name: str
    sha256: str


@dataclass(frozen=True)
class Lock:
    """gui_forms.lock: the release and each platform's archive."""
    repository: str
    tag: str
    sdk_asset: str
    assets: dict[str, Asset]


def read_lock(path: Path) -> Lock:
    with path.open('rb') as stream:
        document: dict[str, Any] = tomllib.load(stream)
    if document.get('schema') != 1:
        raise FetchError(str(path) + ': schema must be 1')
    release: Any = document.get('release')
    if type(release) is not dict:
        raise FetchError(str(path) + ': [release] is missing')
    fields: list[str] = []
    key: str
    for key in ('repository', 'tag', 'sdk_asset'):
        value: Any = release.get(key)
        if type(value) is not str or not value:
            raise FetchError(str(path) + ': release.' + key + ' must be a non-empty string')
        fields.append(value)
    table: Any = document.get('sdk', {})
    if type(table) is not dict:
        raise FetchError(str(path) + ': [sdk] must be a table')
    assets: dict[str, Asset] = {}
    name: str
    for name in table:
        entry: Any = table[name]
        if name not in PLATFORMS or type(entry) is not dict:
            raise FetchError(str(path) + ': [sdk.' + name + '] is not a known platform table')
        asset_name: Any = entry.get('name')
        digest: Any = entry.get('sha256')
        if type(asset_name) is not str or not asset_name.endswith(ARCHIVE_SUFFIXES):
            raise FetchError(str(path) + ': sdk.' + name + '.name must name a .tar.gz or .zip archive')
        if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise FetchError(str(path) + ': sdk.' + name + '.sha256 must be 64 lowercase hex digits')
        assets[name] = Asset(name, asset_name, digest)
    result: Lock = Lock(fields[0], fields[1], fields[2], assets)
    return result


def running_platform() -> str:
    """The platform name of this host, as the lock and GUI.Forms' releases spell it."""
    system: str = host_platform.system()
    machine: str = host_platform.machine().lower()
    arm: bool = machine in ('arm64', 'aarch64')
    if system == 'Windows' and not arm:
        return 'windows-x64'
    if system == 'Darwin' and arm:
        return 'macos-arm64'
    if system == 'Linux':
        result: str = 'linux-arm64' if arm else 'linux-x64'
        return result
    raise FetchError('GUI.Forms publishes no SDK for ' + system + ' ' + machine)


def default_cache() -> Path:
    named: str = os.environ.get('HARNESS_CACHE', '')
    if named:
        return Path(named)
    if host_platform.system() == 'Windows':
        local: str = os.environ.get('LOCALAPPDATA', '')
        if local:
            return Path(local) / 'template_harness' / 'cache'
    xdg: str = os.environ.get('XDG_CACHE_HOME', '')
    base: Path = Path(xdg) if xdg else Path.home() / '.cache'
    result: Path = base / 'template_harness'
    return result


def checked_cache(cache: Path) -> Path:
    resolved: Path = cache.resolve()
    if resolved == REPOSITORY_ROOT or REPOSITORY_ROOT in resolved.parents:
        raise FetchError('the cache ' + str(resolved) + ' is inside the repository; choose a directory outside it')
    return resolved


def destination_of(cache: Path, lock: Lock, platform: str) -> Path:
    result: Path = cache / 'gui-forms-sdk' / lock.tag / platform
    return result


def file_sha256(path: Path) -> str:
    digest: Any = hashlib.sha256()
    stream: BinaryIO
    with path.open('rb') as stream:
        while True:
            chunk: bytes = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    result: str = digest.hexdigest()
    return result


def download(url: str, target: Path) -> None:
    """Stream url into target; a failure leaves no target behind."""
    request: urllib.request.Request = urllib.request.Request(url, headers={'User-Agent': 'template_harness-fetch-sdk'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, target.open('wb') as stream:
            shutil.copyfileobj(response, stream, CHUNK_BYTES)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def safe_member(name: str) -> bool:
    """Whether an archive member's path stays inside the extraction directory."""
    path: PurePosixPath = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or (path.parts and ':' in path.parts[0]):
        return False
    return True


def extract(archive: Path, name: str, into: Path) -> None:
    """Unpack a whole archive into an empty directory, refusing unsafe members first."""
    if name.endswith('.zip'):
        with zipfile.ZipFile(archive) as bundle:
            member: zipfile.ZipInfo
            for member in bundle.infolist():
                if not safe_member(member.filename):
                    raise FetchError(name + ': member ' + member.filename + ' would land outside the SDK')
            bundle.extractall(into)
        return
    with tarfile.open(archive, 'r:gz') as bundle:
        entries: list[tarfile.TarInfo] = bundle.getmembers()
        entry: tarfile.TarInfo
        for entry in entries:
            if not safe_member(entry.name) or entry.islnk() or entry.isdev():
                raise FetchError(name + ': member ' + entry.name + ' would land outside the SDK or is not a file')
            if entry.issym() and not safe_member(str(PurePosixPath(entry.name).parent / entry.linkname)):
                raise FetchError(name + ': link ' + entry.name + ' points outside the SDK')
        if hasattr(tarfile, 'data_filter'):
            bundle.extractall(into, filter='data')
        else:
            bundle.extractall(into)


def sdk_prefix(root: Path) -> Path:
    """The install prefix inside an unpacked SDK: the directory above lib/cmake/GUIForms."""
    configs: list[Path] = sorted(root.rglob('GUIFormsConfig.cmake'))
    if len(configs) != 1:
        raise FetchError(str(root) + ' holds ' + str(len(configs)) + ' GUIFormsConfig.cmake files; expected one')
    package_dir: Path = configs[0].parent
    if package_dir.name != 'GUIForms' or package_dir.parent.name != 'cmake':
        raise FetchError(str(configs[0]) + ' is not under <prefix>/<libdir>/cmake/GUIForms')
    result: Path = package_dir.parent.parent.parent
    return result


def report(prefix: Path) -> list[str]:
    lines: list[str] = ['prefix=' + prefix.as_posix()]
    runtime: Path = prefix / 'llvm-runtime'
    if runtime.is_dir():
        lines.append('llvm_runtime=' + runtime.as_posix())
    output: str = os.environ.get('GITHUB_OUTPUT', '')
    if output:
        with open(output, 'a', encoding='utf-8') as stream:
            stream.write('\n'.join(lines) + '\n')
    return lines


def pinned_asset(lock: Lock, platform: str) -> Asset:
    asset: Asset | None = lock.assets.get(platform)
    if asset is None:
        raise FetchError(LOCK_PATH.name + ' pins no SDK archive for ' + platform + ' in ' + lock.tag
                         + '; pin a release that publishes SDK archives (fetch_sdk.py pin TAG)')
    return asset


def fetch_command(lock: Lock, platform: str, cache: Path) -> list[str]:
    asset: Asset = pinned_asset(lock, platform)
    destination: Path = destination_of(cache, lock, platform)
    marker: Path = destination / COMPLETE_MARKER
    if marker.is_file() and marker.read_text(encoding='utf-8').strip() == asset.sha256:
        reused: list[str] = report(sdk_prefix(destination))
        return reused
    destination.parent.mkdir(parents=True, exist_ok=True)
    stale: Path
    for stale in destination.parent.glob('.' + platform + '-*'):
        shutil.rmtree(stale, ignore_errors=True)
    work: Path = Path(tempfile.mkdtemp(prefix='.' + platform + '-', dir=destination.parent))
    try:
        archive: Path = work / asset.name
        url: str = 'https://github.com/' + lock.repository + '/releases/download/' + lock.tag + '/' + asset.name
        print('downloading ' + url, file=sys.stderr, flush=True)
        download(url, archive)
        actual: str = file_sha256(archive)
        if actual != asset.sha256:
            raise FetchError(asset.name + ' has SHA-256 ' + actual + '; ' + LOCK_PATH.name + ' pins ' + asset.sha256)
        unpacked: Path = work / 'sdk'
        unpacked.mkdir()
        extract(archive, asset.name, unpacked)
        sdk_prefix(unpacked)
        (unpacked / COMPLETE_MARKER).write_text(asset.sha256 + '\n', encoding='utf-8')
        if destination.exists():
            shutil.rmtree(destination)
        unpacked.rename(destination)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    fetched: list[str] = report(sdk_prefix(destination))
    return fetched


def prefix_command(lock: Lock, platform: str, cache: Path) -> list[str]:
    asset: Asset = pinned_asset(lock, platform)
    destination: Path = destination_of(cache, lock, platform)
    marker: Path = destination / COMPLETE_MARKER
    if not marker.is_file() or marker.read_text(encoding='utf-8').strip() != asset.sha256:
        raise FetchError('the pinned SDK for ' + platform + ' is not fetched into ' + str(destination) + '; run fetch')
    lines: list[str] = report(sdk_prefix(destination))
    return lines


def release_assets(repository: str, tag: str) -> list[dict[str, Any]]:
    """A release's asset list from the GitHub API (names and digests only)."""
    url: str = 'https://api.github.com/repos/' + repository + '/releases/tags/' + tag
    headers: dict[str, str] = {'Accept': 'application/vnd.github+json', 'User-Agent': 'template_harness-fetch-sdk'}
    token: str = os.environ.get('GH_TOKEN', '') or os.environ.get('GITHUB_TOKEN', '')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    request: urllib.request.Request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        document: dict[str, Any] = json.load(response)
    assets: list[dict[str, Any]] = list(document.get('assets', []))
    return assets


def pin_command(lock: Lock, tag: str) -> list[str]:
    version: str = tag[1:] if tag.startswith('v') else tag
    listed: list[dict[str, Any]] = release_assets(lock.repository, tag)
    by_name: dict[str, str] = {}
    entry: dict[str, Any]
    for entry in listed:
        digest: str = str(entry.get('digest') or '')
        if digest.startswith('sha256:'):
            by_name[str(entry.get('name'))] = digest[len('sha256:'):]
    pinned: list[Asset] = []
    platform: str
    for platform in PLATFORMS:
        stem: str = lock.sdk_asset.replace('{version}', version).replace('{platform}', platform)
        suffix: str
        for suffix in ARCHIVE_SUFFIXES:
            if stem + suffix in by_name:
                pinned.append(Asset(platform, stem + suffix, by_name[stem + suffix]))
                break
        else:
            raise FetchError(tag + ' publishes no ' + stem + '.tar.gz or .zip with a digest')
    text: str = LOCK_PATH.read_text(encoding='utf-8')
    head: str = text.split('\n[sdk]', 1)[0]
    head = head.replace('tag = "' + lock.tag + '"', 'tag = "' + tag + '"', 1)
    lines: list[str] = [head.rstrip('\n'), '', '[sdk]']
    asset: Asset
    for asset in pinned:
        lines.append('')
        lines.append('[sdk.' + asset.platform + ']')
        lines.append('name = "' + asset.name + '"')
        lines.append('sha256 = "' + asset.sha256 + '"')
    replacement: str = '\n'.join(lines) + '\n'
    staged: Path = LOCK_PATH.with_suffix('.lock.new')
    staged.write_text(replacement, encoding='utf-8', newline='\n')
    read_lock(staged)
    staged.replace(LOCK_PATH)
    result: list[str] = ['pinned ' + tag + ': ' + ', '.join(a.platform for a in pinned)]
    return result


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__,
                                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('fetch', 'prefix', 'pin'))
    parser.add_argument('tag', nargs='?', help='pin: the release tag to pin')
    parser.add_argument('--platform', choices=PLATFORMS, help='default: this host')
    parser.add_argument('--cache', type=Path, help='default: HARNESS_CACHE, else the user cache directory')
    args: argparse.Namespace = parser.parse_args()
    try:
        lock: Lock = read_lock(LOCK_PATH)
        lines: list[str]
        if args.command == 'pin':
            if not args.tag:
                raise FetchError('pin needs a release tag')
            lines = pin_command(lock, args.tag)
        else:
            platform: str = args.platform or running_platform()
            cache: Path = checked_cache(args.cache or default_cache())
            if args.command == 'fetch':
                lines = fetch_command(lock, platform, cache)
            else:
                lines = prefix_command(lock, platform, cache)
    except FetchError as error:
        print('fetch_sdk: ' + str(error), file=sys.stderr)
        return 1
    line: str
    for line in lines:
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
