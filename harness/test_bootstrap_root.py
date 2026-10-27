"""bootstrap.py for a product at the workspace root (source_dir "."), with local bare repositories.

The product is cloned into the workspace directory itself; the pinned
harness, the notes and their worktree then sit inside the product's
checkout, which must ignore them. test_bootstrap.py has the fixture.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest import mock

import harness_testing  # noqa: F401  (the reference project and workspace, first)
import bootstrap
from test_bootstrap import GIT_ENVIRONMENT, commit_file, git, make_remote, project_text

# What a root-layout product's .gitignore lists (docs/adopting.md).
IGNORED: str = '''/template_harness/
/notes/
/notes-*/
/projects/
/.build/
/.ccache/
/.ci/
/.toolchain/
/.bootstrap/
/smoke.json
'''


def root_project_text(notes_branch: str) -> str:
    """The test project file with the product at the workspace root."""
    text: str = project_text(notes_branch)
    original: str = 'source_dir = "gui_forms"'
    if text.count(original) != 1:
        raise AssertionError('the reference project names its source_dir once')
    result: str = text.replace(original, 'source_dir = "."')
    return result


def lock_text(commit: str) -> str:
    lines: list[str] = ['[harness]', 'repository = "malkuth-software/template_harness"', 'commit = "' + commit + '"', '']
    result: str = '\n'.join(lines)
    return result


class RootLayoutBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        environment: mock._patch_dict = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root: Path = Path(directory.name).resolve()
        self.github: Path = self.root / 'github'
        self.product_seed: Path = make_remote(self.github, 'malkuth-software/gui_forms', {'main': 'product.txt'})
        make_remote(self.github, 'malkuth-software/notes', {'main': 'index.md', 'gui_forms': 'gui_forms.md'})
        self.workspace: Path = self.root / 'workspace'
        self.project_file: Path = self.root / 'gui_forms.toml'
        self.project_file.write_text(root_project_text('gui_forms'), encoding='utf-8')

    def run_bootstrap(self, *extra: str) -> tuple[int, str, str]:
        output: io.StringIO = io.StringIO()
        errors: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status: int = bootstrap.main(['--workspace', str(self.workspace), '--project-file', str(self.project_file),
                                          '--github-base', str(self.github), *extra])
        return status, output.getvalue(), errors.getvalue()

    def pin_a_harness(self) -> str:
        """A harness repository holding the project file, and the product's lock on it; returns the commit."""
        harness_seed: Path = make_remote(self.github, 'malkuth-software/template_harness', {'main': 'first.txt'})
        (harness_seed / 'projects').mkdir()
        (harness_seed / 'projects' / 'gui_forms.toml').write_text(root_project_text('gui_forms'), encoding='utf-8')
        git(harness_seed, 'add', 'projects')
        git(harness_seed, 'commit', '-q', '-m', 'The project file')
        git(harness_seed, 'push', '-q', 'origin', 'main')
        pinned: str = git(harness_seed, 'rev-parse', 'HEAD')
        commit_file(self.product_seed, 'harness.lock', lock_text(pinned))
        git(self.product_seed, 'push', '-q', 'origin', 'main')
        return pinned

    def test_the_product_is_cloned_into_the_workspace_itself(self) -> None:
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--no-restore')
        self.assertEqual(status, 0, errors)
        self.assertEqual(git(self.workspace, 'rev-parse', '--show-toplevel'), self.workspace.as_posix())
        self.assertTrue((self.workspace / 'product.txt').is_file())
        self.assertFalse((self.workspace / '.' / '.git').is_file())
        # The notes and their worktree sit inside the product's checkout.
        self.assertEqual(git(self.workspace / 'notes', 'branch', '--show-current'), 'main')
        self.assertEqual(git(self.workspace / 'notes-gui_forms', 'branch', '--show-current'), 'gui_forms')
        smoke: list[str] = json.loads((self.workspace / 'smoke.json').read_text(encoding='utf-8'))
        self.assertEqual(smoke[0], str(self.workspace))

    def test_entries_the_checkout_does_not_ignore_are_warned_about(self) -> None:
        output: str = self.run_bootstrap('--no-restore', '--no-smoke')[1]
        warning: list[str] = []
        line: str
        for line in output.splitlines():
            if line.startswith('warning: the product is at the workspace root'):
                warning.append(line)
        self.assertEqual(len(warning), 1, output)
        entry: str
        for entry in ('template_harness/', 'notes/', 'notes-gui_forms/', '.build/', '.ccache/', '.ci/', '.toolchain/',
                      '.bootstrap/'):
            self.assertIn(entry, warning[0])
        self.assertNotIn('projects/', warning[0])

    def test_an_ignoring_checkout_gets_its_harness_inside_and_no_warning(self) -> None:
        commit_file(self.product_seed, '.gitignore', IGNORED)
        git(self.product_seed, 'push', '-q', 'origin', 'main')
        pinned: str = self.pin_a_harness()
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--no-restore')
        self.assertEqual(status, 0, errors)
        self.assertNotIn('warning:', output)
        harness: Path = self.workspace / 'template_harness'
        self.assertEqual(git(harness, 'rev-parse', 'HEAD'), pinned)
        self.assertEqual(git(harness, 'rev-parse', '--show-toplevel'), harness.as_posix())
        # Everything the bootstrap put inside the checkout is ignored: the checkout is clean.
        self.assertEqual(git(self.workspace, 'status', '--porcelain'), '')
        smoke: list[str] = json.loads((self.workspace / 'smoke.json').read_text(encoding='utf-8'))
        self.assertEqual(smoke, [str(self.workspace), str(harness / 'projects' / 'gui_forms.toml')])
        # A re-run updates the root checkout in place.
        status, output, errors = self.run_bootstrap('--no-restore', '--no-smoke')
        self.assertEqual(status, 0, errors)
        self.assertIn('the product: updating ' + str(self.workspace), output)

    def test_a_workspace_holding_other_files_is_not_cloned_over(self) -> None:
        self.workspace.mkdir()
        (self.workspace / 'stray.txt').write_text('mine\n', encoding='utf-8')
        status: int
        errors: str
        status, _, errors = self.run_bootstrap('--no-restore')
        self.assertEqual(status, 1)
        self.assertIn('is not empty and is not a git clone', errors)
        names: list[str] = []
        entry: Path
        for entry in self.workspace.iterdir():
            names.append(entry.name)
        self.assertEqual(names, ['stray.txt'])

    def test_a_dry_run_clones_into_the_workspace_and_skips_a_cache_the_project_lacks(self) -> None:
        text: str = root_project_text('gui_forms')
        original: str = 'cache_asset_prefix = "gui-forms-cache-"\n'
        self.assertEqual(text.count(original), 1)
        self.project_file.write_text(text.replace(original, ''), encoding='utf-8')
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--dry-run', '--platform', 'linux-x64')
        self.assertEqual(status, 0, errors)
        self.assertFalse(self.workspace.exists())
        clone: list[str] = ['git', 'clone', '--quiet',
                            bootstrap.repository_url(str(self.github), 'malkuth-software/gui_forms'), str(self.workspace)]
        self.assertIn('would run: ' + shlex.join(clone) + '\n', output)
        self.assertIn(' toolchain --platform linux-x64', output)
        self.assertNotIn(' cache --platform', output)
        self.assertIn('Cache restore skipped (the project publishes no compiler caches', output)


if __name__ == '__main__':
    unittest.main()
