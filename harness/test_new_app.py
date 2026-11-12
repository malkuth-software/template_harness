"""new_app.py: the templates, the names, the dry run, and a whole run against local bare repositories.

Local bare repositories under file://<tmp>/remotes/malkuth-software stand in
for GitHub (new_app's --remote-base), so nothing here reaches the network:
template_harness (seeded with this repository's project files, so the name
checks see the real neighbours), notes and projects. The app repository is
created by --create-repo, which for a file:// base is git init --bare.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
from typing import Any
import unittest
from unittest import mock

import harness_testing
import new_app
import project

GIT_ENVIRONMENT: dict[str, str] = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
OWNER: str = 'malkuth-software'
APP: str = 'paint_box'
SUSHICOMMS: Path = harness_testing.REPOSITORY / 'claude' / 'plugins' / 'sushicomms' / 'scripts' / 'sushicomms.py'
# What templates/app/ must render to, at least.
APP_FILES: tuple[str, ...] = (
    'LICENSE', 'THIRD_PARTY_NOTICES.md', 'README.md', '.gitignore', '.gitattributes', 'CLAUDE.md', 'AGENTS.md', 'harness.lock',
    'CMakeLists.txt', 'ci_build.py', 'fetch_sdk.py', 'test_fetch_sdk.py', 'gui_forms.lock',
    'src/greeter.cpp', 'src/greeter.hpp', 'src/main.cpp', 'tests/greeter_tests.cpp',
    '.claude/settings.json', '.claude/agents/paint_box-coder.md', '.claude/agents/paint_box-integrator.md',
    '.claude/agents/paint_box-tedium.md', '.github/workflows/ci.yml',
    '.sushi/sushistyle.json', '.sushi/sushirice.json', '.sushi/sushibuild.json', '.sushi/sushicomms.json',
    '.sushi/sushiwrap.json', '.sushi/wasabi.json', '.sushi/sushicomms/roles.json')
PLUGINS: tuple[str, ...] = ('sushibuild', 'sushicomms', 'sushidelivery', 'sushirice', 'sushistyle', 'sushiwrap',
                            'wasabi')
HARNESS_COMMIT: str = 'c' * 40


def git(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def refs(bare: Path) -> str:
    """Every ref and its commit: equal before and after means nothing was pushed."""
    if not bare.exists():
        return '<absent>'
    listing: str = git(bare, 'for-each-ref', '--format=%(refname) %(objectname)')
    return listing


def plan_values() -> dict[str, str]:
    names: new_app.Names = new_app.derive_names(APP, 'Paint Box')
    remote: new_app.Remote = new_app.Remote('https://github.com/' + OWNER, True, False)
    plan: new_app.Plan = new_app.Plan(
        names=names, workspace=Path('/nonexistent'), harness=harness_testing.REPOSITORY, remote=remote, dry_run=True,
        restore=False, smoke=False, skip_workspace=True, build_host='', build_host_workspace='', claude='',
        options={'license': 'proprietary', 'licensor': 'Test Holder', 'build_wrapper': 'm4ws', 'workers_dir': '', 'projects_dir': '',
                 'notes_dir': ''})
    values: dict[str, str] = new_app.values_for(plan, HARNESS_COMMIT)
    return values


class NamesTests(unittest.TestCase):
    def test_names_follow_the_prefix_rules(self) -> None:
        names: new_app.Names = new_app.derive_names('paint_box')
        self.assertEqual((names.pascal, names.kebab, names.env_prefix, names.namespace, names.title),
                         ('PaintBox', 'paint-box', 'PAINT_BOX', 'paint_box', 'Paint Box'))
        self.assertEqual((names.build_name, names.artifact_prefix, names.cache_asset_prefix),
                         ('paint-box', 'paint-box-', 'paint-box-cache-'))
        self.assertEqual(new_app.name_collisions(names, new_app.project_files(harness_testing.REPOSITORY)), [])

    def test_bad_names_are_refused(self) -> None:
        bad: str
        for bad in ('PaintBox', 'paint-box', '2paint', 'paint__box', 'paint_', '', 'gui_paint', 'notes',
                    'template_harness', 'a' * 41):
            with self.assertRaises(new_app.NewAppError, msg=bad):
                new_app.derive_names(bad)
        with self.assertRaises(new_app.NewAppError):
            new_app.derive_names('paint', title='Say "hi"')

    def test_a_name_that_overlaps_a_neighbour_is_found(self) -> None:
        # example-... overlaps the example app's example-app- prefixes and EXAMPLE_APP_ variables.
        findings: list[str] = new_app.name_collisions(new_app.derive_names('example'),
                                                      new_app.project_files(harness_testing.REPOSITORY))
        kinds: list[str] = []
        finding: str
        for finding in findings:
            kinds.append(finding.split(' ')[0])
        self.assertIn('project.env_prefix', kinds)
        self.assertIn('ci.artifact_prefix', kinds)


class TemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root: Path = Path(directory.name).resolve()
        self.values: dict[str, str] = plan_values()

    def render(self, template: str) -> Path:
        destination: Path = self.root / template
        if template == 'app':
            new_app.render_app(destination, self.values, 'proprietary')
        else:
            new_app.render_tree(new_app.TEMPLATES / template, destination, self.values)
        return destination

    def test_each_license_choice(self) -> None:
        expected: dict[str, str] = {'proprietary': 'All rights reserved.', 'mit': 'MIT License',
                                    'polyform-noncommercial': 'PolyForm Noncommercial License 1.0.0'}
        choice: str
        for choice in new_app.LICENSES:
            destination: Path = self.root / ('license-' + choice)
            new_app.render_app(destination, self.values, choice)
            if choice == 'none':
                self.assertFalse((destination / 'LICENSE').exists())
            else:
                text: str = (destination / 'LICENSE').read_text(encoding='utf-8')
                self.assertIn(expected[choice], text)
                self.assertIn('Test Holder', text)
                self.assertFalse(new_app.leftover_placeholders(text))
            self.assertIn('MIT License', (destination / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8'))

    def test_the_app_renders_without_placeholders(self) -> None:
        app: Path = self.render('app')
        relative: str
        for relative in APP_FILES:
            self.assertTrue((app / relative).is_file(), relative)
        path: Path
        for path in app.rglob('*'):
            self.assertNotIn('{{', path.relative_to(app).as_posix())
            if path.is_file():
                text: str = path.read_text(encoding='utf-8')
                self.assertFalse(new_app.leftover_placeholders(text), str(path))
                self.assertNotIn('example_app', text, str(path))
                self.assertNotIn('/Users/', text, str(path))
        lock: dict[str, Any] = tomllib.loads((app / 'harness.lock').read_text(encoding='utf-8'))
        self.assertEqual(lock['harness'], {'repository': OWNER + '/template_harness', 'commit': HARNESS_COMMIT})
        self.assertIn('All rights reserved.', (app / 'LICENSE').read_text(encoding='utf-8'))
        self.assertIn('@AGENTS.md', (app / 'CLAUDE.md').read_text(encoding='utf-8'))
        workflow: str = (app / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        self.assertEqual(workflow.count('@' + HARNESS_COMMIT), 3)
        self.assertIn('${{ matrix.runner }}', workflow)
        self.assertIn('namespace paint_box {', (app / 'src/greeter.hpp').read_text(encoding='utf-8'))
        self.assertIn('options.title = "Paint Box";', (app / 'src/main.cpp').read_text(encoding='utf-8'))
        self.assertIn("/ 'paint-box'", (app / 'ci_build.py').read_text(encoding='utf-8'))
        ignored: str = (app / '.gitignore').read_text(encoding='utf-8')
        entry: str
        for entry in ('/template_harness/', '/.build/', '/.ccache/', '/.ci/', '/.toolchain/', '/.bootstrap/',
                      '/.sushi/state/', '/.sushi/logs/'):
            self.assertIn(entry + '\n', ignored)

    def test_the_settings_declare_the_harness_marketplace_and_the_plugins(self) -> None:
        app: Path = self.render('app')
        settings: dict[str, Any] = json.loads((app / '.claude/settings.json').read_text(encoding='utf-8'))
        source: dict[str, Any] = settings['extraKnownMarketplaces']['fransushi']['source']
        # The sibling harness checkout, which lock.py checkout keeps at the pin (owner, Q22).
        self.assertEqual(source, {'source': 'directory', 'path': '../template_harness/claude'})
        expected: dict[str, bool] = {}
        plugin: str
        for plugin in PLUGINS:
            expected[plugin + '@fransushi'] = plugin != 'sushidelivery'
        self.assertEqual(settings['enabledPlugins'], expected)
        # The agents name themselves after the app and carry no machine paths.
        agent: str
        for agent in ('coder', 'integrator', 'tedium'):
            text: str = (app / '.claude/agents' / ('paint_box-' + agent + '.md')).read_text(encoding='utf-8')
            self.assertTrue(text.startswith('---\nname: paint_box-' + agent + '\n'), agent)
            self.assertIn('<workspace>/', text)

    def test_the_sushi_configs_point_into_the_workspace(self) -> None:
        app: Path = self.render('app')
        configs: dict[str, dict[str, Any]] = {}
        path: Path
        for path in sorted((app / '.sushi').glob('*.json')):
            configs[path.stem] = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(configs['sushistyle']['rule_packs'], ['../template_harness/claude/rules/house.json'])
        self.assertEqual(configs['sushiwrap']['seats_dir'], '../projects/desks/seats')
        self.assertEqual(configs['sushicomms']['state_file'], '../projects/desks/paint_box/sushicomms.json')
        self.assertEqual(configs['sushirice']['watchdog_mode'], 'enforce')
        self.assertNotIn('build_command', configs['sushibuild'])
        registry: dict[str, Any] = json.loads((app / '.sushi/sushicomms/roles.json').read_text(encoding='utf-8'))
        self.assertEqual(registry['roles']['coder']['agent_types'], ['paint_box-coder'])
        self.assertIn('paint_box-tedium', registry['settings']['agent_spawn']['allowed_subagent_types'])

    @unittest.skipUnless(SUSHICOMMS.is_file(), 'the sushicomms plugin is not in claude/plugins')
    def test_the_role_registry_validates(self) -> None:
        app: Path = self.render('app')
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [sys.executable, '-B', str(SUSHICOMMS), 'registry', 'validate'], cwd=str(app), capture_output=True,
            text=True, check=False, env=dict(os.environ, SUSHI_ROLE='orchestrator', SUSHICOMMS_PROJECT=str(app)))
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_the_rendered_sdk_fetch_tests_pass_in_the_app_layout(self) -> None:
        app: Path = self.render('app')
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(app), '-p', 'test_*.py'],
            capture_output=True, text=True, check=False, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr[-3000:])

    def test_the_rendered_sources_pass_the_house_checkers(self) -> None:
        app: Path = self.render('app')
        checker: str
        for checker in ('check_house_style.py', 'check_lifetime_rules.py'):
            completed: subprocess.CompletedProcess[str] = subprocess.run(
                [sys.executable, '-B', str(harness_testing.HARNESS / checker), str(app / 'src'), str(app / 'tests')],
                capture_output=True, text=True, check=False, timeout=120)
            self.assertEqual(completed.returncode, 0, checker + ': ' + completed.stdout + completed.stderr)

    def test_the_project_file_loads_and_its_names_test_compiles(self) -> None:
        harness: Path = self.render('harness')
        product: project.Project = project.load(harness / 'projects' / 'paint_box.toml')
        self.assertEqual((product.name, product.source_dir, product.build_name, product.env_prefix),
                         ('paint_box', 'paint_box', 'paint-box', 'PAINT_BOX'))
        self.assertEqual((product.artifact_prefix, product.cache_asset_prefix), ('paint-box-', 'paint-box-cache-'))
        self.assertEqual(product.repository, OWNER + '/paint_box')
        self.assertIsNotNone(product.bootstrap)
        self.assertIsNotNone(product.checkpoint)
        test: Path = harness / 'harness' / 'test_project_names_paint_box.py'
        compile(test.read_text(encoding='utf-8'), str(test), 'exec')
        self.assertIn('class PaintBoxProjectTests', test.read_text(encoding='utf-8'))

    def test_an_unknown_placeholder_is_refused(self) -> None:
        with self.assertRaisesRegex(new_app.NewAppError, 'unknown placeholders: missing'):
            new_app.render_text('a {{missing}} b', self.values, 'x')
        self.assertEqual(new_app.render_text('${{ github.ref }} {{app}}', self.values, 'x'),
                         '${{ github.ref }} paint_box')
        self.assertFalse(new_app.leftover_placeholders('${{ matrix.platform }}'))
        self.assertTrue(new_app.leftover_placeholders('x {{ y'))


class RunTests(unittest.TestCase):
    """new_app end to end against local bare repositories."""

    def setUp(self) -> None:
        environment: mock._patch_dict = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root: Path = Path(directory.name).resolve()
        self.remotes: Path = self.root / 'remotes' / OWNER
        self.remotes.mkdir(parents=True)
        seed: Path = self.root / 'seed-harness'
        (seed / 'projects').mkdir(parents=True)
        path: Path
        for path in sorted((harness_testing.REPOSITORY / 'projects').glob('*.toml')):
            shutil.copyfile(path, seed / 'projects' / path.name)
        (seed / 'README.md').write_text('template_harness stand-in\n', encoding='utf-8')
        self.seed_bare('template_harness', seed)
        notes: Path = self.root / 'seed-notes'
        notes.mkdir()
        (notes / 'HOUSE_STYLE.md').write_text('**1. Memory use**\n', encoding='utf-8')
        self.seed_bare('notes', notes)
        projects: Path = self.root / 'seed-projects'
        (projects / 'desks' / 'seats').mkdir(parents=True)
        (projects / 'desks' / 'seats' / 'gui_forms-w1.md').write_text('# Seat gui_forms-w1\n', encoding='utf-8')
        self.seed_bare('projects', projects)
        self.harness: Path = self.root / 'template_harness'
        subprocess.run(['git', 'clone', '-q', str(self.remotes / 'template_harness.git'), str(self.harness)],
                       check=True)
        self.workspace: Path = self.root / 'ws'

    def seed_bare(self, name: str, tree: Path) -> None:
        git(tree, 'init', '-q', '-b', 'main')
        git(tree, 'add', '--all')
        git(tree, 'commit', '-q', '-m', 'Seed ' + name)
        bare: Path = self.remotes / (name + '.git')
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)], check=True)
        git(tree, 'push', '-q', str(bare), 'main')

    def bare(self, name: str) -> Path:
        return self.remotes / (name + '.git')

    def snapshot(self) -> dict[str, str]:
        state: dict[str, str] = {}
        name: str
        for name in ('template_harness', 'notes', 'projects', APP):
            state[name] = refs(self.bare(name))
        state['harness clone'] = git(self.harness, 'for-each-ref', '--format=%(refname) %(objectname)')
        state['harness status'] = git(self.harness, 'status', '--porcelain')
        return state

    def run_new_app(self, *extra: str) -> tuple[int, str, str]:
        output: io.StringIO = io.StringIO()
        errors: io.StringIO = io.StringIO()
        arguments: list[str] = ['--name', APP, '--title', 'Paint Box', '--workspace', str(self.workspace),
                                '--remote-base', 'file://' + str(self.remotes), '--harness', str(self.harness), '--licensor', 'Test Holder',
                                '--create-repo', '--no-restore', '--claude', '/nonexistent/claude', *extra]
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status: int = new_app.main(arguments)
        return status, output.getvalue(), errors.getvalue()

    def test_a_dry_run_touches_nothing_and_names_every_repository(self) -> None:
        before: dict[str, str] = self.snapshot()
        status: int
        output: str
        errors: str
        status, output, errors = self.run_new_app('--dry-run')
        self.assertEqual(status, 0, errors + output)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.workspace.exists())
        name: str
        for name in ('template_harness', 'notes', 'projects', APP):
            self.assertIn('file://' + str(self.remotes) + '/' + name + '.git', output)
        number: int
        for number in range(1, new_app.STEPS + 1):
            self.assertIn('[' + str(number) + '/' + str(new_app.STEPS) + ']', output)
        self.assertIn('would create ' + OWNER + '/' + APP + ' (private)', output)
        self.assertIn(str(self.workspace / APP), output)
        self.assertIn('Bootstrap finished (dry run: nothing was changed)', output)

    def test_a_full_run_then_an_idempotent_rerun(self) -> None:
        status: int
        output: str
        errors: str
        status, output, errors = self.run_new_app()
        self.assertEqual(status, 0, errors + output)
        # template_harness: the project file on main, added by one commit, which is the pin.
        harness_main: str = git(self.bare('template_harness'), 'rev-parse', 'main')
        added: str = git(self.bare('template_harness'), 'log', '-1', '--diff-filter=A', '--format=%H', 'main', '--',
                         'projects/paint_box.toml')
        self.assertEqual(added, harness_main)
        self.assertEqual(git(self.bare('template_harness'), 'show', '--name-only', '--format=', 'main').split('\n'),
                         ['harness/test_project_names_paint_box.py', 'projects/paint_box.toml'])
        # The app: one commit, pinned to that harness commit everywhere.
        self.assertEqual(git(self.bare(APP), 'rev-list', '--count', 'main'), '1')
        lock_text: str = git(self.bare(APP), 'show', 'main:harness.lock')
        self.assertIn('commit = "' + added + '"', lock_text)
        settings: dict[str, Any] = json.loads(git(self.bare(APP), 'show', 'main:.claude/settings.json'))
        self.assertEqual(settings['extraKnownMarketplaces']['fransushi']['source'],
                         {'source': 'directory', 'path': '../template_harness/claude'})
        self.assertIn('@' + added, git(self.bare(APP), 'show', 'main:.github/workflows/ci.yml'))
        tree: list[str] = git(self.bare(APP), 'ls-tree', '-r', '--name-only', 'main').split('\n')
        relative: str
        for relative in APP_FILES:
            self.assertIn(relative, tree)
        # notes: the branch, with the skeleton and nothing of main's.
        notes_tree: list[str] = git(self.bare('notes'), 'ls-tree', '-r', '--name-only', APP).split('\n')
        self.assertIn('planning/BRIEF.md', notes_tree)
        self.assertIn('decisions/README.md', notes_tree)
        self.assertNotIn('HOUSE_STYLE.md', notes_tree)
        # projects: tracks, desk and three seats on main, beside what was there.
        projects_tree: list[str] = git(self.bare('projects'), 'ls-tree', '-r', '--name-only', 'main').split('\n')
        expected: str
        for expected in ('tracks/paint_box/README.md', 'tracks/paint_box/w1.md', 'desks/paint_box/README.md',
                         'desks/seats/paint_box-orchestrator.md', 'desks/seats/paint_box-w1.md',
                         'desks/seats/paint_box-integrator.md', 'desks/seats/gui_forms-w1.md'):
            self.assertIn(expected, projects_tree)
        self.assertEqual(git(self.bare('projects'), 'rev-list', '--count', 'main'), '2')
        seat: str = git(self.bare('projects'), 'show', 'main:desks/seats/paint_box-w1.md')
        self.assertIn('"seat": "paint_box-w1"', seat)
        self.assertIn('"role": "coder"', seat)
        # The workspace: the sibling layout, the harness at the pin.
        self.assertTrue((self.workspace / APP / 'harness.lock').is_file())
        self.assertEqual(git(self.workspace / 'template_harness', 'rev-parse', 'HEAD'), added)
        self.assertTrue((self.workspace / 'notes' / 'HOUSE_STYLE.md').is_file())
        self.assertTrue((self.workspace / ('notes-' + APP) / 'planning' / 'BRIEF.md').is_file())
        self.assertTrue((self.workspace / 'projects' / 'tracks' / APP / 'w1.md').is_file())
        # The user's harness clone is left as it was (a temporary worktree did the commit).
        self.assertEqual(git(self.harness, 'status', '--porcelain'), '')
        self.assertEqual(git(self.harness, 'worktree', 'list', '--porcelain').count('worktree '), 1)
        self.assertIn('plugin validate: skipped', output)
        self.assertIn('Open Claude in ' + str(self.workspace / APP), output)
        # Again: every step finds its work done; no remote moves.
        before: dict[str, str] = self.snapshot()
        status, output, errors = self.run_new_app()
        self.assertEqual(status, 0, errors + output)
        self.assertEqual(self.snapshot(), before)
        self.assertIn('already on main, added in ' + added, output)
        self.assertIn('already instantiated, nothing to do', output)
        self.assertIn('branch ' + APP + ' exists', output)
        self.assertIn('tracks/' + APP + '/ exists; nothing to do', output)

    def test_a_missing_app_repository_needs_create_repo(self) -> None:
        output: io.StringIO = io.StringIO()
        errors: io.StringIO = io.StringIO()
        arguments: list[str] = ['--name', APP, '--workspace', str(self.workspace), '--remote-base',
                                'file://' + str(self.remotes), '--harness', str(self.harness), '--no-restore',
                                '--skip-workspace', '--licensor', 'Test Holder']
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status: int = new_app.main(arguments)
        self.assertEqual(status, 1)
        self.assertIn('pass --create-repo', errors.getvalue())
        self.assertFalse(self.bare(APP).exists())

    def test_a_colliding_name_stops_before_any_push(self) -> None:
        before: dict[str, str] = self.snapshot()
        output: io.StringIO = io.StringIO()
        errors: io.StringIO = io.StringIO()
        arguments: list[str] = ['--name', 'example', '--workspace', str(self.workspace), '--remote-base',
                                'file://' + str(self.remotes), '--harness', str(self.harness), '--create-repo',
                                '--licensor', 'Test Holder']
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status: int = new_app.main(arguments)
        self.assertEqual(status, 1)
        self.assertIn('collide', errors.getvalue())
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
