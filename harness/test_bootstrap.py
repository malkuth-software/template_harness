"""Unit tests for bootstrap.py with local bare repositories standing in for GitHub (no network).

A temporary `github/` directory holds malkuth-software/gui_forms.git,
notes.git (main and the gui_forms side branch) and, for some tests,
projects.git; --github-base points the bootstrap at it. The project file is
projects/gui_forms.toml with a smoke command that records its environment.
Git runs with no system or global configuration and a fixed identity.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import harness_testing
import bootstrap
import restore_local

GIT_ENVIRONMENT: dict[str, str] = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
# Writes the workspace and project variables the smoke step sees to smoke.json in its working directory.
SMOKE: str = ("smoke = ['{python}', '-c', 'import json, os; open(\"smoke.json\", \"w\").write(json.dumps("
              "[os.environ[\"HARNESS_WORKSPACE\"], os.environ[\"HARNESS_PROJECT\"]]))']")


def git(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def commit_file(clone: Path, name: str, text: str) -> None:
    (clone / name).write_text(text, encoding='utf-8')
    git(clone, 'add', name)
    git(clone, 'commit', '-q', '-m', 'Write ' + name)


def make_remote(github: Path, repository: str, branches: dict[str, str]) -> Path:
    """A bare repository github/<repository>.git whose branches each hold one file, all from one main commit."""
    bare: Path = github / (repository + '.git')
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)], check=True)
    seed: Path = github / 'seeds' / repository
    subprocess.run(['git', 'clone', '-q', str(bare), str(seed)], check=True, capture_output=True)
    git(seed, 'switch', '-q', '-c', 'main')
    commit_file(seed, 'README.md', repository + '\n')
    git(seed, 'push', '-q', 'origin', 'main')
    branch: str
    for branch in branches:
        if branch != 'main':
            git(seed, 'switch', '-q', '-c', branch, 'main')
        commit_file(seed, branches[branch], branch + '\n')
        git(seed, 'push', '-q', 'origin', branch)
    git(seed, 'switch', '-q', 'main')
    return seed


def project_text(notes_branch: str) -> str:
    """projects/gui_forms.toml with the test smoke command and the given notes branch."""
    text: str = harness_testing.REFERENCE_PROJECT.read_text(encoding='utf-8')
    lines: list[str] = []
    line: str
    for line in text.splitlines():
        if line.startswith('smoke = '):
            lines.append(SMOKE)
        elif line.startswith('notes_branch = '):
            lines.append('notes_branch = "' + notes_branch + '"')
        else:
            lines.append(line)
    result: str = '\n'.join(lines) + '\n'
    return result


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        environment: mock._patch_dict = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root: Path = Path(directory.name).resolve()
        self.github: Path = self.root / 'github'
        self.product_seed: Path = make_remote(self.github, 'malkuth-software/gui_forms', {'main': 'product.txt'})
        self.notes_seed: Path = make_remote(self.github, 'malkuth-software/notes',
                                            {'main': 'index.md', 'gui_forms': 'gui_forms.md'})
        self.workspace: Path = self.root / 'workspace'
        self.project_file: Path = self.root / 'gui_forms.toml'
        self.project_file.write_text(project_text('gui_forms'), encoding='utf-8')

    def run_bootstrap(self, *extra: str) -> tuple[int, str, str]:
        output: io.StringIO = io.StringIO()
        errors: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status: int = bootstrap.main(['--workspace', str(self.workspace), '--project-file', str(self.project_file),
                                          '--github-base', str(self.github), *extra])
        return status, output.getvalue(), errors.getvalue()

    def test_a_fresh_workspace_gets_the_product_notes_and_smoke_build(self) -> None:
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--no-restore')
        self.assertEqual(status, 0, errors)
        self.assertEqual(git(self.workspace / 'gui_forms', 'branch', '--show-current'), 'main')
        self.assertTrue((self.workspace / 'gui_forms' / 'product.txt').is_file())
        self.assertEqual(git(self.workspace / 'notes', 'branch', '--show-current'), 'main')
        worktree: Path = self.workspace / 'notes-gui_forms'
        self.assertEqual(git(worktree, 'branch', '--show-current'), 'gui_forms')
        self.assertTrue((worktree / 'gui_forms.md').is_file())
        self.assertEqual(git(worktree, 'rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/gui_forms')
        smoke: list[str] = json.loads((self.workspace / 'smoke.json').read_text(encoding='utf-8'))
        self.assertEqual(smoke, [str(self.workspace), str(self.project_file)])
        self.assertIn('Toolchain and cache restore skipped', output)
        self.assertFalse((self.workspace / 'projects').exists())

    def test_a_rerun_updates_instead_of_cloning(self) -> None:
        self.assertEqual(self.run_bootstrap('--no-restore', '--no-smoke')[0], 0)
        (self.workspace / 'gui_forms' / 'local.txt').write_text('untracked\n', encoding='utf-8')
        commit_file(self.product_seed, 'later.txt', 'later\n')
        git(self.product_seed, 'push', '-q', 'origin', 'main')
        git(self.notes_seed, 'switch', '-q', 'gui_forms')
        commit_file(self.notes_seed, 'later.md', 'later\n')
        git(self.notes_seed, 'push', '-q', 'origin', 'gui_forms')
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--no-restore', '--no-smoke')
        self.assertEqual(status, 0, errors)
        self.assertNotIn('cloning', output)
        self.assertIn('the product: updating', output)
        self.assertEqual(git(self.workspace / 'gui_forms', 'rev-parse', 'HEAD'),
                         git(self.product_seed, 'rev-parse', 'main'))
        self.assertTrue((self.workspace / 'gui_forms' / 'local.txt').is_file())
        self.assertTrue((self.workspace / 'notes-gui_forms' / 'later.md').is_file())

    def test_a_checkout_on_another_branch_is_left_as_it_is(self) -> None:
        self.assertEqual(self.run_bootstrap('--no-restore', '--no-smoke')[0], 0)
        checkout: Path = self.workspace / 'gui_forms'
        git(checkout, 'switch', '-q', '-c', 'work')
        before: str = git(checkout, 'rev-parse', 'HEAD')
        commit_file(self.product_seed, 'later.txt', 'later\n')
        git(self.product_seed, 'push', '-q', 'origin', 'main')
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--no-restore', '--no-smoke')
        self.assertEqual(status, 0, errors)
        self.assertIn('is on work, not main; left as it is', output)
        self.assertEqual(git(checkout, 'rev-parse', 'HEAD'), before)
        self.assertEqual(git(checkout, 'branch', '--show-current'), 'work')

    def test_a_dry_run_prints_every_step_and_changes_nothing(self) -> None:
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--dry-run', '--in-house', '--platform', 'linux-x64')
        self.assertEqual(status, 0, errors)
        self.assertFalse(self.workspace.exists())
        lines: list[str] = []
        line: str
        for line in output.splitlines():
            if line.startswith('would run: '):
                lines.append(line)
        self.assertEqual(len(lines), 7, output)
        self.assertIn('git clone --quiet', lines[0])
        self.assertIn('malkuth-software/gui_forms.git', lines[0])
        self.assertIn('--branch main', lines[1])
        self.assertIn('worktree add --track -b gui_forms', lines[2])
        self.assertIn('malkuth-software/projects.git', lines[3])
        # shlex quotes a path with backslashes, so the script name may be followed by a quote.
        self.assertRegex(lines[4], r"restore_local\.py'? toolchain --platform linux-x64$")
        self.assertRegex(lines[5], r"restore_local\.py'? cache --platform linux-x64$")
        self.assertIn('smoke.json', lines[6])

    def test_in_house_without_access_stops_clearly(self) -> None:
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--in-house', '--no-restore', '--no-smoke')
        self.assertEqual(status, 1)
        self.assertIn('malkuth-software/projects is private', errors)
        make_remote(self.github, 'malkuth-software/projects', {'main': 'tracks.md'})
        status, output, errors = self.run_bootstrap('--in-house', '--no-restore', '--no-smoke')
        self.assertEqual(status, 0, errors)
        self.assertTrue((self.workspace / 'projects' / 'tracks.md').is_file())

    def test_a_missing_notes_branch_stops(self) -> None:
        self.project_file.write_text(project_text('absent'), encoding='utf-8')
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--no-restore', '--no-smoke')
        self.assertEqual(status, 1)
        self.assertIn('no branch absent', errors)

    def test_the_platform_name_is_a_ci_platform(self) -> None:
        self.assertIn(bootstrap.host_platform_name(), bootstrap.PLATFORMS)
        self.assertEqual(bootstrap.repository_url('https://github.com/', 'owner/name'),
                         'https://github.com/owner/name.git')

    def test_the_restored_toolchain_comes_first_on_the_steps_path(self) -> None:
        record: Path = self.workspace / restore_local.TOOLCHAIN_PATH_RECORD
        record.parent.mkdir(parents=True)
        record.write_text('/toolchain/clang64/bin\n/toolchain/usr/bin\n', encoding='utf-8')
        self.assertEqual(bootstrap.TOOLCHAIN_PATH_RECORD, restore_local.TOOLCHAIN_PATH_RECORD)
        plan: bootstrap.Plan = bootstrap.Plan(
            workspace=self.workspace, product=bootstrap.project.load(self.project_file), github=str(self.github),
            platform='windows-x64', in_house=False, restore=False, smoke=False, dry_run=False)
        harness: bootstrap.PinnedHarness = bootstrap.PinnedHarness(bootstrap.REPOSITORY, plan.product)
        environment: dict[str, str] = bootstrap.step_environment(plan, harness)
        self.assertEqual(environment['PATH'].split(os.pathsep)[:2], ['/toolchain/clang64/bin', '/toolchain/usr/bin'])
        self.assertEqual(environment['HARNESS_WORKSPACE'], str(self.workspace))

    def test_a_root_layout_product_must_ignore_what_the_restore_creates(self) -> None:
        directory: str
        for directory in (restore_local.TOOLCHAIN_DIRECTORY, restore_local.DOWNLOAD_DIRECTORY,
                          str(Path(restore_local.CACHE_RECORD).parent), Path(restore_local.MACOS_RUNTIME).parts[0]):
            self.assertIn(directory + '/', bootstrap.ROOT_LAYOUT_ENTRIES)

    def test_a_locked_product_gets_its_pinned_harness(self) -> None:
        # A harness repository whose second commit carries the project file the steps must use.
        harness_seed: Path = make_remote(self.github, 'malkuth-software/template_harness', {'main': 'first.txt'})
        (harness_seed / 'projects').mkdir()
        (harness_seed / 'projects' / 'gui_forms.toml').write_text(project_text('gui_forms'), encoding='utf-8')
        git(harness_seed, 'add', 'projects')
        git(harness_seed, 'commit', '-q', '-m', 'The project file')
        pinned: str = git(harness_seed, 'rev-parse', 'HEAD')
        commit_file(harness_seed, 'later.txt', 'after the pin\n')
        git(harness_seed, 'push', '-q', 'origin', 'main')
        commit_file(self.product_seed, 'harness.lock',
                    '[harness]\nrepository = "malkuth-software/template_harness"\ncommit = "' + pinned + '"\n')
        git(self.product_seed, 'push', '-q', 'origin', 'main')
        status: int
        output: str
        errors: str
        status, output, errors = self.run_bootstrap('--no-restore')
        self.assertEqual(status, 0, errors)
        harness: Path = self.workspace / 'template_harness'
        self.assertEqual(git(harness, 'rev-parse', 'HEAD'), pinned)
        self.assertFalse((harness / 'later.txt').exists())
        smoke: list[str] = json.loads((self.workspace / 'smoke.json').read_text(encoding='utf-8'))
        self.assertEqual(smoke, [str(self.workspace), str(harness / 'projects' / 'gui_forms.toml')])
        self.assertIn('(harness.lock)', output)


if __name__ == '__main__':
    unittest.main()
