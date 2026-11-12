"""crlf guard against throwaway repos holding real CRLF files."""

import os
import unittest

from wasabi_testlib import RepoTestCase, git, wasabi

LF = b"line one\nline two\n"
CRLF = b"line one\r\nline two\r\n"


class CrlfGuard(RepoTestCase):
    def setUp(self):
        super().setUp()
        files = {"src/f%02d.c" % k: LF for k in range(30)}
        files["third_party/fix.patch"] = LF
        files[".gitattributes"] = b"third_party/*.patch eol=lf\n"
        self.repo = self.make_repo("crlfrepo", files)

    def crlf(self, command, cwd=None):
        return self.actions(command, cwd, guard="crlf")

    def convert(self, count, start=0, content=CRLF):
        for k in range(start, start + count):
            self.write(self.repo, "src/f%02d.c" % k, content)

    def test_add_many_crlf_files_denied(self):
        self.convert(25)
        self.assertEqual(self.crlf("git add ."), ["deny"])
        self.assertEqual(self.crlf("git add -A"), ["deny"])
        self.assertEqual(self.crlf("git add -u"), ["deny"])
        self.assertEqual(self.crlf("git add src"), ["deny"])
        self.assertEqual(self.crlf("cd src && git add ."), ["deny"])

    def test_add_few_crlf_files_allowed(self):
        self.convert(5)
        self.assertEqual(self.crlf("git add ."), [])

    def test_threshold_is_configurable(self):
        self.convert(5)
        self.write_config(self.repo, {"crlf": {"max_files": 3}})
        self.assertEqual(self.crlf("git add ."), ["deny"])

    def test_new_crlf_files_count(self):
        for k in range(22):
            self.write(self.repo, "new/n%02d.txt" % k, CRLF)
        self.assertEqual(self.crlf("git add new"), ["deny"])
        self.assertEqual(self.crlf("git add src"), [])  # nothing changed there

    def test_ignored_files_not_counted(self):
        self.write(self.repo, ".gitignore", "gen/\n")
        for k in range(25):
            self.write(self.repo, "gen/g%02d.txt" % k, CRLF)
        self.assertEqual(self.crlf("git add ."), [])

    def test_allow_paths(self):
        self.convert(25)
        self.write_config(self.repo, {"crlf": {"allow_paths": ["src/*.c"]}})
        self.assertEqual(self.crlf("git add ."), [])

    def test_files_already_crlf_in_index_are_not_introduced(self):
        self.convert(25)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "crlf on purpose")
        for k in range(25):
            self.write(self.repo, "src/f%02d.c" % k, CRLF + b"more\r\n")
        self.assertEqual(self.crlf("git add ."), [])

    def test_text_auto_normalizes_so_allowed(self):
        self.write(self.repo, ".gitattributes", "* text=auto\nthird_party/*.patch eol=lf\n")
        self.convert(25)
        self.assertEqual(self.crlf("git add src"), [])

    def test_autocrlf_true_normalizes_so_allowed(self):
        git(self.repo, "config", "core.autocrlf", "true")
        self.convert(25)
        self.assertEqual(self.crlf("git add src"), [])

    def test_dash_c_autocrlf_is_honored(self):
        self.convert(25)
        self.assertEqual(self.crlf("git -c core.autocrlf=input add src"), [])

    def test_sourced_env_uses_raw_check(self):
        # After `source`, git config may differ from what the hook sees: judge by raw bytes.
        git(self.repo, "config", "core.autocrlf", "true")
        self.convert(25)
        self.assertEqual(self.crlf("source toolchain_env.sh && git add src"), ["deny"])

    def test_staged_crlf_denied_at_commit(self):
        self.convert(25)
        git(self.repo, "add", ".")
        self.assertEqual(self.crlf("git commit -m x"), ["deny"])

    def test_commit_all_checks_worktree(self):
        self.convert(25)
        self.assertEqual(self.crlf("git commit -am x"), ["deny"])
        self.assertEqual(self.crlf("git commit -m x"), [])  # nothing staged

    def test_commit_with_paths_checks_only_those(self):
        self.convert(25)
        self.assertEqual(self.crlf("git commit -m x src/f00.c"), [])
        self.assertEqual(self.crlf("git commit -m x -- src"), ["deny"])

    def test_eol_lf_pinned_file_denied_even_alone(self):
        blob = git(self.repo, "hash-object", "-w", "--no-filters", "--stdin", data=CRLF).strip()
        git(self.repo, "update-index", "--cacheinfo", "100644,%s,third_party/fix.patch" % blob)
        result = self.evaluate("git commit -m x")
        crlf = [f for f in result.findings if f.guard == "crlf"]
        self.assertEqual([f.action for f in crlf], ["deny"])
        self.assertIn("eol=lf", crlf[0].message)

    def test_binary_files_ignored(self):
        for k in range(25):
            self.write(self.repo, "bin/b%02d.dat" % k, b"\0\x01\r\n\x02")
        self.assertEqual(self.crlf("git add bin"), [])

    def test_interactive_add_skipped(self):
        self.convert(25)
        self.assertEqual(self.crlf("git add -p"), [])

    def test_crlf_guard_takes_no_index_lock(self):
        self.convert(25)
        self.crlf("git add .")
        self.crlf("git commit -am x")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "index.lock")))
        status = git(self.repo, "status", "--porcelain")
        self.assertNotIn("A ", status)  # nothing was staged by the check

    def test_message_lists_examples_and_fix(self):
        self.convert(25)
        message = [f for f in self.evaluate("git add .").findings if f.guard == "crlf"][0].message
        self.assertIn("25 files", message)
        self.assertIn("src/f0", message)
        self.assertIn("allow_paths", message)


if __name__ == "__main__":
    unittest.main()
