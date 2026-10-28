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
        with self.assertRaisesRegex(ValueError, 'reviewed_shapes'):
            parsed(MINIMAL.replace('full_tree_triggers = []', 'full_tree_triggers = []\nreviewed_shapes = [["a.hpp", "R"]]'))

    def test_reviewed_shapes_are_optional_triples(self) -> None:
        self.assertEqual(parsed(MINIMAL).reviewed_shapes, ())
        loaded: project.Project = parsed(MINIMAL.replace(
            'full_tree_triggers = []', 'full_tree_triggers = []\nreviewed_shapes = [["src/a.hpp", "Record", "why"]]'))
        self.assertEqual(loaded.reviewed_shapes, (project.ReviewedShape('src/a.hpp', 'Record', 'why'),))

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

    def test_the_optional_checkpoint_and_bootstrap_tables(self) -> None:
        loaded: project.Project = parsed(MINIMAL)
        self.assertIsNone(loaded.checkpoint)
        self.assertIsNone(loaded.bootstrap)
        tables: str = (MINIMAL + '[checkpoint]\ntree = "tree"\nverify = ["make"]\n'
                       '[bootstrap]\nsmoke = ["{python}", "smoke.py"]\n')
        loaded = parsed(tables)
        self.assertEqual((loaded.checkpoint.tree, loaded.checkpoint.verify, loaded.checkpoint.merge_method),
                         ('tree', ('make',), 'merge'))
        self.assertEqual((loaded.bootstrap.notes_repository, loaded.bootstrap.notes_branch,
                          loaded.bootstrap.projects_repository, loaded.bootstrap.smoke),
                         ('owner/notes', 'example', 'owner/projects', ('{python}', 'smoke.py')))
        with self.assertRaisesRegex(ValueError, 'merge_method'):
            parsed(tables.replace('verify = ["make"]', 'verify = ["make"]\nmerge_method = "fast"'))
        with self.assertRaisesRegex(ValueError, 'checkpoint.verify'):
            parsed(tables.replace('verify = ["make"]', 'verify = []'))
        self.assertIsNone(loaded.bootstrap.linux_packages)
        named: project.Project = parsed(tables + 'linux_packages = "more"\n')
        self.assertEqual(named.bootstrap.linux_packages, 'more')
        with self.assertRaisesRegex(ValueError, 'bootstrap.linux_packages names no'):
            parsed(tables + 'linux_packages = "absent"\n')

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

    def test_a_product_at_the_workspace_root(self) -> None:
        at_root: project.Project = parsed(MINIMAL.replace('source_dir = "example"', 'source_dir = "."'))
        self.assertTrue(at_root.at_workspace_root())
        self.assertFalse(parsed(MINIMAL).at_workspace_root())
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            with mock.patch.dict(os.environ, {'HARNESS_WORKSPACE': str(root)}):
                self.assertEqual(at_root.source_root(), root.resolve())
                self.assertEqual(at_root.source_root() / 'harness.lock', root.resolve() / 'harness.lock')
        self.assertEqual(at_root.build_tree('linux-x64'), '.build/native-linux-x64/example')
        bad: str
        for bad in ('./example', 'example/./x', '../example', 'example/../x', '/example', 'a//b'):
            with self.assertRaisesRegex(ValueError, 'source_dir', msg=bad):
                parsed(MINIMAL.replace('source_dir = "example"', 'source_dir = "' + bad + '"'))
        self.assertEqual(parsed(MINIMAL.replace('source_dir = "example"', 'source_dir = "a/b"')).source_dir, 'a/b')

    def test_the_optional_fields_fail_only_when_a_tool_needs_them(self) -> None:
        text: str = MINIMAL
        line: str
        for line in ('cache_asset_prefix = "example-cache-"\n', 'pinned_cache_revisions = "packaging/pinned.json"\n',
                     'areas_file = "tools/test_areas.json"\n'):
            self.assertEqual(text.count(line), 1, line)
            text = text.replace(line, '')
        start: int = text.index('[state_machines]')
        end: int = text.index('[build]')
        text = text[:start] + text[end:]
        bare: project.Project = parsed(text)
        self.assertIsNone(bare.cache_asset_prefix)
        self.assertIsNone(bare.pinned_cache_revisions)
        self.assertIsNone(bare.test_areas_file)
        self.assertFalse(bare.has_state_machines)
        self.assertEqual((bare.first_party_dirs, bare.vocabulary_headers, bare.state_machine_areas,
                          bare.full_tree_triggers), ((), (), (), ()))
        self.assertEqual(bare.loop_branch_decisions, '')
        with self.assertRaisesRegex(project.MissingField, '^project example has no ci.cache_asset_prefix '):
            bare.cache_asset('linux-x64')
        with self.assertRaisesRegex(project.MissingField, '^project example has no ci.pinned_cache_revisions '):
            bare.require_pinned_cache_revisions()
        with self.assertRaisesRegex(project.MissingField, '^project example has no tests.areas_file '):
            bare.require_test_areas_file()
        with self.assertRaisesRegex(project.MissingField, r'^project example has no \[state_machines\] '):
            bare.require_state_machines()
        full: project.Project = parsed(MINIMAL)
        self.assertTrue(full.has_state_machines)
        full.require_state_machines()
        self.assertEqual(full.require_test_areas_file(), 'tools/test_areas.json')
        with self.assertRaisesRegex(ValueError, 'ci.cache_asset_prefix'):
            parsed(MINIMAL.replace('cache_asset_prefix = "example-cache-"', 'cache_asset_prefix = ""'))

    def test_the_loop_branch_decisions_path(self) -> None:
        loaded: project.Project = parsed(MINIMAL + '[loop_branches]\ndecisions = "tools/loop_branches.json"\n')
        self.assertEqual(loaded.loop_branch_decisions, 'tools/loop_branches.json')
        with self.assertRaisesRegex(ValueError, 'loop_branches.decisions'):
            parsed(MINIMAL + '[loop_branches]\ndecisions = "../outside.json"\n')

    def test_the_const_restrict_decisions_path(self) -> None:
        self.assertEqual(parsed(MINIMAL).const_restrict_decisions, '')
        loaded: project.Project = parsed(MINIMAL + '[const_restrict]\ndecisions = "tools/const_restrict.json"\n')
        self.assertEqual(loaded.const_restrict_decisions, 'tools/const_restrict.json')
        with self.assertRaisesRegex(ValueError, 'const_restrict.decisions'):
            parsed(MINIMAL + '[const_restrict]\ndecisions = "/absolute.json"\n')

    def test_the_native_job_as_a_prefix_or_a_template(self) -> None:
        prefix: project.Project = parsed(MINIMAL)
        self.assertTrue(prefix.native_job_matches('native (ubuntu-24.04, linux-x64, bash) / build', 'linux-x64'))
        self.assertFalse(prefix.native_job_matches('native (ubuntu-24.04-arm, linux-arm64, bash) / build',
                                                   'linux-x64'))
        self.assertFalse(prefix.native_job_matches('linux-x64', 'linux-x64'))
        # A matrix job named by its platform (${{ matrix.label || matrix.platform }}).
        matrix: project.Project = parsed(MINIMAL.replace('native_job = "native"', 'native_job = "{platform}"'))
        self.assertTrue(matrix.native_job_matches('linux-x64', 'linux-x64'))
        self.assertFalse(matrix.native_job_matches('linux-arm64', 'linux-x64'))
        self.assertFalse(matrix.native_job_matches('linux-x64-sanitize', 'linux-x64'))
        self.assertFalse(matrix.native_job_matches('native (ubuntu-24.04, linux-x64, bash) / build', 'linux-x64'))
        # The same matrix job inside a called workflow: <caller job> / <job>.
        called: project.Project = parsed(MINIMAL.replace('native_job = "native"',
                                                         'native_job = "builds / {platform}"'))
        self.assertTrue(called.native_job_matches('builds / windows-x64', 'windows-x64'))
        self.assertFalse(called.native_job_matches('windows-x64', 'windows-x64'))

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
