#!/usr/bin/env python3
"""Set up a workspace for one product: its checkout, the notes, the toolchain, the caches, a smoke build.

bootstrap.sh (and bootstrap.ps1, which finds Git Bash) runs this with a
Python of 3.11 or later:

    sh bootstrap.sh --workspace DIR --project NAME [--in-house] [--dry-run]
                    [--platform P] [--no-restore] [--no-smoke]
                    [--github-base URL] [--project-file PATH]

NAME selects projects/NAME.toml in this repository (--project-file names
another file). In order:

1. the product: <github>/<repository>.git cloned into DIR/<source_dir> on
   its default branch, or into DIR itself when source_dir is "." (the
   product at the workspace root), with its submodules checked out at the
   commits it records. When it has a harness.lock,
   DIR/template_harness becomes that commit (harness/lock.py), and the
   restore and the smoke build use that harness and its projects/NAME.toml;
2. the notes: <github>/<notes_repository>.git cloned into DIR/notes with
   main checked out, and the project's notes branch (notes_branch, by
   default the project name) as the worktree DIR/notes-<branch>;
3. with --in-house only: <github>/<projects_repository>.git into
   DIR/projects. It is private; without access the bootstrap stops and
   says so;
4. the toolchain and the compiler cache, through harness/restore_local.py
   (skipped with --no-restore; the cache also when the project publishes
   none, having no ci.cache_asset_prefix);
5. the project's [bootstrap] smoke command, run from DIR (skipped with
   --no-smoke, or when the project names none).

With the product at the workspace root, template_harness/, notes/,
notes-<branch>/, projects/ and the CI state (.build/, .ccache/, .ci/) all
sit inside the product's checkout. After the clones the bootstrap asks
git whether the checkout ignores each of them (git check-ignore) and warns
about any it does not: the product's .gitignore should list them
(docs/adopting.md).

Re-running updates instead of re-cloning: each clone is fetched, and a
checkout that is on its tracked branch with no uncommitted changes to
tracked files is fast-forwarded. Any other checkout is left as it is, with
the reason printed. The product's submodules follow a cloned or
fast-forwarded checkout (git submodule update --init --recursive, which
refuses to overwrite local changes in them); a checkout left as it is
keeps them as they are. Nothing is ever reset, deleted or force-updated.

--dry-run prints every step's command and changes nothing (it still reads
the state of existing clones to choose the steps). --github-base replaces
https://github.com, so tests can stand local bare repositories in for the
GitHub ones. The steps after the clones run with HARNESS_PROJECT and
HARNESS_WORKSPACE set.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import platform as host_platform
import shlex
import subprocess
import sys

import lock
import project

HARNESS: Path = Path(__file__).resolve().parent
REPOSITORY: Path = HARNESS.parent
GITHUB: str = 'https://github.com'
PLATFORMS: tuple[str, ...] = ('windows-x64', 'linux-x64', 'linux-arm64', 'macos-arm64')
PYTHON_ARGUMENT: str = '{python}'
# The pinned harness's directory in the workspace, beside the product (owner decision 2).
HARNESS_DIRECTORY: str = 'template_harness'
# What a product at the workspace root must ignore: the directories the
# bootstrap and the tools put there (projects/ only with --in-house), including
# restore_local.py's TOOLCHAIN_DIRECTORY and DOWNLOAD_DIRECTORY.
ROOT_LAYOUT_ENTRIES: tuple[str, ...] = (HARNESS_DIRECTORY + '/', 'notes/', '.build/', '.ccache/', '.ci/',
                                        '.toolchain/', '.bootstrap/')
# Written by restore_local.py (its TOOLCHAIN_PATH_RECORD).
TOOLCHAIN_PATH_RECORD: str = '.ci/local-toolchain-path.txt'


class BootstrapError(Exception):
    """An expected failure; the bootstrap prints it and exits 1."""


@dataclass(frozen=True)
class Plan:
    """What one bootstrap run does, decided from its arguments and the project file."""
    workspace: Path
    product: project.Project
    github: str
    platform: str
    in_house: bool
    restore: bool
    smoke: bool
    dry_run: bool


def host_platform_name() -> str:
    """This host as a CI platform name (windows-x64, linux-x64, linux-arm64, macos-arm64)."""
    machine: str = host_platform.machine().lower()
    arm: bool = machine in ('arm64', 'aarch64')
    if sys.platform == 'win32':
        return 'windows-x64'
    if sys.platform == 'darwin':
        return 'macos-arm64'
    return 'linux-arm64' if arm else 'linux-x64'


def repository_url(github: str, repository: str) -> str:
    url: str = github.rstrip('/') + '/' + repository + '.git'
    return url


def say(text: str) -> None:
    print(text, flush=True)


def git_query(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """A read-only git command; runs in a dry run too."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=False)
    return completed


def change(plan: Plan, command: list[str], what: str, cwd: Path | None = None,
           environment: dict[str, str] | None = None) -> None:
    """A step that changes something: printed, then run unless this is a dry run; a failure raises."""
    say(('would run: ' if plan.dry_run else '+ ') + shlex.join(command))
    if plan.dry_run:
        return
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        command, cwd=None if cwd is None else str(cwd), env=environment, capture_output=True, text=True,
        check=False)
    if completed.stdout.strip():
        say(completed.stdout.rstrip())
    if completed.returncode != 0:
        raise BootstrapError(what + ' failed: ' + completed.stderr.strip())


def fast_forward(plan: Plan, checkout: Path, branch: str) -> bool:
    """Fast-forwards checkout to origin/<branch> when it is on branch and has no tracked changes;
    whether it did (False: the checkout was left as it is, and says why)."""
    current: str = git_query(checkout, 'branch', '--show-current').stdout.strip()
    if current != branch:
        say(str(checkout) + ' is on ' + (current or 'a detached HEAD') + ', not ' + branch + '; left as it is')
        return False
    changes: str = git_query(checkout, 'status', '--porcelain', '--untracked-files=no').stdout.strip()
    if changes:
        say(str(checkout) + ' has uncommitted changes; left as it is')
        return False
    change(plan, ['git', '-C', str(checkout), 'merge', '--ff-only', '--quiet', 'origin/' + branch],
           'fast-forwarding ' + str(checkout))
    return True


def sync_clone(plan: Plan, url: str, checkout: Path, branch: str | None, what: str) -> bool:
    """Clones url into checkout (on branch, or the default branch), or fetches and fast-forwards an existing
    clone; whether checkout is now at its tracked branch's tip (False: an existing clone was left as it is)."""
    if (checkout / '.git').exists():
        say(what + ': updating ' + str(checkout))
        change(plan, ['git', '-C', str(checkout), 'fetch', '--quiet', 'origin'], 'fetching ' + what)
        tracked: str = branch if branch is not None else default_branch(checkout)
        moved: bool = fast_forward(plan, checkout, tracked)
        return moved
    if checkout.exists() and any(checkout.iterdir()):
        raise BootstrapError(str(checkout) + ' exists, is not empty and is not a git clone; move it aside')
    say(what + ': cloning ' + url)
    command: list[str] = ['git', 'clone', '--quiet']
    if branch is not None:
        command.extend(['--branch', branch])
    command.extend([url, str(checkout)])
    change(plan, command, 'cloning ' + what + ' from ' + url)
    return True


def sync_submodules(plan: Plan, checkout: Path, settled: bool) -> None:
    """Checks the product's submodules out at the commits it records (File Manager's gui_forms/ and
    backend/), after a clone or a fast-forward (settled). A checkout left as it is keeps its submodules
    as they are, so local work in them is never overwritten."""
    command: list[str] = ['git', '-C', str(checkout), 'submodule', 'update', '--init', '--recursive']
    if plan.dry_run and not (checkout / '.git').exists():
        say('would run: ' + shlex.join(command) + ' (when the product has .gitmodules)')
        return
    if not (checkout / '.gitmodules').is_file():
        return
    if not settled:
        say('the product: its submodules are left as they are')
        return
    say('the product: updating its submodules')
    change(plan, command, "updating the product's submodules")


def default_branch(checkout: Path) -> str:
    """The branch origin/HEAD names in a clone (main when it names none)."""
    head: str = git_query(checkout, 'symbolic-ref', '--short', 'refs/remotes/origin/HEAD').stdout.strip()
    branch: str = head.partition('/')[2] if head else 'main'
    return branch


def sync_notes_worktree(plan: Plan, notes: Path, branch: str) -> None:
    """Adds the notes branch as the worktree notes-<branch> beside notes/, or fast-forwards it."""
    worktree: Path = notes.parent / ('notes-' + branch)
    if (worktree / '.git').exists():
        say('notes branch ' + branch + ': updating ' + str(worktree))
        fast_forward(plan, worktree, branch)
        return
    if worktree.exists() and any(worktree.iterdir()):
        raise BootstrapError(str(worktree) + ' exists, is not empty and is not a worktree; move it aside')
    say('notes branch ' + branch + ': adding the worktree ' + str(worktree))
    if not (notes / '.git').exists():
        # A dry run on an empty workspace: the clone does not exist yet.
        change(plan, ['git', '-C', str(notes), 'worktree', 'add', '--track', '-b', branch, str(worktree),
                      'origin/' + branch], 'adding the notes worktree')
        return
    local: bool = git_query(notes, 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch).returncode == 0
    if local:
        change(plan, ['git', '-C', str(notes), 'worktree', 'add', str(worktree), branch], 'adding the notes worktree')
        return
    remote: bool = git_query(notes, 'rev-parse', '--verify', '--quiet',
                             'refs/remotes/origin/' + branch).returncode == 0
    if not remote:
        raise BootstrapError('The notes repository has no branch ' + branch + ' (the project\'s notes_branch)')
    change(plan, ['git', '-C', str(notes), 'worktree', 'add', '--track', '-b', branch, str(worktree),
                  'origin/' + branch], 'adding the notes worktree')


@dataclass(frozen=True)
class PinnedHarness:
    """The harness the steps after the clones use, and the project file read from it."""
    root: Path
    product: project.Project


def product_checkout(plan: Plan) -> Path:
    """The product's checkout: DIR itself for a product at the workspace root, else DIR/<source_dir>."""
    checkout: Path = plan.workspace if plan.product.at_workspace_root() else plan.workspace / plan.product.source_dir
    return checkout


def unignored_entries(plan: Plan) -> list[str]:
    """The workspace entries a root-layout product's checkout does not ignore (empty for any other layout)."""
    if not plan.product.at_workspace_root() or not (plan.workspace / '.git').exists():
        return []
    entries: list[str] = list(ROOT_LAYOUT_ENTRIES)
    if plan.product.bootstrap is not None:
        entries.append('notes-' + plan.product.bootstrap.notes_branch + '/')
    if plan.in_house:
        entries.append('projects/')
    unignored: list[str] = []
    entry: str
    for entry in entries:
        # check-ignore: 0 ignored, 1 not ignored; a directory path ends in / so
        # it matches a directory pattern before the directory exists.
        if git_query(plan.workspace, 'check-ignore', '--quiet', entry).returncode != 0:
            unignored.append(entry)
    return unignored


def warn_unignored(plan: Plan) -> None:
    entries: list[str] = unignored_entries(plan)
    if entries:
        say('warning: the product is at the workspace root, and its checkout does not ignore '
            + ', '.join(entries) + '; add them to its .gitignore (docs/adopting.md)')


def pin_harness(plan: Plan) -> PinnedHarness:
    """The workspace's template_harness at the product's harness.lock commit, or this harness without a lock.

    The pinned harness's projects/<name>.toml replaces the bootstrap's project
    file when it has one, so the restore and the smoke build are the pinned
    harness's own.
    """
    product: project.Project = plan.product
    lock_path: Path = product_checkout(plan) / lock.LOCK_NAME
    if not lock_path.is_file():
        if plan.dry_run and not (product_checkout(plan) / '.git').exists():
            say('harness: would read ' + str(lock_path) + ' once the product is cloned; this harness meanwhile')
        else:
            say('harness: the product has no ' + lock.LOCK_NAME + '; using this harness, ' + str(REPOSITORY))
        return PinnedHarness(REPOSITORY, product)
    target: Path = plan.workspace / HARNESS_DIRECTORY
    try:
        pinned: lock.Lock = lock.load(lock_path)
        say('harness: ' + pinned.repository + ' at ' + pinned.commit + ' (' + lock.LOCK_NAME + ') in ' + str(target))
        if plan.dry_run:
            say('would run: ' + shlex.join([sys.executable, '-B', str(HARNESS / 'lock.py'), '--lock', str(lock_path),
                                            'checkout', str(target)]))
            return PinnedHarness(REPOSITORY, product)
        lock.checkout(pinned, target, plan.github)
    except lock.LockError as error:
        raise BootstrapError('pinning the harness failed: ' + str(error)) from error
    pinned_project: Path = target / 'projects' / (product.name + '.toml')
    if not pinned_project.is_file():
        say('harness: the pinned harness has no ' + str(pinned_project) + '; keeping ' + str(product.path))
        return PinnedHarness(target, product)
    return PinnedHarness(target, project.load(pinned_project))


def step_environment(plan: Plan, harness: PinnedHarness) -> dict[str, str]:
    """The environment of the steps after the clones: the project and workspace variables, and the
    restored Windows toolchain (restore_local.TOOLCHAIN_PATH_RECORD) before PATH when there is one."""
    environment: dict[str, str] = dict(os.environ)
    record: Path = plan.workspace / TOOLCHAIN_PATH_RECORD
    if record.is_file():
        directories: list[str] = record.read_text(encoding='utf-8').splitlines()
        environment['PATH'] = os.pathsep.join(directories + [environment.get('PATH', '')])
    environment[project.PROJECT_VARIABLE] = str(harness.product.path)
    environment[project.WORKSPACE_VARIABLE] = str(plan.workspace)
    return environment


def smoke_command(product: project.Project) -> list[str]:
    command: list[str] = []
    argument: str
    for argument in product.bootstrap.smoke if product.bootstrap is not None else ():
        command.append(sys.executable if argument == PYTHON_ARGUMENT else argument)
    return command


def bootstrap(plan: Plan) -> None:
    product: project.Project = plan.product
    if product.bootstrap is None:
        raise BootstrapError('[bootstrap] is missing from ' + str(product.path))
    if not plan.dry_run:
        plan.workspace.mkdir(parents=True, exist_ok=True)
    say('Workspace ' + str(plan.workspace) + ' for ' + product.name + ' (' + plan.platform + ')')
    settled: bool = sync_clone(plan, repository_url(plan.github, product.repository), product_checkout(plan), None,
                               'the product')
    sync_submodules(plan, product_checkout(plan), settled)
    harness: PinnedHarness = pin_harness(plan)
    notes: Path = plan.workspace / 'notes'
    sync_clone(plan, repository_url(plan.github, product.bootstrap.notes_repository), notes, 'main', 'the notes')
    sync_notes_worktree(plan, notes, product.bootstrap.notes_branch)
    if plan.in_house:
        projects_url: str = repository_url(plan.github, product.bootstrap.projects_repository)
        try:
            sync_clone(plan, projects_url, plan.workspace / 'projects', None, 'the projects repository')
        except BootstrapError as error:
            raise BootstrapError(str(error) + '\n' + product.bootstrap.projects_repository
                                 + ' is private: --in-house needs read access to it') from error
    warn_unignored(plan)
    environment: dict[str, str] = step_environment(plan, harness)
    if plan.restore:
        kinds: list[str] = ['toolchain']
        if harness.product.cache_asset_prefix is not None:
            kinds.append('cache')
        else:
            say('Cache restore skipped (the project publishes no compiler caches: no ci.cache_asset_prefix)')
        kind: str
        for kind in kinds:
            command: list[str] = [sys.executable, '-B', str(harness.root / 'harness' / 'restore_local.py'), kind,
                                  '--platform', plan.platform]
            change(plan, command, 'restoring the ' + kind, plan.workspace, environment)
    else:
        say('Toolchain and cache restore skipped (--no-restore)')
    smoke: list[str] = smoke_command(harness.product)
    environment = step_environment(plan, harness)
    if not plan.smoke or not smoke:
        say('Smoke build skipped' + (' (--no-smoke)' if not plan.smoke else ' (the project names none)'))
    else:
        change(plan, smoke, 'the smoke build', plan.workspace, environment)
    say('Bootstrap finished' + (' (dry run: nothing was changed)' if plan.dry_run else ''))


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--project', default='', help='projects/<NAME>.toml in this repository')
    parser.add_argument('--project-file', type=Path, help='a project file elsewhere, instead of --project')
    parser.add_argument('--in-house', action='store_true', help='also clone the private projects repository')
    parser.add_argument('--dry-run', action='store_true', help='print every step; change nothing')
    parser.add_argument('--platform', choices=PLATFORMS, default='')
    parser.add_argument('--no-restore', action='store_true')
    parser.add_argument('--no-smoke', action='store_true')
    parser.add_argument('--github-base', default=GITHUB, help='the base URL of the repositories (tests)')
    args: argparse.Namespace = parser.parse_args(argv)
    if (args.project_file is None) == (not args.project):
        parser.error('give exactly one of --project and --project-file')
    path: Path = args.project_file if args.project_file is not None else REPOSITORY / 'projects' / (args.project + '.toml')
    try:
        if not path.is_file():
            raise BootstrapError('No project file ' + str(path))
        plan: Plan = Plan(workspace=args.workspace.resolve(), product=project.load(path), github=args.github_base,
                          platform=args.platform or host_platform_name(), in_house=args.in_house,
                          restore=not args.no_restore, smoke=not args.no_smoke, dry_run=args.dry_run)
        bootstrap(plan)
    except (BootstrapError, ValueError) as error:
        print('bootstrap: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
