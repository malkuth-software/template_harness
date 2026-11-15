"""Run the early-exit integrity check (docs/early_exit_check.md) over a product's compile database.

The rule, shared by GUI.Forms and STXlib: every call to code we don't control
is a possible early exit; at each such call the invariants are whole (form 1)
or the broken state is safe to unwind through (form 2); allocators do not
unwind; termination frees nothing twice. The vocabulary (marks, site
markers, exit classes, rules R1 to R10, patterns P1 to P13) is STXlib's,
defined in its audit E, section 1.

The findings come from a clang tool, early_exit_check (harness/early_exit/),
built here once against the pinned LLVM 22's Clang libraries and run over
the first-party units of a configured ninja tree. Each entry prints as

  path:line:column: R5 [P4] function: message

Findings fail a --gate run unless the project's allowance admits them;
report-level entries (NoExit markers, R7 and R8's report-only cases) are
listed "for review" and never fail a run.

The allowance (the project file's [early_exit] allowance, a path in the
product checkout) holds one line per function with findings,
`<rule> <path>::<function> <count>`. It may only shrink: a count above the
findings it covers is stale and fails the run too, as a new finding does.
--write-allowance rewrites it from the current findings. A missing file is
an empty allowance.

  --build-dir TREE        the configured ninja tree whose units are checked
  --tool PATH             an early_exit_check already built (default: build it)
  --llvm-prefix DIR       the LLVM 22 prefix with Clang's CMake package
                          (default: `llvm-config --prefix` from PATH)
  --tool-build-dir DIR    where the tool is built
                          (default: <workspace>/.build/harness/early_exit_check)
  --self-test             run the tool over harness/testdata/early_exit and
                          compare with the fixtures' `// expect:` lines

Exit status: 0 when the run passed, 1 on a gated finding, a stale allowance
line or a self-test difference, 2 when the tool cannot be built or run.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any

import project

HARNESS: Path = Path(__file__).resolve().parent
TOOL_SOURCE: Path = HARNESS / 'early_exit'
FIXTURES: Path = HARNESS / 'testdata' / 'early_exit'
SOURCE_SUFFIXES: tuple[str, ...] = ('.c', '.cc', '.cpp', '.cxx', '.m', '.mm')

# POSIX.1-2024 XSH 2.9.5.2: the required cancellation points, and the
# may-occur entries glibc implements that STXlib's audit names. A project
# extends the list in its [early_exit] cancellation_points.
CANCELLATION_POINTS: tuple[str, ...] = (
    'accept', 'aio_suspend', 'clock_nanosleep', 'close', 'connect', 'creat', 'fcntl', 'fdatasync', 'fsync',
    'lockf', 'mq_receive', 'mq_send', 'mq_timedreceive', 'mq_timedsend', 'msgrcv', 'msgsnd', 'msync',
    'nanosleep', 'open', 'openat', 'pause', 'poll', 'ppoll', 'pread', 'pselect', 'pthread_cond_clockwait',
    'pthread_cond_timedwait', 'pthread_cond_wait', 'pthread_join', 'pthread_testcancel', 'pwrite', 'read',
    'readv', 'recv', 'recvfrom', 'recvmsg', 'select', 'sem_clockwait', 'sem_timedwait', 'sem_wait', 'send',
    'sendmsg', 'sendto', 'sigsuspend', 'sigtimedwait', 'sigwait', 'sigwaitinfo', 'sleep', 'tcdrain', 'wait',
    'waitid', 'waitpid', 'write', 'writev',
    # may occur (glibc): stdio, directories and names
    'fclose', 'fflush', 'fopen', 'fread', 'fwrite', 'fgets', 'fputs', 'printf', 'fprintf', 'vfprintf',
    'puts', 'getline', 'getdelim', 'opendir', 'closedir', 'readdir', 'rename', 'renameat', 'unlink',
    'unlinkat', 'remove', 'mkstemp', 'tmpfile', 'system', 'popen', 'pclose', 'syslog', 'getaddrinfo',
)

# C functions that neither are cancellation points nor take a callback, for
# hosts whose headers carry no __THROW (Apple, Microsoft); STXlib's table.
NO_EXIT_FUNCTIONS: tuple[str, ...] = (
    'memcpy', 'memmove', 'memset', 'memcmp', 'strlen', 'getenv', 'setenv', 'unsetenv', 'pipe', 'pipe2',
    'fcntl', 'posix_spawn', 'posix_spawnp', 'kill', 'sigaction', 'pthread_sigmask', 'atexit',
    'pthread_mutex_lock', 'pthread_mutex_unlock', 'pthread_setcancelstate', 'clock_gettime', 'getpid',
    '__cxa_demangle', 'std::to_chars',
)

ALLOCATOR_ALLOWLIST: tuple[str, ...] = (
    'malloc', 'calloc', 'realloc', 'free', 'aligned_alloc', 'posix_memalign', 'mmap', 'munmap', 'mi_.*',
    'VirtualAlloc', 'VirtualFree', 'HeapAlloc', 'HeapReAlloc', 'HeapFree',
)

RELEASE_FUNCTIONS: tuple[str, ...] = ('free', 'std::free', 'release', 'mi_free')

FOREIGN_FUNCTIONS: tuple[str, ...] = (
    'std::function::operator\\(\\)', 'std::move_only_function::operator\\(\\)', 'std::invoke',
)


def exact(names: tuple[str, ...] | list[str]) -> list[str]:
    """Whole-name patterns: each name anchored, as a regular expression."""
    patterns: list[str] = ['^' + name + '$' for name in names]
    return patterns


@dataclass(frozen=True)
class Settings:
    """The project file's [early_exit] table, with the defaults filled in."""
    first_party_dirs: tuple[str, ...]
    foreign_paths: tuple[str, ...]
    standard_library_foreign: bool
    foreign_functions: tuple[str, ...]
    no_exit_functions: tuple[str, ...]
    cancellation_points: tuple[str, ...]
    allocator_allowlist: tuple[str, ...]
    release_functions: tuple[str, ...]
    allowance: str


def string_list(table: dict[str, Any], key: str, default: list[str]) -> tuple[str, ...]:
    value: Any = table.get(key, default)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError('[early_exit] ' + key + ' must be a list of strings')
    result: tuple[str, ...] = tuple(value)
    return result


def read_settings(document: dict[str, Any]) -> Settings:
    """Validate a parsed project file's [early_exit] table whole and fill in the defaults.

    The name lists are regular expressions over qualified names; a project's
    `*_extra` list is appended to the default list of the same name."""
    table: Any = document.get('early_exit', {})
    if not isinstance(table, dict):
        raise ValueError('[early_exit] must be a table')
    machines: Any = document.get('state_machines', {})
    default_dirs: list[str] = list(machines.get('first_party_dirs', [])) if isinstance(machines, dict) else []
    standard: Any = table.get('standard_library', 'own')
    if standard not in ('own', 'foreign'):
        raise ValueError('[early_exit] standard_library must be "own" or "foreign"')
    allowance: Any = table.get('allowance', 'tools/early_exit_allowance.txt')
    if not isinstance(allowance, str):
        raise ValueError('[early_exit] allowance must be a path')

    def listed(key: str, default: list[str]) -> tuple[str, ...]:
        chosen: tuple[str, ...] = string_list(table, key, default) + string_list(table, key + '_extra', [])
        return chosen

    settings: Settings = Settings(
        first_party_dirs=string_list(table, 'first_party_dirs', default_dirs),
        foreign_paths=string_list(table, 'foreign_paths', []),
        standard_library_foreign=standard == 'foreign',
        foreign_functions=listed('foreign_functions', exact(FOREIGN_FUNCTIONS)),
        no_exit_functions=listed('no_exit_functions', exact(NO_EXIT_FUNCTIONS)),
        cancellation_points=listed('cancellation_points', exact(CANCELLATION_POINTS)),
        allocator_allowlist=listed('allocator_allowlist', exact(ALLOCATOR_ALLOWLIST)),
        release_functions=listed('release_functions', exact(RELEASE_FUNCTIONS)),
        allowance=allowance)
    if not settings.first_party_dirs:
        raise ValueError('[early_exit] first_party_dirs (or [state_machines] first_party_dirs) is required')
    return settings


def tool_settings(settings: Settings, root: Path) -> dict[str, Any]:
    """The tool's settings file: the directories as absolute paths with forward slashes.

    Paths are made absolute without resolving links, as the tool spells them
    (absolute, dots removed), so both sides compare the same text."""
    def absolute(relative: str) -> str:
        path: str = Path(os.path.abspath(root / relative)).as_posix()
        return path

    document: dict[str, Any] = {
        'first_party_roots': [absolute(directory) for directory in settings.first_party_dirs],
        'foreign_paths': [absolute(directory) for directory in settings.foreign_paths],
        'standard_library_foreign': settings.standard_library_foreign,
        'foreign_functions': list(settings.foreign_functions),
        'no_exit_functions': list(settings.no_exit_functions),
        'cancellation_points': list(settings.cancellation_points),
        'allocator_allowlist': list(settings.allocator_allowlist),
        'release_functions': list(settings.release_functions),
    }
    return document


# --- entries ------------------------------------------------------------------------

@dataclass(frozen=True, order=True)
class Entry:
    """One finding or report, its path relative to the product checkout."""
    path: str
    line: int
    column: int
    rule: str
    level: str
    pattern: str
    function: str
    message: str

    def key(self) -> tuple[str, str, str]:
        result: tuple[str, str, str] = (self.rule, self.path, self.function)
        return result

    def text(self) -> str:
        pattern: str = ' [' + self.pattern + ']' if self.pattern else ''
        result: str = (f'{self.path}:{self.line}:{self.column}: {self.rule}{pattern} {self.function}: '
                       f'{self.message}')
        return result


def parse_entries(output: str, root: Path) -> list[Entry]:
    """The tool's JSON lines as entries; files outside `root` are dropped."""
    entries: list[Entry] = []
    resolved_root: str = Path(os.path.abspath(root)).as_posix().rstrip('/') + '/'
    line: str
    for line in output.splitlines():
        if not line.strip():
            continue
        record: dict[str, Any] = json.loads(line)
        file: str = str(record['file'])
        if not file.startswith(resolved_root):
            continue
        entries.append(Entry(path=file[len(resolved_root):], line=int(record['line']),
                             column=int(record['column']), rule=str(record['rule']),
                             level=str(record['level']), pattern=str(record.get('pattern', '')),
                             function=str(record['function']), message=str(record['message'])))
    return entries


# --- the allowance --------------------------------------------------------------------

# A path holds no blank and no colon; the function runs to the count.
ALLOWANCE_LINE: re.Pattern[str] = re.compile(r'^(R\d+)\s+([^\s:]+)::(\S.*?)\s+(\d+)$')


def parse_allowance(text: str) -> dict[tuple[str, str, str], int]:
    """The allowance's counts by (rule, path, function); `#` starts a comment line."""
    counts: dict[tuple[str, str, str], int] = {}
    number: int
    raw: str
    for number, raw in enumerate(text.splitlines(), start=1):
        line: str = raw.strip()
        if not line or line.startswith('#'):
            continue
        match: re.Match[str] | None = ALLOWANCE_LINE.match(line)
        if match is None:
            raise ValueError(f'allowance line {number}: expected "<rule> <path>::<function> <count>"')
        counts[(match.group(1), match.group(2), match.group(3))] = int(match.group(4))
    return counts


def format_allowance(findings: list[Entry]) -> str:
    counts: Counter[tuple[str, str, str]] = Counter(entry.key() for entry in findings)
    lines: list[str] = ['# early-exit allowance: existing findings, one line per rule and function.',
                        '# Shrink-only: lower a count as findings are fixed; never raise one.']
    key: tuple[str, str, str]
    for key in sorted(counts):
        lines.append(f'{key[0]} {key[1]}::{key[2]} {counts[key]}')
    result: str = '\n'.join(lines) + '\n'
    return result


@dataclass(frozen=True)
class Judgement:
    """Findings beyond the allowance, and allowance lines above their findings."""
    new: list[Entry]
    stale: list[str]


def judge(findings: list[Entry], allowance: dict[tuple[str, str, str], int]) -> Judgement:
    """Compare findings with the allowance: within one key, the findings after the
    allowed count (in position order) are new; a count above the findings is stale."""
    by_key: dict[tuple[str, str, str], list[Entry]] = {}
    entry: Entry
    for entry in sorted(findings):
        by_key.setdefault(entry.key(), []).append(entry)
    new: list[Entry] = []
    key: tuple[str, str, str]
    group: list[Entry]
    for key, group in by_key.items():
        allowed: int = allowance.get(key, 0)
        new.extend(group[allowed:])
    stale: list[str] = []
    count: int
    for key, count in sorted(allowance.items()):
        found: int = len(by_key.get(key, []))
        if count > found:
            stale.append(f'{key[0]} {key[1]}::{key[2]} allows {count}, finds {found}: lower it')
    return Judgement(new=sorted(new), stale=stale)


# --- building and running the tool --------------------------------------------------------

def llvm_prefix(named: str | None) -> Path:
    if named:
        return Path(named)
    config: str | None = shutil.which('llvm-config')
    if config is None:
        raise RuntimeError('no llvm-config on PATH: name the LLVM 22 prefix with --llvm-prefix')
    prefix: str = subprocess.check_output([config, '--prefix'], text=True).strip()
    return Path(prefix)


def build_tool(prefix: Path, build_dir: Path) -> Path:
    """Configures (once) and builds early_exit_check; Ninja makes a rebuild a no-op."""
    executable: str = 'early_exit_check.exe' if os.name == 'nt' else 'early_exit_check'
    compiler: Path = prefix / 'bin' / ('clang++.exe' if os.name == 'nt' else 'clang++')
    if not (build_dir / 'build.ninja').is_file():
        subprocess.run(['cmake', '-S', str(TOOL_SOURCE), '-B', str(build_dir), '-G', 'Ninja',
                        '-DCMAKE_BUILD_TYPE=Release', f'-DCMAKE_PREFIX_PATH={prefix}',
                        f'-DCMAKE_CXX_COMPILER={compiler}'], check=True)
    subprocess.run(['cmake', '--build', str(build_dir)], check=True)
    tool: Path = build_dir / executable
    return tool


def compile_database(build_dir: Path) -> list[dict[str, Any]]:
    compdb: str = subprocess.check_output(['ninja', '-C', str(build_dir), '-t', 'compdb'], text=True,
                                          encoding='utf-8', errors='replace')
    entries: list[dict[str, Any]] = json.loads(compdb)
    return entries


def first_party_units(entries: list[dict[str, Any]], roots: list[str]) -> list[str]:
    """The absolute sources of the database's first-party units, once each."""
    units: list[str] = []
    seen: set[str] = set()
    entry: dict[str, Any]
    for entry in entries:
        source: str = Path(os.path.abspath(Path(entry['directory'], entry['file']))).as_posix()
        if Path(source).suffix not in SOURCE_SUFFIXES or source in seen:
            continue
        if any(source == root or source.startswith(root.rstrip('/') + '/') for root in roots):
            seen.add(source)
            units.append(source)
    return units


def run_tool(tool: Path, config: Path, database_dir: Path, units: list[str], jobs: int) -> str:
    """The tool's output over `units`, in `jobs` batches run side by side."""
    batches: list[list[str]] = [units[index::jobs] for index in range(jobs) if units[index::jobs]]

    def one(batch: list[str]) -> str:
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [str(tool), '--config', str(config), '-p', str(database_dir), *batch], capture_output=True, text=True,
            encoding='utf-8', errors='replace')
        if completed.returncode != 0:
            sys.stderr.write(completed.stderr)
            raise RuntimeError(f'early_exit_check exited {completed.returncode}')
        return completed.stdout

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        outputs: list[str] = list(pool.map(one, batches))
    result: str = ''.join(outputs)
    return result


# --- the self-test --------------------------------------------------------------------------

FLAGS_LINE: re.Pattern[str] = re.compile(r'^//\s*flags:\s*(.*)$')
EXPECT: re.Pattern[str] = re.compile(r'//\s*expect:\s*([R0-9, ]+)')
EXPECT_REPORT: re.Pattern[str] = re.compile(r'//\s*expect-report:\s*([R0-9, ]+)')


def expectations(text: str) -> tuple[list[str], set[tuple[int, str, str]]]:
    """A fixture's compiler flags and its expected (line, rule, level) entries."""
    lines: list[str] = text.splitlines()
    flags: list[str] = []
    if lines:
        match: re.Match[str] | None = FLAGS_LINE.match(lines[0])
        if match is not None:
            flags = match.group(1).split()
    expected: set[tuple[int, str, str]] = set()
    number: int
    line: str
    for number, line in enumerate(lines, start=1):
        level: str
        pattern: re.Pattern[str]
        for level, pattern in (('finding', EXPECT), ('report', EXPECT_REPORT)):
            found: re.Match[str] | None = pattern.search(line)
            if found is None:
                continue
            rule: str
            for rule in found.group(1).replace(',', ' ').split():
                expected.add((number, rule, level))
    return flags, expected


def self_test(tool: Path) -> int:
    failures: int = 0
    with tempfile.TemporaryDirectory(prefix='early-exit-self-test-') as scratch:
        config: Path = Path(scratch) / 'settings.json'
        fixture_settings: Settings = read_settings({'early_exit': {'first_party_dirs': ['.']}})
        config.write_text(json.dumps(tool_settings(fixture_settings, FIXTURES)), encoding='utf-8')
        fixture: Path
        for fixture in sorted(FIXTURES.glob('*.cpp')):
            flags: list[str]
            expected: set[tuple[int, str, str]]
            flags, expected = expectations(fixture.read_text(encoding='utf-8'))
            completed: subprocess.CompletedProcess[str] = subprocess.run(
                [str(tool), '--config', str(config), str(fixture), '--', '-std=c++20', f'-I{FIXTURES}', *flags],
                capture_output=True, text=True, encoding='utf-8', errors='replace')
            if completed.returncode != 0:
                print(f'{fixture.name}: the tool failed\n{completed.stderr}')
                failures += 1
                continue
            entries: list[Entry] = parse_entries(completed.stdout, FIXTURES)
            actual: set[tuple[int, str, str]] = {(entry.line, entry.rule, entry.level)
                                                 for entry in entries if entry.path == fixture.name}
            missing: set[tuple[int, str, str]] = expected - actual
            unexpected: set[tuple[int, str, str]] = actual - expected
            item: tuple[int, str, str]
            for item in sorted(missing):
                print(f'{fixture.name}:{item[0]}: expected {item[1]} ({item[2]}), not reported')
            for item in sorted(unexpected):
                text: str = next(entry.text() for entry in entries
                                 if (entry.line, entry.rule, entry.level) == item and entry.path == fixture.name)
                print(f'unexpected ({item[2]}): {text}')
            if missing or unexpected:
                failures += 1
            else:
                print(f'{fixture.name}: ok')
    print(f'early-exit self-test: {failures} fixture(s) differ')
    result: int = 1 if failures else 0
    return result


# --- main ---------------------------------------------------------------------------------

def load_project_settings() -> tuple[Settings, Path]:
    current: project.Project = project.current()
    with current.path.open('rb') as stream:
        document: dict[str, Any] = tomllib.load(stream)
    settings: Settings = read_settings(document)
    return settings, current.source_root()


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__,
                                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--build-dir', help='a configured ninja tree')
    parser.add_argument('--tool', help='an early_exit_check already built')
    parser.add_argument('--llvm-prefix', help='the LLVM 22 prefix holding Clang\'s CMake package')
    parser.add_argument('--tool-build-dir', help='where the tool is built')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--gate', action='store_true', help='exit 1 on a finding outside the allowance')
    parser.add_argument('--write-allowance', action='store_true', help='rewrite the allowance from the findings')
    parser.add_argument('--self-test', action='store_true', help='check the tool against its fixtures')
    parser.add_argument('--json', dest='json_path', help='also write every entry as JSON here')
    arguments: argparse.Namespace = parser.parse_args(argv)

    try:
        tool: Path
        if arguments.tool:
            tool = Path(arguments.tool)
        else:
            build_dir: Path = (Path(arguments.tool_build_dir) if arguments.tool_build_dir
                               else project.workspace_root() / '.build' / 'harness' / 'early_exit_check')
            tool = build_tool(llvm_prefix(arguments.llvm_prefix), build_dir)
    except (RuntimeError, subprocess.CalledProcessError, OSError) as problem:
        print(f'check_early_exit: cannot build the tool: {problem}', file=sys.stderr)
        return 2
    if arguments.self_test:
        return self_test(tool)
    if not arguments.build_dir:
        parser.error('--build-dir is required (or --self-test)')

    settings: Settings
    root: Path
    settings, root = load_project_settings()
    document: dict[str, Any] = tool_settings(settings, root)
    try:
        entries_db: list[dict[str, Any]] = compile_database(Path(arguments.build_dir))
        units: list[str] = first_party_units(entries_db, document['first_party_roots'])
        with tempfile.TemporaryDirectory(prefix='early-exit-') as scratch:
            config: Path = Path(scratch) / 'settings.json'
            config.write_text(json.dumps(document), encoding='utf-8')
            (Path(scratch) / 'compile_commands.json').write_text(json.dumps(entries_db), encoding='utf-8')
            output: str = run_tool(tool, config, Path(scratch), units, max(1, arguments.jobs)) if units else ''
    except (RuntimeError, subprocess.CalledProcessError, OSError) as problem:
        print(f'check_early_exit: {problem}', file=sys.stderr)
        return 2

    entries: list[Entry] = sorted(set(parse_entries(output, root)))
    findings: list[Entry] = [entry for entry in entries if entry.level == 'finding']
    reports: list[Entry] = [entry for entry in entries if entry.level == 'report']
    if arguments.json_path:
        Path(arguments.json_path).write_text(json.dumps([entry.__dict__ for entry in entries], indent=1),
                                             encoding='utf-8')
    allowance_path: Path = root / settings.allowance
    if arguments.write_allowance:
        allowance_path.parent.mkdir(parents=True, exist_ok=True)
        allowance_path.write_text(format_allowance(findings), encoding='utf-8')
        print(f'check_early_exit: wrote {allowance_path} ({len(findings)} finding(s))')
        return 0
    allowance_text: str = allowance_path.read_text(encoding='utf-8') if allowance_path.is_file() else ''
    judgement: Judgement = judge(findings, parse_allowance(allowance_text))
    entry: Entry
    for entry in judgement.new:
        print(entry.text())
    line: str
    for line in judgement.stale:
        print(f'{settings.allowance}: stale: {line}')
    if reports:
        print('for review:')
        for entry in reports:
            print('  ' + entry.text())
    print(f'check_early_exit: {len(units)} unit(s); {len(findings)} finding(s), {len(judgement.new)} outside the '
          f'allowance, {len(judgement.stale)} stale allowance line(s), {len(reports)} for review')
    failed: bool = bool(judgement.stale) or (arguments.gate and bool(judgement.new))
    result: int = 1 if failed else 0
    return result


if __name__ == '__main__':
    raise SystemExit(main())
