#!/usr/bin/env python3
"""Choose the tests a branch's changes can affect (owner plan of 2026-10-22).

Every ctest test carries one area label, area_<area> (the product's CMake
labels them). The project's test-area rules (tests.areas_file, GUI.Forms'
tools/test_areas.json) map source paths to the areas whose tests read them;
its "about" entry states the matching rules. This module turns a list of
changed paths into a selection: every test, or the tests of some areas plus
tests chosen by name or by the source paths their commands name. A test with
no area label is always selected.

As a program it prints the selection for a configured build tree:

    python3 harness/change_selection.py --tree .build/native-windows-x64/gui-forms --base origin/main

prints a ctest -R pattern (or ALL, or NONE); --format areas prints the areas
and the reason for each changed path instead, and needs no tree.
"""
from __future__ import annotations
import argparse
import fnmatch
import json
import os
from pathlib import Path
import subprocess
from typing import NamedTuple

import project

SOURCE_ROOT: Path = project.current().source_root()
# None when the project names no test-area rules: load_rules then fails.
AREAS_FILE: Path | None = (None if project.current().test_areas_file is None
                           else SOURCE_ROOT / project.current().test_areas_file)
EVERY_AREA: str = '*'
AREA_LABEL_PREFIX: str = 'area_'
# Characters a test name would need escaped in a ctest -R regular expression.
REGEX_SPECIAL: str = '.^$|()[]{}*+?\\'


class PathRule(NamedTuple):
    """One path_rules entry: a directory (ending in /), a glob (with *) or a file or file-name prefix."""
    entry: str
    areas: tuple[str, ...]


class Selection(NamedTuple):
    """The tests to run for a set of changed paths.

    every_test: run the whole suite (a path in the touches-everything set, or
    a path no rule names). Otherwise areas are the area names whose tests
    run, and tests the further test names chosen by name or by command path.
    reasons maps each changed path to a short account of what it selected.
    """
    every_test: bool
    areas: frozenset[str]
    tests: frozenset[str]
    reasons: dict[str, str]


def load_path_rules(document: dict) -> list[PathRule]:
    rules: list[PathRule] = []
    group: dict
    for group in document['path_rules']:
        areas: tuple[str, ...] = tuple(group['areas'])
        entry: str
        for entry in group['paths']:
            rules.append(PathRule(entry, areas))
    return rules


def rule_matches(entry: str, path: str) -> bool:
    """Whether a path_rules entry names path (both use forward slashes, relative to the source root)."""
    if '*' in entry:
        # A glob's * stays within one segment: the path has the entry's depth.
        if entry.count('/') != path.count('/'):
            return False
        return fnmatch.fnmatchcase(path, entry)
    if entry.endswith('/'):
        return path.startswith(entry)
    if entry.startswith('tests/'):
        # Under tests/ an entry may be a file-name prefix such as tests/drawing_.
        return path.startswith(entry)
    return path == entry


def areas_for_path(path: str, rules: list[PathRule]) -> tuple[str, ...] | None:
    """The areas of the longest entry naming path, or None when no entry names it."""
    best: PathRule | None = None
    rule: PathRule
    for rule in rules:
        if not rule_matches(rule.entry, path):
            continue
        if best is None or len(rule.entry) > len(best.entry):
            best = rule
    if best is None:
        return None
    return best.areas


def any_path_read(changed: list[str], rules: list[PathRule]) -> bool:
    """Whether some changed path can select a test before any tree is listed.

    False only when every path has a rule with no areas (documentation,
    planning, results): those paths are read by no test, so a caller can
    skip configuring a tree. A file under tests/ may name a test and counts
    as read.
    """
    path: str
    for path in changed:
        if path.startswith('tests/'):
            return True
        path_areas: tuple[str, ...] | None = areas_for_path(path, rules)
        if path_areas is None or path_areas:
            return True
    return False


def tests_named_by_file(path: str, test_names: list[str]) -> list[str]:
    """The tests a file under tests/ names by its stem: X, or X after a project test-name prefix
    (tests.name_prefixes: gui_forms_X or gui_drawing_X for GUI.Forms)."""
    if not path.startswith('tests/'):
        return []
    stem: str = Path(path).stem
    candidates: list[str] = [stem]
    prefix: str
    for prefix in project.current().test_name_prefixes:
        candidates.append(prefix + stem)
    named: list[str] = []
    name: str
    for name in test_names:
        if name in candidates:
            named.append(name)
    return named


def test_area(test: dict) -> str | None:
    """The area label of one test of a ctest --show-only=json-v1 listing, without its prefix."""
    property_record: dict
    for property_record in test.get('properties', []):
        if property_record.get('name') != 'LABELS':
            continue
        label: str
        for label in property_record.get('value', []):
            if label.startswith(AREA_LABEL_PREFIX):
                return label[len(AREA_LABEL_PREFIX):]
    return None


def source_paths_of_test(test: dict, source_root: Path) -> list[str]:
    """The source-root-relative paths a test's command and environment name.

    A -DNAME=value or NAME=value argument is read by its value. Paths in the
    build tree are not source paths and are left out.
    """
    values: list[str] = []
    argument: str
    for argument in test.get('command', []):
        values.append(argument)
    property_record: dict
    for property_record in test.get('properties', []):
        if property_record.get('name') != 'ENVIRONMENT':
            continue
        setting: str
        for setting in property_record.get('value', []):
            values.append(setting)
    root: str = os.path.normcase(os.path.normpath(str(source_root)))
    paths: list[str] = []
    value: str
    for value in values:
        if '=' in value:
            value = value.partition('=')[2]
        if not value:
            continue
        path: str = os.path.normcase(os.path.normpath(value))
        if not path.startswith(root + os.sep):
            continue
        relative: str = os.path.relpath(os.path.normpath(value), os.path.normpath(str(source_root)))
        relative = relative.replace(os.sep, '/')
        if relative.startswith('.build/') or '/.build/' in relative:
            continue
        paths.append(relative)
    return paths


def path_lies_under(path: str, named: str) -> bool:
    return path == named or path.startswith(named.rstrip('/') + '/')


def select(changed: list[str], rules: list[PathRule], listing: dict, source_root: Path) -> Selection:
    """The selection for the changed paths over the tests of listing (ctest --show-only=json-v1).

    listing may be empty ({}) when only the areas are wanted; tests are then
    chosen by path rules alone.
    """
    tests: list[dict] = listing.get('tests', [])
    test_names: list[str] = []
    test: dict
    for test in tests:
        test_names.append(test['name'])
    every_test: bool = False
    areas: set[str] = set()
    chosen: set[str] = set()
    reasons: dict[str, str] = {}
    path: str
    for path in changed:
        accounts: list[str] = []
        named: list[str] = tests_named_by_file(path, test_names)
        if named:
            chosen.update(named)
            accounts.append('names ' + ', '.join(named))
        else:
            path_areas: tuple[str, ...] | None = areas_for_path(path, rules)
            if path_areas is None:
                every_test = True
                accounts.append('no rule names it: every test')
            elif EVERY_AREA in path_areas:
                every_test = True
                accounts.append('touches everything')
            elif path_areas:
                areas.update(path_areas)
                accounts.append('areas ' + ', '.join(path_areas))
            else:
                accounts.append('read by no test')
        for test in tests:
            source_path: str
            for source_path in source_paths_of_test(test, source_root):
                if path_lies_under(path, source_path):
                    chosen.add(test['name'])
                    accounts.append('read by ' + test['name'])
                    break
        reasons[path] = '; '.join(accounts)
    return Selection(every_test, frozenset(areas), frozenset(chosen), reasons)


def selected_tests(listing: dict, selection: Selection) -> list[dict]:
    """The tests of listing that selection runs, in listing order; unlabelled tests always run."""
    tests: list[dict] = []
    test: dict
    for test in listing.get('tests', []):
        area: str | None = test_area(test)
        if selection.every_test or area is None or area in selection.areas or test['name'] in selection.tests:
            tests.append(test)
    return tests


def name_pattern(names: list[str]) -> str:
    """A ctest -R pattern matching exactly the given test names."""
    escaped_names: list[str] = []
    name: str
    for name in names:
        escaped: str = ''
        character: str
        for character in name:
            if character in REGEX_SPECIAL:
                escaped += '\\'
            escaped += character
        escaped_names.append(escaped)
    return '^(' + '|'.join(escaped_names) + ')$'


def git_lines(source_root: Path, *arguments: str) -> list[str] | None:
    """The output lines of a git command run in source_root, or None when it fails."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(source_root), *arguments], capture_output=True, text=True, encoding='utf-8')
    if completed.returncode != 0:
        return None
    lines: list[str] = []
    line: str
    for line in completed.stdout.splitlines():
        if line.strip():
            lines.append(line.strip())
    return lines


def changed_paths(source_root: Path, base: str) -> list[str]:
    """The paths changed since base: committed since the merge base, staged, unstaged and untracked.

    Without the history for a merge base (a shallow CI checkout) the diff is
    taken from base itself, which can only add paths. Renames list both
    names. Paths are relative to source_root. Raises RuntimeError when git
    cannot compare with base at all.
    """
    start: str = base
    merge_base: list[str] | None = git_lines(source_root, 'merge-base', base, 'HEAD')
    if merge_base:
        start = merge_base[0]
    committed: list[str] | None = git_lines(source_root, 'diff', '--name-only', '--no-renames', '--relative',
                                             start, 'HEAD')
    if committed is None:
        raise RuntimeError('git cannot compare ' + base + ' with HEAD in ' + str(source_root))
    # A checkout written with core.autocrlf by one git and read by another
    # without it (MSYS2's git on Windows) differs from HEAD only in line
    # endings in every file; such files are not changed.
    working: list[str] = git_lines(source_root, 'diff', '--name-only', '--no-renames', '--relative',
                                   '--ignore-cr-at-eol', 'HEAD') or []
    untracked: list[str] = git_lines(source_root, 'ls-files', '--others', '--exclude-standard') or []
    paths: list[str] = []
    path: str
    for path in committed + working + untracked:
        if path not in paths:
            paths.append(path)
    return paths


def ctest_listing(tree: Path) -> dict:
    """Every test of tree, with the commands of tests not built yet (ci_build.test_listing)."""
    # Imported here: ci_build imports this module.
    import ci_build
    return ci_build.test_listing(tree, '', '')


def load_rules() -> list[PathRule]:
    if AREAS_FILE is None:
        raise project.current().missing('tests.areas_file')
    return load_path_rules(json.loads(AREAS_FILE.read_text(encoding='utf-8')))


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__,
                                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base', required=True, help='the branch point: a ref or commit to compare HEAD with')
    parser.add_argument('--tree', default='', help='a configured build tree, to list its tests')
    parser.add_argument('--format', choices=('regex', 'areas'), default='regex',
                        help='regex (the default): a ctest -R pattern, ALL or NONE; areas: the areas and reasons')
    args: argparse.Namespace = parser.parse_args()
    if args.format == 'regex' and not args.tree:
        parser.error('--format regex needs --tree')
    listing: dict = {}
    if args.tree:
        listing = ctest_listing(Path(args.tree))
    changed: list[str] = changed_paths(SOURCE_ROOT, args.base)
    selection: Selection = select(changed, load_rules(), listing, SOURCE_ROOT)
    if args.format == 'areas':
        print('every test' if selection.every_test else 'areas: ' + ', '.join(sorted(selection.areas)))
        if selection.tests:
            print('tests: ' + ', '.join(sorted(selection.tests)))
        path: str
        for path in changed:
            print(path + ': ' + selection.reasons[path])
        return
    if selection.every_test:
        print('ALL')
        return
    tests: list[dict] = selected_tests(listing, selection)
    if not tests:
        print('NONE')
        return
    names: list[str] = []
    test: dict
    for test in tests:
        names.append(test['name'])
    print(name_pattern(names))


if __name__ == '__main__':
    main()
