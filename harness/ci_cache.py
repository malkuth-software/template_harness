#!/usr/bin/env python3
"""Keep the Actions cache to what the next run restores (notes:planning/BUILD_SIZE_PLAN_2026-10-23.md).

`evict` runs before a cache save on main. It removes every ccache entry
this job neither created nor hit, so the saved cache is the current tree's
working set instead of every generation up to CCACHE_MAXSIZE. --since is
the job's start in seconds since the epoch, recorded before the cache was
restored: restored entries keep their earlier timestamps (cache archives
keep file times), and ccache refreshes an entry's timestamp on every hit,
so after the build an entry older than the job start was not used by it.
Entries up to EVICTION_MARGIN seconds older than the start are kept too.

Eviction is skipped, with the reason printed, when the job compiled
nothing through ccache (nothing marks the working set) or when fewer
entries are newer than the job start than the job had compilations (the
installed ccache did not refresh hit entries, so eviction would drop
current objects). Either way the cache is left as it was. A failing ccache
command raises, leaving the cache as ccache left it.

`prune` runs once per push to main, after every saving job. It lists the
caches of --ref, groups each key into its family (the key up to its last
hash segment: forty or more hexadecimal characters), and keeps the newest
entry of each family by creation time, which is the one every restore-key
lookup picks. The older entries are superseded; with --apply they are
deleted, otherwise only listed. Keys with no hash segment belong to no
family and are never deleted.

The family ends before the last hash, not the first, so that an earlier
hash (the compiler identity, the third-party scripts) stays part of it: a
runner image rolling out a new compiler leaves the other compiler's newest
entry for jobs still on the old image. Such an entry is superseded by no
save and expires after GitHub's seven days without a restore.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

HASH_SEGMENT: re.Pattern[str] = re.compile(r'-[0-9a-f]{40,}(?=-|$)')
# Files in a ccache directory that are not cache entries.
BOOKKEEPING_NAMES: frozenset[str] = frozenset(('stats', 'CACHEDIR.TAG', 'ccache.conf'))
BOOKKEEPING_DIRECTORY: str = 'tmp'
CACHES_PER_PAGE: int = 100
# Seconds of entries older than the job start that eviction keeps (see evict).
EVICTION_MARGIN: int = 300


# --- evict -----------------------------------------------------------------

class CcacheState:
    """What `ccache --print-stats` reports, by counter name."""

    def __init__(self, counters: dict[str, int]) -> None:
        self.counters: dict[str, int] = counters

    def compilations(self) -> int:
        """Cacheable compilations since the last --zero-stats: hits of either kind, and misses."""
        direct: int = self.counters.get('direct_cache_hit', 0)
        preprocessed: int = self.counters.get('preprocessed_cache_hit', 0)
        missed: int = self.counters.get('cache_miss', 0)
        return direct + preprocessed + missed

    def describe(self) -> str:
        size: float = self.counters.get('cache_size_kibibyte', 0) / 1024.0
        files: int = self.counters.get('files_in_cache', 0)
        return '%.1f MiB in %d files' % (size, files)


def parse_stats(text: str) -> CcacheState:
    counters: dict[str, int] = {}
    line: str
    for line in text.splitlines():
        fields: list[str] = line.split('\t')
        if len(fields) == 2 and fields[1].isdigit():
            counters[fields[0]] = int(fields[1])
    return CcacheState(counters)


def read_stats() -> CcacheState:
    return parse_stats(subprocess.check_output(['ccache', '--print-stats'], text=True))


def cache_directory() -> Path:
    configured: str = os.environ.get('CCACHE_DIR', '')
    if configured:
        return Path(configured)
    return Path(subprocess.check_output(['ccache', '-k', 'cache_dir'], text=True).strip())


def entries_newer_than(directory: Path, since: float) -> int:
    """Cache entry files (results and manifests) modified at or after since."""
    fresh: int = 0
    folder: str
    folders: list[str]
    names: list[str]
    for folder, folders, names in os.walk(directory):
        if Path(folder) == directory and BOOKKEEPING_DIRECTORY in folders:
            folders.remove(BOOKKEEPING_DIRECTORY)
        name: str
        for name in names:
            if name in BOOKKEEPING_NAMES or name.endswith('.lock'):
                continue
            if os.stat(os.path.join(folder, name)).st_mtime >= since:
                fresh += 1
    return fresh


def eviction_refusal(state: CcacheState, fresh: int) -> str:
    """Why eviction must not run, or '' when it may."""
    compilations: int = state.compilations()
    if compilations == 0:
        return 'this job compiled nothing through ccache, so no entry marks the working set'
    if fresh < compilations:
        return ('only ' + str(fresh) + ' entries are newer than the job start for ' + str(compilations)
                + ' compilations: this ccache does not refresh entries on a hit')
    return ''


def evict(since: int, now: float) -> int:
    directory: Path = cache_directory()
    before: CcacheState = read_stats()
    fresh: int = entries_newer_than(directory, float(since))
    refusal: str = eviction_refusal(before, fresh)
    print('ccache before eviction: ' + before.describe() + '; ' + str(before.compilations())
          + ' compilations this job, ' + str(fresh) + ' entries newer than its start', flush=True)
    if refusal:
        print('Eviction skipped: ' + refusal + '. The cache is saved as it is.', flush=True)
        return 0
    # ccache stores whole-second timestamps on a hit and compares them with
    # the age at a granularity this cannot see, and an age of only the job's
    # length removed entries hit in the job's first second (half of four local
    # trials). EVICTION_MARGIN keeps entries used shortly before the job
    # started too, which costs a few extra entries, never a current one.
    age: int = int(now - since) + EVICTION_MARGIN
    subprocess.run(['ccache', '--evict-older-than', str(age) + 's'], check=True)
    after: CcacheState = read_stats()
    print('ccache after eviction: ' + after.describe(), flush=True)
    return 0


# --- prune -----------------------------------------------------------------

class GitHubCaches:
    """The repository's Actions caches through `gh api`."""

    def __init__(self, repository: str) -> None:
        self.repository: str = repository

    def list(self, ref: str) -> list[dict[str, Any]]:
        caches: list[dict[str, Any]] = []
        page: int = 1
        while True:
            endpoint: str = ('repos/' + self.repository + '/actions/caches?per_page=' + str(CACHES_PER_PAGE)
                             + '&page=' + str(page) + '&ref=' + ref)
            batch: list[dict[str, Any]] = json.loads(
                subprocess.check_output(['gh', 'api', endpoint], text=True))['actions_caches']
            caches.extend(batch)
            if len(batch) < CACHES_PER_PAGE:
                return caches
            page += 1

    def delete(self, cache_id: int) -> None:
        subprocess.run(['gh', 'api', '-X', 'DELETE', 'repos/' + self.repository + '/actions/caches/' + str(cache_id)],
                       check=True, stdout=subprocess.DEVNULL)


def family(key: str) -> str:
    """The key up to its last hash segment, or '' for a key with none."""
    start: int = -1
    match: re.Match[str]
    for match in HASH_SEGMENT.finditer(key):
        start = match.start()
    if start < 0:
        return ''
    return key[:start]


def newer(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Whether first was created after second; equal times fall to the larger id."""
    first_created: str = str(first['created_at'])
    second_created: str = str(second['created_at'])
    if first_created != second_created:
        return first_created > second_created
    return int(first['id']) > int(second['id'])


def superseded(caches: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """(newest entry of each family, every other entry of a family), in listing order."""
    newest: dict[str, dict[str, Any]] = {}
    cache: dict[str, Any]
    for cache in caches:
        name: str = family(str(cache['key']))
        if not name:
            continue
        if name not in newest or newer(cache, newest[name]):
            newest[name] = cache
    older: list[dict[str, Any]] = []
    for cache in caches:
        name = family(str(cache['key']))
        if name and newest[name]['id'] != cache['id']:
            older.append(cache)
    return newest, older


def gigabytes(size: int) -> str:
    return '%.2f GB' % (size / 1e9)


def prune(caches_api: GitHubCaches, ref: str, apply: bool) -> int:
    caches: list[dict[str, Any]] = caches_api.list(ref)
    newest: dict[str, dict[str, Any]]
    older: list[dict[str, Any]]
    newest, older = superseded(caches)
    total: int = 0
    cache: dict[str, Any]
    for cache in caches:
        total += int(cache['size_in_bytes'])
    print('Caches of ' + ref + ': ' + str(len(caches)) + ', ' + gigabytes(total) + ', in ' + str(len(newest))
          + ' families', flush=True)
    name: str
    for name in sorted(newest):
        print('  keep   ' + newest[name]['key'] + '  ' + gigabytes(int(newest[name]['size_in_bytes'])), flush=True)
    freed: int = 0
    for cache in older:
        freed += int(cache['size_in_bytes'])
        print('  ' + ('delete' if apply else 'older ') + ' ' + cache['key'] + '  '
              + gigabytes(int(cache['size_in_bytes'])), flush=True)
        if apply:
            caches_api.delete(int(cache['id']))
    verb: str = 'Deleted' if apply else 'Superseded (dry run, nothing deleted):'
    print(verb + ' ' + str(len(older)) + ' entries, ' + gigabytes(freed), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands: Any = parser.add_subparsers(dest='command', required=True)
    evict_parser: argparse.ArgumentParser = commands.add_parser('evict', help='drop ccache entries this job did not use')
    evict_parser.add_argument('--since', type=int, required=True, help='the job start, seconds since the epoch')
    prune_parser: argparse.ArgumentParser = commands.add_parser('prune', help='delete superseded cache generations')
    prune_parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY'), help='owner/name')
    prune_parser.add_argument('--ref', default='refs/heads/main')
    prune_parser.add_argument('--apply', action='store_true', help='delete; without it the entries are only listed')
    args: argparse.Namespace = parser.parse_args(argv)
    if args.command == 'evict':
        return evict(args.since, time.time())
    if not args.repo:
        parser.error('prune needs --repo or GITHUB_REPOSITORY')
    return prune(GitHubCaches(args.repo), args.ref, args.apply)


if __name__ == '__main__':
    sys.exit(main())
