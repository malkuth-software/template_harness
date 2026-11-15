---
name: wasabi-status
description: Explain which Wasabi guards are active for the current repository (shared-tree protection, CRLF staging, dependency-download asks, stdin-wait warnings), why a tree counts as shared, and what a given command would trigger. Use when asked about Wasabi, when a Bash command was denied or questioned by Wasabi, or before git work in a tree that might be shared.
---

Run, from the directory in question (or pass it as an argument):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wasabi.py" status [DIR]
```

It prints, for each guard (`shared_tree`, `crlf`, `fetch`, `stdin_wait`), whether it is on, the
settings in force, and the config files that set them (`~/.sushi/wasabi.json`, then
`<project>/.sushi/wasabi.json`). For `shared_tree` it says whether this repo is shared and why
(a `sushi-shared-tree` marker in the repo's git directory, a `.sushi/shared-tree` file in the
repo root, or a path listed in `shared_tree.paths`), warns if `.sushi/shared-tree` is tracked
by git, and
which git operations are denied, asked about, or allowed there. For `crlf` it also reports
whether the index already holds staged CRLF files.

To see what one command would trigger without running it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wasabi.py" check --cwd DIR -- '<command>'
```

Explain the result to the user in plain words:

- **Denied in a shared tree**: other agents keep uncommitted work there. Don't retry or work
  around the guard (no `git -C` tricks, no wrapping in `bash -c`). Do the git work in your own
  clone or `git worktree add ../<name> <branch>`, and commit only your own paths with
  `git commit <path>...`. Only the user can unmark the tree or change its rules.
- **CRLF denied**: the files would enter the index with Windows line endings. Convert them to
  LF, or restage from a shell that has not sourced a Windows toolchain environment. If CRLF
  is intended, the user can list the paths in `crlf.allow_paths`.
- **Asked about a download**: the user decides. Offline alternatives: CMake with
  `-DFETCHCONTENT_FULLY_DISCONNECTED=ON`, `pip install --no-index`, `npm install --offline`.
- **Stdin-wait warning**: the command would block forever. Give the program a script, `-c`,
  a heredoc, a pipe, or `< /dev/null`; through ssh-like wrappers pass the remote command as
  one quoted string.

Marking is the user's decision. When the user asks to mark or unmark the current clone or
worktree as shared, run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/wasabi.py" mark-shared [--unmark]`.
Do not edit `.sushi/wasabi.json` or any marker yourself unless the user asks you to.
