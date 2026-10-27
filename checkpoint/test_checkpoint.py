"""Unit tests for checkpoint.py on temporary repositories (no network, no gh).

Each test builds a bare `origin` with one commit on main, two worker clones
with a topic branch each, a third worker whose topic conflicts with the
first, and a marked integration tree cloned from origin. Git runs with no
system or global configuration and a fixed identity.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import checkpoint

TODAY: str = '2026-10-27'
PASSING: tuple[str, ...] = (sys.executable, '-c', 'import sys; sys.exit(0)')
FAILING: tuple[str, ...] = (sys.executable, '-c', 'import sys; sys.exit(3)')
# Appends to a tracked file of the tree the verification is given.
EDITING: tuple[str, ...] = (sys.executable, '-c',
                            'import os, pathlib; path = pathlib.Path(os.environ["HARNESS_CHECKPOINT_TREE"]) / "base.txt";'
                            ' path.write_text(path.read_text() + "edited\\n")')
GIT_ENVIRONMENT: dict[str, str] = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}


def git(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def commit_file(clone: Path, name: str, text: str, subject: str) -> str:
    (clone / name).write_text(text, encoding='utf-8')
    git(clone, 'add', name)
    git(clone, 'commit', '-q', '-m', subject)
    return git(clone, 'rev-parse', 'HEAD')


class GhCalls:
    """Stands in for checkpoint.run_gh, recording each call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, arguments: list[str]) -> None:
        self.calls.append(list(arguments))


CLONES: tuple[str, ...] = ('first', 'second', 'third', 'integration/product')


def make_worker(root: Path, name: str, branch: str, file_name: str, subject: str) -> Path:
    clone: Path = root / name
    subprocess.run(['git', 'clone', '-q', str(root / 'origin.git'), str(clone)], check=True, capture_output=True)
    git(clone, 'switch', '-q', '-c', branch)
    commit_file(clone, file_name, name + '\n', subject)
    return clone


def make_fixture(root: Path) -> None:
    """origin.git, the three workers and the marked integration tree, under root."""
    origin: Path = root / 'origin.git'
    seed: Path = root / 'seed'
    subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(origin)], check=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(seed)], check=True)
    commit_file(seed, 'base.txt', 'one\ntwo\nthree\n', 'Base')
    git(seed, 'push', '-q', str(origin), 'main')
    first: Path = make_worker(root, 'first', 'topic/first', 'first.txt', 'First topic, part one')
    commit_file(first, 'first.txt', 'first\nmore\n', 'First topic, part two')
    make_worker(root, 'second', 'topic/second', 'second.txt', 'Second topic')
    third: Path = make_worker(root, 'third', 'topic/third', 'other.txt', 'Third topic')
    # The conflicting worker edits the line the first topic also edits.
    commit_file(first, 'base.txt', 'one\nfirst\nthree\n', 'First topic edits the base')
    commit_file(third, 'base.txt', 'one\nthird\nthree\n', 'Third topic edits the base')
    settings: checkpoint.Settings = checkpoint.parse_settings(
        {'tree': 'integration/product', 'verify': list(PASSING)}, root, 'owner/product')
    with contextlib.redirect_stdout(io.StringIO()):
        checkpoint.init_tree(settings, str(origin))


class CheckpointTests(unittest.TestCase):
    """Each test copies one fixture made for the class (git process starts dominate the run time)."""

    @classmethod
    def setUpClass(cls) -> None:
        environment: mock._patch_dict = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        cls.addClassCleanup(environment.stop)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.template: Path = Path(directory.name).resolve() / 'template'
        cls.template.mkdir()
        make_fixture(cls.template)

    def setUp(self) -> None:
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root: Path = Path(directory.name).resolve() / 'fixture'
        shutil.copytree(self.template, self.root)
        self.origin: Path = self.root / 'origin.git'
        name: str
        for name in CLONES:
            git(self.root / name, 'remote', 'set-url', 'origin', str(self.origin))
        self.first: Path = self.root / 'first'
        self.second: Path = self.root / 'second'
        self.third: Path = self.root / 'third'
        self.tree: Path = self.root / 'integration' / 'product'
        self.settings: checkpoint.Settings = self.settings_with(PASSING)

    def settings_with(self, verify: tuple[str, ...]) -> checkpoint.Settings:
        table: dict[str, object] = {'tree': 'integration/product', 'verify': list(verify)}
        settings: checkpoint.Settings = checkpoint.parse_settings(table, self.root, 'owner/product')
        return settings

    def quietly(self, function: object, *arguments: object) -> object:
        with contextlib.redirect_stdout(io.StringIO()):
            result: object = function(*arguments)  # type: ignore[operator]
        return result

    def fold_two(self) -> dict:
        record: dict = self.quietly(checkpoint.fold, self.settings,
                                    [str(self.first) + ':topic/first', str(self.second) + ':topic/second'], [])
        return record

    def test_two_topics_fold_into_one_commit_each(self) -> None:
        heads: list[str] = [git(self.first, 'rev-parse', 'topic/first'), git(self.second, 'rev-parse', 'topic/second')]
        record: dict = self.fold_two()
        main: str = git(self.origin, 'rev-parse', 'main')
        subjects: str = git(self.tree, 'log', '--format=%s', main + '..HEAD')
        self.assertEqual(subjects.splitlines(), ['Second topic', 'First topic, part one'])
        body: str = git(self.tree, 'log', '-1', '--format=%b', 'HEAD~1')
        self.assertIn('Folded from topic/first in ' + str(self.first) + ' at ' + heads[0], body)
        self.assertIn('First topic, part two', body)
        self.assertIn('First topic edits the base', body)
        self.assertEqual(git(self.tree, 'show', 'HEAD~1:first.txt'), 'first\nmore')
        self.assertEqual(record['base_commit'], main)
        log: str = (Path(git(self.tree, 'rev-parse', '--absolute-git-dir')) / 'checkpoint' / 'fold-log.md').read_text(
            encoding='utf-8')
        self.assertIn('- **First topic, part one** (`topic/first`', log)
        self.assertIn('- **Second topic** (`topic/second`', log)
        self.assertIn('  - `' + heads[1][:12] + '` Second topic', log)
        # The workers' branches are untouched.
        self.assertEqual(git(self.first, 'rev-parse', 'topic/first'), heads[0])
        self.assertEqual(git(self.second, 'rev-parse', 'topic/second'), heads[1])

    def test_a_title_replaces_the_first_subject(self) -> None:
        self.quietly(checkpoint.fold, self.settings, [str(self.second) + ':topic/second'], ['Named topic'])
        self.assertEqual(git(self.tree, 'log', '-1', '--format=%s'), 'Named topic')
        self.quietly(checkpoint.fold, self.settings,
                     [str(self.first) + ':topic/first', str(self.second) + ':topic/second'], ['2=Second, renamed'])
        self.assertEqual(git(self.tree, 'log', '-2', '--format=%s').splitlines(),
                         ['Second, renamed', 'First topic, part one'])
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'N=TEXT'):
            self.quietly(checkpoint.fold, self.settings,
                         [str(self.first) + ':topic/first', str(self.second) + ':topic/second'], ['Ambiguous'])

    def test_a_conflicting_topic_stops_and_resets_the_tree(self) -> None:
        self.fold_two()
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'topic/third[\\s\\S]*base.txt'):
            self.quietly(checkpoint.fold, self.settings,
                         [str(self.first) + ':topic/first', str(self.third) + ':topic/third'], [])
        self.assertEqual(git(self.tree, 'rev-parse', 'HEAD'), git(self.origin, 'rev-parse', 'main'))
        self.assertEqual(git(self.tree, 'status', '--porcelain'), '')
        state: Path = Path(git(self.tree, 'rev-parse', '--absolute-git-dir')) / 'checkpoint'
        self.assertFalse((state / 'fold.json').exists())
        self.assertFalse((state / 'fold-log.md').exists())
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'No fold'):
            self.quietly(checkpoint.verify, self.settings, self.root)

    def test_a_topic_already_on_main_is_skipped(self) -> None:
        git(self.second, 'push', '-q', 'origin', 'topic/second:main')
        record: dict = self.quietly(checkpoint.fold, self.settings,
                                    [str(self.first) + ':topic/first', str(self.second) + ':topic/second'], [])
        self.assertIsNone(record['topics'][1]['folded'])
        self.assertEqual(git(self.tree, 'log', '--format=%s', git(self.origin, 'rev-parse', 'main') + '..HEAD'),
                         'First topic, part one')

    def test_an_unmarked_tree_or_a_worker_clone_is_refused(self) -> None:
        git(self.tree, 'config', '--local', '--unset', checkpoint.TREE_MARK)
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'not marked'):
            self.fold_two()
        worker: checkpoint.Settings = checkpoint.parse_settings(
            {'tree': str(self.first), 'verify': list(PASSING)}, self.root, 'owner/product')
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'not marked'):
            self.quietly(checkpoint.fold, worker, [str(self.second) + ':topic/second'], [])
        self.assertEqual(git(self.first, 'branch', '--show-current'), 'topic/first')

    def test_a_failed_verification_blocks_the_push(self) -> None:
        self.fold_two()
        failing: checkpoint.Settings = self.settings_with(FAILING)
        self.assertFalse(self.quietly(checkpoint.verify, failing, self.root))
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'verification failed'):
            self.quietly(checkpoint.push, failing, True, TODAY)

    def test_an_unverified_fold_is_not_pushed(self) -> None:
        self.fold_two()
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'not verified'):
            self.quietly(checkpoint.push, self.settings, True, TODAY)

    def test_a_tree_changed_after_verification_is_not_pushed(self) -> None:
        self.fold_two()
        self.assertTrue(self.quietly(checkpoint.verify, self.settings, self.root))
        commit_file(self.tree, 'late.txt', 'late\n', 'A late commit')
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'changed since it was verified'):
            self.quietly(checkpoint.push, self.settings, True, TODAY)

    def test_a_verification_that_edits_the_tree_fails(self) -> None:
        self.fold_two()
        editing: checkpoint.Settings = self.settings_with(EDITING)
        self.assertFalse(self.quietly(checkpoint.verify, editing, self.root))
        record: dict = json.loads((Path(git(self.tree, 'rev-parse', '--absolute-git-dir')) / 'checkpoint'
                                   / 'verification.json').read_text(encoding='utf-8'))
        self.assertEqual(record['reason'], 'the verification changed the tree')

    def test_push_dry_run_prints_the_commands_and_pushes_nothing(self) -> None:
        self.fold_two()
        self.quietly(checkpoint.verify, self.settings, self.root)
        git(self.tree, 'push', '-q', 'origin', 'HEAD:refs/heads/checkpoint/' + TODAY + '-1')
        printed: io.StringIO = io.StringIO()
        gh: GhCalls = GhCalls()
        with contextlib.redirect_stdout(printed), mock.patch.object(checkpoint, 'run_gh', gh.run):
            checkpoint.push(self.settings, True, TODAY)
        lines: list[str] = printed.getvalue().splitlines()
        self.assertEqual(lines[0], 'git -C ' + shlex.quote(str(self.tree)) + ' push origin HEAD:refs/heads/checkpoint/' + TODAY + '-2')
        self.assertTrue(lines[1].startswith('gh pr create --repo owner/product --base main --head checkpoint/'
                                            + TODAY + "-2 --title 'Checkpoint " + TODAY + "-2: 2 topics' --body-file "))
        self.assertEqual(lines[2], 'gh pr merge checkpoint/' + TODAY + '-2 --repo owner/product --auto --merge')
        self.assertEqual(gh.calls, [])
        self.assertEqual(git(self.origin, 'branch', '--list', 'checkpoint/' + TODAY + '-2'), '')

    def test_push_then_after_merge_lists_the_contained_topics(self) -> None:
        self.fold_two()
        self.quietly(checkpoint.verify, self.settings, self.root)
        gh: GhCalls = GhCalls()
        with mock.patch.object(checkpoint, 'run_gh', gh.run):
            self.quietly(checkpoint.push, self.settings, False, TODAY)
        branch: str = 'checkpoint/' + TODAY + '-1'
        self.assertEqual(git(self.origin, 'rev-parse', branch), git(self.tree, 'rev-parse', 'HEAD'))
        self.assertEqual([call[:3] for call in gh.calls], [['gh', 'pr', 'create'], ['gh', 'pr', 'merge']])
        self.assertIn('--body-file', gh.calls[0])
        # GitHub merges the PR: main gains a merge of the checkpoint branch.
        merger: Path = self.root / 'merger'
        subprocess.run(['git', 'clone', '-q', str(self.origin), str(merger)], check=True, capture_output=True)
        git(merger, 'merge', '-q', '--no-ff', '-m', 'Merge the checkpoint', 'origin/' + branch)
        git(merger, 'push', '-q', 'origin', 'main')
        commit_file(self.third, 'late.txt', 'late\n', 'Third topic, after the fold')
        printed: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(printed):
            suggestions: list[str] = checkpoint.after_merge(self.settings)
        self.assertEqual(suggestions, ['git -C ' + shlex.quote(str(self.first)) + ' branch -D topic/first',
                                       'git -C ' + shlex.quote(str(self.second)) + ' branch -D topic/second'])
        self.assertIn('topic/first in ' + str(self.first) + ': contained in origin/main', printed.getvalue())
        # Nothing was deleted.
        self.assertNotEqual(git(self.first, 'branch', '--list', 'topic/first'), '')
        self.assertNotEqual(git(self.second, 'branch', '--list', 'topic/second'), '')

    def test_after_merge_names_a_branch_not_yet_on_main(self) -> None:
        self.fold_two()
        printed: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(printed):
            suggestions: list[str] = checkpoint.after_merge(self.settings)
        self.assertEqual(suggestions, [])
        self.assertIn('topic/first in ' + str(self.first) + ': not yet in origin/main', printed.getvalue())


class SettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self) -> None:
        settings: checkpoint.Settings = checkpoint.parse_settings(
            {'tree': 'tree', 'verify': ['true']}, Path('/workspace'), 'owner/product')
        self.assertEqual((settings.base, settings.remote, settings.branch_prefix, settings.merge_method),
                         ('main', 'origin', 'checkpoint/', 'merge'))
        self.assertEqual(settings.tree, (Path('/workspace') / 'tree').resolve())
        with self.assertRaisesRegex(ValueError, 'missing'):
            checkpoint.parse_settings(None, Path('/workspace'), 'owner/product')
        with self.assertRaisesRegex(ValueError, 'verify'):
            checkpoint.parse_settings({'tree': 'tree', 'verify': 'true'}, Path('/workspace'), 'owner/product')
        with self.assertRaisesRegex(ValueError, 'merge_method'):
            checkpoint.parse_settings({'tree': 'tree', 'verify': ['true'], 'merge_method': 'fast'},
                                      Path('/workspace'), 'owner/product')

    def test_the_reference_project_names_its_checkpoint(self) -> None:
        reference: Path = Path(__file__).resolve().parents[1] / 'projects' / 'gui_forms.toml'
        loaded: checkpoint.project.Project = checkpoint.project.load(reference)
        self.assertIsNotNone(loaded.checkpoint)
        settings: checkpoint.Settings = checkpoint.settings_from(loaded.checkpoint, Path('/workspace'), loaded.repository)
        self.assertEqual(settings.verify, ('sh', 'build.sh', 'all'))
        self.assertEqual(settings.tree, (Path('/workspace') / 'gui_forms').resolve())

    def test_topics_split_at_the_last_colon(self) -> None:
        with self.assertRaisesRegex(checkpoint.CheckpointError, '<clone>:<branch>'):
            checkpoint.parse_topic('no-branch')
        with tempfile.TemporaryDirectory() as directory:
            clone: Path = Path(directory).resolve()
            subprocess.run(['git', 'init', '-q', str(clone)], check=True)
            topic: checkpoint.Topic = checkpoint.parse_topic(str(clone) + ':topic/a')
            self.assertEqual((topic.clone, topic.branch), (clone, 'topic/a'))
            with self.assertRaisesRegex(checkpoint.CheckpointError, 'not a branch name'):
                checkpoint.parse_topic(str(clone) + ':bad..name')



# Writes the tree and the working directory the verification sees to verified.json there.
RECORDING: tuple[str, ...] = (sys.executable, '-c',
                              'import json, os; open("verified.json", "w").write(json.dumps('
                              '[os.environ["HARNESS_CHECKPOINT_TREE"], os.getcwd()]))')


class RootLayoutTests(unittest.TestCase):
    """A product whose checkout is the workspace root: [checkpoint] tree = "."."""

    def setUp(self) -> None:
        environment: mock._patch_dict = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root: Path = Path(directory.name).resolve()
        self.origin: Path = self.root / 'origin.git'
        seed: Path = self.root / 'seed'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.origin)], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(seed)], check=True)
        commit_file(seed, 'base.txt', 'one\n', 'Base')
        commit_file(seed, '.gitignore', '/template_harness/\n/verified.json\n', 'Ignore the harness')
        git(seed, 'push', '-q', str(self.origin), 'main')
        self.worker: Path = make_worker(self.root, 'worker', 'topic/root', 'root.txt', 'Root topic')
        # The integration workspace already holds the harness, as the bootstrap leaves it.
        self.workspace: Path = self.root / 'integration'
        (self.workspace / 'template_harness').mkdir(parents=True)
        (self.workspace / 'template_harness' / 'marker.txt').write_text('harness\n', encoding='utf-8')
        self.settings: checkpoint.Settings = checkpoint.parse_settings(
            {'tree': '.', 'verify': list(RECORDING)}, self.workspace, 'owner/product')

    def quietly(self, function: object, *arguments: object) -> object:
        with contextlib.redirect_stdout(io.StringIO()):
            result: object = function(*arguments)  # type: ignore[operator]
        return result

    def test_init_clones_into_the_workspace_beside_the_harness(self) -> None:
        self.assertTrue(self.settings.at_workspace_root)
        self.assertEqual(self.settings.tree, self.workspace)
        self.quietly(checkpoint.init_tree, self.settings, str(self.origin))
        self.assertEqual(git(self.workspace, 'rev-parse', '--show-toplevel'), self.workspace.as_posix())
        self.assertEqual(git(self.workspace, 'branch', '--show-current'), 'main')
        self.assertEqual(git(self.workspace, 'rev-parse', '--abbrev-ref', '@{upstream}'), 'origin/main')
        self.assertEqual(git(self.workspace, 'config', '--local', checkpoint.TREE_MARK), 'true')
        self.assertEqual((self.workspace / 'template_harness' / 'marker.txt').read_text(encoding='utf-8'), 'harness\n')
        self.assertEqual(git(self.workspace, 'status', '--porcelain'), '')

    def test_fold_and_verify_run_in_the_workspace_root(self) -> None:
        self.quietly(checkpoint.init_tree, self.settings, str(self.origin))
        self.quietly(checkpoint.fold, self.settings, [str(self.worker) + ':topic/root'], [])
        self.assertEqual(git(self.workspace, 'log', '-1', '--format=%s'), 'Root topic')
        self.assertTrue((self.workspace / 'template_harness' / 'marker.txt').is_file())
        self.assertTrue(self.quietly(checkpoint.verify, self.settings, self.workspace))
        seen: list[str] = json.loads((self.workspace / 'verified.json').read_text(encoding='utf-8'))
        self.assertEqual([Path(seen[0]), Path(seen[1]).resolve()], [self.workspace, self.workspace])
        commands: list[list[str]] = self.quietly(checkpoint.push, self.settings, True, TODAY)
        self.assertTrue(commands)

    def test_an_entry_the_clone_would_overwrite_stops_init_and_leaves_no_git(self) -> None:
        (self.workspace / 'base.txt').write_text('mine\n', encoding='utf-8')
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'failed at git checkout'):
            self.quietly(checkpoint.init_tree, self.settings, str(self.origin))
        self.assertFalse((self.workspace / '.git').exists())
        self.assertEqual((self.workspace / 'base.txt').read_text(encoding='utf-8'), 'mine\n')

    def test_a_tree_below_the_workspace_is_never_cloned_in_place(self) -> None:
        below: checkpoint.Settings = checkpoint.parse_settings(
            {'tree': 'template_harness', 'verify': list(PASSING)}, self.workspace, 'owner/product')
        self.assertFalse(below.at_workspace_root)
        with self.assertRaisesRegex(checkpoint.CheckpointError, 'exists and is not a git checkout'):
            self.quietly(checkpoint.init_tree, below, str(self.origin))

if __name__ == '__main__':
    unittest.main()
