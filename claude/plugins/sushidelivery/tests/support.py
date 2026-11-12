"""Shared test setup: an isolated project, config, state dir and env. No real services."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from mock_servers import TOKEN

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(PLUGIN_ROOT, "scripts", "delivery.py")


def load_delivery():
    spec = importlib.util.spec_from_file_location("sushidelivery_delivery", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


delivery = load_delivery()


class IsolatedCase(unittest.TestCase):
    """Each test gets its own project dir, config file and state dir, and a clean env."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sushidelivery-test-")
        self.project = os.path.join(self.tmp, "project")
        os.makedirs(os.path.join(self.project, ".sushi"))
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self.config_file = os.path.join(self.project, ".sushi", "sushidelivery.json")
        self.out_file = os.path.join(self.tmp, "stdout-transport.txt")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("CLAUDE_PLUGIN_OPTION_", "SUSHIDELIVERY_"))
               and key not in ("CLAUDE_CLIENT_PRESENCE_FILE", "CLAUDE_PLUGIN_DATA")}
        env.update({
            "HOME": self.tmp,
            "CLAUDE_PROJECT_DIR": self.project,
            "SUSHIDELIVERY_CONFIG": self.config_file,
            "SUSHIDELIVERY_STATE_DIR": self.state,
            "SUSHIDELIVERY_STDOUT_FILE": self.out_file,
        })
        self.env = env
        self.env_patch = mock.patch.dict(os.environ, env, clear=True)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, config):
        with open(self.config_file, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

    def telegram_config(self, base, **overrides):
        config = {
            "transport": "telegram",
            "label": "testbox",
            "presence": {"mode": "off"},
            "telegram": {"api_base": base, "chat_id": 111, "allowed_user_ids": [111]},
            "permission_relay": {"enabled": True, "tools": ["Bash"], "timeout_s": 3},
            "question_relay": {"enabled": True, "timeout_s": 3},
        }
        for key, value in overrides.items():
            if isinstance(config.get(key), dict) and isinstance(value, dict):
                config[key].update(value)
            else:
                config[key] = value
        return config

    def use_token(self):
        os.environ["CLAUDE_PLUGIN_OPTION_TELEGRAM_BOT_TOKEN"] = TOKEN
        self.env["CLAUDE_PLUGIN_OPTION_TELEGRAM_BOT_TOKEN"] = TOKEN

    def run_hook(self, command, hook_input, timeout=30):
        """Run the hook as Claude Code would: JSON on stdin, decision JSON (or nothing) on stdout."""
        completed = subprocess.run([sys.executable, SCRIPT, command], input=json.dumps(hook_input),
                                   capture_output=True, text=True, env=self.env, timeout=timeout,
                                   cwd=self.project)
        return completed

    def read_log(self):
        path = os.path.join(self.project, ".sushi", "logs", "sushidelivery.log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()
