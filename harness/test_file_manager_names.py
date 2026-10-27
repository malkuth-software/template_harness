"""The names projects/file_manager.toml produces, asserted literally.

File Manager runs the harness with its own project file for its own
artifacts and with GUI.Forms' for GUI.Forms' published Windows toolchain. A
failing assertion here means an edit would rename one of File Manager's
artifacts, build trees, variables or package profiles, or stop it finding
GUI.Forms' toolchain. Change an expected value only together with File Manager.
"""
from __future__ import annotations
from pathlib import Path
import unittest

import project

PROJECT_FILE: Path = Path(__file__).resolve().parents[1] / 'projects' / 'file_manager.toml'
PLATFORMS: tuple[str, ...] = ('linux-x64', 'macos-arm64', 'windows-x64')
TREE: str = '0123456789abcdef0123456789abcdef01234567'


class FileManagerProjectNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.product: project.Project = project.load(PROJECT_FILE)

    def test_product_and_layout(self) -> None:
        self.assertEqual(self.product.name, 'file_manager')
        self.assertEqual(self.product.repository, 'malkuth-software/file_manager')
        self.assertEqual(self.product.source_dir, '.')
        self.assertTrue(self.product.at_workspace_root())
        self.assertEqual(self.product.build_tree('linux-x64'), '.build/native-linux-x64/frontend')
        self.assertEqual(self.product.build_tree('windows-x64', sanitize=True),
                         '.build/native-windows-x64/frontend-sanitize')
        self.assertEqual(self.product.native_workflow, '.github/workflows/native-builds.yml')

    def test_native_job_is_the_platform(self) -> None:
        platform: str
        for platform in PLATFORMS:
            self.assertTrue(self.product.native_job_matches(platform, platform), platform)
        # A matrix entry labelled other than its platform produces no receipts.
        self.assertFalse(self.product.native_job_matches('macos-arm64-control', 'macos-arm64'))
        self.assertFalse(self.product.native_job_matches('native (macos-15, macos-arm64, bash)', 'macos-arm64'))

    def test_environment_variables(self) -> None:
        expected: dict[str, str] = {
            'LLVM_RUNTIME': 'FILE_MANAGER_LLVM_RUNTIME', 'PLATFORM': 'FILE_MANAGER_PLATFORM',
            'COMPILER_ID': 'FILE_MANAGER_COMPILER_ID', 'RUNNER': 'FILE_MANAGER_RUNNER',
            'SANITIZER_DEBUG_INFO': 'FILE_MANAGER_SANITIZER_DEBUG_INFO',
            'PACKAGE_WINDOWS_TOOLCHAIN': 'FILE_MANAGER_PACKAGE_WINDOWS_TOOLCHAIN'}
        name: str
        for name in expected:
            self.assertEqual(self.product.variable(name), expected[name])

    def test_artifacts(self) -> None:
        kind: str
        for kind in ('validated-', 'inputs-', 'tree-', 'sanitize-'):
            self.assertEqual(self.product.artifact(kind + 'linux-x64-' + TREE),
                             'file-manager-' + kind + 'linux-x64-' + TREE)

    def test_no_cache_publication(self) -> None:
        self.assertIsNone(self.product.cache_asset_prefix)

    def test_reused_gui_forms_toolchain(self) -> None:
        self.assertEqual(self.product.toolchain_repository, 'malkuth-software/gui_forms')
        self.assertEqual(self.product.toolchain_asset_prefix, 'gui-forms-toolchain-')

    def test_linux_package_profile(self) -> None:
        native: str = ('cmake ninja-build ccache clang-22 generate-ninja pkg-config '
                       'libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxfixes-dev libxi-dev '
                       'libatk1.0-dev libatk-bridge2.0-dev '
                       'xvfb xauth xcompmgr weston xwayland xdg-utils dbus at-spi2-core python3-gi '
                       'gir1.2-atspi-2.0 patchelf')
        self.assertEqual(' '.join(self.product.linux_packages['native']), native)
        self.assertEqual(sorted(self.product.linux_packages), ['native'])

    def test_native_profile_holds_gui_forms_toolkit(self) -> None:
        # File Manager builds GUI.Forms; its profile must hold GUI.Forms' toolkit set.
        gui_forms: project.Project = project.load(PROJECT_FILE.parent / 'gui_forms.toml')
        missing: list[str] = []
        package: str
        for package in gui_forms.linux_packages['toolkit']:
            if package not in self.product.linux_packages['native']:
                missing.append(package)
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
