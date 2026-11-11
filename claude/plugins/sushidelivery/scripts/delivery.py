#!/usr/bin/env python3
"""SushiDelivery: urgency-tagged decision reports and an optional phone relay.

Subcommands
  hook-notify       Notification hook. Sends a short, send-only push when Claude needs
                    attention. Rate-limited. Off until "notify.enabled" is true.
  hook-permission   PermissionRequest hook. Relays the request to your Telegram bot with
                    Allow/Deny buttons and waits for an authenticated reply. On timeout or
                    any error it prints nothing, so the normal prompt appears.
                    Off until "permission_relay.enabled" is true and tools are listed.
  hook-question     PreToolUse hook on AskUserQuestion. Relays the questions and options;
                    an authenticated answer in time is returned as updatedInput.answers.
                    Otherwise nothing is printed and the normal dialog appears.
                    Off until "question_relay.enabled" is true.
  render [--transport T]
                    Render a report JSON (stdin) as it would be sent. No network.
  send [--urgency U] [--title T]
                    Send stdin text, send-only, via the configured transport.
  test-send         Send a short test message via the configured transport.
  telegram-ids      Print the user and chat ids of recent messages to your bot, to fill in
                    the allowlist. Reads only; does not confirm or delete updates.

Config: $SUSHIDELIVERY_CONFIG, else <project>/.sushi/sushidelivery.json, else
~/.sushi/sushidelivery.json. A missing config means everything is off.
Tokens: userConfig (CLAUDE_PLUGIN_OPTION_TELEGRAM_BOT_TOKEN / _NTFY_TOKEN) or the env vars
SUSHIDELIVERY_TELEGRAM_TOKEN / SUSHIDELIVERY_NTFY_TOKEN. Never from files.
Standard library only. Python 3.9+.
"""

import base64
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

URGENCIES = ("low", "normal", "urgent", "blocking")
URGENCY_TAG = re.compile(r"^\s*\[(low|normal|urgent|blocking)\]\s*", re.IGNORECASE)
NTFY_PRIORITY = {"low": 2, "normal": 3, "urgent": 4, "blocking": 5}

# Must match the "timeout" values in hooks/hooks.json (a test checks this).
HOOK_TIMEOUT_S = {"hook-notify": 20, "hook-permission": 330, "hook-question": 330}
WAIT_MARGIN_S = 30          # the relay always gives up this long before the hook timeout
MAX_TEXT = 3500             # below Telegram's 4096 and ntfy's 4096-byte message limits
LONG_POLL_MAX_S = 20

NOTIFY_URGENCY = {
    "permission_prompt": "blocking",
    "elicitation_dialog": "blocking",
    "elicitation_url_dialog": "blocking",
    "agent_needs_input": "urgent",
    "auth_storage_failure": "urgent",
    "idle_prompt": "normal",
    "agent_completed": "low",
}

DEFAULT_CONFIG = {
    "transport": None,               # "telegram" | "ntfy" | "stdout" | None (off)
    "label": None,                   # machine label shown in messages; default: hostname
    "notify": {
        "enabled": False,
        "types": ["permission_prompt", "idle_prompt", "elicitation_dialog",
                  "elicitation_url_dialog", "agent_needs_input"],
        "per_type_interval_s": 120,
        "max_per_hour": 20,
        "skip_when_present": True,
    },
    "permission_relay": {
        "enabled": False,
        "tools": [],                 # exact tool names, e.g. ["Bash"]; empty relays nothing
        "input_regex": None,         # optional extra filter on the input preview
        "timeout_s": 240,
    },
    "question_relay": {
        "enabled": False,
        "timeout_s": 240,
        "allow_free_text": True,
    },
    "presence": {
        "mode": "auto",              # "off" | "file" | "idle" | "auto"
        "file": None,                # default: $CLAUDE_CLIENT_PRESENCE_FILE
        "idle_minutes": 5,
    },
    "telegram": {
        "api_base": "https://api.telegram.org",
        "chat_id": None,
        "allowed_user_ids": [],
    },
    "ntfy": {
        "server": "https://ntfy.sh",
        "topic": None,
    },
}

SECRET_ENV = {
    "telegram_bot_token": "SUSHIDELIVERY_TELEGRAM_TOKEN",
    "ntfy_token": "SUSHIDELIVERY_NTFY_TOKEN",
}

SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)((?:token|secret|password|passwd|api[_-]?key|authorization)[\"']?\s*[=:\s]\s*[\"']?)[^\s\"']+"),
)


class DeliveryError(Exception):
    """A transport or configuration problem. Hooks turn it into 'no decision'."""


# ---------------------------------------------------------------- config and state

def find_project_root(start):
    directory = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(directory, ".sushi")) or os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def config_path(project_root):
    override = os.environ.get("SUSHIDELIVERY_CONFIG")
    if override:
        return override
    candidates = []
    if project_root:
        candidates.append(os.path.join(project_root, ".sushi", "sushidelivery.json"))
    candidates.append(os.path.join(os.path.expanduser("~"), ".sushi", "sushidelivery.json"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def load_config(project_root):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = config_path(project_root)
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            user = json.load(handle)
        if not isinstance(user, dict):
            raise DeliveryError("config is not a JSON object")
        for key, value in user.items():
            if isinstance(config.get(key), dict) and isinstance(value, dict):
                config[key].update(value)
            else:
                config[key] = value
    return config


def get_secret(name):
    for env_name in ("CLAUDE_PLUGIN_OPTION_" + name.upper(), SECRET_ENV[name]):
        value = os.environ.get(env_name, "").strip()
        if value and not value.startswith("${"):
            return value
    return None


def state_dir():
    path = os.environ.get("SUSHIDELIVERY_STATE_DIR")
    if not path:
        data = os.environ.get("CLAUDE_PLUGIN_DATA")
        path = os.path.join(data, "state") if data else os.path.join(os.path.expanduser("~"), ".sushi", "state", "sushidelivery")
    os.makedirs(path, exist_ok=True)
    return path


def scrub(text):
    text = str(text)
    for name in SECRET_ENV:
        value = get_secret(name)
        if value:
            text = text.replace(value, "<token>")
    return text


def log(project_root, message):
    try:
        directory = os.path.join(project_root or os.path.expanduser("~"), ".sushi", "logs")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "sushidelivery.log"), "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), scrub(message)))
    except Exception:
        pass


class FileLock:
    def __init__(self, path, blocking=True):
        self.path = path
        self.blocking = blocking
        self.handle = None
        self.acquired = False

    def __enter__(self):
        self.handle = open(self.path, "a+")
        if fcntl is None:
            self.acquired = True
            return self
        flags = fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(self.handle, flags)
            self.acquired = True
        except OSError:
            self.acquired = False
        return self

    def __exit__(self, *exc):
        if self.acquired and fcntl is not None:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()
        return False


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data):
    temporary = "%s.%d.tmp" % (path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)


# ---------------------------------------------------------------- presence and rate limits

def hid_idle_seconds():
    """Seconds since the last keyboard/mouse input on macOS, or None if unknown."""
    if sys.platform != "darwin":
        return None
    try:
        output = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True,
                                text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', output)
    if match is None:
        return None
    return int(match.group(1)) / 1e9


def owner_present(config, idle_probe=hid_idle_seconds):
    presence = config.get("presence") or {}
    mode = presence.get("mode", "auto")
    if mode == "off":
        return False
    marker = presence.get("file") or os.environ.get("CLAUDE_CLIENT_PRESENCE_FILE")
    if mode in ("file", "auto") and marker:
        if os.path.exists(os.path.expanduser(marker)):
            return True
        if mode == "file":
            return False
    if mode == "file":
        return False
    if mode in ("idle", "auto"):
        idle = idle_probe()
        if idle is None:
            return False  # unknown: treat as away, since the owner turned the relay on
        return idle < float(presence.get("idle_minutes", 5)) * 60
    return False


def rate_limited(key, per_key_interval_s, max_per_hour, now=None, directory=None):
    """Record a send for `key` and return False, or return True if over a limit."""
    now = time.time() if now is None else now
    directory = directory or state_dir()
    path = os.path.join(directory, "ratelimit.json")
    with FileLock(path + ".lock"):
        data = read_json(path, {})
        sent = [stamp for stamp in data.get("sent", []) if now - stamp < 3600]
        last = data.get("last", {})
        if now - last.get(key, 0) < per_key_interval_s:
            return True
        if len(sent) >= max_per_hour:
            return True
        sent.append(now)
        last[key] = now
        write_json_atomic(path, {"sent": sent, "last": last})
    return False


# ---------------------------------------------------------------- rendering

def split_urgency(text, default="normal"):
    """Return (urgency, text without the leading [tag])."""
    text = text or ""
    match = URGENCY_TAG.match(text)
    if match is None:
        return default, text.strip()
    return match.group(1).lower(), text[match.end():].strip()


def option_key(index):
    return "abcdefghijklmnopqrstuvwxyz"[index] if index < 26 else str(index + 1)


def truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def redact(text):
    text = text or ""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda match: match.group(1) + "***", text)
    return text


def render_report(report, transport="telegram"):
    """Render a decision report dict to (title, body, urgency) for a transport.

    report = {"urgency", "title", "context": str or [str], "options": [{"label",
              "explanation", "consequence"}], "recommendation": {"option", "why"},
              "footer"}
    Plain text for telegram/stdout; markdown-lite (bold labels) for ntfy.
    """
    urgency = (report.get("urgency") or "normal").lower()
    if urgency not in URGENCIES:
        urgency = "normal"
    markdown = transport == "ntfy"
    title = truncate("[%s] %s" % (urgency.upper(), report.get("title") or "Decision needed"), 200)
    lines = []
    context = report.get("context") or []
    if isinstance(context, str):
        context = [line for line in context.splitlines() if line.strip()]
    lines.extend(line.strip() for line in context[:3])
    options = report.get("options") or []
    if options:
        lines.append("")
    for index, option in enumerate(options):
        key = option.get("key") or option_key(index)
        label = (option.get("label") or "").strip()
        head = "%s) %s" % (key, label)
        lines.append("**%s**" % head if markdown else head)
        if option.get("explanation"):
            lines.append("   " + option["explanation"].strip())
        if option.get("consequence"):
            lines.append("   -> " + option["consequence"].strip())
    recommendation = report.get("recommendation") or {}
    if recommendation.get("option"):
        lines.append("")
        why = recommendation.get("why")
        text = "Recommended: %s" % recommendation["option"]
        if why:
            text += " - " + why.strip()
        lines.append(text)
    if report.get("footer"):
        lines.append("")
        lines.append(report["footer"])
    body = "\n".join(lines).strip()
    if transport != "ntfy":
        body = title + "\n" + body if body else title
    return title, truncate(body, MAX_TEXT), urgency


def question_to_report(question, index=0, total=1, label=None):
    """Turn one AskUserQuestion question into a report dict."""
    urgency, text = split_urgency(question.get("question", ""), default="blocking")
    header = (question.get("header") or "Question").strip()
    if total > 1:
        header = "%s (%d/%d)" % (header, index + 1, total)
    if label:
        header = "%s - %s" % (header, label)
    options = []
    recommended = None
    for option_index, option in enumerate(question.get("options") or []):
        option_label = option.get("label", "")
        if "(recommended)" in option_label.lower() and recommended is None:
            recommended = option_key(option_index)
        options.append({"label": option_label, "explanation": option.get("description", "")})
    footer = "Tap a button, or reply to this message with a letter."
    if question.get("multiSelect"):
        footer = "Several allowed: tap options, then Done. Or reply with letters, e.g. \"a c\"."
    report = {"urgency": urgency, "title": header, "context": text, "options": options, "footer": footer}
    if recommended:
        report["recommendation"] = {"option": recommended}
    return report


def input_preview(tool_name, tool_input, limit=300):
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Bash" and "command" in tool_input:
        text = str(tool_input.get("command"))
    elif "file_path" in tool_input:
        text = str(tool_input.get("file_path"))
    elif "url" in tool_input:
        text = str(tool_input.get("url"))
    elif "notebook_path" in tool_input:
        text = str(tool_input.get("notebook_path"))
    else:
        text = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    return truncate(redact(text), limit)


def render_permission(tool_name, tool_input, description, project, label, timeout_s):
    lines = ["[BLOCKING] Permission needed - %s" % label, "Project: %s" % project, "Tool: %s" % tool_name,
             "Input: %s" % input_preview(tool_name, tool_input)]
    if description:
        lines.append("Why: %s" % truncate(redact(description), 200))
    lines.append("")
    lines.append("Allow or Deny within %s. No answer: the prompt waits at the terminal." % human_seconds(timeout_s))
    return "\n".join(lines)


def human_seconds(seconds):
    seconds = int(seconds)
    if seconds >= 120:
        return "%d min" % (seconds // 60)
    return "%d s" % seconds


def machine_label(config):
    if config.get("label"):
        return str(config["label"])
    try:
        return os.uname().nodename.split(".")[0]
    except Exception:
        return "this machine"


# ---------------------------------------------------------------- HTTP

def check_base_url(url):
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme == "https" and parsed.hostname:
        return url.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return url.rstrip("/")
    raise DeliveryError("base URL must be https (or http on loopback)")


def http_request(url, body=None, headers=None, timeout=10):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(1000000)
    except urllib.error.HTTPError as error:
        raise DeliveryError("HTTP %s" % error.code)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise DeliveryError("network error: %s" % type(error).__name__)


def http_json(url, payload, timeout=10):
    raw = http_request(url, json.dumps(payload).encode("utf-8"), {"Content-Type": "application/json"}, timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise DeliveryError("malformed JSON response")


# ---------------------------------------------------------------- transports

class StdoutTransport:
    """Testing transport: writes messages to $SUSHIDELIVERY_STDOUT_FILE, or stderr in hooks."""

    name = "stdout"

    def __init__(self, stream=None):
        self.stream = stream

    def send(self, title, body, urgency="normal", buttons=None):
        text = "--- sushidelivery [%s] %s\n%s\n" % (urgency, title, body)
        if buttons:
            text += "buttons: %s\n" % " | ".join(label for label, _ in buttons)
        path = os.environ.get("SUSHIDELIVERY_STDOUT_FILE")
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text)
        else:
            (self.stream or sys.stderr).write(text)
        return None


class NtfyTransport:
    """Send-only push to an ntfy server/topic you configure."""

    name = "ntfy"

    def __init__(self, config, token=None):
        ntfy = config.get("ntfy") or {}
        self.server = check_base_url(ntfy.get("server") or "https://ntfy.sh")
        topic = ntfy.get("topic")
        if not topic or not re.match(r"^[-_A-Za-z0-9]{1,64}$", str(topic)):
            raise DeliveryError("ntfy topic missing or invalid")
        self.topic = str(topic)
        self.token = token

    @staticmethod
    def header_value(text):
        try:
            text.encode("ascii")
            return text
        except UnicodeEncodeError:
            return "=?UTF-8?B?%s?=" % base64.b64encode(text.encode("utf-8")).decode("ascii")

    def send(self, title, body, urgency="normal", buttons=None):
        headers = {
            "Title": self.header_value(truncate(title.replace("\n", " "), 200)),
            "Priority": str(NTFY_PRIORITY.get(urgency, 3)),
            "Tags": "sushidelivery,%s" % urgency,
            "Markdown": "yes",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        http_request("%s/%s" % (self.server, self.topic), body.encode("utf-8"), headers, timeout=8)
        return None


class TelegramTransport:
    """Telegram Bot API: sendMessage with inline buttons, getUpdates for replies.

    Only updates from an allowlisted user id in the configured chat are accepted.
    Concurrent waiters share one poller (file lock) and an inbox file of accepted updates.
    """

    name = "telegram"

    def __init__(self, config, token, directory=None, project_root=None):
        telegram = config.get("telegram") or {}
        if not token:
            raise DeliveryError("telegram token missing")
        self.token = token
        self.base = check_base_url(telegram.get("api_base") or "https://api.telegram.org")
        allowed = telegram.get("allowed_user_ids") or []
        try:
            self.allowed = set(int(value) for value in allowed)
        except (TypeError, ValueError):
            raise DeliveryError("allowed_user_ids must be integers")
        chat_id = telegram.get("chat_id")
        if chat_id is None and len(self.allowed) == 1:
            chat_id = next(iter(self.allowed))  # private chat id equals the user id
        try:
            self.chat_id = int(chat_id) if chat_id is not None else None
        except (TypeError, ValueError):
            raise DeliveryError("chat_id must be an integer")
        self.directory = directory or state_dir()
        self.project_root = project_root

    @property
    def can_receive(self):
        return bool(self.allowed) and self.chat_id is not None

    def call(self, method, payload, timeout=10):
        result = http_json("%s/bot%s/%s" % (self.base, self.token, method), payload, timeout)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise DeliveryError("telegram %s failed" % method)
        return result.get("result")

    def send(self, title, body, urgency="normal", buttons=None):
        if self.chat_id is None:
            raise DeliveryError("telegram chat_id missing")
        payload = {"chat_id": self.chat_id, "text": body, "disable_notification": urgency == "low"}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": truncate(label, 60), "callback_data": data}]
                                                           for label, data in buttons]}
        result = self.call("sendMessage", payload)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise DeliveryError("telegram sendMessage returned no message_id")
        return result["message_id"]

    def best_effort(self, method, payload):
        try:
            self.call(method, payload, timeout=5)
        except Exception as error:
            log(self.project_root, "telegram %s ignored: %s" % (method, error))

    def finish(self, message_id, text, callback_id=None):
        if callback_id:
            self.best_effort("answerCallbackQuery", {"callback_query_id": callback_id, "text": truncate(text, 180)})
        if message_id is not None:
            self.best_effort("editMessageReplyMarkup", {"chat_id": self.chat_id, "message_id": message_id,
                                                        "reply_markup": {"inline_keyboard": []}})
            self.best_effort("sendMessage", {"chat_id": self.chat_id, "text": truncate(text, 500),
                                             "reply_to_message_id": message_id, "disable_notification": True})

    # -- authentication

    def authenticate(self, update):
        """Return an inbox entry for an update from the owner, else None."""
        if not isinstance(update, dict):
            return None
        if isinstance(update.get("callback_query"), dict):
            query = update["callback_query"]
            sender = query.get("from")
            message = query.get("message")
            kind = "callback"
        elif isinstance(update.get("message"), dict):
            message = update["message"]
            sender = message.get("from")
            query = None
            kind = "text"
        else:
            return None
        if not isinstance(sender, dict) or not isinstance(message, dict):
            return None
        sender_id = sender.get("id")
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if sender.get("is_bot") or not isinstance(sender_id, int) or isinstance(sender_id, bool):
            return None
        if sender_id not in self.allowed or chat.get("id") != self.chat_id:
            return None
        entry = {"update_id": update.get("update_id"), "t": time.time(), "kind": kind, "from": sender_id}
        if kind == "callback":
            data = query.get("data")
            if not isinstance(data, str):
                return None
            entry.update({"data": data, "callback_id": query.get("id"), "message_id": message.get("message_id")})
        else:
            text = message.get("text")
            reply = message.get("reply_to_message") if isinstance(message.get("reply_to_message"), dict) else {}
            if not isinstance(text, str):
                return None
            entry.update({"text": text, "reply_to": reply.get("message_id")})
        return entry

    # -- polling

    def inbox_path(self):
        return os.path.join(self.directory, "telegram_inbox.jsonl")

    def poll_once(self, long_poll_s):
        """Fetch new updates if no other process is polling; store the owner's in the inbox."""
        lock_path = os.path.join(self.directory, "telegram_poll.lock")
        with FileLock(lock_path, blocking=False) as lock:
            if not lock.acquired:
                time.sleep(0.5)
                return
            offset_path = os.path.join(self.directory, "telegram_offset.json")
            offset = read_json(offset_path, {}).get("offset")
            payload = {"timeout": int(max(0, long_poll_s)), "allowed_updates": ["message", "callback_query"]}
            if isinstance(offset, int):
                payload["offset"] = offset
            updates = self.call("getUpdates", payload, timeout=long_poll_s + 10)
            if not isinstance(updates, list):
                raise DeliveryError("telegram getUpdates returned no list")
            accepted = []
            new_offset = offset
            for update in updates:
                if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                    continue
                new_offset = max(new_offset or 0, update["update_id"] + 1)
                entry = self.authenticate(update)
                if entry is None:
                    log(self.project_root, "telegram: ignored update %s from a sender or chat not on the allowlist"
                        % update.get("update_id"))
                    continue
                accepted.append(entry)
            if accepted:
                self.append_inbox(accepted)
            if new_offset != offset:
                write_json_atomic(offset_path, {"offset": new_offset})

    def append_inbox(self, entries):
        path = self.inbox_path()
        now = time.time()
        kept = [entry for entry in self.read_inbox() if now - entry.get("t", 0) < 7200]
        kept.extend(entries)
        temporary = "%s.%d.tmp" % (path, os.getpid())
        with open(temporary, "w", encoding="utf-8") as handle:
            for entry in kept:
                handle.write(json.dumps(entry) + "\n")
        os.replace(temporary, path)

    def read_inbox(self):
        entries = []
        try:
            with open(self.inbox_path(), encoding="utf-8") as handle:
                for line in handle:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict):
                        entries.append(entry)
        except OSError:
            pass
        return entries

    def wait(self, deadline, handle_entry, clock=time.time):
        """Poll until handle_entry(entry) returns a non-None result or the deadline passes."""
        seen = set()
        while True:
            for entry in self.read_inbox():
                key = entry.get("update_id")
                if key in seen:
                    continue
                seen.add(key)
                result = handle_entry(entry)
                if result is not None:
                    return result
            remaining = deadline - clock()
            if remaining <= 0:
                return None
            started = clock()
            self.poll_once(min(LONG_POLL_MAX_S, max(0, int(remaining) - 1)))
            if clock() - started < 0.3:
                time.sleep(0.3)


def make_transport(config, project_root=None, stream=None):
    name = config.get("transport")
    if name == "stdout":
        return StdoutTransport(stream)
    if name == "ntfy":
        return NtfyTransport(config, get_secret("ntfy_token"))
    if name == "telegram":
        return TelegramTransport(config, get_secret("telegram_bot_token"), project_root=project_root)
    if name in (None, "", "off"):
        return None
    raise DeliveryError("unknown transport %r" % (name,))


# ---------------------------------------------------------------- relays

def relay_wait_seconds(configured, command):
    ceiling = HOOK_TIMEOUT_S[command] - WAIT_MARGIN_S
    try:
        value = float(configured)
    except (TypeError, ValueError):
        value = 240.0
    return max(1.0, min(value, float(ceiling)))


def ask_permission(transport, tool_name, tool_input, description, project, label, wait_s, clock=time.time):
    """Send the request and return "allow", "deny", or None (no decision)."""
    start = clock()
    nonce = secrets.token_hex(6)
    text = render_permission(tool_name, tool_input, description, project, label, wait_s)
    message_id = transport.send("Permission needed", text, "blocking",
                                [("Allow", "sd:%s:allow" % nonce), ("Deny", "sd:%s:deny" % nonce)])

    def handle(entry):
        decision = None
        if entry.get("kind") == "callback" and entry.get("data", "").startswith("sd:%s:" % nonce):
            decision = entry["data"].rsplit(":", 1)[1]
        elif entry.get("kind") == "text" and entry.get("reply_to") == message_id:
            word = entry.get("text", "").strip().lower()
            decision = {"allow": "allow", "yes": "allow", "y": "allow", "deny": "deny", "no": "deny",
                        "n": "deny"}.get(word)
        if decision not in ("allow", "deny"):
            return None
        transport.finish(message_id, "%s. Sent to Claude." % ("Allowed" if decision == "allow" else "Denied"),
                         entry.get("callback_id"))
        return decision

    decision = transport.wait(start + wait_s, handle, clock=clock)
    if decision is None:
        transport.finish(message_id, "Timed out. The prompt is waiting at the terminal.")
    return decision


def parse_letters(text, count):
    tokens = [token for token in re.split(r"[\s,;]+", text.strip().lower()) if token]
    if not tokens:
        return None
    indices = []
    for token in tokens:
        token = token.rstrip(").")
        if len(token) == 1 and "a" <= token <= "z" and ord(token) - 97 < count:
            index = ord(token) - 97
        elif token.isdigit() and 1 <= int(token) <= count:
            index = int(token) - 1
        else:
            return None
        if index not in indices:
            indices.append(index)
    return indices


def ask_question(transport, question, index, total, label, deadline, allow_free_text=True, clock=time.time):
    """Relay one question. Return the answer string or None."""
    options = question.get("options") or []
    labels = [str(option.get("label", "")) for option in options]
    multi = bool(question.get("multiSelect"))
    nonce = secrets.token_hex(6)
    title, body, urgency = render_report(question_to_report(question, index, total, label), "telegram")
    buttons = [("%s) %s" % (option_key(i), truncate(text, 40)), "sd:%s:%d" % (nonce, i)) for i, text in enumerate(labels)]
    if multi:
        buttons.append(("Done", "sd:%s:done" % nonce))
    message_id = transport.send(title, body, urgency, buttons)
    selected = []

    def handle(entry):
        answer = None
        callback_id = entry.get("callback_id")
        if entry.get("kind") == "callback" and entry.get("data", "").startswith("sd:%s:" % nonce):
            value = entry["data"].rsplit(":", 1)[1]
            if value == "done":
                if multi and selected:
                    answer = ", ".join(labels[i] for i in sorted(selected))
            elif value.isdigit() and int(value) < len(labels):
                choice = int(value)
                if not multi:
                    answer = labels[choice]
                else:
                    if choice in selected:
                        selected.remove(choice)
                    else:
                        selected.append(choice)
                    marks = ", ".join(option_key(i) for i in sorted(selected)) or "none"
                    transport.best_effort("answerCallbackQuery", {"callback_query_id": callback_id,
                                                                  "text": "Selected: %s" % marks})
        elif entry.get("kind") == "text" and entry.get("reply_to") == message_id:
            text = entry.get("text", "").strip()
            indices = parse_letters(text, len(labels))
            if indices and (multi or len(indices) == 1):
                answer = ", ".join(labels[i] for i in sorted(indices))
            elif text and allow_free_text:
                answer = truncate(text, 500)
        if answer is None:
            return None
        transport.finish(message_id, "Answer sent to Claude: %s" % truncate(answer, 200), callback_id)
        return answer

    answer = transport.wait(deadline, handle, clock=clock)
    if answer is None:
        transport.finish(message_id, "Timed out. The question is waiting at the terminal.")
    return answer


# ---------------------------------------------------------------- hooks

def read_hook_input():
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise DeliveryError("hook input is not an object")
    return data


def hook_context(hook_input):
    project_root = find_project_root(os.environ.get("CLAUDE_PROJECT_DIR") or hook_input.get("cwd"))
    return project_root, load_config(project_root)


def project_name(project_root, hook_input):
    return os.path.basename(project_root or hook_input.get("cwd") or "") or "unknown"


def command_hook_notify(hook_input, idle_probe=hid_idle_seconds):
    project_root, config = hook_context(hook_input)
    notify = config.get("notify") or {}
    if not notify.get("enabled") or not config.get("transport"):
        return None
    kind = hook_input.get("notification_type") or "unknown"
    if kind not in (notify.get("types") or []):
        return None
    if notify.get("skip_when_present", True) and owner_present(config, idle_probe):
        log(project_root, "notify %s skipped: owner present" % kind)
        return None
    if rate_limited("notify:" + kind, float(notify.get("per_type_interval_s", 120)),
                    int(notify.get("max_per_hour", 20))):
        log(project_root, "notify %s skipped: rate limit" % kind)
        return None
    urgency = NOTIFY_URGENCY.get(kind, "normal")
    label = machine_label(config)
    title = "[%s] %s - %s" % (urgency.upper(), hook_input.get("title") or "Claude needs you", label)
    message = truncate(redact(str(hook_input.get("message") or "")), 500)
    body = "%s\nProject: %s\n%s" % (title, project_name(project_root, hook_input), message)
    transport = make_transport(config, project_root)
    if transport is None:
        return None
    if isinstance(transport, NtfyTransport):
        body = "Project: %s\n%s" % (project_name(project_root, hook_input), message)
    transport.send(title, body, urgency)
    log(project_root, "notify %s sent via %s" % (kind, transport.name))
    return None


def receiving_transport(config, project_root):
    """The relay needs a transport that can receive authenticated replies (Telegram)."""
    if config.get("transport") != "telegram":
        log(project_root, "relay skipped: needs transport telegram")
        return None
    transport = make_transport(config, project_root)
    if not transport.can_receive:
        log(project_root, "relay skipped: telegram allowed_user_ids/chat_id not set")
        return None
    return transport


def command_hook_permission(hook_input, idle_probe=hid_idle_seconds, transport=None):
    project_root, config = hook_context(hook_input)
    relay = config.get("permission_relay") or {}
    if not relay.get("enabled"):
        return None
    tool_name = hook_input.get("tool_name")
    tools = relay.get("tools") or []
    if not isinstance(tool_name, str) or tool_name not in tools:
        return None
    tool_input = hook_input.get("tool_input") if isinstance(hook_input.get("tool_input"), dict) else {}
    pattern = relay.get("input_regex")
    if pattern and re.search(pattern, input_preview(tool_name, tool_input, limit=2000)) is None:
        return None
    if owner_present(config, idle_probe):
        log(project_root, "permission relay skipped: owner present")
        return None
    transport = transport or receiving_transport(config, project_root)
    if transport is None:
        return None
    wait_s = relay_wait_seconds(relay.get("timeout_s"), "hook-permission")
    decision = ask_permission(transport, tool_name, tool_input, tool_input.get("description"),
                              project_name(project_root, hook_input), machine_label(config), wait_s)
    log(project_root, "permission relay %s: %s" % (tool_name, decision or "no decision"))
    if decision == "allow":
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
    if decision == "deny":
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
            "behavior": "deny", "message": "The owner denied this from their phone (SushiDelivery)."}}}
    return None


def command_hook_question(hook_input, idle_probe=hid_idle_seconds, transport=None, clock=time.time):
    start = clock()
    project_root, config = hook_context(hook_input)
    relay = config.get("question_relay") or {}
    if not relay.get("enabled") or hook_input.get("tool_name") != "AskUserQuestion":
        return None
    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        return None
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("question"), str):
            return None
        options = question.get("options")
        if not isinstance(options, list) or not options or not all(isinstance(o, dict) for o in options):
            return None
    if owner_present(config, idle_probe):
        log(project_root, "question relay skipped: owner present")
        return None
    transport = transport or receiving_transport(config, project_root)
    if transport is None:
        return None
    deadline = start + relay_wait_seconds(relay.get("timeout_s"), "hook-question")
    answers = {}
    label = machine_label(config)
    for index, question in enumerate(questions):
        answer = ask_question(transport, question, index, len(questions), label, deadline,
                              relay.get("allow_free_text", True), clock=clock)
        if answer is None:
            log(project_root, "question relay: no answer in time")
            return None
        answers[question["question"]] = answer
    updated = dict(tool_input)
    updated["answers"] = answers
    log(project_root, "question relay: answered %d question(s)" % len(answers))
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "Answered by the owner from their phone (SushiDelivery).",
        "updatedInput": updated,
    }}


HOOKS = {
    "hook-notify": command_hook_notify,
    "hook-permission": command_hook_permission,
    "hook-question": command_hook_question,
}


def run_hook(command):
    """Run a hook. Never raises; prints JSON only for a real decision."""
    project_root = None
    try:
        hook_input = read_hook_input()
        project_root = find_project_root(os.environ.get("CLAUDE_PROJECT_DIR") or hook_input.get("cwd"))
        output = HOOKS[command](hook_input)
        if output is not None:
            sys.stdout.write(json.dumps(output))
            sys.stdout.flush()
    except Exception as error:  # a hook must never break the session: no output = no decision
        log(project_root, "%s error: %s" % (command, scrub(repr(error))))
    return 0


# ---------------------------------------------------------------- CLI

def option_value(arguments, name, default=None):
    if name in arguments:
        position = arguments.index(name)
        if position + 1 < len(arguments):
            return arguments[position + 1]
    return default


def command_render(arguments):
    report = json.loads(sys.stdin.read())
    title, body, urgency = render_report(report, option_value(arguments, "--transport", "telegram"))
    print("urgency: %s\ntitle: %s\n\n%s" % (urgency, title, body))
    return 0


def command_send(arguments, text=None):
    project_root = find_project_root(os.getcwd())
    config = load_config(project_root)
    transport = make_transport(config, project_root, stream=sys.stdout)
    if transport is None:
        print("sushidelivery: no transport configured (everything is off).", file=sys.stderr)
        return 1
    urgency = option_value(arguments, "--urgency", "normal")
    if urgency not in URGENCIES:
        urgency = "normal"
    title = "[%s] %s" % (urgency.upper(), option_value(arguments, "--title", "Message from Claude"))
    body = truncate(text if text is not None else sys.stdin.read(), MAX_TEXT)
    if not isinstance(transport, NtfyTransport):
        body = title + "\n" + body
    try:
        transport.send(title, body, urgency)
    except DeliveryError as error:
        print("sushidelivery: send failed: %s" % scrub(error), file=sys.stderr)
        return 1
    print("sent via %s" % transport.name)
    return 0


def command_telegram_ids():
    project_root = find_project_root(os.getcwd())
    config = load_config(project_root)
    token = get_secret("telegram_bot_token")
    if not token:
        print("Set SUSHIDELIVERY_TELEGRAM_TOKEN in this shell first.", file=sys.stderr)
        return 1
    base = check_base_url((config.get("telegram") or {}).get("api_base") or "https://api.telegram.org")
    try:
        result = http_json("%s/bot%s/getUpdates" % (base, token), {"timeout": 0}, timeout=15)
    except DeliveryError as error:
        print("getUpdates failed: %s" % scrub(error), file=sys.stderr)
        return 1
    if not isinstance(result, dict) or result.get("ok") is not True:
        print("getUpdates failed (is a webhook or another poller using this bot?)", file=sys.stderr)
        return 1
    for update in result.get("result") or []:
        message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        sender = update.get("message", {}).get("from") or (update.get("callback_query") or {}).get("from") or {}
        print("user id %s (%s)  chat id %s" % (sender.get("id"), sender.get("username") or sender.get("first_name"),
                                               (message.get("chat") or {}).get("id")))
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    command = sys.argv[1]
    if command in HOOKS:
        return run_hook(command)
    if command == "render":
        return command_render(sys.argv[2:])
    if command == "send":
        return command_send(sys.argv[2:])
    if command == "test-send":
        return command_send(["--urgency", "low", "--title", "SushiDelivery test"],
                            text="If you can read this on your phone, the transport works.")
    if command == "telegram-ids":
        return command_telegram_ids()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
