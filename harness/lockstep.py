#!/usr/bin/env python3
"""The local lockstep GUI.Forms: one clone on this machine, built once per commit, that every product builds against.

    python3 harness/lockstep.py init [--reference DIR] [--seed DIR ...]
    python3 harness/lockstep.py advance --to REF [--from REMOTE]
    python3 harness/lockstep.py build [--target T] [--jobs N]
    python3 harness/lockstep.py prefix [--target T] [--json]
    python3 harness/lockstep.py status [--target T]

Every command takes --root DIR (else HARNESS_LOCKSTEP_ROOT, else D:/malkuth
on Windows and ~/malkuth elsewhere). Under the root:

    gui_forms_lockstep/              the lockstep workspace (CCACHE_BASEDIR)
        gui_forms/                   the clone, detached at the lockstep commit
        .build/native-<target>/      build_sdk.py's work trees, reused across commits
    cache/ccache/                    the compiler cache every local build shares
    cache/lockstep/gui-forms-<commit>-<target>/   one install per built commit
    cache/lockstep/current-<target>.json          the record naming the active install
    cache/lockstep/.lockstep.lock                 held by build and advance

The workspace is laid out as CI's (gui_forms/ beside .build/native-<target>/
gui-forms), on Windows the clone checks text out with CRLF and the build
runs with LANG=en_US.UTF-8, as CI's Windows jobs do: ccache keys objects by
the bytes and paths they read and by the locale, so the shared cache can
also take GUI.Forms' published per-commit caches.

init      clones the repository (--repository, default
          malkuth-software/gui_forms on GitHub) at its default branch,
          detached, and creates the cache directories. --reference borrows
          objects from a local clone while cloning (--dissociate, so nothing
          stays shared). --seed copies the untracked third-party sources a
          build reads (third_party/harfbuzz, freetype, libunibreak, miniaudio)
          from a GUI.Forms checkout or source SDK that has them, so the first
          build fetches nothing; GUI.Forms' fetch scripts still fetch a
          revision the clone pins and the seed lacks. Re-running init on an
          initialised tree only seeds what is missing.
advance   moves the clone to REF (fetched from --from, default origin) and
          leaves it detached. It is the coordinator's command; it refuses a
          clone with changes to tracked files, and waits for a running build.
build     builds and installs the clone's HEAD for the target (default: this
          host) with tools/build_sdk.py, once per (commit, target, toolchain
          identity), then writes the install's build record and
          current-<target>.json. A matching record means nothing is built.
          One build runs at a time; others wait on the lock, then find the
          record. A failed build leaves current-<target>.json as it was and no
          record in the install, which the next build replaces.
prefix    prints the active install's prefix (CMAKE_PREFIX_PATH), or with
          --json the whole record. It fails, saying what to run, when the
          clone's HEAD is not the built commit.
status    prints the clone's commit, whether it is built, the active prefix,
          and the shared cache's size and hit counters.

The record (current-<target>.json, and .lockstep-build.json in the install),
for product build drivers to read:

    schema              1
    commit              the GUI.Forms commit the install was built from
    target              windows-x64, macos-arm64, linux-x64 or linux-arm64
    toolchain_identity  HARNESS_TOOLCHAIN_IDENTITY, else a digest of the clang
                        binary, its version and the host
    compiler            the clang that built it (absolute path)
    prefix              the install: CMAKE_PREFIX_PATH
    package_dir         <prefix>/lib/cmake/GUIForms: GUIForms_DIR
    source              the lockstep clone (its HEAD is commit while current)
    toolchain_file      <source>/cmake/llvm22.cmake, the toolchain file used
    font_dir            <prefix>/share/GUIForms/fonts: GUI_FORMS_FONT_DIR
    llvm_runtime        GUI_FORMS_LLVM_RUNTIME of the build (macOS), else ''
    ccache_dir          the shared compiler cache
    ccache_basedir      the lockstep workspace
    lang                the LANG the build ran with ('' when left alone)
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform as host_platform
import shutil
import subprocess
import sys
import time
from typing import Any, BinaryIO

DEFAULT_REPOSITORY: str = 'https://github.com/malkuth-software/gui_forms.git'
ROOT_VARIABLE: str = 'HARNESS_LOCKSTEP_ROOT'
IDENTITY_VARIABLE: str = 'HARNESS_TOOLCHAIN_IDENTITY'
WORKSPACE_NAME: str = 'gui_forms_lockstep'
SOURCE_NAME: str = 'gui_forms'
MARKER_NAME: str = '.lockstep-tree'
RECORD_NAME: str = '.lockstep-build.json'
LOCK_NAME: str = '.lockstep.lock'
RECORD_SCHEMA: int = 1
TARGETS: tuple[str, ...] = ('windows-x64', 'macos-arm64', 'linux-x64', 'linux-arm64')
# The untracked sources a build reads, which init can copy from a seed.
SEEDED_SOURCES: tuple[str, ...] = ('harfbuzz', 'freetype', 'libunibreak', 'miniaudio')
# The fields that decide whether an install is the one wanted.
RECORD_KEY: tuple[str, ...] = ('commit', 'target', 'toolchain_identity')
# CI's Windows build shell sets this LANG, and ccache hashes the locale.
WINDOWS_LANG: str = 'en_US.UTF-8'
CCACHE_MAXSIZE: str = '20G'
# Waiting for the lock: poll interval where locks cannot block (Windows),
# and attempts at replacing the record while a reader holds it open.
LOCK_POLL_SECONDS: float = 0.5
REPLACE_ATTEMPTS: int = 20


class LockstepError(Exception):
    """A command that cannot proceed; the message says why and what was left."""


@dataclass(frozen=True)
class Layout:
    """Where the lockstep clone, its build trees, the shared cache and the installs live under one root."""
    root: Path
    workspace: Path
    source: Path
    marker: Path
    cache: Path
    ccache: Path
    installs: Path
    lock_file: Path


def layout_of(root: Path) -> Layout:
    workspace: Path = root / WORKSPACE_NAME
    cache: Path = root / 'cache'
    installs: Path = cache / 'lockstep'
    result: Layout = Layout(root, workspace, workspace / SOURCE_NAME, workspace / MARKER_NAME, cache,
                            cache / 'ccache', installs, installs / LOCK_NAME)
    return result


def default_root() -> Path:
    named: str = os.environ.get(ROOT_VARIABLE, '')
    if named:
        return Path(named)
    result: Path = Path('D:/malkuth') if host_platform.system() == 'Windows' else Path.home() / 'malkuth'
    return result


def host_target() -> str:
    """The target of this machine, as tools/build_sdk.py names it."""
    systems: dict[str, str] = {'Darwin': 'macos', 'Linux': 'linux', 'Windows': 'windows'}
    system: str = systems.get(host_platform.system(), host_platform.system().lower())
    architecture: str = 'arm64' if host_platform.machine().lower() in ('arm64', 'aarch64') else 'x64'
    result: str = system + '-' + architecture
    return result


def git(checkout: Path, *arguments: str) -> str:
    """git's standard output in checkout, stripped; LockstepError with git's message when it fails."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(['git', '-C', str(checkout), *arguments],
                                                                 capture_output=True, text=True)
    if completed.returncode != 0:
        raise LockstepError('git ' + ' '.join(arguments) + ' failed: ' + completed.stderr.strip())
    result: str = completed.stdout.strip()
    return result


def checked_tree(layout: Layout) -> None:
    """Raise unless init made the lockstep clone."""
    if not layout.marker.is_file() or not (layout.source / '.git').exists():
        raise LockstepError('no lockstep clone at ' + layout.source.as_posix() + '; run lockstep.py init')


def head_commit(layout: Layout) -> str:
    result: str = git(layout.source, 'rev-parse', 'HEAD')
    return result


def refuse_tracked_changes(layout: Layout) -> None:
    changes: str = git(layout.source, 'status', '--porcelain', '--untracked-files=no')
    if changes:
        raise LockstepError('the lockstep clone has changes to tracked files; it builds commits only:\n' + changes)


class TreeLock:
    """The lockstep lock file, held exclusively by one build or advance; others wait for it.

    The operating system releases the lock when its holder exits, so a killed
    build never leaves the tree locked. The file's contents are not used.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream: BinaryIO | None = None

    def __enter__(self) -> TreeLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream: BinaryIO = open(self.path, 'a+b')
        announced: bool = False
        while not try_lock(stream):
            if not announced:
                print('lockstep: waiting for another lockstep build or advance to finish', file=sys.stderr,
                      flush=True)
                announced = True
            time.sleep(LOCK_POLL_SECONDS)
        self.stream = stream
        return self

    def __exit__(self, kind: object, value: object, trace: object) -> None:
        stream: BinaryIO | None = self.stream
        self.stream = None
        if stream is not None:
            unlock(stream)
            stream.close()


def try_lock(stream: BinaryIO) -> bool:
    """Take the exclusive lock on stream's first byte without waiting; whether it was taken."""
    if host_platform.system() == 'Windows':
        import msvcrt
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock(stream: BinaryIO) -> None:
    if host_platform.system() == 'Windows':
        import msvcrt
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def seed_sources(layout: Layout, seeds: list[Path]) -> list[str]:
    """Copy each missing SEEDED_SOURCES directory from the first seed that has it; the names still missing."""
    missing: list[str] = []
    name: str
    for name in SEEDED_SOURCES:
        destination: Path = layout.source / 'third_party' / name
        if destination.exists():
            continue
        found: Path | None = None
        seed: Path
        for seed in seeds:
            candidate: Path = seed / 'third_party' / name
            if candidate.is_dir():
                found = candidate
                break
        if found is None:
            missing.append(name)
            continue
        print('lockstep: seeding third_party/' + name + ' from ' + found.as_posix(), file=sys.stderr, flush=True)
        shutil.copytree(found, destination, symlinks=True)
    return missing


def init_command(layout: Layout, repository: str, reference: Path | None, seeds: list[Path]) -> list[str]:
    layout.ccache.mkdir(parents=True, exist_ok=True)
    layout.installs.mkdir(parents=True, exist_ok=True)
    if not layout.marker.is_file():
        if layout.source.exists() and any(layout.source.iterdir()):
            raise LockstepError(layout.source.as_posix() + ' exists and is not a lockstep clone; remove it first')
        layout.workspace.mkdir(parents=True, exist_ok=True)
        command: list[str] = ['git', 'clone', '--quiet']
        if host_platform.system() == 'Windows':
            # CI's Windows checkout writes text with CRLF whatever this user's git says.
            command += ['--config', 'core.autocrlf=true']
        if reference is not None:
            command += ['--reference-if-able', str(reference), '--dissociate']
        command += [repository, str(layout.source)]
        completed: subprocess.CompletedProcess[str] = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode != 0:
            raise LockstepError('cloning ' + repository + ' failed: ' + completed.stderr.strip())
        git(layout.source, 'checkout', '--quiet', '--detach')
        layout.marker.write_text('lockstep clone of ' + repository + '\n', encoding='utf-8')
    missing: list[str] = seed_sources(layout, seeds)
    lines: list[str] = ['source=' + layout.source.as_posix(), 'commit=' + head_commit(layout)]
    if missing:
        lines.append('unseeded=' + ','.join(missing))
    return lines


def advance_command(layout: Layout, ref: str, remote: str) -> list[str]:
    checked_tree(layout)
    with TreeLock(layout.lock_file):
        refuse_tracked_changes(layout)
        before: str = head_commit(layout)
        git(layout.source, 'fetch', '--quiet', remote, ref)
        git(layout.source, 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
        after: str = head_commit(layout)
    lines: list[str] = ['from=' + before, 'commit=' + after]
    return lines


def file_sha256(path: Path) -> str:
    digest: Any = hashlib.sha256()
    stream: BinaryIO
    with path.open('rb') as stream:
        chunk: bytes = stream.read(1 << 20)
        while chunk:
            digest.update(chunk)
            chunk = stream.read(1 << 20)
    result: str = digest.hexdigest()
    return result


def compiler_path() -> Path:
    found: str | None = shutil.which('clang')
    if found is None:
        raise LockstepError('clang is not on PATH; the build needs the pinned LLVM 22.1.8 toolchain')
    result: Path = Path(found).resolve()
    return result


def toolchain_identity(compiler: Path) -> str:
    """HARNESS_TOOLCHAIN_IDENTITY, else a digest of the compiler binary, its version and the host."""
    named: str = os.environ.get(IDENTITY_VARIABLE, '')
    if named:
        return named
    values: list[str] = [file_sha256(compiler), host_platform.machine(),
                         subprocess.check_output([str(compiler), '--version'], text=True)]
    if host_platform.system() == 'Darwin':
        values.append(subprocess.check_output(['xcrun', '--show-sdk-version'], text=True))
    result: str = hashlib.sha256('\n'.join(values).encode('utf-8')).hexdigest()
    return result


def install_of(layout: Layout, commit: str, target: str) -> Path:
    result: Path = layout.installs / ('gui-forms-' + commit + '-' + target)
    return result


def current_of(layout: Layout, target: str) -> Path:
    result: Path = layout.installs / ('current-' + target + '.json')
    return result


def read_record(path: Path) -> dict[str, Any] | None:
    """The record at path, or None when it is missing or unreadable."""
    try:
        value: Any = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    result: dict[str, Any] | None = value if type(value) is dict else None
    return result


def same_build(record: dict[str, Any] | None, wanted: dict[str, Any]) -> bool:
    if record is None:
        return False
    name: str
    for name in RECORD_KEY:
        if record.get(name) != wanted[name]:
            return False
    return True


def write_record(path: Path, record: dict[str, Any]) -> None:
    """Replace path with record in one step, so a reader sees the old or the new record and never a part."""
    staged: Path = path.with_name(path.name + '.new')
    staged.write_text(json.dumps(record, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    attempt: int
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(staged, path)
            return
        except PermissionError:
            # Windows refuses the replace while a reader has the file open.
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(LOCK_POLL_SECONDS)


def build_environment(layout: Layout, target: str) -> dict[str, str]:
    """The build's environment: ccache in the lockstep layout, and on Windows CI's LANG."""
    environment: dict[str, str] = dict(os.environ)
    environment['CCACHE_DIR'] = layout.ccache.as_posix()
    environment['CCACHE_BASEDIR'] = layout.workspace.as_posix()
    environment['CCACHE_COMPILERCHECK'] = 'content'
    environment['CCACHE_MAXSIZE'] = CCACHE_MAXSIZE
    language: str
    for language in ('C', 'CXX', 'OBJCXX'):
        environment['CMAKE_' + language + '_COMPILER_LAUNCHER'] = 'ccache'
    if target.startswith('windows'):
        environment['LANG'] = WINDOWS_LANG
    return environment


def ccache_counters(environment: dict[str, str]) -> dict[str, int]:
    """ccache --print-stats for the build's cache directory, as name: value."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(['ccache', '--print-stats'], env=environment,
                                                                 capture_output=True, text=True)
    values: dict[str, int] = {}
    if completed.returncode != 0:
        return values
    line: str
    for line in completed.stdout.splitlines():
        fields: list[str] = line.split('\t')
        if len(fields) == 2 and fields[1].strip().isdigit():
            values[fields[0]] = int(fields[1])
    return values


def hits_and_compilations(before: dict[str, int], after: dict[str, int]) -> tuple[int, int]:
    """Cache hits and cacheable compilations between two counter readings (other users' included)."""
    hit_names: tuple[str, ...] = ('direct_cache_hit', 'preprocessed_cache_hit')
    hits: int = 0
    name: str
    for name in hit_names:
        hits += after.get(name, 0) - before.get(name, 0)
    misses: int = after.get('cache_miss', 0) - before.get('cache_miss', 0)
    result: tuple[int, int] = (hits, hits + misses)
    return result


def full_record(layout: Layout, commit: str, target: str, identity: str, compiler: Path,
                environment: dict[str, str]) -> dict[str, Any]:
    prefix: Path = install_of(layout, commit, target)
    record: dict[str, Any] = {
        'schema': RECORD_SCHEMA, 'commit': commit, 'target': target, 'toolchain_identity': identity,
        'compiler': compiler.as_posix(), 'prefix': prefix.as_posix(),
        'package_dir': (prefix / 'lib' / 'cmake' / 'GUIForms').as_posix(),
        'source': layout.source.as_posix(), 'toolchain_file': (layout.source / 'cmake' / 'llvm22.cmake').as_posix(),
        'font_dir': (prefix / 'share' / 'GUIForms' / 'fonts').as_posix(),
        'llvm_runtime': environment.get('GUI_FORMS_LLVM_RUNTIME', '') if target.startswith('macos') else '',
        'ccache_dir': layout.ccache.as_posix(), 'ccache_basedir': layout.workspace.as_posix(),
        'lang': environment.get('LANG', '') if target.startswith('windows') else ''}
    return record


def build_command(layout: Layout, target: str, jobs: int) -> list[str]:
    checked_tree(layout)
    if target != host_target():
        raise LockstepError('cannot build ' + target + ' on this ' + host_target() + ' host')
    compiler: Path = compiler_path()
    identity: str = toolchain_identity(compiler)
    with TreeLock(layout.lock_file):
        refuse_tracked_changes(layout)
        commit: str = head_commit(layout)
        environment: dict[str, str] = build_environment(layout, target)
        wanted: dict[str, Any] = full_record(layout, commit, target, identity, compiler, environment)
        prefix: Path = install_of(layout, commit, target)
        recorded: dict[str, Any] | None = read_record(prefix / RECORD_NAME)
        if same_build(recorded, wanted) and recorded is not None:
            if not same_build(read_record(current_of(layout, target)), wanted):
                write_record(current_of(layout, target), recorded)
            up_to_date: list[str] = ['prefix=' + prefix.as_posix(), 'built=already']
            return up_to_date
        if prefix.exists():
            shutil.rmtree(prefix)
        before: dict[str, int] = ccache_counters(environment)
        work: Path = layout.workspace / '.build' / ('native-' + target)
        command: list[str] = [sys.executable, str(layout.source / 'tools' / 'build_sdk.py'), '--target', target,
                              '--prefix', str(prefix), '--jobs', str(jobs), '--work', str(work)]
        print('+ ' + ' '.join(command), file=sys.stderr, flush=True)
        completed: subprocess.CompletedProcess[bytes] = subprocess.run(command, cwd=layout.source, env=environment)
        if completed.returncode != 0:
            raise LockstepError('the build of ' + commit + ' failed (exit ' + str(completed.returncode) + '); '
                                + current_of(layout, target).name + ' is unchanged')
        if not (prefix / 'lib' / 'cmake' / 'GUIForms' / 'GUIFormsConfig.cmake').is_file():
            raise LockstepError('the build installed no lib/cmake/GUIForms/GUIFormsConfig.cmake into '
                                + prefix.as_posix())
        hits: int
        compilations: int
        hits, compilations = hits_and_compilations(before, ccache_counters(environment))
        write_record(prefix / RECORD_NAME, wanted)
        write_record(current_of(layout, target), wanted)
    built: list[str] = ['prefix=' + prefix.as_posix(), 'built=' + commit,
                        'compiler_cache_hits=' + str(hits) + '/' + str(compilations)]
    return built


def active_record(layout: Layout, target: str) -> dict[str, Any]:
    """current-<target>.json, checked against the install's record and the clone's HEAD."""
    checked_tree(layout)
    current: dict[str, Any] | None = read_record(current_of(layout, target))
    if current is None:
        raise LockstepError('nothing is built for ' + target + '; run lockstep.py build')
    installed: dict[str, Any] | None = read_record(Path(str(current.get('prefix', ''))) / RECORD_NAME)
    if not same_build(installed, current):
        raise LockstepError(current_of(layout, target).name + ' names an install without its build record; '
                            'run lockstep.py build')
    commit: str = head_commit(layout)
    if current.get('commit') != commit:
        raise LockstepError('the lockstep clone is at ' + commit + ' but the build for ' + target + ' is of '
                            + str(current.get('commit')) + '; run lockstep.py build')
    return current


def prefix_command(layout: Layout, target: str, as_json: bool) -> list[str]:
    record: dict[str, Any] = active_record(layout, target)
    if as_json:
        printed: list[str] = [json.dumps(record, indent=2, sort_keys=True)]
        return printed
    result: list[str] = [str(record['prefix'])]
    return result


def status_command(layout: Layout, target: str) -> list[str]:
    checked_tree(layout)
    commit: str = head_commit(layout)
    current: dict[str, Any] | None = read_record(current_of(layout, target))
    built: str = 'yes' if current is not None and current.get('commit') == commit else 'no'
    lines: list[str] = ['source=' + layout.source.as_posix(), 'commit=' + commit, 'built=' + built,
                        'prefix=' + (str(current.get('prefix', '')) if current is not None else '')]
    environment: dict[str, str] = build_environment(layout, target)
    counters: dict[str, int] = ccache_counters(environment)
    hits: int = counters.get('direct_cache_hit', 0) + counters.get('preprocessed_cache_hit', 0)
    lines.append('ccache_dir=' + layout.ccache.as_posix())
    lines.append('ccache_size_mib=' + str(counters.get('cache_size_kibibyte', 0) // 1024))
    lines.append('ccache_hits=' + str(hits) + '/' + str(hits + counters.get('cache_miss', 0)))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=None, help='default: ' + ROOT_VARIABLE + ', else D:/malkuth '
                        'on Windows and ~/malkuth elsewhere')
    commands: Any = parser.add_subparsers(dest='command', required=True)
    init_parser: argparse.ArgumentParser = commands.add_parser('init')
    init_parser.add_argument('--repository', default=DEFAULT_REPOSITORY)
    init_parser.add_argument('--reference', type=Path, default=None)
    init_parser.add_argument('--seed', type=Path, action='append', default=[])
    advance_parser: argparse.ArgumentParser = commands.add_parser('advance')
    advance_parser.add_argument('--to', required=True)
    advance_parser.add_argument('--from', dest='remote', default='origin')
    build_parser: argparse.ArgumentParser = commands.add_parser('build')
    build_parser.add_argument('--target', choices=TARGETS, default=None)
    build_parser.add_argument('--jobs', type=int, default=os.cpu_count() or 4)
    prefix_parser: argparse.ArgumentParser = commands.add_parser('prefix')
    prefix_parser.add_argument('--target', choices=TARGETS, default=None)
    prefix_parser.add_argument('--json', action='store_true')
    status_parser: argparse.ArgumentParser = commands.add_parser('status')
    status_parser.add_argument('--target', choices=TARGETS, default=None)
    args: argparse.Namespace = parser.parse_args(argv)
    layout: Layout = layout_of((args.root if args.root is not None else default_root()).resolve())
    target: str = getattr(args, 'target', None) or host_target()
    lines: list[str]
    try:
        if args.command == 'init':
            lines = init_command(layout, args.repository, args.reference, args.seed)
        elif args.command == 'advance':
            lines = advance_command(layout, args.to, args.remote)
        elif args.command == 'build':
            if args.jobs < 1:
                parser.error('--jobs must be positive')
            lines = build_command(layout, target, args.jobs)
        elif args.command == 'prefix':
            lines = prefix_command(layout, target, args.json)
        else:
            lines = status_command(layout, target)
    except LockstepError as error:
        print('lockstep: ' + str(error), file=sys.stderr)
        return 1
    line: str
    for line in lines:
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
