"""The per-build watchdog inside `slot`: decision rules, then live runs on process trees this test spawns.

Live tests only signal processes they start: builds made of `sleep` processes reached through
a temp-dir symlink named `fake-clang` (config compiler_names = ["fake-clang"]).
"""

import io
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest

from helpers import SandboxCase, sushirice

CALM = {"cores": 4, "load": 1.0, "free_percent": 60, "pressure": "normal", "compilers": 0}


def proc(pid, age, name="fake-clang", uid=None, stat="R", ppid=1, pgid=None):
    return {"pid": pid, "ppid": ppid, "pgid": pgid if pgid is not None else pid,
            "uid": os.getuid() if uid is None else uid, "stat": stat, "age_s": age, "name": name, "path": "/t/" + name}


def rules(**watchdog):
    config = json.loads(json.dumps(sushirice.DEFAULT_CONFIG))
    config["compiler_names"] = ["fake-clang"]
    settings = {"max_compilers": 2, "pressure_level": "critical", "min_free_memory_percent": 10,
                "resume_margin_percent": 10, "pause_step": 1, "resume_step": 2, "min_running": 1,
                "terminate_after_s": 60, "exclude_names": []}
    settings.update(watchdog)
    config["watchdog"].update(settings)
    return config


@unittest.skipIf(os.name == "nt", "POSIX only")
class DecideTests(unittest.TestCase):
    def setUp(self):
        self.uid = os.getuid()
        self.empty = {"paused": {}, "triggered_since": None}

    def verbs(self, actions):
        return [(verb, process["pid"]) for verb, process, _ in actions]

    def test_pauses_newest_over_count(self):
        processes = [proc(101, 50), proc(102, 5), proc(103, 1), proc(104, 30)]
        actions, state = sushirice.watchdog_decide(processes, CALM, self.empty, rules(), 1000, self.uid)
        self.assertEqual(self.verbs(actions), [("pause", 103), ("pause", 102)])
        self.assertEqual(state["triggered_since"], 1000)

    def test_never_other_users(self):
        processes = [proc(201, 50), proc(202, 1, uid=self.uid + 1), proc(203, 2, uid=0), proc(205, 4)]
        actions, _ = sushirice.watchdog_decide(processes, CALM, self.empty, rules(max_compilers=1), 1000, self.uid)
        self.assertEqual(self.verbs(actions), [("pause", 205)])

    def test_memory_pause_keeps_min_running(self):
        config = rules(max_compilers=10, pause_step=5)
        actions, _ = sushirice.watchdog_decide([proc(301, 50), proc(302, 5)], dict(CALM, free_percent=3),
                                               self.empty, config, 1000, self.uid)
        self.assertEqual(self.verbs(actions), [("pause", 302)])
        actions, _ = sushirice.watchdog_decide([proc(301, 50)], dict(CALM, pressure="critical"), self.empty,
                                               config, 1000, self.uid)
        self.assertEqual(actions, [])

    def test_resume_oldest_first_with_hysteresis(self):
        processes = [proc(401, 100), proc(402, 50, stat="T"), proc(403, 40, stat="T")]
        state = {"paused": {"402": {"name": "fake-clang", "start": 950, "since": 990},
                            "403": {"name": "fake-clang", "start": 960, "since": 990}}, "triggered_since": 980}
        config = rules(max_compilers=10, resume_step=1)
        actions, _ = sushirice.watchdog_decide(processes, dict(CALM, free_percent=15), state, config, 1000, self.uid)
        self.assertEqual(actions, [])  # above trigger, below resume line
        actions, after = sushirice.watchdog_decide(processes, CALM, state, config, 1000, self.uid)
        self.assertEqual(self.verbs(actions), [("resume", 402)])
        self.assertEqual(list(after["paused"]), ["403"])

    def test_terminate_only_after_sustained_period(self):
        def processes(now):
            return [proc(501, now - 900), proc(502, now - 950, stat="T")]
        state = {"paused": {"502": {"name": "fake-clang", "start": 950, "since": 1000}}, "triggered_since": 1000}
        bad = dict(CALM, free_percent=2)
        config = rules(max_compilers=10, terminate_after_s=60)
        actions, state2 = sushirice.watchdog_decide(processes(1030), bad, state, config, 1030, self.uid)
        self.assertNotIn("terminate", [v for v, _, _ in actions])
        actions, _ = sushirice.watchdog_decide(processes(1061), bad, state2, config, 1061, self.uid)
        self.assertIn(("terminate", 502), self.verbs(actions))
        config["watchdog"]["terminate_after_s"] = None
        actions, _ = sushirice.watchdog_decide(processes(5000), bad, state2, config, 5000, self.uid)
        self.assertNotIn("terminate", [v for v, _, _ in actions])

    def test_drivers_waiting_on_children_are_not_counted(self):
        # three jobs: clang++ driver (fake-clang name reused) with a compiler child each
        processes = []
        for job in range(3):
            driver = 700 + job * 10
            processes.append(proc(driver, 30 - job * 5, ppid=1))
            processes.append(proc(driver + 1, 29 - job * 5, ppid=driver))
        actions, _ = sushirice.watchdog_decide(processes, CALM, self.empty, rules(max_compilers=2), 1000, self.uid)
        self.assertEqual(self.verbs(actions), [("pause", 721)])  # newest leaf, never a driver

    def test_pid_reuse_is_forgotten(self):
        state = {"paused": {"601": {"name": "fake-clang", "start": 100.0, "since": 900}}, "triggered_since": None}
        actions, new_state = sushirice.watchdog_decide([proc(601, 2)], CALM, state, rules(), 1000, self.uid)
        self.assertEqual((actions, new_state["paused"]), ([], {}))

    def test_tree_of(self):
        processes = [proc(10, 9, name="ninja", ppid=5, pgid=10), proc(11, 5, ppid=10, pgid=10),
                     proc(12, 4, ppid=11, pgid=10), proc(13, 3, ppid=1, pgid=10),  # orphan, same group
                     proc(14, 2, ppid=1, pgid=14), proc(15, 1, ppid=14, pgid=14)]
        self.assertEqual(sorted(p["pid"] for p in sushirice.tree_of(processes, 10)), [10, 11, 12, 13])


@unittest.skipIf(os.name == "nt", "POSIX only")
class LiveWatchdogTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.bin = tempfile.mkdtemp(prefix="sushirice-bin-")
        self.fake = os.path.join(self.bin, "fake-clang")
        os.symlink(shutil.which("sleep") or "/bin/sleep", self.fake)
        self.builds = []
        self.outsiders = []

    def tearDown(self):
        for child in self.builds:  # build groups this test started
            if child.poll() is None:
                sushirice.signal_group(child.pid, signal.SIGCONT)
                sushirice.signal_group(child.pid, signal.SIGKILL)
                child.wait()
        for child in self.outsiders:
            if child.poll() is None:
                os.kill(child.pid, signal.SIGCONT)
                child.kill()
                child.wait()
        shutil.rmtree(self.bin, ignore_errors=True)
        super().tearDown()

    def script(self, count, seconds, gap=1.1):
        parts = []
        for _ in range(count):
            parts.append("'%s' %s &" % (self.fake, seconds))
            parts.append("sleep %s;" % gap)
        return " ".join(parts) + " wait"

    def start(self, count, seconds, gap=1.1):
        child = sushirice.start_build(["sh", "-c", self.script(count, seconds, gap)], None)
        self.builds.append(child)
        deadline = time.time() + 10
        while time.time() < deadline:
            tree = [p for p in sushirice.tree_of(sushirice.list_processes(), child.pid) if p["name"] == "fake-clang"]
            if len(tree) >= count:
                return child, sorted(tree, key=lambda p: -p["age_s"])  # oldest first
            time.sleep(0.1)
        self.fail("build did not start")

    def stat(self, pid):
        return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE).stdout.decode().strip()

    def fake_procs(self):
        return [p for p in sushirice.list_processes() if p["path"] == self.fake]

    def write_rules(self, mode="enforce", **watchdog):
        settings = {"interval_s": 0.2, "max_compilers": 1, "pressure_level": "none", "min_free_memory_percent": 0,
                    "resume_margin_percent": 0, "pause_step": 1, "resume_step": 5, "terminate_after_s": None,
                    "stall_s": 0.5, "kill_grace_s": 3}
        settings.update(watchdog)
        self.write_config({"watchdog_mode": mode, "compiler_names": ["fake-clang"], "watchdog": settings})

    def test_pause_resume_only_inside_tree(self):
        outsider = subprocess.Popen([self.fake, "120"])  # ours, but not part of the build
        self.outsiders.append(outsider)
        child, tree = self.start(3, 120)
        self.write_rules()
        watchdog = sushirice.Watchdog(self.config(), self.project, child.pid, "enforce", err=io.StringIO(),
                                      measure_fn=lambda processes: CALM)
        done = watchdog.tick()
        paused = sorted(p["pid"] for p in done["pause"])
        self.assertEqual(paused, sorted(p["pid"] for p in tree[1:]))
        for pid in paused:
            self.assertIn("T", self.stat(pid))
        self.assertNotIn("T", self.stat(outsider.pid))
        self.assertNotIn("T", self.stat(tree[0]["pid"]))
        self.write_rules(max_compilers=10)
        watchdog.config = self.config()
        done = watchdog.tick()
        self.assertEqual(sorted(p["pid"] for p in done["resume"]), paused)
        for pid in paused:
            self.assertNotIn("T", self.stat(pid))

    def run_supervised(self, child, watchdog):
        result = {}
        thread = threading.Thread(target=lambda: result.setdefault("code", sushirice.supervise(child, watchdog, io.StringIO(), 0.1)))
        thread.start()
        thread.join(timeout=40)
        self.assertFalse(thread.is_alive(), "build never finished: a paused tree was left behind")
        return result["code"]

    def test_dead_watchdog_releases(self):
        child, tree = self.start(3, 4, gap=0.3)
        self.write_rules()
        calls = []

        def measure(processes):
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("simulated watchdog fault")
            return CALM

        watchdog = sushirice.Watchdog(self.config(), self.project, child.pid, "enforce", err=io.StringIO(),
                                      measure_fn=measure)
        self.assertEqual(self.run_supervised(child, watchdog), 0)
        summary = watchdog.summary()
        self.assertGreaterEqual(summary["paused"], 1)
        self.assertIn("thread died", summary["released"])
        self.assertIn("simulated", summary["failure"])

    def test_stalled_watchdog_releases(self):
        child, tree = self.start(3, 4, gap=0.3)
        self.write_rules()
        calls = []

        def measure(processes):
            calls.append(1)
            if len(calls) > 1:
                time.sleep(3600)  # stalls; the thread is a daemon
            return CALM

        watchdog = sushirice.Watchdog(self.config(), self.project, child.pid, "enforce", err=io.StringIO(),
                                      measure_fn=measure)
        self.assertEqual(self.run_supervised(child, watchdog), 0)
        self.assertIn("stalled", watchdog.summary()["released"])

    def test_cli_slot_pauses_and_resumes(self):
        self.write_rules()
        result = self.run_cli("slot", "--", "sh", "-c", self.script(3, 2, gap=0.4), timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"watchdog paused", result.stderr)
        record = self.runs()[-1]["watchdog"]
        self.assertEqual(record["mode"], "enforce")
        self.assertGreaterEqual(record["paused"], 1)
        self.assertGreaterEqual(record["resumed"], record["paused"])
        self.assertFalse(record["terminated"])
        self.assertEqual(self.fake_procs(), [])

    def test_cli_slot_terminates_after_sustained_pressure(self):
        self.write_rules(max_compilers=10, min_free_memory_percent=101, pause_step=5, terminate_after_s=1)
        started = time.time()
        result = self.run_cli("slot", "--", "sh", "-c", self.script(3, 60, gap=0.3), timeout=60)
        self.assertEqual(result.returncode, 128 + signal.SIGTERM, result.stderr)
        self.assertLess(time.time() - started, 30)
        self.assertIn(b"terminated the build", result.stderr)
        self.assertTrue(self.runs()[-1]["watchdog"]["terminated"])
        time.sleep(0.5)
        self.assertEqual(self.fake_procs(), [])

    def test_cli_terminate_reaches_jobs_in_their_own_groups(self):
        # like ninja, `set -m` puts each job in a process group of its own
        self.write_rules(max_compilers=10, min_free_memory_percent=101, pause_step=5, terminate_after_s=1)
        result = self.run_cli("slot", "--", "sh", "-c", "set -m; " + self.script(3, 60, gap=0.3), timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.runs()[-1]["watchdog"]["terminated"])
        time.sleep(0.5)
        self.assertEqual(self.fake_procs(), [])

    def test_sigterm_to_wrapper_releases_paused_jobs(self):
        self.write_rules(max_compilers=1)
        wrapper = subprocess.Popen([sushirice.sys.executable, sushirice.SCRIPT_PATH, "slot", "--", "sh", "-c",
                                    self.script(3, 60, gap=0.3)], cwd=self.project, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 15
            while time.time() < deadline and not any("T" in p["stat"] for p in self.fake_procs()):
                time.sleep(0.2)
            self.assertTrue(any("T" in p["stat"] for p in self.fake_procs()), "nothing was paused")
            wrapper.terminate()  # started by this test
            wrapper.wait(timeout=30)
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
        self.assertEqual(wrapper.returncode, 128 + signal.SIGTERM)
        time.sleep(0.5)
        # paused jobs were continued, so the forwarded SIGTERM ended them: nothing is left, stopped or not
        self.assertEqual(self.fake_procs(), [])

    def test_cli_dry_run(self):
        self.write_rules()
        result = self.run_cli("slot", "--dry-run", "--", "sh", "-c", self.script(3, 2, gap=0.4), timeout=60)
        self.assertEqual(result.returncode, 0)
        record = self.runs()[-1]["watchdog"]
        self.assertEqual(record["mode"], "dry-run")
        self.assertTrue(any("would pause" in event["event"] for event in record["events"]))
        self.assertIn(b"(dry-run) would pause", result.stderr)

    def test_config_off(self):
        self.write_rules(mode="off")
        self.run_cli("slot", "--", "true")
        self.assertNotIn("watchdog", self.runs()[-1])

    def test_guard_diagnostic_never_signals(self):
        outsider = subprocess.Popen([self.fake, "120"])
        self.outsiders.append(outsider)
        self.outsiders.append(subprocess.Popen([self.fake, "120"]))
        time.sleep(1.1)
        self.outsiders.append(subprocess.Popen([self.fake, "120"]))
        self.write_rules()
        result = self.run_cli("guard")
        self.assertIn(b"never signals", result.stdout)
        self.assertIn(b"would pause", result.stdout)
        for child in self.outsiders:
            self.assertNotIn("T", self.stat(child.pid))


if __name__ == "__main__":
    unittest.main()
