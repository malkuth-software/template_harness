"""Tests for SushiBuild. Run: python3 -m unittest discover -s plugins/sushibuild/tests

No compiler is needed: builds are simulated with `cat <sample log>; exit N`.
"""

import atexit
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "sushibuild.py")
LOGS = os.path.join(HERE, "logs")
sys.path.insert(0, os.path.dirname(SCRIPT))
import sushibuild  # noqa: E402

# Every test runs with its own HOME, so a real ~/.sushi/sushibuild.json (the user layer) never leaks in.
ISOLATED_HOME = tempfile.mkdtemp(prefix="sushibuild-home-")
os.environ["HOME"] = ISOLATED_HOME
atexit.register(shutil.rmtree, ISOLATED_HOME, True)


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def load_json(path):
    return json.loads(read_text(path))


def classify(name, exit_code=1, base=None):
    with open(os.path.join(LOGS, name), encoding="utf-8") as handle:
        return sushibuild.classify_text(handle.read(), exit_code, base=base)


def run_cli(args, cwd, stdin=None):
    process = subprocess.run([sys.executable, SCRIPT] + args, cwd=cwd, input=stdin,
                             capture_output=True, text=True, timeout=60)
    return process


def run_json(args, cwd):
    process = run_cli(args, cwd)
    return process.returncode, json.loads(process.stdout)


class ClassifyTests(unittest.TestCase):
    def test_missing_include_cascade_is_small(self):
        # Real log from the 2026-11-11 live trial: a missing <vector> gives four errors in one
        # file, the later ones unrecognized fallout. The first error decides the kind.
        result = classify("clang_missing_include_cascade.log")
        self.assertEqual(result["prelim"], "small", result["reasons"])
        files = sorted(os.path.basename(unit["file"]) for unit in result["units"])
        self.assertIn("util.cpp", files)

    def test_clang_typo_is_small_with_hint(self):
        result = classify("clang_typo.log")
        self.assertEqual(result["prelim"], "small")
        self.assertEqual(len(result["units"]), 1)
        unit = result["units"][0]
        self.assertEqual(unit["file"], "/Users/dev/proj/src/main.cpp")
        self.assertEqual(unit["errors"][0]["line"], 12)
        self.assertEqual(unit["errors"][0]["col"], 5)
        self.assertEqual(unit["hint"], "did you mean cout")
        # the note pointing into libc++ is attached, not counted as an error file
        self.assertTrue(any("cout' declared here" in note for note in unit["errors"][0]["notes"]))

    def test_missing_project_include_is_small_and_records_include_stack(self):
        result = classify("clang_missing_include.log")
        self.assertEqual(result["prelim"], "small")
        error = result["units"][0]["errors"][0]
        self.assertEqual(error["severity"], "fatal error")
        self.assertEqual(error["file"], "src/widget.h")
        self.assertEqual(error["included_from"], ["src/widget.cpp:3"])

    def test_missing_std_include_gets_include_hint(self):
        result = classify("clang_missing_std_include.log")
        self.assertEqual(result["prelim"], "small")
        self.assertEqual(result["units"][0]["hint"], "add #include <vector>")

    def test_relative_paths_resolve_against_base(self):
        result = classify("gcc_typo.log", base="/work/proj")
        self.assertEqual(result["units"][0]["abs_file"], "/work/proj/src/parser.cpp")

    def test_gcc_typo_and_semicolon_one_file_small(self):
        result = classify("gcc_typo.log")
        self.assertEqual(result["prelim"], "small")
        unit = result["units"][0]
        self.assertEqual([e["line"] for e in unit["errors"]], [27, 31])
        self.assertEqual(unit["hint"], "did you mean length")

    def test_apple_ld_multi_file_undefined_is_big(self):
        result = classify("apple_ld_multi.log")
        self.assertEqual(result["prelim"], "big")
        symbols = {entry["symbol"]: entry["referenced_from"] for entry in result["linker"]}
        self.assertIn("layout::measure(Box const&)", symbols)
        self.assertEqual(len(symbols["layout::measure(Box const&)"]), 2)
        self.assertIn("Scene::Scene(int)", symbols)
        self.assertTrue(result["reasons"][0].startswith("linker errors"))

    def test_gnu_ld_undefined_and_multiple_definition_is_big(self):
        result = classify("gnu_ld.log")
        self.assertEqual(result["prelim"], "big")
        kinds = sorted({entry["kind"] for entry in result["linker"]})
        self.assertEqual(kinds, ["duplicate", "undefined"])

    def test_lld_is_big_with_references(self):
        result = classify("lld.log")
        self.assertEqual(result["prelim"], "big")
        self.assertEqual(result["linker"][0]["referenced_from"], ["main.cpp:9 (src/main.cpp:9)", "render.cpp:20 (src/render.cpp:20)"])

    def test_clang_template_cascade_is_big(self):
        result = classify("clang_template_cascade.log")
        self.assertEqual(result["prelim"], "big")
        self.assertGreaterEqual(result["counts"]["template_notes"], 3)
        self.assertTrue(any("template instantiation cascade" in r for r in result["reasons"]))

    def test_gcc_template_cascade_is_big(self):
        result = classify("gcc_template.log")
        self.assertEqual(result["prelim"], "big")
        self.assertTrue(any("template" in r for r in result["reasons"]))

    def test_msvc_compile_errors_small(self):
        result = classify("msvc.log")
        self.assertEqual(result["prelim"], "small")
        self.assertEqual(result["counts"]["warnings"], 1)
        unit = result["units"][0]
        self.assertEqual(unit["file"], "C:\\src\\gui_forms\\src\\form_layout.cpp")
        self.assertEqual(unit["errors"][0]["code"], "C2065")
        self.assertEqual(unit["errors"][0]["col"], 17)
        self.assertIsNone(unit["errors"][1]["col"])
        self.assertNotIn("vcxproj", unit["errors"][0]["message"])

    def test_msvc_linker_is_big(self):
        result = classify("msvc_link.log")
        self.assertEqual(result["prelim"], "big")
        self.assertEqual(result["linker"][0]["code"], "LNK2019")
        self.assertEqual(result["linker"][0]["symbol"], "public: int __cdecl Layout::measure(class Box const &)")

    def test_errors_across_many_files_is_big(self):
        result = classify("many_files.log")
        self.assertEqual(result["prelim"], "big")
        self.assertTrue(any("errors in 4 files" in r for r in result["reasons"]))

    def test_cmake_error_without_diagnostics_is_big(self):
        result = classify("cmake_error.log")
        self.assertEqual(result["prelim"], "big")

    def test_success_is_none(self):
        result = classify("success.log", exit_code=0)
        self.assertEqual(result["prelim"], "none")

    def test_nonzero_exit_without_anything_is_big(self):
        result = sushibuild.classify_text("something odd happened\n", 2)
        self.assertEqual(result["prelim"], "big")

    def test_duplicate_diagnostics_from_two_tus_are_deduped(self):
        text = ("In file included from a.cpp:1:\nsrc/x.h:3:5: error: unknown type name 'Strng'\n"
                "In file included from b.cpp:1:\nsrc/x.h:3:5: error: unknown type name 'Strng'\n")
        result = sushibuild.classify_text(text, 1)
        self.assertEqual(result["counts"]["errors"], 1)
        self.assertEqual(result["prelim"], "small")

    def test_ansi_colours_are_stripped(self):
        text = "\x1b[1msrc/a.cpp:3:5: \x1b[0;1;31merror: \x1b[0m\x1b[1muse of undeclared identifier 'x'\x1b[0m\n"
        result = sushibuild.classify_text(text, 1)
        self.assertEqual(result["units"][0]["file"], "src/a.cpp")

    def test_cli_compact_output(self):
        process = run_cli(["classify", os.path.join(LOGS, "clang_typo.log"), "--compact", "--exit", "1", "--no-local-check"], cwd=HERE)
        data = json.loads(process.stdout)
        self.assertEqual(data["prelim"], "small")
        self.assertNotIn("diagnostics", data)

    def test_cli_local_check_makes_missing_file_big(self):
        # /Users/dev/proj/src/main.cpp does not exist here, so it can't be fixed locally
        process = run_cli(["classify", os.path.join(LOGS, "clang_typo.log"), "--compact", "--exit", "1"], cwd=HERE)
        data = json.loads(process.stdout)
        self.assertEqual(data["prelim"], "big")
        self.assertIn("file not on this machine", data["units"][0]["reasons"][0])


class ProjectCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sushibuild-test-")
        self.root = os.path.realpath(self.tmp)
        os.makedirs(os.path.join(self.root, ".sushi"))
        os.makedirs(os.path.join(self.root, "src"))
        self.main = os.path.join(self.root, "src", "main.cpp")
        with open(self.main, "w") as handle:
            handle.write("#include <iostream>\nint main() {\n    cnout << 1;\n}\n")
        self.other = os.path.join(self.root, "src", "other.cpp")
        with open(self.other, "w") as handle:
            handle.write("int other() { return 1; }\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, **values):
        with open(os.path.join(self.root, ".sushi", "sushibuild.json"), "w") as handle:
            json.dump(values, handle)

    def state(self, *parts):
        return os.path.join(self.root, ".sushi", "state", "sushibuild", *parts)

    def fake_build(self, log_name, exit_code):
        return "cat %s; exit %d" % (shlex.quote(os.path.join(LOGS, log_name)), exit_code)


class StateTests(ProjectCase):
    def test_round_trip_lock_log_unlock_summary(self):
        code, out = run_json(["begin", "--command", self.fake_build("clang_typo.log", 1)], self.root)
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        code, out = run_json(["lock", self.main, "--fix", "line 3: cnout -> std::cout", "--error", "main.cpp:3:5: use of undeclared identifier 'cnout'"], self.root)
        self.assertEqual(code, 0, out)
        lock = load_json(self.state("fixlock.json"))
        self.assertEqual(lock["realpath"], os.path.realpath(self.main))
        # simulate the fixer's edit
        with open(self.main, "w") as handle:
            handle.write("#include <iostream>\nint main() {\n    std::cout << 1;\n}\n")
        code, out = run_json(["log", "--file", self.main, "--error", "main.cpp:3:5: use of undeclared identifier 'cnout'",
                              "--fix", "line 3: cnout -> std::cout", "--diff-stat", "+1 -1"], self.root)
        self.assertEqual(out["diff_stat"], "+1 -1")
        self.assertEqual(out["verdict"], "applied")
        self.assertFalse(out["repeat"])
        self.assertEqual(out["next"], "unlock, then run")
        code, out = run_json(["unlock"], self.root)
        self.assertTrue(out["was_locked"])
        self.assertFalse(os.path.exists(self.state("fixlock.json")))
        records = [json.loads(line) for line in read_text(self.state("fixups.jsonl")).splitlines()]
        self.assertEqual(len(records), 1)
        self.assertIn("+    std::cout << 1;", records[0]["diff"])
        process = run_cli(["summary", "--outcome", "success"], self.root)
        text = process.stdout
        self.assertTrue(text.startswith("=== SUSHIBUILD RESULT ===\noutcome: success\n"))
        self.assertIn("1. src/main.cpp [applied, +1 -1]", text)
        self.assertIn("fix:   line 3: cnout -> std::cout", text)
        self.assertTrue(text.rstrip().endswith("=== END SUSHIBUILD ==="))
        self.assertNotIn("log excerpt", text)
        self.assertEqual(read_text(self.state("last-result.txt")), text)

    def test_log_after_unlock_still_diffs(self):
        run_json(["begin", "--command", "true"], self.root)
        run_json(["lock", self.main, "--fix", "x"], self.root)
        with open(self.main, "a") as handle:
            handle.write("// added\n")
        run_json(["unlock"], self.root)
        code, out = run_json(["log", "--file", self.main, "--error", "e"], self.root)
        self.assertEqual(out["diff_stat"], "+1 -0")

    def test_no_change_means_escalate(self):
        run_json(["begin", "--command", "true"], self.root)
        run_json(["lock", self.main, "--fix", "x"], self.root)
        code, out = run_json(["log", "--file", self.main, "--error", "e"], self.root)
        self.assertEqual(out["verdict"], "no-change")
        self.assertEqual(out["next"], "escalate")

    def test_lock_refuses_relative_missing_and_double(self):
        run_json(["begin", "--command", "true"], self.root)
        code, out = run_json(["lock", "src/main.cpp", "--fix", "x"], self.root)
        self.assertEqual(code, 2)
        code, out = run_json(["lock", os.path.join(self.root, "nope.cpp"), "--fix", "x"], self.root)
        self.assertEqual(code, 2)
        self.assertEqual(run_json(["lock", self.main, "--fix", "x"], self.root)[0], 0)
        code, out = run_json(["lock", self.other, "--fix", "x"], self.root)
        self.assertEqual(code, 2)
        self.assertIn("already locked", out["error"])

    def test_max_fixes_cap(self):
        self.write_config(max_fixes=2)
        run_json(["begin", "--command", "true"], self.root)
        for index in range(2):
            self.assertEqual(run_json(["lock", self.main, "--fix", "f%d" % index], self.root)[0], 0)
            run_json(["log", "--file", self.main, "--error", "error %d" % index, "--verdict", "applied"], self.root)
            run_json(["unlock"], self.root)
        code, out = run_json(["lock", self.main, "--fix", "f3"], self.root)
        self.assertEqual(code, 3)
        self.assertIn("max_fixes", out["cap_hit"][0])

    def test_wall_time_cap(self):
        self.write_config(max_wall_minutes=0)
        run_json(["begin", "--command", "true"], self.root)
        code, out = run_json(["lock", self.main, "--fix", "x"], self.root)
        self.assertEqual(code, 3)
        self.assertIn("wall_time", out["cap_hit"][0])
        code, out = run_json(["run"], self.root)
        self.assertEqual(code, 3)

    def test_same_error_logged_twice_is_repeat(self):
        run_json(["begin", "--command", "true"], self.root)
        run_json(["log", "--file", self.main, "--error", "main.cpp:3:5: use of undeclared identifier 'cnout'", "--verdict", "applied"], self.root)
        code, out = run_json(["log", "--file", self.main, "--error", "main.cpp:4:9: use of undeclared identifier 'cnout'", "--verdict", "applied"], self.root)
        self.assertTrue(out["repeat"])
        self.assertEqual(out["next"], "escalate")

    def test_begin_needs_a_command(self):
        code, out = run_json(["begin"], self.root)
        self.assertEqual(code, 2)
        self.write_config(build_command="true")
        code, out = run_json(["begin"], self.root)
        self.assertEqual(code, 0)
        self.assertEqual(out["command"], "true")

    def test_begin_clears_previous_run(self):
        run_json(["begin", "--command", "true"], self.root)
        run_json(["log", "--file", self.main, "--error", "e", "--verdict", "applied"], self.root)
        subprocess.run([sys.executable, SCRIPT, "summary", "--outcome", "success"], cwd=self.root,
                       capture_output=True, check=True)
        run_json(["begin", "--command", "true"], self.root)
        self.assertFalse(os.path.exists(self.state("fixups.jsonl")))


class RunTests(ProjectCase):
    def test_run_success(self):
        run_json(["begin", "--command", self.fake_build("success.log", 0)], self.root)
        code, out = run_json(["run"], self.root)
        self.assertTrue(out["ok"])
        self.assertEqual(out["prelim"], "none")
        self.assertEqual(out["build"], 1)
        self.assertTrue(os.path.isfile(self.state("build-1.log")))

    def test_run_small_then_repeat_becomes_big(self):
        log = os.path.join(self.root, "fake.log")
        with open(log, "w") as handle:
            handle.write("src/main.cpp:3:5: error: use of undeclared identifier 'cnout'; did you mean 'cout'?\n1 error generated.\n")
        run_json(["begin", "--command", "cat %s; exit 1" % shlex.quote(log)], self.root)
        code, out = run_json(["run"], self.root)
        self.assertEqual(out["prelim"], "small")
        unit = out["units"][0]
        self.assertEqual(unit["abs_file"], os.path.join(self.root, "src", "main.cpp"))
        run_json(["lock", unit["abs_file"], "--fix", "cnout -> cout", "--error", unit["first_error"]], self.root)
        run_json(["log", "--file", unit["abs_file"], "--error", unit["first_error"], "--verdict", "applied"], self.root)
        run_json(["unlock"], self.root)
        code, out = run_json(["run"], self.root)  # the same error comes back
        self.assertEqual(out["prelim"], "big")
        self.assertTrue(out["repeat_errors"])
        self.assertIn("same error came back", out["reasons"][0])

    def test_run_refuses_while_locked(self):
        run_json(["begin", "--command", "true"], self.root)
        run_json(["lock", self.main, "--fix", "x"], self.root)
        code, out = run_json(["run"], self.root)
        self.assertEqual(code, 2)

    def test_escalated_summary_has_log_excerpt(self):
        run_json(["begin", "--command", self.fake_build("apple_ld_multi.log", 1)], self.root)
        run_json(["run"], self.root)
        text = run_cli(["summary", "--outcome", "escalated", "--reason", "linker errors"], self.root).stdout
        self.assertIn("outcome: escalated", text)
        self.assertIn("reason: linker errors", text)
        self.assertIn("fixups: none", text)
        self.assertIn("log excerpt:", text)
        self.assertIn("Undefined symbols for architecture arm64", text)


M4_PREFIX = "/Users/builder/Developer/Projects/gui_forms_workers/"


class PathMapTests(ProjectCase):
    """The owner's case: m4ws rsyncs ~/Developer/gui_forms_workers/wN/ on the laptop to
    /Users/builder/Developer/Projects/gui_forms_workers/wN/ on the M4, so the
    compiler prints M4 paths."""

    def setUp(self):
        super().setUp()
        self.local_workers = os.path.join(self.root, "gui_forms_workers")
        os.makedirs(os.path.join(self.local_workers, "w4", "src"))
        self.layout = os.path.join(self.local_workers, "w4", "src", "form_layout.cpp")
        with open(self.layout, "w") as handle:
            handle.write("int f() {\n    return heigth;\n}\n")
        self.config = dict(sushibuild.DEFAULT_CONFIG, path_map=[[M4_PREFIX, self.local_workers + "/"]])
        self.m4_layout = M4_PREFIX + "w4/src/form_layout.cpp"

    def test_m4_path_maps_to_local_and_stays_small(self):
        text = ("In file included from %sw4/src/form.cpp:3:\n"
                "%s:2:12: error: use of undeclared identifier 'heigth'; did you mean 'height'?\n"
                "%sw4/src/form_layout.h:9:9: note: 'height' declared here\n" % (M4_PREFIX, self.m4_layout, M4_PREFIX))
        result = sushibuild.classify_text(text, 1, base=self.root, config=self.config, check_local=True)
        self.assertEqual(result["prelim"], "small", result["reasons"])
        unit = result["units"][0]
        self.assertEqual(unit["abs_file"], self.layout)
        error = unit["errors"][0]
        self.assertEqual(error["printed_file"], self.m4_layout)
        self.assertEqual(error["included_from"], [os.path.join(self.local_workers, "w4/src/form.cpp") + ":3"])
        self.assertIn(os.path.join(self.local_workers, "w4/src/form_layout.h"), error["notes"][0])
        self.assertEqual(sushibuild.compact_classification(result)["units"][0]["printed_file"], self.m4_layout)

    def test_unmapped_remote_path_is_big(self):
        text = "/Users/someoneelse/tree/src/a.cpp:3:5: error: use of undeclared identifier 'x'\n"
        result = sushibuild.classify_text(text, 1, base=self.root, config=self.config, check_local=True)
        self.assertEqual(result["prelim"], "big")
        self.assertIn("file not on this machine", result["units"][0]["reasons"][0])
        self.assertIn("path_map", result["units"][0]["reasons"][0])

    def test_mapped_but_missing_locally_is_big(self):
        text = "%sw4/src/not_synced.cpp:3:5: error: use of undeclared identifier 'x'\n" % M4_PREFIX
        result = sushibuild.classify_text(text, 1, base=self.root, config=self.config, check_local=True)
        self.assertEqual(result["prelim"], "big")
        self.assertIn("printed as %sw4/src/not_synced.cpp" % M4_PREFIX, result["units"][0]["reasons"][0])

    def test_prefix_matches_only_at_component_boundary_and_longest_first(self):
        pairs = sushibuild.normalized_path_map([["/r/w4", "/l/w4"], ["/r", "/l/base"], ["/x/", "~/y/"]])
        self.assertEqual(sushibuild.map_path("/r/w4/a.cpp", pairs), "/l/w4/a.cpp")
        self.assertEqual(sushibuild.map_path("/r/w40/a.cpp", pairs), "/l/base/w40/a.cpp")
        self.assertEqual(sushibuild.map_path("/rx/a.cpp", pairs), "/rx/a.cpp")
        self.assertEqual(sushibuild.map_path("/x/a.cpp", pairs), os.path.expanduser("~/y/a.cpp"))
        self.assertEqual(sushibuild.map_path("src/a.cpp", pairs), "src/a.cpp")

    def test_linker_references_are_mapped(self):
        text = ("ld.lld: error: undefined symbol: f()\n>>> referenced by form.cpp:9 (%sw4/src/form.cpp:9)\n" % M4_PREFIX)
        result = sushibuild.classify_text(text, 1, config=self.config)
        self.assertIn(self.local_workers, result["linker"][0]["referenced_from"][0])

    def test_run_lock_log_summary_with_m4_paths(self):
        self.write_config(path_map=[[M4_PREFIX, self.local_workers + "/"]])
        log = os.path.join(self.root, "m4.log")
        with open(log, "w") as handle:
            handle.write("%s:2:12: error: use of undeclared identifier 'heigth'\n1 error generated.\n" % self.m4_layout)
        run_json(["begin", "--command", "cat %s; exit 1" % shlex.quote(log)], self.root)
        code, out = run_json(["run"], self.root)
        self.assertEqual(out["prelim"], "small", out["reasons"])
        self.assertEqual(out["units"][0]["abs_file"], self.layout)
        # the triage agent may pass the printed (M4) path: lock maps it to the local file
        code, out = run_json(["lock", self.m4_layout, "--fix", "heigth -> height"], self.root)
        self.assertEqual(code, 0, out)
        self.assertEqual(out["locked"], self.layout)
        with open(self.layout, "w") as handle:
            handle.write("int f() {\n    return height;\n}\n")
        code, out = run_json(["log", "--file", self.m4_layout, "--error", "%s:2:12: use of undeclared identifier 'heigth'" % self.m4_layout], self.root)
        self.assertEqual(out["diff_stat"], "+1 -1")
        run_json(["unlock"], self.root)
        text = run_cli(["summary", "--outcome", "escalated", "--reason", "test"], self.root).stdout
        self.assertNotIn(M4_PREFIX, text)
        self.assertIn(os.path.join(self.local_workers, "w4/src/form_layout.cpp:2:12"), text)

    def test_hook_allows_fixer_on_mapped_lock(self):
        self.write_config(path_map=[[M4_PREFIX, self.local_workers + "/"]])
        run_json(["begin", "--command", "true"], self.root)
        run_json(["lock", self.m4_layout, "--fix", "x"], self.root)
        payload = {"tool_name": "Edit", "tool_input": {"file_path": self.layout}, "cwd": self.root,
                   "agent_type": "sushibuild:sushi-fixer", "agent_id": "a"}
        self.assertIsNone(sushibuild.hook_decision(payload, env={}))
        payload["tool_input"]["file_path"] = self.m4_layout  # the remote path is not a local file
        self.assertIsNotNone(sushibuild.hook_decision(payload, env={}))


class DetachTests(ProjectCase):
    def small_log(self):
        log = os.path.join(self.root, "slow.log")
        with open(log, "w") as handle:
            handle.write("src/main.cpp:3:5: error: use of undeclared identifier 'cnout'\n1 error generated.\n")
        return log

    def test_detach_then_wait_running_then_done(self):
        command = "echo starting; sleep 2; cat %s; exit 1" % shlex.quote(self.small_log())
        run_json(["begin", "--command", command], self.root)
        started = time.time()
        code, out = run_json(["run", "--detach"], self.root)
        self.assertLess(time.time() - started, 1.8)
        self.assertTrue(out["detached"])
        self.assertEqual(out["build"], 1)
        run = load_json(self.state("run.json"))
        self.assertEqual(run["detached"]["pid"], out["pid"])
        code, out = run_json(["wait", "--max", "0.3"], self.root)
        self.assertEqual(out["status"], "running")
        self.assertIn("log_tail", out)
        code, out = run_json(["run"], self.root)  # no second build while one runs
        self.assertEqual(code, 2)
        self.assertIn("detached build is still running", out["error"])
        code, out = run_json(["wait", "--max", "20"], self.root)
        self.assertEqual(out["status"], "done")
        self.assertEqual(out["exit"], 1)
        self.assertEqual(out["prelim"], "small")
        self.assertEqual(out["units"][0]["abs_file"], self.main)
        log_text = read_text(out["log"])
        self.assertTrue(log_text.startswith("$ echo starting"))
        self.assertIn("starting\n", log_text)
        self.assertNotIn("detached", load_json(self.state("run.json")))
        code, out = run_json(["wait"], self.root)
        self.assertEqual(code, 2)

    def test_detach_success(self):
        run_json(["begin", "--command", "sleep 1; echo built"], self.root)
        run_json(["run", "--detach"], self.root)
        code, out = run_json(["wait", "--max", "20"], self.root)
        self.assertEqual((out["status"], out["ok"], out["exit"], out["prelim"]), ("done", True, 0, "none"))

    def test_detached_build_timeout_kills_its_group(self):
        self.write_config(build_timeout_s=1)
        run_json(["begin", "--command", "sleep 30"], self.root)
        code, out = run_json(["run", "--detach"], self.root)
        pid = out["pid"]
        code, out = run_json(["wait", "--max", "15"], self.root)
        self.assertEqual(out["status"], "done")
        self.assertTrue(out["timed_out"])
        self.assertEqual(out["prelim"], "big")
        deadline = time.time() + 5
        alive = True
        while time.time() < deadline and alive:
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                alive = False
        self.assertFalse(alive, "the timed-out build's process group should be gone")

    def test_wait_max_is_capped(self):
        run_json(["begin", "--command", "sleep 1"], self.root)
        run_json(["run", "--detach"], self.root)
        code, out = run_json(["wait", "--max", "99999"], self.root)  # capped at 590, returns when done
        self.assertEqual(out["status"], "done")


class HookTests(ProjectCase):
    FIXER = "sushibuild:sushi-fixer"
    BUILD = "sushibuild:sushi-build"

    def lock(self, path=None):
        path = path or self.main
        os.makedirs(self.state(), exist_ok=True)
        with open(self.state("fixlock.json"), "w") as handle:
            json.dump({"path": path, "realpath": os.path.realpath(path), "fix": "x"}, handle)

    def payload(self, tool, tool_input, agent_type=None, agent_id="a1", cwd=None):
        data = {"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": tool,
                "tool_input": tool_input, "cwd": cwd or self.root}
        if agent_type is not None:
            data["agent_type"] = agent_type
        if agent_id is not None:
            data["agent_id"] = agent_id
        return data

    def decide(self, *args, **kwargs):
        return sushibuild.hook_decision(self.payload(*args, **kwargs), env={})

    def assertDenied(self, result):
        self.assertIsNotNone(result)
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")

    # fixer, edits
    def test_fixer_locked_absolute_path_passes(self):
        self.lock()
        self.assertIsNone(self.decide("Edit", {"file_path": self.main}, self.FIXER))

    def test_fixer_other_path_denied(self):
        self.lock()
        result = self.decide("Edit", {"file_path": self.other}, self.FIXER)
        self.assertDenied(result)
        self.assertIn(self.main, result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_fixer_write_and_multiedit_and_notebook(self):
        self.lock()
        self.assertIsNone(self.decide("Write", {"file_path": self.main, "content": ""}, self.FIXER))
        self.assertDenied(self.decide("MultiEdit", {"file_path": self.other, "edits": []}, self.FIXER))
        self.assertDenied(self.decide("NotebookEdit", {"notebook_path": os.path.join(self.root, "n.ipynb")}, self.FIXER))
        self.assertDenied(self.decide("Edit", {}, self.FIXER))

    def test_fixer_relative_paths(self):
        self.lock()
        self.assertIsNone(self.decide("Edit", {"file_path": "src/main.cpp"}, self.FIXER))
        self.assertIsNone(self.decide("Edit", {"file_path": "main.cpp"}, self.FIXER, cwd=os.path.join(self.root, "src")))
        self.assertIsNone(self.decide("Edit", {"file_path": "src/../src/main.cpp"}, self.FIXER))
        self.assertDenied(self.decide("Edit", {"file_path": "src/other.cpp"}, self.FIXER))
        self.assertDenied(self.decide("Edit", {"file_path": "main.cpp"}, self.FIXER))  # relative to root: no such file

    def test_fixer_symlinks(self):
        self.lock()
        link_to_locked = os.path.join(self.root, "alias.cpp")
        os.symlink(self.main, link_to_locked)
        link_to_other = os.path.join(self.root, "src", "sneaky.cpp")
        os.symlink(self.other, link_to_other)
        # editing through a symlink that resolves to the locked file edits the locked file
        self.assertIsNone(self.decide("Edit", {"file_path": link_to_locked}, self.FIXER))
        # a symlink that resolves elsewhere is the other file
        self.assertDenied(self.decide("Edit", {"file_path": link_to_other}, self.FIXER))

    def test_fixer_symlinked_directory_spelling(self):
        # lock via the real path; edit via a symlinked parent directory (like /tmp -> /private/tmp)
        alias_dir = os.path.join(tempfile.gettempdir(), "sushibuild-alias-%d" % os.getpid())
        os.symlink(self.root, alias_dir)
        self.addCleanup(os.remove, alias_dir)
        self.lock()
        self.assertIsNone(self.decide("Edit", {"file_path": os.path.join(alias_dir, "src", "main.cpp")}, self.FIXER))
        self.assertDenied(self.decide("Edit", {"file_path": os.path.join(alias_dir, "src", "other.cpp")}, self.FIXER))

    def test_lock_on_symlink_restricts_to_target(self):
        link = os.path.join(self.root, "alias.cpp")
        os.symlink(self.main, link)
        self.lock(link)
        self.assertIsNone(self.decide("Edit", {"file_path": self.main}, self.FIXER))

    def test_fixer_without_lock_denied(self):
        self.assertDenied(self.decide("Edit", {"file_path": self.main}, self.FIXER))

    def test_bare_fixer_name_also_matches(self):
        self.lock()
        self.assertDenied(self.decide("Edit", {"file_path": self.other}, "sushi-fixer"))

    def test_fixer_read_restricted(self):
        self.lock()
        self.assertIsNone(self.decide("Read", {"file_path": self.main}, self.FIXER))
        self.assertDenied(self.decide("Read", {"file_path": self.other}, self.FIXER))

    # non-fixer callers pass untouched
    def test_main_session_untouched_even_while_locked(self):
        self.lock()
        self.assertIsNone(self.decide("Edit", {"file_path": self.other}, None, agent_id=None))
        self.assertIsNone(self.decide("Bash", {"command": "make"}, None, agent_id=None))
        self.assertIsNone(self.decide("Read", {"file_path": self.other}, None, agent_id=None))

    def test_other_subagent_untouched_while_locked(self):
        self.lock()
        self.assertIsNone(self.decide("Edit", {"file_path": self.other}, "Explore"))
        self.assertIsNone(self.decide("Edit", {"file_path": self.other}, "gui-forms-coder"))

    def test_fallback_without_agent_type(self):
        self.lock()
        self.assertDenied(self.decide("Edit", {"file_path": self.other}, None, agent_id="a9"))
        self.assertIsNone(self.decide("Edit", {"file_path": self.main}, None, agent_id="a9"))
        os.remove(self.state("fixlock.json"))
        self.assertIsNone(self.decide("Edit", {"file_path": self.other}, None, agent_id="a9"))

    # Bash
    def test_fixer_bash_denied(self):
        self.lock()
        self.assertDenied(self.decide("Bash", {"command": "ls"}, self.FIXER))

    def test_build_agent_bash(self):
        sb = "python3 /plugins/sushibuild/scripts/sushibuild.py"
        self.assertIsNone(self.decide("Bash", {"command": sb + " run"}, self.BUILD))
        self.assertIsNone(self.decide("Bash", {"command": sb + " begin --command 'ssh m4mini \"make -C /p && ninja\"'"}, self.BUILD))
        self.assertIsNone(self.decide("Bash", {"command": "grep -n error .sushi/state/sushibuild/build-1.log | head"}, self.BUILD))
        for command in ("make -j8", "ninja -C build", "ssh m4mini 'cd /p && cmake --build b'",
                        "sed -i '' s/cnout/cout/ src/main.cpp", "echo x > src/main.cpp",
                        "git checkout -- src/main.cpp", sb + " run && clang++ a.cpp",
                        "nice -n 10 xcodebuild", "bash -c 'g++ x.cpp'"):
            self.assertDenied(self.decide("Bash", {"command": command}, self.BUILD))

    def test_build_agent_writes_only_state(self):
        self.assertIsNone(self.decide("Write", {"file_path": self.state("notes.md")}, self.BUILD))
        self.assertDenied(self.decide("Write", {"file_path": self.main}, self.BUILD))
        self.assertDenied(self.decide("Edit", {"file_path": os.path.join(self.root, ".sushi", "sushibuild.json")}, self.BUILD))

    # Agent
    def test_build_agent_may_start_only_locked_foreground_fixer(self):
        call = {"subagent_type": self.FIXER, "prompt": "FILE: x", "description": "fix"}
        self.assertDenied(self.decide("Agent", call, self.BUILD))  # no lock yet
        self.lock()
        self.assertIsNone(self.decide("Agent", call, self.BUILD))
        self.assertIsNone(self.decide("Task", dict(call, subagent_type="sushi-fixer"), self.BUILD))
        self.assertDenied(self.decide("Agent", dict(call, subagent_type="general-purpose"), self.BUILD))
        self.assertDenied(self.decide("Agent", dict(call, run_in_background=True), self.BUILD))

    def test_fixer_may_not_start_agents(self):
        self.lock()
        self.assertDenied(self.decide("Agent", {"subagent_type": "general-purpose"}, self.FIXER))

    def test_others_agent_calls_untouched(self):
        self.assertIsNone(self.decide("Agent", {"subagent_type": "general-purpose"}, None, agent_id=None))

    # CLI: piping hook JSON
    def test_cli_hook_deny_and_pass(self):
        self.lock()
        denied = run_cli(["hook"], self.root, stdin=json.dumps(self.payload("Edit", {"file_path": self.other}, self.FIXER)))
        self.assertEqual(denied.returncode, 0)
        output = json.loads(denied.stdout)
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        passed = run_cli(["hook"], self.root, stdin=json.dumps(self.payload("Edit", {"file_path": self.main}, self.FIXER)))
        self.assertEqual((passed.returncode, passed.stdout), (0, ""))
        log = read_text(os.path.join(self.root, ".sushi", "logs", "sushibuild.log"))
        self.assertIn("hook deny tool=Edit", log)

    def test_cli_hook_never_breaks_on_garbage(self):
        for stdin in ("", "not json", "[]", json.dumps({"tool_name": "Edit", "tool_input": None})):
            process = run_cli(["hook"], self.root, stdin=stdin)
            self.assertEqual((process.returncode, process.stdout), (0, ""), stdin)

    def test_cli_hook_outside_any_project(self):
        empty = tempfile.mkdtemp(prefix="sushibuild-noproj-")
        self.addCleanup(shutil.rmtree, empty, True)
        process = run_cli(["hook"], empty, stdin=json.dumps(self.payload("Edit", {"file_path": "/x"}, "Explore", cwd=empty)))
        self.assertEqual((process.returncode, process.stdout), (0, ""))


class PluginLayoutTests(unittest.TestCase):
    ROOT = os.path.dirname(HERE)

    def frontmatter(self, name):
        text = read_text(os.path.join(self.ROOT, "agents", name))
        head = text.split("---")[1]
        return dict(line.split(":", 1) for line in head.strip().splitlines())

    def test_agents_frontmatter(self):
        build = self.frontmatter("sushi-build.md")
        self.assertEqual(build["name"].strip(), "sushi-build")
        self.assertEqual(build["model"].strip(), "haiku")
        self.assertEqual({t.strip() for t in build["tools"].split(",")}, {"Bash", "Read", "Grep", "Agent", "Write"})
        fixer = self.frontmatter("sushi-fixer.md")
        self.assertEqual(fixer["name"].strip(), "sushi-fixer")
        self.assertEqual(fixer["model"].strip(), "sonnet")
        self.assertEqual({t.strip() for t in fixer["tools"].split(",")}, {"Read", "Edit", "Write"})

    def test_hooks_json_and_manifest(self):
        hooks = load_json(os.path.join(self.ROOT, "hooks", "hooks.json"))
        entry = hooks["hooks"]["PreToolUse"][0]
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "Agent"):
            self.assertIn(tool, entry["matcher"].split("|"))
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", entry["hooks"][0]["command"])
        manifest = load_json(os.path.join(self.ROOT, ".claude-plugin", "plugin.json"))
        self.assertEqual(manifest["name"], "sushibuild")
        self.assertEqual(manifest["author"], {"name": "Malkuth Software"})


class ConfigLayers(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, ".sushi"))
        self.user = os.path.join(ISOLATED_HOME, ".sushi")
        os.makedirs(self.user, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.user, True)

    def write(self, directory, data):
        with open(os.path.join(directory, "sushibuild.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_user_layer_under_project(self):
        self.write(self.user, {"build_command": "m4ws w1 python3 app/ci_build.py", "path_map": [["/r/", "/l/"]],
                               "max_fixes": 3})
        self.write(os.path.join(self.root, ".sushi"), {"build_cwd": "..", "max_fixes": 5})
        config = sushibuild.load_config(self.root)
        self.assertEqual(config["build_command"], "m4ws w1 python3 app/ci_build.py")
        self.assertEqual(config["path_map"], [["/r/", "/l/"]])
        self.assertEqual(config["build_cwd"], "..")
        self.assertEqual(config["max_fixes"], 5)  # the project wins
        self.assertEqual(config["max_wall_minutes"], sushibuild.DEFAULT_CONFIG["max_wall_minutes"])

    def test_explicit_config_replaces_both(self):
        self.write(self.user, {"max_fixes": 3})
        explicit = os.path.join(self.root, "explicit.json")
        with open(explicit, "w", encoding="utf-8") as handle:
            json.dump({"max_small_files": 1}, handle)
        os.environ["SUSHIBUILD_CONFIG"] = explicit
        try:
            config = sushibuild.load_config(self.root)
        finally:
            del os.environ["SUSHIBUILD_CONFIG"]
        self.assertEqual(config["max_fixes"], sushibuild.DEFAULT_CONFIG["max_fixes"])
        self.assertEqual(config["max_small_files"], 1)


class GapTests(ProjectCase):
    """Gaps found while using SushiBuild (notes: desks/coordinator/fransushi/PLAN.md, G8 and G12-G15)."""

    def test_g12_relative_paths_resolve_against_the_entered_build_directory(self):
        build = os.path.join(self.root, "out", "build")
        os.makedirs(build)
        text = ("ninja: Entering directory `out/build'\n"
                "../../src/main.cpp:3:5: error: use of undeclared identifier 'cnout'\n")
        result = sushibuild.classify_text(text, 1, base=self.root, check_local=True, project_root=self.root)
        self.assertEqual(result["units"][0]["abs_file"], self.main)
        self.assertEqual(result["prelim"], "small", result["reasons"])

    def test_g12_make_directories_nest_and_leave(self):
        text = ("make[1]: Entering directory '%s/out'\n"
                "make[1]: Leaving directory '%s/out'\n"
                "src/main.cpp:3:5: error: use of undeclared identifier 'cnout'\n" % (self.root, self.root))
        result = sushibuild.classify_text(text, 1, base=self.root, check_local=True, project_root=self.root)
        self.assertEqual(result["units"][0]["abs_file"], self.main)

    def test_g12_a_file_outside_the_project_is_big(self):
        outside = tempfile.mkdtemp(prefix="sushibuild-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        stray = os.path.join(outside, "a.cpp")
        with open(stray, "w") as handle:
            handle.write("int a;\n")
        text = "%s:1:1: error: use of undeclared identifier 'x'\n" % stray
        result = sushibuild.classify_text(text, 1, base=self.root, check_local=True, project_root=self.root)
        self.assertEqual(result["prelim"], "big")
        self.assertIn("outside the project root", result["units"][0]["reasons"][0])

    def test_g12_lock_refuses_a_file_outside_the_project(self):
        outside = tempfile.mkdtemp(prefix="sushibuild-outside-")
        self.addCleanup(shutil.rmtree, outside, True)
        stray = os.path.join(outside, "a.cpp")
        with open(stray, "w") as handle:
            handle.write("int a;\n")
        run_json(["begin", "--command", "true"], self.root)
        code, out = run_json(["lock", stray, "--fix", "x"], self.root)
        self.assertEqual(code, 2)
        self.assertIn("outside the project root", out["error"])
        link = os.path.join(self.root, "src", "link.cpp")
        os.symlink(stray, link)
        code, out = run_json(["lock", link, "--fix", "x"], self.root)
        self.assertEqual(code, 2)
        self.assertIn("outside the project root", out["error"])

    def test_g12_lock_refuses_a_shared_tree(self):
        os.makedirs(os.path.join(self.root, ".git"))
        run_json(["begin", "--command", "true"], self.root)
        with open(os.path.join(self.root, ".git", "sushi-shared-tree"), "w") as handle:
            handle.write("marked\n")
        code, out = run_json(["lock", self.main, "--fix", "x"], self.root)
        self.assertEqual(code, 2)
        self.assertIn("shared working tree", out["error"])

    def test_g13_two_spellings_of_one_file_are_one_unit(self):
        os.makedirs(os.path.join(self.root, "build"))
        text = ("ninja: Entering directory `build'\n"
                "../src/main.cpp:3:5: error: use of undeclared identifier 'cnout'\n"
                "%s:4:1: error: expected ';' after expression\n" % self.main)
        config = dict(sushibuild.DEFAULT_CONFIG, max_small_files=1)
        result = sushibuild.classify_text(text, 1, base=self.root, config=config, check_local=True,
                                          project_root=self.root)
        self.assertEqual(len(result["units"]), 1)
        self.assertEqual(result["prelim"], "small", result["reasons"])
        self.assertEqual(len(result["units"][0]["spellings"]), 2)

    def test_g14_one_missing_include_in_many_files_is_one_fix_unit(self):
        names = []
        for name in ("a", "b", "c", "d"):
            path = os.path.join(self.root, "src", name + ".cpp")
            with open(path, "w") as handle:
                handle.write('#include "gone.hpp"\n')
            names.append(path)
        text = "".join("%s:1:10: fatal error: 'gone.hpp' file not found\n" % path for path in names)
        result = sushibuild.classify_text(text, 1, base=self.root, check_local=True, project_root=self.root)
        self.assertEqual(result["prelim"], "small", result["reasons"])
        self.assertEqual(result["counts"]["distinct_causes"], 1)
        compact = sushibuild.compact_classification(result)
        self.assertEqual(compact["units"][0]["same_cause_files"], 4)

    def test_g14_the_same_include_fix_in_many_files_counts_once_toward_max_fixes(self):
        self.write_config(max_fixes=2)
        run_json(["begin", "--command", "true"], self.root)
        for index, path in enumerate((self.main, self.other, self.main)):
            self.assertEqual(run_json(["lock", path, "--fix", "add #include <vector>"], self.root)[0], 0)
            run_json(["log", "--file", path, "--error", "%s:%d:1: error: no template named 'vector' in namespace "
                      "'std'; did you forget to '#include <vector>'?" % (path, index + 1),
                      "--fix", "add #include <vector>", "--verdict", "applied"], self.root)
            run_json(["unlock"], self.root)
        code, out = run_json(["status"], self.root)
        self.assertEqual(out["caps"]["fixes"], 1)
        self.assertEqual(out["caps"]["fixer_calls"], 3)
        self.assertEqual(out["caps"]["cap_hit"], [])

    def test_g14_result_accepts_a_well_formed_block_and_rejects_free_text(self):
        run_json(["begin", "--command", "true"], self.root)
        run_cli(["summary", "--outcome", "escalated", "--reason", "linker errors"], self.root)
        code, out = run_json(["result"], self.root)
        self.assertEqual(code, 0)
        self.assertEqual(out["fields"]["outcome"], "escalated")
        self.assertEqual(out["fields"]["reason"], "linker errors")
        loose = ("  ===  sushibuild result ===\n reason: x\n   OUTCOME :  Success \n"
                 "=== END SUSHIBUILD ===\n")
        process = run_cli(["result", "-"], self.root, stdin=loose)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(json.loads(process.stdout)["fields"]["outcome"], "success")
        process = run_cli(["result", "-"], self.root, stdin="The build failed on 5 undefined symbols.\n")
        self.assertEqual(process.returncode, 1)
        self.assertIn("ask the sushi-build agent", json.loads(process.stdout)["next"])
        process = run_cli(["result", "-"], self.root, stdin="=== SUSHIBUILD RESULT ===\noutcome: maybe\n"
                                                            "=== END SUSHIBUILD ===\n")
        self.assertEqual(process.returncode, 1)

    def test_g8_named_path_map_entries(self):
        pairs = sushibuild.normalized_path_map([{"remote": "/r/", "local": "/l/"}, ["/q/", "/p/"], {"local": "/x"}])
        self.assertEqual(sushibuild.map_path("/r/a.cpp", pairs), "/l/a.cpp")
        self.assertEqual(sushibuild.map_path("/q/a.cpp", pairs), "/p/a.cpp")
        self.assertEqual(len(pairs), 2)

    def test_g15_a_second_run_is_refused_while_one_is_active(self):
        self.assertEqual(run_json(["begin", "--command", "true"], self.root)[0], 0)
        run_json(["log", "--file", self.main, "--error", "e", "--verdict", "applied"], self.root)
        code, out = run_json(["begin", "--command", "false"], self.root)
        self.assertEqual(code, 4)
        self.assertIn("another SushiBuild run is active", out["error"])
        self.assertTrue(os.path.exists(self.state("fixups.jsonl")))  # the first run's state is untouched
        code, out = run_json(["begin", "--command", "false", "--takeover"], self.root)
        self.assertEqual(code, 0)
        run_cli(["summary", "--outcome", "failed"], self.root)
        self.assertEqual(run_json(["begin", "--command", "true"], self.root)[0], 0)

    def test_g15_an_abandoned_run_does_not_block(self):
        self.write_config(max_wall_minutes=0, build_timeout_s=0)
        run_json(["begin", "--command", "true"], self.root)
        self.assertEqual(run_json(["begin", "--command", "true"], self.root)[0], 0)

    def test_g7_project_option_anywhere_and_no_sushi_in_a_shared_tree(self):
        shared = tempfile.mkdtemp(prefix="sushibuild-shared-")
        self.addCleanup(shutil.rmtree, shared, True)
        os.makedirs(os.path.join(shared, ".git"))
        with open(os.path.join(shared, ".git", "sushi-shared-tree"), "w") as handle:
            handle.write("marked\n")
        code, out = run_json(["begin", "--command", "true"], shared)
        self.assertEqual(code, 2)
        self.assertIn("shared working tree", out["error"])
        self.assertFalse(os.path.exists(os.path.join(shared, ".sushi")))
        code, out = run_json(["--project", self.root, "begin", "--command", "true"], shared)
        self.assertEqual(code, 0)
        self.assertEqual(out["project_root"], self.root)
        code, out = run_json(["status", "--project=" + self.root], shared)
        self.assertEqual(out["project_root"], self.root)


if __name__ == "__main__":
    unittest.main()
