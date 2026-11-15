"""Shared helpers for the Wasabi tests: an isolated HOME and git config, and throwaway repos.

Every repository is created under a fresh temporary directory and removed afterwards. Nothing
here touches a real repository or the user's git configuration.
"""

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "scripts", "wasabi.py")
EXAMPLE_CONFIG = os.path.join(os.path.dirname(HERE), "examples", "wasabi.malkuth.json")
sys.path.insert(0, os.path.dirname(SCRIPT))

import wasabi  # noqa: E402

_ISOLATED = {}


def isolate_environment():
    """Point HOME and global git config at an empty temp dir for the whole test process."""
    if _ISOLATED:
        return _ISOLATED["home"]
    home = tempfile.mkdtemp(prefix="wasabi-home-")
    atexit.register(shutil.rmtree, home, True)
    global_config = os.path.join(home, ".gitconfig")
    open(global_config, "w").close()
    os.environ.update({
        "HOME": home,
        "GIT_CONFIG_GLOBAL": global_config,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Wasabi Test",
        "GIT_AUTHOR_EMAIL": "wasabi@example.invalid",
        "GIT_COMMITTER_NAME": "Wasabi Test",
        "GIT_COMMITTER_EMAIL": "wasabi@example.invalid",
    })
    for key in ("WASABI_CONFIG", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        os.environ.pop(key, None)
    _ISOLATED["home"] = home
    return home


def git(repo, *args, data=None):
    result = subprocess.run(["git"] + list(args), cwd=repo, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=True)
    return result.stdout.decode("utf-8", "replace")


class RepoTestCase(unittest.TestCase):
    """Gives each test a temp dir (self.tmp) and a fresh repo (self.repo) with one LF commit."""

    def setUp(self):
        isolate_environment()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wasabi-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.make_repo("repo")

    def make_repo(self, name, files=None):
        repo = os.path.join(self.tmp, name)
        os.makedirs(repo)
        git(repo, "init", "-q", "-b", "main")
        git(repo, "config", "core.autocrlf", "false")
        for path, content in (files or {"README.txt": b"hello\nworld\n"}).items():
            self.write(repo, path, content)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "initial")
        return repo

    def write(self, repo, path, content):
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(content if isinstance(content, bytes) else content.encode("utf-8"))
        return full

    def mark_shared(self, repo):
        """The primary marker: <git-dir>/sushi-shared-tree."""
        git_dir = git(repo, "rev-parse", "--absolute-git-dir").strip()
        open(os.path.join(git_dir, "sushi-shared-tree"), "w").close()

    def mark_shared_legacy(self, repo):
        """The older marker: .sushi/shared-tree in the repo root."""
        os.makedirs(os.path.join(repo, ".sushi"), exist_ok=True)
        open(os.path.join(repo, ".sushi", "shared-tree"), "w").close()

    def use_example_config(self):
        """Install examples/wasabi.malkuth.json as the (isolated) user-level config for this test."""
        target = os.path.join(os.environ["HOME"], ".sushi", "wasabi.json")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copyfile(EXAMPLE_CONFIG, target)
        self.addCleanup(os.remove, target)

    def write_config(self, project, config):
        import json
        os.makedirs(os.path.join(project, ".sushi"), exist_ok=True)
        with open(os.path.join(project, ".sushi", "wasabi.json"), "w") as handle:
            json.dump(config, handle)

    def evaluate(self, command, cwd=None):
        return wasabi.evaluate(command, cwd or self.repo)

    def actions(self, command, cwd=None, guard=None):
        result = self.evaluate(command, cwd)
        self.assertEqual(result.errors, [], result.errors)
        return [f.action for f in result.findings if guard is None or f.guard == guard]
