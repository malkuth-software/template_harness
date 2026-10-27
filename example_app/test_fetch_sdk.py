#!/usr/bin/env python3
"""fetch_sdk.py without the network: downloads and release listings are replaced by local files."""
from __future__ import annotations
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
from typing import Any
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_sdk  # noqa: E402

LOCK_TEXT: str = '''schema = 1

[release]
repository = "malkuth-software/gui_forms"
tag = "v0.2.7"
sdk_asset = "gui-forms-sdk-{version}-{platform}"

[sdk]
'''


def sdk_tarball(path: Path, extra: tuple[str, bytes] | None = None) -> str:
    """Write a minimal SDK archive (one top directory, the package config) and return its digest."""
    with tarfile.open(path, 'w:gz') as bundle:
        members: list[tuple[str, bytes]] = [('sdk/lib/cmake/GUIForms/GUIFormsConfig.cmake', b'# config\n'),
                                            ('sdk/include/gui_forms/gui_forms.hpp', b'#pragma once\n')]
        if extra is not None:
            members.append(extra)
        name: str
        data: bytes
        for name, data in members:
            info: tarfile.TarInfo = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    result: str = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def locked(asset_name: str, digest: str) -> fetch_sdk.Lock:
    asset: fetch_sdk.Asset = fetch_sdk.Asset('linux-x64', asset_name, digest)
    result: fetch_sdk.Lock = fetch_sdk.Lock('malkuth-software/gui_forms', 'v0.2.7',
                                            'gui-forms-sdk-{version}-{platform}', {'linux-x64': asset})
    return result


class CopyingDownload:
    """Stands in for fetch_sdk.download: copies one local archive and counts the calls."""

    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls = 0

    def __call__(self, url: str, target: Path) -> None:
        self.calls += 1
        target.write_bytes(self.source.read_bytes())


class FetchTests(unittest.TestCase):
    def test_the_lock_is_validated_whole(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / 'gui_forms.lock'
            path.write_text(LOCK_TEXT, encoding='utf-8')
            self.assertEqual(fetch_sdk.read_lock(path).assets, {})
            path.write_text(LOCK_TEXT + '[sdk.linux-x64]\nname = "a.tar.gz"\nsha256 = "abc"\n', encoding='utf-8')
            with self.assertRaises(fetch_sdk.FetchError):
                fetch_sdk.read_lock(path)
            path.write_text(LOCK_TEXT + '[sdk.plan9]\nname = "a.tar.gz"\nsha256 = "' + '0' * 64 + '"\n',
                            encoding='utf-8')
            with self.assertRaises(fetch_sdk.FetchError):
                fetch_sdk.read_lock(path)

    def test_the_committed_lock_reads(self) -> None:
        lock: fetch_sdk.Lock = fetch_sdk.read_lock(fetch_sdk.LOCK_PATH)
        self.assertEqual(lock.repository, 'malkuth-software/gui_forms')
        self.assertTrue(lock.tag.startswith('v0.2.'))

    def test_member_paths_stay_inside(self) -> None:
        self.assertTrue(fetch_sdk.safe_member('sdk/lib/x.a'))
        self.assertFalse(fetch_sdk.safe_member('../x'))
        self.assertFalse(fetch_sdk.safe_member('/etc/x'))
        self.assertFalse(fetch_sdk.safe_member('C:/x'))
        self.assertFalse(fetch_sdk.safe_member('sdk\\..\\..\\x'))

    def test_a_cache_inside_the_repository_is_refused(self) -> None:
        with self.assertRaises(fetch_sdk.FetchError):
            fetch_sdk.checked_cache(fetch_sdk.HERE / 'cache')

    def test_fetch_verifies_unpacks_and_reuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            archive: Path = root / 'source.tar.gz'
            digest: str = sdk_tarball(archive)
            fake: CopyingDownload = CopyingDownload(archive)
            lock: fetch_sdk.Lock = locked('gui-forms-sdk-0.2.7-linux-x64.tar.gz', digest)
            cache: Path = root / 'cache'
            with mock.patch.object(fetch_sdk, 'download', fake), mock.patch.dict('os.environ', {'GITHUB_OUTPUT': ''}):
                first: list[str] = fetch_sdk.fetch_command(lock, 'linux-x64', cache)
                second: list[str] = fetch_sdk.fetch_command(lock, 'linux-x64', cache)
                third: list[str] = fetch_sdk.prefix_command(lock, 'linux-x64', cache)
            expected: str = 'prefix=' + (cache / 'gui-forms-sdk' / 'v0.2.7' / 'linux-x64' / 'sdk').as_posix()
            self.assertEqual(first, [expected])
            self.assertEqual(second, [expected])
            self.assertEqual(third, [expected])
            self.assertEqual(fake.calls, 1)

    def test_a_wrong_digest_leaves_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            archive: Path = root / 'source.tar.gz'
            sdk_tarball(archive)
            lock: fetch_sdk.Lock = locked('gui-forms-sdk-0.2.7-linux-x64.tar.gz', '0' * 64)
            cache: Path = root / 'cache'
            with mock.patch.object(fetch_sdk, 'download', CopyingDownload(archive)):
                with self.assertRaises(fetch_sdk.FetchError):
                    fetch_sdk.fetch_command(lock, 'linux-x64', cache)
            platform_dir: Path = cache / 'gui-forms-sdk' / 'v0.2.7'
            self.assertEqual(list(platform_dir.iterdir()), [])

    def test_a_traversing_member_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            archive: Path = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('sdk/lib/cmake/GUIForms/GUIFormsConfig.cmake', '# config\n')
                bundle.writestr('../escaped.txt', 'no\n')
            into: Path = root / 'into'
            into.mkdir()
            with self.assertRaises(fetch_sdk.FetchError):
                fetch_sdk.extract(archive, 'bad.zip', into)
            self.assertFalse((root / 'escaped.txt').exists())

    def test_pin_rewrites_the_sdk_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / 'gui_forms.lock'
            path.write_text(LOCK_TEXT, encoding='utf-8')
            listed: list[dict[str, Any]] = []
            platform: str
            for platform in fetch_sdk.PLATFORMS:
                suffix: str = '.zip' if platform.startswith('windows') else '.tar.gz'
                listed.append({'name': 'gui-forms-sdk-0.2.9-' + platform + suffix,
                               'digest': 'sha256:' + hashlib.sha256(platform.encode()).hexdigest()})
            listed.append({'name': 'gui-forms-gallery-0.2.9-linux-x64.deb', 'digest': 'sha256:' + '1' * 64})
            with mock.patch.object(fetch_sdk, 'LOCK_PATH', path), \
                    mock.patch.object(fetch_sdk, 'release_assets', return_value=listed):
                fetch_sdk.pin_command(fetch_sdk.read_lock(path), 'v0.2.9')
            lock: fetch_sdk.Lock = fetch_sdk.read_lock(path)
            self.assertEqual(lock.tag, 'v0.2.9')
            self.assertEqual(sorted(lock.assets), sorted(fetch_sdk.PLATFORMS))
            self.assertEqual(lock.assets['windows-x64'].name, 'gui-forms-sdk-0.2.9-windows-x64.zip')

    def test_pin_refuses_a_release_without_sdks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / 'gui_forms.lock'
            path.write_text(LOCK_TEXT, encoding='utf-8')
            with mock.patch.object(fetch_sdk, 'LOCK_PATH', path), \
                    mock.patch.object(fetch_sdk, 'release_assets', return_value=[]):
                with self.assertRaises(fetch_sdk.FetchError):
                    fetch_sdk.pin_command(fetch_sdk.read_lock(path), 'v0.2.9')
            self.assertEqual(path.read_text(encoding='utf-8'), LOCK_TEXT)


if __name__ == '__main__':
    unittest.main()
