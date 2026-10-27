#!/usr/bin/env python3
"""The project file: every product-specific name the harness tools use.

A product describes itself once, in a TOML file (projects/gui_forms.toml is
GUI.Forms'). The tools read it through current(), which loads the file named
by HARNESS_PROJECT, or else harness.toml at the workspace root. The
workspace root is HARNESS_WORKSPACE when set, else the current directory; it
holds the product's checkout (source_dir) and the CI state (.ci/, .ccache/,
.build/), as GitHub's workspace does today.

The names derived here are the names on disk and on GitHub: artifact and
release asset names, environment variables, build directories. A product
that changes a field starts a new history of caches and receipts, so a
field is changed deliberately or not at all.

    python3 harness/project.py show                 print the loaded project
    python3 harness/project.py linux-packages NAME  print a package profile, space-separated

TOML is read with tomllib (Python 3.11 and later). Every CI job that runs the
tools sets up Python 3.12 or 3.14, the Windows toolchain itself needs 3.14,
and Ubuntu 24.04's own python3 is 3.12, so no supported host lacks it.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tomllib
from typing import Any

PROJECT_VARIABLE: str = 'HARNESS_PROJECT'
WORKSPACE_VARIABLE: str = 'HARNESS_WORKSPACE'
DEFAULT_PROJECT_NAME: str = 'harness.toml'
SCHEMA: int = 1


@dataclass(frozen=True)
class StateMachineArea:
    """A path prefix under the source root and the owning area its findings are grouped under."""
    prefix: str
    area: str


@dataclass(frozen=True)
class Project:
    """One product's names, as read from its project file (see projects/gui_forms.toml)."""
    path: Path
    name: str
    # The product's own repository (owner/name), recorded in manifests.
    repository: str
    # The product's checkout, relative to the workspace root, without a slash.
    source_dir: str
    # The build tree's directory name under .build/native-<platform>/.
    build_name: str
    # Environment variables are <env_prefix>_<NAME> (GUI_FORMS_LLVM_RUNTIME).
    env_prefix: str
    # Every workflow artifact name starts with it (gui-validated-..., gui-tree-...).
    artifact_prefix: str
    # A compiler-cache archive is <cache_asset_prefix><platform>.tar.gz.
    cache_asset_prefix: str
    # The workflow whose runs may produce receipts and caches, and its native job's name.
    native_workflow: str
    native_job: str
    # The repository whose releases hold the Windows toolchain; empty means GITHUB_REPOSITORY.
    toolchain_repository: str
    # A toolchain archive is <toolchain_asset_prefix><platform>.tar.zst.
    toolchain_asset_prefix: str
    # Under the source root: the pinned cache revisions and the test-area rules.
    pinned_cache_revisions: str
    test_areas_file: str
    # A test file's stem X names the tests X and <prefix>X for each prefix.
    test_name_prefixes: tuple[str, ...]
    # Ubuntu package profiles for install_linux_ci_packages.sh, in install order.
    linux_packages: dict[str, tuple[str, ...]]
    # check_state_machines.py: the first-party directories, the atom-vocabulary
    # headers, the area of each path prefix (most specific first), and the paths
    # whose change widens a changed-code run to the full tree.
    first_party_dirs: tuple[str, ...]
    vocabulary_headers: tuple[str, ...]
    state_machine_areas: tuple[StateMachineArea, ...]
    full_tree_triggers: tuple[str, ...]

    def variable(self, name: str) -> str:
        """The environment variable <env_prefix>_<name>."""
        result: str = self.env_prefix + '_' + name
        return result

    def artifact(self, suffix: str) -> str:
        """A workflow artifact's name: the prefix, then the suffix (validated-..., tree-...)."""
        result: str = self.artifact_prefix + suffix
        return result

    def cache_asset(self, platform: str) -> str:
        """The compiler-cache archive's file and release asset name for a platform."""
        result: str = self.cache_asset_prefix + platform + '.tar.gz'
        return result

    def workspace_root(self) -> Path:
        result: Path = workspace_root()
        return result

    def source_root(self) -> Path:
        result: Path = workspace_root() / self.source_dir
        return result

    def build_tree(self, platform: str, sanitize: bool = False) -> str:
        """The build tree relative to the workspace root: .build/native-<platform>/<build_name>[-sanitize]."""
        name: str = self.build_name + ('-sanitize' if sanitize else '')
        result: str = '.build/native-' + platform + '/' + name
        return result


def workspace_root() -> Path:
    named: str = os.environ.get(WORKSPACE_VARIABLE, '')
    result: Path = Path(named).resolve() if named else Path.cwd().resolve()
    return result


def text_field(table: dict[str, Any], key: str, where: str) -> str:
    value: Any = table.get(key)
    if type(value) is not str:
        raise ValueError(where + '.' + key + ' must be a string')
    return value


def text_list(table: dict[str, Any], key: str, where: str) -> tuple[str, ...]:
    value: Any = table.get(key)
    if type(value) is not list:
        raise ValueError(where + '.' + key + ' must be a list of strings')
    entry: Any
    for entry in value:
        if type(entry) is not str:
            raise ValueError(where + '.' + key + ' must be a list of strings')
    result: tuple[str, ...] = tuple(value)
    return result


def table_field(document: dict[str, Any], key: str) -> dict[str, Any]:
    value: Any = document.get(key)
    if type(value) is not dict:
        raise ValueError('[' + key + '] is missing')
    return value


def package_profiles(table: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Each profile's packages: an `extends` profile's packages first, then its own."""
    profiles: dict[str, tuple[str, ...]] = {}
    name: str
    for name in table:
        profile: Any = table[name]
        if type(profile) is not dict:
            raise ValueError('[linux_packages.' + name + '] must be a table')
        packages: tuple[str, ...] = text_list(profile, 'packages', 'linux_packages.' + name)
        base: Any = profile.get('extends', '')
        if base:
            if type(base) is not str or base not in table or base == name:
                raise ValueError('linux_packages.' + name + '.extends must name another profile')
            if table[base].get('extends'):
                raise ValueError('linux_packages.' + name + ' extends a profile that itself extends one')
            packages = text_list(table[base], 'packages', 'linux_packages.' + base) + packages
        profiles[name] = packages
    return profiles


def state_machine_areas(value: Any) -> tuple[StateMachineArea, ...]:
    if type(value) is not list:
        raise ValueError('state_machines.areas must be a list of [prefix, area] pairs')
    areas: list[StateMachineArea] = []
    pair: Any
    for pair in value:
        if type(pair) is not list or len(pair) != 2 or type(pair[0]) is not str or type(pair[1]) is not str:
            raise ValueError('state_machines.areas must be a list of [prefix, area] pairs')
        areas.append(StateMachineArea(pair[0], pair[1]))
    result: tuple[StateMachineArea, ...] = tuple(areas)
    return result


def parse(document: dict[str, Any], path: Path) -> Project:
    """Validate a parsed project file whole before building the record."""
    if document.get('schema') != SCHEMA:
        raise ValueError(str(path) + ': schema must be ' + str(SCHEMA))
    product: dict[str, Any] = table_field(document, 'project')
    ci: dict[str, Any] = table_field(document, 'ci')
    toolchain: dict[str, Any] = table_field(document, 'toolchain')
    tests: dict[str, Any] = table_field(document, 'tests')
    packages: dict[str, Any] = table_field(document, 'linux_packages')
    machines: dict[str, Any] = table_field(document, 'state_machines')
    source_dir: str = text_field(product, 'source_dir', 'project')
    if not source_dir or source_dir.endswith('/') or '\\' in source_dir:
        raise ValueError('project.source_dir must be a relative directory without a trailing slash')
    project: Project = Project(
        path=path,
        name=text_field(product, 'name', 'project'),
        repository=text_field(product, 'repository', 'project'),
        source_dir=source_dir,
        build_name=text_field(product, 'build_name', 'project'),
        env_prefix=text_field(product, 'env_prefix', 'project'),
        artifact_prefix=text_field(ci, 'artifact_prefix', 'ci'),
        cache_asset_prefix=text_field(ci, 'cache_asset_prefix', 'ci'),
        native_workflow=text_field(ci, 'native_workflow', 'ci'),
        native_job=text_field(ci, 'native_job', 'ci'),
        toolchain_repository=text_field(toolchain, 'repository', 'toolchain'),
        toolchain_asset_prefix=text_field(toolchain, 'asset_prefix', 'toolchain'),
        pinned_cache_revisions=text_field(ci, 'pinned_cache_revisions', 'ci'),
        test_areas_file=text_field(tests, 'areas_file', 'tests'),
        test_name_prefixes=text_list(tests, 'name_prefixes', 'tests'),
        linux_packages=package_profiles(packages),
        first_party_dirs=text_list(machines, 'first_party_dirs', 'state_machines'),
        vocabulary_headers=text_list(machines, 'vocabulary_headers', 'state_machines'),
        state_machine_areas=state_machine_areas(machines.get('areas')),
        full_tree_triggers=text_list(machines, 'full_tree_triggers', 'state_machines'))
    return project


def load(path: Path) -> Project:
    with path.open('rb') as stream:
        document: dict[str, Any] = tomllib.load(stream)
    project: Project = parse(document, path.resolve())
    return project


def project_path() -> Path:
    named: str = os.environ.get(PROJECT_VARIABLE, '')
    if named:
        return Path(named)
    candidate: Path = workspace_root() / DEFAULT_PROJECT_NAME
    if candidate.is_file():
        return candidate
    raise RuntimeError('No project file: set ' + PROJECT_VARIABLE + ' or put ' + DEFAULT_PROJECT_NAME
                       + ' at the workspace root (' + str(workspace_root()) + ')')


_loaded: Project | None = None


def current() -> Project:
    """The project the tools run for, loaded once per process."""
    global _loaded
    if _loaded is None:
        _loaded = load(project_path())
    return _loaded


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__,
                                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    commands: argparse._SubParsersAction = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('show', help='print the loaded project')
    packages: argparse.ArgumentParser = commands.add_parser('linux-packages', help='print a package profile')
    packages.add_argument('profile')
    args: argparse.Namespace = parser.parse_args(argv)
    project: Project = current()
    if args.command == 'show':
        print(project)
        return 0
    if args.profile not in project.linux_packages:
        expected: str = ' or '.join(sorted(project.linux_packages))
        print('unknown package profile: ' + args.profile + ' (expected ' + expected + ')', file=sys.stderr)
        return 2
    print(' '.join(project.linux_packages[args.profile]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
