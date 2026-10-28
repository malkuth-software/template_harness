#!/usr/bin/env python3
"""The product-independent steps of a CI build: run, configure, select tests, sanitize, record.

A product's own build driver (GUI.Forms: tools/ci_build.py, whose
build_toolkit and main stay in GUI.Forms with its fetch scripts, SDK
checks and gallery option) calls these. They came from that driver; the
only product names they used are now read from the project file:

- the product checkout, whose changed paths select the tests;
- <env_prefix>_SANITIZER_DEBUG_INFO, the sanitizer debug-information mode;
- <env_prefix>_LLVM_RUNTIME and the project's build.configure_variables,
  which join the configure record's environment.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import change_selection
import project


def run(*arguments: str | Path | int) -> None:
    command: list[str] = []
    argument: str | Path | int
    for argument in arguments:
        command.append(str(argument))
    print(' '.join(command), flush=True)
    subprocess.run(command, check=True)


def captured(*arguments: str | Path) -> str:
    """Run a query command and return its standard output (it must succeed)."""
    command: list[str] = []
    argument: str | Path
    for argument in arguments:
        command.append(str(argument))
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        command, check=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return completed.stdout


def tree_relative(argument: str, tree: Path) -> str | None:
    """The build-tree-relative path an argument names, or None if it names none.

    A -DNAME=value argument is read by its value. The result uses forward
    slashes, as ninja spells its outputs.
    """
    value: str = argument
    if argument.startswith('-') and '=' in argument:
        value = argument.partition('=')[2]
    if not value:
        return None
    root: str = os.path.normcase(os.path.normpath(str(tree)))
    path: str = os.path.normcase(os.path.normpath(value))
    if not path.startswith(root + os.sep):
        return None
    return os.path.relpath(os.path.normpath(value), os.path.normpath(str(tree))).replace(os.sep, '/')


def targets_for_tests(listing: dict, known_outputs: set[str], tree: Path) -> list[str] | None:
    """The ninja outputs the listed tests read, or None to build everything.

    listing is `ctest --show-only=json-v1` output for the selected tests and
    known_outputs every output ninja can build in tree. A test reads the
    build-tree files its command names (its executable, or the libraries and
    programs an audit script inspects). The whole tree is built instead when
    a test names a build-tree file ninja does not produce, or depends on other
    tests (fixtures, DEPENDS), whose products this reading cannot see, or
    has no command at all (see test_commands). A test naming no build-tree
    file needs nothing built.
    """
    targets: set[str] = set()
    test: dict
    for test in listing.get('tests', []):
        if 'command' not in test:
            return None
        property_record: dict
        for property_record in test.get('properties', []):
            if property_record.get('name') in ('FIXTURES_REQUIRED', 'DEPENDS', 'REQUIRED_FILES'):
                return None
        argument: str
        for argument in test.get('command', []):
            relative: str | None = tree_relative(argument, tree)
            if relative is None:
                continue
            if relative not in known_outputs:
                return None
            targets.add(relative)
    return sorted(targets)


def ctest_selection(tests: str, skip_labels: str) -> list[str]:
    """ctest arguments choosing tests: -R tests (none matching fails), -LE skip_labels."""
    selection: list[str] = []
    if tests:
        # A pattern that matches no test is a mistake, not a pass.
        selection.extend(['-R', tests, '--no-tests=error'])
    if skip_labels:
        selection.extend(['-LE', skip_labels])
    return selection


# One argument of a CMake command: a quoted argument (a backslash escapes the
# next character), a bracket argument [=[...]=], or an unquoted run.
CMAKE_ARGUMENT: re.Pattern[str] = re.compile(r'"((?:[^"\\]|\\.)*)"|\[(=*)\[(.*?)\]\2\]|([^\s()"]+)', re.DOTALL)
CMAKE_ESCAPE: re.Pattern[str] = re.compile(r'\\(.)', re.DOTALL)
CMAKE_CALL: re.Pattern[str] = re.compile(r'^(add_test|subdirs)\((.*)\)\s*$', re.MULTILINE)


def cmake_arguments(text: str) -> list[str]:
    """The arguments of one CMake command's parenthesized text, unquoted."""
    arguments: list[str] = []
    match: re.Match[str]
    for match in CMAKE_ARGUMENT.finditer(text):
        if match.group(1) is not None:
            arguments.append(CMAKE_ESCAPE.sub(r'\1', match.group(1)))
        elif match.group(3) is not None:
            arguments.append(match.group(3))
        else:
            arguments.append(match.group(4))
    return arguments


def test_commands(directory: Path) -> dict[str, list[str]]:
    """Every test's command as directory's CTestTestfile.cmake (and its subdirs) records it.

    ctest --show-only=json-v1 leaves out the command of a test whose
    executable is not built yet, which is every test of a fresh tree; the
    generated file still names it.
    """
    commands: dict[str, list[str]] = {}
    test_file: Path = directory / 'CTestTestfile.cmake'
    if not test_file.is_file():
        return commands
    call: re.Match[str]
    for call in CMAKE_CALL.finditer(test_file.read_text(encoding='utf-8', errors='replace')):
        arguments: list[str] = cmake_arguments(call.group(2))
        if call.group(1) == 'subdirs':
            subdirectory: str
            for subdirectory in arguments:
                commands.update(test_commands(directory / subdirectory))
        elif arguments:
            commands[arguments[0]] = arguments[1:]
    return commands


def test_listing(toolkit: Path, tests: str, skip_labels: str) -> dict:
    """ctest's --show-only=json-v1 listing of the tests ctest_selection(tests, skip_labels) chooses.

    A test whose executable is not built yet gets its command from the
    generated test files (test_commands), so that the listing names it.
    """
    listing: dict = json.loads(captured('ctest', '--test-dir', toolkit, '--show-only=json-v1',
                                        *ctest_selection(tests, skip_labels)))
    commands: dict[str, list[str]] | None = None
    test: dict
    for test in listing.get('tests', []):
        if 'command' in test:
            continue
        if commands is None:
            commands = test_commands(toolkit)
        if test['name'] in commands:
            test['command'] = commands[test['name']]
    return listing


def selected_test_targets(toolkit: Path, listing: dict) -> list[str] | None:
    """Ask ninja which outputs the tests of listing need (see targets_for_tests)."""
    known_outputs: set[str] = set()
    line: str
    for line in captured('ninja', '-C', toolkit, '-t', 'targets', 'all').splitlines():
        output: str = line.rpartition(': ')[0]
        if output:
            known_outputs.add(output)
    return targets_for_tests(listing, known_outputs, toolkit)


# The sanitized build type carries -g, and ccache then adds the build
# directory to every object's hash (debug information records it), so no two
# checkouts could share a sanitized object. -fdebug-compilation-dir=. records
# "." instead; ccache recognizes the option and leaves the directory out, so
# objects of identical sources hit across checkouts with the same layout.
# Reports still name file and line, relative to the build directory.
SANITIZER_FLAGS: str = ('-fsanitize=address,undefined -fno-sanitize-recover=undefined -fno-omit-frame-pointer '
                        '-fdebug-compilation-dir=.')


SANITIZER_LINK_FLAGS: str = '-fsanitize=address,undefined'


# Debug information of the sanitized build type (owner, 2026-10-21). Line
# tables are the default: every report frame keeps file, line and column,
# objects are about 58% smaller and programs about 70% smaller than with -g,
# and compiles about 28% faster (measured on the tool_strip test program).
# Frames inlined into our code then lose their namespace and parameter types,
# and a debugger loses locals; `full` restores -g while hunting a defect.
DEBUG_INFO_FLAGS: dict[str, str] = {'lines': '-gline-tables-only', 'full': '-g'}
DEBUG_INFO_VARIABLE: str = project.current().variable('SANITIZER_DEBUG_INFO')


def debug_info_mode(flag: str, environ: dict[str, str]) -> str:
    """The sanitizer debug-information mode: the flag, else the environment, else `lines`."""
    mode: str = flag or environ.get(DEBUG_INFO_VARIABLE, '').strip().lower() or 'lines'
    if mode not in DEBUG_INFO_FLAGS:
        raise ValueError(DEBUG_INFO_VARIABLE + ' must be lines or full, not ' + repr(mode))
    return mode


def sanitizer_options(host: str, lsan_suppressions: Path,
                      debug_info: str = 'lines') -> tuple[list[str], dict[str, str]]:
    """CMake options and the configure and test environment for the sanitized suite on host.

    Address and undefined-behaviour sanitizers everywhere, and the leak
    sanitizer where it runs inside AddressSanitizer: Linux and macOS (the
    MinGW runtime on Windows has none). Every report stops its test, so any
    finding fails the run. Third-party sources built by CMake are
    instrumented too (GUI.Forms: HarfBuzz and FreeType).

    On macOS harness/cmake/llvm22.cmake puts the pinned libc++ headers and runtime
    link flags in CMAKE_<LANG>_FLAGS_INIT, which a -DCMAKE_<LANG>_FLAGS on
    the command line replaces (the tree then compiles against Homebrew's
    libc++ headers). There the flags go through CFLAGS, CXXFLAGS,
    OBJCXXFLAGS and LDFLAGS instead, which CMake joins with the _INIT values
    when it first configures the tree; OBJCXXFLAGS also instruments the
    Objective-C++ host. Being read at the first configure only, they reach a
    macOS tree configured earlier only after that tree is deleted.

    debug_info (`lines` or `full`) sets the build type's own flags,
    CMAKE_<LANG>_FLAGS_RELWITHDEBINFO, on every host and every configure, so
    changing the mode rebuilds an existing tree with the other flags.
    """
    detect_leaks: str = '0'
    if host in ('linux', 'macos'):
        detect_leaks = '1'
    build_type_flags: str = '-O2 ' + DEBUG_INFO_FLAGS[debug_info] + ' -DNDEBUG'
    options: list[str] = ['-DCMAKE_BUILD_TYPE=RelWithDebInfo',
                          '-DCMAKE_C_FLAGS_RELWITHDEBINFO=' + build_type_flags,
                          '-DCMAKE_CXX_FLAGS_RELWITHDEBINFO=' + build_type_flags]
    if host == 'macos':
        options.append('-DCMAKE_OBJCXX_FLAGS_RELWITHDEBINFO=' + build_type_flags)
    environment: dict[str, str] = {
        'ASAN_OPTIONS': 'detect_leaks=' + detect_leaks + ':halt_on_error=1:abort_on_error=0:print_summary=1',
        'UBSAN_OPTIONS': 'print_stacktrace=1:halt_on_error=1:print_summary=1',
    }
    if host == 'macos':
        environment['CFLAGS'] = SANITIZER_FLAGS
        environment['CXXFLAGS'] = SANITIZER_FLAGS
        environment['OBJCXXFLAGS'] = SANITIZER_FLAGS
        environment['LDFLAGS'] = SANITIZER_LINK_FLAGS
    else:
        options.extend([
            '-DCMAKE_C_FLAGS=' + SANITIZER_FLAGS,
            '-DCMAKE_CXX_FLAGS=' + SANITIZER_FLAGS,
            '-DCMAKE_EXE_LINKER_FLAGS=' + SANITIZER_LINK_FLAGS,
            '-DCMAKE_SHARED_LINKER_FLAGS=' + SANITIZER_LINK_FLAGS,
        ])
    if detect_leaks == '1':
        environment['LSAN_OPTIONS'] = 'suppressions=' + str(lsan_suppressions) + ':print_suppressions=0'
    return options, environment


# A fetch script (GUI.Forms: third_party/fetch_text_stack.sh) pins each
# checkout beside it as <directory>_revision=<commit>.
PIN_PATTERN: re.Pattern[str] = re.compile(r'^(\w+)_revision=([0-9a-f]{40})$', re.MULTILINE)


def pinned_checkouts_current(script: Path) -> bool:
    """Whether every checkout the fetch script pins is already at its pinned commit.

    The script would then only re-run `git checkout --detach` of the commit
    HEAD already names, which changes nothing (it keeps local edits either
    way) but rescans thousands of files. A script without readable pins, a
    missing checkout or a failing git counts as not current.
    """
    pins: list[tuple[str, str]] = PIN_PATTERN.findall(script.read_text(encoding='utf-8'))
    if not pins:
        return False
    pin: tuple[str, str]
    for pin in pins:
        checkout: Path = script.parent / pin[0]
        if not (checkout / '.git').exists():
            return False
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            ['git', '-C', str(checkout), 'rev-parse', 'HEAD'], capture_output=True, text=True)
        if completed.returncode != 0 or completed.stdout.strip() != pin[1]:
            return False
    return True


# Environment that changes what a first configure records (compilers,
# launchers, the macOS sanitizer and runtime flags, and the product's own
# configure variables: GUI_FORMS_SYSTEM_BUILD_TOOLS for GUI.Forms). The
# record sorts its keys, so the order here never changes a record.
CONFIGURE_ENVIRONMENT: tuple[str, ...] = (
    'CC', 'CXX', 'OBJCXX', 'CFLAGS', 'CXXFLAGS', 'OBJCXXFLAGS', 'LDFLAGS',
    'CMAKE_C_COMPILER_LAUNCHER', 'CMAKE_CXX_COMPILER_LAUNCHER', 'CMAKE_OBJCXX_COMPILER_LAUNCHER',
    project.current().variable('LLVM_RUNTIME')) + project.current().configure_variables()


CONFIGURE_RECORD_NAME: str = 'ci_build-configure.json'


def configure_record(configure: list[str], environ: dict[str, str], cache_text: bytes) -> str:
    """What one configure of a tree depended on: its command, environment and resulting cache."""
    environment: dict[str, str] = {}
    name: str
    for name in CONFIGURE_ENVIRONMENT:
        environment[name] = environ.get(name, '')
    record: dict = {'command': configure, 'environment': environment,
                    'cmake_cache_sha256': hashlib.sha256(cache_text).hexdigest()}
    return json.dumps(record, indent=1, sort_keys=True)


def configure_tree(toolkit: Path, configure: list[str]) -> None:
    """Configure toolkit, unless the same configure already produced its current cache.

    After a configure the record of its command, environment and
    CMakeCache.txt digest is kept in the tree. When all three still match,
    configuring again would change nothing, so only ninja's own regeneration
    step runs, which re-runs CMake if a CMake input changed since. Any other
    change to the cache (a manual cmake -D, a different profile) or a missing
    record configures again.
    """
    cache: Path = toolkit / 'CMakeCache.txt'
    record_path: Path = toolkit / CONFIGURE_RECORD_NAME
    if (toolkit / 'build.ninja').is_file() and cache.is_file() and record_path.is_file():
        expected: str = configure_record(configure, dict(os.environ), cache.read_bytes())
        if record_path.read_text(encoding='utf-8') == expected:
            print(f'{toolkit.name}: configured by this command already; regenerating only if CMake inputs changed',
                  flush=True)
            run('cmake', '--build', toolkit, '--target', 'build.ninja')
            return
    record_path.unlink(missing_ok=True)
    run(*configure)
    record_path.write_text(configure_record(configure, dict(os.environ), cache.read_bytes()), encoding='utf-8')


def changed_tests_listing(changed: list[str], listing: dict) -> dict | None:
    """The part of listing the changed paths select (change_selection.py), or None for every test."""
    selection: change_selection.Selection = change_selection.select(
        changed, change_selection.load_rules(), listing, project.current().source_root())
    path: str
    for path in changed:
        print(f'changed {path}: {selection.reasons[path]}', flush=True)
    if selection.every_test:
        return None
    return {'tests': change_selection.selected_tests(listing, selection)}


# What the product's last build call built into a tree, beside the tree as
# <tree>.record.json: {"built": BUILT_NOTHING | BUILT_SELECTED | BUILT_EVERYTHING,
# "reason": text}. Removed when a call starts, so a call that fails leaves
# none. The product's driver writes it; GUI.Forms' tools/stack_frames.py reads
# it: a run that built nothing (a change selection no test reads) has no
# objects to check.
BUILT_NOTHING: str = 'nothing'
BUILT_SELECTED: str = 'selected'
BUILT_EVERYTHING: str = 'everything'


def build_record_path(toolkit: Path) -> Path:
    return toolkit.with_name(toolkit.name + '.record.json')


def write_build_record(toolkit: Path, built: str, reason: str) -> None:
    record: dict[str, str] = {'built': built, 'reason': reason}
    path: Path = build_record_path(toolkit)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record) + '\n', encoding='utf-8')
