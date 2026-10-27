#!/usr/bin/env python3
"""Fetch the GUI.Forms source SDK pinned in gui_forms.lock and build it, or pin a release.

    python3 example_app/fetch_sdk.py fetch [--target T] [--cache DIR] [--jobs N] [--allow-network-inputs]
    python3 example_app/fetch_sdk.py prefix [--target T] [--cache DIR]
    python3 example_app/fetch_sdk.py pin TAG

GUI.Forms releases its SDK as source: one gui-forms-sdk-<version>-source.tar.gz
holding gui-forms-sdk-<version>/, the release tree with its pinned
third-party sources and SDK_MANIFEST.json. The manifest's "build" key names
the SDK's build entry point (tools/build_sdk.py), which builds GUI.Forms
for one target and installs it:

    <build> --target <platform> --prefix <dir> --jobs N --work <dir>

fetch, for one target (default: this host; a target other than the host is
refused until cross builds are admitted):
1. Reuses <cache>/gui-forms-<version>-<target>/ when its build record
   names the pinned archive digest, the target and this toolchain's
   identity; nothing is downloaded or built.
2. Otherwise downloads the pinned archive, checks its SHA-256 against the
   lock before unpacking anything, and unpacks it into
   <cache>/gui-forms-sdk/<tag>/source/ (reused while its digest matches).
3. Stops, listing them, when the manifest names inputs the build still
   downloads (network_inputs), unless --allow-network-inputs is given.
4. Runs the build entry point with the harness toolchain into the install
   directory (its build tree is <install>.work/, kept for the next build),
   then writes the build record.
It prints `prefix=<install>` (the CMAKE_PREFIX_PATH) and, when the install
carries the macOS LLVM runtime, `llvm_runtime=<path>`; with GITHUB_OUTPUT
set, the same lines are appended there. prefix prints the same lines for a
build already recorded, without the network or a build.

The toolchain identity is HARNESS_TOOLCHAIN_IDENTITY when set (CI passes
harness/compiler_identity.py's, so the record matches the Actions cache
key), else a digest of the clang binary, its version, the host machine, the
macOS SDK version and harness/cmake/llvm22.cmake.

On macOS, GUI.Forms' own toolchain file reads GUI_FORMS_LLVM_RUNTIME; when
that is unset, the build gets the harness's runtime prefix
(<HARNESS_ENV_PREFIX>_LLVM_RUNTIME, else HARNESS_LLVM_RUNTIME).

pin reads a release's asset list and its SHA256SUMS.txt (no archive is
downloaded) and rewrites the lock's tag and [source] table with the source
archive named by the lock's source_asset pattern and the digest
SHA256SUMS.txt records, which must agree with the digest GitHub publishes
for the asset when it publishes one. It prints the archive's size.

The cache is outside the source tree: --cache, else HARNESS_CACHE, else
%LOCALAPPDATA%/template_harness/cache on Windows and
${XDG_CACHE_HOME:-~/.cache}/template_harness elsewhere. A cache inside the
repository is refused, so a fetched SDK never shows up as a source change.

Failures: unpacking is all or nothing (into a temporary directory beside
the destination, then renamed), so an interrupted fetch leaves no partial
source tree. A failed build leaves its install directory without a build
record; the next fetch removes it and builds again. An archive member that
would land outside its directory, or a link pointing outside, is refused.
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
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from typing import Any, BinaryIO
import urllib.error
import urllib.request
import zipfile

HERE: Path = Path(__file__).resolve().parent
REPOSITORY_ROOT: Path = HERE.parent
LOCK_PATH: Path = HERE / 'gui_forms.lock'
HARNESS_TOOLCHAIN_FILE: Path = REPOSITORY_ROOT / 'harness' / 'cmake' / 'llvm22.cmake'
PLATFORMS: tuple[str, ...] = ('windows-x64', 'macos-arm64', 'linux-x64', 'linux-arm64')
# The release's checksum list: `<sha256>  <asset name>` per line.
CHECKSUMS_ASSET: str = 'SHA256SUMS.txt'
MANIFEST_NAME: str = 'SDK_MANIFEST.json'
DEFAULT_BUILD_ENTRY: str = 'tools/build_sdk.py'
# The file that marks a complete unpack, holding the archive's digest.
UNPACKED_MARKER: str = '.fetched-sha256'
# The file that marks a complete build: the archive digest, target and toolchain identity.
BUILD_RECORD: str = '.harness-build.json'
IDENTITY_VARIABLE: str = 'HARNESS_TOOLCHAIN_IDENTITY'
CHUNK_BYTES: int = 1 << 20


class FetchError(Exception):
    """A fetch or pin that cannot proceed; the message says why and what was left."""


@dataclass(frozen=True)
class Source:
    """The pinned source archive."""
    name: str
    sha256: str


@dataclass(frozen=True)
class Lock:
    """gui_forms.lock: the release and its source archive (None until pinned)."""
    repository: str
    tag: str
    source_asset: str
    source: Source | None

    def version(self) -> str:
        result: str = self.tag[1:] if self.tag.startswith('v') else self.tag
        return result


def is_digest(value: Any) -> bool:
    result: bool = type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
    return result


def read_lock(path: Path) -> Lock:
    with path.open('rb') as stream:
        document: dict[str, Any] = tomllib.load(stream)
    if document.get('schema') != 2:
        raise FetchError(str(path) + ': schema must be 2')
    release: Any = document.get('release')
    if type(release) is not dict:
        raise FetchError(str(path) + ': [release] is missing')
    fields: list[str] = []
    key: str
    for key in ('repository', 'tag', 'source_asset'):
        value: Any = release.get(key)
        if type(value) is not str or not value:
            raise FetchError(str(path) + ': release.' + key + ' must be a non-empty string')
        fields.append(value)
    source: Source | None = None
    table: Any = document.get('source')
    if table is not None:
        if type(table) is not dict:
            raise FetchError(str(path) + ': [source] must be a table')
        name: Any = table.get('name')
        digest: Any = table.get('sha256')
        if type(name) is not str or not name.endswith('.tar.gz') or not safe_member(name) or '/' in name:
            raise FetchError(str(path) + ': source.name must name a .tar.gz release asset')
        if not is_digest(digest):
            raise FetchError(str(path) + ': source.sha256 must be 64 lowercase hex digits')
        source = Source(name, digest)
    result: Lock = Lock(fields[0], fields[1], fields[2], source)
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
    raise FetchError('GUI.Forms builds for no ' + system + ' ' + machine + ' target')


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


def install_of(cache: Path, lock: Lock, target: str) -> Path:
    result: Path = cache / ('gui-forms-' + lock.version() + '-' + target)
    return result


def source_dir_of(cache: Path, lock: Lock) -> Path:
    result: Path = cache / 'gui-forms-sdk' / lock.tag / 'source'
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


def source_root(unpacked: Path) -> Path:
    """The SDK's top directory: the one directory under unpacked holding SDK_MANIFEST.json."""
    manifests: list[Path] = sorted(unpacked.glob('*/' + MANIFEST_NAME))
    if len(manifests) != 1:
        raise FetchError(str(unpacked) + ' holds ' + str(len(manifests)) + ' top directories with '
                         + MANIFEST_NAME + '; expected one')
    result: Path = manifests[0].parent
    return result


def read_manifest(root: Path) -> dict[str, Any]:
    try:
        document: Any = json.loads((root / MANIFEST_NAME).read_text(encoding='utf-8'))
    except ValueError as error:
        raise FetchError(str(root / MANIFEST_NAME) + ' is not JSON: ' + str(error)) from error
    if type(document) is not dict:
        raise FetchError(str(root / MANIFEST_NAME) + ' is not a JSON object')
    return document


def build_entry(root: Path, manifest: dict[str, Any]) -> Path:
    """The build entry point the manifest names, inside the source tree."""
    named: Any = manifest.get('build', DEFAULT_BUILD_ENTRY)
    if type(named) is not str or not named or not safe_member(named):
        raise FetchError(MANIFEST_NAME + ': build must be a relative path inside the SDK')
    entry: Path = root / named
    if not entry.is_file():
        raise FetchError(MANIFEST_NAME + ' names the build entry ' + named + ', which the SDK does not hold')
    return entry


def network_inputs(manifest: dict[str, Any]) -> list[str]:
    """What the build still downloads, one description each (the manifest's network_inputs)."""
    listed: Any = manifest.get('network_inputs', [])
    if type(listed) is not list:
        raise FetchError(MANIFEST_NAME + ': network_inputs must be a list')
    described: list[str] = []
    item: Any
    for item in listed:
        if type(item) is dict:
            parts: list[str] = []
            key: str
            for key in sorted(item):
                parts.append(key + '=' + str(item[key]))
            described.append(', '.join(parts))
        else:
            described.append(str(item))
    return described


def toolchain_identity() -> str:
    """HARNESS_TOOLCHAIN_IDENTITY, else a digest of the compiler and the harness toolchain file."""
    named: str = os.environ.get(IDENTITY_VARIABLE, '')
    if named:
        return named
    compiler: str | None = shutil.which('clang')
    if compiler is None:
        raise FetchError('clang is not on PATH; the build needs the harness toolchain (LLVM 22.1.8)')
    values: list[str] = [file_sha256(Path(compiler)), host_platform.machine(), file_sha256(HARNESS_TOOLCHAIN_FILE)]
    values.append(subprocess.check_output(['clang', '--version'], text=True))
    if host_platform.system() == 'Darwin':
        values.append(subprocess.check_output(['xcrun', '--show-sdk-version'], text=True))
    result: str = hashlib.sha256('\n'.join(values).encode('utf-8')).hexdigest()
    return result


def harness_runtime() -> str:
    """The macOS runtime prefix harness/cmake/llvm22.cmake reads, or '' when unset."""
    prefix: str = os.environ.get('HARNESS_ENV_PREFIX', '')
    variable: str = prefix + '_LLVM_RUNTIME' if prefix else 'HARNESS_LLVM_RUNTIME'
    result: str = os.environ.get(variable, '')
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


def pinned_source(lock: Lock) -> Source:
    if lock.source is None:
        raise FetchError(LOCK_PATH.name + ' pins no source archive in ' + lock.tag
                         + '; pin a release that publishes the source SDK (fetch_sdk.py pin TAG)')
    return lock.source


def checked_target(target: str) -> str:
    host: str = running_platform()
    if target != host:
        raise FetchError('building for ' + target + ' on a ' + host + ' host is not supported yet; '
                         'build on a ' + target + ' host')
    return target


def build_record(source: Source, target: str, identity: str) -> dict[str, str]:
    result: dict[str, str] = {'archive_sha256': source.sha256, 'target': target, 'toolchain': identity}
    return result


def recorded(install: Path) -> dict[str, Any] | None:
    path: Path = install / BUILD_RECORD
    if not path.is_file():
        return None
    try:
        document: Any = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return None
    result: dict[str, Any] | None = document if type(document) is dict else None
    return result


def unpacked_source(lock: Lock, source: Source, cache: Path) -> Path:
    """The unpacked source tree's top directory, downloading and unpacking it when needed."""
    destination: Path = source_dir_of(cache, lock)
    marker: Path = destination / UNPACKED_MARKER
    if marker.is_file() and marker.read_text(encoding='utf-8').strip() == source.sha256:
        reused: Path = source_root(destination)
        return reused
    destination.parent.mkdir(parents=True, exist_ok=True)
    stale: Path
    for stale in destination.parent.glob('.source-*'):
        shutil.rmtree(stale, ignore_errors=True)
    work: Path = Path(tempfile.mkdtemp(prefix='.source-', dir=destination.parent))
    try:
        archive: Path = work / source.name
        url: str = 'https://github.com/' + lock.repository + '/releases/download/' + lock.tag + '/' + source.name
        print('downloading ' + url, file=sys.stderr, flush=True)
        download(url, archive)
        actual: str = file_sha256(archive)
        if actual != source.sha256:
            raise FetchError(source.name + ' has SHA-256 ' + actual + '; ' + LOCK_PATH.name + ' pins ' + source.sha256)
        unpacked: Path = work / 'source'
        unpacked.mkdir()
        extract(archive, source.name, unpacked)
        source_root(unpacked)
        (unpacked / UNPACKED_MARKER).write_text(source.sha256 + '\n', encoding='utf-8')
        if destination.exists():
            shutil.rmtree(destination)
        unpacked.rename(destination)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result: Path = source_root(destination)
    return result


def fetch_command(lock: Lock, target: str, cache: Path, jobs: int, allow_network_inputs: bool) -> list[str]:
    source: Source = pinned_source(lock)
    checked_target(target)
    identity: str = toolchain_identity()
    wanted: dict[str, str] = build_record(source, target, identity)
    install: Path = install_of(cache, lock, target)
    if recorded(install) == wanted:
        reused: list[str] = report(install)
        return reused
    root: Path = unpacked_source(lock, source, cache)
    manifest: dict[str, Any] = read_manifest(root)
    entry: Path = build_entry(root, manifest)
    downloads: list[str] = network_inputs(manifest)
    if downloads and not allow_network_inputs:
        raise FetchError('the SDK build still downloads: ' + '; '.join(downloads)
                         + '. Rerun with --allow-network-inputs to let it.')
    if install.exists():
        shutil.rmtree(install)
    work: Path = install.with_name(install.name + '.work')
    environment: dict[str, str] = dict(os.environ)
    runtime: str = harness_runtime()
    if target.startswith('macos') and runtime and not environment.get('GUI_FORMS_LLVM_RUNTIME', ''):
        environment['GUI_FORMS_LLVM_RUNTIME'] = runtime
    command: list[str] = [sys.executable, str(entry), '--target', target, '--prefix', str(install),
                          '--jobs', str(jobs), '--work', str(work)]
    print('+ ' + ' '.join(command), file=sys.stderr, flush=True)
    completed: subprocess.CompletedProcess[bytes] = subprocess.run(command, cwd=root, env=environment)
    if completed.returncode != 0:
        raise FetchError('the SDK build failed (exit ' + str(completed.returncode) + '); ' + str(install)
                         + ' has no build record and is rebuilt next time')
    if not (install / 'lib' / 'cmake' / 'GUIForms' / 'GUIFormsConfig.cmake').is_file():
        raise FetchError('the SDK build installed no lib/cmake/GUIForms/GUIFormsConfig.cmake into ' + str(install))
    (install / BUILD_RECORD).write_text(json.dumps(wanted, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    built: list[str] = report(install)
    return built


def prefix_command(lock: Lock, target: str, cache: Path) -> list[str]:
    source: Source = pinned_source(lock)
    install: Path = install_of(cache, lock, target)
    found: dict[str, Any] | None = recorded(install)
    if found is None or found.get('archive_sha256') != source.sha256 or found.get('target') != target:
        raise FetchError('the pinned SDK is not built for ' + target + ' into ' + str(install) + '; run fetch')
    lines: list[str] = report(install)
    return lines


def release_assets(repository: str, tag: str) -> list[dict[str, Any]]:
    """A release's asset list from the GitHub API (names, sizes and digests only)."""
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


def release_checksums(repository: str, tag: str) -> str:
    """The text of a release's SHA256SUMS.txt (a few kilobytes)."""
    url: str = 'https://github.com/' + repository + '/releases/download/' + tag + '/' + CHECKSUMS_ASSET
    request: urllib.request.Request = urllib.request.Request(url, headers={'User-Agent': 'template_harness-fetch-sdk'})
    with urllib.request.urlopen(request, timeout=60) as response:
        text: str = response.read().decode('utf-8')
    return text


def parse_checksums(text: str) -> dict[str, str]:
    """Asset name to digest from `<sha256>  <name>` lines (sha256sum's text or `*` binary marker)."""
    digests: dict[str, str] = {}
    line: str
    for line in text.splitlines():
        fields: list[str] = line.split(None, 1)
        if len(fields) != 2:
            continue
        digest: str = fields[0].lower()
        name: str = fields[1].strip().lstrip('*')
        if is_digest(digest):
            digests[name] = digest
    return digests


def pin_command(lock: Lock, tag: str) -> list[str]:
    version: str = tag[1:] if tag.startswith('v') else tag
    name: str = lock.source_asset.replace('{version}', version)
    listed: list[dict[str, Any]] = release_assets(lock.repository, tag)
    sizes: dict[str, int] = {}
    published: dict[str, str] = {}
    entry: dict[str, Any]
    for entry in listed:
        entry_name: str = str(entry.get('name'))
        sizes[entry_name] = int(entry.get('size') or 0)
        published_digest: str = str(entry.get('digest') or '')
        if published_digest.startswith('sha256:'):
            published[entry_name] = published_digest[len('sha256:'):]
    if name not in sizes:
        raise FetchError(tag + ' publishes no ' + name)
    if CHECKSUMS_ASSET not in sizes:
        raise FetchError(tag + ' publishes no ' + CHECKSUMS_ASSET)
    sums: dict[str, str] = parse_checksums(release_checksums(lock.repository, tag))
    if name not in sums:
        raise FetchError(tag + "'s " + CHECKSUMS_ASSET + ' records no digest for ' + name)
    if name in published and published[name] != sums[name]:
        raise FetchError(name + ': ' + CHECKSUMS_ASSET + ' records ' + sums[name]
                         + ' but GitHub publishes ' + published[name])
    text: str = LOCK_PATH.read_text(encoding='utf-8')
    head: str = text.split('\n[source]', 1)[0]
    head = head.replace('tag = "' + lock.tag + '"', 'tag = "' + tag + '"', 1)
    lines: list[str] = [head.rstrip('\n'), '', '[source]', 'name = "' + name + '"', 'sha256 = "' + sums[name] + '"']
    replacement: str = '\n'.join(lines) + '\n'
    staged: Path = LOCK_PATH.with_suffix('.lock.new')
    staged.write_text(replacement, encoding='utf-8', newline='\n')
    try:
        read_lock(staged)
    except FetchError:
        staged.unlink(missing_ok=True)
        raise
    staged.replace(LOCK_PATH)
    result: list[str] = ['pinned ' + tag, name + ' ' + str(sizes[name]) + ' bytes']
    return result


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__,
                                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=('fetch', 'prefix', 'pin'))
    parser.add_argument('tag', nargs='?', help='pin: the release tag to pin')
    parser.add_argument('--target', choices=PLATFORMS, help='default: this host')
    parser.add_argument('--cache', type=Path, help='default: HARNESS_CACHE, else the user cache directory')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--allow-network-inputs', action='store_true',
                        help='let the SDK build download the inputs its manifest lists')
    args: argparse.Namespace = parser.parse_args()
    try:
        lock: Lock = read_lock(LOCK_PATH)
        lines: list[str]
        if args.command == 'pin':
            if not args.tag:
                raise FetchError('pin needs a release tag')
            lines = pin_command(lock, args.tag)
        else:
            target: str = args.target or running_platform()
            cache: Path = checked_cache(args.cache or default_cache())
            if args.command == 'fetch':
                lines = fetch_command(lock, target, cache, args.jobs, args.allow_network_inputs)
            else:
                lines = prefix_command(lock, target, cache)
    except FetchError as error:
        print('fetch_sdk: ' + str(error), file=sys.stderr)
        return 1
    except urllib.error.URLError as error:
        print('fetch_sdk: ' + str(error), file=sys.stderr)
        return 1
    line: str
    for line in lines:
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
