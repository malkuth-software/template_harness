"""The names projects/gui_forms.toml produces are byte for byte the names GUI.Forms used.

Every expected value below is a literal copied from GUI.Forms at origin/main
e13a02a4 (tools/*.py, cmake/llvm22.cmake, tools/install_linux_ci_packages.sh),
before the tools moved here. The tools compute their names from the project
file; if one of these assertions fails, a GUI.Forms cache, receipt, artifact,
release asset or environment variable would change its name, which starts a
new history on GitHub. Change an expected value only together with GUI.Forms.
"""
from __future__ import annotations
from pathlib import Path
import unittest

import harness_testing
import project
import change_selection
import check_state_machines
import ci_build_steps
import ci_promote
import ci_reuse
import ci_sanitizer_seed
import ci_tree_receipt
import prune_cache_releases
import windows_toolchain

PLATFORMS: tuple[str, ...] = ('linux-x64', 'linux-arm64', 'macos-arm64', 'windows-x64')
TREE: str = '0123456789abcdef0123456789abcdef01234567'
ENVIRONMENT: str = 'f' * 64


class ReferenceProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reference: project.Project = project.load(harness_testing.REFERENCE_PROJECT)

    def test_the_tools_run_for_the_reference_project(self) -> None:
        # The tools resolved project.current() at import; it must be this file.
        self.assertEqual(project.current().path, self.reference.path)
        self.assertEqual(project.current().source_root(), project.workspace_root() / 'gui_forms')

    def test_product_and_layout(self) -> None:
        self.assertEqual(self.reference.repository, 'malkuth-software/gui_forms')
        self.assertEqual(self.reference.source_dir, 'gui_forms')
        self.assertEqual(self.reference.build_tree('linux-x64'), '.build/native-linux-x64/gui-forms')
        self.assertEqual(self.reference.build_tree('windows-x64', sanitize=True),
                         '.build/native-windows-x64/gui-forms-sanitize')
        self.assertEqual(self.reference.native_workflow, '.github/workflows/native.yml')
        self.assertEqual(self.reference.native_job, 'native')

    def test_environment_variables(self) -> None:
        expected: dict[str, str] = {
            'LLVM_RUNTIME': 'GUI_FORMS_LLVM_RUNTIME', 'PLATFORM': 'GUI_FORMS_PLATFORM',
            'COMPILER_ID': 'GUI_FORMS_COMPILER_ID', 'RUNNER': 'GUI_FORMS_RUNNER',
            'SANITIZER_DEBUG_INFO': 'GUI_FORMS_SANITIZER_DEBUG_INFO',
            'PACKAGE_WINDOWS_TOOLCHAIN': 'GUI_FORMS_PACKAGE_WINDOWS_TOOLCHAIN'}
        name: str
        for name in expected:
            self.assertEqual(self.reference.variable(name), expected[name])
        self.assertEqual(windows_toolchain.PACKAGE_VARIABLE, 'GUI_FORMS_PACKAGE_WINDOWS_TOOLCHAIN')
        self.assertEqual(ci_build_steps.DEBUG_INFO_VARIABLE, 'GUI_FORMS_SANITIZER_DEBUG_INFO')
        self.assertEqual(ci_build_steps.CONFIGURE_ENVIRONMENT, (
            'CC', 'CXX', 'OBJCXX', 'CFLAGS', 'CXXFLAGS', 'OBJCXXFLAGS', 'LDFLAGS',
            'CMAKE_C_COMPILER_LAUNCHER', 'CMAKE_CXX_COMPILER_LAUNCHER', 'CMAKE_OBJCXX_COMPILER_LAUNCHER',
            'GUI_FORMS_LLVM_RUNTIME', 'GUI_FORMS_SYSTEM_BUILD_TOOLS'))

    def test_workflow_artifacts(self) -> None:
        identity: dict[str, str] = {'platform': 'linux-x64', 'tree': TREE, 'environment': ENVIRONMENT}
        self.assertEqual(ci_reuse.artifact_name('validated', identity),
                         'gui-validated-linux-x64-' + TREE + '-' + ENVIRONMENT)
        self.assertEqual(ci_reuse.artifact_name('inputs', identity),
                         'gui-inputs-linux-x64-' + TREE + '-' + ENVIRONMENT)
        self.assertEqual(ci_tree_receipt.artifact_name('docs', TREE), 'gui-tree-docs-' + TREE)
        self.assertEqual(ci_sanitizer_seed.ARTIFACT_NAME, 'gui-sanitize-ccache-linux-x64')
        self.assertEqual(ci_sanitizer_seed.ARCHIVE_NAME, 'gui-sanitize-ccache.tar')
        self.assertEqual(self.reference.artifact('validated-' + 'macos-arm64' + '-' + TREE + '-'),
                         'gui-validated-macos-arm64-' + TREE + '-')

    def test_cache_and_toolchain_assets(self) -> None:
        platform: str
        for platform in PLATFORMS:
            self.assertEqual(self.reference.cache_asset(platform), 'gui-forms-cache-' + platform + '.tar.gz')
        self.assertEqual(windows_toolchain.ARCHIVE_NAME, 'gui-forms-toolchain-windows-x64.tar.zst')
        self.assertEqual(windows_toolchain.MANIFEST_NAME, 'gui-forms-toolchain-windows-x64.json')
        self.assertEqual(windows_toolchain.CACHE_NAME, 'gui-forms-cache-windows-x64.tar.gz')
        self.assertEqual(windows_toolchain.artifact_name('ab' * 32), 'gui-forms-toolchain-windows-x64-' + 'ab' * 32)
        self.assertEqual(self.reference.toolchain_repository, 'malkuth-software/gui_forms')

    def test_checkout_files(self) -> None:
        workspace: Path = project.workspace_root()
        self.assertEqual(change_selection.AREAS_FILE, workspace / 'gui_forms' / 'tools' / 'test_areas.json')
        self.assertEqual(prune_cache_releases.PINNED_PATH,
                         workspace / 'gui_forms' / 'packaging' / 'pinned_cache_revisions.json')

    def test_test_name_prefixes(self) -> None:
        self.assertEqual(change_selection.tests_named_by_file('tests/example_tests.cpp', [
            'example_tests', 'gui_forms_example_tests', 'gui_drawing_example_tests', 'other_example_tests']),
            ['example_tests', 'gui_forms_example_tests', 'gui_drawing_example_tests'])

    def test_cache_manifest_options(self) -> None:
        shared: list[tuple[str, str | int | bool]] = [
            ('build_type', 'Release'), ('cxx_standard', 20), ('pic', True), ('audio', True), ('tests', True),
            ('gallery', False), ('skia', True), ('harfbuzz', True)]
        platform: str
        for platform in PLATFORMS:
            expected: list[tuple[str, str | int | bool]] = list(shared)
            if platform == 'windows-x64':
                expected[6] = ('skia', False)
            self.assertEqual(list(self.reference.native_options_for(platform).items()), expected, platform)
        self.assertEqual(self.reference.cache_profile,
                         'native Release plus separately tested development text/host profiles')

    def test_linux_package_profiles(self) -> None:
        toolkit: str = ('cmake ninja-build ccache clang-22 generate-ninja pkg-config '
                        'libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxfixes-dev libxi-dev '
                        'libatk1.0-dev libatk-bridge2.0-dev '
                        'xvfb xauth xcompmgr weston xwayland xdg-utils dbus at-spi2-core python3-gi gir1.2-atspi-2.0')
        self.assertEqual(' '.join(self.reference.linux_packages['toolkit']), toolkit)
        self.assertEqual(' '.join(self.reference.linux_packages['programs']), toolkit + ' xdotool patchelf dpkg-dev')
        self.assertEqual(sorted(self.reference.linux_packages), ['programs', 'toolkit'])

    def test_state_machine_scope(self) -> None:
        self.assertEqual(check_state_machines.FIRST_PARTY, (
            'include', 'src', 'demo', 'tests', 'examples', 'tools', 'file_manager_demoboard', 'stx_vorbis',
            'stx_zlib', 'stx_png', 'stx_conv', 'fuzz'))
        self.assertEqual(check_state_machines.VOCABULARY_HEADERS, (
            'include/gui_forms/anatomy/types/anatomy_types.hpp',
            'include/gui_forms/semantics/types/semantic_types.hpp',
            'include/gui_forms/theme/part_painting/part_painting.hpp',
            'include/gui_forms/theme/types/theme_types.hpp'))
        self.assertEqual(check_state_machines.AREAS, (
            ('include/gui_forms/controls/', 'controls'), ('src/controls/', 'controls'), ('demo/', 'controls'),
            ('src/host/windows/', 'host-windows'), ('src/host/macos/', 'host-macos'),
            ('src/host/linux/', 'host-linux'), ('src/host/', 'host'), ('src/render/', 'render'),
            ('src/abi/', 'c-abi'), ('src/audio/', 'audio'), ('src/runtime/', 'runtime'), ('src/core/', 'core'),
            ('include/', 'core'), ('tests/', 'tests'), ('file_manager_demoboard/', 'demoboard'),
            ('stx_vorbis/', 'stx_vorbis'), ('stx_zlib/', 'stx_zlib'), ('stx_png/', 'stx_png'),
            ('stx_conv/', 'stx_conv'), ('fuzz/', 'fuzz'), ('examples/', 'examples'), ('tools/', 'tools')))
        self.assertEqual(check_state_machines.CHECKER_FILES, (
            'tools/check_state_machines.py', 'tools/check_house_style.py', '.github/workflows/state-machines.yml'))


if __name__ == '__main__':
    unittest.main()
