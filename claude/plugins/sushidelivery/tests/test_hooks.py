"""Hook JSON in/out for all three hooks, run as subprocesses against local mocks."""

import json
import os
import time
import unittest

from mock_servers import MockNtfy, MockTelegram
from support import IsolatedCase, delivery

BASE_INPUT = {"session_id": "abc123", "transcript_path": "/tmp/t.jsonl", "permission_mode": "default"}

PERMISSION_INPUT = dict(BASE_INPUT, hook_event_name="PermissionRequest", tool_name="Bash",
                        tool_input={"command": "git push origin main", "description": "Push the branch"})

QUESTIONS = [
    {"question": "[blocking] CI is green. Push v0.4 now?", "header": "Push",
     "options": [{"label": "Push now (Recommended)", "description": "Ship it -> team gets v0.4"},
                 {"label": "Wait", "description": "Hold for review -> slips a day"}],
     "multiSelect": False},
    {"question": "Which changelog style?", "header": "Changelog",
     "options": [{"label": "Short", "description": "One line each"},
                 {"label": "Long", "description": "Full notes"}],
     "multiSelect": False},
]

QUESTION_INPUT = dict(BASE_INPUT, hook_event_name="PreToolUse", tool_name="AskUserQuestion",
                      tool_use_id="toolu_01", tool_input={"questions": QUESTIONS})


class PermissionHookTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.api = MockTelegram().start()
        self.addCleanup(self.api.stop)
        self.use_token()

    def test_allow_from_allowlisted_owner(self):
        self.write_config(self.telegram_config(self.api.base))
        self.api.add_update(self.api.callback(":allow"))
        completed = self.run_hook("hook-permission", PERMISSION_INPUT)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout), {"hookSpecificOutput": {
            "hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}})
        sent = self.api.sent_texts()[0]
        self.assertIn("git push origin main", sent)
        self.assertIn("Tool: Bash", sent)

    def test_deny_from_allowlisted_owner(self):
        self.write_config(self.telegram_config(self.api.base))
        self.api.add_update(self.api.callback(":deny"))
        output = json.loads(self.run_hook("hook-permission", PERMISSION_INPUT).stdout)
        decision = output["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("SushiDelivery relay", decision["message"])
        self.assertNotIn("interrupt", decision)
        self.assertNotIn("updatedPermissions", decision)

    def test_non_allowlisted_reply_gives_no_decision(self):
        self.write_config(self.telegram_config(self.api.base, permission_relay={"timeout_s": 1.5}))
        self.api.add_update(self.api.callback(":allow", user_id=4242))
        completed = self.run_hook("hook-permission", PERMISSION_INPUT)
        self.assertEqual((completed.returncode, completed.stdout), (0, ""))

    def test_timeout_gives_no_decision(self):
        self.write_config(self.telegram_config(self.api.base, permission_relay={"timeout_s": 1}))
        started = time.time()
        completed = self.run_hook("hook-permission", PERMISSION_INPUT)
        self.assertEqual((completed.returncode, completed.stdout), (0, ""))
        self.assertLess(time.time() - started, 15)

    def test_malformed_response_gives_no_decision(self):
        self.write_config(self.telegram_config(self.api.base))
        self.api.malformed = "getUpdates"
        completed = self.run_hook("hook-permission", PERMISSION_INPUT)
        self.assertEqual((completed.returncode, completed.stdout), (0, ""))
        self.assertIn("hook-permission error", self.read_log())

    def test_unreachable_api_gives_no_decision(self):
        self.write_config(self.telegram_config("http://127.0.0.1:9"))
        completed = self.run_hook("hook-permission", PERMISSION_INPUT)
        self.assertEqual((completed.returncode, completed.stdout), (0, ""))

    def test_token_never_logged(self):
        self.write_config(self.telegram_config(self.api.base))
        self.api.malformed = "sendMessage"
        self.run_hook("hook-permission", PERMISSION_INPUT)
        from mock_servers import TOKEN
        self.assertNotIn(TOKEN, self.read_log())

    def test_tool_not_listed_is_not_relayed(self):
        self.write_config(self.telegram_config(self.api.base))
        completed = self.run_hook("hook-permission", dict(PERMISSION_INPUT, tool_name="Write",
                                                          tool_input={"file_path": "/x"}))
        self.assertEqual(completed.stdout, "")
        self.assertEqual(self.api.requests, [])

    def test_input_regex_filters(self):
        self.write_config(self.telegram_config(self.api.base, permission_relay={"input_regex": "^rm "}))
        self.assertEqual(self.run_hook("hook-permission", PERMISSION_INPUT).stdout, "")
        self.assertEqual(self.api.requests, [])

    def test_disabled_by_default(self):
        self.write_config({"transport": "telegram", "telegram": {"api_base": self.api.base, "allowed_user_ids": [111]},
                           "permission_relay": {"tools": ["Bash"]}})
        self.assertEqual(self.run_hook("hook-permission", PERMISSION_INPUT).stdout, "")
        self.assertEqual(self.api.requests, [])

    def test_missing_config_is_off(self):
        self.assertEqual(self.run_hook("hook-permission", PERMISSION_INPUT).stdout, "")
        self.assertEqual(self.api.requests, [])

    def test_missing_allowlist_is_off(self):
        config = self.telegram_config(self.api.base)
        config["telegram"] = {"api_base": self.api.base, "allowed_user_ids": []}
        self.write_config(config)
        self.assertEqual(self.run_hook("hook-permission", PERMISSION_INPUT).stdout, "")
        self.assertEqual(self.api.requests, [])

    def test_missing_token_is_off(self):
        del self.env["CLAUDE_PLUGIN_OPTION_TELEGRAM_BOT_TOKEN"]
        self.write_config(self.telegram_config(self.api.base))
        self.assertEqual(self.run_hook("hook-permission", PERMISSION_INPUT).stdout, "")
        self.assertEqual(self.api.requests, [])

    def test_ntfy_cannot_approve(self):
        config = self.telegram_config(self.api.base)
        config.update({"transport": "ntfy", "ntfy": {"server": self.api.base, "topic": "t"}})
        self.write_config(config)
        self.assertEqual(self.run_hook("hook-permission", PERMISSION_INPUT).stdout, "")
        self.assertEqual(self.api.requests, [])

    def test_owner_present_skips_relay(self):
        marker = os.path.join(self.tmp, "present")
        open(marker, "w").close()
        self.write_config(self.telegram_config(self.api.base, presence={"mode": "file", "file": marker}))
        self.assertEqual(self.run_hook("hook-permission", PERMISSION_INPUT).stdout, "")
        self.assertEqual(self.api.requests, [])

    def test_garbage_stdin_is_silent(self):
        self.write_config(self.telegram_config(self.api.base))
        import subprocess
        import sys
        from support import SCRIPT
        completed = subprocess.run([sys.executable, SCRIPT, "hook-permission"], input="not json",
                                   capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual((completed.returncode, completed.stdout), (0, ""))


class QuestionHookTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.api = MockTelegram().start()
        self.addCleanup(self.api.stop)
        self.use_token()

    def test_answers_returned_in_updated_input(self):
        self.write_config(self.telegram_config(self.api.base, question_relay={"timeout_s": 5}))
        self.api.add_update(self.api.callback(":0"), release_after=1)
        self.api.add_update(self.api.text_reply("b"), release_after=2)
        completed = self.run_hook("hook-question", QUESTION_INPUT)
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertEqual(output["updatedInput"]["questions"], QUESTIONS)  # echoed unchanged
        self.assertEqual(output["updatedInput"]["answers"], {
            "[blocking] CI is green. Push v0.4 now?": "Push now (Recommended)",
            "Which changelog style?": "Long",
        })
        first = self.api.sent_texts()[0]
        self.assertTrue(first.startswith("[BLOCKING] Push (1/2) - testbox"))
        self.assertIn("Recommended: a", first)

    def test_partial_answers_give_no_decision(self):
        self.write_config(self.telegram_config(self.api.base, question_relay={"timeout_s": 2}))
        self.api.add_update(self.api.callback(":0"), release_after=1)
        completed = self.run_hook("hook-question", QUESTION_INPUT)
        self.assertEqual((completed.returncode, completed.stdout), (0, ""))

    def test_non_allowlisted_answer_gives_no_decision(self):
        self.write_config(self.telegram_config(self.api.base, question_relay={"timeout_s": 1.5}))
        single = dict(QUESTION_INPUT, tool_input={"questions": QUESTIONS[:1]})
        self.api.add_update(self.api.callback(":0", user_id=31337))
        self.assertEqual(self.run_hook("hook-question", single).stdout, "")

    def test_malformed_response_gives_no_decision(self):
        self.write_config(self.telegram_config(self.api.base))
        self.api.malformed = "getUpdates"
        self.assertEqual(self.run_hook("hook-question", QUESTION_INPUT).stdout, "")

    def test_invalid_tool_input_is_ignored(self):
        self.write_config(self.telegram_config(self.api.base))
        for tool_input in ({}, {"questions": []}, {"questions": [{"question": "x", "options": []}]},
                           {"questions": "nope"}):
            completed = self.run_hook("hook-question", dict(QUESTION_INPUT, tool_input=tool_input))
            self.assertEqual(completed.stdout, "")
        self.assertEqual(self.api.requests, [])

    def test_disabled_by_default(self):
        config = self.telegram_config(self.api.base)
        del config["question_relay"]
        self.write_config(config)
        self.assertEqual(self.run_hook("hook-question", QUESTION_INPUT).stdout, "")
        self.assertEqual(self.api.requests, [])


class NotifyHookTests(IsolatedCase):
    def notify_input(self, kind="permission_prompt"):
        return dict(BASE_INPUT, hook_event_name="Notification", cwd=self.project,
                    message="Claude needs your permission to use Bash", title="Permission needed",
                    notification_type=kind)

    def test_stdout_transport_and_urgency(self):
        self.write_config({"transport": "stdout", "label": "box", "presence": {"mode": "off"},
                           "notify": {"enabled": True}})
        completed = self.run_hook("hook-notify", self.notify_input())
        self.assertEqual((completed.returncode, completed.stdout), (0, ""))
        written = open(self.out_file).read()
        self.assertIn("[blocking] [BLOCKING] Permission needed - box", written)
        self.assertIn("Project: project", written)

    def test_rate_limited_per_type(self):
        self.write_config({"transport": "stdout", "presence": {"mode": "off"},
                           "notify": {"enabled": True, "per_type_interval_s": 600}})
        self.run_hook("hook-notify", self.notify_input())
        self.run_hook("hook-notify", self.notify_input())
        self.run_hook("hook-notify", self.notify_input("idle_prompt"))
        written = open(self.out_file).read()
        self.assertEqual(written.count("--- sushidelivery"), 2)
        self.assertIn("rate limit", self.read_log())

    def test_type_not_selected_is_skipped(self):
        self.write_config({"transport": "stdout", "presence": {"mode": "off"}, "notify": {"enabled": True}})
        self.run_hook("hook-notify", self.notify_input("agent_completed"))
        self.assertFalse(os.path.exists(self.out_file))

    def test_off_by_default(self):
        self.write_config({"transport": "stdout"})
        self.run_hook("hook-notify", self.notify_input())
        self.assertFalse(os.path.exists(self.out_file))

    def test_ntfy_notification(self):
        server = MockNtfy().start()
        self.addCleanup(server.stop)
        self.write_config({"transport": "ntfy", "presence": {"mode": "off"}, "notify": {"enabled": True},
                           "ntfy": {"server": server.base, "topic": "my-topic"}})
        completed = self.run_hook("hook-notify", self.notify_input("idle_prompt"))
        self.assertEqual(completed.stdout, "")
        request = server.calls("/my-topic")[0]
        headers = {key.lower(): value for key, value in request["headers"].items()}
        self.assertEqual(headers["priority"], "3")
        self.assertIn(b"Claude needs your permission", request["body"])

    def test_ntfy_failure_is_silent(self):
        server = MockNtfy(status=503).start()
        self.addCleanup(server.stop)
        self.write_config({"transport": "ntfy", "presence": {"mode": "off"}, "notify": {"enabled": True},
                           "ntfy": {"server": server.base, "topic": "my-topic"}})
        completed = self.run_hook("hook-notify", self.notify_input())
        self.assertEqual((completed.returncode, completed.stdout), (0, ""))
        self.assertIn("HTTP 503", self.read_log())

    def test_in_process_presence_skip(self):
        self.write_config({"transport": "stdout", "presence": {"mode": "idle", "idle_minutes": 5},
                           "notify": {"enabled": True}})
        delivery.command_hook_notify(self.notify_input(), idle_probe=lambda: 10)
        self.assertFalse(os.path.exists(self.out_file))
        delivery.command_hook_notify(self.notify_input(), idle_probe=lambda: 900)
        self.assertTrue(os.path.exists(self.out_file))


if __name__ == "__main__":
    unittest.main()
