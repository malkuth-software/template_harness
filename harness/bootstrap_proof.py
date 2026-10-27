#!/usr/bin/env python3
"""Check one bootstrap run's output and workspace for the bootstrap proof (.github/workflows/bootstrap.yml).

    python -B harness/bootstrap_proof.py snapshot --workspace DIR --checkout NAME --out FILE
    python -B harness/bootstrap_proof.py {first|again} --log FILE --workspace DIR
        --checkout NAME --notes-branch BRANCH --platform P [--before FILE]

The harness's own CI tool; products do not run it. NAME is the product's
checkout under DIR (its source_dir); the log is the bootstrap's output.

snapshot: records, before a repeated run, the commits of the product and of
DIR/template_harness and the tag of the restored compiler cache
(.ci/local-cache.json), so that the check can tell an update from a repeat.

first: the run that set an empty workspace up. It cloned the product, the
notes and the pinned harness, added the notes worktree, and restored the
Windows toolchain on Windows and the compiler cache where one is published.

again (with --before): a later run, which must be a no-op update. It updated
the product, the notes and the notes worktree and cloned nothing. It may
restore a cache only when it is not the one already restored: the product's
main moved on, and a newer build of it has a published cache. It may fetch a
harness commit and restore the Windows toolchain only when harness.lock moved
the harness. Otherwise the toolchain is "already at" its directory, the cache
"already restored" (or still not published), and on macOS the runtime the
workflow built matches its manifest. Each such move is printed as "moved:".

Both: the run finished; DIR/template_harness is detached at the commit the
product's harness.lock pins; DIR/notes-BRANCH is on BRANCH.

Only the bootstrap's own lines are read: each pattern is anchored at the
start of a line as bootstrap.py, lock.py and restore_local.py print it, so
the smoke build's output (a dependency's "Fetching ...") cannot match.

Each check prints "ok: ..." or "FAILED: ..."; any failure exits 1, and a
missing file or an unreadable lock or snapshot exits 2.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

import lock

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


@dataclass(frozen=True)
class Before:
    """The workspace before a repeated run (None where there was nothing to record)."""
    product: str | None
    harness: str | None
    cache: str | None


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


def first_failures(lines: list[str], platform: str, notes_branch: str) -> list[str]:
    """What the run that set an empty workspace up must show."""
    failures: list[str] = []
    pattern: re.Pattern[str]
    for pattern in (PRODUCT_CLONED, NOTES_CLONED, worktree_pattern(notes_branch, 'adding the worktree'),
                    HARNESS_CLONED, FINISHED):
        require(lines, pattern, failures)
    if platform == 'windows-x64':
        require(lines, TOOLCHAIN_RESTORED, failures)
    if not matching(lines, CACHE_RESTORED) and not matching(lines, CACHE_NOT_PUBLISHED):
        failures.append('the log shows neither a restored cache nor that none is published')
    return failures


def again_failures(lines: list[str], platform: str, notes_branch: str, before: Before, pinned: str,
                   moves: list[str]) -> list[str]:
    """What a repeated run must show; appends to moves each legitimate change since before."""
    failures: list[str] = []
    pattern: re.Pattern[str]
    for pattern in (PRODUCT_UPDATED, NOTES_UPDATED, worktree_pattern(notes_branch, 'updating'), FINISHED):
        require(lines, pattern, failures)
    for pattern in (PRODUCT_CLONED, NOTES_CLONED, worktree_pattern(notes_branch, 'adding the worktree'),
                    HARNESS_CLONED):
        forbid(lines, pattern, failures)
    if before.harness != pinned:
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
    before: Before = Before(product=git_output(workspace / checkout, 'rev-parse', 'HEAD'),
                            harness=git_output(workspace / HARNESS_DIRECTORY, 'rev-parse', 'HEAD'), cache=cache)
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


def workspace_failures(workspace: Path, checkout: str, notes_branch: str, pinned: lock.Lock) -> list[str]:
    """One message per workspace check that fails: the product clone, the pinned harness, the notes worktree."""
    failures: list[str] = []
    product: Path = workspace / checkout
    if git_output(product, 'rev-parse', '--is-inside-work-tree') != 'true':
        failures.append(str(product) + ' is not a git checkout')
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
        pinned: lock.Lock = lock.load(args.workspace / args.checkout / lock.LOCK_NAME)
        before: Before | None = None if args.before is None else load_before(args.before)
    except (OSError, ValueError, AttributeError, lock.LockError) as error:
        print('bootstrap_proof: ' + str(error), file=sys.stderr)
        return 2
    moves: list[str] = []
    failures: list[str]
    if run == 'first':
        failures = first_failures(lines, args.platform, args.notes_branch)
    elif before is None:
        print('bootstrap_proof: again needs --before, a snapshot taken before the run', file=sys.stderr)
        return 2
    else:
        failures = again_failures(lines, args.platform, args.notes_branch, before, pinned.commit, moves)
    failures.extend(workspace_failures(args.workspace, args.checkout, args.notes_branch, pinned))
    move: str
    for move in moves:
        print('moved: ' + move, flush=True)
    failure: str
    for failure in failures:
        print('FAILED: ' + failure, flush=True)
    if failures:
        return 1
    print('ok: the ' + run + ' run on ' + args.platform + ' cloned, updated and restored as it should; '
          'the harness is at ' + pinned.commit + ' and the notes worktree on ' + args.notes_branch, flush=True)
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
    if args.command == 'snapshot':
        status = record(args)
    else:
        status = check(args.command, args)
    return status


if __name__ == '__main__':
    sys.exit(main())
