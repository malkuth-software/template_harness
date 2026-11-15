"""Command parsing table: how Wasabi splits a Bash command into the commands that will run."""

import unittest

from wasabi_testlib import EXAMPLE_CONFIG, isolate_environment, wasabi

BASE = "/base"
WRAPPERS = wasabi.read_json(EXAMPLE_CONFIG)["stdin_wait"]["remote_wrappers"]

# (command, expected argv of every invocation in run order: substitutions first, then the command,
#  then what it runs nested: sh -c strings, eval, ssh remote commands, shell heredocs)
ARGV_CASES = [
    ("git status", [["git", "status"]]),
    ("git add . && git commit -m 'msg here'", [["git", "add", "."], ["git", "commit", "-m", "msg here"]]),
    ("a || b; c", [["a"], ["b"], ["c"]]),
    ("a | b |& c", [["a"], ["b"], ["c"]]),
    ("a & b", [["a"], ["b"]]),
    ("a\nb\n\nc", [["a"], ["b"], ["c"]]),
    ("echo \"a b\" 'c d' e\\ f", [["echo", "a b", "c d", "e f"]]),
    ("echo \"it's\" 'say \"hi\"'", [["echo", "it's", 'say "hi"']]),
    ("echo 'a'\"b\"c", [["echo", "abc"]]),
    ("echo \"a \\\"q\\\" \\$HOME\"", [["echo", 'a "q" $HOME']]),
    ("echo $'a\\tb'", [["echo", "a\tb"]]),
    ("echo ''", [["echo", ""]]),
    ("FOO=1 BAR='x y' git stash", [["git", "stash"]]),
    ("echo $(git rev-parse HEAD)", [["git", "rev-parse", "HEAD"], ["echo", "$(git rev-parse HEAD)"]]),
    ("echo \"$(git stash list)\"", [["git", "stash", "list"], ["echo", "$(git stash list)"]]),
    ("echo `git stash`", [["git", "stash"], ["echo", "`git stash`"]]),
    ("echo $(echo $(git stash))", [["git", "stash"], ["echo", "$(git stash)"], ["echo", "$(echo $(git stash))"]]),
    ("echo $((1 + 2))", [["echo", "$((1 + 2))"]]),
    ("echo ${HOME}/x", [["echo", "${HOME}/x"]]),
    ("bash -c 'git stash && git pull'", [["bash", "-c", "git stash && git pull"], ["git", "stash"], ["git", "pull"]]),
    ("sh -c \"cd sub; git clean -fd\"", [["sh", "-c", "cd sub; git clean -fd"], ["cd", "sub"], ["git", "clean", "-fd"]]),
    ("bash -lc 'git switch main'", [["bash", "-lc", "git switch main"], ["git", "switch", "main"]]),
    ("zsh -euc 'git stash pop' extra", [["zsh", "-euc", "git stash pop", "extra"], ["git", "stash", "pop"]]),
    ("bash -c \"bash -c 'git stash'\"", [["bash", "-c", "bash -c 'git stash'"], ["bash", "-c", "git stash"],
                                         ["git", "stash"]]),
    ("eval 'git stash'", [["eval", "git stash"], ["git", "stash"]]),
    ("sudo -u bob git stash", [["git", "stash"]]),
    ("env -i FOO=1 git stash", [["git", "stash"]]),
    ("nice -n 10 nohup git stash", [["git", "stash"]]),
    ("timeout 30 python3", [["python3"]]),
    ("command -v git", []),
    ("command git stash", [["git", "stash"]]),
    ("exec git stash", [["git", "stash"]]),
    ("time -p make", [["make"]]),
    ("xargs -n 1 git add", [["git", "add"]]),
    ("/usr/bin/git stash", [["/usr/bin/git", "stash"]]),
    ("\\git stash", [["git", "stash"]]),
    ("git log > out.txt 2>&1", [["git", "log"]]),
    ("git log 2>/dev/null | head -5", [["git", "log"], ["head", "-5"]]),
    ("cat <<EOF\ngit stash\nEOF\ngit status", [["cat"], ["git", "status"]]),
    ("cat <<'EOF' | python3\nimport os; os.system('x')\nEOF", [["cat"], ["python3"]]),
    ("bash <<EOF\ngit stash\nEOF", [["bash"], ["git", "stash"]]),
    ("if true; then git stash; fi", [["true"], ["git", "stash"]]),
    ("for f in a b; do git add \"$f\"; done", [["git", "add", "$f"]]),
    ("while read l; do echo \"$l\"; done < list.txt", [["read", "l"], ["echo", "$l"]]),
    ("(cd sub && git stash)", [["cd", "sub"], ["git", "stash"]]),
    ("{ git stash; }", [["git", "stash"]]),
    ("diff <(git show HEAD:a) a", [["git", "show", "HEAD:a"], ["diff", "/dev/fd/63", "a"]]),
    ("git commit -m \"fix: a && b; c | d\"", [["git", "commit", "-m", "fix: a && b; c | d"]]),
    ("echo a # git stash", [["echo", "a"]]),
    ("echo a#b", [["echo", "a#b"]]),
    ("git \\\n  stash", [["git", "stash"]]),
    ("ssh m4mini 'cd x && git stash'", [["ssh", "m4mini", "cd x && git stash"], ["cd", "x"], ["git", "stash"]]),
    ("ssh -p 22 -o BatchMode=yes host python3 run.py", [["ssh", "-p", "22", "-o", "BatchMode=yes", "host", "python3",
                                                         "run.py"], ["python3", "run.py"]]),
    ("m4run w1 -- bash -c 'python3 -c \"print(1)\"'", [["m4run", "w1", "--", "bash", "-c", "python3 -c \"print(1)\""],
                                                       ["bash", "-c", "python3", "-c", "print(1)"], ["python3"]]),
    ("m4ws w2 python3 tools/ci_build.py --tests \"'a|b'\"", [["m4ws", "w2", "python3", "tools/ci_build.py", "--tests",
                                                              "'a|b'"],
                                                             ["python3", "tools/ci_build.py", "--tests", "a|b"]]),
]

# (command, invocation index, attribute, expected)
CONTEXT_CASES = [
    ("cd /repo && git stash", 1, "cwd", "/repo"),
    ("cd sub && git stash", 1, "cwd", "/base/sub"),
    ("cd sub; cd ..; git stash", 2, "cwd", "/base"),
    ("cd ~/x && git stash", 1, "cwd", "HOME/x"),
    ("env -C /other git stash", 0, "cwd", "/other"),
    ("bash -c 'cd /deep && git stash'", 2, "cwd", "/deep"),
    ("cd /repo && bash -c 'git stash'", 2, "cwd", "/repo"),
    ("cd \"$(git rev-parse --show-toplevel)\" && git stash", 2, "cwd", None),
    ("python3", 0, "stdin", "open"),
    ("echo x | python3", 1, "stdin", "piped"),
    ("python3 < in.txt", 0, "stdin", "redirected"),
    ("python3 <<< 'print(1)'", 0, "stdin", "heredoc"),
    ("python3 &", 0, "stdin", "redirected"),
    ("echo x | bash -c 'python3'", 2, "stdin", "piped"),
    ("ssh -n host python3", 1, "stdin", "redirected"),
    ("ssh host python3", 1, "remote", "host"),
    ("timeout 5 python3", 0, "bounded", True),
    ("FOO=1 git stash", 0, "env", {"FOO": "1"}),
    ("source env.sh && git add .", 1, "env_changed", True),
    ("git add . && source env.sh", 0, "env_changed", False),
    ("curl -s https://x | sh", 0, "pipe_to", "sh"),
]


class ParseTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = isolate_environment()

    def test_argv_table(self):
        self.assertGreaterEqual(len(ARGV_CASES) + len(CONTEXT_CASES), 40)
        for command, expected in ARGV_CASES:
            with self.subTest(command=command):
                got = [inv.argv for inv in wasabi.analyze(command, BASE, WRAPPERS)]
                self.assertEqual(got, expected)

    def test_context_table(self):
        for command, index, attribute, expected in CONTEXT_CASES:
            with self.subTest(command=command):
                invocations = wasabi.analyze(command, BASE, WRAPPERS)
                value = getattr(invocations[index], attribute)
                if isinstance(expected, str) and expected.startswith("HOME"):
                    expected = self.home + expected[4:]
                self.assertEqual(value, expected)

    def test_unbalanced_quote_falls_back_conservatively(self):
        analyzer = wasabi.Analyzer()
        invocations = analyzer.run("echo 'oops && git stash", BASE)
        self.assertTrue(analyzer.degraded)
        self.assertIn(["git", "stash"], [inv.argv for inv in invocations])

    def test_fallback_finds_git_inside_broken_bash_c(self):
        invocations = wasabi.analyze("bash -c \"git -C /r stash", BASE)
        self.assertTrue(any(inv.argv[:1] == ["git"] and "stash" in inv.argv for inv in invocations))

    def test_scan_options_skips_option_values(self):
        letters, longs, positionals, after, saw = wasabi.scan_options(
            ["-m", "-a", "--author", "x", "-am", "msg", "p1", "--", "p2"], short_with_arg="m",
            long_with_arg=("--author",))
        self.assertEqual(positionals, ["p1"])
        self.assertEqual(after, ["p2"])
        self.assertTrue(saw)
        self.assertIn("a", letters)  # from -am
        letters, _, _, _, _ = wasabi.scan_options(["-m", "-a"], short_with_arg="m")
        self.assertNotIn("a", letters)  # -a is the message

    def test_no_wrappers_by_default(self):
        self.assertEqual(wasabi.DEFAULT_CONFIG["stdin_wait"]["remote_wrappers"], {})
        got = [inv.argv for inv in wasabi.analyze("m4ws w1 python3", BASE)]
        self.assertEqual(got, [["m4ws", "w1", "python3"]])

    def test_deeply_nested_input_terminates(self):
        command = "bash -c " + "'" + "bash -c " * 50 + "x'"
        wasabi.analyze(command, BASE)


if __name__ == "__main__":
    unittest.main()
