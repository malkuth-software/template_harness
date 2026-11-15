"""Checks the handler-rules fixtures (docs/handler_methodology.md, section 4) are well formed.

The rules are a design proposal and the tool is not built yet, so this
checks what can be checked without clang: every fixture names its flags,
uses only the proposed rule names, has a bad case (at least one
expectation) and a fixed case (a function whose name says so, with no
expectation inside it), and the document's fixture table lists exactly the
fixtures present. When the tool is built, its self-test runs the fixtures.
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

HARNESS: Path = Path(__file__).resolve().parent
FIXTURES: Path = HARNESS / 'testdata' / 'handler_rules'
DOCUMENT: Path = HARNESS.parent / 'docs' / 'handler_methodology.md'
RULES: frozenset[str] = frozenset({'D1', 'D2', 'D3', 'H1', 'H2', 'H3'})
EXPECT: re.Pattern[str] = re.compile(r'//\s*expect:\s*(.+)$')
# A function definition's first line: a name, a parameter list, optional
# qualifiers, and the opening brace.
DEFINITION: re.Pattern[str] = re.compile(r'\b(\w+)\([^;]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{\s*$', re.MULTILINE)
FIXED_NAME: re.Pattern[str] = re.compile(r'(_good$|^try_|_from_input$)')


def fixture_paths() -> list[Path]:
    paths: list[Path] = sorted(FIXTURES.glob('*.cpp'))
    return paths


def expectations(path: Path) -> list[tuple[int, str, list[str]]]:
    """Each expectation as (line number, enclosing function, rules)."""
    found: list[tuple[int, str, list[str]]] = []
    function: str = ''
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        definition: re.Match[str] | None = DEFINITION.search(line)
        if definition is not None and not line.lstrip().startswith(('if ', 'for ', 'while ', 'switch ')):
            function = definition.group(1)
        expected: re.Match[str] | None = EXPECT.search(line)
        if expected is not None:
            rules: list[str] = [rule.strip() for rule in expected.group(1).split(',')]
            found.append((number, function, rules))
    return found


class FixtureTests(unittest.TestCase):
    def test_fixtures_exist(self) -> None:
        self.assertGreaterEqual(len(fixture_paths()), 10)
        self.assertTrue((FIXTURES / 'vocabulary.hpp').is_file())

    def test_first_line_names_flags(self) -> None:
        for path in fixture_paths():
            first: str = path.read_text(encoding='utf-8').splitlines()[0]
            self.assertTrue(first.startswith('// flags:'), path.name)

    def test_expectations_use_known_rules_inside_functions(self) -> None:
        for path in fixture_paths():
            found: list[tuple[int, str, list[str]]] = expectations(path)
            self.assertTrue(found, f'{path.name} has no bad case')
            for number, function, rules in found:
                self.assertTrue(function, f'{path.name}:{number} is outside a function')
                for rule in rules:
                    self.assertIn(rule, RULES, f'{path.name}:{number}')

    def test_fixed_cases_expect_nothing(self) -> None:
        for path in fixture_paths():
            text: str = path.read_text(encoding='utf-8')
            names: list[str] = [match.group(1) for match in DEFINITION.finditer(text)]
            fixed: list[str] = [name for name in names if FIXED_NAME.search(name) is not None]
            self.assertTrue(fixed, f'{path.name} has no fixed case')
            for number, function, rules in expectations(path):
                self.assertIsNone(FIXED_NAME.search(function), f'{path.name}:{number} expects {rules} in {function}')

    def test_rule_prefix_matches_file(self) -> None:
        for path in fixture_paths():
            family: str = path.name[0].upper()
            for number, function, rules in expectations(path):
                self.assertTrue(all(rule[0] == family for rule in rules), f'{path.name}:{number}')

    def test_document_lists_every_fixture(self) -> None:
        listed: set[str] = set(re.findall(r'^\| `(\w+\.cpp)` \|', DOCUMENT.read_text(encoding='utf-8'), re.MULTILINE))
        present: set[str] = {path.name for path in fixture_paths()}
        self.assertEqual(listed, present)


if __name__ == '__main__':
    unittest.main()
