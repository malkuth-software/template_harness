"""Reuse analysis: ninja log parsing, changed-source counting, ccache stats, baselines, trees census."""

import json
import os
import subprocess
import sys
import time
import unittest

from helpers import SandboxCase, sushirice
from test_hook import write_cache

CCACHE_PRINT_STATS = "stats_updated_timestamp\t1731300000\ndirect_cache_hit\t40\npreprocessed_cache_hit\t10\ncache_miss\t150\n"
CCACHE_4_S = """Cacheable calls:   200 / 210 (95.24%)
  Hits:             50 / 200 (25.00%)
    Direct:         40 /  50 (80.00%)
    Preprocessed:   10 /  50 (20.00%)
  Misses:          150 / 200 (75.00%)
"""
CCACHE_3_S = """cache directory                     /Users/x/.ccache
cache hit (direct)                    40
cache hit (preprocessed)              10
cache miss                           150
"""


def synthetic_ninja_log(previous_end_ns, last_start_ns, objects_last=30):
    lines = ["# ninja log v5"]
    # previous build: 3 edges
    for index in range(3):
        lines.append("%d\t%d\t%d\tobj/prev%d.o\tabc%d" % (index * 100, index * 100 + 900, previous_end_ns, index, index))
    # last build: objects_last objects plus a link; end times rise
    for index in range(objects_last):
        end = 1000 + index * 50
        lines.append("%d\t%d\t%d\tobj/f%d.o\th%d" % (end - 400, end, last_start_ns + end * 10 ** 6, index, index))
    lines.append("%d\t%d\t%d\tbin/app\thlink" % (1000, 9000, last_start_ns + 9000 * 10 ** 6))
    return "\n".join(lines) + "\n"


class NinjaLogTests(unittest.TestCase):
    def test_parse_splits_builds(self):
        builds = sushirice.parse_ninja_log(synthetic_ninja_log(10 ** 18, 2 * 10 ** 18))
        self.assertEqual(len(builds), 2)
        self.assertEqual(len(builds[0]), 3)
        self.assertEqual(len(builds[1]), 31)

    def test_parse_ignores_garbage(self):
        builds = sushirice.parse_ninja_log("# ninja log v6\nbad line\n1\t2\t3\tx.o\th\n")
        self.assertEqual(len(builds), 1)

    def test_mtime_units(self):
        self.assertAlmostEqual(sushirice.mtime_seconds(1731300000), 1731300000)
        self.assertAlmostEqual(sushirice.mtime_seconds(1731300000 * 10 ** 9), 1731300000)


class CcacheTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(sushirice.parse_ccache_stats(CCACHE_PRINT_STATS), (50, 150))
        self.assertEqual(sushirice.parse_ccache_stats(CCACHE_4_S), (50, 150))
        self.assertEqual(sushirice.parse_ccache_stats(CCACHE_3_S), (50, 150))
        self.assertIsNone(sushirice.parse_ccache_stats(""))

    def test_low_hit_rate_warns(self):
        config = sushirice.load_config(None)
        warnings = sushirice.report_warnings(None, (50, 150), None, None, config)
        self.assertEqual(warnings, ["ccache hit rate 25% (50 of 200) since the last report"])
        self.assertEqual(sushirice.report_warnings(None, (190, 10), None, None, config), [])


class ReportTests(SandboxCase):
    def make_build(self, changed_files, objects=30):
        source = os.path.join(self.project, "src")
        os.makedirs(source)
        git = ["git", "-C", source, "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
        subprocess.run(["git", "-C", source, "init", "-q"], check=True)
        now = time.time()
        previous_end = now - 3600
        last_start = now - 600
        names = ["a.cpp", "b.cpp", "c.h", "d.cpp", "e.cpp"]
        for name in names:
            with open(os.path.join(source, name), "w") as handle:
                handle.write("// %s\n" % name)
            os.utime(os.path.join(source, name), (previous_end - 7200, previous_end - 7200))
        for name in names[:changed_files]:
            stamp = previous_end + 60  # changed between the two builds
            os.utime(os.path.join(source, name), (stamp, stamp))
        subprocess.run(git + ["add", "."], check=True)
        build = os.path.join(source, "build")
        write_cache(build, source)
        with open(os.path.join(build, ".ninja_log"), "w") as handle:
            handle.write(synthetic_ninja_log(int(previous_end * 10 ** 9), int(last_start * 10 ** 9), objects))
        return build

    def test_rebuilt_far_more_than_changed(self):
        build = self.make_build(changed_files=1, objects=30)
        result = sushirice.analyze_ninja(build, self.config())
        self.assertEqual(result["objects_rebuilt"], 30)
        self.assertEqual(result["changed_sources"], 1)
        self.assertEqual(result["longest"][0]["output"], "bin/app")
        warnings = sushirice.report_warnings(result, None, None, None, self.config())
        self.assertEqual(warnings, ["rebuilt 30 objects for 1 changed file"])

    def test_header_noted(self):
        build = self.make_build(changed_files=3, objects=40)
        result = sushirice.analyze_ninja(build, self.config())
        self.assertEqual(result["changed_sources"], 3)
        warnings = sushirice.report_warnings(result, None, None, None, self.config())
        self.assertEqual(warnings, ["rebuilt 40 objects for 3 changed files (1 header)"])

    def test_proportional_rebuild_is_quiet(self):
        build = self.make_build(changed_files=5, objects=30)
        result = sushirice.analyze_ninja(build, self.config())
        self.assertEqual(sushirice.report_warnings(result, None, None, None, self.config()), [])

    def test_baseline(self):
        runs = [{"cmd": "ninja", "exit": 0, "duration_s": d, "ts": "2026-11-0%dT00:00:00" % (i + 1)}
                for i, d in enumerate([20, 25, 30, 22])]
        current = {"cmd": "ninja", "exit": 0, "duration_s": 60, "ts": "2026-11-09T00:00:00"}
        runs.append(current)
        baseline = sushirice.baseline_for(runs, current, self.config())
        self.assertEqual(baseline, 23.5)
        warnings = sushirice.report_warnings(None, None, current, baseline, self.config())
        self.assertEqual(warnings, ["build took 60 s vs baseline 24 s (ninja)"])
        self.assertIsNone(sushirice.baseline_for(runs[:2] + [current], current, self.config()))

    def test_cli_report(self):
        build = self.make_build(changed_files=1, objects=30)
        result = self.run_cli("report", "--build-dir", build)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.decode()
        self.assertIn("30 objects rebuilt", output)
        self.assertIn("WARNING: rebuilt 30 objects for 1 changed file", output)
        as_json = json.loads(self.run_cli("report", "--build-dir", build, "--json").stdout)
        self.assertEqual(as_json["ninja"]["objects_rebuilt"], 30)

    def test_cli_report_uses_last_run(self):
        build = self.make_build(changed_files=1, objects=30)
        self.run_cli("slot", "--", sys.executable, "-c", "pass", "--build", build)
        output = self.run_cli("report").stdout.decode()
        self.assertIn("30 objects rebuilt", output)


class TreesTests(SandboxCase):
    def setUp(self):
        super().setUp()
        self.lib = os.path.join(self.project, "lib")
        os.makedirs(self.lib)
        write_cache(os.path.join(self.lib, "build"), self.lib)
        write_cache(os.path.join(self.lib, "build-copy"), self.lib)          # duplicate of build
        write_cache(os.path.join(self.lib, "build-asan"), self.lib, "Debug",
                    {"CMAKE_CXX_FLAGS": "-O1 -fsanitize=address,undefined"})
        write_cache(os.path.join(self.project, "out", "lib-rel"), self.lib, "RelWithDebInfo",
                    {"CMAKE_INSTALL_PREFIX": os.path.join(self.root, "pkg")})
        write_cache(os.path.join(self.project, "out", "lib-rel2"), self.lib, "MinSizeRel",
                    {"GUI_FORMS_PACKAGE_DIR": os.path.join(self.root, "pkg")})
        other = os.path.join(self.project, "app")
        os.makedirs(other)
        write_cache(os.path.join(other, "build"), other,
                    extra={"lib_DIR": os.path.join(self.lib, "build", "cmake")})

    def test_census(self):
        result = sushirice.census([self.project])
        self.assertEqual(len(result["trees"]), 6)
        self.assertEqual(len(result["duplicates"]), 1)
        self.assertEqual(sorted(os.path.basename(d) for d in result["duplicates"][0]["dirs"]), ["build", "build-copy"])
        asan = [t for t in result["trees"] if t["build_dir"].endswith("build-asan")][0]
        self.assertEqual(asan["sanitizers"], ["address", "undefined"])
        whys = sorted(item["why"].split(" (")[0] for item in result["shared"])
        self.assertEqual(whys, ["output/package dir shared by 2 trees", "points into another build tree"])
        warnings = sushirice.tree_warnings(result, self.config())
        self.assertTrue(any(w.startswith("5 build trees for one source") and w.endswith("; 1 duplicate")
                            for w in warnings), warnings)

    def test_g1_census_reaches_depth_four_and_follows_symlinked_trees_once(self):
        workers = os.path.join(self.root, "workers")
        workspace = os.path.join(workers, "w4")
        write_cache(os.path.join(workspace, ".build", "native-macos-arm64", "gui-forms"), workspace)
        volume = os.path.join(self.root, "volume", "gui-forms-sanitize")
        write_cache(volume, workspace, "Debug", {"CMAKE_CXX_FLAGS": "-fsanitize=address"})
        os.symlink(volume, os.path.join(workspace, ".build", "native-macos-arm64", "gui-forms-sanitize"))
        os.symlink(volume, os.path.join(workspace, "sanitize-again"))
        os.symlink(workspace, os.path.join(workspace, "loop"))
        found = sushirice.find_build_dirs([workers])
        self.assertEqual(len(found), 2, found)
        self.assertEqual(sorted({os.path.realpath(d) for d in found}),
                         sorted({os.path.realpath(os.path.join(workspace, ".build", "native-macos-arm64", "gui-forms")),
                                 os.path.realpath(volume)}))
        shallow = sushirice.find_build_dirs([workers], 3)  # <workers>/w4/.build/<platform>/<tree> is depth 4
        self.assertEqual([os.path.realpath(d) for d in shallow], [os.path.realpath(volume)])
        self.assertEqual(sushirice.DEFAULT_CONFIG["trees"]["max_depth"], 4)

    def test_ab_option_trees_are_not_duplicates(self):
        source = os.path.join(self.root, "consumer")
        for name, value in (("build-on", "ON"), ("build-off", "OFF"), ("build-off2", "OFF")):
            write_cache(os.path.join(source, name), source)
            with open(os.path.join(source, name, "CMakeCache.txt"), "a") as handle:
                handle.write("CONSUMER_USE_FEATURE:BOOL=%s\nCMAKE_EXPORT_COMPILE_COMMANDS:BOOL=%s\n" % (value, value))
        result = sushirice.census([source])
        self.assertEqual(len(result["duplicates"]), 1)
        self.assertEqual(sorted(os.path.basename(d) for d in result["duplicates"][0]["dirs"]),
                         ["build-off", "build-off2"])

    def test_sanitizer_option_variable(self):
        tree = sushirice.describe_cache("/x", {"CMAKE_HOME_DIRECTORY": "/s", "GUI_FORMS_SANITIZE": "ON"})
        self.assertEqual(tree["sanitizers"], ["GUI_FORMS_SANITIZE"])
        tree = sushirice.describe_cache("/x", {"CMAKE_HOME_DIRECTORY": "/s", "ENABLE_SANITIZERS": "OFF"})
        self.assertEqual(tree["sanitizers"], [])

    def test_worktree_list_parsing(self):
        text = ("worktree /r/main\nHEAD 1111\nbranch refs/heads/main\n\n"
                "worktree /r/job2\nHEAD 2222\ndetached\n\nworktree /r/bare.git\nbare\n")
        worktrees = sushirice.parse_worktree_list(text)
        self.assertEqual([w["branch"] for w in worktrees], ["main", "(detached)", None])
        self.assertTrue(worktrees[2]["bare"])

    def test_cli_trees(self):
        result = self.run_cli("trees", "--root", self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.decode()
        self.assertIn("DUPLICATE (Ninja, release, no sanitizer, clang++)", output)
        self.assertIn("SHARED", output)
        self.assertIn("WARNING: 5 build trees for one source", output)
        self.assertEqual(self.run_cli("trees", "--root", self.project, "--gate").returncode, 1)
        data = json.loads(self.run_cli("trees", "--root", self.project, "--json").stdout)
        self.assertEqual(len(data["trees"]), 6)

    def test_report_surfaces_trees(self):
        output = self.run_cli("report").stdout.decode()
        self.assertIn("trees: 6 configured build trees, 1 duplicate set", output)
        self.assertIn("WARNING: 5 build trees for one source", output)


class MonitorTests(SandboxCase):
    def test_monitor_speaks_on_slow_build_and_trees(self):
        lib = os.path.join(self.project, "lib")
        write_cache(os.path.join(lib, "build"), lib)
        write_cache(os.path.join(lib, "build-2"), lib)
        self.write_config({"monitor_interval_s": 0.2,
                           "headroom": {"enabled": True}, "trees": {"every_s": 3600}})
        state = os.path.join(self.project, ".sushi", "state", "sushirice")
        os.makedirs(state)
        runs_path = os.path.join(state, "runs.jsonl")
        with open(runs_path, "w") as handle:
            for index, duration in enumerate([20, 21, 22]):
                handle.write(json.dumps({"cmd": "ninja", "exit": 0, "duration_s": duration,
                                         "ts": "2026-11-0%dT00:00:00" % (index + 1)}) + "\n")
        monitor = subprocess.Popen([sys.executable, sushirice.SCRIPT_PATH, "monitor"], cwd=self.project,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ))
        try:
            time.sleep(1.0)
            with open(runs_path, "a") as handle:
                handle.write(json.dumps({"cmd": "ninja", "exit": 0, "duration_s": 90,
                                         "ts": "2026-11-09T00:00:00"}) + "\n")
            time.sleep(1.5)
        finally:
            monitor.terminate()  # started by this test
            output, _ = monitor.communicate(timeout=10)
        text = output.decode()
        self.assertIn("sushirice: build took 1.5 min vs baseline 21 s (ninja)", text)
        self.assertIn("2 build trees for one source", text)
        self.assertNotIn("20 s", text)  # old runs are not re-reported

    def test_monitor_skips_other_roles(self):
        result = self.run_cli("monitor", env={"SUSHI_ROLE": "coder"}, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")


if __name__ == "__main__":
    unittest.main()
