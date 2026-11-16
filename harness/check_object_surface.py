#!/usr/bin/env python3
"""Proves from its compiled objects what a component can reach outside itself.

A component (an allocator, a line breaker, a parser) states that it calls
nothing but a short list of host functions. This check tests that statement
on the objects, which is what runs. It lists every symbol the component's
objects leave undefined and removes those another of its objects defines.
Each remaining leaf must then be on the component's list for the host
family, a toolchain helper, or (in a sanitizer build only) a sanitizer
runtime entry. Anything else fails: malloc, getenv, a stream, a lock of the
C library, the exception runtime, a thread-local initialiser the compiler
should not have needed.

It is a statement about the whole call graph's leaves: a function reaches
outside code only through a symbol its object does not define. It does not
see calls made through function pointers handed in from outside, and it
cannot tell two uses of one allowed symbol apart.

The component is described by a TOML file (docs/object_surface_check.md):
its objects (CMake targets or object directories), the allowed leaves per
host family with a reason for each, the leaf a family's scan must see (so
that a toolchain which hides host calls fails instead of passing), the
toolchain helpers (names and patterns, for every family), and the sanitizer
allowance.

A shrink-only allowance file can carry leaves that are known and being
removed. Each line is `<family> <symbol> <count>`: the symbol may be left
undefined by at most <count> objects in that family. A run fails on more,
and on a line whose count is higher than the objects seen (a stale line:
lower it or run --write-allowance, which only ever lowers or drops lines).
A missing allowance file counts as empty.

Usage:
  python3 -B harness/check_object_surface.py --config C.toml --family F
      --nm NM (--build-dir DIR | --objects DIR [--under PREFIX])
      [--sanitized] [--allowance FILE] [--write-allowance] [--json OUT]
  python3 -B harness/check_object_surface.py --config C.toml --list

Exit status (stable, for a CI gate):
  0  every leaf is allowed (and no allowance line is stale);
  1  findings: an unlisted leaf, a leaf over its allowance, a stale
     allowance line, or a blind scan (the family's must-see leaf absent);
  2  a usage, configuration or tool error (bad config, no objects, nm
     failed).

Output lines (stable prefixes, one finding a line):
  LEAF <object>: <symbol>          an unlisted leaf
  OVER <family> <symbol> <seen> <allowed>
  STALE <family> <symbol> <allowed> <seen>
  BLIND <family> <symbol>          the must-see leaf was not seen
  SUMMARY <component> <family>: PASS|FAIL (<objects> objects, <leaves> leaves)
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import fnmatch
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any, Callable

OBJECT_SUFFIXES: tuple[str, ...] = ('.o', '.obj')
EXIT_PASS: int = 0
EXIT_FINDINGS: int = 1
EXIT_ERROR: int = 2


class ConfigError(Exception):
    """The component file is missing a field or holds a malformed one."""


@dataclass(frozen=True)
class Allowed:
    """One allowed leaf or pattern, with the reason it is allowed."""
    name: str
    reason: str
    pattern: bool = False

    def matches(self, bare: str) -> bool:
        result: bool = fnmatch.fnmatchcase(bare, self.name) if self.pattern else bare == self.name
        return result


@dataclass(frozen=True)
class Family:
    name: str
    must_see: str
    allowed: tuple[Allowed, ...]


@dataclass(frozen=True)
class Component:
    name: str
    families: dict[str, Family]
    toolchain: tuple[Allowed, ...]
    sanitized: tuple[Allowed, ...]
    targets: tuple[str, ...] = ()
    directories: tuple[str, ...] = ()
    under: str = ''


@dataclass
class Report:
    component: str
    family: str
    objects: int = 0
    leaves: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings


def normalised(name: str) -> str:
    """The symbol without leading underscores, import prefix or ELF version."""
    bare: str = name.split('@', 1)[0].lstrip('_')
    if bare.startswith('imp_'):
        bare = bare[len('imp_'):].lstrip('_')
    return bare


def allowed_entries(table: Any, where: str) -> tuple[Allowed, ...]:
    """Reads [[...]] entries: each has `symbol` or `pattern`, and `reason`."""
    if table is None:
        return ()
    if not isinstance(table, list):
        raise ConfigError(f'{where}: expected an array of tables')
    entries: list[Allowed] = []
    for index, entry in enumerate(table):
        if not isinstance(entry, dict):
            raise ConfigError(f'{where}[{index}]: expected a table')
        reason: Any = entry.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise ConfigError(f'{where}[{index}]: every allowed leaf needs a reason')
        has_symbol: bool = isinstance(entry.get('symbol'), str)
        has_pattern: bool = isinstance(entry.get('pattern'), str)
        if has_symbol == has_pattern:
            raise ConfigError(f'{where}[{index}]: give exactly one of symbol or pattern')
        name: str = entry['symbol'] if has_symbol else entry['pattern']
        entries.append(Allowed(normalised(name) if has_symbol else name, reason.strip(), has_pattern))
    return tuple(entries)


def load_component(path: Path) -> Component:
    """Reads a component file (docs/object_surface_check.md, section 2)."""
    try:
        with path.open('rb') as stream:
            document: dict[str, Any] = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as problem:
        raise ConfigError(f'{path}: {problem}') from problem
    component: Any = document.get('component')
    if not isinstance(component, dict) or not isinstance(component.get('name'), str):
        raise ConfigError(f'{path}: [component] needs a name')
    objects: Any = document.get('objects', {})
    if not isinstance(objects, dict):
        raise ConfigError(f'{path}: [objects] must be a table')
    families_table: Any = document.get('families')
    if not isinstance(families_table, dict) or not families_table:
        raise ConfigError(f'{path}: [families.<name>] needs at least one family')
    families: dict[str, Family] = {}
    for family_name, table in families_table.items():
        if not isinstance(table, dict) or not isinstance(table.get('must_see'), str):
            raise ConfigError(f'{path}: families.{family_name} needs must_see (a leaf every scan must find)')
        families[family_name] = Family(family_name, normalised(table['must_see']),
                                       allowed_entries(table.get('allowed'), f'families.{family_name}.allowed'))
    toolchain: Any = document.get('toolchain', {})
    sanitized: Any = document.get('sanitized', {})
    if not isinstance(toolchain, dict) or not isinstance(sanitized, dict):
        raise ConfigError(f'{path}: [toolchain] and [sanitized] must be tables')
    result: Component = Component(
        name=component['name'], families=families,
        toolchain=allowed_entries(toolchain.get('allowed'), 'toolchain.allowed'),
        sanitized=allowed_entries(sanitized.get('allowed'), 'sanitized.allowed'),
        targets=tuple(objects.get('targets', ())), directories=tuple(objects.get('directories', ())),
        under=str(objects.get('under', '')))
    return result


def is_allowed(component: Component, family: str, name: str, sanitized: bool) -> bool:
    bare: str = normalised(name)
    for entry in component.families[family].allowed:
        if entry.matches(bare):
            return True
    for entry in component.toolchain:
        if entry.matches(bare):
            return True
    if sanitized:
        for entry in component.sanitized:
            if entry.matches(bare):
                return True
    return False


def symbols_of(nm: str, path: Path) -> tuple[set[str], set[str]]:
    """The names one object defines, and the names it leaves undefined (nm -P)."""
    completed = subprocess.run([nm, '-P', str(path)], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f'{path}: {nm} failed: {completed.stderr.strip()}')
    defined: set[str] = set()
    undefined: set[str] = set()
    for line in completed.stdout.splitlines():
        fields: list[str] = line.split()
        if len(fields) < 2 or line.rstrip().endswith(':'):
            continue
        if fields[1] in ('U', 'w', 'v'):
            undefined.add(fields[0])
        else:
            defined.add(fields[0])
    return defined, undefined


def objects_under(directory: Path, prefix: str) -> list[Path]:
    found: list[Path] = []
    for path in sorted(directory.rglob('*')):
        if not path.is_file() or not path.name.endswith(OBJECT_SUFFIXES):
            continue
        if prefix and not path.relative_to(directory).as_posix().startswith(prefix):
            continue
        found.append(path)
    return found


def object_directories(component: Component, build_dir: Path | None, objects: Path | None) -> list[Path]:
    """--objects, or the component's targets (CMake's CMakeFiles/<target>.dir) and directories under --build-dir."""
    if objects is not None:
        return [objects]
    if build_dir is None:
        raise ConfigError('give --objects, or --build-dir with targets or directories in [objects]')
    directories: list[Path] = [build_dir / 'CMakeFiles' / f'{target}.dir' for target in component.targets]
    directories += [build_dir / directory for directory in component.directories]
    if not directories:
        raise ConfigError('[objects] names no targets or directories')
    return directories


def read_allowance(path: Path | None) -> dict[tuple[str, str], int]:
    allowance: dict[tuple[str, str], int] = {}
    if path is None or not path.exists():
        return allowance
    for number, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        line: str = raw.split('#', 1)[0].strip()
        if not line:
            continue
        fields: list[str] = line.split()
        if len(fields) != 3 or not fields[2].isdigit():
            raise ConfigError(f'{path}:{number}: expected "<family> <symbol> <count>"')
        allowance[(fields[0], normalised(fields[1]))] = int(fields[2])
    return allowance


def write_allowance(path: Path, old: dict[tuple[str, str], int], seen: dict[tuple[str, str], int],
                    family: str) -> None:
    """Shrink-only: each line of this family is lowered to what was seen and dropped at zero; no line is added."""
    kept: dict[tuple[str, str], int] = {}
    for key, count in old.items():
        if key[0] != family:
            kept[key] = count
            continue
        lowered: int = min(count, seen.get(key, 0))
        if lowered > 0:
            kept[key] = lowered
    lines: list[str] = [f'{key[0]} {key[1]} {count}' for key, count in sorted(kept.items())]
    path.write_text('# Shrink-only: lower or remove lines; never add (docs/object_surface_check.md).\n'
                    + ''.join(line + '\n' for line in lines), encoding='utf-8')


SymbolReader = Callable[[str, Path], tuple[set[str], set[str]]]


def check(component: Component, family: str, objects: list[Path], nm: str, sanitized: bool,
          allowance: dict[tuple[str, str], int], reader: SymbolReader = symbols_of,
          seen_out: dict[tuple[str, str], int] | None = None) -> Report:
    """The check itself: leaves, findings against the lists and the allowance."""
    report: Report = Report(component.name, family, objects=len(objects))
    defined_anywhere: set[str] = set()
    undefined_by_object: dict[Path, set[str]] = {}
    for path in objects:
        defined, undefined = reader(nm, path)
        defined_anywhere |= defined
        undefined_by_object[path] = undefined
    leaves: set[str] = set()
    unlisted: dict[str, list[tuple[Path, str]]] = {}
    for path in objects:
        for name in sorted(undefined_by_object[path] - defined_anywhere):
            bare: str = normalised(name)
            leaves.add(bare)
            if not is_allowed(component, family, name, sanitized):
                unlisted.setdefault(bare, []).append((path, name))
    seen: dict[tuple[str, str], int] = {}
    for bare, where in sorted(unlisted.items()):
        seen[(family, bare)] = len(where)
        limit: int = allowance.get((family, bare), 0)
        if limit == 0:
            for path, name in where:
                report.findings.append(f'LEAF {path}: {name}')
        elif len(where) > limit:
            report.findings.append(f'OVER {family} {bare} {len(where)} {limit}')
    for (line_family, bare), limit in sorted(allowance.items()):
        if line_family == family and seen.get((family, bare), 0) < limit:
            report.findings.append(f'STALE {family} {bare} {limit} {seen.get((family, bare), 0)}')
    must_see: str = component.families[family].must_see
    if must_see not in leaves:
        report.findings.append(f'BLIND {family} {must_see}')
    report.leaves = sorted(leaves)
    if seen_out is not None:
        seen_out.update(seen)
    return report


def describe(component: Component) -> str:
    """--list: every allowed leaf with its reason, for review."""
    lines: list[str] = [f'component {component.name}']
    for family in component.families.values():
        lines.append(f'family {family.name} (must see {family.must_see})')
        lines += [f'  {entry.name}: {entry.reason}' for entry in family.allowed]
    lines.append('toolchain (every family)')
    lines += [f'  {entry.name}: {entry.reason}' for entry in component.toolchain]
    lines.append('sanitized builds only')
    lines += [f'  {entry.name}: {entry.reason}' for entry in component.sanitized]
    return '\n'.join(lines)


def main(arguments: list[str] | None = None, reader: SymbolReader = symbols_of,
         find_objects: Callable[[Path, str], list[Path]] = objects_under) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--config', required=True, type=Path, help='the component file (TOML)')
    parser.add_argument('--family', help='the host family to check (a [families.<name>] of the config)')
    parser.add_argument('--nm', default='nm', help='the toolchain nm (llvm-nm, or CMAKE_NM)')
    parser.add_argument('--build-dir', type=Path, help='the build tree the [objects] targets live in')
    parser.add_argument('--objects', type=Path, help='an object directory, instead of the config targets')
    parser.add_argument('--under', help='keep only objects whose path below the directory starts with this')
    parser.add_argument('--sanitized', action='store_true', help='allow the [sanitized] entries too')
    parser.add_argument('--allowance', type=Path, help='the shrink-only allowance file')
    parser.add_argument('--write-allowance', action='store_true', help='lower or drop stale allowance lines')
    parser.add_argument('--json', dest='json_path', type=Path, help='write the report as JSON here')
    parser.add_argument('--list', action='store_true', help='print every allowed leaf with its reason')
    options = parser.parse_args(arguments)
    try:
        component: Component = load_component(options.config)
        if options.list:
            print(describe(component))
            return EXIT_PASS
        if options.family not in component.families:
            raise ConfigError(f'--family must be one of: {", ".join(sorted(component.families))}')
        under: str = component.under if options.under is None else options.under
        objects: list[Path] = []
        for directory in object_directories(component, options.build_dir, options.objects):
            objects += find_objects(directory, under)
        if not objects:
            raise ConfigError(f'no objects found (under "{under}"); build first')
        allowance: dict[tuple[str, str], int] = read_allowance(options.allowance)
        seen: dict[tuple[str, str], int] = {}
        report: Report = check(component, options.family, objects, options.nm, options.sanitized, allowance,
                               reader, seen)
    except (ConfigError, RuntimeError) as problem:
        print(f'ERROR {problem}')
        return EXIT_ERROR
    if options.write_allowance:
        if options.allowance is None:
            print('ERROR --write-allowance needs --allowance')
            return EXIT_ERROR
        write_allowance(options.allowance, allowance, seen, options.family)
    for finding in report.findings:
        print(finding)
    print(f'leaves: {", ".join(report.leaves)}')
    verdict: str = 'PASS' if report.passed else 'FAIL'
    print(f'SUMMARY {report.component} {report.family}: {verdict} '
          f'({report.objects} objects, {len(report.leaves)} leaves)')
    if options.json_path is not None:
        options.json_path.write_text(json.dumps({
            'component': report.component, 'family': report.family, 'passed': report.passed,
            'objects': report.objects, 'leaves': report.leaves, 'findings': report.findings}, indent=2) + '\n',
            encoding='utf-8')
    return EXIT_PASS if report.passed else EXIT_FINDINGS


if __name__ == '__main__':
    sys.exit(main())
