"""Config layers: ~/.sushi/sushirice.json under <project>/.sushi/sushirice.json (helpers isolates HOME)."""

import json
import os
import shutil
import unittest

from helpers import ISOLATED_HOME, SandboxCase, sushirice


class ConfigLayers(SandboxCase):
    def setUp(self):
        super().setUp()
        self.user = os.path.join(ISOLATED_HOME, ".sushi")
        os.makedirs(self.user, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.user, True)

    def write_user(self, data):
        with open(os.path.join(self.user, "sushirice.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_home_is_isolated(self):
        self.assertEqual(os.path.expanduser("~"), ISOLATED_HOME)

    def test_user_layer_merges_under_the_project(self):
        self.write_user({"max_slots": 4, "foreign_slot_dirs": ["~/ws/.m4ws.slot*"],
                         "watchdog": {"terminate_after_s": 900, "kill_grace_s": 5}})
        self.write_config({"watchdog": {"kill_grace_s": 45}})
        config = self.config()
        self.assertEqual(config["max_slots"], 2)  # the project's (SandboxCase writes max_slots 2)
        self.assertEqual(config["foreign_slot_dirs"], ["~/ws/.m4ws.slot*"])  # per machine
        self.assertEqual(config["watchdog"]["terminate_after_s"], 900)  # the user layer, deep-merged
        self.assertEqual(config["watchdog"]["kill_grace_s"], 45)  # the project wins
        self.assertEqual(config["watchdog"]["interval_s"], 0.2)

    def test_user_layer_without_a_project(self):
        self.write_user({"max_slots": 7})
        self.assertEqual(sushirice.load_config(None)["max_slots"], 7)

    def test_explicit_config_replaces_both(self):
        self.write_user({"max_slots": 4})
        explicit = os.path.join(self.root, "explicit.json")
        with open(explicit, "w", encoding="utf-8") as handle:
            json.dump({"start_spacing_s": 3}, handle)
        os.environ["SUSHIRICE_CONFIG"] = explicit
        config = self.config()
        self.assertEqual(config["max_slots"], sushirice.DEFAULT_CONFIG["max_slots"])
        self.assertEqual(config["start_spacing_s"], 3)


if __name__ == "__main__":
    unittest.main()
