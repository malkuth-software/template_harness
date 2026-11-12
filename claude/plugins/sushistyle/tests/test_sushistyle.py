import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import sushistyle  # noqa: E402

# Every test runs with its own HOME, so a real ~/.sushi/sushistyle.json (the user layer) never leaks in.
ISOLATED_HOME = tempfile.mkdtemp(prefix="sushistyle-home-")
os.environ["HOME"] = ISOLATED_HOME
atexit.register(shutil.rmtree, ISOLATED_HOME, True)

RULE_AUTO = {"id": "HS-auto", "severity": "error", "message": "m"}
RULE_RANGES = {"id": "HS-ranges", "severity": "error", "tier": "regex", "regex": r"std::(ranges|views)::", "message": "m"}


class MainFileOnly(unittest.TestCase):
    def test_empty_argument_list(self):
        self.assertEqual(sushistyle.main_file_only("lambdaExpr()"), "lambdaExpr(isExpansionInMainFile())")

    def test_existing_arguments(self):
        self.assertEqual(sushistyle.main_file_only("varDecl(hasType(autoType()))"),
                         "varDecl(isExpansionInMainFile(), hasType(autoType()))")


class AllowComments(unittest.TestCase):
    def finding(self, line):
        return sushistyle.Finding(RULE_AUTO, line, 1, "")

    def test_same_line(self):
        lines = ["auto x = 1; // sushi-allow: HS-auto external API"]
        self.assertTrue(sushistyle.is_allowed(self.finding(1), "f.cpp", lines, {"allow": []}, None))

    def test_comment_line_above(self):
        lines = ["// sushi-allow: HS-auto, HS-arrow reason", "auto x = 1;"]
        self.assertTrue(sushistyle.is_allowed(self.finding(2), "f.cpp", lines, {"allow": []}, None))

    def test_trailing_comment_above_does_not_carry(self):
        lines = ["int y = 0; // sushi-allow: HS-auto reason", "auto x = 1;"]
        self.assertFalse(sushistyle.is_allowed(self.finding(2), "f.cpp", lines, {"allow": []}, None))

    def test_other_rule_not_allowed(self):
        lines = ["auto x = 1; // sushi-allow: HS-arrow"]
        self.assertFalse(sushistyle.is_allowed(self.finding(1), "f.cpp", lines, {"allow": []}, None))

    def test_config_allow_glob(self):
        config = {"allow": [{"rule": "HS-auto", "files": "third_party/*"}]}
        self.assertTrue(sushistyle.is_allowed(self.finding(1), "/p/third_party/a.cpp", ["auto x;"], config, "/p"))


class RegexTier(unittest.TestCase):
    def test_skips_comment_lines(self):
        lines = ["// std::views::filter is banned", "auto v = xs | std::views::filter(f);"]
        found = sushistyle.run_regex_tier([RULE_RANGES], lines)
        self.assertEqual([finding.line for finding in found], [2])


class HookDedupe(unittest.TestCase):
    def test_reminds_once_and_ignores_other_files(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, ".sushi"))
            with open(os.path.join(root, ".sushi", "sushistyle.json"), "w") as handle:
                json.dump({"rule_packs": ["house"]}, handle)  # no host: regex tier only
            source = os.path.join(root, "a.cpp")
            with open(source, "w") as handle:
                handle.write("int f() { for (int x : xs | std::views::take(2)) {} return 0; }\n")
            event = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": source}})

            def run():
                with mock.patch("sys.stdin", io.StringIO(event)), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    sushistyle.command_hook()
                    return out.getvalue()

            first = run()
            self.assertIn("HS-ranges", json.loads(first)["hookSpecificOutput"]["additionalContext"])
            self.assertEqual(run(), "")
            other = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": os.path.join(root, "notes.md")}})
            with mock.patch("sys.stdin", io.StringIO(other)), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                sushistyle.command_hook()
                self.assertEqual(out.getvalue(), "")


class ConfigLayers(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.project = os.path.join(self.root, "app")
        os.makedirs(os.path.join(self.project, ".sushi"))
        self.user = os.path.join(ISOLATED_HOME, ".sushi")
        os.makedirs(self.user, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.user, True)

    def write(self, directory, data):
        with open(os.path.join(directory, "sushistyle.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_user_layer_under_project(self):
        self.write(self.user, {"remote": {"host": "m4mini", "clang_query": "/opt/llvm/bin/clang-query"},
                               "allow_local_clang": True, "rule_packs": ["user-pack"]})
        self.write(os.path.join(self.project, ".sushi"), {"remote": {"include_dirs": ["src"]},
                                                          "rule_packs": ["house"]})
        config = sushistyle.load_config(self.project)
        self.assertEqual(config["remote"]["host"], "m4mini")  # per machine, from the user layer
        self.assertEqual(config["remote"]["clang_query"], "/opt/llvm/bin/clang-query")
        self.assertEqual(config["remote"]["include_dirs"], ["src"])  # the project's
        self.assertEqual(config["remote"]["timeout_s"], 25)  # the default
        self.assertEqual(config["rule_packs"], ["house"])  # the project wins
        self.assertTrue(config["allow_local_clang"])

    def test_user_layer_alone_and_explicit_override(self):
        self.write(self.user, {"remote": {"host": "m4mini"}})
        self.assertEqual(sushistyle.load_config(None)["remote"]["host"], "m4mini")
        explicit = os.path.join(self.root, "explicit.json")
        with open(explicit, "w", encoding="utf-8") as handle:
            json.dump({"remote": {"slots": 3}}, handle)
        with mock.patch.dict(os.environ, {"SUSHISTYLE_CONFIG": explicit}):
            config = sushistyle.load_config(self.project)
        self.assertIsNone(config["remote"]["host"])  # the explicit file replaces both layers
        self.assertEqual(config["remote"]["slots"], 3)

    def test_rule_pack_paths(self):
        harness = os.path.join(self.root, "template_harness", "claude", "rules")
        os.makedirs(os.path.join(harness, "fixtures"))
        with open(os.path.join(harness, "house.json"), "w", encoding="utf-8") as handle:
            json.dump({"pack": "house", "rules": [dict(RULE_RANGES, fixtures={"fail": "fixtures/x.fail.cpp"})]}, handle)
        relative = {"rule_packs": ["../template_harness/claude/rules/house.json"]}
        rules = sushistyle.load_rules(relative, self.project)
        self.assertEqual([rule["id"] for rule in rules], ["HS-ranges"])
        self.assertEqual(rules[0]["_base"], harness)  # fixtures resolve beside the pack
        absolute = {"rule_packs": [os.path.join(harness, "house.json")]}
        self.assertEqual([rule["id"] for rule in sushistyle.load_rules(absolute, None)], ["HS-ranges"])
        self.assertEqual(sushistyle.load_rules({"rule_packs": ["../missing/house.json"]}, self.project), [])
        # A plain name still finds the plugin's own pack.
        self.assertTrue(sushistyle.load_rules({"rule_packs": ["house"]}, self.project))
        self.assertFalse(sushistyle.is_pack_path("house"))
        self.assertTrue(sushistyle.is_pack_path("house.json"))


class LocalClangRefused(unittest.TestCase):
    def test_localhost_refused_by_default(self):
        config = json.loads(json.dumps(sushistyle.DEFAULT_CONFIG))
        config["remote"]["host"] = "localhost"
        findings, status = sushistyle.run_clang_tier("a.cpp", "int x = 0;", [dict(RULE_AUTO, matcher="varDecl()")], config, None, False)
        self.assertEqual(findings, [])
        self.assertIn("local clang disabled", status)


class PathMapForms(unittest.TestCase):
    """G8: {"local", "remote"} entries, the convention shared with SushiBuild; legacy pairs keep their order."""

    def test_named_and_legacy_entries(self):
        path_map = [{"local": "/l/w4/", "remote": "/r/w4/"}, ["/l/", "/r/base/"], {"remote": "/x/"}]
        self.assertEqual(sushistyle.map_remote_path("/l/w4/a.cpp", path_map), "/r/w4/a.cpp")
        self.assertEqual(sushistyle.map_remote_path("/l/b.cpp", path_map), "/r/base/b.cpp")
        self.assertIsNone(sushistyle.map_remote_path("/elsewhere/c.cpp", path_map))


if __name__ == "__main__":
    unittest.main()
