"""Unit tests for check_early_exit.py: settings, entries, the shrink-only allowance and the fixtures' expectations.

These need no clang. The fixtures themselves run through the built tool in
the self-test (`check_early_exit.py --self-test`); with HARNESS_EARLY_EXIT_TOOL
naming a built early_exit_check, test_self_test runs it here too.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest

import harness_testing
import check_early_exit as early_exit

TOOL: str = os.environ.get('HARNESS_EARLY_EXIT_TOOL', '')


def record(path: str, line: int, rule: str, level: str = 'finding', function: str = 'ns::f') -> str:
    text: str = json.dumps({'rule': rule, 'level': level, 'pattern': '', 'file': path, 'line': line, 'column': 5,
                            'function': function, 'message': 'm'})
    return text


class SettingsTests(unittest.TestCase):
    def test_defaults_fill_in(self) -> None:
        settings: early_exit.Settings = early_exit.read_settings({'state_machines': {'first_party_dirs': ['src']}})
        self.assertEqual(settings.first_party_dirs, ('src',))
        self.assertFalse(settings.standard_library_foreign)
        self.assertIn('^read$', settings.cancellation_points)
        self.assertIn('^memcpy$', settings.no_exit_functions)
        self.assertIn('^mi_.*$', settings.allocator_allowlist)
        self.assertEqual(settings.allowance, 'tools/early_exit_allowance.txt')
        self.assertEqual(settings.letting_go_resets, ('^std::unique_ptr$', '^std::shared_ptr$', '^std::weak_ptr$'))

    def test_extra_lists_append(self) -> None:
        settings: early_exit.Settings = early_exit.read_settings(
            {'early_exit': {'first_party_dirs': ['src'], 'foreign_functions_extra': ['^ns::Event::emit$'],
                            'standard_library': 'foreign'}})
        self.assertIn('^std::invoke$', settings.foreign_functions)
        self.assertEqual(settings.foreign_functions[-1], '^ns::Event::emit$')
        self.assertTrue(settings.standard_library_foreign)
        listed: early_exit.Settings = early_exit.read_settings(
            {'early_exit': {'first_party_dirs': ['src'], 'letting_go_resets_extra': ['^stxlib::Unique$']}})
        self.assertEqual(listed.letting_go_resets[-1], '^stxlib::Unique$')
        self.assertIn('^std::weak_ptr$', listed.letting_go_resets)

    def test_refuses_bad_values(self) -> None:
        with self.assertRaises(ValueError):
            early_exit.read_settings({'early_exit': {'first_party_dirs': ['src'], 'standard_library': 'mine'}})
        with self.assertRaises(ValueError):
            early_exit.read_settings({'early_exit': {'first_party_dirs': 'src'}})
        with self.assertRaises(ValueError):
            early_exit.read_settings({'early_exit': {}})

    def test_reference_project_parses(self) -> None:
        with harness_testing.REFERENCE_PROJECT.open('rb') as stream:
            document: dict = tomllib.load(stream)
        settings: early_exit.Settings = early_exit.read_settings(document)
        self.assertIn('src', settings.first_party_dirs)
        self.assertIn('^gui_forms::Event::emit$', settings.foreign_functions)

    def test_tool_settings_are_absolute(self) -> None:
        settings: early_exit.Settings = early_exit.read_settings(
            {'early_exit': {'first_party_dirs': ['src'], 'foreign_paths': ['third_party']}})
        root: Path = Path(tempfile.gettempdir()) / 'checkout'
        document: dict = early_exit.tool_settings(settings, root)
        self.assertEqual(document['first_party_roots'], [Path(os.path.abspath(root / 'src')).as_posix()])
        self.assertTrue(document['foreign_paths'][0].endswith('/third_party'))
        self.assertFalse(document['standard_library_foreign'])
        self.assertIn('^std::unique_ptr$', document['letting_go_resets'])


class EntryTests(unittest.TestCase):
    def test_parse_relativizes_and_drops_outside(self) -> None:
        root: Path = Path(tempfile.gettempdir()) / 'checkout'
        inside: str = Path(os.path.abspath(root / 'src' / 'a.cpp')).as_posix()
        output: str = '\n'.join([record(inside, 3, 'R1'), record('/elsewhere/b.cpp', 4, 'R2'), ''])
        entries: list[early_exit.Entry] = early_exit.parse_entries(output, root)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].path, 'src/a.cpp')
        self.assertEqual(entries[0].text(), 'src/a.cpp:3:5: R1 ns::f: m')


def entry(line: int, rule: str = 'R1', function: str = 'ns::f') -> early_exit.Entry:
    made: early_exit.Entry = early_exit.Entry(path='src/a.cpp', line=line, column=1, rule=rule, level='finding',
                                              pattern='', function=function, message='m')
    return made


class AllowanceTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        findings: list[early_exit.Entry] = [entry(3), entry(9), entry(4, 'R5')]
        text: str = early_exit.format_allowance(findings)
        counts: dict = early_exit.parse_allowance(text)
        self.assertEqual(counts, {('R1', 'src/a.cpp', 'ns::f'): 2, ('R5', 'src/a.cpp', 'ns::f'): 1})

    def test_new_findings_beyond_the_count(self) -> None:
        allowance: dict = {('R1', 'src/a.cpp', 'ns::f'): 1}
        judgement: early_exit.Judgement = early_exit.judge([entry(3), entry(9), entry(2, 'R2')], allowance)
        self.assertEqual([(item.line, item.rule) for item in judgement.new], [(2, 'R2'), (9, 'R1')])
        self.assertEqual(judgement.stale, [])

    def test_stale_counts_fail(self) -> None:
        allowance: dict = {('R1', 'src/a.cpp', 'ns::f'): 3, ('R9', 'src/b.cpp', 'ns::g'): 1}
        judgement: early_exit.Judgement = early_exit.judge([entry(3)], allowance)
        self.assertEqual(judgement.new, [])
        self.assertEqual(len(judgement.stale), 2)

    def test_refuses_a_malformed_line(self) -> None:
        with self.assertRaises(ValueError):
            early_exit.parse_allowance('R1 src/a.cpp ns::f 2\n')
        self.assertEqual(early_exit.parse_allowance('# comment\n\n'), {})


class FixtureTests(unittest.TestCase):
    def test_expectations_parse(self) -> None:
        text: str = '// flags: -fno-exceptions\nf();  // expect: R1, R2\n// expect-report: R7\n'
        flags: list[str]
        expected: set
        flags, expected = early_exit.expectations(text)
        self.assertEqual(flags, ['-fno-exceptions'])
        self.assertEqual(expected, {(2, 'R1', 'finding'), (2, 'R2', 'finding'), (3, 'R7', 'report')})

    def test_windows_fixture_targets_windows(self) -> None:
        flags: list[str]
        expected: set
        flags, expected = early_exit.expectations((early_exit.FIXTURES / 'r2_windows.cpp').read_text(encoding='utf-8'))
        self.assertIn('--target=x86_64-pc-windows-gnu', flags)

    def test_every_rule_has_a_fixture(self) -> None:
        rules: set[str] = set()
        fixture: Path
        for fixture in sorted(early_exit.FIXTURES.glob('*.cpp')):
            flags: list[str]
            expected: set
            flags, expected = early_exit.expectations(fixture.read_text(encoding='utf-8'))
            self.assertTrue(flags, fixture.name + ' names no flags')
            rules.update(rule for _, rule, level in expected if level == 'finding')
        self.assertEqual(rules, {'R' + str(number) for number in range(1, 11)})

    @unittest.skipUnless(TOOL, 'HARNESS_EARLY_EXIT_TOOL names no built early_exit_check')
    def test_self_test(self) -> None:
        self.assertEqual(early_exit.self_test(Path(TOOL)), 0)


if __name__ == '__main__':
    unittest.main()
