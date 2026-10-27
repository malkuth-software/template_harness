#!/usr/bin/env python3
"""Check one bootstrap run's output and workspace for the bootstrap proof (.github/workflows/bootstrap.yml).

    python -B harness/bootstrap_proof.py {first|again} --log FILE --workspace DIR
        --checkout NAME --notes-branch BRANCH --platform P

The harness's own CI tool; products do not run it. FILE is the bootstrap's
standard output; NAME is the product's checkout under DIR (its source_dir).

first: the run that set an empty workspace up. It cloned the product, the
notes and the pinned harness, added the notes worktree, and restored the
Windows toolchain on Windows and the compiler cache where one is published.

again: a later run, which must be a no-op update. It updated the product,
the notes and the notes worktree; it cloned nothing, fetched no harness
commit and restored nothing again: the Windows toolchain is "already at"
its directory, the cache "already restored" (or still not published), and
on macOS the runtime the workflow built matches its manifest.

Both: the run finished; DIR/template_harness is detached at the commit the
product's harness.lock pins; DIR/notes-BRANCH is on BRANCH.

Each check prints "ok: ..." or "FAILED: ..."; any failure exits 1, and a
missing file or an unreadable lock exits 2.
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

import lock

RUNS: tuple[str, ...] = ('first', 'again')
PLATFORMS: tuple[str, ...] = ('windows-x64', 'macos-arm64', 'linux-x64', 'linux-arm64')
HARNESS_DIRECTORY: str = 'template_harness'
FINISHED: str = 'Bootstrap finished'
# The cache restore prints one of these on any run that reaches it.
CACHE_RESTORED: str = 'Restored the cache of '
CACHE_ALREADY_RESTORED: str = ' is already restored'
CACHE_NOT_PUBLISHED: str = 'No published '


@dataclass(frozen=True)
class Expectations:
    """Fragments the log must contain, fragments it must not, and fragments of which it needs one."""
    required: tuple[str, ...]
    forbidden: tuple[str, ...]
    one_of: tuple[tuple[str, ...], ...]


def expectations(run: str, platform: str, notes_branch: str) -> Expectations:
    """What the log of run shows on platform: bootstrap.py, lock.py and restore_local.py print these."""
    notes_worktree: str = 'notes branch ' + notes_branch + ': '
    required: list[str] = [FINISHED]
    forbidden: list[str] = []
    one_of: list[tuple[str, ...]] = []
    if run == 'first':
        required.extend(['the product: cloning ', 'the notes: cloning ', notes_worktree + 'adding the worktree ',
                         'Cloning '])
        one_of.append((CACHE_RESTORED, CACHE_NOT_PUBLISHED))
        if platform == 'windows-x64':
            required.append('Restored the Windows toolchain ')
    else:
        required.extend(['the product: updating ', 'the notes: updating ', notes_worktree + 'updating '])
        forbidden.extend([': cloning ', 'Cloning ', 'Fetching ', 'adding the worktree ', 'Restored the '])
        one_of.append((CACHE_ALREADY_RESTORED, CACHE_NOT_PUBLISHED))
        if platform == 'windows-x64':
            required.append(' is already at ')
        if platform.startswith('macos-'):
            required.append(' matches its manifest')
    result: Expectations = Expectations(tuple(required), tuple(forbidden), tuple(one_of))
    return result


def contains(lines: list[str], fragment: str) -> bool:
    line: str
    for line in lines:
        if fragment in line:
            return True
    return False


def log_failures(lines: list[str], expected: Expectations) -> list[str]:
    """One message per unmet expectation, in the order of expected's fields."""
    failures: list[str] = []
    fragment: str
    for fragment in expected.required:
        if not contains(lines, fragment):
            failures.append('the log has no line with ' + repr(fragment))
    for fragment in expected.forbidden:
        if contains(lines, fragment):
            failures.append('the log has a line with ' + repr(fragment))
    choices: tuple[str, ...]
    for choices in expected.one_of:
        found: bool = False
        for fragment in choices:
            if contains(lines, fragment):
                found = True
        if not found:
            failures.append('the log has no line with any of ' + repr(choices))
    return failures


def git_output(directory: Path, *arguments: str) -> str | None:
    """A git command's trimmed standard output, or None when it fails."""
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', '-C', str(directory), *arguments], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        return None
    output: str = completed.stdout.strip()
    return output


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


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('run', choices=RUNS)
    parser.add_argument('--log', required=True, type=Path)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--checkout', required=True)
    parser.add_argument('--notes-branch', required=True)
    parser.add_argument('--platform', required=True, choices=PLATFORMS)
    args: argparse.Namespace = parser.parse_args(argv)
    try:
        lines: list[str] = args.log.read_text(encoding='utf-8', errors='replace').splitlines()
        pinned: lock.Lock = lock.load(args.workspace / args.checkout / lock.LOCK_NAME)
    except (OSError, lock.LockError) as error:
        print('bootstrap_proof: ' + str(error), file=sys.stderr)
        return 2
    expected: Expectations = expectations(args.run, args.platform, args.notes_branch)
    failures: list[str] = log_failures(lines, expected)
    failures.extend(workspace_failures(args.workspace, args.checkout, args.notes_branch, pinned))
    failure: str
    for failure in failures:
        print('FAILED: ' + failure, flush=True)
    if failures:
        return 1
    print('ok: the ' + args.run + ' run on ' + args.platform + ' shows ' + str(len(expected.required))
          + ' required lines, none of ' + str(len(expected.forbidden)) + ' forbidden ones, the cache outcome, '
          + 'the harness at ' + pinned.commit + ' and the notes worktree on ' + args.notes_branch, flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
