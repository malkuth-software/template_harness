"""sync_fransushi.py against a fixture plugins repository, and the claude/ marketplace it keeps here."""
from __future__ import annotations
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
from typing import Any
import unittest
from unittest import mock

import harness_testing
import sync_fransushi

GIT_ENVIRONMENT: dict[str, str] = {
    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
    'GIT_COMMITTER_NAME': 'fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
CLAUDE: Path = harness_testing.REPOSITORY / 'claude'
PLUGINS: tuple[str, ...] = ('sushibuild', 'sushicomms', 'sushidelivery', 'sushirice', 'sushistyle', 'sushiwrap',
                            'wasabi')
HOUSE_STYLE_SECTIONS: dict[str, str] = {
    '1': 'Memory use', '2': 'Failure and foreign interfaces', '3': 'Types and values', '4': 'Objects and authority',
    '5': 'Callbacks and execution', '6': 'Procedures and data', '7': 'Admitted abstractions', '8': 'Repeated kernels',
    '9': 'Statements and expressions', '10': 'Source layout and comments', '11': 'C++ spelling'}


def git(directory: Path, *arguments: str) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        environment: mock._patch_dict = mock.patch.dict(os.environ, GIT_ENVIRONMENT)
        environment.start()
        self.addCleanup(environment.stop)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root: Path = Path(directory.name).resolve()
        self.source: Path = self.root / 'fransushi'
        entries: list[dict[str, Any]] = []
        name: str
        for name in ('alpha', 'beta'):
            plugin: Path = self.source / 'plugins' / name
            write(plugin / '.claude-plugin' / 'plugin.json',
                  json.dumps({'name': name, 'version': '1.2.' + str(len(entries)), 'description': name}))
            write(plugin / 'scripts' / (name + '.py'), 'print("' + name + '")\n')
            write(plugin / 'README.md', '# ' + name + '\n')
            write(plugin / 'tests' / ('test_' + name + '.py'), 'pass\n')
            write(plugin / 'rules' / 'fixtures' / 'keep.cpp', 'int x = 0;\n')
            entries.append({'name': name, 'source': './plugins/' + name, 'description': name, 'version': '0.0.1'})
        write(self.source / '.claude-plugin' / 'marketplace.json', json.dumps(
            {'name': 'fransushi', 'owner': {'name': 'Malkuth Software'},
             'metadata': {'description': 'FranSushi.'}, 'plugins': entries}))
        git(self.source, 'init', '-q', '-b', 'main')
        git(self.source, 'add', '--all')
        git(self.source, 'commit', '-q', '-m', 'plugins')
        # Ignored development leftovers, which the copy must leave out.
        write(self.source / 'plugins' / 'alpha' / 'scripts' / '__pycache__' / 'alpha.cpython-312.pyc', 'x')
        write(self.source / 'plugins' / 'alpha' / 'state' / 'run.json', '{}')
        write(self.source / 'plugins' / 'alpha' / '.DS_Store', 'x')
        write(self.source / '.gitignore', '__pycache__/\nstate/\n.DS_Store\n')
        git(self.source, 'add', '.gitignore')
        git(self.source, 'commit', '-q', '-m', 'ignore')
        self.destination: Path = self.root / 'claude'
        write(self.destination / 'rules' / 'house.json', '{"pack": "house"}\n')
        write(self.destination / 'plugins' / 'retired' / 'README.md', '# retired\n')

    def run_sync(self, *extra: str) -> tuple[int, str, str]:
        output: io.StringIO = io.StringIO()
        errors: io.StringIO = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status: int = sync_fransushi.main(['--from', str(self.source), '--to', str(self.destination), *extra])
        return status, output.getvalue(), errors.getvalue()

    def test_sync_copies_the_plugins_without_development_files(self) -> None:
        status: int
        output: str
        errors: str
        status, output, errors = self.run_sync()
        self.assertEqual(status, 0, errors)
        alpha: Path = self.destination / 'plugins' / 'alpha'
        self.assertTrue((alpha / 'scripts' / 'alpha.py').is_file())
        self.assertTrue((alpha / 'rules' / 'fixtures' / 'keep.cpp').is_file())
        left_out: str
        for left_out in ('tests', 'state', 'scripts/__pycache__', '.DS_Store'):
            self.assertFalse((alpha / left_out).exists(), left_out)
        self.assertFalse((self.destination / 'plugins' / 'retired').exists())
        self.assertIn('no longer listed', output)
        # The harness's own rule pack is never touched.
        self.assertEqual((self.destination / 'rules' / 'house.json').read_text(encoding='utf-8'), '{"pack": "house"}\n')
        marketplace: dict[str, Any] = json.loads(
            (self.destination / '.claude-plugin' / 'marketplace.json').read_text(encoding='utf-8'))
        self.assertEqual(marketplace['name'], 'fransushi')
        sources: list[str] = []
        entry: dict[str, Any]
        for entry in marketplace['plugins']:
            sources.append(entry['source'])
        self.assertEqual(sources, ['./plugins/alpha', './plugins/beta'])
        lock: dict[str, Any] = tomllib.loads((self.destination / 'FRANSUSHI.lock').read_text(encoding='utf-8'))
        self.assertEqual(lock['source'], {'repository': 'malkuth-software/fransushi',
                                          'commit': git(self.source, 'rev-parse', 'HEAD'), 'dirty': False})
        self.assertEqual(lock['plugins'], {'alpha': '1.2.0', 'beta': '1.2.1'})

    def test_a_dirty_source_is_refused_unless_allowed(self) -> None:
        write(self.source / 'plugins' / 'beta' / 'scripts' / 'beta.py', 'print("edited")\n')
        status: int
        errors: str
        status, _, errors = self.run_sync()
        self.assertEqual(status, 1)
        self.assertIn('uncommitted plugin changes', errors)
        self.assertFalse((self.destination / 'plugins' / 'beta').exists())
        status, _, errors = self.run_sync('--allow-dirty')
        self.assertEqual(status, 0, errors)
        lock: dict[str, Any] = tomllib.loads((self.destination / 'FRANSUSHI.lock').read_text(encoding='utf-8'))
        self.assertTrue(lock['source']['dirty'])
        self.assertIn('edited', (self.destination / 'plugins' / 'beta' / 'scripts' / 'beta.py').read_text())

    def test_a_dry_run_writes_nothing(self) -> None:
        status: int
        output: str
        status, output, _ = self.run_sync('--dry-run')
        self.assertEqual(status, 0)
        self.assertIn('would copy', output)
        self.assertFalse((self.destination / 'plugins' / 'alpha').exists())
        self.assertTrue((self.destination / 'plugins' / 'retired').exists())
        self.assertFalse((self.destination / 'FRANSUSHI.lock').exists())

    def test_a_mismatched_entry_is_refused(self) -> None:
        write(self.source / 'plugins' / 'beta' / '.claude-plugin' / 'plugin.json', json.dumps({'name': 'gamma'}))
        status: int
        errors: str
        status, _, errors = self.run_sync('--allow-dirty')
        self.assertEqual(status, 1)
        self.assertIn('does not name beta', errors)


class ClaudeDirectoryTests(unittest.TestCase):
    """The marketplace this repository ships: the synced plugins, their lock and the house rule pack."""

    def test_the_marketplace_lists_the_seven_plugins_and_the_lock_matches(self) -> None:
        marketplace: dict[str, Any] = json.loads((CLAUDE / '.claude-plugin' / 'marketplace.json').read_text())
        self.assertEqual(marketplace['name'], 'fransushi')
        lock: dict[str, Any] = tomllib.loads((CLAUDE / 'FRANSUSHI.lock').read_text(encoding='utf-8'))
        self.assertRegex(lock['source']['commit'], '^[0-9a-f]{40}$')
        names: list[str] = []
        entry: dict[str, Any]
        for entry in marketplace['plugins']:
            names.append(entry['name'])
            self.assertEqual(entry['source'], './plugins/' + entry['name'])
            manifest: dict[str, Any] = json.loads(
                (CLAUDE / 'plugins' / entry['name'] / '.claude-plugin' / 'plugin.json').read_text())
            self.assertEqual(lock['plugins'][entry['name']], manifest['version'])
            self.assertFalse((CLAUDE / 'plugins' / entry['name'] / 'tests').exists())
        self.assertEqual(sorted(names), list(PLUGINS))

    def test_each_house_rule_cites_its_section_and_has_fixtures(self) -> None:
        pack: dict[str, Any] = json.loads((CLAUDE / 'rules' / 'house.json').read_text(encoding='utf-8'))
        self.assertEqual(pack['pack'], 'house')
        identities: list[str] = []
        rule: dict[str, Any]
        for rule in pack['rules']:
            identities.append(rule['id'])
            cited: dict[str, Any] = rule['house_style']
            self.assertEqual(cited['section'], rule['section'], rule['id'])
            self.assertEqual(cited['title'], HOUSE_STYLE_SECTIONS[cited['section']], rule['id'])
            self.assertTrue(cited['rule'], rule['id'])
            kind: str
            for kind in ('fail', 'pass'):
                self.assertTrue((CLAUDE / 'rules' / rule['fixtures'][kind]).is_file(), rule['id'] + ' ' + kind)
        self.assertEqual(len(identities), len(set(identities)))
        self.assertIn('HS-auto', identities)

    def test_sushistyle_reads_the_pack_by_path_and_its_regex_rule_fires(self) -> None:
        script: Path = CLAUDE / 'plugins' / 'sushistyle' / 'scripts' / 'sushistyle.py'
        specification: Any = importlib.util.spec_from_file_location('synced_sushistyle', script)
        module: Any = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app: Path = Path(directory.name) / 'app'
        app.mkdir()
        relative: str = os.path.relpath(CLAUDE / 'rules' / 'house.json', app)
        rules: list[dict[str, Any]] = module.load_rules({'rule_packs': [relative]}, str(app))
        self.assertGreaterEqual(len(rules), 14)
        failing: list[str] = (CLAUDE / 'rules' / 'fixtures' / 'HS-ranges.fail.cpp').read_text().splitlines()
        found: list[Any] = module.run_regex_tier(rules, failing)
        self.assertTrue(found)
        passing: list[str] = (CLAUDE / 'rules' / 'fixtures' / 'HS-ranges.pass.cpp').read_text().splitlines()
        self.assertEqual(module.run_regex_tier(rules, passing), [])


if __name__ == '__main__':
    unittest.main()
