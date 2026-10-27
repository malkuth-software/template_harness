"""Pinned cache releases and the N most recent survive; nothing is deleted without --apply."""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import harness_testing
import prune_cache_releases as prune

PINNED_SHA: str = "5bb7a8ba74ccb358f11d1b9056831e0130bdd8d0"


def release(sha: str, day: int, prerelease: bool = True, tag: str | None = None) -> dict[str, Any]:
    return {"tag_name": tag or "build-" + sha, "prerelease": prerelease, "draft": False,
            "created_at": f"2026-09-{day:02d}T00:00:00Z"}


def sha(index: int) -> str:
    return f"{index:040x}"


class SelectionTests(unittest.TestCase):
    def test_pinned_and_recent_are_kept_and_the_rest_pruned(self) -> None:
        releases = [release(sha(index), index) for index in range(1, 9)] + [release(PINNED_SHA, 0)]
        kept, prunable = prune.select_prunable(releases, [PINNED_SHA], keep=3)
        self.assertEqual([item["tag_name"] for item in kept],
                         ["build-" + sha(8), "build-" + sha(7), "build-" + sha(6), "build-" + PINNED_SHA])
        self.assertEqual({item["reason"] for item in kept if item["sha"] != PINNED_SHA}, {"recent (1 of 3)", "recent (2 of 3)", "recent (3 of 3)"})
        self.assertEqual(kept[-1]["reason"], "pinned")
        self.assertEqual([item["tag_name"] for item in prunable],
                         ["build-" + sha(index) for index in (5, 4, 3, 2, 1)])

    def test_abbreviated_pins_match_prefixes(self) -> None:
        releases = [release(PINNED_SHA, 1), release(sha(2), 2)]
        kept, prunable = prune.select_prunable(releases, ["5bb7a8b"], keep=1)
        self.assertEqual({item["sha"] for item in kept}, {PINNED_SHA, sha(2)})
        self.assertEqual(prunable, [])

    def test_application_releases_and_other_tags_are_ignored(self) -> None:
        releases = [release(sha(1), 1, prerelease=False, tag="v0.1.12"), release(sha(2), 2, tag="toolchain-windows-abc"),
                    release(sha(3), 3, prerelease=False), release(sha(4), 4)]
        kept, prunable = prune.select_prunable(releases, [], keep=0)
        self.assertEqual(kept, [])
        self.assertEqual([item["tag_name"] for item in prunable], ["build-" + sha(4)])

    def test_pin_file_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = Path(directory) / "good.json"
            good.write_text(json.dumps({"pinned": [{"sha": PINNED_SHA.upper()}]}))
            self.assertEqual(prune.load_pinned(good), [PINNED_SHA])
            bad = Path(directory) / "pins.json"
            bad.write_text(json.dumps({"pinned": [{"sha": "12345"}]}))
            with self.assertRaisesRegex(ValueError, "hex"):
                prune.load_pinned(bad)


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.releases = [release(sha(index), index) for index in range(1, 4)]
        self.commands: list[list[str]] = []

    def fake_check_output(self, command: list[str], **_: Any) -> str:
        self.commands.append(command)
        page = int(command[2].rsplit("page=", 1)[1])
        return json.dumps(self.releases if page == 1 else [])

    def fake_run(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    def run_main(self, *arguments: str) -> str:
        """Runs the command with a pin file naming a revision none of the releases carries."""
        buffer = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            pins = Path(directory) / "pins.json"
            pins.write_text(json.dumps({"pinned": [{"sha": PINNED_SHA}]}))
            with patch.object(prune.subprocess, "check_output", side_effect=self.fake_check_output), \
                 patch.object(prune.subprocess, "run", side_effect=self.fake_run), \
                 contextlib.redirect_stdout(buffer):
                self.assertEqual(prune.main(["--repo", "owner/repo", "--keep", "1", "--pinned", str(pins),
                                             *arguments]), 0)
        return buffer.getvalue()

    def test_dry_run_lists_without_deleting(self) -> None:
        text = self.run_main()
        self.assertIn("prune  build-" + sha(1), text)
        self.assertIn("keep   build-" + sha(3), text)
        self.assertIn("Dry run: 2 release(s) would be deleted", text)
        self.assertFalse(any(command[:3] == ["gh", "release", "delete"] for command in self.commands))

    def test_apply_deletes_only_the_prunable_releases(self) -> None:
        text = self.run_main("--apply")
        deletes = [command for command in self.commands if command[:3] == ["gh", "release", "delete"]]
        self.assertEqual([command[3] for command in deletes], ["build-" + sha(2), "build-" + sha(1)])
        for command in deletes:
            self.assertIn("--cleanup-tag", command)
            self.assertIn("--yes", command)
        self.assertIn("Deleted 2", text)


if __name__ == "__main__":
    unittest.main()
