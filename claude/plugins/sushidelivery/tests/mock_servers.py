"""Local mock HTTP servers for the Telegram Bot API and ntfy. Loopback only; no real services."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "123456:TEST-not-a-real-token"


class MockServer:
    """Records every request; replies via a per-path handler function."""

    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with outer.lock:
                    outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status, payload = outer.respond(self.path, body)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = _handle
            do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def respond(self, path, body):
        return 200, {"ok": True}

    def calls(self, suffix):
        with self.lock:
            return [request for request in self.requests if request["path"].endswith(suffix)]


class MockTelegram(MockServer):
    """Telegram Bot API mock.

    Queue updates with add_update(); they are delivered on the first getUpdates after
    `release_after` sendMessage calls (so replies can reference the sent message id).
    Set `malformed` to a method name to make that method return garbage.
    """

    def __init__(self, user_id=111, chat_id=111):
        super().__init__()
        self.user_id = user_id
        self.chat_id = chat_id
        self.next_message_id = 500
        self.pending = []          # list of (release_after_sends, factory(message_ids) -> update)
        self.message_ids = []
        self.update_id = 9000
        self.malformed = None
        self.fail_status = None

    def add_update(self, factory, release_after=1):
        self.pending.append((release_after, factory))

    def respond(self, path, body):
        if not path.startswith("/bot%s/" % TOKEN):
            return 404, {"ok": False, "description": "Not Found"}
        method = path.rsplit("/", 1)[1]
        if self.fail_status:
            return self.fail_status, {"ok": False}
        if self.malformed == method:
            return 200, b"<html>not json"
        payload = json.loads(body.decode("utf-8")) if body else {}
        if method == "sendMessage":
            self.next_message_id += 1
            if "reply_markup" in payload or not payload.get("reply_to_message_id"):
                self.message_ids.append(self.next_message_id)
            return 200, {"ok": True, "result": {"message_id": self.next_message_id,
                                                "chat": {"id": payload.get("chat_id")}}}
        if method == "getUpdates":
            ready = []
            keep = []
            for release_after, factory in self.pending:
                if len(self.message_ids) >= release_after:
                    self.update_id += 1
                    update = factory(self.message_ids)
                    update["update_id"] = self.update_id
                    ready.append(update)
                else:
                    keep.append((release_after, factory))
            self.pending = keep
            if not ready:
                time.sleep(0.05)
            return 200, {"ok": True, "result": ready}
        return 200, {"ok": True, "result": True}

    # -- update factories

    def callback(self, data_suffix, user_id=None, chat_id=None, message_index=-1, is_bot=False):
        """A button press whose callback_data ends with data_suffix, on the n-th sent message."""
        def factory(message_ids):
            sent = self.sent_buttons()[message_index]
            data = next(button for button in sent if button.endswith(data_suffix))
            return {"callback_query": {
                "id": "cb%d" % len(message_ids),
                "from": {"id": user_id or self.user_id, "is_bot": is_bot, "first_name": "Owner"},
                "message": {"message_id": message_ids[message_index], "chat": {"id": chat_id or self.chat_id}},
                "data": data}}
        return factory

    def text_reply(self, text, user_id=None, chat_id=None, message_index=-1):
        def factory(message_ids):
            return {"message": {
                "message_id": 7000 + len(message_ids),
                "from": {"id": user_id or self.user_id, "is_bot": False, "first_name": "Owner"},
                "chat": {"id": chat_id or self.chat_id},
                "text": text,
                "reply_to_message": {"message_id": message_ids[message_index]}}}
        return factory

    def sent_buttons(self):
        result = []
        for request in self.calls("/sendMessage"):
            payload = json.loads(request["body"].decode("utf-8"))
            markup = payload.get("reply_markup")
            if markup:
                result.append([row[0]["callback_data"] for row in markup["inline_keyboard"]])
        return result

    def sent_texts(self):
        return [json.loads(request["body"].decode("utf-8"))["text"] for request in self.calls("/sendMessage")]


class MockNtfy(MockServer):
    def __init__(self, status=200):
        super().__init__()
        self.status = status

    def respond(self, path, body):
        return self.status, {"id": "abc", "event": "message"}
