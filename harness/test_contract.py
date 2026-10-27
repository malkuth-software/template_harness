"""The tools products run or import keep their names and shapes.

docs/contract.md lists the contract. A failure here means a change renamed
or reshaped one of them: restore it, or record the change under "Breaking
changes" in the harness's release notes and update the page and this test
together.
"""
from __future__ import annotations
import inspect
from pathlib import Path
import subprocess
import sys
import unittest

import harness_testing
import ci_build_steps
import macos_runtime_manifest
import project

HARNESS: Path = harness_testing.HARNESS
REPOSITORY: Path = harness_testing.REPOSITORY
CONTRACT_FILES: tuple[str, ...] = (
    'harness/project.py',
    'harness/ci_build_steps.py',
    'harness/windows_toolchain.py',
    'harness/compiler_identity.py',
    'harness/install_linux_ci_packages.sh',
    'harness/macos_runtime_manifest.py',
    'harness/build_macos_runtimes.py',
    'harness/audit_macos_minimum.py',
    'harness/ci_cache.py',
    'harness/check_house_style.py',
    'harness/check_lifetime_rules.py',
    'harness/cmake/llvm22.cmake',
)
CHANGE_RULE: str = ('products use this (docs/contract.md); a change needs a Breaking changes '
                    'entry in the harness release notes')


def parameter_names(function: object) -> list[str]:
    names: list[str] = list(inspect.signature(function).parameters)  # type: ignore[arg-type]
    return names


def command_output(script: str, *arguments: str) -> str:
    """A tool's standard output for arguments; the environment carries harness_testing's project."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, '-B', str(HARNESS / script), *arguments],
        capture_output=True, text=True, timeout=60, check=False)
    if completed.returncode != 0:
        raise AssertionError(script + ' ' + ' '.join(arguments) + ' failed: ' + completed.stderr)
    return completed.stdout


class ContractTests(unittest.TestCase):
    def test_every_contract_file_exists(self) -> None:
        relative: str
        for relative in CONTRACT_FILES:
            self.assertTrue((REPOSITORY / relative).is_file(), relative + ': ' + CHANGE_RULE)

    def test_the_project_file_interface(self) -> None:
        self.assertEqual(project.SCHEMA, 1, CHANGE_RULE)
        self.assertEqual(project.PROJECT_VARIABLE, 'HARNESS_PROJECT', CHANGE_RULE)
        self.assertEqual(project.WORKSPACE_VARIABLE, 'HARNESS_WORKSPACE', CHANGE_RULE)
        self.assertEqual(project.DEFAULT_PROJECT_NAME, 'harness.toml', CHANGE_RULE)
        self.assertEqual(parameter_names(project.load), ['path'], CHANGE_RULE)
        self.assertEqual(parameter_names(project.current), [], CHANGE_RULE)
        self.assertIn('cmake', command_output('project.py', 'linux-packages', 'toolkit').split(), CHANGE_RULE)
        self.assertEqual(command_output('project.py', 'variable', 'LLVM_RUNTIME'), 'GUI_FORMS_LLVM_RUNTIME\n',
                         CHANGE_RULE)
        command_output('project.py', 'show')

    def test_build_step_functions_keep_their_parameters(self) -> None:
        self.assertEqual(parameter_names(ci_build_steps.configure_tree), ['toolkit', 'configure'], CHANGE_RULE)
        self.assertEqual(parameter_names(ci_build_steps.sanitizer_options)[:3],
                         ['host', 'lsan_suppressions', 'debug_info'], CHANGE_RULE)
        default: object = inspect.signature(ci_build_steps.sanitizer_options).parameters['debug_info'].default
        self.assertEqual(default, 'lines', CHANGE_RULE)

    def test_macos_runtime_manifest_verify_takes_a_prefix(self) -> None:
        self.assertEqual(parameter_names(macos_runtime_manifest.verify), ['prefix'], CHANGE_RULE)

    def test_documented_commands_still_parse(self) -> None:
        self.assertIn('restore', command_output('windows_toolchain.py', '--help'), CHANGE_RULE)
        command_output('ci_cache.py', 'evict', '--help')
        command_output('ci_cache.py', 'prune', '--help')

    def test_the_linux_package_script_reads_the_project_profile(self) -> None:
        script: str = (HARNESS / 'install_linux_ci_packages.sh').read_text(encoding='utf-8')
        self.assertIn('project.py" linux-packages "$profile"', script, CHANGE_RULE)

    def test_the_toolchain_file_reads_the_prefixed_runtime_variable(self) -> None:
        toolchain: str = (HARNESS / 'cmake' / 'llvm22.cmake').read_text(encoding='utf-8')
        self.assertIn('"$ENV{HARNESS_ENV_PREFIX}_LLVM_RUNTIME"', toolchain, CHANGE_RULE)
        self.assertIn('HARNESS_LLVM_RUNTIME', toolchain, CHANGE_RULE)


if __name__ == '__main__':
    unittest.main()
