"""shared_tree guard against throwaway repos in a temp dir."""

import os
import subprocess
import sys
import unittest

from wasabi_testlib import SCRIPT, RepoTestCase, git, wasabi


class SharedTreeGuard(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.mark_shared(self.repo)
        git(self.repo, "branch", "feature")
        self.write(self.repo, "notes.txt", "another agent's uncommitted work\n")

    def shared(self, command, cwd=None):
        return self.actions(command, cwd, guard="shared_tree")

    def test_denies_stash_forms(self):
        for command in ("git stash", "git stash push -m x", "git stash -u", "git stash pop", "git stash apply",
                        "git stash drop", "git stash save wip", "git pull --autostash"):
            with self.subTest(command=command):
                self.assertEqual(self.shared(command), ["deny"])

    def test_allows_read_only_stash(self):
        self.assertEqual(self.shared("git stash list"), [])
        self.assertEqual(self.shared("git stash show -p"), [])

    def test_denies_branch_switches(self):
        for command in ("git switch feature", "git switch -c new", "git checkout feature", "git checkout -b x",
                        "git checkout -B x origin/main", "git checkout --detach", "git checkout -",
                        "git checkout nosuchbranch", "git checkout --orphan gh-pages"):
            with self.subTest(command=command):
                self.assertEqual(self.shared(command), ["deny"])

    def test_restore_asks(self):
        for command in ("git checkout -- README.txt", "git checkout README.txt", "git checkout .",
                        "git checkout HEAD~0 -- README.txt", "git restore README.txt", "git checkout -p",
                        "git checkout -f"):
            with self.subTest(command=command):
                self.assertEqual(self.shared(command), ["ask"])
        self.assertEqual(self.shared("git restore --staged README.txt"), [])

    def test_denies_clean_but_not_dry_run(self):
        self.assertEqual(self.shared("git clean -fd"), ["deny"])
        self.assertEqual(self.shared("git clean -fdx"), ["deny"])
        self.assertEqual(self.shared("git clean -n"), [])
        self.assertEqual(self.shared("git clean --dry-run -d"), [])

    def test_commit_all_denied_plain_commit_allowed(self):
        for command in ("git commit -a -m x", "git commit --all -m x", "git commit -am 'msg'", "git commit -qam x"):
            with self.subTest(command=command):
                self.assertEqual(self.shared(command), ["deny"])
        self.assertEqual(self.shared("git commit -m '-a is in the message'"), [])
        self.assertEqual(self.shared("git commit notes.txt -m mine"), [])

    def test_reset_rebase(self):
        self.assertEqual(self.shared("git reset --hard"), ["deny"])
        self.assertEqual(self.shared("git reset --hard origin/main"), ["deny"])
        self.assertEqual(self.shared("git reset --keep HEAD~1"), ["deny"])
        self.assertEqual(self.shared("git reset README.txt"), [])
        self.assertEqual(self.shared("git rebase origin/main"), ["deny"])
        self.assertEqual(self.shared("git pull --rebase"), ["deny"])
        self.assertEqual(self.shared("git pull -r origin main"), ["deny"])
        self.assertEqual(self.shared("git rebase --abort"), [])
        self.assertEqual(self.shared("git pull"), [])

    def test_read_only_commands_allowed(self):
        for command in ("git status", "git log --oneline", "git diff", "git fetch origin", "git branch -a",
                        "git worktree add ../mine feature", "git show HEAD", "git stash --help"):
            with self.subTest(command=command):
                self.assertEqual(self.shared(command), [])

    def test_effective_repo_from_cd_and_dash_c(self):
        other = self.make_repo("other")
        self.assertEqual(self.shared("git stash", cwd=other), [])
        self.assertEqual(self.shared("cd %s && git stash" % self.repo, cwd=other), ["deny"])
        self.assertEqual(self.shared("cd ../repo && git stash", cwd=other), ["deny"])
        self.assertEqual(self.shared("git -C %s stash" % self.repo, cwd=other), ["deny"])
        self.assertEqual(self.shared("git -C ../repo stash", cwd=other), ["deny"])
        self.assertEqual(self.shared("bash -c 'cd %s && git stash'" % self.repo, cwd=other), ["deny"])
        self.assertEqual(self.shared("sh -c \"git -C '%s' switch feature\"" % self.repo, cwd=other), ["deny"])
        self.assertEqual(self.shared("cd %s; cd %s; git stash" % (self.repo, other), cwd=other), [])
        self.assertEqual(self.shared("git --git-dir=%s/.git --work-tree=%s stash" % (self.repo, self.repo),
                                     cwd=other), ["deny"])
        self.assertEqual(self.shared("GIT_WORK_TREE=%s GIT_DIR=%s/.git git clean -fd" % (self.repo, self.repo),
                                     cwd=other), ["deny"])

    def test_subdirectory_of_shared_tree(self):
        sub = os.path.join(self.repo, "src", "deep")
        os.makedirs(sub)
        self.assertEqual(self.shared("git stash", cwd=sub), ["deny"])

    def test_nonexistent_target_dir_is_skipped(self):
        # `git clone x new && cd new && git checkout dev` must not be blamed on the shared tree.
        self.assertEqual(self.shared("cd brand-new && git checkout dev"), [])

    def test_unmarked_repo_untouched(self):
        other = self.make_repo("plain")
        for command in ("git stash", "git switch -c x", "git clean -fd", "git commit -am x", "git reset --hard"):
            with self.subTest(command=command):
                self.assertEqual(self.shared(command, cwd=other), [])

    def test_shared_by_config_path(self):
        other = self.make_repo("listed")
        project = os.path.join(self.tmp, "project")
        os.makedirs(project)
        self.write_config(project, {"shared_tree": {"paths": [other]}})
        self.assertEqual(self.shared("git -C %s stash" % other, cwd=project), ["deny"])
        self.assertEqual(self.shared("git -C %s stash" % other, cwd=other), [])  # other's own config is empty

    def test_per_tree_rules(self):
        strict = self.make_repo("strict")
        relaxed = self.make_repo("relaxed")
        self.write_config(os.environ["HOME"], {"shared_tree": {"paths": [relaxed], "trees": {
            strict: {"rules": {"commit": "deny"}, "note": "Use a worker workspace."}}}})
        self.addCleanup(os.remove, os.path.join(os.environ["HOME"], ".sushi", "wasabi.json"))
        self.assertEqual(self.shared("git commit -m x", cwd=strict), ["deny"])
        self.assertIn("Use a worker workspace.", self.evaluate("git commit -m x", strict).findings[0].message)
        self.assertEqual(self.shared("git stash", cwd=strict), ["deny"])
        self.assertEqual(self.shared("git commit -m x", cwd=relaxed), [])
        self.assertEqual(self.shared("git stash", cwd=relaxed), ["deny"])

    def test_example_config_denies_commit_in_gui_forms(self):
        from wasabi_testlib import EXAMPLE_CONFIG
        example = wasabi.read_json(EXAMPLE_CONFIG)
        tree = example["shared_tree"]["trees"]["~/Developer/gui_forms"]
        self.assertEqual(tree["rules"]["commit"], "deny")
        # Point the same tree entry at a throwaway repo and check it takes effect.
        throwaway = self.make_repo("gui_forms_standin")
        example["shared_tree"]["trees"] = {throwaway: tree}
        self.write_config(os.environ["HOME"], example)
        self.addCleanup(os.remove, os.path.join(os.environ["HOME"], ".sushi", "wasabi.json"))
        self.assertEqual(self.shared("git commit -m x", cwd=throwaway), ["deny"])
        self.assertEqual(self.shared("git commit -am x", cwd=throwaway), ["deny"])
        self.assertEqual(self.shared("git switch -c x", cwd=throwaway), ["deny"])
        self.assertEqual(self.shared("git status", cwd=throwaway), [])

    def test_user_level_config_lists_shared_paths(self):
        other = self.make_repo("userlisted")
        self.write_config(os.environ["HOME"], {"shared_tree": {"paths": [other]}})
        self.addCleanup(os.remove, os.path.join(os.environ["HOME"], ".sushi", "wasabi.json"))
        self.assertEqual(self.shared("git stash", cwd=other), ["deny"])

    def test_repo_rules_override(self):
        self.write_config(self.repo, {"shared_tree": {"rules": {"commit": "deny", "rebase": "off",
                                                                "restore": "deny"}}})
        self.assertEqual(self.shared("git commit -m x"), ["deny"])
        self.assertEqual(self.shared("git rebase origin/main"), [])
        self.assertEqual(self.shared("git checkout -- README.txt"), ["deny"])

    def test_guard_can_be_switched_off(self):
        self.write_config(self.repo, {"guards": {"shared_tree": False}})
        self.assertEqual(self.shared("git stash"), [])

    def test_alias_is_resolved(self):
        git(self.repo, "config", "alias.sv", "stash push")
        self.assertEqual(self.shared("git sv"), ["deny"])

    def test_deny_message_is_short_and_says_what_to_do(self):
        result = self.evaluate("git stash")
        message = result.findings[0].message
        self.assertIn("shared working tree", message)
        self.assertIn("worktree", message)
        self.assertLess(len(message), 400)

    def test_remote_git_is_not_judged_locally(self):
        self.assertEqual(self.shared("ssh host 'cd %s && git stash'" % self.repo), [])


class SharedTreeMarkers(RepoTestCase):
    def shared(self, command, cwd=None):
        return self.actions(command, cwd, guard="shared_tree")

    def run_script(self, *args):
        result = subprocess.run([sys.executable, SCRIPT] + list(args), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=60)
        return result.returncode, result.stdout.decode("utf-8") + result.stderr.decode("utf-8")

    def test_git_dir_marker_is_primary_and_never_tracked(self):
        self.mark_shared(self.repo)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, ".git", "sushi-shared-tree")))
        self.assertEqual(self.shared("git stash"), ["deny"])
        self.assertIn(".git/sushi-shared-tree", self.evaluate("git stash").findings[0].message)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")  # nothing to commit

    def test_worktrees_are_marked_individually(self):
        git(self.repo, "branch", "feature")
        worktree = os.path.join(self.tmp, "wt")
        git(self.repo, "worktree", "add", "-q", worktree, "feature")
        self.mark_shared(self.repo)
        self.assertEqual(self.shared("git stash"), ["deny"])
        self.assertEqual(self.shared("git stash", cwd=worktree), [])
        self.mark_shared(worktree)
        self.assertEqual(self.shared("git stash", cwd=worktree), ["deny"])
        git_dir = git(worktree, "rev-parse", "--absolute-git-dir").strip()
        self.assertTrue(os.path.isfile(os.path.join(git_dir, "sushi-shared-tree")))
        self.assertIn(os.sep + "worktrees" + os.sep, git_dir)

    def test_clones_are_marked_individually(self):
        clone = os.path.join(self.tmp, "clone")
        git(self.tmp, "clone", "-q", self.repo, clone)
        self.mark_shared(self.repo)
        self.assertEqual(self.shared("git stash", cwd=clone), [])

    def test_mark_shared_subcommand(self):
        code, out = self.run_script("mark-shared", self.repo)
        self.assertEqual(code, 0, out)
        self.assertIn("marked", out)
        self.assertEqual(self.shared("git clean -fd"), ["deny"])
        sub = os.path.join(self.repo, "src")
        os.makedirs(sub)
        code, out = self.run_script("mark-shared", "--unmark", sub)
        self.assertEqual(code, 0, out)
        self.assertIn("unmarked", out)
        self.assertEqual(self.shared("git clean -fd"), [])
        code, out = self.run_script("mark-shared", "--unmark", self.repo)
        self.assertIn("was not marked", out)

    def test_mark_shared_outside_a_repo_fails_cleanly(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        code, out = self.run_script("mark-shared", plain)
        self.assertEqual(code, 1)
        self.assertIn("not inside a git repository", out)

    def test_unmark_reports_remaining_legacy_marker(self):
        self.mark_shared_legacy(self.repo)
        code, out = self.run_script("mark-shared", "--unmark", self.repo)
        self.assertIn(".sushi/shared-tree still exists", out)
        self.assertEqual(self.shared("git stash"), ["deny"])

    def test_legacy_marker_still_supported(self):
        self.mark_shared_legacy(self.repo)
        self.assertEqual(self.shared("git stash"), ["deny"])
        self.assertIn(".sushi/shared-tree", self.evaluate("git stash").findings[0].message)

    def test_status_warns_when_legacy_marker_is_tracked(self):
        self.mark_shared_legacy(self.repo)
        code, out = self.run_script("status", self.repo)
        self.assertIn("is SHARED", out)
        self.assertNotIn("tracked by git", out)
        git(self.repo, "add", ".sushi/shared-tree")
        code, out = self.run_script("status", self.repo)
        self.assertIn("WARNING: .sushi/shared-tree is tracked by git", out)

    def test_status_suggests_mark_shared_when_unmarked(self):
        code, out = self.run_script("status", self.repo)
        self.assertIn("is not shared", out)
        self.assertIn("mark-shared", out)


if __name__ == "__main__":
    unittest.main()
