"""Slot acquisition, contention, nesting, priority, stale cleanup, and run records."""

import io
import json
import os
import subprocess
import sys
import time
import unittest

from helpers import SandboxCase, sushirice


def dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def hold_slot(locks, index, pid):
    directory = os.path.join(locks, "slots", "slot%d" % index)
    os.makedirs(directory)
    with open(os.path.join(directory, "pid"), "w") as handle:
        handle.write(str(pid))
    return directory


class SlotCliTests(SandboxCase):
    def test_runs_command_and_records(self):
        result = self.run_cli("slot", "--", sys.executable, "-c", "import sys; sys.exit(3)")
        self.assertEqual(result.returncode, 3)
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["exit"], 3)
        self.assertIn(runs[0]["slot"], (0, 1))
        self.assertIn("sys.exit(3)", runs[0]["cmd"])
        self.assertGreaterEqual(runs[0]["duration_s"], 0)
        self.assertFalse(os.listdir(os.path.join(self.locks, "slots")), "slot not released")

    def test_child_sees_nesting_variables(self):
        script = "import os; print(os.environ.get('SUSHIRICE_SLOT_HELD'), os.environ.get('GUI_FORMS_BUILD_SLOT_HELD'))"
        result = self.run_cli("slot", "--", sys.executable, "-c", script)
        self.assertEqual(result.stdout.decode().split(), ["1", "1"])

    def test_nested_call_skips_slots(self):
        self.write_config({"max_slots": 1})
        hold_slot(self.locks, 0, os.getpid())  # this test process is alive: slot is busy
        for variable in ("SUSHIRICE_SLOT_HELD", "GUI_FORMS_BUILD_SLOT_HELD"):
            result = self.run_cli("slot", "--timeout", "0.3", "--", sys.executable, "-c", "print('ran')",
                                  env={variable: "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b"ran", result.stdout)
        self.assertTrue(all(run.get("nested") for run in self.runs()))

    def test_timeout_exits_75(self):
        self.write_config({"max_slots": 1})
        hold_slot(self.locks, 0, os.getpid())
        started = time.time()
        result = self.run_cli("slot", "--timeout", "0.4", "--", sys.executable, "-c", "print('ran')")
        self.assertEqual(result.returncode, 75)
        self.assertNotIn(b"ran", result.stdout)
        self.assertIn(b"no slot", result.stderr)
        self.assertLess(time.time() - started, 10)
        self.assertTrue(self.runs()[-1]["timed_out"])

    def test_contention_between_two_runs(self):
        self.write_config({"max_slots": 1})
        environment = dict(os.environ)
        first = subprocess.Popen([sys.executable, sushirice.SCRIPT_PATH, "slot", "--", sys.executable, "-c",
                                  "import time; time.sleep(1.5)"], cwd=self.project, env=environment)
        try:
            deadline = time.time() + 10
            while not os.path.isdir(os.path.join(self.locks, "slots", "slot0")) and time.time() < deadline:
                time.sleep(0.05)
            second = self.run_cli("slot", "--timeout", "0.3", "--", sys.executable, "-c", "pass")
            self.assertEqual(second.returncode, 75)
            third = self.run_cli("slot", "--timeout", "15", "--", sys.executable, "-c", "print('after')")
            self.assertEqual(third.returncode, 0)
            self.assertIn(b"after", third.stdout)
        finally:
            first.wait(timeout=20)
        self.assertEqual(first.returncode, 0)

    def test_stale_lock_is_cleaned(self):
        self.write_config({"max_slots": 1})
        stale = hold_slot(self.locks, 0, dead_pid())
        result = self.run_cli("slot", "--timeout", "2", "--", sys.executable, "-c", "print('ok')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.isdir(stale))
        self.assertIn("removed stale lock", self.log_text())

    def test_reserved_slot_for_critical(self):
        self.write_config({"max_slots": 2, "reserved_critical_slots": 1})
        hold_slot(self.locks, 1, os.getpid())
        normal = self.run_cli("slot", "--timeout", "0.3", "--", sys.executable, "-c", "pass")
        self.assertEqual(normal.returncode, 75)
        critical = self.run_cli("slot", "--priority", "critical", "--timeout", "1", "--", sys.executable, "-c",
                                "import os; print(os.environ['SUSHIRICE_SLOT'])")
        self.assertEqual(critical.returncode, 0, critical.stderr)
        self.assertEqual(critical.stdout.decode().strip(), "0")
        via_env = self.run_cli("slot", "--timeout", "1", "--", sys.executable, "-c", "pass",
                               env={"GUI_FORMS_BUILD_PRIORITY": "critical"})
        self.assertEqual(via_env.returncode, 0)

    def test_foreign_slot_dirs_count(self):
        foreign = os.path.join(self.root, "m4ws", ".m4ws.slot1")
        os.makedirs(foreign)
        with open(os.path.join(foreign, "pid"), "w") as handle:
            handle.write(str(os.getpid()))
        self.write_config({"max_slots": 1, "foreign_slot_dirs": [os.path.join(self.root, "m4ws", ".m4ws.slot*")]})
        self.assertEqual(self.run_cli("slot", "--timeout", "0.3", "--", "true").returncode, 75)
        with open(os.path.join(foreign, "pid"), "w") as handle:
            handle.write(str(dead_pid()))
        self.assertEqual(self.run_cli("slot", "--timeout", "1", "--", "true").returncode, 0)

    def test_missing_command_127_and_released(self):
        result = self.run_cli("slot", "--", "/nonexistent/sushirice-no-such-tool")
        self.assertEqual(result.returncode, 127)
        self.assertFalse(os.listdir(os.path.join(self.locks, "slots")))

    def test_signal_exit_code(self):
        result = self.run_cli("slot", "--", "sh", "-c", "kill -TERM $$")
        self.assertEqual(result.returncode, 128 + 15)

    def test_build_dir_recorded(self):
        self.run_cli("slot", "--", "true", "--build", "out")
        self.assertEqual(os.path.realpath(self.runs()[-1]["build_dir"]),
                         os.path.join(os.path.realpath(self.project), "out"))

    def test_usage_without_command(self):
        self.assertEqual(self.run_cli("slot").returncode, 2)


class AcquireInProcessTests(SandboxCase):
    """Headroom and spacing gates, with a fake clock and fake readings."""

    def setUp(self):
        super().setUp()
        self.clock = [1000.0]

    def now(self):
        return self.clock[0]

    def sleep(self, seconds):
        self.clock[0] += seconds

    def test_headroom_blocks_until_timeout(self):
        self.write_config({"headroom": {"enabled": True}})
        busy = {"cores": 4, "load": 8.0, "free_percent": 60, "pressure": "normal", "compilers": 0}
        err = io.StringIO()
        index, _, waited = sushirice.acquire_slot(self.config(), False, 2.0, "x", self.project,
                                                  measure=lambda: busy, now=self.now, sleep=self.sleep, err=err)
        self.assertIsNone(index)
        self.assertIn("headroom: load 8.0", err.getvalue())
        self.assertGreaterEqual(waited, 2.0)

    def test_headroom_critical_skips_load(self):
        self.write_config({"headroom": {"enabled": True}})
        busy = {"cores": 4, "load": 8.0, "free_percent": 60, "pressure": "normal", "compilers": 9}
        index, directory, _ = sushirice.acquire_slot(self.config(), True, 2.0, "x", self.project,
                                                     measure=lambda: busy, now=self.now, sleep=self.sleep,
                                                     err=io.StringIO())
        self.assertEqual(index, 0)
        with open(os.path.join(directory, "owner.json"), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["pid"], os.getpid())
        sushirice.remove_tree(directory)

    def test_headroom_clears(self):
        self.write_config({"headroom": {"enabled": True}})
        readings = iter([{"cores": 4, "load": 8.0, "free_percent": 60, "pressure": "normal", "compilers": 0}] * 3
                        + [{"cores": 4, "load": 1.0, "free_percent": 60, "pressure": "normal", "compilers": 0}] * 5)
        index, directory, waited = sushirice.acquire_slot(self.config(), False, 100.0, "x", self.project,
                                                          measure=lambda: next(readings), now=self.now,
                                                          sleep=self.sleep, err=io.StringIO())
        self.assertIsNotNone(index)
        self.assertGreater(waited, 0)
        sushirice.remove_tree(directory)

    def test_start_spacing(self):
        self.write_config({"start_spacing_s": 30})
        hold_slot(self.locks, 0, os.getpid())  # granted "just now" (mtime = real time)
        real_now = time.time
        err = io.StringIO()
        clock = [real_now()]
        index, _, _ = sushirice.acquire_slot(self.config(), False, 1.0, "x", self.project,
                                             now=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                                             err=err)
        self.assertIsNone(index)
        self.assertIn("granted", err.getvalue())


if __name__ == "__main__":
    unittest.main()
