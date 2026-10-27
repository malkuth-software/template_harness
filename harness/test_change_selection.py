"""Unit tests for change_selection.py over a copy of GUI.Forms' tools/test_areas.json (no build is run).

testdata/gui_forms_test_areas.json is a snapshot: these tests check the selection rules, not
GUI.Forms' current areas, whose consistency with its CMake stays tested in GUI.Forms."""
from __future__ import annotations
import json
from pathlib import Path
import re
import unittest

import harness_testing
import change_selection

TESTDATA: Path = Path(__file__).resolve().parent / 'testdata'
SOURCE: Path = Path('/work/gui_forms')
DOCUMENT: dict = json.loads((TESTDATA / 'gui_forms_test_areas.json').read_text(encoding='utf-8'))
RULES: list[change_selection.PathRule] = change_selection.load_path_rules(DOCUMENT)


def listed(name: str, area: str | None, command: list[str] | None = None) -> dict:
    """One test as ctest --show-only=json-v1 lists it."""
    test: dict = {'name': name, 'command': command or ['/work/.build/' + name], 'properties': []}
    if area is not None:
        test['properties'].append({'name': 'LABELS', 'value': ['area_' + area]})
    return test


class RuleMatchingTests(unittest.TestCase):
    def test_a_directory_entry_names_everything_under_it(self) -> None:
        self.assertTrue(change_selection.rule_matches('src/core/', 'src/core/xml/xml.cpp'))
        self.assertFalse(change_selection.rule_matches('src/core/', 'src/core_extra.cpp'))

    def test_a_glob_stays_within_one_segment(self) -> None:
        self.assertTrue(change_selection.rule_matches('*.md', 'README.md'))
        self.assertFalse(change_selection.rule_matches('*.md', 'docs/README.md'))
        self.assertTrue(change_selection.rule_matches('include/gui_forms/*_controls.hpp',
                                                      'include/gui_forms/basic_controls.hpp'))

    def test_a_tests_entry_is_a_file_name_prefix(self) -> None:
        self.assertTrue(change_selection.rule_matches('tests/drawing_', 'tests/drawing_core_tests.cpp'))

    def test_another_file_entry_names_one_file(self) -> None:
        self.assertTrue(change_selection.rule_matches('CMakeLists.txt', 'CMakeLists.txt'))
        self.assertFalse(change_selection.rule_matches('CMakeLists.txt', 'stx_zlib/CMakeLists.txt'))

    def test_the_longest_entry_wins(self) -> None:
        rules: list[change_selection.PathRule] = [change_selection.PathRule('src/', ('core',)),
                                                  change_selection.PathRule('src/host/', ('host',))]
        self.assertEqual(change_selection.areas_for_path('src/host/windows/a.cpp', rules), ('host',))
        self.assertEqual(change_selection.areas_for_path('src/core/a.cpp', rules), ('core',))
        self.assertIsNone(change_selection.areas_for_path('other/a.cpp', rules))


class CheckedInRuleTests(unittest.TestCase):
    def areas(self, path: str) -> tuple[str, ...] | None:
        return change_selection.areas_for_path(path, RULES)

    def test_the_touches_everything_set(self) -> None:
        path: str
        for path in ('CMakeLists.txt', 'cmake/Audio.cmake', 'third_party/fetch_text_stack.sh',
                     '.github/workflows/native.yml', 'include/gui_forms/control.hpp', 'include/gui_forms/c_api.h',
                     'tools/ci_build.py', 'tools/change_selection.py', 'tools/test_areas.json',
                     'tools/sanitizer/lsan.supp', 'tests/support/test_support.hpp', 'assets/fonts/Carlito-Bold.ttf'):
            self.assertEqual(self.areas(path), (change_selection.EVERY_AREA,), path)

    def test_documentation_and_planning_are_read_by_no_test(self) -> None:
        path: str
        for path in ('docs/dev/roadmap.md', 'docs/dev/contributing.md', 'packaging/RELEASE_NOTES.md',
                     'README.md', 'AGENTS.md', 'docs/dev/decisions.md', 'benchmarks/x.md'):
            self.assertEqual(self.areas(path), (), path)

    def test_a_new_top_level_directory_selects_every_test(self) -> None:
        self.assertIsNone(self.areas('new_project/source.cpp'))

    def test_core_sources_leave_out_the_gui_and_accessibility_tests(self) -> None:
        areas: tuple[str, ...] | None = self.areas('src/core/dispatcher/dispatcher.cpp')
        self.assertIsNotNone(areas)
        self.assertIn('core', areas)
        self.assertIn('controls', areas)
        self.assertNotIn('gui', areas)
        self.assertNotIn('a11y', areas)
        self.assertIn('a11y', self.areas('src/core/semantics/semantic_tree.cpp'))

    def test_controls_render_and_host_sources_reach_the_gui_and_accessibility_tests(self) -> None:
        path: str
        for path in ('src/controls/ribbon/ribbon.cpp', 'include/gui_forms/controls/ribbon/ribbon.hpp',
                     'include/gui_forms/basic_controls.hpp', 'src/render/skia/skia_canvas.cpp',
                     'src/host/windows/window.cpp', 'manifests/controls.json'):
            areas: tuple[str, ...] | None = self.areas(path)
            self.assertIsNotNone(areas, path)
            self.assertIn('gui', areas, path)
            self.assertIn('a11y', areas, path)

    def test_codec_and_audio_sources_reach_only_their_own_area(self) -> None:
        self.assertEqual(self.areas('stx_zlib/src/deflate.cpp'), ('codec',))
        self.assertEqual(self.areas('stx_png/src/png.cpp'), ('codec',))
        self.assertEqual(self.areas('include/gui_forms/compression/inflate.hpp'), ('codec',))
        self.assertEqual(self.areas('stx_vorbis/src/vorbis.c'), ('audio',))
        self.assertEqual(self.areas('src/audio/audio.cpp'), ('audio',))


class SelectionTests(unittest.TestCase):
    LISTING: dict = {'tests': [
        listed('gui_forms_dispatcher_tests', 'core'),
        listed('gui_forms_tool_strip_tests', 'controls'),
        listed('gui_forms_gallery_pages_tests', 'gui'),
        listed('gui_forms_windows_accessibility_uia_gallery_tests', 'a11y'),
        listed('stx_zlib_deflate_tests', 'codec'),
        listed('gui_forms_unclassified_tests', None),
        listed('gui_forms_host_boundary_audit', 'host',
               ['/usr/bin/cmake', '-DSOURCE_DIRECTORY=/work/gui_forms/src', '-P',
                '/work/gui_forms/cmake/check_host_boundaries.cmake']),
    ]}

    def names(self, changed: list[str]) -> list[str]:
        selection: change_selection.Selection = change_selection.select(changed, RULES, self.LISTING, SOURCE)
        names: list[str] = []
        test: dict
        for test in change_selection.selected_tests(self.LISTING, selection):
            names.append(test['name'])
        return names

    def test_a_codec_change_runs_codec_tests_and_unlabelled_tests(self) -> None:
        self.assertEqual(self.names(['stx_zlib/src/deflate.cpp']),
                         ['stx_zlib_deflate_tests', 'gui_forms_unclassified_tests'])

    def test_a_core_change_leaves_out_gui_and_accessibility(self) -> None:
        names: list[str] = self.names(['src/core/dispatcher/dispatcher.cpp'])
        self.assertIn('gui_forms_dispatcher_tests', names)
        self.assertIn('gui_forms_tool_strip_tests', names)
        self.assertNotIn('gui_forms_gallery_pages_tests', names)
        self.assertNotIn('gui_forms_windows_accessibility_uia_gallery_tests', names)
        # Its command names src/, so the boundary audit reads the change.
        self.assertIn('gui_forms_host_boundary_audit', names)

    def test_a_test_source_selects_its_own_test(self) -> None:
        self.assertEqual(self.names(['tests/tool_strip_tests.cpp']),
                         ['gui_forms_tool_strip_tests', 'gui_forms_unclassified_tests'])

    def test_a_cmake_change_runs_every_test(self) -> None:
        self.assertEqual(len(self.names(['CMakeLists.txt'])), len(self.LISTING['tests']))

    def test_an_unclassified_test_source_runs_every_test(self) -> None:
        self.assertEqual(len(self.names(['tests/shared_helper.hpp'])), len(self.LISTING['tests']))

    def test_documentation_selects_only_unlabelled_tests(self) -> None:
        self.assertEqual(self.names(['docs/dev/contributing.md']), ['gui_forms_unclassified_tests'])

    def test_reasons_name_every_changed_path(self) -> None:
        selection: change_selection.Selection = change_selection.select(
            ['README.md', 'src/host/windows/window.cpp'], RULES, self.LISTING, SOURCE)
        self.assertEqual(selection.reasons['README.md'], 'read by no test')
        self.assertIn('areas host', selection.reasons['src/host/windows/window.cpp'])

    def test_only_documentation_needs_no_tree(self) -> None:
        self.assertFalse(change_selection.any_path_read(['README.md', 'docs/dev/roadmap.md'], RULES))
        self.assertTrue(change_selection.any_path_read(['README.md', 'src/core/xml/xml.cpp'], RULES))
        self.assertTrue(change_selection.any_path_read(['tests/xml_tests.cpp'], RULES))
        self.assertTrue(change_selection.any_path_read(['new_project/a.cpp'], RULES))

    def test_the_name_pattern_matches_exactly_the_names(self) -> None:
        pattern: str = change_selection.name_pattern(['a_tests', 'b.c'])
        self.assertEqual(pattern, '^(a_tests|b\\.c)$')
        self.assertIsNotNone(re.match(pattern, 'b.c'))
        self.assertIsNone(re.match(pattern, 'bxc'))
        self.assertIsNone(re.match(pattern, 'a_tests_more'))


if __name__ == '__main__':
    unittest.main()
