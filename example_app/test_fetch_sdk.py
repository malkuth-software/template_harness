#!/usr/bin/env python3
"""fetch_sdk.py without the network: downloads and release listings are replaced by local files, and the
source SDK is a small archive whose build entry point writes a package configuration."""
from __future__ import annotations
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
from typing import Any
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_build  # noqa: E402
import fetch_sdk  # noqa: E402

LOCK_TEXT: str = '''schema = 2

[release]
repository = "malkuth-software/gui_forms"
tag = "v0.2.7"
source_asset = "gui-forms-sdk-{version}-source.tar.gz"
'''
ARCHIVE_NAME: str = 'gui-forms-sdk-0.2.7-source.tar.gz'
# The fake build entry point: records each run in FAKE_BUILD_LOG, fails when
# FAKE_BUILD_FAIL is set, and otherwise installs a package configuration.
BUILD_SCRIPT: bytes = b'''import argparse, os, pathlib, sys
parser = argparse.ArgumentParser()
for name in ('--target', '--prefix', '--jobs', '--work'):
    parser.add_argument(name, required=True)
args = parser.parse_args()
with open(os.environ['FAKE_BUILD_LOG'], 'a', encoding='utf-8') as log:
    log.write(args.target + '\\n')
if os.environ.get('FAKE_BUILD_FAIL'):
    sys.exit(3)
package = pathlib.Path(args.prefix) / 'lib' / 'cmake' / 'GUIForms'
package.mkdir(parents=True)
(package / 'GUIFormsConfig.cmake').write_text('# config\\n')
'''


def source_tarball(path: Path, manifest: dict[str, Any]) -> str:
    """Write a minimal source SDK (one top directory, the manifest, the build entry) and return its digest."""
    members: list[tuple[str, bytes]] = [
        ('gui-forms-sdk-0.2.7/SDK_MANIFEST.json', json.dumps(manifest).encode('utf-8')),
        ('gui-forms-sdk-0.2.7/tools/build_sdk.py', BUILD_SCRIPT),
        ('gui-forms-sdk-0.2.7/CMakeLists.txt', b'# tree\n')]
    with tarfile.open(path, 'w:gz') as bundle:
        name: str
        data: bytes
        for name, data in members:
            info: tarfile.TarInfo = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    result: str = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def locked(digest: str) -> fetch_sdk.Lock:
    result: fetch_sdk.Lock = fetch_sdk.Lock('malkuth-software/gui_forms', 'v0.2.7',
                                            'gui-forms-sdk-{version}-source.tar.gz',
                                            fetch_sdk.Source(ARCHIVE_NAME, digest))
    return result


class CopyingDownload:
    """Stands in for fetch_sdk.download: copies one local archive and counts the calls."""

    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls = 0

    def __call__(self, url: str, target: Path) -> None:
        self.calls += 1
        target.write_bytes(self.source.read_bytes())


class Workspace:
    """A temporary directory with an archive, a cache and the build log, and the environment the fetch sees."""

    def __init__(self, root: Path, manifest: dict[str, Any]) -> None:
        self.root = root
        self.archive = root / 'source.tar.gz'
        self.digest = source_tarball(self.archive, manifest)
        self.cache = root / 'cache'
        self.log = root / 'build.log'
        self.download = CopyingDownload(self.archive)
        self.environment: dict[str, str] = {'FAKE_BUILD_LOG': str(self.log), 'FAKE_BUILD_FAIL': '',
                                            'GITHUB_OUTPUT': '', fetch_sdk.IDENTITY_VARIABLE: 'toolchain-a'}

    def fetch(self, allow_network_inputs: bool = False, target: str = '') -> list[str]:
        chosen: str = target or fetch_sdk.running_platform()
        with mock.patch.object(fetch_sdk, 'download', self.download), \
                mock.patch.dict('os.environ', self.environment):
            result: list[str] = fetch_sdk.fetch_command(locked(self.digest), chosen, self.cache, 2,
                                                        allow_network_inputs)
        return result

    def builds(self) -> int:
        result: int = len(self.log.read_text(encoding='utf-8').splitlines()) if self.log.exists() else 0
        return result


class LockTests(unittest.TestCase):
    def test_the_lock_is_validated_whole(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / 'gui_forms.lock'
            path.write_text(LOCK_TEXT, encoding='utf-8')
            self.assertIsNone(fetch_sdk.read_lock(path).source)
            path.write_text(LOCK_TEXT + '\n[source]\nname = "' + ARCHIVE_NAME + '"\nsha256 = "abc"\n',
                            encoding='utf-8')
            with self.assertRaises(fetch_sdk.FetchError):
                fetch_sdk.read_lock(path)
            path.write_text(LOCK_TEXT + '\n[source]\nname = "../x.tar.gz"\nsha256 = "' + '0' * 64 + '"\n',
                            encoding='utf-8')
            with self.assertRaises(fetch_sdk.FetchError):
                fetch_sdk.read_lock(path)
            path.write_text(LOCK_TEXT.replace('schema = 2', 'schema = 1'), encoding='utf-8')
            with self.assertRaises(fetch_sdk.FetchError):
                fetch_sdk.read_lock(path)

    def test_the_committed_lock_reads(self) -> None:
        lock: fetch_sdk.Lock = fetch_sdk.read_lock(fetch_sdk.LOCK_PATH)
        self.assertEqual(lock.repository, 'malkuth-software/gui_forms')
        self.assertTrue(lock.tag.startswith('v1.0.'))

    def test_member_paths_stay_inside(self) -> None:
        self.assertTrue(fetch_sdk.safe_member('sdk/lib/x.a'))
        self.assertFalse(fetch_sdk.safe_member('../x'))
        self.assertFalse(fetch_sdk.safe_member('/etc/x'))
        self.assertFalse(fetch_sdk.safe_member('C:/x'))
        self.assertFalse(fetch_sdk.safe_member('sdk\\..\\..\\x'))

    def test_a_cache_inside_the_repository_is_refused(self) -> None:
        with self.assertRaises(fetch_sdk.FetchError):
            fetch_sdk.checked_cache(fetch_sdk.HERE / 'cache')


class FetchTests(unittest.TestCase):
    def test_fetch_verifies_unpacks_builds_and_reuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work: Workspace = Workspace(Path(directory), {'build': 'tools/build_sdk.py'})
            target: str = fetch_sdk.running_platform()
            install: Path = work.cache / ('gui-forms-0.2.7-' + target)
            expected: list[str] = ['prefix=' + install.as_posix()]
            self.assertEqual(work.fetch(), expected)
            self.assertEqual(work.fetch(), expected)
            self.assertEqual((work.download.calls, work.builds()), (1, 1))
            with mock.patch.dict('os.environ', {'GITHUB_OUTPUT': ''}):
                printed: list[str] = fetch_sdk.prefix_command(locked(work.digest), target, work.cache)
            self.assertEqual(printed, expected)
            # Another toolchain rebuilds from the unpacked source, without a download.
            work.environment[fetch_sdk.IDENTITY_VARIABLE] = 'toolchain-b'
            self.assertEqual(work.fetch(), expected)
            self.assertEqual((work.download.calls, work.builds()), (1, 2))
            record: dict[str, Any] = json.loads((install / fetch_sdk.BUILD_RECORD).read_text(encoding='utf-8'))
            self.assertEqual(record, {'archive_sha256': work.digest, 'target': target, 'toolchain': 'toolchain-b'})

    def test_another_target_is_refused_before_any_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work: Workspace = Workspace(Path(directory), {})
            host: str = fetch_sdk.running_platform()
            others: list[str] = [name for name in fetch_sdk.PLATFORMS if name != host]
            with self.assertRaisesRegex(fetch_sdk.FetchError, 'not supported yet'):
                work.fetch(target=others[0])
            self.assertEqual(work.download.calls, 0)

    def test_a_wrong_digest_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work: Workspace = Workspace(Path(directory), {})
            work.digest = '0' * 64
            with self.assertRaises(fetch_sdk.FetchError):
                work.fetch()
            self.assertEqual(list((work.cache / 'gui-forms-sdk' / 'v0.2.7').iterdir()), [])
            self.assertEqual(work.builds(), 0)

    def test_network_inputs_stop_the_build_unless_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host: str = fetch_sdk.running_platform()
            others: list[str] = [name for name in fetch_sdk.PLATFORMS if name != host]
            manifest: dict[str, Any] = {'network_inputs': [
                {'name': 'skia', 'platforms': [host], 'revision': 'abc123', 'note': 'large'},
                {'name': 'elsewhere', 'platforms': others, 'sha256': '0' * 64}]}
            work: Workspace = Workspace(Path(directory), manifest)
            with self.assertRaisesRegex(fetch_sdk.FetchError, r'downloads: skia, revision abc123\. Rerun'):
                work.fetch()
            self.assertEqual(work.builds(), 0)
            work.fetch(allow_network_inputs=True)
            self.assertEqual((work.download.calls, work.builds()), (1, 1))

    def test_network_inputs_of_other_targets_do_not_stop_the_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host: str = fetch_sdk.running_platform()
            others: list[str] = [name for name in fetch_sdk.PLATFORMS if name != host]
            manifest: dict[str, Any] = {'network_inputs': [{'name': 'skia', 'platforms': others}]}
            work: Workspace = Workspace(Path(directory), manifest)
            work.fetch()
            self.assertEqual(work.builds(), 1)

    def test_a_failed_build_leaves_no_record_and_builds_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work: Workspace = Workspace(Path(directory), {})
            work.environment['FAKE_BUILD_FAIL'] = '1'
            with self.assertRaisesRegex(fetch_sdk.FetchError, 'build failed'):
                work.fetch()
            install: Path = work.cache / ('gui-forms-0.2.7-' + fetch_sdk.running_platform())
            self.assertIsNone(fetch_sdk.recorded(install))
            work.environment['FAKE_BUILD_FAIL'] = ''
            work.fetch()
            self.assertEqual((work.download.calls, work.builds()), (1, 2))
            self.assertIsNotNone(fetch_sdk.recorded(install))

    def test_a_missing_build_entry_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work: Workspace = Workspace(Path(directory), {'build': 'tools/absent.py'})
            with self.assertRaisesRegex(fetch_sdk.FetchError, 'absent.py'):
                work.fetch()

    def test_a_traversing_member_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            archive: Path = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('gui-forms-sdk-0.2.7/SDK_MANIFEST.json', '{}')
                bundle.writestr('../escaped.txt', 'no\n')
            into: Path = root / 'into'
            into.mkdir()
            with self.assertRaises(fetch_sdk.FetchError):
                fetch_sdk.extract(archive, 'bad.zip', into)
            self.assertFalse((root / 'escaped.txt').exists())


# A stand-in for the SDK's tools/own_runtime.py build: prints a progress line and the three settings
# under --out, or fails when FAKE_OWN_RUNTIME_FAIL is set.
OWN_RUNTIME_SCRIPT: str = '''import os, pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])
if os.environ.get('FAKE_OWN_RUNTIME_FAIL'):
    sys.exit(4)
print('own runtime: built')
for name, leaf in (('GUI_FORMS_OWN_RUNTIME_SOURCE', 'stxlib'), ('GUI_FORMS_OWN_RUNTIME_PREFIX', 'own-runtime'),
                   ('GUI_FORMS_LIBCXX_PREFIX', 'libcxx')):
    (out / leaf).mkdir(parents=True, exist_ok=True)
    print(name + '=' + (out / leaf).as_posix())
'''


class RuntimeTests(unittest.TestCase):
    """The SDK build's runtime: the LLVM runtime prefix on macOS and Linux, STXlib's own runtime on Windows."""

    def test_macos_and_linux_get_the_harness_runtime_and_windows_does_not(self) -> None:
        harness: dict[str, str] = {'HARNESS_ENV_PREFIX': 'EXAMPLE_APP', 'EXAMPLE_APP_LLVM_RUNTIME': '/rt'}
        target: str
        for target in ('macos-arm64', 'linux-x64', 'linux-arm64'):
            self.assertEqual(fetch_sdk.runtime_environment(target, harness)['GUI_FORMS_LLVM_RUNTIME'], '/rt')
        self.assertNotIn('GUI_FORMS_LLVM_RUNTIME', fetch_sdk.runtime_environment('windows-x64', harness))
        own: dict[str, str] = dict(harness, GUI_FORMS_LLVM_RUNTIME='/own')
        self.assertEqual(fetch_sdk.runtime_environment('linux-x64', own)['GUI_FORMS_LLVM_RUNTIME'], '/own')
        unprefixed: dict[str, str] = {'HARNESS_LLVM_RUNTIME': '/plain'}
        self.assertEqual(fetch_sdk.runtime_environment('linux-x64', unprefixed)['GUI_FORMS_LLVM_RUNTIME'], '/plain')
        self.assertNotIn('GUI_FORMS_LLVM_RUNTIME', fetch_sdk.runtime_environment('linux-x64', {}))

    def test_the_printed_settings_are_the_three_own_runtime_lines(self) -> None:
        lines: list[str] = ['+ cmake --build x\n', 'GUI_FORMS_OWN_RUNTIME_SOURCE=/o/stxlib\n', 'OTHER=1\n',
                            'GUI_FORMS_LIBCXX_PREFIX=/o/libcxx\n']
        self.assertEqual(fetch_sdk.printed_settings(lines), {'GUI_FORMS_OWN_RUNTIME_SOURCE': '/o/stxlib',
                                                             'GUI_FORMS_LIBCXX_PREFIX': '/o/libcxx'})

    def test_the_own_runtime_comes_from_the_environment_or_the_sdks_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory) / 'sdk'
            install: Path = Path(directory) / 'cache' / 'gui-forms-1.0.1-windows-x64'
            named: dict[str, str] = {name: '/' + name for name in fetch_sdk.OWN_RUNTIME_SETTINGS}
            self.assertEqual(fetch_sdk.own_runtime(root, install, 2, named, False), named)
            # An SDK without the tool predates the own runtime.
            self.assertEqual(fetch_sdk.own_runtime(root, install, 2, {}, False), {})
            (root / 'tools').mkdir(parents=True)
            (root / fetch_sdk.OWN_RUNTIME_ENTRY).write_text(OWN_RUNTIME_SCRIPT, encoding='utf-8')
            with self.assertRaisesRegex(fetch_sdk.FetchError, '--allow-network-inputs'):
                fetch_sdk.own_runtime(root, install, 2, {}, False)
            environment: dict[str, str] = dict(os.environ)
            settings: dict[str, str] = fetch_sdk.own_runtime(root, install, 2, environment, True)
            out: Path = fetch_sdk.own_runtime_dir(install)
            self.assertEqual(out.name, 'gui-forms-1.0.1-windows-x64.own-runtime')
            self.assertEqual(settings, {'GUI_FORMS_OWN_RUNTIME_SOURCE': (out / 'stxlib').as_posix(),
                                        'GUI_FORMS_OWN_RUNTIME_PREFIX': (out / 'own-runtime').as_posix(),
                                        'GUI_FORMS_LIBCXX_PREFIX': (out / 'libcxx').as_posix()})
            environment['FAKE_OWN_RUNTIME_FAIL'] = '1'
            with self.assertRaisesRegex(fetch_sdk.FetchError, 'own runtime failed'):
                fetch_sdk.own_runtime(root, install, 2, environment, True)

    def test_a_windows_install_keeps_its_toolchain_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory) / 'sdk'
            (root / 'cmake').mkdir(parents=True)
            relative: str
            for relative in fetch_sdk.WINDOWS_TOOLCHAIN_FILES:
                (root / relative).write_text('# ' + relative + '\n', encoding='utf-8')
            install: Path = Path(directory) / 'install'
            package: Path = install / 'lib' / 'cmake' / 'GUIForms'
            package.mkdir(parents=True)
            runtime: Path = Path(directory) / 'runtime'
            settings: dict[str, str] = {}
            name: str
            for name in fetch_sdk.OWN_RUNTIME_SETTINGS:
                (runtime / name).mkdir(parents=True)
                settings[name] = (runtime / name).as_posix()
            self.assertEqual(fetch_sdk.describe(install), ['prefix=' + install.as_posix()])
            self.assertTrue(fetch_sdk.settings_present(install))
            fetch_sdk.keep_windows_toolchain(root, install, settings)
            self.assertTrue((package / 'llvm22-windows-msvc-own.cmake').is_file())
            expected: list[str] = ['prefix=' + install.as_posix(),
                                   'windows_toolchain=' + (package / 'llvm22-windows-msvc.cmake').as_posix()]
            expected.extend(name + '=' + settings[name] for name in fetch_sdk.OWN_RUNTIME_SETTINGS)
            self.assertEqual(fetch_sdk.describe(install), expected)
            self.assertTrue(fetch_sdk.settings_present(install))
            # A cache that restored the install without its runtime builds it again.
            (runtime / 'GUI_FORMS_LIBCXX_PREFIX').rmdir()
            self.assertFalse(fetch_sdk.settings_present(install))


class ToolchainTests(unittest.TestCase):
    """ci_build.py configures with the harness toolchain, or on Windows the SDK's own runtime toolchain."""

    def test_macos_and_linux_use_the_harness_toolchain(self) -> None:
        harness: str = '-DCMAKE_TOOLCHAIN_FILE=' + (ci_build.HARNESS_ROOT / 'harness/cmake/llvm22.cmake').as_posix()
        self.assertEqual(ci_build.toolchain_options('linux-x64', ['prefix=/p']), [harness])
        self.assertEqual(ci_build.toolchain_options('macos-arm64', ['prefix=/p', 'windows_toolchain=/w.cmake']),
                         [harness])
        self.assertEqual(ci_build.toolchain_options('windows-x64', ['prefix=/p']), [harness])

    def test_windows_configures_with_the_sdks_toolchain_and_own_runtime(self) -> None:
        lines: list[str] = ['prefix=/p', 'windows_toolchain=/p/lib/cmake/GUIForms/llvm22-windows-msvc.cmake',
                            'GUI_FORMS_OWN_RUNTIME_SOURCE=/o/stxlib', 'GUI_FORMS_OWN_RUNTIME_PREFIX=/o/own-runtime']
        with mock.patch.dict('os.environ', {'GUI_FORMS_LIBCXX_PREFIX': '/env/libcxx'}):
            options: list[str] = ci_build.toolchain_options('windows-x64', lines)
        self.assertEqual(options, ['-DCMAKE_TOOLCHAIN_FILE=/p/lib/cmake/GUIForms/llvm22-windows-msvc.cmake',
                                   '-DGUI_FORMS_MSVC_RUNTIME=OWN', '-DGUI_FORMS_OWN_RUNTIME_SOURCE=/o/stxlib',
                                   '-DGUI_FORMS_OWN_RUNTIME_PREFIX=/o/own-runtime',
                                   '-DGUI_FORMS_LIBCXX_PREFIX=/env/libcxx'])
        with mock.patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(fetch_sdk.FetchError, 'GUI_FORMS_LIBCXX_PREFIX'):
                ci_build.toolchain_options('windows-x64', lines)


class PinTests(unittest.TestCase):
    def release(self) -> tuple[list[dict[str, Any]], str]:
        """A v0.2.9 listing with the source SDK, a program package and SHA256SUMS.txt, and that file's text."""
        digest: str = hashlib.sha256(b'source').hexdigest()
        listed: list[dict[str, Any]] = [
            {'name': 'gui-forms-sdk-0.2.9-source.tar.gz', 'size': 1000, 'digest': 'sha256:' + digest},
            {'name': 'gui-forms-gallery-0.2.9-linux-x64.deb', 'size': 5, 'digest': 'sha256:' + '1' * 64},
            {'name': fetch_sdk.CHECKSUMS_ASSET, 'size': 300, 'digest': None}]
        lines: list[str] = [digest + '  gui-forms-sdk-0.2.9-source.tar.gz',
                            '1' * 64 + ' *gui-forms-gallery-0.2.9-linux-x64.deb']
        text: str = '\n'.join(lines) + '\n'
        result: tuple[list[dict[str, Any]], str] = (listed, text)
        return result

    def pin(self, path: Path, listed: list[dict[str, Any]], sums: str) -> list[str]:
        with mock.patch.object(fetch_sdk, 'LOCK_PATH', path), \
                mock.patch.object(fetch_sdk, 'release_assets', return_value=listed), \
                mock.patch.object(fetch_sdk, 'release_checksums', return_value=sums):
            result: list[str] = fetch_sdk.pin_command(fetch_sdk.read_lock(path), 'v0.2.9')
        return result

    def test_pin_writes_the_source_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / 'gui_forms.lock'
            path.write_text(LOCK_TEXT, encoding='utf-8')
            listed: list[dict[str, Any]]
            sums: str
            listed, sums = self.release()
            printed: list[str] = self.pin(path, listed, sums)
            lock: fetch_sdk.Lock = fetch_sdk.read_lock(path)
            self.assertEqual(lock.tag, 'v0.2.9')
            self.assertEqual(lock.source, fetch_sdk.Source('gui-forms-sdk-0.2.9-source.tar.gz',
                                                           hashlib.sha256(b'source').hexdigest()))
            self.assertIn('gui-forms-sdk-0.2.9-source.tar.gz 1000 bytes', printed)
            # Pinning again replaces the table rather than adding another.
            self.pin(path, listed, sums)
            self.assertEqual(path.read_text(encoding='utf-8').count('[source]'), 1)

    def test_pin_refuses_disagreeing_or_missing_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / 'gui_forms.lock'
            path.write_text(LOCK_TEXT, encoding='utf-8')
            listed: list[dict[str, Any]]
            sums: str
            listed, sums = self.release()
            listed[0]['digest'] = 'sha256:' + '2' * 64
            with self.assertRaises(fetch_sdk.FetchError):
                self.pin(path, listed, sums)
            listed[0]['digest'] = None
            kept: list[str] = [line for line in sums.splitlines() if 'source' not in line]
            with self.assertRaises(fetch_sdk.FetchError):
                self.pin(path, listed, '\n'.join(kept) + '\n')
            self.assertEqual(path.read_text(encoding='utf-8'), LOCK_TEXT)

    def test_pin_refuses_a_release_without_the_source_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / 'gui_forms.lock'
            path.write_text(LOCK_TEXT, encoding='utf-8')
            with self.assertRaisesRegex(fetch_sdk.FetchError, 'publishes no gui-forms-sdk-0.2.9-source'):
                self.pin(path, [{'name': fetch_sdk.CHECKSUMS_ASSET, 'size': 1}], '')
            self.assertEqual(path.read_text(encoding='utf-8'), LOCK_TEXT)


if __name__ == '__main__':
    unittest.main()
