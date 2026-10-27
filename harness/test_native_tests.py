#!/usr/bin/env python3
"""Tests for native_tests.py: the native job's passed tests and the programs job's remainder."""
from __future__ import annotations
from pathlib import Path
import tempfile
import unittest

import native_tests

# A CTest JUnit report as CTest 3.28-4.x writes it: one passed, one failed,
# one skipped through SKIP_RETURN_CODE and one whose executable is missing.
JUNIT: str = '''<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="gui-forms" tests="4" failures="1" disabled="0" skipped="2" hostname="" time="3" timestamp="2026-10-22T09:57:37">
\t<testcase name="gui_forms_core_tests" classname="gui_forms_core_tests" time="1" status="run">
\t\t<system-out>ok</system-out>
\t</testcase>
\t<testcase name="gui_forms_threading_tests" classname="gui_forms_threading_tests" time="1" status="fail">
\t\t<failure message="Failed"/>
\t</testcase>
\t<testcase name="gui_forms_linux_accessibility_tests" classname="gui_forms_linux_accessibility_tests" time="0" status="notrun">
\t\t<skipped message="Skipped"/>
\t</testcase>
\t<testcase name="stx_png_core_tests" classname="stx_png_core_tests" time="0" status="notrun">
\t\t<skipped message="Unable to find executable"/>
\t</testcase>
\t<testcase name="gui_forms_core_tests" classname="gui_forms_core_tests" time="1" status="run"/>
</testsuite>
'''


class PassedTestsTests(unittest.TestCase):
    def test_only_run_tests_without_failure_or_skip_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            junit: Path = Path(directory) / native_tests.JUNIT_NAME
            junit.write_text(JUNIT, encoding='utf-8')
            self.assertEqual(native_tests.passed_tests(junit), ['gui_forms_core_tests'])

    def test_a_run_status_with_a_failure_child_is_not_passed(self) -> None:
        report: str = ('<testsuite><testcase name="a" status="run"><failure message="x"/></testcase>'
                       '<testcase name="b" status="run"/></testsuite>')
        with tempfile.TemporaryDirectory() as directory:
            junit: Path = Path(directory) / native_tests.JUNIT_NAME
            junit.write_text(report, encoding='utf-8')
            self.assertEqual(native_tests.passed_tests(junit), ['b'])


class ValidatedTestsTests(unittest.TestCase):
    def test_a_manifest_without_the_list_records_none(self) -> None:
        self.assertIsNone(native_tests.validated_tests({'schema': 2}))
        self.assertIsNone(native_tests.validated_tests({'validated_tests': 'gui_forms_core_tests'}))

    def test_the_recorded_names_are_returned(self) -> None:
        self.assertEqual(native_tests.validated_tests({'validated_tests': ['a', 'b']}), ['a', 'b'])

    def test_a_name_that_is_not_a_string_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            native_tests.validated_tests({'validated_tests': ['a', 3]})


class RemainingPatternTests(unittest.TestCase):
    def test_no_recorded_list_runs_everything(self) -> None:
        self.assertEqual(native_tests.remaining_pattern(['a', 'b'], None), 'ALL')

    def test_every_test_validated_leaves_nothing(self) -> None:
        self.assertEqual(native_tests.remaining_pattern(['a', 'b'], ['b', 'a', 'c']), 'NONE')

    def test_the_tests_outside_the_list_form_an_anchored_pattern(self) -> None:
        pattern: str = native_tests.remaining_pattern(
            ['gui_forms_core_tests', 'gui_forms_gallery_page_tests', 'gui_forms_file_manager_demoboard_tests'],
            ['gui_forms_core_tests'])
        self.assertEqual(pattern, '^(gui_forms_gallery_page_tests|gui_forms_file_manager_demoboard_tests)$')

    def test_an_empty_list_runs_every_test_by_name(self) -> None:
        self.assertEqual(native_tests.remaining_pattern(['a'], []), '^(a)$')

    def test_regular_expression_operators_in_names_are_escaped(self) -> None:
        self.assertEqual(native_tests.remaining_pattern(['a.b+(c)'], []), '^(a\\.b\\+\\(c\\))$')


if __name__ == '__main__':
    unittest.main()
