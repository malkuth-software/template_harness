"""Unit tests for build_environment.py: CI's build environment for a local workspace."""
from __future__ import annotations
import dataclasses
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import build_environment
import harness_testing
import project


class BuildEnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace: Path = Path(directory.name).resolve()
        patched: mock._patch_dict = mock.patch.dict(os.environ, {'HARNESS_WORKSPACE': str(self.workspace)})
        patched.start()
        self.addCleanup(patched.stop)
        base: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        assert base.bootstrap is not None
        table: project.BootstrapPlatform = project.BootstrapPlatform(
            prepare=(), environment=(('PLATFORM_ONLY', '{workspace}/w'),))
        self.product: project.Project = dataclasses.replace(
            base, bootstrap=dataclasses.replace(
                base.bootstrap, platforms={'windows-x64': table},
                environment=(('GUI_FORMS_STX_PACKAGE_DIR', '{workspace}/.ci/stx-package'),)))

    def manifest(self, value: object) -> None:
        (self.workspace / build_environment.CACHE_MANIFEST).write_text(json.dumps(value), encoding='utf-8')

    def test_an_old_windows_manifest_gets_the_settings_ci_had(self) -> None:
        values: dict[str, str] = build_environment.build_environment(self.product, 'windows-x64', [('EXTRA', 'x')])
        base: str = self.workspace.as_posix()
        self.assertEqual(values['CCACHE_DIR'], base + '/.ccache')
        self.assertEqual(values['CCACHE_BASEDIR'], base)
        self.assertEqual(values['CCACHE_COMPILERCHECK'], 'content')
        self.assertEqual(values['CCACHE_SLOPPINESS'], 'pch_defines,time_macros')
        self.assertEqual(values['CMAKE_CXX_COMPILER_LAUNCHER'], 'ccache')
        # The msys2 shell's LANG, the only locale measured; every other one is unset.
        self.assertEqual(values['LANG'], 'en_US.UTF-8')
        self.assertEqual(values['LC_ALL'], '')
        self.assertEqual(values['GUI_FORMS_STX_PACKAGE_DIR'], base + '/.ci/stx-package')
        self.assertEqual(values['PLATFORM_ONLY'], base + '/w')
        self.assertEqual(values['EXTRA'], 'x')

    def test_the_recorded_environment_wins_and_a_null_unsets(self) -> None:
        self.manifest({'environment': {'CCACHE_SLOPPINESS': 'pch_defines,time_macros,locale',
                                       'CCACHE_COMPILERCHECK': 'content', 'LANG': 'C.UTF-8', 'LC_ALL': None}})
        values: dict[str, str] = build_environment.build_environment(self.product, 'linux-x64', [])
        self.assertEqual(values['CCACHE_SLOPPINESS'], 'pch_defines,time_macros,locale')
        self.assertEqual(values['LANG'], 'C.UTF-8')
        self.assertEqual(values['LC_ALL'], '')
        self.assertNotIn('PLATFORM_ONLY', values)

    def test_the_one_runtime_under_the_toolchain_directory_is_named(self) -> None:
        runtime: Path = self.workspace / build_environment.RUNTIME_DIRECTORY / 'llvm-22.1.8-linux'
        runtime.mkdir(parents=True)
        (runtime / build_environment.RUNTIME_MANIFEST).write_text('{}', encoding='utf-8')
        values: dict[str, str] = build_environment.build_environment(self.product, 'linux-x64', [])
        self.assertEqual(values[self.product.variable('LLVM_RUNTIME')], runtime.as_posix())
        second: Path = runtime.parent / 'other'
        second.mkdir()
        (second / build_environment.RUNTIME_MANIFEST).write_text('{}', encoding='utf-8')
        values = build_environment.build_environment(self.product, 'linux-x64', [])
        self.assertNotIn(self.product.variable('LLVM_RUNTIME'), values)

    def test_the_record_and_the_scripts_are_written_and_applied(self) -> None:
        values: dict[str, str] = build_environment.build_environment(self.product, 'windows-x64', [])
        record: Path = build_environment.write_build_environment(self.workspace, values)
        powershell: str = (self.workspace / build_environment.BUILD_ENVIRONMENT_POWERSHELL).read_text(encoding='utf-8')
        shell: str = (self.workspace / build_environment.BUILD_ENVIRONMENT_SHELL).read_text(encoding='utf-8')
        self.assertIn("$env:LANG = 'en_US.UTF-8'", powershell)
        self.assertIn('Remove-Item -ErrorAction SilentlyContinue env:LC_ALL', powershell)
        self.assertIn('export LANG=en_US.UTF-8', shell)
        self.assertIn('unset LC_ALL', shell)
        applied: dict[str, str] = build_environment.apply_build_environment(record, {'LC_ALL': 'C', 'PATH': 'p'})
        self.assertNotIn('LC_ALL', applied)
        self.assertEqual(applied['LANG'], 'en_US.UTF-8')
        self.assertEqual(applied['PATH'], 'p')

    def test_a_bad_name_is_refused(self) -> None:
        with self.assertRaisesRegex(build_environment.BuildEnvironmentError, 'Not an environment variable name'):
            build_environment.build_environment(self.product, 'linux-x64', [('lower', 'x')])

    def test_another_compiler_build_is_named(self) -> None:
        tools: Path = self.workspace / 'bin'
        tools.mkdir()
        compiler: Path = tools / 'clang'
        compiler.write_bytes(b'not the producer')
        compiler.chmod(0o755)
        problems: list[str] = build_environment.compatibility(
            {'compiler_sha256': '0' * 64, 'compiler_version': '22.1.8'}, 'linux-x64',
            {'PATH': str(tools), 'CCACHE_COMPILERCHECK': 'content'})
        self.assertEqual(len(problems), 1)
        self.assertIn('another build of clang', problems[0])
        self.assertEqual(build_environment.compatibility({}, 'linux-x64', {'PATH': str(tools)}), [])


if __name__ == '__main__':
    unittest.main()
