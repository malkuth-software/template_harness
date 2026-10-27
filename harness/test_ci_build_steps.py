"""Unit tests for ci_build_steps.py (no build is run).

These came from GUI.Forms' tools/test_ci_build.py; its tests of the gallery
option, the build record of build_toolkit, its fetch-script pins, its
workflow, its CMake labels and its leak suppressions stay there."""
from __future__ import annotations
import os
from pathlib import Path
import tempfile
import unittest

import harness_testing
import ci_build_steps as ci_build


class SanitizerOptionTests(unittest.TestCase):
    def test_every_finding_stops_its_test(self) -> None:
        options: list[str]
        environment: dict[str, str]
        options, environment = ci_build.sanitizer_options('linux', Path('lsan.supp'))
        self.assertIn('-DCMAKE_CXX_FLAGS=' + ci_build.SANITIZER_FLAGS, options)
        self.assertIn('-fno-sanitize-recover=undefined', ci_build.SANITIZER_FLAGS)
        # Without it ccache hashes the build directory into every -g object.
        self.assertIn('-fdebug-compilation-dir=.', ci_build.SANITIZER_FLAGS)
        self.assertIn('detect_leaks=1', environment['ASAN_OPTIONS'])
        self.assertIn('halt_on_error=1', environment['UBSAN_OPTIONS'])
        self.assertIn('suppressions=lsan.supp', environment['LSAN_OPTIONS'])

    def test_leaks_are_detected_where_the_runtime_has_a_leak_sanitizer(self) -> None:
        options: list[str]
        environment: dict[str, str]
        host: str
        for host in ('linux', 'macos'):
            options, environment = ci_build.sanitizer_options(host, Path('lsan.supp'))
            self.assertIn('detect_leaks=1', environment['ASAN_OPTIONS'], host)
            self.assertIn('LSAN_OPTIONS', environment, host)
        options, environment = ci_build.sanitizer_options('windows', Path('lsan.supp'))
        self.assertIn('-DCMAKE_CXX_FLAGS=' + ci_build.SANITIZER_FLAGS, options)
        self.assertIn('detect_leaks=0', environment['ASAN_OPTIONS'])
        self.assertNotIn('LSAN_OPTIONS', environment)
        self.assertIn('halt_on_error=1', environment['UBSAN_OPTIONS'])

    def test_macos_flags_join_the_toolchain_flags(self) -> None:
        options: list[str]
        environment: dict[str, str]
        options, environment = ci_build.sanitizer_options('macos', Path('lsan.supp'))
        flag: str
        for flag in options:
            # Only the build type and its own flags: CMAKE_<LANG>_FLAGS would replace the toolchain's.
            self.assertTrue(flag.startswith('-DCMAKE_BUILD_TYPE=') or '_FLAGS_RELWITHDEBINFO=' in flag, flag)
        self.assertIn('-DCMAKE_OBJCXX_FLAGS_RELWITHDEBINFO=-O2 -gline-tables-only -DNDEBUG', options)
        name: str
        for name in ('CFLAGS', 'CXXFLAGS', 'OBJCXXFLAGS'):
            self.assertEqual(environment[name], ci_build.SANITIZER_FLAGS, name)
        self.assertEqual(environment['LDFLAGS'], ci_build.SANITIZER_LINK_FLAGS)

    def test_linux_and_windows_keep_command_line_flags(self) -> None:
        options: list[str]
        environment: dict[str, str]
        host: str
        for host in ('linux', 'windows'):
            options, environment = ci_build.sanitizer_options(host, Path('lsan.supp'))
            self.assertIn('-DCMAKE_C_FLAGS=' + ci_build.SANITIZER_FLAGS, options, host)
            self.assertIn('-DCMAKE_EXE_LINKER_FLAGS=' + ci_build.SANITIZER_LINK_FLAGS, options, host)
            self.assertIn('-DCMAKE_SHARED_LINKER_FLAGS=' + ci_build.SANITIZER_LINK_FLAGS, options, host)
            self.assertNotIn('CXXFLAGS', environment, host)
            self.assertNotIn('LDFLAGS', environment, host)


TREE: Path = Path(os.path.abspath('example-build-tree'))


def test_record(command: list[str], properties: list[dict] | None = None) -> dict:
    return {'name': 'example', 'command': command, 'properties': properties or []}


class TestTargetTests(unittest.TestCase):
    KNOWN: set[str] = {'gui_forms_example_tests.exe', 'libgui_forms_core.a', 'sub/tool.exe'}

    def test_a_test_executable_is_its_own_target(self) -> None:
        listing: dict = {'tests': [test_record([str(TREE / 'gui_forms_example_tests.exe')])]}
        self.assertEqual(ci_build.targets_for_tests(listing, self.KNOWN, TREE), ['gui_forms_example_tests.exe'])

    def test_definition_values_and_nested_outputs_are_read(self) -> None:
        listing: dict = {'tests': [test_record(['cmake', '-DCORE=' + str(TREE / 'libgui_forms_core.a'),
                                                str(TREE / 'sub' / 'tool.exe'), '-P', 'check.cmake'])]}
        self.assertEqual(ci_build.targets_for_tests(listing, self.KNOWN, TREE),
                         ['libgui_forms_core.a', 'sub/tool.exe'])

    def test_a_test_reading_only_sources_needs_nothing_built(self) -> None:
        listing: dict = {'tests': [test_record(['python', os.path.abspath('tools/check.py'), '--check'])]}
        self.assertEqual(ci_build.targets_for_tests(listing, self.KNOWN, TREE), [])

    def test_an_unknown_tree_file_builds_everything(self) -> None:
        listing: dict = {'tests': [test_record([str(TREE / 'generated' / 'table.txt')])]}
        self.assertIsNone(ci_build.targets_for_tests(listing, self.KNOWN, TREE))

    def test_tests_depending_on_other_tests_build_everything(self) -> None:
        name: str
        for name in ('FIXTURES_REQUIRED', 'DEPENDS', 'REQUIRED_FILES'):
            listing: dict = {'tests': [test_record([str(TREE / 'gui_forms_example_tests.exe')],
                                                   [{'name': name, 'value': ['setup']}])]}
            self.assertIsNone(ci_build.targets_for_tests(listing, self.KNOWN, TREE), name)

    def test_a_test_without_a_command_builds_everything(self) -> None:
        listing: dict = {'tests': [{'name': 'example', 'properties': []}]}
        self.assertIsNone(ci_build.targets_for_tests(listing, self.KNOWN, TREE))

    def test_a_sibling_directory_with_the_same_prefix_is_outside_the_tree(self) -> None:
        listing: dict = {'tests': [test_record([str(TREE) + '-other' + os.sep + 'gui_forms_example_tests.exe'])]}
        self.assertEqual(ci_build.targets_for_tests(listing, self.KNOWN, TREE), [])


class TestCommandTests(unittest.TestCase):
    def test_arguments_are_unquoted(self) -> None:
        self.assertEqual(ci_build.cmake_arguments('[=[a b]=] "x\\"y" plain [[c]]'), ['a b', 'x"y', 'plain', 'c'])

    def test_commands_come_from_the_tree_and_its_subdirectories(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            tree: Path = Path(scratch)
            (tree / 'sub').mkdir()
            (tree / 'CTestTestfile.cmake').write_text(
                '# CMake generated Testfile\n'
                'add_test("first_tests" "/b/first_tests.exe")\n'
                'set_tests_properties("first_tests" PROPERTIES  LABELS "area_core")\n'
                'subdirs("sub")\n', encoding='utf-8')
            (tree / 'sub' / 'CTestTestfile.cmake').write_text(
                'add_test([=[second_tests]=] "/usr/bin/python3" "/s/check.py" "-DX=/b/sub/tool")\n', encoding='utf-8')
            commands: dict[str, list[str]] = ci_build.test_commands(tree)
        self.assertEqual(commands, {'first_tests': ['/b/first_tests.exe'],
                                    'second_tests': ['/usr/bin/python3', '/s/check.py', '-DX=/b/sub/tool']})

    def test_a_tree_without_tests_has_no_commands(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            self.assertEqual(ci_build.test_commands(Path(scratch)), {})


class UnbuiltListingCommands:
    """Stands in for ci_build.captured: ctest's listing of a fresh tree, where no
    test executable exists yet, so ctest leaves every command out."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def capture(self, *arguments: str | Path) -> str:
        command: list[str] = []
        argument: str | Path
        for argument in arguments:
            command.append(str(argument))
        self.calls.append(command)
        return ('{"tests": [{"name": "stx_png_core_tests", "properties": []},'
                ' {"name": "gui_forms_paint_group_tests", "properties": []}]}')


class TestsPatternListingTests(unittest.TestCase):
    """--tests on a fresh tree (#217): the matching tests are listed with their
    commands, so the narrowed build makes their executables and none is Not Run."""

    def test_unbuilt_tests_matching_the_pattern_get_commands_and_targets(self) -> None:
        fake: UnbuiltListingCommands = UnbuiltListingCommands()
        original: object = ci_build.captured
        ci_build.captured = fake.capture
        try:
            with tempfile.TemporaryDirectory() as scratch:
                tree: Path = Path(scratch)
                (tree / 'stx_png').mkdir()
                (tree / 'CTestTestfile.cmake').write_text(
                    'add_test([=[gui_forms_paint_group_tests]=] "' + (tree / 'gui_forms_paint_group_tests.exe').as_posix()
                    + '")\nsubdirs("stx_png")\n', encoding='utf-8')
                (tree / 'stx_png' / 'CTestTestfile.cmake').write_text(
                    'add_test([=[stx_png_core_tests]=] "' + (tree / 'stx_png' / 'stx_png_core_tests.exe').as_posix()
                    + '")\n', encoding='utf-8')
                listing: dict = ci_build.test_listing(tree, 'stx_png_core|paint_group', '')
                known: set[str] = {'gui_forms_paint_group_tests.exe', 'stx_png/stx_png_core_tests.exe'}
                targets: list[str] | None = ci_build.targets_for_tests(listing, known, tree)
        finally:
            ci_build.captured = original
        self.assertIn('-R', fake.calls[0])
        self.assertIn('stx_png_core|paint_group', fake.calls[0])
        self.assertEqual(targets, ['gui_forms_paint_group_tests.exe', 'stx_png/stx_png_core_tests.exe'])


class SetupSkipTests(unittest.TestCase):
    def test_pins_without_checkouts_are_not_current(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            script: Path = Path(directory) / 'fetch.sh'
            script.write_text('example_revision=' + '0' * 40 + '\n', encoding='utf-8')
            self.assertFalse(ci_build.pinned_checkouts_current(script))

    def test_a_script_without_pins_is_not_current(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            script: Path = Path(directory) / 'fetch.sh'
            script.write_text('git fetch somewhere\n', encoding='utf-8')
            self.assertFalse(ci_build.pinned_checkouts_current(script))

    def test_the_configure_record_covers_command_environment_and_cache(self) -> None:
        base: str = ci_build.configure_record(['cmake', '-DA=1'], {'CC': 'clang'}, b'cache')
        self.assertEqual(base, ci_build.configure_record(['cmake', '-DA=1'], {'CC': 'clang', 'HOME': 'x'}, b'cache'))
        self.assertNotEqual(base, ci_build.configure_record(['cmake', '-DA=2'], {'CC': 'clang'}, b'cache'))
        self.assertNotEqual(base, ci_build.configure_record(['cmake', '-DA=1'], {'CC': 'gcc'}, b'cache'))
        self.assertNotEqual(base, ci_build.configure_record(['cmake', '-DA=1'], {'CC': 'clang'}, b'edited'))


class DebugInfoTests(unittest.TestCase):
    def test_line_tables_are_the_default(self) -> None:
        self.assertEqual(ci_build.debug_info_mode('', {}), 'lines')
        options: list[str]
        environment: dict[str, str]
        options, environment = ci_build.sanitizer_options('linux', Path('lsan.supp'))
        self.assertIn('-DCMAKE_CXX_FLAGS_RELWITHDEBINFO=-O2 -gline-tables-only -DNDEBUG', options)
        self.assertIn('-DCMAKE_C_FLAGS_RELWITHDEBINFO=-O2 -gline-tables-only -DNDEBUG', options)

    def test_full_restores_g_from_the_flag_or_the_environment(self) -> None:
        self.assertEqual(ci_build.debug_info_mode('full', {}), 'full')
        self.assertEqual(ci_build.debug_info_mode('', {ci_build.DEBUG_INFO_VARIABLE: ' Full '}), 'full')
        self.assertEqual(ci_build.debug_info_mode('lines', {ci_build.DEBUG_INFO_VARIABLE: 'full'}), 'lines')
        options: list[str]
        environment: dict[str, str]
        options, environment = ci_build.sanitizer_options('windows', Path('lsan.supp'), 'full')
        self.assertIn('-DCMAKE_CXX_FLAGS_RELWITHDEBINFO=-O2 -g -DNDEBUG', options)

    def test_other_modes_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            ci_build.debug_info_mode('', {ci_build.DEBUG_INFO_VARIABLE: 'some'})


class SelectionTests(unittest.TestCase):
    def test_patterns_and_labels_become_ctest_arguments(self) -> None:
        self.assertEqual(ci_build.ctest_selection('', ''), [])
        self.assertEqual(ci_build.ctest_selection('tool_strip', ''), ['-R', 'tool_strip', '--no-tests=error'])
        self.assertEqual(ci_build.ctest_selection('', 'gallery_accessibility'), ['-LE', 'gallery_accessibility'])


if __name__ == '__main__':
    unittest.main()
