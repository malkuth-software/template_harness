"""A product at the workspace root (source_dir ".") with only the required project-file fields.

File Manager's checkout is its workspace root: gui_forms/, .build/ and
.ccache/ sit inside it and CCACHE_BASEDIR is the checkout itself. These
tests run the tools as separate processes for such a product, whose
project file also leaves out every optional field (no compiler caches, no
pinned revisions, no test-area rules, no [state_machines]):

- every tool imports, so a product uses the tools it needs without
  supplying fields for the ones it does not;
- a tool that needs a missing field stops with "project <name> has no
  <field>";
- the paths the tools derive land in the checkout, the workspace root.
"""
from __future__ import annotations
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import harness_testing

HARNESS: Path = harness_testing.HARNESS
# Only the required fields; source_dir "." puts the product at the workspace root.
ROOT_PROJECT: str = '''schema = 1
[project]
name = "rooted"
repository = "owner/rooted"
source_dir = "."
build_name = "rooted"
env_prefix = "ROOTED"
[ci]
artifact_prefix = "rooted-"
native_workflow = ".github/workflows/native-builds.yml"
native_job = "{platform}"
[toolchain]
repository = "malkuth-software/gui_forms"
asset_prefix = "gui-forms-toolchain-"
[tests]
name_prefixes = []
[linux_packages.toolkit]
packages = ["cmake"]
[build]
configure_variables = []
[cache_manifest]
profile = "rooted"
[cache_manifest.native_options]
tests = true
'''
# Every tool a product may run or import (the tests and helpers excepted).
TOOLS: tuple[str, ...] = (
    'audit_macos_minimum', 'bootstrap', 'build_macos_runtimes', 'cache_manifest', 'change_selection',
    'check_house_style', 'check_lifetime_rules', 'check_state_machines', 'ci_build_steps', 'ci_cache', 'ci_promote',
    'ci_reuse', 'ci_sanitizer_seed', 'ci_tree_receipt', 'compiler_identity', 'lock', 'macos_runtime_manifest',
    'native_tests', 'project', 'prune_cache_releases', 'publish_cache', 'restore_local', 'windows_toolchain')


class RootLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace: Path = Path(directory.name).resolve() / 'rooted'
        self.workspace.mkdir()
        harness_testing.git(self.workspace, 'init', '-q')
        (self.workspace / 'README.md').write_text('rooted\n', encoding='utf-8')
        harness_testing.git(self.workspace, 'add', 'README.md')
        harness_testing.git(self.workspace, 'commit', '-q', '-m', 'rooted')
        self.project_file: Path = Path(directory.name).resolve() / 'rooted.toml'
        self.project_file.write_text(ROOT_PROJECT, encoding='utf-8')

    def python(self, program: str) -> subprocess.CompletedProcess[str]:
        environment: dict[str, str] = dict(os.environ)
        environment['HARNESS_PROJECT'] = str(self.project_file)
        environment['HARNESS_WORKSPACE'] = str(self.workspace)
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [sys.executable, '-B', '-c', 'import sys; sys.path.insert(0, ' + repr(str(HARNESS)) + ')\n' + program],
            capture_output=True, text=True, check=False, env=environment, cwd=str(self.workspace))
        return completed

    def test_every_tool_imports(self) -> None:
        name: str
        for name in TOOLS:
            completed: subprocess.CompletedProcess[str] = self.python('import ' + name)
            self.assertEqual(completed.returncode, 0, name + ': ' + completed.stderr)

    def test_the_checkout_is_the_workspace_root(self) -> None:
        completed: subprocess.CompletedProcess[str] = self.python(
            'import project, ci_reuse, change_selection, prune_cache_releases\n'
            'product = project.current()\n'
            'print(product.source_root())\n'
            'print(ci_reuse.SOURCE)\n'
            'print(change_selection.SOURCE_ROOT)\n'
            'print(ci_reuse.git_field("%T"))\n'
            'print(change_selection.AREAS_FILE, prune_cache_releases.PINNED_PATH)\n')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        lines: list[str] = completed.stdout.splitlines()
        self.assertEqual(Path(lines[0]), self.workspace)
        self.assertEqual(Path(lines[1]), self.workspace)
        self.assertEqual(Path(lines[2]), self.workspace)
        tree: str = subprocess.run(['git', '-C', str(self.workspace), 'show', '-s', '--format=%T', 'HEAD'],
                                   capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(lines[3], tree)
        self.assertEqual(lines[4], 'None None')

    def test_a_tool_that_needs_a_missing_field_names_it(self) -> None:
        cases: tuple[tuple[str, str], ...] = (
            ('import project; project.current().cache_asset("linux-x64")', 'ci.cache_asset_prefix'),
            ('import change_selection; change_selection.load_rules()', 'tests.areas_file'),
            ('import prune_cache_releases; prune_cache_releases.load_pinned()', 'ci.pinned_cache_revisions'),
            ('import check_state_machines; sys.argv = ["check_state_machines.py"]; check_state_machines.main()',
             '[state_machines]'),
        )
        program: str
        field: str
        for program, field in cases:
            completed: subprocess.CompletedProcess[str] = self.python(program)
            self.assertNotEqual(completed.returncode, 0, program)
            self.assertIn('project rooted has no ' + field + ' (', completed.stderr, program)

    def test_the_native_job_template_names_the_matrix_job(self) -> None:
        completed: subprocess.CompletedProcess[str] = self.python(
            'import project\n'
            'product = project.current()\n'
            'print(product.native_job_matches("linux-x64", "linux-x64"),'
            ' product.native_job_matches("native (ubuntu-24.04, linux-x64, bash)", "linux-x64"))\n')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), 'True False')

    @unittest.skipUnless(shutil.which('git') is not None, 'needs git')
    def test_the_build_tree_and_state_sit_inside_the_checkout(self) -> None:
        completed: subprocess.CompletedProcess[str] = self.python(
            'import project\n'
            'product = project.current()\n'
            'print(product.workspace_root() / product.build_tree("linux-x64"))\n')
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(Path(completed.stdout.strip()), self.workspace / '.build' / 'native-linux-x64' / 'rooted')


if __name__ == '__main__':
    unittest.main()
