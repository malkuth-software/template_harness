"""The names projects/books.toml produces, literally (Pocket Library, malkuth-software/books).

Each expected value is a name on disk or on GitHub: a build tree, an
environment variable, a workflow artifact, a compiler-cache asset, the
shared Windows toolchain's assets or an Ubuntu package profile. If one of
these assertions fails, an edit to the project file would rename it and
start a new history; change an expected value only deliberately.
"""
from __future__ import annotations
from pathlib import Path
import unittest

import harness_testing
import project

BOOKS_PROJECT: Path = Path(__file__).resolve().parent.parent / 'projects' / 'books.toml'
PLATFORMS: tuple[str, ...] = ('linux-x64', 'linux-arm64', 'macos-arm64', 'windows-x64')
TREE: str = '0123456789abcdef0123456789abcdef01234567'
ENVIRONMENT: str = 'f' * 64


class BooksProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.books: project.Project = project.load(BOOKS_PROJECT)

    def test_product_and_layout(self) -> None:
        self.assertEqual(self.books.name, 'books')
        self.assertEqual(self.books.repository, 'malkuth-software/books')
        self.assertEqual(self.books.source_dir, '.')
        self.assertTrue(self.books.at_workspace_root())
        self.assertEqual(self.books.build_tree('linux-x64'), '.build/native-linux-x64/books')
        self.assertEqual(self.books.build_tree('windows-x64', sanitize=True),
                         '.build/native-windows-x64/books-sanitize')
        self.assertEqual(self.books.native_workflow, '.github/workflows/applications.yml')
        self.assertEqual(self.books.native_job, 'native')
        self.assertTrue(self.books.native_job_matches('native (ubuntu-24.04, linux-x64, bash)', 'linux-x64'))
        self.assertFalse(self.books.native_job_matches('native (ubuntu-24.04, linux-x64, bash)', 'linux-arm64'))

    def test_environment_variables(self) -> None:
        expected: dict[str, str] = {
            'LLVM_RUNTIME': 'BOOKS_LLVM_RUNTIME', 'PLATFORM': 'BOOKS_PLATFORM',
            'COMPILER_ID': 'BOOKS_COMPILER_ID', 'RUNNER': 'BOOKS_RUNNER',
            'SANITIZER_DEBUG_INFO': 'BOOKS_SANITIZER_DEBUG_INFO',
            'PACKAGE_WINDOWS_TOOLCHAIN': 'BOOKS_PACKAGE_WINDOWS_TOOLCHAIN'}
        name: str
        for name in expected:
            self.assertEqual(self.books.variable(name), expected[name])
        self.assertEqual(self.books.configure_variables(), ())

    def test_workflow_artifacts(self) -> None:
        kind: str
        for kind in ('validated', 'inputs'):
            self.assertEqual(self.books.artifact(kind + '-linux-x64-' + TREE + '-' + ENVIRONMENT),
                             'books-' + kind + '-linux-x64-' + TREE + '-' + ENVIRONMENT)
        self.assertEqual(self.books.artifact('tree-docs-' + TREE), 'books-tree-docs-' + TREE)
        self.assertEqual(self.books.artifact('sanitize-ccache-linux-x64'), 'books-sanitize-ccache-linux-x64')

    def test_cache_and_toolchain_assets(self) -> None:
        expected: dict[str, str] = {
            'linux-x64': 'books-cache-linux-x64.tar.gz', 'linux-arm64': 'books-cache-linux-arm64.tar.gz',
            'macos-arm64': 'books-cache-macos-arm64.tar.gz', 'windows-x64': 'books-cache-windows-x64.tar.gz'}
        platform: str
        for platform in PLATFORMS:
            self.assertEqual(self.books.cache_asset(platform), expected[platform])
        # The Windows toolchain is GUI.Forms' published one, shared as published.
        self.assertEqual(self.books.toolchain_repository, 'malkuth-software/gui_forms')
        self.assertEqual(self.books.toolchain_asset_prefix + 'windows-x64.tar.zst',
                         'gui-forms-toolchain-windows-x64.tar.zst')
        self.assertEqual(self.books.toolchain_asset_prefix + 'windows-x64.json',
                         'gui-forms-toolchain-windows-x64.json')

    def test_linux_package_profiles(self) -> None:
        consumer: str = ('cmake ninja-build ccache clang-22 generate-ninja pkg-config '
                         'libx11-dev libxext-dev libxrandr-dev libxcursor-dev libxfixes-dev libxi-dev '
                         'libatk1.0-dev libatk-bridge2.0-dev libasound2t64 xvfb xauth')
        self.assertEqual(' '.join(self.books.linux_packages['consumer']), consumer)
        self.assertEqual(' '.join(self.books.linux_packages['programs']), consumer + ' patchelf dpkg-dev')
        self.assertEqual(sorted(self.books.linux_packages), ['consumer', 'programs'])

    def test_state_machine_scope(self) -> None:
        self.assertTrue(self.books.has_state_machines)
        self.assertEqual(self.books.first_party_dirs, ('src', 'tests', 'books', 'shared', 'new-games'))
        self.assertEqual(self.books.vocabulary_headers, ())
        self.assertEqual(self.books.full_tree_triggers, ('harness.lock',))

    def test_cache_manifest_options(self) -> None:
        shared: list[tuple[str, str | int | bool]] = [
            ('build_type', 'Release'), ('cxx_standard', 20), ('audio', True), ('text_masks', True),
            ('harfbuzz', True), ('skia', True)]
        platform: str
        for platform in PLATFORMS:
            expected: list[tuple[str, str | int | bool]] = list(shared)
            if platform == 'windows-x64':
                expected[5] = ('skia', False)
            self.assertEqual(list(self.books.native_options_for(platform).items()), expected, platform)
        self.assertEqual(self.books.cache_profile, 'Pocket Library native Release against the pinned GUI.Forms source')

    def test_bootstrap(self) -> None:
        bootstrap: project.BootstrapTable | None = self.books.bootstrap
        self.assertIsNotNone(bootstrap)
        if bootstrap is None:
            return
        self.assertEqual(bootstrap.notes_repository, 'malkuth-software/notes')
        self.assertEqual(bootstrap.notes_branch, 'books')
        self.assertEqual(bootstrap.projects_repository, 'malkuth-software/projects')
        # No smoke build yet: Pocket Library's build driver arrives with the SDK port.
        self.assertEqual(bootstrap.smoke, ())


if __name__ == '__main__':
    unittest.main()
