"""The macOS audit's C++ runtime rule: bundled for programs and libraries, the
loading compiler's own for a compiler plugin.

lipo and otool are replaced by recorded listings, so the rule is tested on
every host; the real tools run only in the macOS jobs.
"""
from __future__ import annotations
import harness_testing  # noqa: F401  (first: the fixture workspace)
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import audit_macos_minimum as audit_tool

MACH_O_ARM64: bytes = b'\xcf\xfa\xed\xfe' + bytes(28)
SYSTEM_LIBCXX: str = '/usr/lib/libc++.1.dylib'
BUNDLED_LIBCXX: str = '@rpath/libc++.1.dylib'


class FakeTools:
    """Answers lipo -archs, otool -l and otool -L from a table keyed by file name."""

    def __init__(self, dependencies: dict[str, list[str]], minimum: str = '14.0') -> None:
        self.dependencies: dict[str, list[str]] = dependencies
        self.minimum: str = minimum

    def __call__(self, command: list[str], text: bool = False) -> str:
        name: str = Path(command[-1]).name
        if command[0] == 'lipo':
            return 'arm64\n'
        if command[1] == '-l':
            return 'Load command 9\n      cmd LC_BUILD_VERSION\n    minos ' + self.minimum + '\n'
        lines: list[str] = [command[-1] + ':']
        library: str
        for library in self.dependencies[name] + ['/usr/lib/libSystem.B.dylib']:
            lines.append('\t' + library + ' (compatibility version 1.0.0, current version 1.0.0)')
        return '\n'.join(lines) + '\n'


class CxxRuntimeRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.root: Path = Path(self.directory.name) / 'sdk'
        (self.root / 'lib/gui_forms').mkdir(parents=True)
        self.compiler: Path = Path(self.directory.name) / 'clang-22'
        self.compiler.write_bytes(MACH_O_ARM64)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def image(self, relative: str) -> Path:
        path: Path = self.root / relative
        path.write_bytes(MACH_O_ARM64)
        return path

    def run_audit(self, tools: FakeTools, arguments: list[str]) -> str:
        output: io.StringIO = io.StringIO()
        with mock.patch.object(audit_tool.subprocess, 'check_output', tools), \
                contextlib.redirect_stdout(output):
            audit_tool.main(arguments)
        return output.getvalue()

    def test_a_library_must_bundle_its_cxx_runtime(self) -> None:
        self.image('lib/libgui_forms_core.dylib')
        tools: FakeTools = FakeTools({'libgui_forms_core.dylib': [SYSTEM_LIBCXX]})
        with self.assertRaisesRegex(RuntimeError, 'unbundled C\\+\\+ runtime dependency: /usr/lib/libc'):
            self.run_audit(tools, [str(self.root)])

    def test_a_bundled_runtime_passes_for_a_library(self) -> None:
        self.image('lib/libgui_forms_core.dylib')
        tools: FakeTools = FakeTools({'libgui_forms_core.dylib': [BUNDLED_LIBCXX, '@rpath/libunwind.1.dylib']})
        output: str = self.run_audit(tools, [str(self.root)])
        self.assertIn('Audited 1 Mach-O images', output)

    def test_an_unnamed_plugin_is_held_to_the_library_rule(self) -> None:
        # The CI failure this rule answers: the plugin links the compiler's
        # system libc++, and without --compiler-plugin it is a library.
        self.image('lib/gui_forms/gui_forms_localizable.so')
        tools: FakeTools = FakeTools({'gui_forms_localizable.so': [SYSTEM_LIBCXX]})
        with self.assertRaisesRegex(RuntimeError, 'gui_forms_localizable.so: unbundled'):
            self.run_audit(tools, [str(self.root)])

    def test_a_plugin_using_the_compilers_runtime_passes(self) -> None:
        self.image('lib/libgui_forms_core.dylib')
        self.image('lib/gui_forms/gui_forms_localizable.so')
        tools: FakeTools = FakeTools({'libgui_forms_core.dylib': [BUNDLED_LIBCXX],
                                      'gui_forms_localizable.so': [SYSTEM_LIBCXX],
                                      'clang-22': [SYSTEM_LIBCXX]})
        output: str = self.run_audit(tools, ['--compiler', str(self.compiler), '--compiler-plugin',
                                             'gui_forms_localizable.so', str(self.root)])
        self.assertIn('gui_forms_localizable.so: arm64, minimum 14.0 (compiler plugin)', output)
        self.assertIn('Audited 2 Mach-O images', output)

    def test_a_plugin_bundling_its_own_runtime_fails(self) -> None:
        # A second libc++ in the compiler's process: refused even though the
        # library rule would accept it.
        self.image('lib/gui_forms/gui_forms_localizable.so')
        tools: FakeTools = FakeTools({'gui_forms_localizable.so': [BUNDLED_LIBCXX],
                                      'clang-22': [SYSTEM_LIBCXX]})
        with self.assertRaisesRegex(RuntimeError, "differs from the loading compiler's"):
            self.run_audit(tools, ['--compiler', str(self.compiler), '--compiler-plugin',
                                   'gui_forms_localizable.so', str(self.root)])

    def test_a_plugin_is_still_held_to_the_minimum(self) -> None:
        self.image('lib/gui_forms/gui_forms_localizable.so')
        tools: FakeTools = FakeTools({'gui_forms_localizable.so': [SYSTEM_LIBCXX],
                                      'clang-22': [SYSTEM_LIBCXX]}, minimum='15.0')
        with self.assertRaisesRegex(RuntimeError, 'minimum exceeds macOS 14.0: 15.0'):
            self.run_audit(tools, ['--compiler', str(self.compiler), '--compiler-plugin',
                                   'gui_forms_localizable.so', str(self.root)])

    def test_a_named_plugin_may_be_absent(self) -> None:
        # The plugin is built only where the compiler's Clang package works.
        self.image('lib/libgui_forms_core.dylib')
        tools: FakeTools = FakeTools({'libgui_forms_core.dylib': [BUNDLED_LIBCXX], 'clang-22': [SYSTEM_LIBCXX]})
        output: str = self.run_audit(tools, ['--compiler', str(self.compiler), '--compiler-plugin',
                                             'gui_forms_localizable.so', str(self.root)])
        self.assertIn('Audited 1 Mach-O images', output)

    def test_a_plugin_needs_the_loading_compiler(self) -> None:
        self.image('lib/gui_forms/gui_forms_localizable.so')
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            audit_tool.main(['--compiler-plugin', 'gui_forms_localizable.so', str(self.root)])


if __name__ == '__main__':
    unittest.main()
