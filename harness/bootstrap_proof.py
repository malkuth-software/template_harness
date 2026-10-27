#!/usr/bin/env python3
"""Plan and check the bootstrap proof (.github/workflows/bootstrap.yml).

    python -B harness/bootstrap_proof.py matrix --projects DIR --output FILE --summary FILE [--only NAME ...]
    python -B harness/bootstrap_proof.py snapshot --workspace DIR --checkout NAME --out FILE
    python -B harness/bootstrap_proof.py {first|again} --log FILE --workspace DIR
        --checkout NAME --notes-branch BRANCH --platform P [--before FILE]

The harness's own CI tool; products do not run it. NAME is the product's
checkout under DIR (its source_dir, "." for a product at the workspace
root); the log is the bootstrap's output.

matrix: the jobs to run, one per host for each project file in DIR that has
a [bootstrap] table and is ready. A project is ready when its notes branch
exists and the script its smoke command runs is on the product's default
branch (smoke_paths); until then it is skipped, and the reason is written to
the summary and printed as a GitHub notice. It reads GitHub through gh, so
GH_TOKEN must be set. FILE receives "matrix=<JSON>" and "count=<jobs>" lines
(GITHUB_OUTPUT); the summary is Markdown (GITHUB_STEP_SUMMARY).

snapshot: records, before a repeated run, the commits of the product and of
DIR/template_harness and the tag of the restored compiler cache
(.ci/local-cache.json), so that the check can tell an update from a repeat.

first: the run that set an empty workspace up. It cloned the product and
the notes, added the notes worktree, cloned the pinned harness (or said the
product has no harness.lock), and restored the Windows toolchain on Windows
and the compiler cache where one is published.

again (with --before): a later run, which must be a no-op update. It updated
the product, the notes and the notes worktree and cloned nothing. It may
restore a cache only when it is not the one already restored: the product's
main moved on, and a newer build of it has a published cache. It may fetch a
harness commit and restore the Windows toolchain only when harness.lock moved
the harness. Otherwise the toolchain is "already at" its directory, the cache
"already restored" (or still not published), and on macOS the runtime the
workflow built matches its manifest. Each such move is printed as "moved:".

Both: the run finished; with a harness.lock, DIR/template_harness is
detached at the commit it pins; DIR/notes-BRANCH is on BRANCH.

Only the bootstrap's own lines are read: each pattern is anchored at the
start of a line as bootstrap.py, lock.py and restore_local.py print it, so
the smoke build's output (a dependency's "Fetching ...") cannot match.

Each check prints "ok: ..." or "FAILED: ..."; any failure exits 1, and a
missing file or an unreadable lock, snapshot or project file exits 2.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import urllib.parse

import lock
import project

RUNS: tuple[str, ...] = ('first', 'again')
PLATFORMS: tuple[str, ...] = ('windows-x64', 'macos-arm64', 'linux-x64', 'linux-arm64')
HARNESS_DIRECTORY: str = 'template_harness'
# restore_local.CACHE_RECORD (test_bootstrap_proof checks that the two agree).
CACHE_RECORD: str = '.ci/local-cache.json'
# bootstrap.py.
FINISHED: re.Pattern[str] = re.compile(r'Bootstrap finished')
PRODUCT_CLONED: re.Pattern[str] = re.compile(r'the product: cloning ')
PRODUCT_UPDATED: re.Pattern[str] = re.compile(r'the product: updating ')
NOTES_CLONED: re.Pattern[str] = re.compile(r'the notes: cloning ')
NOTES_UPDATED: re.Pattern[str] = re.compile(r'the notes: updating ')
HARNESS_UNLOCKED: re.Pattern[str] = re.compile(r'harness: the product has no ' + re.escape(lock.LOCK_NAME))
# lock.py: a harness clone, or a missing pinned commit fetched into an existing one.
HARNESS_CLONED: re.Pattern[str] = re.compile(r'Cloning \S+ into ')
HARNESS_FETCHED: re.Pattern[str] = re.compile(r'Fetching [0-9a-f]{12} into ')
# restore_local.py.
TOOLCHAIN_RESTORED: re.Pattern[str] = re.compile(r'Restored the Windows toolchain ')
TOOLCHAIN_IN_PLACE: re.Pattern[str] = re.compile(r'The Windows toolchain [0-9a-f]+ is already at ')
CACHE_RESTORED: re.Pattern[str] = re.compile(r'Restored the cache of (\S+) into ')
CACHE_IN_PLACE: re.Pattern[str] = re.compile(r'The cache of \S+ is already restored')
CACHE_NOT_PUBLISHED: re.Pattern[str] = re.compile(r'No published ')
RUNTIME_IN_PLACE: re.Pattern[str] = re.compile(r'The macOS runtime at .* matches its manifest')
# The smoke command's interpreter placeholder (bootstrap.PYTHON_ARGUMENT).
PYTHON_ARGUMENT: str = '{python}'
SCRIPT_SUFFIXES: tuple[str, ...] = ('.py', '.sh', '.ps1', '.cmd', '.bat', '.cmake')


@dataclass(frozen=True)
class Host:
    runner: str
    platform: str


# The hosts every ready project is bootstrapped on.
HOSTS: tuple[Host, ...] = (Host('windows-2022', 'windows-x64'), Host('macos-15', 'macos-arm64'),
                           Host('ubuntu-24.04', 'linux-x64'))


@dataclass(frozen=True)
class Before:
    """The workspace before a repeated run (None where there was nothing to record)."""
    product: str | None
    harness: str | None
    cache: str | None


class GitHub:
    """Read-only questions about public repositories, answered through gh api."""

    def answer(self, endpoint: str, query: str) -> str | None:
        """gh api endpoint --jq query's trimmed output, or None when the request fails (a 404 included)."""
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            ['gh', 'api', endpoint, '--jq', query], capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            return None
        output: str = completed.stdout.strip()
        return output

    def default_branch(self, repository: str) -> str | None:
        branch: str | None = self.answer('repos/' + repository, '.default_branch')
        return branch or None

    def has_path(self, repository: str, path: str, ref: str) -> bool:
        endpoint: str = ('repos/' + repository + '/contents/' + urllib.parse.quote(path) + '?ref='
                         + urllib.parse.quote(ref))
        found: bool = self.answer(endpoint, '.path') is not None
        return found

    def has_branch(self, repository: str, branch: str) -> bool:
        found: bool = self.answer('repos/' + repository + '/branches/' + urllib.parse.quote(branch), '.name') is not None
        return found


def smoke_paths(smoke: tuple[str, ...]) -> list[str]:
    """The files in the product's checkout the smoke command runs, as far as its form shows them.

    {python} SCRIPT ...: SCRIPT. {python} -m unittest discover -s DIR -p FILE: DIR/FILE when FILE
    is a plain name. {python} -m another module: nothing to check. Any other program: each argument
    that is a relative path ending in a script suffix.
    """
    paths: list[str] = []
    if not smoke:
        return paths
    if smoke[0] != PYTHON_ARGUMENT:
        argument: str
        for argument in smoke[1:]:
            if not argument.startswith('-') and argument.endswith(SCRIPT_SUFFIXES):
                paths.append(argument)
        return paths
    arguments: list[str] = list(smoke[1:])
    index: int = 0
    while index < len(arguments):
        current: str = arguments[index]
        if current == '-m':
            module: str = arguments[index + 1] if index + 1 < len(arguments) else ''
            if module == 'unittest' and 'discover' in arguments:
                start: str = option_value(arguments, '-s') or '.'
                pattern: str | None = option_value(arguments, '-p')
                if pattern is not None and '*' not in pattern and '?' not in pattern:
                    paths.append(str(PurePosixPath(start) / pattern))
            return paths
        if not current.startswith('-'):
            paths.append(current)
            return paths
        index = index + 1
    return paths


def option_value(arguments: list[str], option: str) -> str | None:
    """The argument after option, or None when option is absent or last."""
    if option not in arguments:
        return None
    index: int = arguments.index(option)
    value: str | None = arguments[index + 1] if index + 1 < len(arguments) else None
    return value


def readiness(product: project.Project, github: GitHub) -> str | None:
    """None when the project can be bootstrapped now, else why not."""
    if product.bootstrap is None:
        return 'it has no [bootstrap] table'
    if not product.bootstrap.smoke:
        return 'its [bootstrap] table names no smoke command yet'
    branch: str | None = github.default_branch(product.repository)
    if branch is None:
        return product.repository + ' cannot be read (missing or private)'
    if not github.has_branch(product.bootstrap.notes_repository, product.bootstrap.notes_branch):
        return product.bootstrap.notes_repository + ' has no branch ' + product.bootstrap.notes_branch + ' yet'
    path: str
    for path in smoke_paths(product.bootstrap.smoke):
        inside: str | None = repository_path(product, path)
        if inside is not None and not github.has_path(product.repository, inside, branch):
            return 'its smoke script ' + inside + ' is not on ' + product.repository + ' ' + branch + ' yet'
    return None


def repository_path(product: project.Project, path: str) -> str | None:
    """A smoke path (relative to the workspace, where the smoke runs) as a path in the product's
    repository, or None when it lies outside the product's checkout (the harness's, say)."""
    if product.at_workspace_root():
        return path
    prefix: str = product.source_dir.rstrip('/') + '/'
    inside: str | None = path[len(prefix):] if path.startswith(prefix) else None
    return inside


def matrix_entries(projects: list[project.Project], github: GitHub) -> tuple[list[dict[str, str]], list[str]]:
    """The ready projects' jobs, one per host, and one line per skipped project."""
    entries: list[dict[str, str]] = []
    skipped: list[str] = []
    product: project.Project
    for product in projects:
        if product.bootstrap is None:
            continue
        reason: str | None = readiness(product, github)
        if reason is not None:
            skipped.append(product.name + ': skipped, because ' + reason)
            continue
        host: Host
        for host in HOSTS:
            entries.append({'project': product.name, 'runner': host.runner, 'platform': host.platform,
                            'checkout': product.source_dir, 'notes_branch': product.bootstrap.notes_branch})
    return entries, skipped


def plan(args: argparse.Namespace, github: GitHub) -> int:
    """Writes the matrix and the summary; the exit status."""
    projects: list[project.Project] = []
    try:
        path: Path
        for path in sorted(args.projects.glob('*.toml')):
            loaded: project.Project = project.load(path)
            if not args.only or loaded.name in args.only:
                projects.append(loaded)
    except (OSError, ValueError) as error:
        print('bootstrap_proof: ' + str(error), file=sys.stderr)
        return 2
    entries: list[dict[str, str]]
    skipped: list[str]
    entries, skipped = matrix_entries(projects, github)
    ready: list[str] = []
    entry: dict[str, str]
    for entry in entries:
        if entry['project'] not in ready:
            ready.append(entry['project'])
    summary: list[str] = ['### Bootstrap proof plan', '']
    name: str
    for name in ready:
        summary.append('- ' + name + ': bootstrapped on ' + str(len(HOSTS)) + ' hosts')
    line: str
    for line in skipped:
        summary.append('- ' + line)
        print('::notice title=Bootstrap proof::' + line, flush=True)
    summary.append('')
    output: str = 'matrix=' + json.dumps({'include': entries}) + '\ncount=' + str(len(entries)) + '\n'
    with args.output.open('a', encoding='utf-8') as handle:
        handle.write(output)
    with args.summary.open('a', encoding='utf-8') as handle:
        handle.write('\n'.join(summary) + '\n')
    print('\n'.join(summary), flush=True)
    return 0


def worktree_pattern(notes_branch: str, action: str) -> re.Pattern[str]:
    pattern: re.Pattern[str] = re.compile(re.escape('notes branch ' + notes_branch + ': ' + action + ' '))
    return pattern


def matching(lines: list[str], pattern: re.Pattern[str]) -> list[re.Match[str]]:
    """The matches of pattern at the start of each line, in order."""
    matches: list[re.Match[str]] = []
    line: str
    for line in lines:
        found: re.Match[str] | None = pattern.match(line)
        if found is not None:
            matches.append(found)
    return matches


def require(lines: list[str], pattern: re.Pattern[str], failures: list[str]) -> None:
    if not matching(lines, pattern):
        failures.append('the log has no line starting ' + repr(pattern.pattern))


def forbid(lines: list[str], pattern: re.Pattern[str], failures: list[str]) -> None:
    if matching(lines, pattern):
        failures.append('the log has a line starting ' + repr(pattern.pattern))


def first_failures(lines: list[str], platform: str, notes_branch: str, locked: bool) -> list[str]:
    """What the run that set an empty workspace up must show."""
    failures: list[str] = []
    harness: re.Pattern[str] = HARNESS_CLONED if locked else HARNESS_UNLOCKED
    pattern: re.Pattern[str]
    for pattern in (PRODUCT_CLONED, NOTES_CLONED, worktree_pattern(notes_branch, 'adding the worktree'),
                    harness, FINISHED):
        require(lines, pattern, failures)
    if platform == 'windows-x64':
        require(lines, TOOLCHAIN_RESTORED, failures)
    if not matching(lines, CACHE_RESTORED) and not matching(lines, CACHE_NOT_PUBLISHED):
        failures.append('the log shows neither a restored cache nor that none is published')
    return failures


def again_failures(lines: list[str], platform: str, notes_branch: str, before: Before, pinned: str | None,
                   moves: list[str]) -> list[str]:
    """What a repeated run must show (pinned: the locked harness commit, None without a lock);
    appends to moves each legitimate change since before."""
    failures: list[str] = []
    pattern: re.Pattern[str]
    for pattern in (PRODUCT_UPDATED, NOTES_UPDATED, worktree_pattern(notes_branch, 'updating'), FINISHED):
        require(lines, pattern, failures)
    for pattern in (PRODUCT_CLONED, NOTES_CLONED, worktree_pattern(notes_branch, 'adding the worktree'),
                    HARNESS_CLONED):
        forbid(lines, pattern, failures)
    if pinned is not None and before.harness != pinned:
        moves.append('harness.lock moved the harness from ' + str(before.harness) + ' to ' + pinned)
    else:
        forbid(lines, HARNESS_FETCHED, failures)
        forbid(lines, TOOLCHAIN_RESTORED, failures)
        if platform == 'windows-x64':
            require(lines, TOOLCHAIN_IN_PLACE, failures)
    if platform.startswith('macos-'):
        require(lines, RUNTIME_IN_PLACE, failures)
    restored: list[re.Match[str]] = matching(lines, CACHE_RESTORED)
    found: re.Match[str]
    for found in restored:
        if found.group(1) == before.cache:
            failures.append('the run restored the cache of ' + found.group(1) + ' again')
        else:
            moves.append('the cache moved from ' + str(before.cache) + ' to ' + found.group(1)
                         + ' (the product was at ' + str(before.product) + ')')
    if not restored and not matching(lines, CACHE_IN_PLACE) and not matching(lines, CACHE_NOT_PUBLISHED):
        failures.append('the log shows no cache outcome: not restored, already restored or unpublished')
    return failures


def git_output(directory: Path, *arguments: str) -> str | None:
    """A git command's trimmed standard output, or None when it fails."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return None
    output: str = completed.stdout.strip()
    return output


def snapshot(workspace: Path, checkout: str) -> Before:
    """The product's and the harness's commits and the restored cache's tag, as they are now."""
    cache: str | None = None
    record: Path = workspace / CACHE_RECORD
    if record.is_file():
        recorded: object = json.loads(record.read_text(encoding='utf-8')).get('tag')
        if type(recorded) is str:
            cache = recorded
    harness: Path = workspace / HARNESS_DIRECTORY
    before: Before = Before(product=git_output(workspace / checkout, 'rev-parse', 'HEAD'),
                            harness=git_output(harness, 'rev-parse', 'HEAD') if harness.is_dir() else None,
                            cache=cache)
    return before


def load_before(path: Path) -> Before:
    """A snapshot file; a field that is not text reads as None."""
    fields: dict[str, object] = json.loads(path.read_text(encoding='utf-8'))
    values: list[str | None] = []
    name: str
    for name in ('product', 'harness', 'cache'):
        value: object = fields.get(name)
        values.append(value if type(value) is str else None)
    before: Before = Before(product=values[0], harness=values[1], cache=values[2])
    return before


def workspace_failures(workspace: Path, checkout: str, notes_branch: str, pinned: lock.Lock | None) -> list[str]:
    """One message per workspace check that fails: the product clone, the pinned harness (with a lock),
    the notes worktree."""
    failures: list[str] = []
    product: Path = workspace / checkout
    if git_output(product, 'rev-parse', '--is-inside-work-tree') != 'true':
        failures.append(str(product) + ' is not a git checkout')
    if pinned is not None:
        harness: Path = workspace / HARNESS_DIRECTORY
        head: str | None = git_output(harness, 'rev-parse', 'HEAD')
        if head != pinned.commit:
            failures.append(str(harness) + ' is at ' + str(head) + ', not the locked ' + pinned.commit)
        if git_output(harness, 'symbolic-ref', '--quiet', 'HEAD') is not None:
            failures.append(str(harness) + ' is on a branch, not detached at the locked commit')
    worktree: Path = workspace / ('notes-' + notes_branch)
    branch: str | None = git_output(worktree, 'rev-parse', '--abbrev-ref', 'HEAD')
    if branch != notes_branch:
        failures.append(str(worktree) + ' is on ' + str(branch) + ', not ' + notes_branch)
    return failures


def check(run: str, args: argparse.Namespace) -> int:
    """The first or again check of one run's log and the workspace; the exit status."""
    try:
        lines: list[str] = args.log.read_text(encoding='utf-8', errors='replace').splitlines()
        lock_path: Path = args.workspace / args.checkout / lock.LOCK_NAME
        pinned: lock.Lock | None = lock.load(lock_path) if lock_path.is_file() else None
        before: Before | None = None if args.before is None else load_before(args.before)
    except (OSError, ValueError, AttributeError, lock.LockError) as error:
        print('bootstrap_proof: ' + str(error), file=sys.stderr)
        return 2
    pinned_commit: str | None = None if pinned is None else pinned.commit
    moves: list[str] = []
    failures: list[str]
    if run == 'first':
        failures = first_failures(lines, args.platform, args.notes_branch, pinned is not None)
    elif before is None:
        print('bootstrap_proof: again needs --before, a snapshot taken before the run', file=sys.stderr)
        return 2
    else:
        failures = again_failures(lines, args.platform, args.notes_branch, before, pinned_commit, moves)
    failures.extend(workspace_failures(args.workspace, args.checkout, args.notes_branch, pinned))
    move: str
    for move in moves:
        print('moved: ' + move, flush=True)
    failure: str
    for failure in failures:
        print('FAILED: ' + failure, flush=True)
    if failures:
        return 1
    harness: str = ('the harness is at ' + pinned_commit if pinned_commit is not None
                    else 'the product has no ' + lock.LOCK_NAME)
    print('ok: the ' + run + ' run on ' + args.platform + ' cloned, updated and restored as it should; '
          + harness + ', and the notes worktree is on ' + args.notes_branch, flush=True)
    return 0


def record(args: argparse.Namespace) -> int:
    """Writes the snapshot file; the exit status."""
    try:
        before: Before = snapshot(args.workspace, args.checkout)
    except (OSError, ValueError, AttributeError) as error:
        print('bootstrap_proof: ' + str(error), file=sys.stderr)
        return 2
    fields: dict[str, str | None] = {'product': before.product, 'harness': before.harness, 'cache': before.cache}
    text: str = json.dumps(fields, indent=2) + '\n'
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding='utf-8')
    print('snapshot: ' + json.dumps(fields), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands: argparse._SubParsersAction = parser.add_subparsers(dest='command', required=True)
    planning: argparse.ArgumentParser = commands.add_parser('matrix')
    planning.add_argument('--projects', required=True, type=Path)
    planning.add_argument('--output', required=True, type=Path)
    planning.add_argument('--summary', required=True, type=Path)
    planning.add_argument('--only', nargs='*', default=[])
    recording: argparse.ArgumentParser = commands.add_parser('snapshot')
    recording.add_argument('--workspace', required=True, type=Path)
    recording.add_argument('--checkout', required=True)
    recording.add_argument('--out', required=True, type=Path)
    run: str
    for run in RUNS:
        checking: argparse.ArgumentParser = commands.add_parser(run)
        checking.add_argument('--log', required=True, type=Path)
        checking.add_argument('--workspace', required=True, type=Path)
        checking.add_argument('--checkout', required=True)
        checking.add_argument('--notes-branch', required=True)
        checking.add_argument('--platform', required=True, choices=PLATFORMS)
        checking.add_argument('--before', type=Path)
    args: argparse.Namespace = parser.parse_args(argv)
    status: int
    if args.command == 'matrix':
        status = plan(args, GitHub())
    elif args.command == 'snapshot':
        status = record(args)
    else:
        status = check(args.command, args)
    return status


if __name__ == '__main__':
    sys.exit(main())
