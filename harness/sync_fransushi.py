#!/usr/bin/env python3
"""Copy the released FranSushi plugins from their private development repository into claude/.

    python3 harness/sync_fransushi.py --from DIR [--to DIR] [--allow-dirty] [--dry-run]

DIR is a clone of the plugins' development repository (malkuth-software/
fransushi): its .claude-plugin/marketplace.json lists the plugins, each in
plugins/<name>/. The sync

1. replaces claude/plugins/<name>/ with a copy of each listed plugin,
   leaving out what only development needs: tests/, __pycache__/, state/,
   .sushi/, *.pyc and .DS_Store. A plugin directory claude/plugins/ holds
   that the source no longer lists is removed;
2. writes claude/.claude-plugin/marketplace.json, the marketplace named
   fransushi that apps declare in .claude/settings.json (templates/app/):
   the source's marketplace with the same entries, every source a
   ./plugins/<name> path inside claude/;
3. writes claude/FRANSUSHI.lock: the source repository's commit, whether
   its plugins had uncommitted changes (refused unless --allow-dirty), and
   each plugin's version from its plugin.json.

claude/rules/ (the house rule pack) belongs to the harness and is never
touched. --to defaults to this repository's claude/. --dry-run prints what
would change and writes nothing.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

HARNESS: Path = Path(__file__).resolve().parent
REPOSITORY: Path = HARNESS.parent
DESTINATION: Path = REPOSITORY / 'claude'
MARKETPLACE: Path = Path('.claude-plugin') / 'marketplace.json'
LOCK_NAME: str = 'FRANSUSHI.lock'
SOURCE_REPOSITORY: str = 'malkuth-software/fransushi'
MARKETPLACE_NAME: str = 'fransushi'
# Development-only entries, left out of the copy at any depth.
EXCLUDED_DIRECTORIES: frozenset[str] = frozenset({'tests', '__pycache__', 'state', '.sushi'})
EXCLUDED_FILES: frozenset[str] = frozenset({'.DS_Store'})
EXCLUDED_SUFFIXES: tuple[str, ...] = ('.pyc', '.pyo')


class SyncError(Exception):
    """An expected refusal; the command prints it and exits 1."""


@dataclass(frozen=True)
class Plugin:
    name: str
    source: Path
    version: str


def read_json(path: Path) -> Any:
    try:
        document: Any = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise SyncError('cannot read ' + str(path) + ': ' + str(error)) from error
    return document


def listed_plugins(source: Path) -> tuple[dict[str, Any], list[Plugin]]:
    """The source marketplace and its plugins, each checked: a ./plugins/<name> directory with a plugin.json
    whose name matches the entry."""
    marketplace: Any = read_json(source / MARKETPLACE)
    if type(marketplace) is not dict or type(marketplace.get('plugins')) is not list:
        raise SyncError(str(source / MARKETPLACE) + ' has no plugins list')
    plugins: list[Plugin] = []
    entry: Any
    for entry in marketplace['plugins']:
        name: Any = entry.get('name') if type(entry) is dict else None
        if type(name) is not str or entry.get('source') != './plugins/' + name:
            raise SyncError('marketplace entry ' + repr(entry)[:80] + ' is not a ./plugins/<name> plugin')
        directory: Path = source / 'plugins' / name
        manifest: Any = read_json(directory / '.claude-plugin' / 'plugin.json')
        if type(manifest) is not dict or manifest.get('name') != name:
            raise SyncError(str(directory) + "'s plugin.json does not name " + name)
        version: Any = manifest.get('version')
        plugins.append(Plugin(name, directory, version if type(version) is str else ''))
    return marketplace, plugins


def source_state(source: Path) -> tuple[str, bool]:
    """The source's HEAD commit and whether plugins/ or the marketplace has uncommitted changes."""
    head: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=False)
    if head.returncode != 0:
        raise SyncError(str(source) + ' is not a git clone: ' + head.stderr.strip())
    status: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(source), 'status', '--porcelain', '--', 'plugins', str(MARKETPLACE)],
        capture_output=True, text=True, check=True)
    return head.stdout.strip(), bool(status.stdout.strip())


def ignored(directory: str, names: list[str]) -> set[str]:
    """shutil.copytree's ignore: the development-only entries of one directory."""
    left_out: set[str] = set()
    name: str
    for name in names:
        is_directory: bool = (Path(directory) / name).is_dir()
        if (is_directory and name in EXCLUDED_DIRECTORIES) or name in EXCLUDED_FILES or name.endswith(EXCLUDED_SUFFIXES):
            left_out.add(name)
    return left_out


def harness_marketplace(source_marketplace: dict[str, Any], plugins: list[Plugin]) -> dict[str, Any]:
    """The fransushi marketplace inside claude/: the source's description and entries, each pointing into
    claude/plugins/."""
    entries: list[dict[str, Any]] = []
    entry: dict[str, Any]
    for entry in source_marketplace['plugins']:
        copied: dict[str, Any] = dict(entry)
        copied['source'] = './plugins/' + copied['name']
        entries.append(copied)
    marketplace: dict[str, Any] = {'name': MARKETPLACE_NAME,
                                   'owner': source_marketplace.get('owner', {'name': 'Malkuth Software'})}
    metadata: Any = source_marketplace.get('metadata')
    description: str = ''
    if type(metadata) is dict and type(metadata.get('description')) is str:
        description = metadata['description']
    marketplace['description'] = (description + ' ' if description else '') + \
        'Shipped inside template_harness (claude/, synced by harness/sync_fransushi.py).'
    marketplace['plugins'] = entries
    return marketplace


def lock_text(commit: str, dirty: bool, plugins: list[Plugin]) -> str:
    lines: list[str] = ['# The FranSushi plugins in claude/plugins/, copied by harness/sync_fransushi.py.',
                        '# Do not edit the copies: change the source repository and sync again.', '',
                        '[source]', 'repository = "' + SOURCE_REPOSITORY + '"', 'commit = "' + commit + '"',
                        '# true: the copy includes changes not yet committed at that commit.',
                        'dirty = ' + ('true' if dirty else 'false'), '', '[plugins]']
    plugin: Plugin
    for plugin in plugins:
        lines.append(plugin.name + ' = "' + plugin.version + '"')
    text: str = '\n'.join(lines) + '\n'
    return text


def write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')


def sync(source: Path, destination: Path, allow_dirty: bool, dry_run: bool) -> list[str]:
    """Copies the plugins and writes the marketplace and lock; returns what it did, one line each."""
    source_marketplace: dict[str, Any]
    plugins: list[Plugin]
    source_marketplace, plugins = listed_plugins(source)
    commit: str
    dirty: bool
    commit, dirty = source_state(source)
    if dirty and not allow_dirty:
        raise SyncError(str(source) + ' has uncommitted plugin changes; commit them, or pass --allow-dirty '
                        '(FRANSUSHI.lock then records dirty = true)')
    done: list[str] = []
    plugins_directory: Path = destination / 'plugins'
    names: set[str] = set()
    plugin: Plugin
    for plugin in plugins:
        names.add(plugin.name)
        target: Path = plugins_directory / plugin.name
        done.append('copy ' + str(plugin.source) + ' -> ' + str(target) + ' (' + plugin.version + ')')
        if not dry_run:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(plugin.source, target, ignore=ignored)
    if plugins_directory.is_dir():
        stale: Path
        for stale in sorted(plugins_directory.iterdir()):
            if stale.is_dir() and stale.name not in names:
                done.append('remove ' + str(stale) + ' (no longer listed)')
                if not dry_run:
                    shutil.rmtree(stale)
    done.append('write ' + str(destination / MARKETPLACE))
    done.append('write ' + str(destination / LOCK_NAME) + ' (' + commit[:12] + (', dirty' if dirty else '') + ')')
    if not dry_run:
        write_json(destination / MARKETPLACE, harness_marketplace(source_marketplace, plugins))
        (destination / LOCK_NAME).write_text(lock_text(commit, dirty, plugins), encoding='utf-8', newline='\n')
    return done


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--from', dest='source', required=True, type=Path, help='the plugins repository clone')
    parser.add_argument('--to', dest='destination', type=Path, default=DESTINATION)
    parser.add_argument('--allow-dirty', action='store_true', help='copy uncommitted plugin changes too')
    parser.add_argument('--dry-run', action='store_true', help='print what would change; write nothing')
    args: argparse.Namespace = parser.parse_args(argv)
    try:
        line: str
        for line in sync(args.source.resolve(), args.destination.resolve(), args.allow_dirty, args.dry_run):
            print(('would ' if args.dry_run else '') + line, flush=True)
    except SyncError as error:
        print('sync_fransushi: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
