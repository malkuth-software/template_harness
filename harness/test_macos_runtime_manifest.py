"""The runtime cache must reject drift or corruption before reuse."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import macos_runtime_manifest as manifest


def symbolic_links_available() -> bool:
    """Whether this process may create a symbolic link: Windows without Developer Mode
    or the privilege refuses (WinError 1314); macOS and Linux allow it."""
    with tempfile.TemporaryDirectory() as directory:
        link: Path = Path(directory) / 'link'
        try:
            link.symlink_to('target')
        except OSError:
            return False
    return True


SYMBOLIC_LINKS: bool = symbolic_links_available()


class RuntimeManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.root: Path = Path(self.directory.name)
        self.headers: Path = self.root / 'include/c++/v1'
        self.headers.mkdir(parents=True)
        (self.headers / '__config_site').write_text('configured headers\n', encoding='utf-8')
        (self.root / 'lib').mkdir()
        name: str
        for name in ('libc++', 'libc++abi', 'libunwind'):
            (self.root / 'lib' / (name + '.1.0.dylib')).write_bytes(name.encode('ascii'))
        self.sdk: str = "15.5"
        self.compiler: Path = self.root / 'clang++'
        self.compiler.write_bytes(b'compiler binary')

    def tearDown(self) -> None:
        self.directory.cleanup()

    def output(self, command: list[str], text: bool) -> str:
        self.assertTrue(text)
        if command[0] == 'xcrun':
            return self.sdk + '\n'
        return 'clang version 22.1.8\n'

    def record(self) -> None:
        value: dict[str, object] = manifest.description(self.root)
        (self.root / 'runtime-manifest.json').write_text(json.dumps(value), encoding='utf-8')

    def test_payload_and_compiler_drift(self) -> None:
        with mock.patch('macos_runtime_manifest.subprocess.check_output', side_effect=self.output):
            with mock.patch('macos_runtime_manifest.shutil.which', return_value=str(self.compiler)):
                self.record()
                manifest.verify(self.root)
                (self.headers / '__config_site').write_text('changed headers\n', encoding='utf-8')
                with self.assertRaises(RuntimeError):
                    manifest.verify(self.root)
                self.record()
                library: Path = self.root / 'lib/libc++.1.0.dylib'
                library.write_bytes(b'changed library')
                with self.assertRaises(RuntimeError):
                    manifest.verify(self.root)
                self.record()
                self.compiler.write_bytes(b'different compiler with same version')
                with self.assertRaises(RuntimeError):
                    manifest.verify(self.root)

    @unittest.skipUnless(SYMBOLIC_LINKS, 'this account may not create symbolic links; the macOS runtime has them')
    def test_sdk_and_symlink_drift(self) -> None:
        alias: Path = self.root / 'lib/libc++.1.dylib'
        alias.symlink_to('libc++.1.0.dylib')
        with mock.patch('macos_runtime_manifest.subprocess.check_output', side_effect=self.output):
            with mock.patch('macos_runtime_manifest.shutil.which', return_value=str(self.compiler)):
                self.record()
                self.sdk = '26.0'
                with self.assertRaises(RuntimeError):
                    manifest.verify(self.root)
                self.sdk = '15.5'
                alias.unlink()
                alias.symlink_to('missing-library.dylib')
                with self.assertRaises(RuntimeError):
                    manifest.verify(self.root)

    def test_missing_configured_headers(self) -> None:
        (self.headers / '__config_site').unlink()
        with self.assertRaises(RuntimeError):
            manifest.payload_hashes(self.root)

    def test_missing_library(self) -> None:
        (self.root / 'lib/libc++.1.0.dylib').unlink()
        with self.assertRaises(RuntimeError):
            manifest.payload_hashes(self.root)


if __name__ == '__main__':
    unittest.main()
