"""The names projects/games.toml produces, asserted literally.

PlaySuite (malkuth-software/games) has published no harness names yet, so
these values are the first ones: a later edit to projects/games.toml that
would rename a workflow artifact, a build tree, an environment variable or
a package profile fails here first (docs/adopting.md section 7).
"""
from __future__ import annotations
import unittest

import harness_testing
import project

PLATFORMS: tuple[str, ...] = ('linux-x64', 'linux-arm64', 'macos-arm64', 'windows-x64')
TREE: str = '0123456789abcdef0123456789abcdef01234567'
GAMES_PROJECT = harness_testing.REPOSITORY / 'projects' / 'games.toml'


class GamesProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.games: project.Project = project.load(GAMES_PROJECT)

    def test_product_and_layout(self) -> None:
        self.assertEqual(self.games.name, 'games')
        self.assertEqual(self.games.repository, 'malkuth-software/games')
        self.assertEqual(self.games.source_dir, '.')
        self.assertTrue(self.games.at_workspace_root())
        self.assertEqual(self.games.native_workflow, '.github/workflows/applications.yml')
        self.assertEqual(self.games.native_job, 'application')
        self.assertTrue(self.games.native_job_matches('application (windows-2022, windows-x64, msys2 {0})',
                                                      'windows-x64'))
        self.assertFalse(self.games.native_job_matches('application (ubuntu-24.04, linux-amd64, bash)', 'linux-x64'))

    def test_build_trees(self) -> None:
        platform: str
        for platform in PLATFORMS:
            self.assertEqual(self.games.build_tree(platform), '.build/native-' + platform + '/games')
            self.assertEqual(self.games.build_tree(platform, sanitize=True),
                             '.build/native-' + platform + '/games-sanitize')

    def test_environment_variables(self) -> None:
        expected: dict[str, str] = {
            'LLVM_RUNTIME': 'GAMES_LLVM_RUNTIME', 'PLATFORM': 'GAMES_PLATFORM',
            'COMPILER_ID': 'GAMES_COMPILER_ID', 'RUNNER': 'GAMES_RUNNER',
            'SANITIZER_DEBUG_INFO': 'GAMES_SANITIZER_DEBUG_INFO',
            'PACKAGE_WINDOWS_TOOLCHAIN': 'GAMES_PACKAGE_WINDOWS_TOOLCHAIN'}
        name: str
        for name in expected:
            self.assertEqual(self.games.variable(name), expected[name])
        self.assertEqual(self.games.configure_variables(), ())

    def test_workflow_artifacts(self) -> None:
        kind: str
        for kind in ('validated', 'inputs', 'tree', 'sanitize'):
            self.assertEqual(self.games.artifact(kind + '-linux-x64-' + TREE),
                             'games-' + kind + '-linux-x64-' + TREE)
        self.assertEqual(self.games.artifact('sanitize-ccache-linux-x64'), 'games-sanitize-ccache-linux-x64')

    def test_no_compiler_cache_or_pinned_revisions(self) -> None:
        self.assertIsNone(self.games.cache_asset_prefix)
        self.assertIsNone(self.games.pinned_cache_revisions)
        self.assertIsNone(self.games.test_areas_file)
        with self.assertRaises(project.MissingField):
            self.games.cache_asset('linux-x64')

    def test_toolchain_is_gui_forms(self) -> None:
        self.assertEqual(self.games.toolchain_repository, 'malkuth-software/gui_forms')
        self.assertEqual(self.games.toolchain_asset_prefix + 'windows-x64.tar.zst',
                         'gui-forms-toolchain-windows-x64.tar.zst')
        self.assertEqual(self.games.toolchain_asset_prefix + 'windows-x64.json',
                         'gui-forms-toolchain-windows-x64.json')

    def test_test_name_prefixes(self) -> None:
        self.assertEqual(self.games.test_name_prefixes, ())

    def test_linux_package_profiles(self) -> None:
        core: str = 'cmake ninja-build ccache clang-22'
        application: str = ('cmake ninja-build ccache clang-22 llvm-22 generate-ninja pkg-config '
                            'libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxfixes-dev libxi-dev '
                            'libatk1.0-dev libatk-bridge2.0-dev libasound2t64 xvfb xauth')
        self.assertEqual(' '.join(self.games.linux_packages['core']), core)
        self.assertEqual(' '.join(self.games.linux_packages['application']), application)
        self.assertEqual(' '.join(self.games.linux_packages['packages']), application + ' patchelf dpkg-dev')
        self.assertEqual(' '.join(self.games.linux_packages['assets']), 'ffmpeg python3-pil')
        self.assertEqual(sorted(self.games.linux_packages), ['application', 'assets', 'core', 'packages'])

    def test_cache_manifest_options(self) -> None:
        platform: str
        for platform in PLATFORMS:
            self.assertEqual(list(self.games.native_options_for(platform).items()),
                             [('build_type', 'Release'), ('cxx_standard', 20)], platform)
        self.assertEqual(self.games.cache_profile,
                         'PlaySuite application Release against the pinned GUI.Forms source')

    def test_no_state_machines_or_checkpoint(self) -> None:
        self.assertFalse(self.games.has_state_machines)
        self.assertIsNone(self.games.checkpoint)

    def test_bootstrap(self) -> None:
        bootstrap: project.BootstrapTable | None = self.games.bootstrap
        self.assertIsNotNone(bootstrap)
        if bootstrap is None:
            return
        self.assertEqual(bootstrap.notes_repository, 'malkuth-software/notes')
        self.assertEqual(bootstrap.notes_branch, 'games')
        self.assertEqual(bootstrap.projects_repository, 'malkuth-software/projects')
        self.assertEqual(tuple(bootstrap.smoke), ('{python}', 'tools/harness_smoke.py'))
        self.assertEqual(bootstrap.linux_packages, 'core')
        self.assertEqual(self.games.workspace_linux_profile(), 'core')


if __name__ == '__main__':
    unittest.main()
