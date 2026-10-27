"""The names projects/plan-paint.toml produces, spelled out.

Plan Paint's caches, receipts, artifacts and environment variables take
these names. An edit to the project file that would rename one fails here
first; change an expected value only together with Plan Paint.
"""
from __future__ import annotations
from pathlib import Path
import unittest

import harness_testing
import project

PLATFORMS: tuple[str, ...] = ('linux-x64', 'linux-arm64', 'macos-arm64', 'windows-x64')
PLAN_PAINT_PROJECT: Path = harness_testing.REPOSITORY / 'projects' / 'plan-paint.toml'


class PlanPaintProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan_paint: project.Project = project.load(PLAN_PAINT_PROJECT)

    def test_product_and_layout(self) -> None:
        self.assertEqual(self.plan_paint.name, 'plan-paint')
        self.assertEqual(self.plan_paint.repository, 'malkuth-software/plan-paint')
        self.assertEqual(self.plan_paint.source_dir, '.')
        self.assertEqual(self.plan_paint.build_tree('linux-x64'), '.build/native-linux-x64/plan-paint')
        self.assertEqual(self.plan_paint.build_tree('windows-x64', sanitize=True),
                         '.build/native-windows-x64/plan-paint-sanitize')
        self.assertEqual(self.plan_paint.native_workflow, '.github/workflows/build.yml')
        self.assertEqual(self.plan_paint.native_job, 'application')

    def test_native_job_matches_each_platform(self) -> None:
        self.assertTrue(self.plan_paint.native_job_matches(
            'application (windows-2022, windows-x64, msys2 {0})', 'windows-x64'))
        self.assertTrue(self.plan_paint.native_job_matches(
            'application (ubuntu-24.04-arm, linux-arm64, bash)', 'linux-arm64'))
        self.assertFalse(self.plan_paint.native_job_matches(
            'application (ubuntu-24.04-arm, linux-arm64, bash)', 'linux-x64'))
        self.assertFalse(self.plan_paint.native_job_matches('prepare', 'linux-x64'))

    def test_environment_variables(self) -> None:
        expected: dict[str, str] = {
            'LLVM_RUNTIME': 'PLAN_PAINT_LLVM_RUNTIME', 'PLATFORM': 'PLAN_PAINT_PLATFORM',
            'COMPILER_ID': 'PLAN_PAINT_COMPILER_ID', 'RUNNER': 'PLAN_PAINT_RUNNER',
            'SANITIZER_DEBUG_INFO': 'PLAN_PAINT_SANITIZER_DEBUG_INFO',
            'PACKAGE_WINDOWS_TOOLCHAIN': 'PLAN_PAINT_PACKAGE_WINDOWS_TOOLCHAIN'}
        name: str
        for name in expected:
            self.assertEqual(self.plan_paint.variable(name), expected[name])
        self.assertEqual(self.plan_paint.configure_variables(), ())

    def test_workflow_artifacts(self) -> None:
        kind: str
        for kind in ('validated-', 'inputs-', 'tree-', 'sanitize-ccache-linux-x64'):
            self.assertEqual(self.plan_paint.artifact(kind), 'plan-paint-' + kind)

    def test_cache_and_toolchain_assets(self) -> None:
        platform: str
        for platform in PLATFORMS:
            self.assertEqual(self.plan_paint.cache_asset(platform), 'plan-paint-cache-' + platform + '.tar.gz')
        # GUI.Forms' published Windows toolchain, shared by name.
        self.assertEqual(self.plan_paint.toolchain_repository, 'malkuth-software/gui_forms')
        self.assertEqual(self.plan_paint.toolchain_asset_prefix + 'windows-x64.tar.zst',
                         'gui-forms-toolchain-windows-x64.tar.zst')

    def test_linux_package_profiles(self) -> None:
        self.assertEqual(tuple(self.plan_paint.linux_packages), ('paint',))
        packages: tuple[str, ...] = self.plan_paint.linux_packages['paint']
        self.assertIn('clang-22', packages)
        self.assertIn('xvfb', packages)
        self.assertEqual(len(packages), len(set(packages)))

    def test_cache_manifest_options(self) -> None:
        self.assertEqual(self.plan_paint.native_options,
                         {'build_type': 'Release', 'cxx_standard': 20, 'bundled_tiff': True})
        self.assertEqual(self.plan_paint.platform_native_options, {})

    def test_optional_tables(self) -> None:
        self.assertEqual(self.plan_paint.test_name_prefixes, ())
        self.assertIsNone(self.plan_paint.pinned_cache_revisions)
        self.assertIsNone(self.plan_paint.test_areas_file)
        self.assertFalse(self.plan_paint.has_state_machines)
        self.assertIsNone(self.plan_paint.checkpoint)
        bootstrap: project.BootstrapTable | None = self.plan_paint.bootstrap
        self.assertIsNotNone(bootstrap)
        if bootstrap is not None:
            self.assertEqual(bootstrap.notes_repository, 'malkuth-software/notes')
            self.assertEqual(bootstrap.notes_branch, 'plan-paint')
            self.assertEqual(bootstrap.projects_repository, 'malkuth-software/projects')
            self.assertEqual(bootstrap.smoke, ('{python}', 'scripts/smoke-build.py'))


if __name__ == '__main__':
    unittest.main()
