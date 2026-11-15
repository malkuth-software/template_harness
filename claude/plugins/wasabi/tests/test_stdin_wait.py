"""stdin_wait guard: warn (never block) on commands that will wait on stdin forever."""

import os
import unittest

from wasabi_testlib import RepoTestCase

INCIDENT = "m4run w1 -- bash -c 'python3 -c \"print(1)\"'"


class StdinWaitGuard(RepoTestCase):
    """Runs with examples/wasabi.malkuth.json loaded, so m4ws and m4run act like ssh."""

    def setUp(self):
        super().setUp()
        self.use_example_config()

    def stdin(self, command):
        return self.actions(command, guard="stdin_wait")

    def test_warns(self):
        for command in ("python3", "python3 -u", "python3 -", "python", "node", "bash", "sh -s", "cat",
                        "grep foo", "sed 's/a/b/'", "awk '{print $1}'", "jq .", "tr a b", "read answer",
                        "bash -c 'python3'", "echo $(python3)", "ssh host python3", "ssh host cat",
                        "ssh host", "cd /tmp && python3", "sudo python3", "m4ws w1 python3"):
            with self.subTest(command=command):
                self.assertIn("warn", self.stdin(command))

    def test_no_warning(self):
        for command in ("python3 script.py", "python3 -c 'print(1)'", "python3 -m pytest", "python3 -V",
                        "python3 -uc 'print(1)'", "python3 - <<'EOF'\nprint(1)\nEOF", "echo 1 | python3",
                        "python3 < in.py", "python3 <<< 'print(1)'", "timeout 60 python3", "python3 &",
                        "node app.js", "node -e 'x'", "bash script.sh", "bash -c 'echo hi'", "cat file.txt",
                        "grep foo file", "grep -r foo", "grep -e foo file", "sed -n 1p f", "jq . f.json",
                        "jq -n '1'", "read -t 5 x", "while read l; do echo $l; done < f",
                        "git log | while read l; do echo $l; done", "ssh -n host python3", "ssh host 'python3 x.py'",
                        "ssh host python3 < /dev/null", "ssh -N -L 8080:localhost:80 host", "echo x | ssh host cat",
                        "find . -name x | xargs grep foo", "bash <<EOF\necho hi\nEOF"):
            with self.subTest(command=command):
                self.assertEqual(self.stdin(command), [])

    def test_incident_quoting_split(self):
        result = self.evaluate(INCIDENT)
        messages = [f.message for f in result.findings if f.guard == "stdin_wait"]
        self.assertEqual(len(messages), 2, messages)
        self.assertIn("joins its arguments with spaces", messages[0])
        self.assertIn("would run only `python3`", messages[0])
        self.assertIn("wait for input forever", messages[1])
        self.assertEqual(result.decision(), "warn")  # warn, never deny

    def test_quoting_split_without_hang(self):
        messages = [f.message for f in self.evaluate("m4ws w1 bash -c 'cd build && ctest'").findings]
        self.assertTrue(any("only `cd`" in m for m in messages), messages)

    def test_whole_remote_command_as_one_string_is_fine(self):
        self.assertEqual(self.stdin("ssh host 'cd build && ctest'"), [])
        self.assertEqual(self.stdin("m4ws w1 \"bash -c 'cd build && ctest'\""), [])

    def test_remote_wrappers_configurable(self):
        self.write_config(self.repo, {"stdin_wait": {"remote_wrappers": {"onm4": 0}}})
        self.assertIn("warn", self.stdin("onm4 python3"))
        self.assertIn("warn", self.stdin("m4ws w1 python3"))  # config dicts merge; the user's stay
        self.write_config(self.repo, {"stdin_wait": {"remote_wrappers": {"m4ws": None}}})
        self.assertEqual(self.stdin("m4ws w1 python3"), [])  # null removes a default wrapper

    def test_without_example_config_wrappers_are_plain_commands(self):
        target = os.path.join(os.environ["HOME"], ".sushi", "wasabi.json")
        os.replace(target, target + ".away")
        self.addCleanup(os.replace, target + ".away", target)
        self.assertEqual(self.stdin("m4ws w1 python3"), [])
        self.assertEqual(self.stdin(INCIDENT), [])  # needs the wrapper config to be seen
        self.assertIn("warn", self.stdin("ssh host bash -c 'python3 -c \"print(1)\"'"))  # ssh is built in

    def test_guard_off(self):
        self.write_config(self.repo, {"guards": {"stdin_wait": False}})
        self.assertEqual(self.stdin("python3"), [])


if __name__ == "__main__":
    unittest.main()
