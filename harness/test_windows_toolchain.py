"""The Windows toolchain archive is the exact tree that filled the cache, and nothing else."""
from __future__ import annotations
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import harness_testing
import windows_toolchain


def write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_package(msys_root: Path, name: str, version: str) -> None:
    description: str = '%NAME%\n' + name + '\n\n%VERSION%\n' + version + '\n\n%DESC%\nfixture\n'
    write_file(msys_root / 'var/lib/pacman/local' / (name + '-' + version) / 'desc', description.encode('utf-8'))


def make_tree(msys_root: Path, spec: dict[str, Any]) -> None:
    write_file(msys_root / 'clang64/bin/clang.exe', b'clang 22.1.8 fixture')
    write_file(msys_root / 'usr/bin/bash.exe', b'bash fixture')
    write_file(msys_root / 'clang64/include/stdio.h', b'/* header */')
    write_file(msys_root / 'etc/pacman.conf', b'[options]\n')
    write_file(msys_root / 'etc/pacman.d/gnupg/pubring.gpg', b'public keys')
    write_file(msys_root / 'etc/pacman.d/gnupg/private-keys-v1.d/local.key', b'SECRET')
    write_file(msys_root / 'etc/pacman.d/gnupg/secring.gpg', b'SECRET')
    write_file(msys_root / 'etc/pacman.d/gnupg/S.gpg-agent', b'socket')
    write_file(msys_root / 'var/cache/pacman/pkg/clang.pkg.tar.zst', b'package cache')
    write_file(msys_root / 'var/lib/pacman/sync/clang64.db', b'sync database')
    write_file(msys_root / 'usr/share/doc/git/README', b'documentation')
    write_file(msys_root / 'clang64/share/man/man1/clang.1', b'manual')
    write_file(msys_root / 'home/runneradmin/.bash_history', b'history')
    write_file(msys_root / 'tmp/runner-scratch.txt', b'scratch')
    name: str
    for name in spec['packages']:
        if name == windows_toolchain.CLANG_PACKAGE:
            write_package(msys_root, name, spec['llvm'] + '-3')
        else:
            write_package(msys_root, name, '1.0-1')
    write_package(msys_root, 'mingw-w64-clang-x86_64-llvm-libs', '22.1.8-3')


def member_names(archive: Path) -> list[str]:
    names: list[str] = []
    with tarfile.open(archive, 'r:zst') as payload:
        member: tarfile.TarInfo
        for member in payload.getmembers():
            names.append(member.name)
    return names


PRODUCER: dict[str, str] = {'run_id': '1', 'revision': 'r', 'runner': 'windows-2022',
                            'runner_image_version': 'image', 'msys_root': 'D:\\a\\_temp\\msys64'}


class SpecificationTests(unittest.TestCase):
    def test_checked_in_spec_names_every_requested_tool(self) -> None:
        spec: dict[str, Any] = windows_toolchain.load_spec()
        expected: list[str] = ['clang', 'lld', 'llvm-tools', 'cmake', 'ninja', 'ccache',
                               'winpthreads', 'python', 'nsis']
        short: str
        for short in expected:
            self.assertIn('mingw-w64-clang-x86_64-' + short, spec['packages'])
        self.assertIn('git', spec['packages'])

    def test_digest_follows_packages_and_layout(self) -> None:
        spec: dict[str, Any] = windows_toolchain.load_spec()
        original: str = windows_toolchain.spec_digest(spec)
        changed: dict[str, Any] = json.loads(json.dumps(spec))
        changed['llvm'] = '22.1.9'
        self.assertNotEqual(windows_toolchain.spec_digest(changed), original)
        with patch.object(windows_toolchain, 'LAYOUT', windows_toolchain.LAYOUT + 1):
            self.assertNotEqual(windows_toolchain.spec_digest(spec), original)
        self.assertEqual(windows_toolchain.spec_digest(spec), original)


@unittest.skipUnless(hasattr(tarfile.TarFile, 'zstopen'), 'requires Python 3.14 zstd')
class ArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.work: Path = Path(self.directory.name)
        self.spec: dict[str, Any] = windows_toolchain.load_spec()
        self.msys_root: Path = self.work / 'producer/msys64'
        make_tree(self.msys_root, self.spec)
        self.output: Path = self.work / 'output'
        self.output.mkdir()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_archive_keeps_toolchain_and_omits_caches_docs_and_secrets(self) -> None:
        manifest: dict[str, Any] = windows_toolchain.pack(self.msys_root, self.output, self.spec, PRODUCER)
        names: list[str] = member_names(self.output / windows_toolchain.ARCHIVE_NAME)
        self.assertIn('msys64/clang64/bin/clang.exe', names)
        self.assertIn('msys64/usr/bin/bash.exe', names)
        self.assertIn('msys64/etc/pacman.d/gnupg/pubring.gpg', names)
        # Runtime directories survive empty: TMP points at /tmp.
        self.assertIn('msys64/tmp', names)
        self.assertIn('msys64/home', names)
        omitted: list[str] = ['msys64/etc/pacman.d/gnupg/private-keys-v1.d/local.key',
                              'msys64/etc/pacman.d/gnupg/secring.gpg',
                              'msys64/etc/pacman.d/gnupg/S.gpg-agent',
                              'msys64/var/cache/pacman/pkg/clang.pkg.tar.zst',
                              'msys64/var/lib/pacman/sync/clang64.db',
                              'msys64/usr/share/doc/git/README',
                              'msys64/clang64/share/man/man1/clang.1',
                              'msys64/home/runneradmin/.bash_history',
                              'msys64/tmp/runner-scratch.txt']
        name: str
        for name in omitted:
            self.assertNotIn(name, names)
        self.assertEqual(manifest['packages']['mingw-w64-clang-x86_64-llvm-libs'], '22.1.8-3')
        self.assertEqual(manifest['spec_sha256'], windows_toolchain.spec_digest(self.spec))
        self.assertEqual(manifest['path'], ['msys64/clang64/bin', 'msys64/usr/bin'])
        self.assertEqual(manifest['producer']['msys_root'], 'D:\\a\\_temp\\msys64')

    def test_extraction_reproduces_the_byte_identical_compiler(self) -> None:
        manifest: dict[str, Any] = windows_toolchain.pack(self.msys_root, self.output, self.spec, PRODUCER)
        consumer: Path = self.work / 'consumer'
        consumer.mkdir()
        restored: Path = windows_toolchain.extract(self.output / windows_toolchain.ARCHIVE_NAME, manifest, consumer)
        self.assertEqual((restored / 'clang64/bin/clang.exe').read_bytes(), b'clang 22.1.8 fixture')
        self.assertTrue((restored / 'tmp').is_dir())
        self.assertTrue((restored / 'var/tmp').is_dir())
        self.assertEqual(windows_toolchain.file_digest(restored / 'clang64/bin/clang.exe'),
                         manifest['compiler_sha256'])
        with self.assertRaisesRegex(ValueError, 'existing'):
            windows_toolchain.extract(self.output / windows_toolchain.ARCHIVE_NAME, manifest, consumer)

    def test_missing_requested_package_is_rejected(self) -> None:
        spec: dict[str, Any] = json.loads(json.dumps(self.spec))
        spec['packages'].append('mingw-w64-clang-x86_64-absent')
        with self.assertRaisesRegex(ValueError, 'absent'):
            windows_toolchain.pack(self.msys_root, self.output, spec, PRODUCER)

    def test_an_llvm_the_specification_does_not_pin_is_not_packaged(self) -> None:
        # MSYS2 moved on: the mirrors installed the current clang, not the pinned one.
        write_package(self.msys_root, windows_toolchain.CLANG_PACKAGE, '23.1.3-1')
        clang_records: list[Path] = sorted((self.msys_root / 'var/lib/pacman/local').glob(
            windows_toolchain.CLANG_PACKAGE + '-' + self.spec['llvm'] + '-*'))
        record: Path
        for record in clang_records:
            (record / 'desc').unlink()
        with self.assertRaisesRegex(windows_toolchain.ToolchainUnavailable, '23.1.3-1'):
            windows_toolchain.pack(self.msys_root, self.output, self.spec, PRODUCER)
        self.assertFalse((self.output / windows_toolchain.ARCHIVE_NAME).exists())

    def test_changed_archive_bytes_fail_closed(self) -> None:
        manifest: dict[str, Any] = windows_toolchain.pack(self.msys_root, self.output, self.spec, PRODUCER)
        archive: Path = self.output / windows_toolchain.ARCHIVE_NAME
        archive.write_bytes(archive.read_bytes() + b'\0')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            windows_toolchain.extract(archive, manifest, self.work)

    def test_member_outside_msys64_is_rejected(self) -> None:
        archive: Path = self.output / windows_toolchain.ARCHIVE_NAME
        with tarfile.open(archive, 'w:zst') as payload:
            member: tarfile.TarInfo = tarfile.TarInfo('elsewhere/file')
            member.size = 1
            payload.addfile(member, io.BytesIO(b'x'))
        manifest: dict[str, Any] = {'schema': 1, 'platform': 'windows-x64',
                                    'archive_sha256': windows_toolchain.file_digest(archive)}
        with self.assertRaisesRegex(ValueError, 'Unexpected toolchain archive member'):
            windows_toolchain.extract(archive, manifest, self.work)


class WrapperTests(unittest.TestCase):
    def test_wrapper_matches_setup_msys2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wrapper: Path = windows_toolchain.write_wrapper(
                Path('D:\\a\\_temp\\msys64'), Path(directory) / 'setup-msys2', 'CLANG64')
            text: str = wrapper.read_bytes().decode('utf-8')
        expected: str = ('@echo off\r\nsetlocal\r\nIF NOT DEFINED MSYSTEM set MSYSTEM=CLANG64\r\n'
                         'IF NOT DEFINED MSYS2_PATH_TYPE set MSYS2_PATH_TYPE=inherit\r\n'
                         'set CHERE_INVOKING=1\r\nD:\\a\\_temp\\msys64\\usr\\bin\\bash.exe -leo pipefail %*')
        self.assertEqual(text, expected)


def toolchain_manifest(archive_sha256: str, compiler_sha256: str) -> dict[str, Any]:
    manifest: dict[str, Any] = {'schema': 1, 'platform': 'windows-x64', 'archive': windows_toolchain.ARCHIVE_NAME,
                                'archive_sha256': archive_sha256, 'spec_sha256': 'spec',
                                'compiler_sha256': compiler_sha256, 'producer': PRODUCER}
    return manifest


def write_cache(root: Path, cache_manifest: dict[str, Any]) -> None:
    data: bytes = json.dumps(cache_manifest).encode('utf-8')
    with tarfile.open(root / windows_toolchain.CACHE_NAME, 'w:gz') as payload:
        member: tarfile.TarInfo = tarfile.TarInfo('cache-manifest.json')
        member.size = len(data)
        payload.addfile(member, io.BytesIO(data))


class PublicationTests(unittest.TestCase):
    def prepare(self, root: Path, recorded_sha256: str) -> None:
        archive: Path = root / windows_toolchain.ARCHIVE_NAME
        archive.write_bytes(b'toolchain')
        manifest: dict[str, Any] = toolchain_manifest(windows_toolchain.file_digest(archive), 'compiler')
        (root / windows_toolchain.MANIFEST_NAME).write_text(json.dumps(manifest))
        record: dict[str, Any] = windows_toolchain.cache_record(manifest)
        record['sha256'] = recorded_sha256
        write_cache(root, {'compiler_sha256': 'compiler', 'toolchain': record})

    def test_recorded_toolchain_is_published_before_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            self.prepare(root, hashlib.sha256(b'toolchain').hexdigest())
            files: list[Path] = windows_toolchain.publication_files(root)
            self.assertEqual(files, [root / windows_toolchain.ARCHIVE_NAME, root / windows_toolchain.MANIFEST_NAME])

    def test_a_different_toolchain_is_not_published_beside_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            self.prepare(root, '0' * 64)
            with self.assertRaisesRegex(ValueError, 'recorded by the cache'):
                windows_toolchain.publication_files(root)

    def test_cache_without_toolchain_record_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            write_cache(root, {'compiler_sha256': 'compiler'})
            with self.assertRaisesRegex(ValueError, 'does not record'):
                windows_toolchain.cache_toolchain(root / windows_toolchain.CACHE_NAME)


class ReleaseCommands:
    """Fake gh: release listings by page and per-tag toolchain manifests."""

    def __init__(self, releases: list[dict[str, Any]], manifests: dict[str, dict[str, Any]]) -> None:
        self.releases: list[dict[str, Any]] = releases
        self.manifests: dict[str, dict[str, Any]] = manifests
        self.downloads: list[str] = []

    def api(self, endpoint: str) -> Any:
        if '/actions/artifacts' in endpoint:
            return {'artifacts': []}
        if 'page=1' in endpoint:
            return self.releases
        return []

    def run(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        tag: str = command[3]
        self.downloads.append(tag)
        destination: Path = Path(command[command.index('--dir') + 1])
        (destination / windows_toolchain.MANIFEST_NAME).write_text(json.dumps(self.manifests[tag]))
        return subprocess.CompletedProcess(command, 0, '', '')


def release(tag: str, with_toolchain: bool, draft: bool = False) -> dict[str, Any]:
    assets: list[dict[str, Any]] = [{'name': windows_toolchain.CACHE_NAME}]
    if with_toolchain:
        assets.append({'name': windows_toolchain.ARCHIVE_NAME})
        assets.append({'name': windows_toolchain.MANIFEST_NAME})
    value: dict[str, Any] = {'tag_name': tag, 'draft': draft, 'assets': assets}
    return value


class DiscoveryTests(unittest.TestCase):
    def find(self, fixture: ReleaseCommands, field: str, value: str) -> tuple[str, dict[str, Any]] | None:
        with patch.object(windows_toolchain.ci_reuse, 'api', side_effect=fixture.api), \
             patch.object(windows_toolchain.subprocess, 'run', side_effect=fixture.run):
            found: tuple[str, dict[str, Any]] | None = windows_toolchain.find_release('owner/repo', field, value)
        return found

    def test_newest_release_with_matching_specification_is_selected(self) -> None:
        old: dict[str, Any] = toolchain_manifest('a' * 64, 'c')
        new: dict[str, Any] = toolchain_manifest('b' * 64, 'c')
        other: dict[str, Any] = toolchain_manifest('d' * 64, 'c')
        other['spec_sha256'] = 'different'
        fixture: ReleaseCommands = ReleaseCommands(
            [release('build-4', False), release('build-3', True), release('build-2', True),
             release('build-1', True), release('build-0', True, draft=True)],
            {'build-3': other, 'build-2': new, 'build-1': old})
        found: tuple[str, dict[str, Any]] | None = self.find(fixture, 'spec_sha256', 'spec')
        self.assertIsNotNone(found)
        if found is not None:
            self.assertEqual(found[0], 'build-2')
        self.assertEqual(fixture.downloads, ['build-3', 'build-2'])

    def test_promotion_finds_the_exact_recorded_archive(self) -> None:
        fixture: ReleaseCommands = ReleaseCommands(
            [release('build-2', True), release('build-1', True)],
            {'build-2': toolchain_manifest('b' * 64, 'c'), 'build-1': toolchain_manifest('a' * 64, 'c')})
        found: tuple[str, dict[str, Any]] | None = self.find(fixture, 'archive_sha256', 'a' * 64)
        self.assertIsNotNone(found)
        if found is not None:
            self.assertEqual(found[0], 'build-1')

    def test_discovery_is_bounded_and_absence_is_reported(self) -> None:
        releases: list[dict[str, Any]] = []
        manifests: dict[str, dict[str, Any]] = {}
        index: int
        for index in range(20):
            tag: str = 'build-' + str(index)
            releases.append(release(tag, True))
            manifests[tag] = toolchain_manifest('a' * 64, 'c')
        fixture: ReleaseCommands = ReleaseCommands(releases, manifests)
        self.assertIsNone(self.find(fixture, 'spec_sha256', 'unpublished'))
        self.assertEqual(len(fixture.downloads), windows_toolchain.MAXIMUM_MANIFESTS)

    def test_unpublished_and_expired_toolchain_fails_closed(self) -> None:
        listing: dict[str, Any] = {'artifacts': [
            {'expired': True, 'workflow_run': {'id': 5, 'head_repository_id': 1, 'repository_id': 1}},
            {'expired': False, 'workflow_run': {'id': 6, 'head_repository_id': 2, 'repository_id': 1}}]}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(windows_toolchain.ci_reuse, 'api', return_value=listing):
            with self.assertRaisesRegex(ValueError, 'force_validation'):
                windows_toolchain.download_artifact('owner/repo', 'a' * 64, Path(directory))


class PinnedReleaseCommands:
    """Fake gh: one release read by its tag (None: not found) and its toolchain manifest."""

    def __init__(self, release: dict[str, Any] | None, manifest: dict[str, Any]) -> None:
        self.release: dict[str, Any] | None = release
        self.manifest: dict[str, Any] = manifest

    def run(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        if command[1] == 'api':
            if self.release is None:
                return subprocess.CompletedProcess(command, 1, '', 'HTTP 404: Not Found')
            return subprocess.CompletedProcess(command, 0, json.dumps(self.release), '')
        destination: Path = Path(command[command.index('--dir') + 1])
        (destination / windows_toolchain.MANIFEST_NAME).write_text(json.dumps(self.manifest))
        return subprocess.CompletedProcess(command, 0, '', '')


class StepOutputs:
    """Records the step outputs restore_command writes."""

    def __init__(self) -> None:
        self.outputs: list[tuple[str, str]] = []

    def record(self, name: str, value: str) -> None:
        self.outputs.append((name, value))


class PinnedReleaseTests(unittest.TestCase):
    def test_the_checked_in_pin_names_the_current_specification(self) -> None:
        spec: dict[str, Any] = windows_toolchain.load_spec()
        pin: dict[str, Any] = json.loads(windows_toolchain.PIN_PATH.read_text(encoding='utf-8'))
        self.assertEqual(pin['spec_sha256'], windows_toolchain.spec_digest(spec))
        self.assertEqual(pin['llvm'], spec['llvm'])
        self.assertTrue(pin['tag'].startswith('build-'))

    def pinned(self, pin: dict[str, Any], release: dict[str, Any] | None,
               manifest: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        commands: PinnedReleaseCommands = PinnedReleaseCommands(release, manifest)
        with tempfile.TemporaryDirectory() as directory:
            pin_path: Path = Path(directory) / 'pin.json'
            pin_path.write_text(json.dumps(pin), encoding='utf-8')
            with patch.object(windows_toolchain, 'PIN_PATH', pin_path), \
                 patch.object(windows_toolchain.subprocess, 'run', side_effect=commands.run):
                found: tuple[str, dict[str, Any]] | None = windows_toolchain.pinned_release('owner/repo', 'spec')
        return found

    def test_the_pinned_release_is_read_by_its_tag(self) -> None:
        manifest: dict[str, Any] = toolchain_manifest('a' * 64, 'c')
        found: tuple[str, dict[str, Any]] | None = self.pinned(
            {'spec_sha256': 'spec', 'archive_sha256': 'a' * 64, 'tag': 'build-1'}, release('build-1', True), manifest)
        self.assertIsNotNone(found)
        if found is not None:
            self.assertEqual(found[0], 'build-1')

    def test_a_pin_of_another_specification_or_archive_is_not_used(self) -> None:
        manifest: dict[str, Any] = toolchain_manifest('a' * 64, 'c')
        self.assertIsNone(self.pinned({'spec_sha256': 'older', 'archive_sha256': 'a' * 64, 'tag': 'build-1'},
                                      release('build-1', True), manifest))
        self.assertIsNone(self.pinned({'spec_sha256': 'spec', 'archive_sha256': 'b' * 64, 'tag': 'build-1'},
                                      release('build-1', True), manifest))

    def test_a_missing_or_empty_pinned_release_is_not_used(self) -> None:
        manifest: dict[str, Any] = toolchain_manifest('a' * 64, 'c')
        pin: dict[str, Any] = {'spec_sha256': 'spec', 'archive_sha256': 'a' * 64, 'tag': 'build-1'}
        self.assertIsNone(self.pinned(pin, None, manifest))
        self.assertIsNone(self.pinned(pin, release('build-1', False), manifest))


class RestoreTests(unittest.TestCase):
    def restore(self, environ: dict[str, str]) -> list[tuple[str, str]]:
        outputs: StepOutputs = StepOutputs()
        with patch.object(windows_toolchain, 'pinned_release', return_value=None), \
             patch.object(windows_toolchain, 'find_release', return_value=None), \
             patch.object(windows_toolchain.ci_reuse, 'output', side_effect=outputs.record), \
             patch.dict(windows_toolchain.os.environ, environ, clear=True):
            windows_toolchain.restore_command('owner/repo')
        return outputs.outputs

    def test_without_a_published_toolchain_the_job_stops(self) -> None:
        with self.assertRaisesRegex(windows_toolchain.ToolchainUnavailable, 'will not install'):
            self.restore({})
        with self.assertRaises(windows_toolchain.ToolchainUnavailable):
            self.restore({windows_toolchain.PACKAGE_VARIABLE: 'false'})

    def test_only_a_packaging_request_reaches_the_mirrors(self) -> None:
        outputs: list[tuple[str, str]] = self.restore({windows_toolchain.PACKAGE_VARIABLE: 'true'})
        self.assertIn(('restored', 'false'), outputs)


class ExclusionTests(unittest.TestCase):
    def test_only_named_paths_are_excluded(self) -> None:
        kept: list[str] = ['clang64/bin/clang.exe', 'usr/bin/pacman.exe', 'var/lib/pacman/local/x/desc',
                           'etc/pacman.d/gnupg/pubring.gpg', 'usr/share/docs-not-doc/file',
                           'clang64/lib/S.file']
        omitted: list[str] = ['var/cache/pacman/pkg/a', 'usr/share/doc', 'etc/pacman.d/gnupg/secring.gpg',
                              'etc/pacman.d/gnupg/S.gpg-agent.ssh', 'home/user/file']
        path: str
        for path in kept:
            self.assertFalse(windows_toolchain.excluded(windows_toolchain.PurePosixPath(path)), path)
        for path in omitted:
            self.assertTrue(windows_toolchain.excluded(windows_toolchain.PurePosixPath(path)), path)


if __name__ == '__main__':
    unittest.main()
