"""SushiComms tests. Run: python3 -m unittest discover -s plugins/sushicomms/tests

Everything runs in temporary project directories. The real INTEGRATION_QUEUE.md is only
ever read and copied; the copy is what gets imported.
"""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

# HOME isolation: logs or state written without a project root never reach the real home.
# The real queue path is resolved first (it is only read and copied).
REAL_QUEUE = os.environ.get("SUSHICOMMS_REAL_QUEUE") or os.path.expanduser(
    "~/Developer/projects/desks/coordinator/INTEGRATION_QUEUE.md")
FAKE_HOME = tempfile.mkdtemp(prefix="sushicomms-home-")
os.environ["HOME"] = FAKE_HOME

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
SCRIPT = os.path.join(PLUGIN, "scripts", "sushicomms.py")
EXAMPLE = os.path.join(PLUGIN, "examples", "roles.json")
sys.path.insert(0, os.path.join(PLUGIN, "scripts"))
sys.dont_write_bytecode = True  # keep __pycache__ out of the plugin tree

import sushicomms as sc  # noqa: E402

SAMPLE_QUEUE = """# Integration queue

Prose before the queue.

## Entry format (append below; one block per branch head)

```
### <branch> @ <head>  (wN, base <sha>)  status: queued
- intent: <one line>
```

## Queue

### claude/effects_lane-fd5b @ 71dcbf0f  (w8, base 815f7ae9)  status: batched integration/batch1 (7e8862c2)
- intent: FD-5b closure: adds a registry-wide refusal test.
- touched: tests/form_language/form_generator_tests.cpp only.
- tests: form_generator_tests (form_language label).
- risks: laptop clang -fsyntax-only clean.

### claude/families_b-b5 @ 7e70c10e  (w6; W-4 segmented control, one commit stacked on 21ec9761)  status: batched integration/batch4; returned (display_controls_tests:357)
- intent: D6 segmented control.
- touched: choice_group header/source.
- tests: gui_forms_display_controls_tests.
- fix 1d4f60bc: segment_bounds rounds each cumulative edge.
- risks: nothing compiled.
  continuation line of the risks bullet.

## w1 claude/t4-inspection 2d8b20e7 (on W's claude/gallery-stack 8d4a3d75): non-standard heading
  - free-form body

### claude/abi-focus @ 65e4f4d2 (w7 Windows, base a5cdb1b1 = claude/abi-window-properties; supersedes a594942e) status: queued
- intent: focus stack.
- touched: gf_abi.h.
- tests: abi tests.
- risks: dry merge conflicts in gf_abi.h.
"""


def tearDownModule():
    shutil.rmtree(FAKE_HOME, ignore_errors=True)


def clean_env(**extra):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("SUSHI_", "SUSHICOMMS_", "CLAUDE_PROJECT_DIR"))}
    env["HOME"] = FAKE_HOME
    env.update({key: value for key, value in extra.items() if value is not None})
    return env


def run(project, args, role=None, seat=None, stdin=None, **extra):
    env = clean_env(SUSHI_ROLE=role, SUSHI_SEAT=seat, **extra)
    return subprocess.run([sys.executable, SCRIPT] + list(args), cwd=project, env=env, input=stdin,
                          capture_output=True, text=True, timeout=60)


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="sushicomms-test-")
        os.makedirs(os.path.join(self.project, ".sushi", "sushicomms"))
        shutil.copyfile(EXAMPLE, os.path.join(self.project, ".sushi", "sushicomms", "roles.json"))
        self.store_file = os.path.join(self.project, ".sushi", "state", "sushicomms", "state.json")

    def tearDown(self):
        shutil.rmtree(self.project, ignore_errors=True)

    def cli(self, args, role=None, seat=None, ok=True, **extra):
        result = run(self.project, args, role=role, seat=seat, **extra)
        if ok:
            self.assertEqual(result.returncode, 0, "%s\nstdout: %s\nstderr: %s" % (args, result.stdout, result.stderr))
        return result

    def reserve(self, block, batch, seat):
        return self.cli(["ticket", "file", "--type", "block-reserve", "--to", "coder", "--to-seat", seat,
                         "--block", block, "--batch", batch, "--intent", "block %s" % block],
                        role="orchestrator", seat="o1")

    def handoff(self, seat, branch, head, block=None, batch=None, extra=()):
        args = ["handoff", "--branch", branch, "--head", head, "--intent", "do %s" % branch,
                "--touched", "src/x.cpp", "--tests", "x_tests", "--risks", "never compiled"]
        if block:
            args += ["--block", block]
        if batch:
            args += ["--batch", batch]
        return self.cli(args + list(extra), role="coder", seat=seat)

    def load(self, ticket_id):
        return sc.TicketStore(self.store_file).get(ticket_id)

    def all_tickets(self):
        return sc.TicketStore(self.store_file).all()

    def monitor(self, role="orchestrator"):
        return self.cli(["monitor", "--once", "--project", self.project], role=role)

    def hook(self, kind, event, role=None):
        event.setdefault("cwd", self.project)
        result = run(self.project, ["hook-" + kind], role=role, stdin=json.dumps(event),
                     CLAUDE_PROJECT_DIR=self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else None


def sample_ticket(ticket_type="handoff", **overrides):
    ticket = sc.blank_ticket()
    ticket.update({"id": "T0001-w4", "schema": 1, "type": ticket_type, "status": "open",
                   "from_seat": "w4", "from_role": "coder", "to_role": "integrator",
                   "created": "2026-11-11T10:00:00Z", "updated": "2026-11-11T10:00:00Z",
                   "history": [{"at": "2026-11-11T10:00:00Z", "seat": "w4", "role": "coder",
                                "action": "filed", "status": "open", "detail": None}]})
    if ticket_type == "handoff":
        ticket.update({"branch": "claude/x", "head": "abc1234", "intent": "i", "touched": "t", "tests": "x", "risks": "r"})
    elif ticket_type == "question":
        ticket.update({"to_role": "orchestrator", "question": "Which?", "options": ["a", "b"], "recommendation": 1})
    elif ticket_type == "return":
        ticket.update({"from_role": "integrator", "to_role": "orchestrator", "refs": ["T0002-w4"],
                       "branch": "claude/x", "head": "abc1234", "reason": "test fails"})
    elif ticket_type == "done":
        ticket.update({"to_role": "orchestrator", "summary": "finished"})
    elif ticket_type == "block-reserve":
        ticket.update({"from_role": "orchestrator", "to_role": "coder", "to_seat": "w4",
                       "block": "1", "batch": "b1", "intent": "do it"})
    elif ticket_type == "notify":
        ticket.update({"to_role": "orchestrator", "refs": ["T0002-w4"]})
    ticket.update(overrides)
    return ticket


# ---------------------------------------------------------------- schema

class SchemaTests(unittest.TestCase):
    def test_every_type_has_a_valid_sample(self):
        for ticket_type in sc.TICKET_TYPES:
            with self.subTest(ticket_type=ticket_type):
                self.assertEqual(sc.validate_ticket(sample_ticket(ticket_type)), [])

    def test_fixed_key_set(self):
        ticket = sample_ticket()
        del ticket["risks"]
        self.assertIn("missing keys: risks", sc.validate_ticket(ticket)[0])
        ticket = sample_ticket()
        ticket["note_to_coder"] = "hi"
        self.assertTrue(any("unknown keys" in error for error in sc.validate_ticket(ticket)))

    def test_required_and_forbidden_fields(self):
        self.assertTrue(any("intent is required" in e for e in sc.validate_ticket(sample_ticket(intent=None))))
        self.assertTrue(any("must not be empty" in e for e in sc.validate_ticket(sample_ticket(intent="  "))))
        self.assertTrue(any("question is not used by handoff" in e
                            for e in sc.validate_ticket(sample_ticket(question="free prose?"))))
        self.assertTrue(any("refs is required" in e for e in sc.validate_ticket(sample_ticket("notify", refs=None))))

    def test_no_free_form_prose(self):
        self.assertTrue(any("one line" in e for e in sc.validate_ticket(sample_ticket(intent="a\nb"))))
        self.assertTrue(any("at most 400" in e for e in sc.validate_ticket(sample_ticket(intent="x" * 401))))
        too_many = ["o%d" % index for index in range(sc.MAX_OPTIONS + 1)]
        self.assertTrue(sc.validate_ticket(sample_ticket("question", options=too_many)))

    def test_field_formats(self):
        self.assertTrue(sc.validate_ticket(sample_ticket(head="not-a-sha")))
        self.assertTrue(sc.validate_ticket(sample_ticket(branch="has space")))
        self.assertTrue(sc.validate_ticket(sample_ticket(status="queued")))
        self.assertTrue(sc.validate_ticket(sample_ticket(id="7")))
        self.assertTrue(sc.validate_ticket(sample_ticket(from_seat="w 4")))
        self.assertTrue(sc.validate_ticket(sample_ticket("notify", refs=["nope"])))
        self.assertTrue(sc.validate_ticket(sample_ticket("question", recommendation=3)))
        self.assertTrue(sc.validate_ticket(sample_ticket(history=[])))
        self.assertTrue(sc.validate_ticket(sample_ticket(history=[{"at": "x"}])))
        self.assertTrue(sc.validate_ticket(sample_ticket(source={"kind": "made-up"})))

    def test_imported_tickets_relax_only_text_limits(self):
        source = {"kind": "queue-import", "header": "### b @ abc1234  (w1)  status: pinned", "body": [], "note": "w1",
                  "status_text": "pinned", "file": "q.md", "line": 3}
        ticket = sample_ticket(source=source, status="other", status_detail="pinned", risks="", branch="repo claude/x",
                               intent="x" * 3000)
        self.assertEqual(sc.validate_ticket(ticket), [])
        self.assertTrue(sc.validate_ticket(dict(ticket, intent="a\nb")))
        self.assertTrue(sc.validate_ticket(sample_ticket(status="other")))  # only imports may be "other"


# ---------------------------------------------------------------- registry and permissions

class RegistryTests(unittest.TestCase):
    def setUp(self):
        with open(EXAMPLE, encoding="utf-8") as handle:
            self.registry = json.load(handle)

    def test_example_is_valid_and_has_the_five_roles(self):
        self.assertEqual(sc.validate_registry(self.registry), [])
        self.assertEqual(set(self.registry["roles"]), {"orchestrator", "coder", "integrator", "standby", "researcher"})

    def test_invalid_registries_are_reported(self):
        cases = []
        broken = copy.deepcopy(self.registry)
        broken["roles"]["coder"]["channels"]["handoff"] = ["nobody"]
        cases.append((broken, "unknown target role"))
        broken = copy.deepcopy(self.registry)
        del broken["roles"]["orchestrator"]
        cases.append((broken, "orchestrator role is required"))
        broken = copy.deepcopy(self.registry)
        broken["drift_rules"][0]["pattern"] = "(unclosed"
        cases.append((broken, "drift_rules[0].pattern"))
        broken = copy.deepcopy(self.registry)
        broken["roles"]["integrator"]["agent_types"] = ["gui-forms-coder"]
        cases.append((broken, "mapped to both"))
        broken = copy.deepcopy(self.registry)
        broken["quorum_rules"][1]["min_fraction"] = 0.5
        cases.append((broken, "exactly one of min"))
        broken = copy.deepcopy(self.registry)
        broken["roles"]["coder"]["channels"]["chat"] = ["integrator"]
        cases.append((broken, "unknown ticket type"))
        for registry, expected in cases:
            with self.subTest(expected=expected):
                errors = sc.validate_registry(registry)
                self.assertTrue(any(expected in error for error in errors), errors)


class PermissionMatrixTests(ProjectCase):
    # (role, ticket type) -> targets allowed in the shipped example; everything else is refused.
    EXPECTED = {
        ("orchestrator", "block-reserve"): {"coder", "standby", "researcher"},
        ("orchestrator", "return"): {"coder"},
        ("orchestrator", "done"): {"coder", "integrator", "standby", "researcher"},
        ("coder", "handoff"): {"integrator"},
        ("coder", "notify"): {"orchestrator"},
        ("coder", "question"): {"orchestrator"},
        ("coder", "done"): {"orchestrator"},
        ("integrator", "return"): {"orchestrator"},
        ("integrator", "question"): {"orchestrator"},
        ("integrator", "done"): {"orchestrator"},
        ("integrator", "notify"): {"orchestrator"},
        ("standby", "done"): {"orchestrator"},
        ("standby", "question"): {"orchestrator"},
        ("researcher", "done"): {"orchestrator"},
        ("researcher", "question"): {"orchestrator"},
    }

    def test_full_matrix_against_registry(self):
        registry = sc.load_registry(self.project)
        roles = list(registry["roles"])
        for role in roles:
            for ticket_type in sc.TICKET_TYPES:
                for target in roles:
                    allowed = target in self.EXPECTED.get((role, ticket_type), set())
                    problem = sc.check_file_permission(registry, role, ticket_type, target)
                    with self.subTest(role=role, type=ticket_type, target=target):
                        self.assertEqual(problem is None, allowed, problem)
        self.assertIsNotNone(sc.check_file_permission(registry, "intruder", "done", "orchestrator"))

    def test_cli_rejects_disallowed_type_and_target(self):
        refused = self.cli(["ticket", "file", "--type", "handoff", "--to", "orchestrator", "--branch", "b",
                            "--head", "abc1234", "--intent", "i", "--touched", "t", "--tests", "x", "--risks", "r"],
                           role="coder", seat="w4", ok=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("only to integrator", refused.stderr)
        refused = self.cli(["ticket", "file", "--type", "block-reserve", "--to", "coder", "--to-seat", "w4",
                            "--block", "1", "--batch", "b", "--intent", "x"], role="integrator", seat="w3", ok=False)
        self.assertIn("may not file block-reserve", refused.stderr)
        unset = self.cli(["ticket", "file", "--type", "done", "--to", "orchestrator", "--summary", "x"], ok=False)
        self.assertIn("set SUSHI_ROLE", unset.stderr)
        self.assertEqual(self.all_tickets(), [])

    def test_schema_enforced_at_filing(self):
        result = self.cli(["ticket", "file", "--type", "question", "--to", "orchestrator", "--question", "two\nlines"],
                          role="coder", seat="w4", ok=False)
        self.assertIn("one line", result.stderr)
        result = self.cli(["ticket", "file", "--type", "done", "--to", "orchestrator", "--summary", "s",
                           "--question", "smuggled prose"], role="coder", seat="w4", ok=False)
        self.assertIn("not used by done", result.stderr)

    def test_claim_update_route_close_permissions(self):
        self.handoff("w4", "claude/a", "abc1234")
        handoff_id = "T0001-w4"
        # Another coder cannot claim a ticket addressed to the integrator.
        self.assertIn("may not", self.cli(["ticket", "claim", handoff_id], role="coder", seat="w6", ok=False).stderr)
        self.cli(["ticket", "claim", handoff_id], role="integrator", seat="w3")
        self.assertEqual(self.load(handoff_id)["claimed_by"], "w3")
        self.cli(["ticket", "update", handoff_id, "--status", "batched", "--detail", "integration/batch9"],
                 role="integrator", seat="w3")
        self.assertIn("may not set status superseded",
                      self.cli(["ticket", "update", handoff_id, "--status", "superseded"], role="integrator",
                               seat="w3", ok=False).stderr)
        # Return goes integrator -> orchestrator, then the orchestrator routes it to the coder.
        self.cli(["ticket", "file", "--type", "return", "--to", "orchestrator", "--ref", handoff_id, "--branch",
                  "claude/a", "--head", "abc1234", "--reason", "display_controls_tests:357 fails"],
                 role="integrator", seat="w3")
        return_id = "T0003-w3"
        self.assertIn("only the orchestrator routes",
                      self.cli(["ticket", "route", return_id, "--to", "coder"], role="integrator", seat="w3",
                               ok=False).stderr)
        self.cli(["ticket", "route", return_id, "--to", "coder", "--to-seat", "w4"], role="orchestrator", seat="o1")
        routed = self.load(return_id)
        self.assertEqual((routed["to_role"], routed["to_seat"]), ("coder", "w4"))
        self.assertEqual([entry["action"] for entry in routed["history"]], ["filed", "routed"])
        # Questions are answered only by the orchestrator.
        self.cli(["ticket", "file", "--type", "question", "--to", "orchestrator", "--question", "Renumber slots?",
                  "--option", "after keys", "--option", "before keys", "--recommend", "1"], role="coder", seat="w4")
        self.assertIn("refused", self.cli(["ticket", "close", "T0004-w4", "--answer", "1"], role="coder", seat="w4",
                                          ok=False).stderr)
        self.cli(["ticket", "close", "T0004-w4", "--answer", "after keys"], role="orchestrator", seat="o1")
        self.assertEqual(self.load("T0004-w4")["answer"], "after keys")
        for ticket in self.all_tickets():
            self.assertEqual(sc.validate_ticket(ticket), [], ticket["id"])

    def test_refs_must_exist(self):
        result = self.cli(["ticket", "file", "--type", "notify", "--to", "orchestrator", "--ref", "T0099-w4"],
                          role="coder", seat="w4", ok=False)
        self.assertIn("does not exist", result.stderr)


# ---------------------------------------------------------------- concurrency

class ConcurrencyTests(ProjectCase):
    def test_parallel_filing_never_loses_or_overwrites(self):
        workers, per_worker = 8, 5
        code = ("import subprocess,sys\n"
                "for n in range(%d):\n"
                "    r=subprocess.run([sys.executable,%r,'ticket','file','--type','done','--to','orchestrator',"
                "'--summary','item %%d'%%n],capture_output=True,text=True)\n"
                "    sys.exit(r.returncode) if r.returncode else None\n") % (per_worker, SCRIPT)
        processes = []
        for index in range(workers):
            seat = "w%d" % (index % 3)  # several processes share a seat (id suffix collides on purpose)
            processes.append(subprocess.Popen([sys.executable, "-c", code], cwd=self.project,
                                              env=clean_env(SUSHI_ROLE="coder", SUSHI_SEAT=seat),
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for process in processes:
            _out, err = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, err)
        names = sorted(os.listdir(os.path.dirname(self.store_file)))
        self.assertEqual(names, ["state.json"], "lock or temp files left behind")
        tickets = self.all_tickets()
        self.assertEqual(len(tickets), workers * per_worker)
        self.assertEqual(len({ticket["id"] for ticket in tickets}), workers * per_worker)
        numbers = sorted(int(sc.ID_RE.match(ticket["id"]).group(1)) for ticket in tickets)
        self.assertEqual(numbers, list(range(1, workers * per_worker + 1)))
        for ticket in tickets:
            self.assertEqual(sc.validate_ticket(ticket), [])

    def test_stale_lock_is_broken(self):
        lock = self.store_file + ".lock"
        os.makedirs(lock)
        old = os.path.getmtime(lock) - 3600
        os.utime(lock, (old, old))
        self.cli(["ticket", "file", "--type", "done", "--to", "orchestrator", "--summary", "x"], role="coder", seat="w4")
        self.assertFalse(os.path.exists(lock))

    def test_corrupt_store_is_never_overwritten(self):
        os.makedirs(os.path.dirname(self.store_file))
        with open(self.store_file, "w") as handle:
            handle.write("{half")
        result = self.cli(["ticket", "file", "--type", "done", "--to", "orchestrator", "--summary", "x"],
                          role="coder", seat="w4", ok=False)
        self.assertIn("left untouched", result.stderr)
        with open(self.store_file) as handle:
            self.assertEqual(handle.read(), "{half")


# ---------------------------------------------------------------- quorum and monitor

class QuorumTests(ProjectCase):
    def test_all_reserved_fires_once(self):
        for block, seat in (("1", "w4"), ("2", "w6"), ("3", "w8")):
            self.reserve(block, "fold26", seat)
        self.handoff("w4", "claude/a", "aaa1111", block="1")
        self.handoff("w6", "claude/b", "bbb2222", block="2")
        self.assertEqual(self.monitor().stdout, "")
        self.handoff("w8", "claude/c", "ccc3333", block="3")
        lines = self.monitor().stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn("Batch fold26: 3 of 3 handoffs in (rule batch-all-in)", lines[0])
        for ticket_id in ("T0004-w4", "T0006-w6", "T0008-w8"):
            self.assertIn(ticket_id, lines[0])
        self.assertEqual(self.monitor().stdout, "")  # same quorum state: silent
        self.assertEqual(self.monitor().stdout, "")
        # The handoff inherited the batch from the reservation and references it.
        self.assertEqual(self.load("T0004-w4")["batch"], "fold26")
        self.assertEqual(self.load("T0004-w4")["refs"], ["T0001-o1"])

    def test_new_head_is_a_new_state_and_superseded_does_not_count(self):
        self.reserve("1", "b2", "w4")
        self.reserve("2", "b2", "w6")
        self.handoff("w4", "claude/a", "aaa1111", block="1")
        self.handoff("w4", "claude/a", "aaa2222", block="1")  # supersedes the first
        self.assertEqual(self.load("T0003-w4")["status"], "superseded")
        self.assertEqual(self.monitor().stdout, "")
        self.handoff("w6", "claude/b", "bbb1111", block="2")
        first = self.monitor().stdout
        self.assertIn("T0005-w4", first)
        self.assertNotIn("T0003-w4", first)
        self.assertEqual(self.monitor().stdout, "")
        self.handoff("w6", "claude/b", "bbb2222", block="2")  # a fresh head: a new quorum state
        self.assertIn("T0009-w6", self.monitor().stdout)

    def test_n_of_m_below_all(self):
        seats = ["w1", "w2", "w4", "w5", "w6"]
        for index, seat in enumerate(seats, start=1):
            self.reserve(str(index), "big", seat)
        for index, seat in enumerate(seats[:3], start=1):
            self.handoff(seat, "claude/n%d" % index, "abc000%d" % index, block=str(index))
        self.assertEqual(self.monitor().stdout, "")
        self.handoff(seats[3], "claude/n4", "abc0004", block="4")
        out = self.monitor().stdout.strip().splitlines()
        self.assertEqual(len(out), 1)
        self.assertIn("Batch big: 4 of 5 handoffs in (rule batch-mostly-in)", out[0])
        self.handoff(seats[4], "claude/n5", "abc0005", block="5")
        out = self.monitor().stdout.strip().splitlines()
        self.assertEqual(len(out), 1)
        self.assertIn("Batch big: 5 of 5 handoffs in (rule batch-all-in)", out[0])

    def test_count_rule_in_process(self):
        registry = sc.load_registry(self.project)
        registry["quorum_rules"][2]["enabled"] = True
        tickets = [sample_ticket(id="T%04d-w4" % number, head="abc%04d" % number) for number in range(1, 4)]
        self.assertEqual(sc.evaluate_quorum(tickets, registry), [])
        tickets.append(sample_ticket(id="T0004-w4", head="abc0004"))
        results = sc.evaluate_quorum(tickets, registry)
        self.assertEqual(len(results), 1)
        self.assertIn("Rule loose-handoffs: 4 handoff tickets to integrator", results[0][1])

    def test_monitor_is_silent_outside_the_orchestrator_seat(self):
        self.reserve("1", "solo", "w4")
        self.handoff("w4", "claude/a", "aaa1111", block="1")
        self.assertEqual(self.monitor(role="coder").stdout, "")
        self.assertFalse(os.path.exists(os.path.join(self.project, ".sushi", "state", "sushicomms", "quorum_seen.json")))
        self.assertIn("1 of 1", self.monitor().stdout)

    def test_monitor_with_empty_project_argument_uses_cwd(self):
        self.reserve("1", "solo", "w4")
        self.handoff("w4", "claude/a", "aaa1111", block="1")
        result = self.cli(["monitor", "--once", "--project", ""], role="orchestrator")
        self.assertIn("1 of 1", result.stdout)


# ---------------------------------------------------------------- hooks

def bash_event(command, session="s1", **extra):
    event = {"session_id": session, "hook_event_name": "PreToolUse", "tool_name": "Bash",
             "tool_input": {"command": command}}
    event.update(extra)
    return event


def edit_event(path, tool="Write", **extra):
    event = {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"file_path": path}}
    event.update(extra)
    return event


def context_of(output):
    return (output or {}).get("hookSpecificOutput", {}).get("additionalContext")


def decision_of(output):
    return (output or {}).get("hookSpecificOutput", {}).get("permissionDecision")


class DriftHookTests(ProjectCase):
    def test_coder_build_commands_get_one_gentle_reminder(self):
        for index, command in enumerate(["cmake --build build -j5", "ninja -C build", "make -j8", "ctest -R x",
                                         "m4ws w4 python3 tools/ci_build.py --jobs 8", "xcodebuild -scheme X",
                                         "cd build && ninja", "m4run w4 --label t -- python3 tools/ci_build.py"]):
            with self.subTest(command=command):
                output = self.hook("bash", bash_event(command, session="coder-%d" % index), role="coder")
                self.assertIn("Coders don't build", context_of(output))
                self.assertIsNone(decision_of(output))

    def test_coder_allowed_commands_are_silent(self):
        for command in ["m4ws w4 clang++ -std=c++23 -fsyntax-only src/a.cpp", "git grep -n make src",
                        "python3 -B tools/check_house_style.py src/a.cpp", "cmake -S . -B build",
                        "echo ninja", "ninja -C build src/a.cpp.o"]:
            with self.subTest(command=command):
                self.assertIsNone(self.hook("bash", bash_event(command, session="quiet"), role="coder"))

    def test_dedupe_per_rule_per_session(self):
        self.assertIsNotNone(self.hook("bash", bash_event("ninja", session="A"), role="coder"))
        self.assertIsNone(self.hook("bash", bash_event("ctest", session="A"), role="coder"))
        self.assertIsNotNone(self.hook("bash", bash_event("ninja", session="B"), role="coder"))

    def test_integrator_wrapper_rule(self):
        output = self.hook("bash", bash_event("sh dev/tools/fold23_build.sh tests", session="i1"), role="integrator")
        self.assertIn("butterbuild", context_of(output))
        self.assertIn("build_slot.py -- sh dev/tools/fold23_build.sh", context_of(output))
        self.assertIsNone(self.hook("bash", bash_event("build_slot.py -- sh dev/tools/fold23_build.sh tests",
                                                       session="i2"), role="integrator"))
        self.assertIsNotNone(self.hook("bash", bash_event("cmake --build ../.build/fold23", session="i3"),
                                       role="integrator"))
        self.assertIsNone(self.hook("bash", bash_event("ninja -C build", session="o"), role="orchestrator"))

    def test_subagent_role_comes_from_agent_type(self):
        # A gui-forms-coder subagent inside the orchestrator session inherits SUSHI_ROLE=orchestrator.
        output = self.hook("bash", bash_event("ninja", session="sub", agent_type="gui-forms-coder", agent_id="a1"),
                           role="orchestrator")
        self.assertIn("Coders don't build", context_of(output))

    def test_deny_mode_rule(self):
        path = os.path.join(self.project, ".sushi", "sushicomms", "roles.json")
        registry = sc.load_registry(self.project)
        registry["drift_rules"].append({"id": "coder-no-push", "roles": ["coder"], "pattern": "{cmd}git\\s+push{end}",
                                        "mode": "deny", "message": "Only the orchestrator pushes."})
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(registry, handle)
        for session in ("d1", "d1"):  # denials are not deduped
            output = self.hook("bash", bash_event("git push origin HEAD", session=session), role="coder")
            self.assertEqual(decision_of(output), "deny")
            self.assertIn("Only the orchestrator pushes", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_identity_guard(self):
        spoof = self.hook("bash", bash_event('SUSHI_ROLE=orchestrator python3 "x/sushicomms.py" ticket route T0001-w4 --to coder'),
                          role="coder")
        self.assertEqual(decision_of(spoof), "deny")
        inherited = self.hook("bash", bash_event('python3 "x/sushicomms.py" handoff --branch b', agent_type="gui-forms-coder",
                                                 agent_id="a1"), role="orchestrator")
        self.assertEqual(decision_of(inherited), "deny")
        self.assertIn("SUSHI_ROLE=coder", inherited["hookSpecificOutput"]["permissionDecisionReason"])
        prefixed = self.hook("bash", bash_event('SUSHI_ROLE=coder SUSHI_SEAT=w4 python3 "x/sushicomms.py" handoff --branch b',
                                                agent_type="gui-forms-coder", agent_id="a1"), role="orchestrator")
        self.assertIsNone(prefixed)
        unmapped = self.hook("bash", bash_event("python3 x/sushicomms.py ticket close T0001-w4", agent_type="Explore",
                                                agent_id="a2"), role="orchestrator")
        self.assertEqual(decision_of(unmapped), "deny")
        reading = self.hook("bash", bash_event("python3 x/sushicomms.py ticket list --open", agent_type="Explore",
                                               agent_id="a2"), role="orchestrator")
        self.assertIsNone(reading)

    def test_hook_never_breaks(self):
        for stdin in ("", "not json", "[]", json.dumps({"tool_name": "Bash"})):
            result = run(self.project, ["hook-bash"], role="coder", stdin=stdin, CLAUDE_PROJECT_DIR=self.project)
            self.assertEqual((result.returncode, result.stdout), (0, ""))
        with open(os.path.join(self.project, ".sushi", "sushicomms", "roles.json"), "w") as handle:
            handle.write("{broken")
        self.assertIsNone(self.hook("bash", bash_event("ninja"), role="coder"))

    def test_no_registry_means_no_drift_rules(self):
        os.remove(os.path.join(self.project, ".sushi", "sushicomms", "roles.json"))
        self.assertIsNone(self.hook("bash", bash_event("ninja"), role="coder"))


class RegistryGuardTests(ProjectCase):
    def setUp(self):
        super().setUp()
        self.registry_file = os.path.join(self.project, ".sushi", "sushicomms", "roles.json")

    def test_only_the_orchestrator_edits_the_registry(self):
        for role in ("coder", "integrator", "standby", "researcher", None):
            for tool in ("Edit", "Write", "MultiEdit"):
                with self.subTest(role=role, tool=tool):
                    output = self.hook("edit", edit_event(self.registry_file, tool=tool), role=role)
                    self.assertEqual(decision_of(output), "deny")
        self.assertIsNone(self.hook("edit", edit_event(self.registry_file), role="orchestrator"))
        config = os.path.join(self.project, ".sushi", "sushicomms.json")
        self.assertEqual(decision_of(self.hook("edit", edit_event(config), role="coder")), "deny")

    def test_subagent_in_orchestrator_session_cannot_edit(self):
        output = self.hook("edit", edit_event(self.registry_file, agent_type="Explore", agent_id="a1"), role="orchestrator")
        self.assertEqual(decision_of(output), "deny")
        output = self.hook("edit", edit_event(self.registry_file, agent_type="gui-forms-coder", agent_id="a1"),
                           role="orchestrator")
        self.assertEqual(decision_of(output), "deny")

    def test_relative_path_and_other_files(self):
        output = self.hook("edit", edit_event(".sushi/sushicomms/roles.json"), role="coder")
        self.assertEqual(decision_of(output), "deny")
        self.assertIsNone(self.hook("edit", edit_event(os.path.join(self.project, "src", "a.cpp")), role="coder"))
        self.assertIsNone(self.hook("edit", edit_event(os.path.join(self.project, ".sushi", "sushistyle.json")),
                                    role="coder"))

    def test_store_is_off_limits_to_file_tools_for_every_role(self):
        self.handoff("w4", "claude/a", "abc1234")
        for role in ("coder", "integrator", "orchestrator", None):
            for tool, key in (("Read", "file_path"), ("Edit", "file_path"), ("Write", "file_path"), ("Grep", "path")):
                with self.subTest(role=role, tool=tool):
                    event = {"session_id": "s1", "tool_name": tool, "tool_input": {key: self.store_file}}
                    output = self.hook("edit", event, role=role)
                    self.assertEqual(decision_of(output), "deny")
                    self.assertIn("ticket list", output["hookSpecificOutput"]["permissionDecisionReason"])
        lock_event = {"tool_name": "Read", "tool_input": {"file_path": os.path.dirname(self.store_file) + "/drift_seen.json"}}
        self.assertEqual(decision_of(self.hook("edit", lock_event, role="orchestrator")), "deny")
        # Reading the registry is fine for everyone.
        registry_read = {"tool_name": "Read", "tool_input": {"file_path": self.registry_file}}
        self.assertIsNone(self.hook("edit", registry_read, role="coder"))

    def test_store_is_off_limits_to_the_shell(self):
        self.handoff("w4", "claude/a", "abc1234")
        for command in ("cat .sushi/state/sushicomms/state.json", "jq . %s" % self.store_file,
                        "sed -n 1,5p .sushi/state/sushicomms/state.json", "echo {} > %s" % self.store_file,
                        "cd .sushi/state/sushicomms && cat state.json", "python3 -c 'open(\"%s\")'" % self.store_file):
            for role in ("coder", "orchestrator"):
                with self.subTest(command=command, role=role):
                    self.assertEqual(decision_of(self.hook("bash", bash_event(command), role=role)), "deny")
        allowed = 'python3 "x/sushicomms.py" ticket list --open'
        self.assertIsNone(self.hook("bash", bash_event(allowed), role="coder"))
        self.assertIsNone(self.hook("bash", bash_event("cat src/state.json"), role="coder"))

    def test_configured_store_path_is_guarded(self):
        elsewhere = tempfile.mkdtemp(prefix="sushicomms-synced-")
        self.addCleanup(shutil.rmtree, elsewhere, True)
        store = os.path.join(elsewhere, "coordinator-tickets.json")
        with open(os.path.join(self.project, ".sushi", "sushicomms.json"), "w") as handle:
            json.dump({"state_file": store}, handle)
        self.cli(["ticket", "file", "--type", "done", "--to", "orchestrator", "--summary", "x"], role="coder", seat="w4")
        self.assertEqual([t["id"] for t in sc.TicketStore(store).all()], ["T0001-w4"])
        self.assertEqual(decision_of(self.hook("edit", edit_event(store, tool="Read"), role="orchestrator")), "deny")
        for command in ("cat %s" % store, "grep x coordinator-tickets.json"):
            self.assertEqual(decision_of(self.hook("bash", bash_event(command), role="integrator")), "deny")
        env_store = os.path.join(elsewhere, "env.json")
        self.cli(["ticket", "file", "--type", "done", "--to", "orchestrator", "--summary", "y"], role="coder", seat="w4",
                 SUSHICOMMS_STATE=env_store)
        self.assertEqual(len(sc.TicketStore(env_store).all()), 1)

    def test_shell_writes_to_registry_are_denied_for_non_orchestrators(self):
        output = self.hook("bash", bash_event("echo {} > .sushi/sushicomms/roles.json"), role="coder")
        self.assertEqual(decision_of(output), "deny")
        self.assertIsNone(self.hook("bash", bash_event("cat .sushi/sushicomms/roles.json 2>/dev/null"), role="coder"))
        self.assertIsNone(self.hook("bash", bash_event("cp x .sushi/sushicomms/roles.json"), role="orchestrator"))

    def test_registry_init_is_orchestrator_only(self):
        os.remove(self.registry_file)
        self.assertIn("only the orchestrator", self.cli(["registry", "init"], role="coder", ok=False).stderr)
        self.cli(["registry", "init"], role="orchestrator")
        self.assertEqual(self.cli(["registry", "validate"]).returncode, 0)


class AgentSpawnHookTests(ProjectCase):
    def spawn(self, role, subagent_type="general-purpose", session="s1", **extra):
        event = {"session_id": session, "tool_name": "Agent",
                 "tool_input": {"subagent_type": subagent_type, "prompt": "x", "description": "x"}}
        event.update(extra)
        return self.hook("agent", event, role=role)

    def test_non_orchestrators_get_one_reminder(self):
        output = self.spawn("coder", session="a")
        self.assertIn("Only the coordinator (orchestrator role) starts agents", context_of(output))
        self.assertIsNone(decision_of(output))  # a reminder, never a block
        self.assertIsNone(self.spawn("coder", session="a"))
        self.assertIsNotNone(self.spawn(None, session="b"))  # unset SUSHI_ROLE is not the orchestrator
        self.assertIsNotNone(self.spawn("integrator", subagent_type="Explore", session="c"))

    def test_orchestrator_and_tedium_helper_are_silent(self):
        self.assertIsNone(self.spawn("orchestrator", session="d"))
        self.assertIsNone(self.spawn("coder", subagent_type="gui-forms-tedium", session="e"))
        self.assertIsNone(self.spawn("coder", subagent_type="someplugin:gui-forms-tedium", session="e2"))

    def test_subagent_inside_orchestrator_session_is_reminded(self):
        output = self.spawn("orchestrator", session="f", agent_type="gui-forms-coder", agent_id="a1")
        self.assertIn("this seat is coder", context_of(output))

    def test_no_registry_or_disabled_means_silent(self):
        registry = sc.load_registry(self.project)
        registry["settings"]["agent_spawn"]["reminder"] = False
        with open(self.registry_path(), "w") as handle:
            json.dump(registry, handle)
        self.assertIsNone(self.spawn("coder", session="g"))
        os.remove(self.registry_path())
        self.assertIsNone(self.spawn("coder", session="h"))

    def registry_path(self):
        return os.path.join(self.project, ".sushi", "sushicomms", "roles.json")


# ---------------------------------------------------------------- handoff convenience

class HandoffTests(ProjectCase):
    def test_handoff_files_handoff_and_notify(self):
        result = self.handoff("w4", "claude/a", "abc1234")
        self.assertIn("T0001-w4 handoff -> integrator", result.stdout)
        self.assertIn("T0002-w4 notify -> orchestrator", result.stdout)
        handoff, notify = self.load("T0001-w4"), self.load("T0002-w4")
        self.assertEqual((handoff["type"], handoff["to_role"], handoff["from_seat"]), ("handoff", "integrator", "w4"))
        self.assertEqual((notify["type"], notify["to_role"], notify["refs"]), ("notify", "orchestrator", ["T0001-w4"]))

    def test_handoff_needs_the_fields(self):
        result = self.cli(["handoff", "--branch", "b", "--head", "abc1234", "--intent", "i"], role="coder", seat="w4",
                          ok=False)
        self.assertIn("--touched", result.stderr)

    def test_handoff_is_coder_only_in_the_example(self):
        result = self.cli(["handoff", "--branch", "b", "--head", "abc1234", "--intent", "i", "--touched", "t",
                           "--tests", "x", "--risks", "r"], role="researcher", seat="r1", ok=False)
        self.assertIn("may not file handoff", result.stderr)

    def test_handoff_reads_head_from_git(self):
        if shutil.which("git") is None:
            self.skipTest("git not available")
        env = clean_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@example.invalid")
        for command in (["git", "init", "-q", "-b", "claude/demo"], ["git", "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(command, cwd=self.project, env=env, check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "--short=9", "HEAD"], cwd=self.project, env=env,
                              capture_output=True, text=True).stdout.strip()
        self.cli(["handoff", "--branch", "claude/demo", "--intent", "i", "--touched", "t", "--tests", "x",
                  "--risks", "r"], role="coder", seat="w4")
        self.assertEqual(self.load("T0001-w4")["head"], head)


# ---------------------------------------------------------------- INTEGRATION_QUEUE.md interop

class QueueInteropTests(ProjectCase):
    def import_text(self, text, project=None):
        project = project or self.project
        path = os.path.join(project, "queue_copy.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        dry = run(project, ["import-queue", path])
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertIn("dry run", dry.stdout)
        self.assertFalse(os.path.exists(os.path.join(project, ".sushi", "state", "sushicomms", "state.json")),
                         "dry run must not write")
        applied = run(project, ["import-queue", path, "--apply"], role="orchestrator", seat="o1")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        return path, dry.stdout

    def assert_round_trip(self, text):
        _path, report = self.import_text(text)
        original, skipped = sc.parse_queue(text)
        exported = self.cli(["export-queue", "--all"]).stdout
        again, again_skipped = sc.parse_queue(exported)
        self.assertEqual(again_skipped, [])
        self.assertEqual(len(again), len(original))
        for before, after in zip(original, again):
            self.assertEqual(before["header"], after["header"], "line %d" % before["line"])
            self.assertEqual(before["body"], after["body"], "line %d" % before["line"])
        # Re-importing the export gives the same structured fields.
        second = tempfile.mkdtemp(prefix="sushicomms-reimport-")
        self.addCleanup(shutil.rmtree, second, True)
        os.makedirs(os.path.join(second, ".sushi"))
        self.import_text(exported, project=second)
        keys = ("branch", "head", "status", "status_detail", "from_seat", "base", "intent", "touched", "tests", "risks")
        first_fields = [tuple(t[k] for k in keys) for t in self.all_tickets()]
        second_fields = [tuple(t[k] for k in keys) for t in sc.TicketStore(os.path.join(second, ".sushi", "state", "sushicomms", "state.json")).all()]
        self.assertEqual(first_fields, second_fields)
        for ticket in self.all_tickets():
            self.assertEqual(sc.validate_ticket(ticket), [], ticket["id"])
        return original, skipped, report

    def test_sample_round_trip_and_parsing(self):
        original, skipped, report = self.assert_round_trip(SAMPLE_QUEUE)
        self.assertEqual(len(original), 3)
        self.assertEqual(len(skipped), 1)
        self.assertIn("1 heading(s) skipped", report)
        tickets = self.all_tickets()
        first, second, third = tickets
        self.assertEqual((first["from_seat"], first["base"], first["status"], first["status_detail"]),
                         ("w8", "815f7ae9", "batched", "integration/batch1 (7e8862c2)"))
        self.assertEqual(second["status"], "batched")
        self.assertIn("continuation line of the risks bullet", second["risks"])
        self.assertEqual((third["from_seat"], third["base"], third["status"]), ("w7", "a5cdb1b1", "open"))

    def test_export_default_is_open_handoffs_only(self):
        self.import_text(SAMPLE_QUEUE)
        exported = self.cli(["export-queue"]).stdout
        self.assertEqual(exported.count("### "), 1)
        self.assertIn("claude/abi-focus @ 65e4f4d2", exported)

    def test_status_change_rerenders_header_and_keeps_body(self):
        self.import_text(SAMPLE_QUEUE)
        self.cli(["ticket", "update", "T0003-o1", "--status", "batched", "--detail", "integration/batch13 (1234abcd)"],
                 role="integrator", seat="w3")
        exported = self.cli(["export-queue", "--all"]).stdout
        self.assertIn("### claude/abi-focus @ 65e4f4d2  (w7 Windows, base a5cdb1b1 = claude/abi-window-properties; "
                      "supersedes a594942e)  status: batched integration/batch13 (1234abcd)\n- intent: focus stack.",
                      exported)

    def test_new_tickets_render_in_the_queue_format(self):
        self.reserve("2", "fold26", "w4")
        self.handoff("w4", "claude/x", "abc1234", block="2", extra=["--base", "1416c6d3"])
        self.cli(["ticket", "claim", "T0002-w4"], role="integrator", seat="w3")
        exported = self.cli(["export-queue"]).stdout
        self.assertEqual(exported,
                         "### claude/x @ abc1234  (w4, base 1416c6d3; block 2 of batch fold26)  status: queued (claimed by w3)\n"
                         "- intent: do claude/x\n- touched: src/x.cpp\n- tests: x_tests\n- risks: never compiled\n")
        entries, _ = sc.parse_queue(exported)
        ticket = sc.ticket_from_queue_entry(entries[0], "x.md")
        self.assertEqual((ticket["status"], ticket["claimed_by"], ticket["block"], ticket["batch"], ticket["base"]),
                         ("claimed", "w3", "2", "fold26", "1416c6d3"))

    def test_import_is_idempotent_and_orchestrator_only(self):
        path, _ = self.import_text(SAMPLE_QUEUE)
        again = run(self.project, ["import-queue", path, "--apply"], role="orchestrator", seat="o1")
        self.assertIn("3 already imported", again.stdout)
        self.assertEqual(len(self.all_tickets()), 3)
        refused = run(self.project, ["import-queue", path, "--apply"], role="coder", seat="w4")
        self.assertIn("only the orchestrator", refused.stderr)

    @unittest.skipUnless(os.path.isfile(REAL_QUEUE), "the owner's INTEGRATION_QUEUE.md is not on this machine")
    def test_round_trip_on_a_copy_of_the_real_queue(self):
        copy_path = os.path.join(self.project, "INTEGRATION_QUEUE.copy.md")
        shutil.copyfile(REAL_QUEUE, copy_path)
        before = os.stat(REAL_QUEUE).st_mtime_ns
        with open(copy_path, encoding="utf-8") as handle:
            text = handle.read()
        original, skipped, _report = self.assert_round_trip(text)
        self.assertGreater(len(original), 50)
        statuses = {ticket["status"] for ticket in self.all_tickets()}
        self.assertTrue({"open", "batched", "folded"} <= statuses, statuses)
        self.assertEqual(os.stat(REAL_QUEUE).st_mtime_ns, before, "the real queue must not be touched")


if __name__ == "__main__":
    unittest.main()
