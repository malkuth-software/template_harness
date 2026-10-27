"""Unit tests for project.py: loading, validation and the package profiles."""
from __future__ import annotations
import contextlib
import io
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Any
import unittest
from unittest import mock

import harness_testing
import project

MINIMAL: str = '''schema = 1
[project]
name = "example"
repository = "owner/example"
source_dir = "example"
build_name = "example"
env_prefix = "EXAMPLE"
[ci]
artifact_prefix = "example-"
cache_asset_prefix = "example-cache-"
native_workflow = ".github/workflows/native.yml"
native_job = "native"
pinned_cache_revisions = "packaging/pinned.json"
[toolchain]
repository = ""
asset_prefix = "example-toolchain-"
[tests]
areas_file = "tools/test_areas.json"
name_prefixes = []
[linux_packages.base]
packages = ["cmake"]
[linux_packages.more]
extends = "base"
packages = ["patchelf"]
[state_machines]
first_party_dirs = ["src"]
vocabulary_headers = []
areas = [["src/", "core"]]
full_tree_triggers = []
[build]
configure_variables = ["TOOLS"]
[cache_manifest]
profile = "example"
[cache_manifest.native_options]
tests = true
skia = true
harfbuzz = true
[cache_manifest.platform_options.windows-x64]
skia = false
'''


def parsed(text: str) -> project.Project:
    document: dict[str, Any] = tomllib.loads(text)
    result: project.Project = project.parse(document, Path('example.toml'))
    return result


class ProjectFileTests(unittest.TestCase):
    def test_the_reference_project_loads(self) -> None:
        loaded: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        self.assertEqual(loaded.name, 'gui_forms')
        self.assertEqual(loaded.linux_packages['programs'][-3:], ('xdotool', 'patchelf', 'dpkg-dev'))

    def test_a_profile_extends_another_in_order(self) -> None:
        loaded: project.Project = parsed(MINIMAL)
        self.assertEqual(loaded.linux_packages['more'], ('cmake', 'patchelf'))

    def test_validation_names_the_bad_field(self) -> None:
        with self.assertRaisesRegex(ValueError, 'ci.artifact_prefix'):
            parsed(MINIMAL.replace('artifact_prefix = "example-"', 'artifact_prefix = 3'))
        with self.assertRaisesRegex(ValueError, 'schema'):
            parsed(MINIMAL.replace('schema = 1', 'schema = 2'))
        with self.assertRaisesRegex(ValueError, 'source_dir'):
            parsed(MINIMAL.replace('source_dir = "example"', 'source_dir = "example/"'))
        with self.assertRaisesRegex(ValueError, 'extends'):
            parsed(MINIMAL.replace('extends = "base"', 'extends = "missing"'))
        with self.assertRaisesRegex(ValueError, 'areas'):
            parsed(MINIMAL.replace('areas = [["src/", "core"]]', 'areas = [["src/"]]'))
        with self.assertRaisesRegex(ValueError, 'platform_options.windows-x64.gallery'):
            parsed(MINIMAL.replace('skia = false', 'gallery = false'))
        with self.assertRaisesRegex(ValueError, 'native_options.tests'):
            parsed(MINIMAL.replace('tests = true', 'tests = [true]'))

    def test_derived_names(self) -> None:
        loaded: project.Project = parsed(MINIMAL)
        self.assertEqual(loaded.variable('PLATFORM'), 'EXAMPLE_PLATFORM')
        self.assertEqual(loaded.configure_variables(), ('EXAMPLE_TOOLS',))
        self.assertEqual(loaded.artifact('tree-docs-abc'), 'example-tree-docs-abc')
        self.assertEqual(loaded.cache_asset('linux-x64'), 'example-cache-linux-x64.tar.gz')
        self.assertEqual(loaded.build_tree('linux-x64', sanitize=True), '.build/native-linux-x64/example-sanitize')

    def test_a_platform_override_keeps_the_option_in_its_place(self) -> None:
        loaded: project.Project = parsed(MINIMAL)
        windows: dict[str, str | int | bool] = loaded.native_options_for('windows-x64')
        self.assertEqual(list(windows.items()), [('tests', True), ('skia', False), ('harfbuzz', True)])
        self.assertEqual(loaded.native_options_for('linux-x64')['skia'], True)
        self.assertEqual(loaded.native_options['skia'], True)

    def test_the_workspace_and_default_project_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            (root / 'harness.toml').write_text(MINIMAL, encoding='utf-8')
            environment: dict[str, str] = {'HARNESS_WORKSPACE': str(root)}
            with mock.patch.dict(os.environ, environment):
                os.environ.pop('HARNESS_PROJECT', None)
                self.assertEqual(project.project_path(), root / 'harness.toml')
                self.assertEqual(parsed(MINIMAL).source_root(), root.resolve() / 'example')
            with mock.patch.dict(os.environ, {'HARNESS_WORKSPACE': str(root / 'empty')}):
                os.environ.pop('HARNESS_PROJECT', None)
                with self.assertRaisesRegex(RuntimeError, 'No project file'):
                    project.project_path()

    def test_the_package_command_prints_a_profile(self) -> None:
        printed: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(printed):
            status: int = project.main(['linux-packages', 'programs'])
        self.assertEqual(status, 0)
        self.assertTrue(printed.getvalue().startswith('cmake ninja-build ccache clang-22 '))
        self.assertTrue(printed.getvalue().endswith(' xdotool patchelf dpkg-dev\n'))

    def test_the_variable_command_prints_a_prefixed_name(self) -> None:
        printed: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(printed):
            status: int = project.main(['variable', 'LLVM_RUNTIME'])
        self.assertEqual(status, 0)
        self.assertEqual(printed.getvalue(), 'GUI_FORMS_LLVM_RUNTIME\n')


if __name__ == '__main__':
    unittest.main()
