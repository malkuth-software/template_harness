"""The names projects/{{app}}.toml produces, spelled out (docs/adopting.md §7).

{{title}}'s caches, receipts, artifacts and environment variables take these
names. An edit to the project file that would rename one fails here first;
change an expected value only together with {{title}}. Written by
harness/new_app.py.
"""
from __future__ import annotations
from pathlib import Path
import unittest

import harness_testing
import project

PLATFORMS: tuple[str, ...] = ('linux-x64', 'linux-arm64', 'macos-arm64', 'windows-x64')
PROJECT_FILE: Path = harness_testing.REPOSITORY / 'projects' / '{{app}}.toml'


class {{App}}ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.product: project.Project = project.load(PROJECT_FILE)

    def test_product_and_layout(self) -> None:
        self.assertEqual(self.product.name, '{{app}}')
        self.assertEqual(self.product.repository, '{{repository}}')
        self.assertEqual(self.product.source_dir, '{{app}}')
        self.assertEqual(self.product.build_tree('linux-x64'), '.build/native-linux-x64/{{build_name}}')
        self.assertEqual(self.product.build_tree('windows-x64', sanitize=True),
                         '.build/native-windows-x64/{{build_name}}-sanitize')
        self.assertEqual(self.product.native_workflow, '.github/workflows/ci.yml')
        self.assertTrue(self.product.native_job_matches('native (ubuntu-24.04, linux-x64, bash) / build', 'linux-x64'))
        self.assertFalse(self.product.native_job_matches('native (ubuntu-24.04, linux-x64, bash) / build', 'linux-arm64'))

    def test_environment_variables(self) -> None:
        name: str
        for name in ('LLVM_RUNTIME', 'PLATFORM', 'COMPILER_ID', 'RUNNER', 'SANITIZER_DEBUG_INFO',
                     'PACKAGE_WINDOWS_TOOLCHAIN'):
            self.assertEqual(self.product.variable(name), '{{env_prefix}}_' + name)
        self.assertEqual(self.product.configure_variables(), ())

    def test_workflow_artifacts(self) -> None:
        kind: str
        for kind in ('validated-', 'inputs-', 'tree-', 'sanitize-ccache-linux-x64'):
            self.assertEqual(self.product.artifact(kind), '{{artifact_prefix}}' + kind)

    def test_cache_and_toolchain_assets(self) -> None:
        platform: str
        for platform in PLATFORMS:
            self.assertEqual(self.product.cache_asset(platform), '{{cache_asset_prefix}}' + platform + '.tar.gz')
        # GUI.Forms' published Windows toolchain, shared by name.
        self.assertEqual(self.product.toolchain_repository, 'malkuth-software/gui_forms')
        self.assertEqual(self.product.toolchain_asset_prefix, 'gui-forms-toolchain-')

    def test_linux_package_profiles(self) -> None:
        self.assertEqual(tuple(self.product.linux_packages), ('consumer',))
        self.assertIn('clang-22', self.product.linux_packages['consumer'])

    def test_bootstrap_and_checkpoint(self) -> None:
        bootstrap: project.BootstrapTable | None = self.product.bootstrap
        self.assertIsNotNone(bootstrap)
        if bootstrap is not None:
            self.assertEqual(bootstrap.notes_repository, '{{notes_repository}}')
            self.assertEqual(bootstrap.notes_branch, '{{app}}')
            self.assertEqual(bootstrap.projects_repository, '{{projects_repository}}')
        checkpoint: project.CheckpointTable | None = self.product.checkpoint
        self.assertIsNotNone(checkpoint)
        if checkpoint is not None:
            self.assertEqual(checkpoint.tree, '{{app}}')
        self.assertEqual(self.product.full_tree_triggers, ('harness.lock',))


if __name__ == '__main__':
    unittest.main()
