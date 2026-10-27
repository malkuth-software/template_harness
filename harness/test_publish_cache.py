"""Concurrent platforms may create one release, but cannot replace its bytes."""
from __future__ import annotations
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import harness_testing
import publish_cache


class PublicationCommands:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.creation_race: bool = False
        self.existing_asset: bool = False
        self.existing_bytes: bytes = b'archive'
        self.views: int = 0

    def run(self, command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        result: int = 0
        action: str = command[2]
        if action == 'view':
            self.views += 1
            if self.views == 1:
                result = 1
        if action == 'create' and self.creation_race:
            result = 1
        if action == 'upload' and self.existing_asset:
            result = 1
        if action == 'download':
            destination: Path = Path(command[command.index('--dir') + 1])
            name: str = command[command.index('--pattern') + 1]
            (destination / name).write_bytes(self.existing_bytes)
        if options.get('check') and result != 0:
            raise subprocess.CalledProcessError(result, command)
        return subprocess.CompletedProcess(command, result, '', '')


class PublicationTests(unittest.TestCase):
    def publish(self, fixture: PublicationCommands, revision: str = 'revision') -> None:
        reference: dict[str, Any] = {'object': {'type': 'commit', 'sha': revision}}
        with tempfile.TemporaryDirectory() as directory:
            archive: Path = Path(directory) / 'gui-forms-cache-linux-x64.tar.gz'
            archive.write_bytes(b'archive')
            with patch.object(publish_cache.subprocess, 'run', side_effect=fixture.run), \
                 patch.object(publish_cache, 'api', return_value=reference):
                publish_cache.publish('owner/repo', 'revision', archive)

    def test_first_platform_creates_and_publishes_without_other_archives(self) -> None:
        fixture: PublicationCommands = PublicationCommands()
        self.publish(fixture)
        self.assertEqual(fixture.calls[-1][2], 'upload')
        self.assertNotIn('--clobber', fixture.calls[-1])

    def test_other_platform_winning_release_creation_is_accepted(self) -> None:
        fixture: PublicationCommands = PublicationCommands()
        fixture.creation_race = True
        self.publish(fixture)
        self.assertEqual(fixture.views, 2)

    def test_wrong_source_tag_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, 'tested source commit'):
            self.publish(PublicationCommands(), 'wrong')

    def test_rerun_accepts_equal_bytes_but_refuses_changed_asset(self) -> None:
        fixture: PublicationCommands = PublicationCommands()
        fixture.existing_asset = True
        self.publish(fixture)
        fixture = PublicationCommands()
        fixture.existing_asset = True
        fixture.existing_bytes = b'different'
        with self.assertRaisesRegex(ValueError, 'different bytes'):
            self.publish(fixture)


if __name__ == '__main__':
    unittest.main()
