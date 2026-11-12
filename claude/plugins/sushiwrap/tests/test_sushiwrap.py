"""SushiWrap tests. Run: python3 -m unittest discover -s plugins/sushiwrap/tests

The remote survey test runs only when SUSHIWRAP_TEST_HOST names an ssh host (read-only probe).
"""

import contextlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
SCRIPT = os.path.join(SCRIPTS, "sushiwrap.py")
sys.path.insert(0, SCRIPTS)

import sushiwrap as sw  # noqa: E402
import sushiwrap_probe as probe  # noqa: E402

TRACK_TEXT = """# Track w9

## RESUME POINTER (2026-11-01, first)
- old work

## Previous RESUME POINTER (2026-11-05, should be skipped)
- skipped

## (superseded) RESUME POINTER (2026-11-06)
- skipped too

## RESUME POINTER (2026-11-08, the latest; supersedes the ones above)
- Branch claude/w9 @ abc123
```
## RESUME POINTER inside a fence does not count
```
### Detail
- a sub-heading stays inside the section

## Gallery stack (2026-11-08)
- not part of the pointer
"""

STATE_TEXT = """# State

## SHUTDOWN 2026-11-02 (reboot). Resume here.
- old

## Resume after the 2026-11-02 reboot
- not a shutdown

## SHUTDOWN 2026-11-09 (owner reboot)
Resume order:
1. Integrator (fold/cp25 in D:/w3): resume from the queue.
2. w9 (claude/w9): finish the thing.
"""

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_NOSYSTEM": "1"}


def git(repo, *args):
    env = dict(os.environ)
    env.update(GIT_ENV)
    env["HOME"] = repo  # ignore the user's global git config and hooks
    subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main", "-C", repo] + list(args),
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)


class Isolated(unittest.TestCase):
    """A temp project with its own config; no CLAUDE_PROJECT_DIR, no user config."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sushiwrap-test-")
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.project, ".sushi"))
        self.config_path = os.path.join(self.project, ".sushi", "sushiwrap.json")
        self.saved_env = dict(os.environ)
        for name in ("CLAUDE_PROJECT_DIR", "SUSHI_SEAT", "SUSHI_ROLE", "SUSHIWRAP_NOW", "SUSHIWRAP_BOOT_TIME",
                     "SUSHIWRAP_DEADLINE_FILE"):
            os.environ.pop(name, None)
        os.environ["SUSHIWRAP_CONFIG"] = self.config_path
        self.write_config({})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, config):
        with open(self.config_path, "w") as handle:
            json.dump(config, handle)
        self.config = sw.load_config(self.project)

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sw.main(list(args) + ["--project", self.project])
        return code, out.getvalue()


# ---------------------------------------------------------------- seats

class SeatFileTests(Isolated):
    def test_round_trip(self):
        header = {"seat": "w4", "role": "coder", "track_file": "tracks/x.md", "branch": "claude/x",
                  "worktree": {"neo": "/w/w4", "*": "D:/w4"}, "owned_paths": ["src/a.cpp"],
                  "open_tickets": ["T-12"], "host_manifest": "manifests/m4.json", "holder": None,
                  "updated_at": "2026-11-11T10:00:00Z", "pointer_updated_at": "2026-11-11T09:58:00Z",
                  "custom": "kept"}
        pointer = "Doing the thing — ünïcode.\n\n### Notes\n- next: X\n## not a new pointer"
        text = sw.render_seat(header, pointer)
        parsed_header, parsed_pointer = sw.parse_seat(text)
        self.assertEqual(parsed_header, header)
        self.assertEqual(parsed_pointer, pointer)
        self.assertEqual(sw.render_seat(parsed_header, parsed_pointer), text)
        self.assertEqual(list(parsed_header)[:len(sw.SEAT_FIELDS)], sw.SEAT_FIELDS)

    def test_empty_pointer_and_bad_file(self):
        header, pointer = sw.parse_seat(sw.render_seat({"seat": "w1"}, ""))
        self.assertEqual(pointer, "")
        with self.assertRaises(ValueError):
            sw.parse_seat("# no header here")

    def test_for_host(self):
        self.assertEqual(sw.for_host({"neo": "/a", "*": "/b"}, "neo"), "/a")
        self.assertEqual(sw.for_host({"neo": "/a", "*": "/b"}, "shadow"), "/b")
        self.assertEqual(sw.for_host("/c", "any"), "/c")

    def test_bad_seat_name(self):
        with self.assertRaises(SystemExit):
            sw.seat_path(self.config, self.project, "../evil")


class SeatCommandTests(Isolated):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.project, "tracks"))
        self.track = os.path.join(self.project, "tracks", "w9.md")
        with open(self.track, "w") as handle:
            handle.write(TRACK_TEXT)
        self.state = os.path.join(self.project, "STATE.md")
        with open(self.state, "w") as handle:
            handle.write(STATE_TEXT)
        self.write_config({"state_file": "STATE.md"})

    def test_init_pointer_take_release(self):
        code, out = self.cli("seat", "init", "w9", "--role", "coder", "--track", "tracks/w9.md",
                             "--branch", "claude/w9", "--worktree", "neo=/nowhere/w9", "--owned", "src/a",
                             "--owned", "src/b", "--ticket", "T-1")
        self.assertEqual(code, 0)
        header, pointer = sw.load_seat(self.config, self.project, "w9")
        self.assertEqual(header["owned_paths"], ["src/a", "src/b"])
        self.assertEqual(header["worktree"], {"neo": "/nowhere/w9"})
        self.assertIsNone(header["pointer_updated_at"])

        os.environ["SUSHIWRAP_NOW"] = str(sw.parse_iso("2026-11-11T10:00:00Z"))
        code, _ = self.cli("seat", "pointer", "w9", "--text", "Half way through X.\nNext: Y.", "--append-track")
        self.assertEqual(code, 0)
        header, pointer = sw.load_seat(self.config, self.project, "w9")
        self.assertEqual(pointer, "Half way through X.\nNext: Y.")
        self.assertEqual(header["pointer_updated_at"], "2026-11-11T10:00:00Z")
        heading, body = sw.track_pointer(self.track)
        self.assertIn("seat w9, via sushiwrap", heading)
        self.assertIn("Next: Y.", body)

        code, out = self.cli("seat", "pointer", "w9", "--text", "Replaced.")
        _, pointer = sw.load_seat(self.config, self.project, "w9")
        self.assertEqual(pointer, "Replaced.")

        code, out = self.cli("seat", "take", "w9", "--by", "test")
        self.assertEqual(code, 0)
        self.assertIn("SEAT w9 (role coder)", out)
        self.assertIn("Replaced.", out)
        self.assertIn("Track file's latest resume note", out)
        self.assertIn("2. w9 (claude/w9): finish the thing.", out)  # STATE line naming the seat
        header, _ = sw.load_seat(self.config, self.project, "w9")
        self.assertEqual(header["holder"]["host"], sw.hostname())

        code, out = self.cli("seat", "list")
        self.assertIn("claimed by", out)
        code, out = self.cli("seat", "release", "w9")
        header, _ = sw.load_seat(self.config, self.project, "w9")
        self.assertIsNone(header["holder"])

    def test_set_and_init_twice(self):
        self.cli("seat", "init", "w8", "--branch", "a")
        self.cli("seat", "set", "w8", "--branch", "b", "--worktree", "/tmp/x")
        header, _ = sw.load_seat(self.config, self.project, "w8")
        self.assertEqual((header["branch"], header["worktree"]), ("b", "/tmp/x"))
        with self.assertRaises(SystemExit):
            self.cli("seat", "init", "w8")


class TrackPointerTests(unittest.TestCase):
    def test_latest_pointer(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(TRACK_TEXT)
        try:
            heading, body = sw.track_pointer(handle.name)
        finally:
            os.remove(handle.name)
        self.assertIn("the latest", heading)
        self.assertIn("claude/w9 @ abc123", body)
        self.assertIn("a sub-heading stays", body)
        self.assertNotIn("not part of the pointer", body)

    def test_latest_shutdown_section(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(STATE_TEXT)
        try:
            heading, body = sw.latest_section(handle.name, sw.DEFAULT_CONFIG["shutdown_heading"])
        finally:
            os.remove(handle.name)
        self.assertIn("2026-11-09", heading)
        self.assertTrue(body.startswith("Resume order:"))


# ---------------------------------------------------------------- deadline

class DeadlineMathTests(Isolated):
    NOW = 1800000000.0

    def test_durations(self):
        self.assertEqual(sw.parse_duration("45m"), 2700)
        self.assertEqual(sw.parse_duration("8h"), 28800)
        self.assertEqual(sw.parse_duration("1h30m"), 5400)
        self.assertEqual(sw.parse_duration("90s"), 90)
        self.assertEqual(sw.parse_duration("20"), 1200)
        for bad in ("abc", "5x", "1h junk"):
            with self.assertRaises(ValueError):
                sw.parse_duration(bad)

    def test_iso(self):
        self.assertEqual(sw.parse_iso("2027-01-15T08:00:00Z"), 1800000000.0)
        self.assertEqual(sw.parse_iso("2027-01-15T09:00:00+01:00"), 1800000000.0)
        local = sw.parse_iso("2027-01-15T08:00:00")
        self.assertEqual(time.localtime(local)[:5], (2027, 1, 15, 8, 0))

    def test_status(self):
        d = sw.make_deadline(self.NOW + 600, "in", self.NOW)
        self.assertEqual(sw.deadline_status(d, self.NOW), ("active", 10.0))
        self.assertEqual(sw.deadline_status(d, self.NOW + 600 + 3599)[0], "active")
        self.assertEqual(sw.deadline_status(d, self.NOW + 600 + 3601)[0], "expired")
        self.assertEqual(sw.deadline_status(None, self.NOW), ("none", None))
        other = dict(d, host="someotherhost")
        self.assertEqual(sw.deadline_status(other, self.NOW)[0], "other-host")
        self.assertEqual(sw.deadline_status(dict(other, scope="all"), self.NOW)[0], "active")
        boot = self.NOW - 3600
        from_boot = sw.make_deadline(boot + 8 * 3600, "from-boot", self.NOW, duration_s=8 * 3600, boot=boot)
        self.assertEqual(sw.deadline_status(from_boot, self.NOW, current_boot=boot + 30)[0], "active")
        self.assertEqual(sw.deadline_status(from_boot, self.NOW, current_boot=boot + 7200)[0], "stale")

    def test_cli_set_in_and_from_boot(self):
        os.environ["SUSHIWRAP_NOW"] = str(self.NOW)
        self.cli("deadline", "set", "--in", "45m", "--note", "test")
        d = sw.read_deadline(sw.deadline_path(self.config, self.project))
        self.assertEqual(d["at"], self.NOW + 2700)
        self.assertEqual(d["source"], "in")
        os.environ["SUSHIWRAP_BOOT_TIME"] = str(self.NOW - 3 * 3600)
        self.cli("deadline", "set", "--from-boot", "8h")
        d = sw.read_deadline(sw.deadline_path(self.config, self.project))
        self.assertEqual(d["at"], self.NOW + 5 * 3600)
        self.assertEqual(d["boot_time"], self.NOW - 3 * 3600)
        code, out = self.cli("deadline", "show")
        self.assertIn("300 min left", out)
        self.cli("deadline", "set", "--at", sw.iso_utc(self.NOW + 60))
        self.assertEqual(sw.read_deadline(sw.deadline_path(self.config, self.project))["at"], self.NOW + 60)
        self.cli("deadline", "clear")
        self.assertIsNone(sw.read_deadline(sw.deadline_path(self.config, self.project)))

    def test_custom_deadline_path(self):
        shared = os.path.join(self.tmp, "synced", "deadline.json")
        self.write_config({"deadline_file": shared})
        os.environ["SUSHIWRAP_NOW"] = str(self.NOW)
        self.cli("deadline", "set", "--in", "10m", "--scope", "all")
        self.assertEqual(sw.read_deadline(shared)["scope"], "all")

    def test_renew_after_reboot(self):
        path = sw.deadline_path(self.config, self.project)
        old_boot = self.NOW - 2 * 3600
        sw.atomic_write(path, json.dumps(sw.make_deadline(old_boot + 8 * 3600, "from-boot", old_boot,
                                                          duration_s=8 * 3600, boot=old_boot)))
        new_boot = self.NOW - 600  # rebooted early, 10 minutes ago
        renewed = sw.renew_deadline(self.config, self.project, self.NOW, new_boot)
        self.assertEqual(renewed["at"], new_boot + 8 * 3600)
        self.assertEqual(sw.read_deadline(path)["boot_time"], new_boot)
        # unchanged boot: nothing to do
        self.assertEqual(sw.renew_deadline(self.config, self.project, self.NOW, new_boot + 5)["at"], renewed["at"])

    def test_auto_from_boot(self):
        self.write_config({"auto_from_boot": "8h"})
        boot = self.NOW - 3600
        created = sw.renew_deadline(self.config, self.project, self.NOW, boot)
        self.assertEqual(created["at"], boot + 8 * 3600)
        self.assertEqual(created["source"], "from-boot")
        # a boot so old that boot+8h already passed is left alone
        os.remove(sw.deadline_path(self.config, self.project))
        self.assertIsNone(sw.renew_deadline(self.config, self.project, self.NOW, self.NOW - 9 * 3600))

    def test_boot_parsers(self):
        self.assertEqual(sw.parse_sysctl_boottime("{ sec = 1793183352, usec = 314749 } Wed Oct 28 05:29:12 2026"),
                         1793183352.0)
        self.assertEqual(sw.parse_wmic_boottime("LastBootUpTime\n20261111083015.500000+060\n"),
                         sw.parse_iso("2026-11-11T08:30:15+01:00"))
        self.assertEqual(sw.parse_powershell_epoch("1793183352\r\n"), 1793183352.0)
        self.assertIsNone(sw.parse_wmic_boottime("garbage"))

    @unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "POSIX boot time")
    def test_real_boot_time(self):
        os.environ.pop("SUSHIWRAP_BOOT_TIME", None)
        boot = sw.boot_time()
        self.assertIsNotNone(boot)
        self.assertLess(boot, time.time())
        self.assertGreater(boot, time.time() - 400 * 86400)


# ---------------------------------------------------------------- monitor

class MonitorTests(Isolated):
    def run_minutes(self, at, minutes, thresholds=(30, 15, 5), state=None):
        state = state or {"at": None, "fired": None}
        fired = []
        for minute in minutes:
            fire, threshold, state = sw.monitor_step(state, at, at - minute * 60, list(thresholds))
            if fire:
                fired.append((minute, threshold))
        return fired, state

    def test_each_threshold_once(self):
        fired, _ = self.run_minutes(10000.0, [m * 0.5 for m in range(80, -4, -1)])
        self.assertEqual(fired, [(30.0, 30), (15.0, 15), (5.0, 5), (0.0, 0)])

    def test_silent_far_away_and_without_deadline(self):
        fired, _ = self.run_minutes(10000.0, range(120, 31, -1))
        self.assertEqual(fired, [])
        self.assertEqual(sw.monitor_step({}, None, 0, [30])[0], False)

    def test_late_start_fires_only_most_urgent(self):
        fired, _ = self.run_minutes(10000.0, [12, 11, 10, 6, 5, 4])
        self.assertEqual(fired, [(12, 15), (5, 5)])

    def test_new_deadline_resets(self):
        fired, state = self.run_minutes(10000.0, [10])
        self.assertEqual(fired, [(10, 15)])
        fired, state = self.run_minutes(20000.0, [40, 29], state=state)  # deadline moved later
        self.assertEqual(fired, [(29, 30)])

    def test_cli_once(self):
        now = 1800000000.0
        os.environ["SUSHIWRAP_NOW"] = str(now)
        sw.atomic_write(sw.deadline_path(self.config, self.project),
                        json.dumps(sw.make_deadline(now + 14 * 60, "in", now)))
        code, out = self.cli("monitor", "--once")
        self.assertIn("Host reset in 14 min", out)
        self.assertIn("Commit your work in progress", out)
        sw.atomic_write(sw.deadline_path(self.config, self.project),
                        json.dumps(sw.make_deadline(now + 90 * 60, "in", now)))
        code, out = self.cli("monitor", "--once")
        self.assertEqual(out, "")

    def test_monitor_rereads_deadline_file(self):
        now = 1800000000.0
        os.environ["SUSHIWRAP_NOW"] = str(now)
        path = sw.deadline_path(self.config, self.project)
        script = ("import sys,runpy; sys.argv=['sushiwrap.py','monitor','--interval','0.2','--project',%r]; "
                  "runpy.run_path(%r, run_name='__main__')" % (self.project, SCRIPT))
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, env=dict(os.environ))
        try:
            time.sleep(0.6)
            sw.atomic_write(path, json.dumps(sw.make_deadline(now + 4 * 60, "in", now)))
            line = process.stdout.readline().decode()
            self.assertIn("Host reset in 4 min", line)
        finally:
            process.terminate()  # a process this test started itself
            process.wait(5)
            process.stdout.close()


# ---------------------------------------------------------------- hook

class HookTests(Isolated):
    NOW = 1800000000.0

    def set_deadline(self, minutes, **extra):
        d = sw.make_deadline(self.NOW + minutes * 60, "in", self.NOW)
        d.update(extra)
        sw.atomic_write(sw.deadline_path(self.config, self.project), json.dumps(d))

    def test_bucket_dedupe(self):
        self.set_deadline(28)
        event = {"session_id": "s1"}
        first = sw.hook_decide(event, self.project, self.config, self.NOW)
        self.assertIn("Host reset in 28 min", first)
        self.assertIsNone(sw.hook_decide(event, self.project, self.config, self.NOW + 60))   # 27 min, same bucket
        self.assertIsNone(sw.hook_decide(event, self.project, self.config, self.NOW + 180))  # 25 min, same bucket
        self.assertIn("24 min", sw.hook_decide(event, self.project, self.config, self.NOW + 240))
        self.assertIsNotNone(sw.hook_decide({"session_id": "s2"}, self.project, self.config, self.NOW + 240))
        self.assertIsNotNone(sw.hook_decide({"session_id": "s1", "agent_id": "a7"}, self.project, self.config,
                                            self.NOW + 240))
        passed = sw.hook_decide(event, self.project, self.config, self.NOW + 29 * 60)
        self.assertIn("passed 1 min ago", passed)

    def test_silent_cases(self):
        self.assertIsNone(sw.hook_decide({}, self.project, self.config, self.NOW))  # no deadline
        self.set_deadline(31)
        self.assertIsNone(sw.hook_decide({}, self.project, self.config, self.NOW))
        self.set_deadline(10, host="otherhost")
        self.assertIsNone(sw.hook_decide({}, self.project, self.config, self.NOW))

    def test_new_deadline_clears_old_markers(self):
        self.set_deadline(20)
        sw.hook_decide({"session_id": "s"}, self.project, self.config, self.NOW)
        self.set_deadline(25)
        self.assertIsNotNone(sw.hook_decide({"session_id": "s"}, self.project, self.config, self.NOW))
        base = os.path.join(self.project, ".sushi", "state", "sushiwrap", "hook")
        self.assertEqual(os.listdir(base), [str(int(self.NOW + 25 * 60))])

    def hook_process(self, stdin, flags=("-S",)):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp, "CLAUDE_PROJECT_DIR": self.project,
               "SUSHIWRAP_CONFIG": self.config_path, "SUSHIWRAP_NOW": str(self.NOW)}
        started = time.perf_counter()
        completed = subprocess.run([sys.executable] + list(flags) + [SCRIPT, "hook"], input=stdin.encode(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        return completed, (time.perf_counter() - started) * 1000

    def test_hook_process_output_and_speed(self):
        self.set_deadline(12)
        completed, _ = self.hook_process(json.dumps({"session_id": "p1", "tool_name": "Bash"}))
        self.assertEqual(completed.returncode, 0)
        output = json.loads(completed.stdout)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", output["hookSpecificOutput"])
        timings = []
        for index in range(12):
            completed, elapsed = self.hook_process(json.dumps({"session_id": "speed%d" % index}))
            self.assertEqual(completed.returncode, 0)
            timings.append(elapsed)
        median = statistics.median(timings)
        sys.stderr.write("\n  hook process median %.1f ms (max %.1f)\n" % (median, max(timings)))
        self.assertLess(median, 150)  # whole process; typically ~30 ms on Apple silicon
        started = time.perf_counter()
        for index in range(50):
            sw.hook_decide({"session_id": "inproc%d" % index}, self.project, self.config, self.NOW)
        self.assertLess((time.perf_counter() - started) * 1000 / 50, 10)

    def test_hook_never_breaks(self):
        completed, _ = self.hook_process("this is not json")
        self.assertEqual((completed.returncode, completed.stdout), (0, b""))
        with open(self.config_path, "w") as handle:
            handle.write("{broken")
        completed, _ = self.hook_process("{}")
        self.assertEqual((completed.returncode, completed.stdout), (0, b""))


# ---------------------------------------------------------------- shutdown-check

class ShutdownCheckTests(Isolated):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "wt")
        os.makedirs(os.path.join(self.repo, "dev", "tools"))
        git(self.repo, "init", "-q")
        with open(os.path.join(self.repo, "a.txt"), "w") as handle:
            handle.write("one\n")
        with open(os.path.join(self.repo, ".gitignore"), "w") as handle:
            handle.write("dev/logs/\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "initial")
        git(self.repo, "checkout", "-q", "-b", "claude/seat")
        self.memory_a = os.path.join(self.tmp, "mem_a")
        self.memory_b = os.path.join(self.tmp, "mem_b")
        os.makedirs(self.memory_a)
        os.makedirs(self.memory_b)
        for name in ("MEMORY.md", "fact.md"):
            with open(os.path.join(self.memory_a, name), "w") as handle:
                handle.write(name)
        self.write_config({"memory_mirror": [[self.memory_a, self.memory_b]]})
        self.cli("seat", "init", "w5", "--branch", "claude/seat", "--worktree", self.repo)
        self.cli("seat", "pointer", "w5", "--text", "Pointer.")

    def test_dirty_then_clean(self):
        with open(os.path.join(self.repo, "a.txt"), "a") as handle:
            handle.write("two\n")
        with open(os.path.join(self.repo, "dev", "tools", "one_off.sh"), "w") as handle:
            handle.write("#!/bin/sh\n")  # untracked one-off scripts are not the check's business
        code, out = self.cli("shutdown-check")
        self.assertEqual(code, 1)
        self.assertIn("[w5] FAIL 1 uncommitted change(s)", out)
        self.assertNotIn("one_off.sh", out)
        self.assertIn("[mirror] FAIL", out)
        self.assertIn("2 missing", out)

        git(self.repo, "commit", "-q", "-am", "WIP: half done")
        for name in ("MEMORY.md", "fact.md"):
            shutil.copy(os.path.join(self.memory_a, name), self.memory_b)
        code, out = self.cli("shutdown-check", "--seat", "w5")
        self.assertEqual(code, 0, out)
        self.assertIn("worktree clean (HEAD", out)
        self.assertIn("WIP: half done", out)
        self.assertIn("[mirror] PASS", out)
        self.assertIn("Result: 0 FAIL, 0 WARN", out)

    def test_stale_pointer_and_wrong_branch(self):
        os.environ["SUSHIWRAP_NOW"] = str(time.time() + 3 * 3600)
        git(self.repo, "checkout", "-q", "main")
        code, out = self.cli("shutdown-check", "--max-age", "30")
        self.assertEqual(code, 1)
        self.assertIn("FAIL resume note updated 3.0 h ago", out)
        self.assertIn("worktree is on main, the seat branch is claude/seat", out)

    def test_template(self):
        code, out = self.cli("shutdown-template", "--reason", "test reset")
        self.assertIn("## SHUTDOWN", out)
        self.assertIn("Resume order:", out)
        self.assertIn("1. w5 (", out)
        self.assertIn("Pointer.", out)


# ---------------------------------------------------------------- survey / provision

class SurveyTests(Isolated):
    def test_redaction(self):
        self.assertEqual(probe.redact_value("GITHUB_TOKEN", "abc"), "<redacted>")
        self.assertEqual(probe.redact_value("AWS_SECRET_ACCESS_KEY", "abc"), "<redacted>")
        self.assertEqual(probe.redact_value("DB_PASSWORD", "x"), "<redacted>")
        self.assertEqual(probe.redact_value("CCACHE_DIR", "/Users/x/.ccache"), "/Users/x/.ccache")
        self.assertEqual(probe.redact_value("REMOTE", "https://u:p4ss@example.com/x"), "<redacted>")
        self.assertEqual(probe.redact_value("X", "ghp_abcdefghijklmnop1234"), "<redacted>")
        self.assertEqual(probe.redact_value("X", "A" * 40), "<redacted>")
        self.assertEqual(probe.strip_url_credentials("https://tok@github.com/o/r.git"), "https://github.com/o/r.git")

    def test_local_probe_on_temp_workspace(self):
        root = os.path.join(self.tmp, "ws")
        repo = os.path.join(root, "w1", "proj")
        os.makedirs(os.path.join(repo, "dev", "tools"))
        git(repo, "init", "-q")
        with open(os.path.join(repo, "README"), "w") as handle:
            handle.write("x")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "c")
        git(repo, "remote", "add", "origin", "https://secret-token@example.com/org/proj.git")
        with open(os.path.join(repo, "dev", "tools", "one_off.sh"), "w") as handle:
            handle.write("#!/bin/sh\necho hi\n")
        os.environ["SUSHI_TEST_API_TOKEN"] = "do-not-leak"
        os.environ["SUSHI_TEST_PLAIN"] = "visible"
        manifest = probe.run_probe({"workspace_roots": [root], "login_path": False})
        self.assertEqual(manifest["env"]["SUSHI_TEST_API_TOKEN"], "<redacted>")
        self.assertEqual(manifest["env"]["SUSHI_TEST_PLAIN"], "visible")
        self.assertNotIn("do-not-leak", json.dumps(manifest))
        self.assertNotIn("secret-token", json.dumps(manifest))
        self.assertTrue(manifest["tools"]["git"]["found"])
        self.assertTrue(manifest["volumes"])
        workspace = manifest["workspaces"][0]
        repo_entry = workspace["repos"][0]
        self.assertEqual(repo_entry["branch"] in ("main", "master"), True)
        self.assertEqual(repo_entry["remotes"]["origin"], "https://example.com/org/proj.git")
        self.assertNotIn("one_off.sh", json.dumps(manifest))  # no untracked-file listing
        self.assertEqual(set(repo_entry), {"path", "is_worktree", "branch", "head", "remotes"})

        markdown = sw.manifest_markdown(manifest)
        self.assertIn("## Toolchain", markdown)
        self.assertIn("proj: ", markdown)
        plan = "\n".join(sw.provision_plan(manifest))
        self.assertIn("git clone https://example.com/org/proj.git", plan)
        self.assertNotIn("Restore", plan)

        anonymous = sw.anonymize(manifest)
        self.assertNotIn(manifest["user"]["home"], json.dumps(anonymous))

    def test_survey_cli_writes_both_files(self):
        self.write_config({"survey": {"login_path": False}})
        out_base = os.path.join(self.tmp, "manifest")
        code, out = self.cli("survey", "--out", out_base + ".json", "--anonymize")
        self.assertEqual(code, 0)
        with open(out_base + ".json") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["host"]["hostname"], "<host>")
        self.assertTrue(os.path.isfile(out_base + ".md"))
        code, out = self.cli("provision", "--manifest", out_base + ".json")
        self.assertIn("Provision plan", out)
        self.assertIn("not executed", out)

    @unittest.skipUnless(os.environ.get("SUSHIWRAP_TEST_HOST"), "set SUSHIWRAP_TEST_HOST to an ssh host")
    def test_remote_probe(self):
        host = os.environ["SUSHIWRAP_TEST_HOST"]
        manifest = sw.remote_survey(host, self.config)
        self.assertEqual(manifest["probe_version"], probe.PROBE_VERSION)
        self.assertTrue(manifest["host"]["python"])
        self.assertTrue(manifest["tools"]["git"]["found"])
        self.assertTrue(manifest["path"]["entries"])
        self.assertTrue(manifest["volumes"])
        self.assertEqual(manifest["errors"], [])


if __name__ == "__main__":
    unittest.main()
