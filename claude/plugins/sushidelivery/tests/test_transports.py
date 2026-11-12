import json
import os
import time
import unittest

from mock_servers import TOKEN, MockNtfy, MockTelegram
from support import IsolatedCase, delivery


class TelegramTransportTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.api = MockTelegram().start()
        self.addCleanup(self.api.stop)
        self.config = self.telegram_config(self.api.base)

    def transport(self, **telegram):
        config = json.loads(json.dumps(self.config))
        config["telegram"].update(telegram)
        return delivery.TelegramTransport(config, TOKEN, directory=self.state, project_root=self.project)

    def test_send_posts_message_with_buttons(self):
        message_id = self.transport().send("t", "hello", "blocking", [("Allow", "sd:1:allow"), ("Deny", "sd:1:deny")])
        self.assertEqual(message_id, 501)
        payload = json.loads(self.api.calls("/sendMessage")[0]["body"])
        self.assertEqual(payload["chat_id"], 111)
        self.assertEqual(payload["text"], "hello")
        self.assertFalse(payload["disable_notification"])
        self.assertEqual(payload["reply_markup"]["inline_keyboard"][1][0], {"text": "Deny", "callback_data": "sd:1:deny"})

    def test_low_urgency_is_silent(self):
        self.transport().send("t", "fyi", "low")
        self.assertTrue(json.loads(self.api.calls("/sendMessage")[0]["body"])["disable_notification"])

    def test_allowlisted_button_allows(self):
        self.api.add_update(self.api.callback(":allow"))
        decision = delivery.ask_permission(self.transport(), "Bash", {"command": "git push"}, None, "p", "box", 5)
        self.assertEqual(decision, "allow")
        self.assertTrue(self.api.calls("/answerCallbackQuery"))
        self.assertTrue(self.api.calls("/editMessageReplyMarkup"))

    def test_allowlisted_text_reply_denies(self):
        self.api.add_update(self.api.text_reply("no"))
        decision = delivery.ask_permission(self.transport(), "Bash", {"command": "rm -rf build"}, None, "p", "box", 5)
        self.assertEqual(decision, "deny")

    def test_non_allowlisted_user_is_ignored(self):
        self.api.add_update(self.api.callback(":allow", user_id=999))
        started = time.time()
        decision = delivery.ask_permission(self.transport(), "Bash", {"command": "git push"}, None, "p", "box", 1.5)
        self.assertIsNone(decision)
        self.assertGreaterEqual(time.time() - started, 1.4)
        self.assertIn("not on the allowlist", self.read_log())

    def test_allowlisted_user_in_wrong_chat_is_ignored(self):
        self.api.add_update(self.api.callback(":allow", chat_id=-100555))
        self.assertIsNone(delivery.ask_permission(self.transport(), "Bash", {"command": "x"}, None, "p", "b", 1.5))

    def test_bot_sender_is_ignored(self):
        self.api.add_update(self.api.callback(":allow", is_bot=True))
        self.assertIsNone(delivery.ask_permission(self.transport(), "Bash", {"command": "x"}, None, "p", "b", 1.5))

    def test_text_not_replying_to_request_is_ignored(self):
        def factory(message_ids):
            return {"message": {"message_id": 1, "from": {"id": 111, "is_bot": False}, "chat": {"id": 111},
                                "text": "yes"}}
        self.api.add_update(factory)
        self.assertIsNone(delivery.ask_permission(self.transport(), "Bash", {"command": "x"}, None, "p", "b", 1.5))

    def test_stale_button_from_other_request_is_ignored(self):
        def factory(message_ids):
            return {"callback_query": {"id": "c", "from": {"id": 111, "is_bot": False},
                                       "message": {"message_id": message_ids[-1], "chat": {"id": 111}},
                                       "data": "sd:000000000000:allow"}}
        self.api.add_update(factory)
        self.assertIsNone(delivery.ask_permission(self.transport(), "Bash", {"command": "x"}, None, "p", "b", 1.5))

    def test_timeout_is_no_decision_and_says_so(self):
        decision = delivery.ask_permission(self.transport(), "Bash", {"command": "x"}, None, "p", "b", 1)
        self.assertIsNone(decision)
        self.assertTrue(any("Timed out" in text for text in self.api.sent_texts()))

    def test_malformed_getupdates_raises(self):
        self.api.malformed = "getUpdates"
        with self.assertRaises(delivery.DeliveryError):
            delivery.ask_permission(self.transport(), "Bash", {"command": "x"}, None, "p", "b", 2)

    def test_malformed_send_raises(self):
        self.api.malformed = "sendMessage"
        with self.assertRaises(delivery.DeliveryError):
            self.transport().send("t", "x", "normal")

    def test_ok_false_raises(self):
        self.api.fail_status = 409  # e.g. a webhook or another poller owns the bot
        with self.assertRaises(delivery.DeliveryError):
            self.transport().send("t", "x", "normal")

    def test_offset_is_persisted_and_unauthenticated_updates_not_stored(self):
        self.api.add_update(self.api.callback(":allow", user_id=999))
        transport = self.transport()
        transport.send("t", "x", "normal", [("Allow", "sd:abc:allow")])
        transport.poll_once(0)
        offset = json.load(open(os.path.join(self.state, "telegram_offset.json")))["offset"]
        self.assertEqual(offset, self.api.update_id + 1)
        self.assertEqual(transport.read_inbox(), [])
        payload = json.loads(self.api.calls("/getUpdates")[0]["body"])
        self.assertEqual(payload["allowed_updates"], ["message", "callback_query"])

    def test_question_single_select_by_button(self):
        self.api.add_update(self.api.callback(":1"))
        question = {"question": "Which?", "header": "H", "options": [{"label": "A"}, {"label": "B"}]}
        answer = delivery.ask_question(self.transport(), question, 0, 1, "box", time.time() + 5)
        self.assertEqual(answer, "B")

    def test_question_multi_select_toggle_and_done(self):
        self.api.add_update(self.api.callback(":0"))
        self.api.add_update(self.api.callback(":2"))
        self.api.add_update(self.api.callback(":done"))
        question = {"question": "Which?", "header": "H", "multiSelect": True,
                    "options": [{"label": "A"}, {"label": "B"}, {"label": "C"}]}
        answer = delivery.ask_question(self.transport(), question, 0, 1, "box", time.time() + 5)
        self.assertEqual(answer, "A, C")

    def test_question_free_text_reply(self):
        self.api.add_update(self.api.text_reply("neither, wait for me"))
        question = {"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}
        answer = delivery.ask_question(self.transport(), question, 0, 1, "box", time.time() + 5)
        self.assertEqual(answer, "neither, wait for me")

    def test_question_free_text_disabled_keeps_waiting(self):
        self.api.add_update(self.api.text_reply("neither"))
        question = {"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}
        answer = delivery.ask_question(self.transport(), question, 0, 1, "box", time.time() + 1.5,
                                       allow_free_text=False)
        self.assertIsNone(answer)

    def test_chat_id_defaults_to_single_allowed_user(self):
        transport = self.transport(chat_id=None)
        self.assertEqual(transport.chat_id, 111)
        self.assertFalse(self.transport(chat_id=None, allowed_user_ids=[]).can_receive)

    def test_plain_http_to_non_loopback_rejected(self):
        with self.assertRaises(delivery.DeliveryError):
            delivery.check_base_url("http://api.telegram.org")
        self.assertEqual(delivery.check_base_url("https://api.telegram.org/"), "https://api.telegram.org")


class NtfyTransportTests(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.server = MockNtfy().start()
        self.addCleanup(self.server.stop)

    def config(self, topic="sushi-test-topic"):
        return {"transport": "ntfy", "ntfy": {"server": self.server.base, "topic": topic}}

    def test_post_with_priority_headers(self):
        transport = delivery.NtfyTransport(self.config(), token="tk_test")
        transport.send("[BLOCKING] Permission needed - box", "body text", "blocking")
        request = self.server.calls("/sushi-test-topic")[0]
        headers = {key.lower(): value for key, value in request["headers"].items()}
        self.assertEqual(headers["priority"], "5")
        self.assertEqual(headers["title"], "[BLOCKING] Permission needed - box")
        self.assertIn("blocking", headers["tags"])
        self.assertEqual(headers["authorization"], "Bearer tk_test")
        self.assertEqual(request["body"], b"body text")

    def test_priority_mapping_and_unicode_title(self):
        delivery.NtfyTransport(self.config()).send("Grüße", "x", "low")
        headers = {key.lower(): value for key, value in self.server.requests[0]["headers"].items()}
        self.assertEqual(headers["priority"], "2")
        self.assertTrue(headers["title"].startswith("=?UTF-8?B?"))
        self.assertNotIn("authorization", headers)

    def test_server_error_raises(self):
        self.server.status = 500
        with self.assertRaises(delivery.DeliveryError):
            delivery.NtfyTransport(self.config()).send("t", "x", "normal")

    def test_invalid_topic_rejected(self):
        with self.assertRaises(delivery.DeliveryError):
            delivery.NtfyTransport(self.config(topic="../etc"))


class RateLimitTests(IsolatedCase):
    def test_per_key_interval(self):
        self.assertFalse(delivery.rate_limited("notify:idle", 60, 10, now=1000, directory=self.state))
        self.assertTrue(delivery.rate_limited("notify:idle", 60, 10, now=1030, directory=self.state))
        self.assertFalse(delivery.rate_limited("notify:perm", 60, 10, now=1030, directory=self.state))
        self.assertFalse(delivery.rate_limited("notify:idle", 60, 10, now=1061, directory=self.state))

    def test_hourly_cap(self):
        for index in range(3):
            self.assertFalse(delivery.rate_limited("k%d" % index, 0, 3, now=2000 + index, directory=self.state))
        self.assertTrue(delivery.rate_limited("k9", 0, 3, now=2010, directory=self.state))
        self.assertFalse(delivery.rate_limited("k9", 0, 3, now=2000 + 3601, directory=self.state))


class PresenceTests(IsolatedCase):
    def test_off_never_present(self):
        self.assertFalse(delivery.owner_present({"presence": {"mode": "off"}}, lambda: 0))

    def test_file_marker(self):
        marker = os.path.join(self.tmp, "here")
        config = {"presence": {"mode": "file", "file": marker}}
        self.assertFalse(delivery.owner_present(config, lambda: 0))
        open(marker, "w").close()
        self.assertTrue(delivery.owner_present(config, lambda: 99999))

    def test_claude_presence_env_is_default_marker(self):
        marker = os.path.join(self.tmp, "present")
        open(marker, "w").close()
        os.environ["CLAUDE_CLIENT_PRESENCE_FILE"] = marker
        self.assertTrue(delivery.owner_present({"presence": {"mode": "auto"}}, lambda: 99999))

    def test_idle_threshold(self):
        config = {"presence": {"mode": "idle", "idle_minutes": 5}}
        self.assertTrue(delivery.owner_present(config, lambda: 30))
        self.assertFalse(delivery.owner_present(config, lambda: 301))
        self.assertFalse(delivery.owner_present(config, lambda: None))  # unknown counts as away


if __name__ == "__main__":
    unittest.main()
