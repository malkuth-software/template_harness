import json
import os
import unittest

from support import PLUGIN_ROOT, IsolatedCase, delivery

REPORT = {
    "urgency": "urgent",
    "title": "Build fix",
    "context": "Nightly build fails on M4 since 3f2a.\nTwo tests use the old clock API.",
    "options": [
        {"label": "Port the tests", "explanation": "Use the new API", "consequence": "fixed for good, 20 min"},
        {"label": "Pin the old API", "explanation": "Revert the bump", "consequence": "quick, debt stays"},
    ],
    "recommendation": {"option": "a", "why": "it removes the cause"},
}


class RenderReportTests(unittest.TestCase):
    def test_plain_text_for_telegram(self):
        title, body, urgency = delivery.render_report(REPORT, "telegram")
        self.assertEqual(urgency, "urgent")
        self.assertEqual(title, "[URGENT] Build fix")
        lines = body.splitlines()
        self.assertEqual(lines[0], "[URGENT] Build fix")
        self.assertIn("Nightly build fails on M4 since 3f2a.", lines)
        self.assertIn("a) Port the tests", lines)
        self.assertIn("   -> fixed for good, 20 min", lines)
        self.assertIn("b) Pin the old API", lines)
        self.assertIn("Recommended: a - it removes the cause", lines)
        self.assertNotIn("**", body)
        self.assertNotIn("|", body)  # no tables

    def test_markdown_lite_for_ntfy(self):
        title, body, _ = delivery.render_report(REPORT, "ntfy")
        self.assertIn("**a) Port the tests**", body)
        self.assertFalse(body.startswith("[URGENT]"))  # ntfy carries the title in a header

    def test_unknown_urgency_falls_back_to_normal(self):
        _, _, urgency = delivery.render_report({"urgency": "panic", "title": "x"}, "stdout")
        self.assertEqual(urgency, "normal")

    def test_context_capped_and_long_body_truncated(self):
        report = {"title": "t", "context": ["1", "2", "3", "4"], "options": [{"label": "x" * 5000}]}
        _, body, _ = delivery.render_report(report, "telegram")
        self.assertNotIn("\n4\n", body)
        self.assertLessEqual(len(body), delivery.MAX_TEXT)
        self.assertTrue(body.endswith("..."))

    def test_split_urgency(self):
        self.assertEqual(delivery.split_urgency("[Blocking] Push now?"), ("blocking", "Push now?"))
        self.assertEqual(delivery.split_urgency("Push now?", "normal"), ("normal", "Push now?"))

    def test_question_to_report_marks_recommendation(self):
        question = {"question": "[urgent] CI is red. Which fix?", "header": "CI",
                    "options": [{"label": "Revert (Recommended)", "description": "Undo 3f2a -> green today"},
                                {"label": "Patch", "description": "Fix forward -> slower"}],
                    "multiSelect": False}
        report = delivery.question_to_report(question, 0, 2, "neo")
        self.assertEqual(report["urgency"], "urgent")
        self.assertEqual(report["title"], "CI (1/2) - neo")
        self.assertEqual(report["recommendation"], {"option": "a"})
        title, body, urgency = delivery.render_report(report, "telegram")
        self.assertIn("a) Revert (Recommended)", body)
        self.assertIn("   Undo 3f2a -> green today", body)
        self.assertIn("CI is red. Which fix?", body)

    def test_untagged_question_defaults_to_blocking(self):
        report = delivery.question_to_report({"question": "Which?", "options": [{"label": "x"}]})
        self.assertEqual(report["urgency"], "blocking")

    def test_permission_render_redacts_and_previews(self):
        text = delivery.render_permission("Bash", {"command": "curl -H 'Authorization: Bearer abcdef123' x",
                                                   "description": "Fetch"}, "Fetch", "proj", "neo", 240)
        self.assertIn("[BLOCKING] Permission needed - neo", text)
        self.assertIn("Tool: Bash", text)
        self.assertNotIn("abcdef123", text)
        self.assertIn("4 min", text)

    def test_input_preview_for_edit_and_unknown(self):
        self.assertEqual(delivery.input_preview("Edit", {"file_path": "/a/b.cpp", "old_string": "x"}), "/a/b.cpp")
        preview = delivery.input_preview("mcp__x__y", {"q": "v" * 1000})
        self.assertLessEqual(len(preview), 300)

    def test_parse_letters(self):
        self.assertEqual(delivery.parse_letters("a c", 3), [0, 2])
        self.assertEqual(delivery.parse_letters("b) ", 3), [1])
        self.assertEqual(delivery.parse_letters("2,3", 3), [1, 2])
        self.assertIsNone(delivery.parse_letters("d", 3))
        self.assertIsNone(delivery.parse_letters("something else", 3))


class RenderCliTests(IsolatedCase):
    def test_render_cli_has_no_network(self):
        import subprocess
        import sys
        completed = subprocess.run([sys.executable, os.path.join(PLUGIN_ROOT, "scripts", "delivery.py"), "render"],
                                   input=json.dumps(REPORT), capture_output=True, text=True, env=self.env)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("urgency: urgent", completed.stdout)
        self.assertIn("a) Port the tests", completed.stdout)


class HookTimeoutConsistencyTests(unittest.TestCase):
    def test_hooks_json_timeouts_match_script(self):
        with open(os.path.join(PLUGIN_ROOT, "hooks", "hooks.json"), encoding="utf-8") as handle:
            hooks = json.load(handle)["hooks"]
        seen = {}
        for groups in hooks.values():
            for group in groups:
                for hook in group["hooks"]:
                    command = hook["command"].rsplit(" ", 1)[1]
                    seen[command] = hook["timeout"]
        self.assertEqual(seen, delivery.HOOK_TIMEOUT_S)
        for command in ("hook-permission", "hook-question"):
            self.assertEqual(delivery.relay_wait_seconds(9999, command), seen[command] - delivery.WAIT_MARGIN_S)
        self.assertEqual(delivery.relay_wait_seconds(0, "hook-permission"), 1.0)

    def test_question_hook_matcher_is_narrow(self):
        with open(os.path.join(PLUGIN_ROOT, "hooks", "hooks.json"), encoding="utf-8") as handle:
            hooks = json.load(handle)["hooks"]
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "AskUserQuestion")
        self.assertNotIn("*", hooks["Notification"][0]["matcher"])

    def test_plugin_manifest_tokens_are_sensitive(self):
        with open(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        for key, option in manifest["userConfig"].items():
            if "token" in key:
                self.assertTrue(option["sensitive"], key)
            self.assertEqual(set(option) - {"type", "title", "description", "sensitive", "required", "default"}, set())


if __name__ == "__main__":
    unittest.main()
