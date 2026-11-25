"""Unit tests for restore_local.py with gh replaced (no network)."""
from __future__ import annotations
import contextlib
import dataclasses
import hashlib
import io
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
from typing import Any
import unittest
from unittest import mock

import harness_testing
import project
import restore_local

GIT_ENVIRONMENT: dict[str, str] = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
UNKNOWN: str = 'c' * 40


def git(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def release(revision: str, created: str, asset: str, digest: str = '') -> dict[str, Any]:
    entry: dict[str, Any] = {'name': asset, 'created_at': created}
    if digest:
        entry['digest'] = 'sha256:' + digest
    return {'tag_name': 'build-' + revision, 'prerelease': True, 'assets': [entry]}


class FakeGitHub:
    """Stands in for restore_local.gh_json and gh_download: one release listing and one archive."""

    def __init__(self, releases: list[dict[str, Any]], archive: bytes) -> None:
        self.releases: list[dict[str, Any]] = releases
        self.archive: bytes = archive
        self.downloads: list[str] = []

    def json(self, path: str) -> Any:
        return self.releases

    def download(self, repository: str, tag: str, asset: str, directory: Path) -> Path:
        self.downloads.append(tag)
        directory.mkdir(parents=True, exist_ok=True)
        path: Path = directory / asset
        path.write_bytes(self.archive)
        return path


def cache_archive() -> bytes:
    """A gzip tar holding .ccache/entry and cache-manifest.json, as a published cache does."""
    buffer: io.BytesIO = io.BytesIO()
    payload: tarfile.TarFile
    with tarfile.open(fileobj=buffer, mode='w:gz') as payload:
        name: str
        for name in ('.ccache/entry', 'cache-manifest.json'):
            data: bytes = (name + '\n').encode('ascii')
            member: tarfile.TarInfo = tarfile.TarInfo(name)
            member.size = len(data)
            payload.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace: Path = Path(directory.name).resolve()
        environment: dict[str, str] = dict(GIT_ENVIRONMENT)
        environment['HARNESS_WORKSPACE'] = str(self.workspace)
        patched: mock._patch_dict = mock.patch.dict(os.environ, environment)
        patched.start()
        self.addCleanup(patched.stop)
        checkout: Path = self.workspace / 'gui_forms'
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(checkout)], check=True)
        revisions: list[str] = []
        index: int
        for index in range(2):
            (checkout / 'file.txt').write_text(str(index) + '\n', encoding='utf-8')
            git(checkout, 'add', 'file.txt')
            git(checkout, 'commit', '-q', '-m', 'Commit ' + str(index))
            revisions.append(git(checkout, 'rev-parse', 'HEAD'))
        self.older: str = revisions[0]
        self.newer: str = revisions[1]
        self.product: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        self.asset: str = 'gui-forms-cache-linux-x64.tar.gz'
        self.archive: bytes = cache_archive()

    def restore(self, github: FakeGitHub, dry_run: bool = False) -> str | None:
        with mock.patch.object(restore_local, 'gh_json', github.json), \
             mock.patch.object(restore_local, 'gh_download', github.download), \
             contextlib.redirect_stdout(io.StringIO()):
            tag: str | None = restore_local.restore_cache(self.product, 'linux-x64', dry_run)
        return tag

    def test_the_newest_release_in_the_checkout_history_is_restored_once(self) -> None:
        digest: str = hashlib.sha256(self.archive).hexdigest()
        github: FakeGitHub = FakeGitHub([
            release(self.older, '2026-10-20T00:00:00Z', self.asset, digest),
            release(UNKNOWN, '2026-10-26T00:00:00Z', self.asset, digest),
            release(self.newer, '2026-10-24T00:00:00Z', self.asset, digest),
            release(self.newer, '2026-10-25T00:00:00Z', 'gui-forms-cache-windows-x64.tar.gz')], self.archive)
        self.assertEqual(self.restore(github), 'build-' + self.newer)
        self.assertEqual((self.workspace / '.ccache' / 'entry').read_text(encoding='ascii'), '.ccache/entry\n')
        self.assertTrue((self.workspace / restore_local.CACHE_RECORD).is_file())
        self.assertEqual(self.restore(github), 'build-' + self.newer)
        self.assertEqual(github.downloads, ['build-' + self.newer])

    def test_a_dry_run_downloads_nothing(self) -> None:
        github: FakeGitHub = FakeGitHub([release(self.older, '2026-10-20T00:00:00Z', self.asset)], self.archive)
        self.assertEqual(self.restore(github, dry_run=True), 'build-' + self.older)
        self.assertEqual(github.downloads, [])
        self.assertFalse((self.workspace / '.ccache').exists())

    def test_a_digest_mismatch_is_refused(self) -> None:
        github: FakeGitHub = FakeGitHub([release(self.newer, '2026-10-24T00:00:00Z', self.asset, '0' * 64)],
                                        self.archive)
        with self.assertRaisesRegex(restore_local.RestoreError, 'digest'):
            self.restore(github)
        self.assertFalse((self.workspace / '.ccache').exists())

    def test_no_usable_release_restores_nothing(self) -> None:
        github: FakeGitHub = FakeGitHub([release(UNKNOWN, '2026-10-26T00:00:00Z', self.asset)], self.archive)
        self.assertIsNone(self.restore(github))
        self.assertEqual(github.downloads, [])

    def test_an_escaping_member_is_refused(self) -> None:
        buffer: io.BytesIO = io.BytesIO()
        payload: tarfile.TarFile
        with tarfile.open(fileobj=buffer, mode='w:gz') as payload:
            member: tarfile.TarInfo = tarfile.TarInfo('../outside')
            member.size = 0
            payload.addfile(member, io.BytesIO(b''))
        github: FakeGitHub = FakeGitHub([release(self.newer, '2026-10-24T00:00:00Z', self.asset)], buffer.getvalue())
        with self.assertRaisesRegex(restore_local.RestoreError, 'Unexpected cache archive member'):
            self.restore(github)


class ToolchainTests(unittest.TestCase):
    def test_linux_prints_the_package_installation(self) -> None:
        printed: io.StringIO = io.StringIO()
        product: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        with contextlib.redirect_stdout(printed):
            restore_local.restore_toolchain(product, 'linux-x64', False)
        # The script needs the project file, so the command names it.
        script: Path = harness_testing.HARNESS / 'install_linux_ci_packages.sh'
        expected: str = (project.PROJECT_VARIABLE + '=' + shlex.quote(str(product.path)) + ' sh '
                         + shlex.quote(str(script)) + ' toolkit <apt archive directory>')
        self.assertIn(expected, printed.getvalue())

    def test_the_linux_profile_is_toolkit_or_the_only_one(self) -> None:
        product: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        self.assertEqual(project.Project.workspace_linux_profile(product), 'toolkit')
        paint: project.Project = dataclasses.replace(product, linux_packages={'paint': ('cmake',)})
        self.assertEqual(project.Project.workspace_linux_profile(paint), 'paint')
        several: project.Project = dataclasses.replace(product, linux_packages={'b': ('x',), 'a': ('y',)})
        self.assertEqual(project.Project.workspace_linux_profile(several), '<profile: a | b>')
        assert several.bootstrap is not None
        chosen: project.Project = dataclasses.replace(
            several, bootstrap=dataclasses.replace(several.bootstrap, linux_packages='b'))
        self.assertEqual(project.Project.workspace_linux_profile(chosen), 'b')
        printed: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(printed):
            restore_local.restore_toolchain(paint, 'linux-x64', False)
        self.assertIn(' paint <apt archive directory>', printed.getvalue())
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            restore_local.restore_toolchain(dataclasses.replace(product, linux_packages={}), 'linux-x64', False)
        self.assertIn('names no Ubuntu packages', printed.getvalue())

    def test_windows_without_a_published_toolchain_stops(self) -> None:
        product: project.Project = project.load(harness_testing.REFERENCE_PROJECT)
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.dict(os.environ, {'HARNESS_WORKSPACE': directory}), \
             mock.patch.object(restore_local.windows_toolchain, 'pinned_release', return_value=None), \
             mock.patch.object(restore_local.windows_toolchain, 'find_release', return_value=None):
            with self.assertRaisesRegex(restore_local.RestoreError, 'No published Windows toolchain'):
                restore_local.restore_toolchain(product, 'windows-x64', True)


class PagedGitHub(FakeGitHub):
    """Lists the releases RELEASES_PER_PAGE at a time, as the API pages them."""

    def __init__(self, releases: list[dict[str, Any]], archive: bytes) -> None:
        super().__init__(releases, archive)
        self.paths: list[str] = []

    def json(self, path: str) -> Any:
        self.paths.append(path)
        page: int = int(path.rpartition('page=')[2])
        size: int = restore_local.RELEASES_PER_PAGE
        return self.releases[(page - 1) * size:page * size]


class ReleaseListingTests(CacheTests):
    def test_a_cache_past_the_first_page_is_found(self) -> None:
        filler: list[dict[str, Any]] = [release(UNKNOWN, '2026-10-30T00:00:00Z', 'other.tar.gz')
                                        for _ in range(restore_local.RELEASES_PER_PAGE)]
        github: PagedGitHub = PagedGitHub(filler + [release(self.newer, '2026-10-24T00:00:00Z', self.asset)],
                                          self.archive)
        self.assertEqual(self.restore(github), 'build-' + self.newer)
        self.assertEqual(len(github.paths), 2)

    def test_a_named_older_revision_is_restored_instead_of_the_newest(self) -> None:
        github: FakeGitHub = FakeGitHub([release(self.older, '2026-10-20T00:00:00Z', self.asset),
                                         release(self.newer, '2026-10-24T00:00:00Z', self.asset)], self.archive)
        with mock.patch.object(restore_local, 'gh_json', github.json), \
             mock.patch.object(restore_local, 'gh_download', github.download), \
             contextlib.redirect_stdout(io.StringIO()):
            tag: str | None = restore_local.restore_cache(self.product, 'linux-x64', False, self.older[:12])
            missing: str | None = restore_local.restore_cache(self.product, 'linux-x64', False, UNKNOWN[:12])
        self.assertEqual(tag, 'build-' + self.older)
        self.assertIsNone(missing)


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
            prepare=(), environment=(('GUI_FORMS_STX_PACKAGE_DIR', '{workspace}/.ci/stx-package'),))
        self.product: project.Project = dataclasses.replace(
            base, bootstrap=dataclasses.replace(base.bootstrap, platforms={'windows-x64': table}))

    def test_windows_gets_the_ccache_settings_the_locale_and_the_project_environment(self) -> None:
        values: dict[str, str] = restore_local.build_environment(self.product, 'windows-x64', [('EXTRA', 'x')])
        base: str = self.workspace.as_posix()
        self.assertEqual(values['CCACHE_DIR'], base + '/.ccache')
        self.assertEqual(values['CCACHE_BASEDIR'], base)
        self.assertEqual(values['CCACHE_COMPILERCHECK'], 'content')
        self.assertEqual(values['CMAKE_CXX_COMPILER_LAUNCHER'], 'ccache')
        # ccache hashes the locale: CI's msys2 shell sets LANG and no LC_* variable.
        self.assertEqual(values['LANG'], 'en_US.UTF-8')
        self.assertEqual(values['LC_ALL'], '')
        self.assertEqual(values['GUI_FORMS_STX_PACKAGE_DIR'], base + '/.ci/stx-package')
        self.assertEqual(values['EXTRA'], 'x')

    def test_a_recorded_locale_wins(self) -> None:
        (self.workspace / 'cache-manifest.json').write_text('{"environment": {"LANG": "C.UTF-8"}}',
                                                            encoding='utf-8')
        values: dict[str, str] = restore_local.build_environment(self.product, 'windows-x64', [])
        self.assertEqual(values['LANG'], 'C.UTF-8')

    def test_the_record_and_the_scripts_are_written_and_applied(self) -> None:
        values: dict[str, str] = restore_local.build_environment(self.product, 'windows-x64', [])
        with contextlib.redirect_stdout(io.StringIO()):
            record: Path = restore_local.write_build_environment(self.product, values)
        powershell: str = (self.workspace / restore_local.BUILD_ENVIRONMENT_POWERSHELL).read_text(encoding='utf-8')
        shell: str = (self.workspace / restore_local.BUILD_ENVIRONMENT_SHELL).read_text(encoding='utf-8')
        self.assertIn("$env:LANG = 'en_US.UTF-8'", powershell)
        self.assertIn('Remove-Item -ErrorAction SilentlyContinue env:LC_ALL', powershell)
        self.assertIn('export LANG=en_US.UTF-8', shell)
        self.assertIn('unset LC_ALL', shell)
        applied: dict[str, str] = restore_local.apply_build_environment(record, {'LC_ALL': 'C', 'PATH': 'p'})
        self.assertNotIn('LC_ALL', applied)
        self.assertEqual(applied['LANG'], 'en_US.UTF-8')

    def test_a_bad_name_is_refused(self) -> None:
        with self.assertRaisesRegex(restore_local.RestoreError, 'Not an environment variable name'):
            restore_local.build_environment(self.product, 'linux-x64', [('lower', 'x')])


class ProducerRootTests(unittest.TestCase):
    def test_the_producer_root_needs_an_msys64_name(self) -> None:
        self.assertEqual(restore_local.producer_root({'producer': {'msys_root': 'D:\\a\\_temp\\msys64'}}),
                         Path('D:\\a\\_temp\\msys64'))
        self.assertIsNone(restore_local.producer_root({'producer': {'msys_root': 'D:\\a\\_temp\\other'}}))
        self.assertIsNone(restore_local.producer_root({}))

    def test_a_free_root_is_used_and_an_occupied_one_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory) / 'temp' / 'msys64'
            manifest: dict[str, Any] = {'compiler_sha256': '0' * 64}
            self.assertTrue(restore_local.usable_producer_root(root, manifest)[0])
            root.mkdir(parents=True)
            (root / 'something').write_text('x', encoding='utf-8')
            usable: bool
            reason: str
            usable, reason = restore_local.usable_producer_root(root, manifest)
            self.assertFalse(usable)
            self.assertIn('holds another toolchain', reason)
            compiler: Path = root.parent / restore_local.windows_toolchain.COMPILER_MEMBER
            compiler.parent.mkdir(parents=True)
            compiler.write_bytes(b'clang')
            manifest = {'compiler_sha256': hashlib.sha256(b'clang').hexdigest()}
            self.assertEqual(restore_local.usable_producer_root(root, manifest), (True, 'holds this toolchain'))


if __name__ == '__main__':
    unittest.main()
