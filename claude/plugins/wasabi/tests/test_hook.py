"""End to end: pipe PreToolUse hook JSON into `wasabi.py hook` and check the JSON it prints."""

import io
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from wasabi_testlib import SCRIPT, RepoTestCase, wasabi


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class HookEndToEnd(RepoTestCase):
    def run_hook(self, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload)
        result = subprocess.run([sys.executable, SCRIPT, "hook"], input=data.encode("utf-8"),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        out = result.stdout.decode("utf-8").strip()
        return json.loads(out) if out else None

    def bash(self, command, cwd=None, session="s1"):
        return {"session_id": session, "cwd": cwd or self.repo, "permission_mode": "default",
                "hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": command, "description": "test"}, "tool_use_id": "toolu_1"}

    def test_deny_in_shared_tree(self):
        self.mark_shared(self.repo)
        out = self.run_hook(self.bash("git stash"))["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("shared working tree", out["permissionDecisionReason"])
        log = os.path.join(self.repo, ".sushi", "logs", "wasabi.log")
        self.assertIn("deny shared_tree", read_text(log))

    def test_deny_wins_over_ask_and_warn(self):
        self.mark_shared(self.repo)
        out = self.run_hook(self.bash("curl -o x https://example.com/x x; python3; git stash"))["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("python3", out["additionalContext"])

    def test_ask_for_download(self):
        out = self.run_hook(self.bash("curl -o x https://example.com/x requests"))["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertIn("downloads a file", out["permissionDecisionReason"])
        self.assertIn("Wasabi asked the user", out["additionalContext"])

    def test_warn_only_adds_context(self):
        out = self.run_hook(self.bash("python3"))["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", out)
        self.assertIn("wait for input forever", out["additionalContext"])

    def test_warning_deduped_per_session(self):
        self.assertIsNotNone(self.run_hook(self.bash("cat", session="a")))
        self.assertIsNone(self.run_hook(self.bash("cat", session="a")))
        self.assertIsNotNone(self.run_hook(self.bash("cat", session="b")))

    def test_harmless_command_prints_nothing(self):
        self.assertIsNone(self.run_hook(self.bash("git status && ls -la")))

    def test_never_auto_allows(self):
        for command in ("git status", "curl -o x https://example.com/x x", "python3", "ls"):
            out = self.run_hook(self.bash(command, session=command)) or {}
            self.assertNotEqual((out.get("hookSpecificOutput") or {}).get("permissionDecision"), "allow")

    def test_bad_input_is_ignored(self):
        self.assertIsNone(self.run_hook(None, raw="not json"))
        self.assertIsNone(self.run_hook(None, raw=""))
        self.assertIsNone(self.run_hook(None, raw="[1, 2]"))
        self.assertIsNone(self.run_hook({"tool_name": "Bash", "tool_input": "git stash"}))
        self.assertIsNone(self.run_hook({"tool_name": "Bash", "tool_input": {"command": ["git", "stash"]}}))
        self.assertIsNone(self.run_hook({"tool_name": "Edit", "tool_input": {"file_path": "/x"}}))

    def test_missing_cwd_is_safe(self):
        payload = self.bash("git stash", cwd=os.path.join(self.tmp, "does-not-exist"))
        self.assertIsNone(self.run_hook(payload))

    def test_broken_config_falls_back_to_defaults(self):
        os.makedirs(os.path.join(self.repo, ".sushi"), exist_ok=True)
        with open(os.path.join(self.repo, ".sushi", "wasabi.json"), "w") as handle:
            handle.write("{not json")
        out = self.run_hook(self.bash("curl -o x https://example.com/x x"))
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")
        log = os.path.join(self.repo, ".sushi", "logs", "wasabi.log")
        self.assertIn("wasabi.json", read_text(log))

    def test_internal_crash_exits_zero_silently(self):
        stdin = io.StringIO(json.dumps(self.bash("git stash")))
        stdout = io.StringIO()
        with mock.patch.object(wasabi, "evaluate", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout), \
                mock.patch.object(sys, "argv", ["wasabi.py", "hook"]):
            self.assertEqual(wasabi.main(), 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_one_failing_guard_does_not_disable_the_others(self):
        self.mark_shared(self.repo)
        guards = tuple((name, (lambda invocations, ctx: 1 / 0) if name == "crlf" else guard)
                       for name, guard in wasabi.GUARDS)
        with mock.patch.object(wasabi, "GUARDS", guards):
            result = wasabi.evaluate("git stash", self.repo)
        self.assertEqual(result.decision(), "deny")
        self.assertTrue(any("crlf" in error for error in result.errors))

    def test_status_and_check_subcommands(self):
        self.mark_shared(self.repo)
        status = subprocess.run([sys.executable, SCRIPT, "status", self.repo], stdout=subprocess.PIPE,
                                check=True).stdout.decode()
        self.assertIn("is SHARED", status)
        self.assertIn("[crlf] on", status)
        check = subprocess.run([sys.executable, SCRIPT, "check", "--cwd", self.repo, "git", "stash"],
                               stdout=subprocess.PIPE, check=True).stdout.decode()
        self.assertIn("decision: deny", check)


if __name__ == "__main__":
    unittest.main()
