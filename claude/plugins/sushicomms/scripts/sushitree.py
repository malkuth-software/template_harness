"""FranSushi shared helpers: wasabi's shared-tree markers and the --project option.

Plugins install separately, so every plugin that needs these carries an
identical copy of this file beside its script; harness/test_fransushi.py
checks that the copies match. Each script loads its own copy by path, only
when it needs it.

A shared working tree is one wasabi treats as shared (wasabi.py
mark-shared and its shared_tree config): a `sushi-shared-tree` marker in
the repository's git dir, a `.sushi/shared-tree` file, or the tree listed
in `shared_tree.trees` / `shared_tree.paths` of ~/.sushi/wasabi.json or the
tree's own .sushi/wasabi.json ($WASABI_CONFIG replaces both). FranSushi
CLIs don't create `.sushi/` in such a tree: state belongs in the worker's
own workspace.

Standard library only, Python 3.9+, no subprocesses (hooks call it).
"""

import json
import os

GIT_DIR_MARKER = "sushi-shared-tree"
LEGACY_MARKER = os.path.join(".sushi", "shared-tree")


class SharedTreeError(Exception):
    """Raised instead of creating .sushi/ inside a shared working tree."""


def repo_root(path):
    """The nearest ancestor of path (or path itself) holding a .git entry, else None."""
    directory = os.path.abspath(path)
    if os.path.isfile(directory):
        directory = os.path.dirname(directory)
    while True:
        if os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def git_dir(root):
    """The git dir of the checkout at root: .git itself, or the target of a `gitdir:` file."""
    dot_git = os.path.join(root, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    try:
        with open(dot_git, encoding="utf-8", errors="replace") as handle:
            first = handle.readline().strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    target = first[len("gitdir:"):].strip()
    if not os.path.isabs(target):
        target = os.path.join(root, target)
    return os.path.normpath(target)


def wasabi_config_paths(root):
    explicit = os.environ.get("WASABI_CONFIG")
    if explicit:
        return [os.path.expanduser(explicit)]
    return [os.path.join(os.path.expanduser("~"), ".sushi", "wasabi.json"), os.path.join(root, ".sushi", "wasabi.json")]


def listed_in_config(path, real):
    """'listed in shared_tree.trees' or 'listed in shared_tree.paths' when the config at path names real."""
    try:
        with open(path, encoding="utf-8") as handle:
            section = json.load(handle).get("shared_tree")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(section, dict):
        return None
    trees = section.get("trees")
    if isinstance(trees, dict):
        for listed in trees:
            if isinstance(listed, str) and os.path.realpath(os.path.expanduser(listed)) == real:
                return "listed in shared_tree.trees of %s" % path
    paths = section.get("paths")
    if isinstance(paths, list):
        for listed in paths:
            if isinstance(listed, str) and os.path.realpath(os.path.expanduser(listed)) == real:
                return "listed in shared_tree.paths of %s" % path
    return None


def shared_tree_reason(path):
    """Why the checkout holding path is a shared working tree, or None when it is not (or is no checkout)."""
    root = repo_root(path)
    if root is None:
        return None
    directory = git_dir(root)
    if directory and os.path.exists(os.path.join(directory, GIT_DIR_MARKER)):
        return "marked by %s" % os.path.join(directory, GIT_DIR_MARKER)
    if os.path.exists(os.path.join(root, LEGACY_MARKER)):
        return "marked by %s" % os.path.join(root, LEGACY_MARKER)
    real = os.path.realpath(root)
    for config_path in wasabi_config_paths(root):
        reason = listed_in_config(config_path, real)
        if reason:
            return reason
    return None


def check_sushi_creation(root, path):
    """Raise SharedTreeError when creating path would create root/.sushi in a shared working tree."""
    sushi = os.path.join(os.path.abspath(root), ".sushi")
    target = os.path.abspath(path)
    if os.path.isdir(sushi) or not (target == sushi or target.startswith(sushi + os.sep)):
        return
    reason = shared_tree_reason(root)
    if reason:
        raise SharedTreeError(
            "not creating %s: %s is a shared working tree (%s). Run from your own workspace, or pass "
            "--project <your workspace>." % (sushi, root, reason))


def make_dirs(root, path):
    """os.makedirs(path, exist_ok=True), refusing to create root/.sushi in a shared working tree."""
    if os.path.isdir(path):
        return
    if root:
        check_sushi_creation(root, path)
    os.makedirs(path, exist_ok=True)


def take_project_option(arguments):
    """Remove `--project DIR` / `--project=DIR` from anywhere in arguments: (value or None, the rest).

    The last occurrence wins. A trailing `--project` with no value is left in place for the caller's
    parser to report."""
    value = None
    rest = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--project" and index + 1 < len(arguments):
            value = arguments[index + 1]
            index += 2
            continue
        if argument.startswith("--project="):
            value = argument[len("--project="):]
            index += 1
            continue
        rest.append(argument)
        index += 1
    return value, rest
