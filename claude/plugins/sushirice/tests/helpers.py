"""Shared test helpers: an isolated project, lock dir, and config per test."""

import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "sushirice.py")


def load_module():
    spec = importlib.util.spec_from_file_location("sushirice", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sushirice = load_module()

# Every test runs with its own HOME, so a real ~/.sushi/sushirice.json (the user layer) never leaks in.
ISOLATED_HOME = tempfile.mkdtemp(prefix="sushirice-home-")
os.environ["HOME"] = ISOLATED_HOME
atexit.register(shutil.rmtree, ISOLATED_HOME, True)


class SandboxCase(unittest.TestCase):
    """Each test gets its own project root (with .sushi/), lock dir, and config file."""

    base_config = {
        "max_slots": 2,
        "reserved_critical_slots": 0,
        "poll_s": 0.05,
        "report_wait_s": 60,
        "start_spacing_s": 0,
        "headroom": {"enabled": False},
        "watchdog": {"interval_s": 0.2},
    }

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sushirice-test-")
        self.project = os.path.join(self.root, "project")
        os.makedirs(os.path.join(self.project, ".sushi"))
        self.locks = os.path.join(self.root, "locks")
        self.config_path = os.path.join(self.project, ".sushi", "sushirice.json")
        self.write_config({})
        self.saved_env = dict(os.environ)
        os.environ["SUSHIRICE_LOCK_DIR"] = self.locks
        os.environ.pop("SUSHIRICE_CONFIG", None)
        for variable in ("SUSHIRICE_SLOT_HELD", "GUI_FORMS_BUILD_SLOT_HELD", "SUSHIRICE_PRIORITY",
                         "GUI_FORMS_BUILD_PRIORITY", "SUSHI_ROLE"):
            os.environ.pop(variable, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        shutil.rmtree(self.root, ignore_errors=True)

    def write_config(self, overrides):
        config = json.loads(json.dumps(self.base_config))
        sushirice.merge(config, overrides)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)

    def config(self):
        return sushirice.load_config(self.project)

    def run_cli(self, *arguments, env=None, timeout=30, input_text=None):
        environment = dict(os.environ)
        environment.update(env or {})
        return subprocess.run([sys.executable, SCRIPT] + list(arguments), cwd=self.project, env=environment,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                              input=input_text.encode("utf-8") if input_text is not None else None)

    def runs(self):
        path = os.path.join(self.project, ".sushi", "state", "sushirice", "runs.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def log_text(self):
        path = os.path.join(self.project, ".sushi", "logs", "sushirice.log")
        if not os.path.isfile(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()
