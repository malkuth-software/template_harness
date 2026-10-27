#!/usr/bin/env python3
"""The project file: every product-specific name the harness tools use.

A product describes itself once, in a TOML file (projects/gui_forms.toml is
GUI.Forms'). The tools read it through current(), which loads the file named
by HARNESS_PROJECT, or else harness.toml at the workspace root. The
workspace root is HARNESS_WORKSPACE when set, else the current directory; it
holds the product's checkout (source_dir) and the CI state (.ci/, .ccache/,
.build/), as GitHub's workspace does today. With source_dir "." the
product's checkout is the workspace root itself, and the harness and the CI
state sit inside it (File Manager's layout).

A few fields may be left out (ci.cache_asset_prefix, ci.pinned_cache_revisions,
tests.areas_file, the [state_machines] table, and the [checkpoint] and
[bootstrap] tables); a tool that needs one fails with MissingField, "project
<name> has no <field>", and the others run.

The names derived here are the names on disk and on GitHub: artifact and
release asset names, environment variables, build directories. A product
that changes a field starts a new history of caches and receipts, so a
field is changed deliberately or not at all.

    python3 harness/project.py show                 print the loaded project
    python3 harness/project.py linux-packages NAME  print a package profile, space-separated
    python3 harness/project.py variable NAME        print the environment variable <env_prefix>_NAME

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
# source_dir for a product whose checkout is the workspace root: the harness
# (template_harness/), the notes and the CI state (.build/, .ccache/, .ci/)
# then sit inside the checkout, which must ignore them.
ROOT_SOURCE_DIR: str = '.'
# In a native_job template, the platform (linux-x64, windows-x64, ...).
PLATFORM_PLACEHOLDER: str = '{platform}'


class MissingField(ValueError):
    """A tool needs a project-file field that this product's file leaves out."""


@dataclass(frozen=True)
class StateMachineArea:
    """A path prefix under the source root and the owning area its findings are grouped under."""
    prefix: str
    area: str


@dataclass(frozen=True)
class CheckpointTable:
    """[checkpoint], for checkpoint/checkpoint.py (its docstring explains each field)."""
    # The integration tree, relative to the workspace root or absolute.
    tree: str
    verify: tuple[str, ...]
    base: str
    remote: str
    branch_prefix: str
    merge_method: str


@dataclass(frozen=True)
class BootstrapTable:
    """[bootstrap], for harness/bootstrap.py: the notes and projects repositories and the smoke build."""
    notes_repository: str
    # The notes side branch checked out as a worktree beside notes/ (default: the project name).
    notes_branch: str
    projects_repository: str
    # Run from the workspace root; the argument {python} becomes the running interpreter.
    smoke: tuple[str, ...]


@dataclass(frozen=True)
class Project:
    """One product's names, as read from its project file (see projects/gui_forms.toml)."""
    path: Path
    name: str
    # The product's own repository (owner/name), recorded in manifests.
    repository: str
    # The product's checkout, relative to the workspace root, without a slash;
    # "." when the product's checkout is the workspace root itself (ROOT_SOURCE_DIR).
    source_dir: str
    # The build tree's directory name under .build/native-<platform>/.
    build_name: str
    # Environment variables are <env_prefix>_<NAME> (GUI_FORMS_LLVM_RUNTIME).
    env_prefix: str
    # Every workflow artifact name starts with it (gui-validated-..., gui-tree-...).
    artifact_prefix: str
    # A compiler-cache archive is <cache_asset_prefix><platform>.tar.gz; None
    # for a product that publishes no compiler caches (cache_asset fails).
    cache_asset_prefix: str | None
    # The workflow whose runs may produce receipts and caches, and its native
    # job's name: a prefix ("native" matches "native (<runner>, <platform>, ...)")
    # or a template with {platform} that a job's whole name must equal
    # (native_job_matches).
    native_workflow: str
    native_job: str
    # The repository the Windows toolchain manifest records as its publisher.
    # Releases are read from the running repository (GITHUB_REPOSITORY), as before.
    toolchain_repository: str
    # A toolchain archive is <toolchain_asset_prefix><platform>.tar.zst.
    toolchain_asset_prefix: str
    # Under the source root: the pinned cache revisions and the test-area rules;
    # None when the product has none (the tools that read them fail).
    pinned_cache_revisions: str | None
    test_areas_file: str | None
    # A test file's stem X names the tests X and <prefix>X for each prefix.
    test_name_prefixes: tuple[str, ...]
    # Ubuntu package profiles for install_linux_ci_packages.sh, in install order.
    linux_packages: dict[str, tuple[str, ...]]
    # check_state_machines.py: the first-party directories, the atom-vocabulary
    # headers, the area of each path prefix (most specific first), and the paths
    # whose change widens a changed-code run to the full tree. Without a
    # [state_machines] table, has_state_machines is false and the lists are empty.
    has_state_machines: bool
    first_party_dirs: tuple[str, ...]
    vocabulary_headers: tuple[str, ...]
    state_machine_areas: tuple[StateMachineArea, ...]
    full_tree_triggers: tuple[str, ...]
    # ci_build_steps.py: the product's own environment variables, without the
    # prefix, that change what a configure records (SYSTEM_BUILD_TOOLS).
    configure_variable_names: tuple[str, ...]
    # cache_manifest.py: the build profile the cache records, the native build
    # options in recorded order, and each platform's overrides of them.
    cache_profile: str
    native_options: dict[str, str | int | bool]
    platform_native_options: dict[str, dict[str, str | int | bool]]
    # The optional [checkpoint] and [bootstrap] tables; None when absent.
    checkpoint: CheckpointTable | None
    bootstrap: BootstrapTable | None

    def variable(self, name: str) -> str:
        """The environment variable <env_prefix>_<name>."""
        result: str = self.env_prefix + '_' + name
        return result

    def artifact(self, suffix: str) -> str:
        """A workflow artifact's name: the prefix, then the suffix (validated-..., tree-...)."""
        result: str = self.artifact_prefix + suffix
        return result

    def missing(self, field: str) -> MissingField:
        """The error a tool raises when it needs a field this project file leaves out."""
        error: MissingField = MissingField('project ' + self.name + ' has no ' + field + ' (' + str(self.path) + ')')
        return error

    def require_cache_asset_prefix(self) -> str:
        if self.cache_asset_prefix is None:
            raise self.missing('ci.cache_asset_prefix')
        return self.cache_asset_prefix

    def require_pinned_cache_revisions(self) -> str:
        if self.pinned_cache_revisions is None:
            raise self.missing('ci.pinned_cache_revisions')
        return self.pinned_cache_revisions

    def require_test_areas_file(self) -> str:
        if self.test_areas_file is None:
            raise self.missing('tests.areas_file')
        return self.test_areas_file

    def require_state_machines(self) -> None:
        if not self.has_state_machines:
            raise self.missing('[state_machines]')

    def cache_asset(self, platform: str) -> str:
        """The compiler-cache archive's file and release asset name for a platform."""
        result: str = self.require_cache_asset_prefix() + platform + '.tar.gz'
        return result

    def native_job_matches(self, job_name: str, platform: str) -> bool:
        """Whether a run's job is this project's native job for platform.

        A native_job with {platform} is a template: the job's whole name must
        equal it with the platform substituted ("{platform}" for a matrix job
        named by its platform, "native / {platform}" for one inside a called
        workflow). Otherwise native_job is the job's name before its matrix
        values, which must list the platform: "native" matches
        "native (ubuntu-24.04, linux-x64, bash) / build".
        """
        if PLATFORM_PLACEHOLDER in self.native_job:
            expected: str = self.native_job.replace(PLATFORM_PLACEHOLDER, platform)
            return job_name == expected
        matched: bool = job_name.startswith(self.native_job + ' (') and ', ' + platform + ',' in job_name
        return matched

    def configure_variables(self) -> tuple[str, ...]:
        """The product's configure variables with their prefix (GUI_FORMS_SYSTEM_BUILD_TOOLS)."""
        names: list[str] = []
        name: str
        for name in self.configure_variable_names:
            names.append(self.variable(name))
        result: tuple[str, ...] = tuple(names)
        return result

    def native_options_for(self, platform: str) -> dict[str, str | int | bool]:
        """The native options a platform's cache records: the shared ones, each override in its place."""
        options: dict[str, str | int | bool] = dict(self.native_options)
        options.update(self.platform_native_options.get(platform, {}))
        return options

    def workspace_root(self) -> Path:
        result: Path = workspace_root()
        return result

    def at_workspace_root(self) -> bool:
        """Whether the product's checkout is the workspace root (source_dir "."), holding the harness and state."""
        result: bool = self.source_dir == ROOT_SOURCE_DIR
        return result

    def source_root(self) -> Path:
        """The product's checkout: the workspace root itself when source_dir is "."."""
        result: Path = workspace_root() if self.at_workspace_root() else workspace_root() / self.source_dir
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


def optional_text(table: dict[str, Any], key: str, where: str) -> str | None:
    """A string field that may be left out (None); present, it must be a non-empty string."""
    if key not in table:
        return None
    value: Any = table[key]
    if type(value) is not str or not value:
        raise ValueError(where + '.' + key + ' must be a non-empty string when present')
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


def option_table(value: Any, where: str) -> dict[str, str | int | bool]:
    if type(value) is not dict:
        raise ValueError('[' + where + '] must be a table')
    key: str
    for key in value:
        if type(value[key]) not in (str, int, bool):
            raise ValueError(where + '.' + key + ' must be a string, an integer or a boolean')
    result: dict[str, str | int | bool] = dict(value)
    return result


def platform_option_tables(value: Any, shared: dict[str, str | int | bool]) -> dict[str, dict[str, str | int | bool]]:
    """Each platform's overrides; an override names an option the shared table has."""
    if type(value) is not dict:
        raise ValueError('[cache_manifest.platform_options] must be a table')
    tables: dict[str, dict[str, str | int | bool]] = {}
    platform: str
    for platform in value:
        where: str = 'cache_manifest.platform_options.' + platform
        table: dict[str, str | int | bool] = option_table(value[platform], where)
        key: str
        for key in table:
            if key not in shared:
                raise ValueError(where + '.' + key + ' overrides no cache_manifest.native_options entry')
        tables[platform] = table
    return tables


MERGE_METHODS: tuple[str, ...] = ('merge', 'squash', 'rebase')


def named_text(table: dict[str, Any], key: str, where: str, default: str) -> str:
    value: Any = table.get(key, default)
    if type(value) is not str or not value:
        raise ValueError(where + '.' + key + ' must be a non-empty string')
    return value


def checkpoint_table(value: Any) -> CheckpointTable | None:
    if value is None:
        return None
    if type(value) is not dict:
        raise ValueError('[checkpoint] must be a table')
    verify: tuple[str, ...] = text_list(value, 'verify', 'checkpoint')
    if not verify:
        raise ValueError('checkpoint.verify must be a non-empty list of strings')
    merge_method: str = named_text(value, 'merge_method', 'checkpoint', 'merge')
    if merge_method not in MERGE_METHODS:
        raise ValueError('checkpoint.merge_method must be one of ' + ', '.join(MERGE_METHODS))
    table: CheckpointTable = CheckpointTable(
        tree=text_field(value, 'tree', 'checkpoint'), verify=verify,
        base=named_text(value, 'base', 'checkpoint', 'main'), remote=named_text(value, 'remote', 'checkpoint', 'origin'),
        branch_prefix=named_text(value, 'branch_prefix', 'checkpoint', 'checkpoint/'), merge_method=merge_method)
    return table


def bootstrap_table(value: Any, name: str, repository: str) -> BootstrapTable | None:
    """Defaults: <owner>/notes, the project name as the notes branch, <owner>/projects, no smoke build."""
    if value is None:
        return None
    if type(value) is not dict:
        raise ValueError('[bootstrap] must be a table')
    owner: str = repository.partition('/')[0]
    smoke: tuple[str, ...] = ()
    if 'smoke' in value:
        smoke = text_list(value, 'smoke', 'bootstrap')
    table: BootstrapTable = BootstrapTable(
        notes_repository=named_text(value, 'notes_repository', 'bootstrap', owner + '/notes'),
        notes_branch=named_text(value, 'notes_branch', 'bootstrap', name),
        projects_repository=named_text(value, 'projects_repository', 'bootstrap', owner + '/projects'),
        smoke=smoke)
    return table


# The [state_machines] values of a project file that leaves the table out.
EMPTY_STATE_MACHINES: dict[str, Any] = {
    'first_party_dirs': [], 'vocabulary_headers': [], 'areas': [], 'full_tree_triggers': []}


def parse(document: dict[str, Any], path: Path) -> Project:
    """Validate a parsed project file whole before building the record."""
    if document.get('schema') != SCHEMA:
        raise ValueError(str(path) + ': schema must be ' + str(SCHEMA))
    product: dict[str, Any] = table_field(document, 'project')
    ci: dict[str, Any] = table_field(document, 'ci')
    toolchain: dict[str, Any] = table_field(document, 'toolchain')
    tests: dict[str, Any] = table_field(document, 'tests')
    packages: dict[str, Any] = table_field(document, 'linux_packages')
    # [state_machines] may be left out: check_state_machines.py then refuses to run.
    has_state_machines: bool = 'state_machines' in document
    machines: dict[str, Any] = table_field(document, 'state_machines') if has_state_machines else EMPTY_STATE_MACHINES
    cache: dict[str, Any] = table_field(document, 'cache_manifest')
    build: dict[str, Any] = table_field(document, 'build')
    native_options: dict[str, str | int | bool] = option_table(cache.get('native_options'), 'cache_manifest.native_options')
    source_dir: str = text_field(product, 'source_dir', 'project')
    if not source_dir or source_dir.endswith('/') or '\\' in source_dir:
        raise ValueError('project.source_dir must be a relative directory without a trailing slash')
    parts: list[str] = source_dir.split('/')
    if source_dir != ROOT_SOURCE_DIR and ('.' in parts or '..' in parts or '' in parts):
        raise ValueError('project.source_dir must be "." or a directory under the workspace root, without . or ..')
    project: Project = Project(
        path=path,
        name=text_field(product, 'name', 'project'),
        repository=text_field(product, 'repository', 'project'),
        source_dir=source_dir,
        build_name=text_field(product, 'build_name', 'project'),
        env_prefix=text_field(product, 'env_prefix', 'project'),
        artifact_prefix=text_field(ci, 'artifact_prefix', 'ci'),
        cache_asset_prefix=optional_text(ci, 'cache_asset_prefix', 'ci'),
        native_workflow=text_field(ci, 'native_workflow', 'ci'),
        native_job=text_field(ci, 'native_job', 'ci'),
        toolchain_repository=text_field(toolchain, 'repository', 'toolchain'),
        toolchain_asset_prefix=text_field(toolchain, 'asset_prefix', 'toolchain'),
        pinned_cache_revisions=optional_text(ci, 'pinned_cache_revisions', 'ci'),
        test_areas_file=optional_text(tests, 'areas_file', 'tests'),
        test_name_prefixes=text_list(tests, 'name_prefixes', 'tests'),
        linux_packages=package_profiles(packages),
        has_state_machines=has_state_machines,
        first_party_dirs=text_list(machines, 'first_party_dirs', 'state_machines'),
        vocabulary_headers=text_list(machines, 'vocabulary_headers', 'state_machines'),
        state_machine_areas=state_machine_areas(machines.get('areas')),
        full_tree_triggers=text_list(machines, 'full_tree_triggers', 'state_machines'),
        configure_variable_names=text_list(build, 'configure_variables', 'build'),
        cache_profile=text_field(cache, 'profile', 'cache_manifest'),
        native_options=native_options,
        platform_native_options=platform_option_tables(cache.get('platform_options', {}), native_options),
        checkpoint=checkpoint_table(document.get('checkpoint')),
        bootstrap=bootstrap_table(document.get('bootstrap'), text_field(product, 'name', 'project'),
                                  text_field(product, 'repository', 'project')))
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
    variable: argparse.ArgumentParser = commands.add_parser('variable', help='print <env_prefix>_NAME')
    variable.add_argument('name')
    args: argparse.Namespace = parser.parse_args(argv)
    project: Project = current()
    if args.command == 'show':
        print(project)
        return 0
    if args.command == 'variable':
        print(project.variable(args.name))
        return 0
    if args.profile not in project.linux_packages:
        expected: str = ' or '.join(sorted(project.linux_packages))
        print('unknown package profile: ' + args.profile + ' (expected ' + expected + ')', file=sys.stderr)
        return 2
    print(' '.join(project.linux_packages[args.profile]))
    return 0


if __name__ == '__main__':
    sys.exit(main())
