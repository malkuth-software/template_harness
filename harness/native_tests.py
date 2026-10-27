#!/usr/bin/env python3
"""Name the tests a native job validated, and the tests a programs job still has to run.

The native job's ci_build.py run writes CTest's JUnit report (JUNIT_NAME in
its toolkit build directory). cache_manifest.py records the names of the
tests that report shows as passed in the cache archive's cache-manifest.json
(validated_tests), and a reused validation carries the producer's list over.

On a pull request the programs job builds the same tree with the Gallery and
the File Manager demoboard added. Every test the native job of its platform
passed is not run again there: `select` prints a ctest -R pattern for the
tests of the programs build that are not in validated_tests, ALL when the
manifest records no list (run everything), or NONE when nothing is left.
Skipped tests (SKIP_RETURN_CODE) are not passed tests and are run again.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ElementTree

JUNIT_NAME: str = 'ctest-native.xml'
# Characters a CMake regular expression treats as operators.
REGEX_OPERATORS: str = '.[]()*+?^$|\\'


def passed_tests(junit: Path) -> list[str]:
    """Names of the test cases a CTest JUnit report shows as run without failure or skip.

    CTest writes status="run" for a test that ran; a failed one carries a
    <failure> child, and a skipped or disabled one has another status or a
    <skipped> child. The result is sorted and has no duplicates.
    """
    root: ElementTree.Element = ElementTree.parse(junit).getroot()
    names: set[str] = set()
    case: ElementTree.Element
    for case in root.iter('testcase'):
        if case.get('status') != 'run':
            continue
        if case.find('failure') is not None or case.find('error') is not None or case.find('skipped') is not None:
            continue
        name: str | None = case.get('name')
        if name:
            names.add(name)
    return sorted(names)


def validated_tests(manifest: dict[str, object]) -> list[str] | None:
    """The manifest's validated_tests, or None when it records none (an older producer)."""
    recorded: object = manifest.get('validated_tests')
    if not isinstance(recorded, list):
        return None
    names: list[str] = []
    item: object
    for item in recorded:
        if not isinstance(item, str):
            raise ValueError('validated_tests holds a value that is not a test name')
        names.append(item)
    return names


def escaped(name: str) -> str:
    """name as a literal in a CMake regular expression."""
    characters: list[str] = []
    character: str
    for character in name:
        if character in REGEX_OPERATORS:
            characters.append('\\')
        characters.append(character)
    return ''.join(characters)


def remaining_pattern(build_tests: list[str], validated: list[str] | None) -> str:
    """The ctest -R pattern of build_tests outside validated, ALL or NONE (see the module text)."""
    if validated is None:
        return 'ALL'
    already: set[str] = set(validated)
    remaining: list[str] = []
    name: str
    for name in build_tests:
        if name not in already:
            remaining.append(escaped(name))
    if not remaining:
        return 'NONE'
    return '^(' + '|'.join(remaining) + ')$'


def build_tests(build: Path) -> list[str]:
    """Names of every test ctest lists in a configured build directory."""
    listing_text: str = subprocess.run(['ctest', '--test-dir', str(build), '--show-only=json-v1'],
                                       check=True, capture_output=True, text=True).stdout
    listing: dict = json.loads(listing_text)
    names: list[str] = []
    test: dict
    for test in listing.get('tests', []):
        names.append(test['name'])
    return names


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', choices=['select'])
    parser.add_argument('--manifest', type=Path, required=True,
                        help="the native job's cache-manifest.json")
    parser.add_argument('--build-dir', type=Path, required=True, help='the configured programs build directory')
    args: argparse.Namespace = parser.parse_args(argv)
    validated: list[str] | None = None
    if args.manifest.is_file():
        manifest: dict[str, object] = json.loads(args.manifest.read_text(encoding='utf-8'))
        validated = validated_tests(manifest)
    pattern: str = remaining_pattern(build_tests(args.build_dir), validated)
    print(pattern)
    return 0


if __name__ == '__main__':
    sys.exit(main())
