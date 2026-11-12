"""PreToolUse hook: pipe hook JSON into `sushirice.py hook` and read what it prints."""

import json
import os
import subprocess
import unittest

from helpers import SandboxCase, sushirice

FLAGGED = [
    "ninja -C build",
    "cd build && ninja -j8",
    "cmake --build build --target all",
    "make -j10",
    "xcodebuild -scheme App build",
    "msbuild App.sln /m",
    "python3 tools/ci_build.py --jobs 8",
    "python3 -B tools/ci_build.py",
    "nice -n 10 ./fold3_build.sh 2>&1 | tee log",
    "FOO=1 env BAR=2 time ninja",
    "for d in a b; do make -C $d; done",
    "ssh m4mini 'cd x && ninja -C build'",
    "bash -c 'cmake --build out'",
    "echo start; ninja\necho done",
    "ls | xargs -n 1 make -C",
]

NOT_FLAGGED = [
    "sushirice slot -- ninja -C build",
    "python3 /x/plugins/sushirice/scripts/sushirice.py slot --priority critical -- cmake --build b",
    "python -B D:/malkuth/projects/tools/build_slot.py --wait 60 -- cmake --build b",
    "m4ws w1 python3 tools/ci_build.py --jobs 8",
    "m4run w2 --label t -- ctest",
    "ninja -t targets all",
    "ninja -n",
    "make --version",
    "xcodebuild -list",
    "cmake -S . -B nowhere-special",
    "git commit -m 'fix ninja build and make it faster'",
    "echo ninja",
    "grep -r make src",
    "cat > notes.txt <<'EOF'\nninja all\nmake install\nEOF",
    "ls -la",
]


class HookCase(SandboxCase):
    def hook(self, command, session="s1", cwd=None, env=None):
        event = {"session_id": session, "cwd": cwd or self.project, "hook_event_name": "PreToolUse",
                 "tool_name": "Bash", "tool_input": {"command": command}}
        result = self.run_cli("hook", input_text=json.dumps(event), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = result.stdout.decode().strip()
        return json.loads(text) if text else None


class BuildHookTests(HookCase):
    def test_flagged_commands(self):
        for index, command in enumerate(FLAGGED):
            with self.subTest(command=command):
                output = self.hook(command, session="flag%d" % index)
                self.assertIsNotNone(output, command)
                context = output["hookSpecificOutput"]["additionalContext"]
                self.assertIn("outside a build slot", context)
                self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_unflagged_commands(self):
        for index, command in enumerate(NOT_FLAGGED):
            with self.subTest(command=command):
                self.assertIsNone(self.hook(command, session="ok%d" % index), command)

    def test_dedupe_per_session(self):
        self.assertIsNotNone(self.hook("ninja -C build", session="d"))
        self.assertIsNone(self.hook("ninja -C build", session="d"))
        self.assertIsNone(self.hook("cd b && ninja", session="d"))
        self.assertIsNotNone(self.hook("make", session="d"))  # a different build kind
        self.assertIsNotNone(self.hook("ninja -C build", session="other"))

    def test_remind_again_after_interval(self):
        self.write_config({"hook_remind_every_s": 0})
        self.assertIsNotNone(self.hook("ninja", session="r"))
        self.assertIsNotNone(self.hook("ninja", session="r"))

    def test_deny_mode(self):
        self.write_config({"hook_mode": "deny"})
        for _ in range(2):  # denials are not deduped
            output = self.hook("ninja -C build", session="deny")
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("build slot", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_off_mode(self):
        self.write_config({"hook_mode": "off"})
        self.assertIsNone(self.hook("ninja -C build"))

    def test_inside_a_slot(self):
        self.assertIsNone(self.hook("ninja", env={"SUSHIRICE_SLOT_HELD": "1"}))

    def test_bad_input_is_silent(self):
        for text in ("not json", "", json.dumps({"tool_name": "Edit", "tool_input": {"file_path": "x"}})):
            result = self.run_cli("hook", input_text=text)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")

    def test_custom_programs(self):
        self.write_config({"hook_build_programs": ["bazel"], "hook_build_scripts": ["build_all*.sh"]})
        self.assertIsNotNone(self.hook("bazel build //...", session="c1"))
        self.assertIsNotNone(self.hook("./build_all_fast.sh", session="c2"))
        self.assertIsNone(self.hook("ninja", session="c3"))


def write_cache(build_dir, source, build_type="Release", extra=None):
    os.makedirs(build_dir, exist_ok=True)
    lines = ["# This is the CMakeCache file.", "CMAKE_HOME_DIRECTORY:INTERNAL=%s" % source,
             "CMAKE_GENERATOR:INTERNAL=Ninja", "CMAKE_BUILD_TYPE:STRING=%s" % build_type,
             "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/clang++"]
    for key, value in (extra or {}).items():
        lines.append("%s:STRING=%s" % (key, value))
    with open(os.path.join(build_dir, "CMakeCache.txt"), "w") as handle:
        handle.write("\n".join(lines) + "\n")


class TreeHookTests(HookCase):
    def setUp(self):
        super().setUp()
        self.source = os.path.join(self.project, "lib")
        os.makedirs(self.source)
        with open(os.path.join(self.source, "CMakeLists.txt"), "w") as handle:
            handle.write("project(x)\n")
        write_cache(os.path.join(self.source, "build"), self.source)

    def context(self, command, session):
        output = self.hook(command, session=session)
        return output["hookSpecificOutput"]["additionalContext"] if output else None

    def test_configure_new_dir(self):
        text = self.context("cmake -S lib -B lib/build-two -DCMAKE_BUILD_TYPE=Release", "t1")
        self.assertIn("new build tree", text)
        self.assertIn(os.path.join(self.source, "build"), text)
        self.assertIn("already has the same configuration", text)

    def test_configure_sanitizer_tree_names_existing(self):
        text = self.context("cd lib && cmake -B build-asan -DCMAKE_BUILD_TYPE=Debug -DGUI_FORMS_SANITIZE=ON", "t2")
        self.assertIn("new build tree", text)
        self.assertNotIn("same configuration", text)

    def test_mkdir_cd_cmake_dotdot(self):
        text = self.context("cd lib && mkdir build-x && cd build-x && cmake ..", "t3")
        self.assertIn("new build tree", text)

    def test_reconfigure_existing_is_quiet(self):
        self.assertIsNone(self.context("cmake -S lib -B lib/build", "t4"))
        self.assertIsNone(self.context("cd lib/build && cmake .", "t5"))
        self.assertIsNone(self.context("cmake lib/build", "t6"))

    def test_first_tree_is_quiet(self):
        other = os.path.join(self.project, "fresh")
        os.makedirs(other)
        self.assertIsNone(self.context("cmake -S fresh -B fresh/build", "t7"))

    def test_trees_deny_mode(self):
        self.write_config({"hook_trees_mode": "deny"})
        output = self.hook("cmake -S lib -B lib/build-3", session="t8")
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_worktree_add(self):
        repo = os.path.join(self.root, "repo")
        os.makedirs(repo)
        git = ["git", "-C", repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t"]
        subprocess.run(git[:3] + ["init", "-q", "-b", "main"], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "init"], check=True)
        write_cache(os.path.join(repo, "build"), repo)
        output = self.hook("git worktree add ../job2 -b job2", session="w1", cwd=repo)
        text = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("git worktree add", text)
        self.assertIn("[main]", text)
        self.assertIn("build trees: build", text)
        self.assertIn("reusing an existing worktree", text)
        self.assertIsNone(self.hook("git worktree list", session="w2", cwd=repo))
        self.assertIsNotNone(self.hook("git -C %s worktree add /tmp/x" % repo, session="w3"))


if __name__ == "__main__":
    unittest.main()
