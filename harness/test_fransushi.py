"""The FranSushi marketplace in claude/: its manifest, the house rule pack, and each plugin's own tests.

The plugins are developed in claude/plugins/ (claude/README.md). Each keeps
its tests in claude/plugins/<name>/tests/; they run here as separate
processes, one per plugin, with HOME pointed at a scratch directory and the
seat variables removed, so no test reads this machine's ~/.sushi or a live
seat.
"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest

import harness_testing

CLAUDE: Path = harness_testing.REPOSITORY / 'claude'
PLUGINS: tuple[str, ...] = ('sushibuild', 'sushicomms', 'sushidelivery', 'sushirice', 'sushistyle', 'sushiwrap',
                            'wasabi')
HOUSE_STYLE_SECTIONS: dict[str, str] = {
    '1': 'Memory use', '2': 'Failure and foreign interfaces', '3': 'Types and values', '4': 'Objects and authority',
    '5': 'Callbacks and execution', '6': 'Procedures and data', '7': 'Admitted abstractions', '8': 'Repeated kernels',
    '9': 'Statements and expressions', '10': 'Source layout and comments', '11': 'C++ spelling'}
# Variables a session or a seat sets; a plugin test must not inherit them.
SEAT_VARIABLES: tuple[str, ...] = (
    'SUSHI_ROLE', 'SUSHI_SEAT', 'CLAUDE_PROJECT_DIR', 'CLAUDE_PLUGIN_ROOT', 'SUSHIWRAP_CONFIG', 'SUSHIBUILD_CONFIG',
    'SUSHIRICE_CONFIG', 'SUSHISTYLE_CONFIG', 'WASABI_CONFIG', 'SUSHICOMMS_STATE', 'SUSHICOMMS_REGISTRY',
    'SUSHICOMMS_PROJECT', 'SUSHIWRAP_TEST_HOST')
PLUGIN_TEST_TIMEOUT_S: int = 600


class MarketplaceTests(unittest.TestCase):
    def test_the_marketplace_lists_the_seven_plugins_with_their_versions(self) -> None:
        marketplace: dict[str, Any] = json.loads((CLAUDE / '.claude-plugin' / 'marketplace.json').read_text())
        self.assertEqual(marketplace['name'], 'fransushi')
        names: list[str] = []
        entry: dict[str, Any]
        for entry in marketplace['plugins']:
            names.append(entry['name'])
            self.assertEqual(entry['source'], './plugins/' + entry['name'])
            manifest: dict[str, Any] = json.loads(
                (CLAUDE / 'plugins' / entry['name'] / '.claude-plugin' / 'plugin.json').read_text())
            self.assertEqual(entry['version'], manifest['version'], entry['name'])
            self.assertTrue((CLAUDE / 'plugins' / entry['name'] / 'tests').is_dir(), entry['name'])
        self.assertEqual(sorted(names), list(PLUGINS))

    def test_no_compiled_python_is_tracked(self) -> None:
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            ['git', '-C', str(harness_testing.REPOSITORY), 'ls-files', 'claude'], capture_output=True, text=True,
            check=False)
        if completed.returncode != 0:
            self.skipTest('not a git checkout')
        tracked: list[str] = [name for name in completed.stdout.splitlines()
                              if '__pycache__/' in name or name.endswith('.pyc')]
        self.assertEqual(tracked, [])

    def test_every_copy_of_sushitree_is_identical(self) -> None:
        copies: list[Path] = sorted((CLAUDE / 'plugins').glob('*/scripts/sushitree.py'))
        self.assertGreaterEqual(len(copies), 4)
        reference: str = copies[0].read_text(encoding='utf-8')
        copy: Path
        for copy in copies[1:]:
            self.assertEqual(copy.read_text(encoding='utf-8'), reference, str(copy) + ' differs from ' + str(copies[0]))

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
        specification: Any = importlib.util.spec_from_file_location('fransushi_sushistyle', script)
        module: Any = importlib.util.module_from_spec(specification)
        sys.dont_write_bytecode = True
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


class PluginSuiteTests(unittest.TestCase):
    """Each plugin's tests/ in its own process: the plugins share module names (test_hook, helpers)."""

    def run_plugin_suite(self, name: str) -> None:
        home: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(prefix='fransushi-home-')
        self.addCleanup(home.cleanup)
        environment: dict[str, str] = dict(os.environ)
        variable: str
        for variable in SEAT_VARIABLES:
            environment.pop(variable, None)
        environment['HOME'] = home.name
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        tests: Path = CLAUDE / 'plugins' / name / 'tests'
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [sys.executable, '-I', '-B', '-m', 'unittest', 'discover', '-s', str(tests)],
            cwd=str(home.name), env=environment, capture_output=True, text=True, timeout=PLUGIN_TEST_TIMEOUT_S,
            check=False)
        self.assertEqual(completed.returncode, 0, name + ' tests failed:\n' + completed.stderr[-6000:])

    def test_sushibuild(self) -> None:
        self.run_plugin_suite('sushibuild')

    def test_sushicomms(self) -> None:
        self.run_plugin_suite('sushicomms')

    def test_sushidelivery(self) -> None:
        self.run_plugin_suite('sushidelivery')

    def test_sushirice(self) -> None:
        self.run_plugin_suite('sushirice')

    def test_sushistyle(self) -> None:
        self.run_plugin_suite('sushistyle')

    def test_sushiwrap(self) -> None:
        self.run_plugin_suite('sushiwrap')

    def test_wasabi(self) -> None:
        self.run_plugin_suite('wasabi')


if __name__ == '__main__':
    unittest.main()
