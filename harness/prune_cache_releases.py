#!/usr/bin/env python3
"""Report, and only with --apply delete, compiler-cache pre-releases no consumer pins.

harness/publish_cache.py publishes one `build-<sha>` pre-release per tested main
revision. Downstream consumers pin exact revisions (for GUI.Forms: PlaySuite,
File Manager), so the kept set is enumerable: every revision listed in the
project's ci.pinned_cache_revisions file (GUI.Forms:
packaging/pinned_cache_revisions.json) plus the N most recent cache releases.
Everything else is listed as prunable. Without --apply nothing is deleted; the
weekly workflow job runs this dry report so the list stays visible. Application
releases (`v*` tags) are never considered.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import project

ROOT: Path = project.current().source_root()
# None when the project names no pinned revisions: --pinned is then required.
PINNED_PATH: Path | None = (None if project.current().pinned_cache_revisions is None
                            else ROOT / project.current().pinned_cache_revisions)
TAG_PATTERN: re.Pattern[str] = re.compile(r"build-([0-9a-f]{40})")
DEFAULT_KEEP: int = 5
MINIMUM_PIN_LENGTH: int = 7


def load_pinned(path: Path | None = PINNED_PATH) -> list[str]:
    if path is None:
        raise project.current().missing('ci.pinned_cache_revisions')
    data = json.loads(path.read_text(encoding="utf-8"))
    pins: list[str] = []
    for entry in data["pinned"]:
        sha = str(entry["sha"]).lower()
        if not re.fullmatch(r"[0-9a-f]{%d,40}" % MINIMUM_PIN_LENGTH, sha):
            raise ValueError(f"pinned revision {sha!r} must be {MINIMUM_PIN_LENGTH}-40 hex characters")
        pins.append(sha)
    return pins


def is_pinned(sha: str, pins: list[str]) -> bool:
    return any(sha.startswith(pin) for pin in pins)


def list_releases(repository: str) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    page = 1
    while True:
        output = subprocess.check_output(
            ["gh", "api", f"repos/{repository}/releases?per_page=100&page={page}"], text=True)
        batch = json.loads(output)
        if not batch:
            return releases
        releases.extend(batch)
        page += 1


def select_prunable(releases: list[dict[str, Any]], pins: list[str], keep: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (kept, prunable) cache releases, each annotated with a 'reason'."""
    cache: list[dict[str, Any]] = []
    for release in releases:
        match = TAG_PATTERN.fullmatch(str(release.get("tag_name", "")))
        if match is None or not release.get("prerelease", False):
            continue
        cache.append({**release, "sha": match.group(1)})
    cache.sort(key=lambda release: str(release.get("created_at", "")), reverse=True)
    kept: list[dict[str, Any]] = []
    prunable: list[dict[str, Any]] = []
    for index, release in enumerate(cache):
        if is_pinned(release["sha"], pins):
            kept.append({**release, "reason": "pinned"})
        elif index < keep:
            kept.append({**release, "reason": f"recent ({index + 1} of {keep})"})
        else:
            prunable.append({**release, "reason": "unpinned and older than the kept set"})
    return kept, prunable


def delete_release(repository: str, tag: str) -> None:
    subprocess.run(["gh", "release", "delete", tag, "--repo", repository, "--yes", "--cleanup-tag"], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name (default $GITHUB_REPOSITORY or the project's repository)")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="most recent cache releases kept besides the pinned ones")
    parser.add_argument("--pinned", type=Path, default=PINNED_PATH)
    parser.add_argument("--apply", action="store_true", help="actually delete the prunable releases and their tags")
    args = parser.parse_args(argv)
    if args.keep < 0:
        parser.error("--keep must not be negative")
    repository = args.repo
    if not repository:
        repository = project.current().repository
    pins = load_pinned(args.pinned)
    kept, prunable = select_prunable(list_releases(repository), pins, args.keep)
    print(f"Compiler-cache pre-releases in {repository}: {len(kept) + len(prunable)} "
          f"(keep {args.keep} recent + {len(pins)} pinned revision(s))")
    for release in kept:
        print(f"  keep   {release['tag_name']}  {release.get('created_at', '')}  {release['reason']}")
    for release in prunable:
        print(f"  prune  {release['tag_name']}  {release.get('created_at', '')}  {release['reason']}")
    if not prunable:
        print("Nothing to prune.")
        return 0
    if not args.apply:
        print(f"Dry run: {len(prunable)} release(s) would be deleted. Re-run with --apply to delete them.")
        return 0
    for release in prunable:
        print(f"deleting {release['tag_name']}", flush=True)
        delete_release(repository, release["tag_name"])
    print(f"Deleted {len(prunable)} cache release(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
