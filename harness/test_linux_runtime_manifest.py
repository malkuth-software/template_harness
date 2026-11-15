"""The Linux runtime payload must reject drift, a missing archive or a shared library before reuse."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import build_linux_runtimes as build
import linux_runtime_manifest as manifest


class LinuxRuntimeManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.root: Path = Path(self.directory.name)
        self.headers: Path = self.root / 'include/c++/v1'
        self.headers.mkdir(parents=True)
        (self.headers / '__config_site').write_text('configured headers\n', encoding='utf-8')
        (self.root / 'lib').mkdir()
        name: str
        for name in manifest.STATIC_ARCHIVES:
            (self.root / 'lib' / name).write_bytes(b'!<arch>\n' + name.encode('ascii'))
        self.compiler: Path = self.root / 'clang++'
        self.compiler.write_bytes(b'compiler binary')
        self.library: str = 'musl'

    def tearDown(self) -> None:
        self.directory.cleanup()

    def output(self, command: list[str], text: bool) -> str:
        self.assertTrue(text)
        return 'clang version 22.1.8\n'

    def host_library(self) -> str:
        return self.library

    def record(self) -> None:
        value: dict[str, object] = manifest.description(self.root, 'musl')
        (self.root / 'runtime-manifest.json').write_text(json.dumps(value), encoding='utf-8')

    def test_payload_and_compiler_drift(self) -> None:
        with mock.patch('linux_runtime_manifest.subprocess.check_output', side_effect=self.output), \
                mock.patch('linux_runtime_manifest.shutil.which', return_value=str(self.compiler)), \
                mock.patch('linux_runtime_manifest.c_library', side_effect=self.host_library):
            self.record()
            manifest.verify(self.root)
            (self.headers / '__config_site').write_text('changed headers\n', encoding='utf-8')
            with self.assertRaises(RuntimeError):
                manifest.verify(self.root)
            self.record()
            (self.root / 'lib/libc++abi.a').write_bytes(b'!<arch>\nchanged archive')
            with self.assertRaises(RuntimeError):
                manifest.verify(self.root)
            self.record()
            self.compiler.write_bytes(b'different compiler with same version')
            with self.assertRaises(RuntimeError):
                manifest.verify(self.root)

    def test_other_c_library_is_refused(self) -> None:
        with mock.patch('linux_runtime_manifest.subprocess.check_output', side_effect=self.output), \
                mock.patch('linux_runtime_manifest.shutil.which', return_value=str(self.compiler)), \
                mock.patch('linux_runtime_manifest.c_library', side_effect=self.host_library):
            self.record()
            self.library = 'glibc'
            with self.assertRaises(RuntimeError):
                manifest.verify(self.root)

    def test_missing_static_archive(self) -> None:
        name: str
        for name in manifest.STATIC_ARCHIVES:
            archive: Path = self.root / 'lib' / name
            content: bytes = archive.read_bytes()
            archive.unlink()
            with self.assertRaises(RuntimeError):
                manifest.payload_hashes(self.root)
            archive.write_bytes(content)

    def test_shared_library_is_refused(self) -> None:
        (self.root / 'lib/libc++.so.1').write_bytes(b'\x7fELF')
        with self.assertRaises(RuntimeError):
            manifest.payload_hashes(self.root)

    def test_missing_configured_headers(self) -> None:
        (self.headers / '__config_site').unlink()
        with self.assertRaises(RuntimeError):
            manifest.payload_hashes(self.root)

    def test_directory_names(self) -> None:
        self.assertEqual(manifest.runtime_directory_name('musl', 'aarch64'), 'llvm-22.1.8-linux-aarch64-musl')
        self.assertEqual(manifest.runtime_directory_name('glibc', 'x86_64'), 'llvm-22.1.8-linux-x86_64-glibc')
        with self.assertRaises(RuntimeError):
            manifest.runtime_directory_name('bionic', 'aarch64')

    def test_configuration_is_static_pic_hermetic(self) -> None:
        arguments: list[str] = build.configure_arguments(Path('/src'), Path('/build'), Path('/prefix'), 'musl')
        for expected in ('-DCMAKE_POSITION_INDEPENDENT_CODE=ON', '-DLIBCXX_ENABLE_SHARED=OFF',
                         '-DLIBCXXABI_ENABLE_SHARED=OFF', '-DLIBUNWIND_ENABLE_SHARED=OFF',
                         '-DLIBCXX_HERMETIC_STATIC_LIBRARY=ON', '-DLIBCXXABI_HERMETIC_STATIC_LIBRARY=ON',
                         '-DLIBUNWIND_HIDE_SYMBOLS=ON', '-DLIBCXX_HAS_MUSL_LIBC=ON'):
            self.assertIn(expected, arguments)
        glibc: list[str] = build.configure_arguments(Path('/src'), Path('/build'), Path('/prefix'), 'glibc')
        self.assertNotIn('-DLIBCXX_HAS_MUSL_LIBC=ON', glibc)


if __name__ == '__main__':
    unittest.main()
