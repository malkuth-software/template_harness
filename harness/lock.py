#!/usr/bin/env python3
"""A product's harness.lock: the exact template_harness commit it builds with (owner decision 2).

The lock is TOML at the product checkout's root:

    [harness]
    repository = "malkuth-software/template_harness"
    commit = "<40 hexadecimal characters>"

    python3 lock.py show [--lock PATH]
    python3 lock.py checkout DIR [--lock PATH] [--github-base URL]
    python3 lock.py bump [--to REF] [--lock PATH] [--github-base URL]

--lock defaults to harness.lock in the current directory.

show      prints the repository and commit.
checkout  makes DIR that commit, detached: clones the repository into a
          missing or empty DIR, or fetches into an existing clone of it.
          It refuses a DIR with uncommitted changes to tracked files, a
          DIR that is not a git clone, and a commit the repository does
          not have; afterwards it verifies that HEAD is the commit.
bump      rewrites the lock's commit to REF's commit in the repository
          (default: main), read with git ls-remote; a full commit hash is
          taken as given. Only the commit line changes.

CI checks the harness out beside the product (<workspace>/template_harness)
with .github/actions/harness-checkout or an equivalent step; the bootstrap
uses `checkout`. --github-base replaces https://github.com (tests).
"""
from __future__ import annotations
import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tomllib
from typing import Any

GITHUB: str = 'https://github.com'
LOCK_NAME: str = 'harness.lock'
COMMIT_PATTERN: re.Pattern[str] = re.compile(r'[0-9a-f]{40}')
REPOSITORY_PATTERN: re.Pattern[str] = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
# The lock's commit line, which bump rewrites in place.
COMMIT_LINE: re.Pattern[str] = re.compile(r'(?m)^(commit\s*=\s*")([0-9a-f]{40})(")')


class LockError(Exception):
    """An expected refusal or failure; the command prints it and exits 1."""


@dataclass(frozen=True)
class Lock:
    repository: str
    commit: str


def parse(text: str, where: str) -> Lock:
    """Validate a lock's whole text before building the record."""
    try:
        document: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise LockError(where + ' is not TOML: ' + str(error)) from error
    table: Any = document.get('harness')
    if type(table) is not dict:
        raise LockError(where + ' has no [harness] table')
    repository: Any = table.get('repository')
    commit: Any = table.get('commit')
    if type(repository) is not str or REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise LockError(where + ': harness.repository must be owner/name')
    if type(commit) is not str or COMMIT_PATTERN.fullmatch(commit) is None:
        raise LockError(where + ': harness.commit must be 40 lowercase hexadecimal characters')
    if len(COMMIT_LINE.findall(text)) != 1:
        raise LockError(where + ': write the commit as one line, commit = "<hash>"')
    lock: Lock = Lock(repository, commit)
    return lock


def load(path: Path) -> Lock:
    if not path.is_file():
        raise LockError('No ' + str(path))
    lock: Lock = parse(path.read_text(encoding='utf-8'), str(path))
    return lock


def repository_url(github: str, repository: str) -> str:
    url: str = github.rstrip('/') + '/' + repository + '.git'
    return url


def git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ['git', *arguments], capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise LockError(shlex.join(['git', *arguments]) + ' failed: ' + completed.stderr.strip())
    return completed


def has_commit(directory: Path, commit: str) -> bool:
    """Whether the clone holds commit as a commit object (cat-file -t avoids ^{commit}, which MSYS2 can mangle)."""
    completed: subprocess.CompletedProcess[str] = git('-C', str(directory), 'cat-file', '-t', commit, check=False)
    return completed.returncode == 0 and completed.stdout.strip() == 'commit'


def checkout(lock: Lock, directory: Path, github: str) -> None:
    """Makes directory the locked commit, detached; see the module docstring for what it refuses."""
    url: str = repository_url(github, lock.repository)
    if directory.exists() and any(directory.iterdir()):
        if not (directory / '.git').exists():
            raise LockError(str(directory) + ' is not empty and is not a git clone')
        changes: str = git('-C', str(directory), 'status', '--porcelain', '--untracked-files=no').stdout.strip()
        if changes:
            raise LockError(str(directory) + ' has uncommitted changes to tracked files:\n' + changes)
        if not has_commit(directory, lock.commit):
            print('Fetching ' + lock.commit[:12] + ' into ' + str(directory), flush=True)
            fetched: subprocess.CompletedProcess[str] = git('-C', str(directory), 'fetch', '--quiet', url,
                                                            lock.commit, check=False)
            if fetched.returncode != 0:
                git('-C', str(directory), 'fetch', '--quiet', url)
    else:
        print('Cloning ' + url + ' into ' + str(directory), flush=True)
        git('clone', '--quiet', '--no-checkout', url, str(directory))
    if not has_commit(directory, lock.commit):
        raise LockError(lock.repository + ' has no commit ' + lock.commit)
    git('-C', str(directory), 'checkout', '--quiet', '--detach', lock.commit)
    head: str = git('-C', str(directory), 'rev-parse', 'HEAD').stdout.strip()
    if head != lock.commit:
        raise LockError(str(directory) + ' is at ' + head + ', not the locked ' + lock.commit)
    print(str(directory) + ' is ' + lock.repository + ' at ' + lock.commit, flush=True)


def resolve(lock: Lock, reference: str, github: str) -> str:
    """REF's commit in the lock's repository: a full hash as given, else a branch or tag from ls-remote."""
    if COMMIT_PATTERN.fullmatch(reference) is not None:
        return reference
    url: str = repository_url(github, lock.repository)
    # Every ref: a pattern would leave out a tag's peeled commit (refs/tags/<tag>^{}).
    listing: str = git('ls-remote', url).stdout
    found: dict[str, str] = {}
    line: str
    for line in listing.splitlines():
        sha: str
        name: str
        sha, _, name = line.partition('\t')
        found[name] = sha
    # A branch first, then a tag's peeled commit (^{}), then a lightweight tag.
    candidate: str
    for candidate in ('refs/heads/' + reference, 'refs/tags/' + reference + '^{}', 'refs/tags/' + reference):
        if candidate in found:
            return found[candidate]
    raise LockError(lock.repository + ' has no branch or tag ' + reference)


def bump(path: Path, reference: str, github: str) -> str:
    """Rewrites the lock's commit line to reference's commit; returns the commit."""
    lock: Lock = load(path)
    commit: str = resolve(lock, reference, github)
    text: str = path.read_text(encoding='utf-8')
    replacement: str = COMMIT_LINE.sub(r'\g<1>' + commit + r'\g<3>', text)
    parse(replacement, str(path))
    path.write_text(replacement, encoding='utf-8')
    print(str(path) + ': ' + lock.commit[:12] + ' -> ' + commit[:12] + ' (' + reference + ')', flush=True)
    return commit


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--lock', type=Path, default=Path(LOCK_NAME))
    parser.add_argument('--github-base', default=GITHUB)
    commands: argparse._SubParsersAction = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('show', help='print the pin')
    checkout_parser: argparse.ArgumentParser = commands.add_parser('checkout', help='check the pinned commit out')
    checkout_parser.add_argument('directory', type=Path)
    bump_parser: argparse.ArgumentParser = commands.add_parser('bump', help="move the pin to a ref's commit")
    bump_parser.add_argument('--to', default='main')
    args: argparse.Namespace = parser.parse_args(argv)
    try:
        if args.command == 'show':
            lock: Lock = load(args.lock)
            print(lock.repository + ' ' + lock.commit, flush=True)
        elif args.command == 'checkout':
            checkout(load(args.lock), args.directory.resolve(), args.github_base)
        else:
            bump(args.lock, args.to, args.github_base)
    except LockError as error:
        print('lock: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
