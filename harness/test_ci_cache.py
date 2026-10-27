#!/usr/bin/env python3
"""Tests for tools/ci_cache.py: ccache eviction guards and cache-generation pruning."""
from __future__ import annotations
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_cache  # noqa: E402

HASH_64: str = '0f63eab35c8a95581a544d41db14cb613b97801450311bf0cbd5945390a23617'
HASH_40: str = 'abb7878417130ce01f0bc9c93a4096f4c5d58335'


def cache(identity: int, key: str, created: str, size: int = 1000) -> dict[str, Any]:
    return {'id': identity, 'key': key, 'created_at': created, 'size_in_bytes': size, 'ref': 'refs/heads/main'}


class FakeCaches:
    def __init__(self, caches: list[dict[str, Any]]) -> None:
        self.caches: list[dict[str, Any]] = caches
        self.deleted: list[int] = []

    def list(self, ref: str) -> list[dict[str, Any]]:
        return self.caches

    def delete(self, cache_id: int) -> None:
        self.deleted.append(cache_id)


class FamilyTests(unittest.TestCase):
    def test_the_repository_keys(self) -> None:
        self.assertEqual(ci_cache.family('gui-objects-v2-windows-x64-' + HASH_64 + '-' + HASH_40),
                         'gui-objects-v2-windows-x64-' + HASH_64)
        self.assertEqual(ci_cache.family('gui-programs-objects-v1-linux-arm64-' + HASH_64 + '-' + HASH_64),
                         'gui-programs-objects-v1-linux-arm64-' + HASH_64)
        self.assertEqual(ci_cache.family('gui-sanitize-v1-lines-' + HASH_64 + '-' + HASH_40),
                         'gui-sanitize-v1-lines-' + HASH_64)
        self.assertEqual(ci_cache.family('linux-debs-v1-toolkit-X64-' + HASH_64 + '-ubuntu24-20261012.334.1'),
                         'linux-debs-v1-toolkit-X64')
        self.assertEqual(ci_cache.family('gui-runtimes-v1-macos-arm64-' + HASH_64 + '-' + HASH_64),
                         'gui-runtimes-v1-macos-arm64-' + HASH_64)

    def test_a_hash_ending_the_key(self) -> None:
        self.assertEqual(ci_cache.family('gui-deps-v3-macos-arm64-' + HASH_64), 'gui-deps-v3-macos-arm64')

    def test_another_compiler_is_another_family(self) -> None:
        other: str = 'a' + HASH_64[1:]
        self.assertNotEqual(ci_cache.family('gui-objects-v2-linux-x64-' + HASH_64 + '-' + HASH_40),
                            ci_cache.family('gui-objects-v2-linux-x64-' + other + '-' + HASH_40))

    def test_no_hash_is_no_family(self) -> None:
        self.assertEqual(ci_cache.family('gui-objects-v2-windows-x64-abc123'), '')
        self.assertEqual(ci_cache.family('setup-python-3.12'), '')


class SupersededTests(unittest.TestCase):
    def test_newest_of_each_family_is_kept(self) -> None:
        caches: list[dict[str, Any]] = [
            cache(1, 'gui-objects-v2-linux-x64-' + HASH_64 + '-a' + HASH_40[1:], '2026-10-23T18:00:00Z'),
            cache(2, 'gui-objects-v2-linux-x64-' + HASH_64 + '-b' + HASH_40[1:], '2026-10-23T20:00:00Z'),
            cache(3, 'gui-objects-v2-linux-x64-' + HASH_64 + '-c' + HASH_40[1:], '2026-10-23T19:00:00Z'),
            cache(4, 'gui-objects-v2-linux-arm64-' + HASH_64 + '-' + HASH_40, '2026-10-22T08:00:00Z'),
            cache(5, 'unrelated-key', '2026-10-01T00:00:00Z'),
        ]
        newest: dict[str, dict[str, Any]]
        older: list[dict[str, Any]]
        newest, older = ci_cache.superseded(caches)
        self.assertEqual(newest['gui-objects-v2-linux-x64-' + HASH_64]['id'], 2)
        self.assertEqual(newest['gui-objects-v2-linux-arm64-' + HASH_64]['id'], 4)
        older_ids: list[int] = []
        entry: dict[str, Any]
        for entry in older:
            older_ids.append(entry['id'])
        self.assertEqual(older_ids, [1, 3])

    def test_equal_times_keep_the_larger_id(self) -> None:
        caches: list[dict[str, Any]] = [
            cache(8, 'gui-sanitize-v1-lines-' + HASH_64 + '-' + HASH_40, '2026-10-23T20:40:00Z'),
            cache(7, 'gui-sanitize-v1-lines-' + HASH_64 + '-a' + HASH_40[1:], '2026-10-23T20:40:00Z'),
        ]
        older: list[dict[str, Any]] = ci_cache.superseded(caches)[1]
        self.assertEqual(len(older), 1)
        self.assertEqual(older[0]['id'], 7)


class PruneTests(unittest.TestCase):
    def caches(self) -> list[dict[str, Any]]:
        return [
            cache(1, 'gui-deps-v3-windows-x64-' + HASH_64 + '-a' + HASH_64[1:], '2026-10-22T10:00:00Z'),
            cache(2, 'gui-deps-v3-windows-x64-' + HASH_64 + '-b' + HASH_64[1:], '2026-10-23T10:00:00Z'),
        ]

    def test_a_dry_run_deletes_nothing(self) -> None:
        fake: FakeCaches = FakeCaches(self.caches())
        self.assertEqual(ci_cache.prune(fake, 'refs/heads/main', False), 0)
        self.assertEqual(fake.deleted, [])

    def test_apply_deletes_only_the_superseded(self) -> None:
        fake: FakeCaches = FakeCaches(self.caches())
        self.assertEqual(ci_cache.prune(fake, 'refs/heads/main', True), 0)
        self.assertEqual(fake.deleted, [1])


class EvictionGuardTests(unittest.TestCase):
    def test_stats_are_read_by_name(self) -> None:
        state: ci_cache.CcacheState = ci_cache.parse_stats(
            'cache_miss\t3\ndirect_cache_hit\t10\npreprocessed_cache_hit\t2\nfiles_in_cache\t40\nstats_updated_timestamp\t17\n')
        self.assertEqual(state.compilations(), 15)

    def test_nothing_compiled_refuses(self) -> None:
        state: ci_cache.CcacheState = ci_cache.parse_stats('cache_miss\t0\ndirect_cache_hit\t0\n')
        self.assertIn('compiled nothing', ci_cache.eviction_refusal(state, 100))

    def test_hits_not_refreshed_refuses(self) -> None:
        state: ci_cache.CcacheState = ci_cache.parse_stats('cache_miss\t2\ndirect_cache_hit\t50\n')
        self.assertIn('does not refresh', ci_cache.eviction_refusal(state, 4))

    def test_refreshed_entries_allow_eviction(self) -> None:
        state: ci_cache.CcacheState = ci_cache.parse_stats('cache_miss\t2\ndirect_cache_hit\t50\n')
        self.assertEqual(ci_cache.eviction_refusal(state, 104), '')

    def test_only_entries_newer_than_the_start_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory: Path = Path(name)
            start: float = 1_800_000_000.0
            paths: dict[str, float] = {
                'a/1/old-entry': start - 3600.0,
                'a/2/new-entry': start + 10.0,
                'b/3/new-manifest': start,
                'a/stats': start + 10.0,
                'a/CACHEDIR.TAG': start + 10.0,
                'tmp/new-temporary': start + 10.0,
                'b/4/entry.lock': start + 10.0,
            }
            relative: str
            for relative in paths:
                path: Path = directory / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'x')
                os.utime(path, (paths[relative], paths[relative]))
            self.assertEqual(ci_cache.entries_newer_than(directory, start), 2)


if __name__ == '__main__':
    unittest.main()
