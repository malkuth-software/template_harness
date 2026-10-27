"""The names projects/swiftedit.toml produces, asserted literally.

SwiftEdit runs the harness with its own project file for its own artifacts
and build tree, and reads GUI.Forms' published Windows toolchain. A failing
assertion here means an edit would rename one of SwiftEdit's artifacts,
build trees, variables or package profiles, or stop it finding GUI.Forms'
toolchain. Change an expected value only together with SwiftEdit.
"""
from __future__ import annotations
import unittest

import harness_testing
import project

PLATFORMS: tuple[str, ...] = ('linux-x64', 'linux-arm64', 'macos-arm64', 'windows-x64')
TREE: str = '0123456789abcdef0123456789abcdef01234567'
PROJECT_FILE = harness_testing.REPOSITORY / 'projects' / 'swiftedit.toml'


class SwiftEditProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.swiftedit: project.Project = project.load(PROJECT_FILE)

    def test_product_and_layout(self) -> None:
        self.assertEqual(self.swiftedit.name, 'swiftedit')
        self.assertEqual(self.swiftedit.repository, 'malkuth-software/swiftedit')
        self.assertEqual(self.swiftedit.source_dir, '.')
        self.assertTrue(self.swiftedit.at_workspace_root())
        self.assertEqual(self.swiftedit.build_tree('linux-x64'), '.build/native-linux-x64/swiftedit')
        self.assertEqual(self.swiftedit.build_tree('windows-x64', sanitize=True),
                         '.build/native-windows-x64/swiftedit-sanitize')

    def test_native_job(self) -> None:
        self.assertEqual(self.swiftedit.native_workflow, '.github/workflows/native-builds.yml')
        self.assertTrue(self.swiftedit.native_job_matches('native (windows-2022, windows-x64, msys2 {0})',
                                                          'windows-x64'))
        self.assertTrue(self.swiftedit.native_job_matches('native (macos-15, macos-arm64, bash)', 'macos-arm64'))
        self.assertTrue(self.swiftedit.native_job_matches('native (ubuntu-24.04, linux-x64, bash)', 'linux-x64'))
        self.assertFalse(self.swiftedit.native_job_matches('native (ubuntu-24.04, linux-x64, bash)', 'macos-arm64'))
        self.assertFalse(self.swiftedit.native_job_matches('prepare', 'linux-x64'))

    def test_environment_variables(self) -> None:
        names: tuple[str, ...] = ('LLVM_RUNTIME', 'PLATFORM', 'COMPILER_ID', 'RUNNER', 'SANITIZER_DEBUG_INFO',
                                  'PACKAGE_WINDOWS_TOOLCHAIN')
        name: str
        for name in names:
            self.assertEqual(self.swiftedit.variable(name), 'SWIFTEDIT_' + name)
        self.assertEqual(self.swiftedit.configure_variables(), ())

    def test_workflow_artifacts(self) -> None:
        self.assertEqual(self.swiftedit.artifact('validated-linux-x64-' + TREE + '-'),
                         'swiftedit-validated-linux-x64-' + TREE + '-')
        self.assertEqual(self.swiftedit.artifact('inputs-windows-x64-'), 'swiftedit-inputs-windows-x64-')
        self.assertEqual(self.swiftedit.artifact('tree-docs-' + TREE), 'swiftedit-tree-docs-' + TREE)
        self.assertEqual(self.swiftedit.artifact('sanitize-ccache-linux-x64'), 'swiftedit-sanitize-ccache-linux-x64')

    def test_no_compiler_caches(self) -> None:
        # SwiftEdit publishes none; a tool that would read one stops by name.
        self.assertIsNone(self.swiftedit.cache_asset_prefix)
        platform: str
        for platform in PLATFORMS:
            with self.assertRaisesRegex(project.MissingField, 'project swiftedit has no ci.cache_asset_prefix'):
                self.swiftedit.cache_asset(platform)

    def test_toolchain(self) -> None:
        # GUI.Forms' published toolchain, named exactly as GUI.Forms names it.
        self.assertEqual(self.swiftedit.toolchain_repository, 'malkuth-software/gui_forms')
        self.assertEqual(self.swiftedit.toolchain_asset_prefix + 'windows-x64.tar.zst',
                         'gui-forms-toolchain-windows-x64.tar.zst')
        self.assertEqual(self.swiftedit.toolchain_asset_prefix + 'windows-x64.json',
                         'gui-forms-toolchain-windows-x64.json')

    def test_linux_package_profiles(self) -> None:
        app: str = ('cmake ninja-build ccache clang-22 generate-ninja pkg-config '
                    'libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxfixes-dev libxi-dev '
                    'libatk1.0-dev libatk-bridge2.0-dev xvfb xauth patchelf dpkg-dev')
        self.assertEqual(sorted(self.swiftedit.linux_packages), ['app'])
        self.assertEqual(' '.join(self.swiftedit.linux_packages['app']), app)

    def test_cache_manifest_options(self) -> None:
        platform: str
        for platform in PLATFORMS:
            self.assertEqual(list(self.swiftedit.native_options_for(platform).items()),
                             [('build_type', 'Release'), ('cxx_standard', 20)], platform)
        self.assertEqual(self.swiftedit.cache_profile,
                         'SwiftEdit against pinned GUI.Forms, picker and stx_rtf2md sources')

    def test_optional_tables(self) -> None:
        self.assertFalse(self.swiftedit.has_state_machines)
        self.assertEqual(self.swiftedit.test_name_prefixes, ())
        self.assertIsNone(self.swiftedit.checkpoint)
        bootstrap: project.BootstrapTable | None = self.swiftedit.bootstrap
        self.assertIsNotNone(bootstrap)
        if bootstrap is not None:
            self.assertEqual(bootstrap.notes_repository, 'malkuth-software/notes')
            self.assertEqual(bootstrap.notes_branch, 'swiftedit')
            self.assertEqual(bootstrap.projects_repository, 'malkuth-software/projects')
            self.assertEqual(bootstrap.smoke, ('{python}', '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-p',
                                               'test_publication.py'))


if __name__ == '__main__':
    unittest.main()
