# Wasabi

Guard hooks for Claude's Bash commands. Each guard comes from a real incident in a
multi-agent setup:

| Guard | Does | Incident it prevents |
|---|---|---|
| `shared_tree` | **denies** stash, branch switches, clean, `commit -a`, hard reset and rebase in a working tree that other agents share | a `stash; pull; stash pop` took several workers' uncommitted edits off disk; a "cleanup" switch removed 287 untracked files from under another agent |
| `crlf` | **denies** `git add`/`git commit` that would put CRLF line endings into more than 20 files (or into any file `.gitattributes` pins to `eol=lf`) | a worker in a Windows-toolchain shell staged about 4,000 CRLF files |
| `fetch` | **asks** before `git clone`, curl/wget downloads and CMake FetchContent configures (package installs only if you turn on `package_install`) | a private build cloned an 84 MB dependency through CMake FetchContent without asking |
| `stdin_wait` | **warns** (never blocks) about commands that will wait on stdin forever | a `bash -c '...'` string passed through an ssh wrapper lost its quoting, and a bare `python3` waited on stdin for 6.5 hours |

## What it does on your machine (disclosure)

- Registers one `PreToolUse` hook on the `Bash` tool. It reads the command Claude is about
  to run and can **deny** it (Claude sees the reason and the command does not run), **ask**
  (you get a permission prompt with the reason), or add a short **warning** to Claude's
  context. It never approves anything: it never returns `allow`, and it never changes
  permission settings. Your own permission rules still apply on top.
- To judge `git` commands it runs read-only git queries in the repository the command
  targets: `rev-parse`, `config --get alias.*`, `ls-files`, `diff --cached --name-only`,
  `cat-file --batch`, `hash-object --stdin-paths` (without `-w`: nothing is written) and
  `check-attr`. They run with `GIT_OPTIONAL_LOCKS=0` and `core.fsmonitor=false`, take no
  index lock, and pass on only safe `-c` settings from the command (`core.autocrlf`,
  `core.eol`, `core.safecrlf`, `core.attributesFile`, `core.quotepath`), so nothing in the
  command can make the hook run a program before you approve it.
- To judge `cmake` configure runs it reads `CMakeLists.txt` and `*.cmake` files under the
  source directory (bounded: 3,000 files, 4,000 directories; build trees, `_deps` and hidden
  directories skipped) and the build directory's `CMakeCache.txt`.
- It reads `~/.sushi/wasabi.json` and `<project>/.sushi/wasabi.json`, and in the target repo
  `<git-dir>/sushi-shared-tree`, `.sushi/shared-tree` and `.sushi/wasabi.json`.
- `wasabi.py mark-shared [--unmark]` (run only when you ask for it) creates or removes
  `<git-dir>/sushi-shared-tree`. The hook itself never writes into a repo's git directory.
- It writes a log of every deny/ask/warning and any internal error to
  `<project>/.sushi/logs/wasabi.log` (or `~/.sushi/logs/` outside a project), and a small
  dedupe file to `<project>/.sushi/state/wasabi/warned.json` so a warning is given once per
  session per command.
- No network access, no background processes. If anything inside the hook fails, it logs
  the error and lets the command through (exit 0, no output).

## Configure

All four guards are on by default; `shared_tree` does nothing until a tree is marked shared,
and `fetch` does not ask about package-manager installs unless `fetch.package_install` is `true`.
`<project>/.sushi/wasabi.json` (or `~/.sushi/wasabi.json` for every project; the project
file wins):

```json
{
  "guards": {"shared_tree": true, "crlf": true, "fetch": true, "stdin_wait": true},
  "shared_tree": {
    "paths": ["~/Developer/shared-clone"],
    "trees": {"~/Developer/gui_forms": {"rules": {"commit": "deny"}, "note": "Use a worker workspace."}},
    "rules": {"stash": "deny", "switch": "deny", "clean": "deny", "commit_all": "deny",
              "reset_hard": "deny", "rebase": "deny", "autostash": "deny",
              "restore": "ask", "commit": "off", "reset": "off", "pull": "off"},
    "note": "Use your worker workspace instead."
  },
  "crlf": {"max_files": 20, "allow_paths": ["*.bat", "*.sln"]},
  "fetch": {
    "allow_hosts": ["github.com/malkuth-software/", "localhost"],
    "allow_commands": ["^pip install -r requirements\\.txt$"],
    "ask_commands": ["tools/ci_build\\.py"],
    "package_install": false
  },
  "stdin_wait": {"remote_wrappers": {"onbuildhost": 1}, "dedupe_minutes": 30}
}
```

### Marking a shared tree

```bash
python3 scripts/wasabi.py mark-shared [DIR]           # creates <git-dir>/sushi-shared-tree
python3 scripts/wasabi.py mark-shared --unmark [DIR]  # removes it
```

The marker lives in the repo's own git directory (`git rev-parse --git-dir`): `.git/` for a
normal clone, `.git/worktrees/<name>/` for a linked worktree. So each clone and each worktree
is marked on its own, and the marker can never be committed. Two other ways still work: list
the repo root (the folder holding `.git`) in `shared_tree.paths`, or create an empty
`.sushi/shared-tree` file in the repo root. Don't commit that file: every clone would then
count as shared. `wasabi.py status` warns when it is tracked by git.

`shared_tree.rules` apply to every shared tree. To give one tree its own rules, list it under
`shared_tree.trees` (path → `{"rules": {...}, "note": "..."}`): that also marks it shared, and
its rules override the general ones. For a tree where nobody may commit at all, set
`"commit": "deny"` (and `"pull": "deny"` if pulls are out too). A shared repo's own
`.sushi/wasabi.json` can also tighten or loosen its rules. Each rule is `deny`, `ask`, or `off`.

Skill: `wasabi-status` explains which guards are active here and why. From a shell:

```bash
python3 scripts/wasabi.py status [DIR]               # guards, config sources, shared or not
python3 scripts/wasabi.py check --cwd DIR -- 'CMD'    # what CMD would trigger
python3 scripts/wasabi.py parse 'CMD'                 # how Wasabi splits CMD
python3 scripts/wasabi.py mark-shared [--unmark] [DIR] # mark this clone/worktree shared
python3 -m unittest discover -s plugins/wasabi/tests  # tests (throwaway repos in a temp dir)
```

## How commands are read

Wasabi parses the command the way a POSIX shell would before judging it: `&&`, `||`, `;`,
`|`, `&`, newlines, subshells and `{ }` groups; single, double and `$'...'` quoting and
backslashes; `$( )`, backticks and `<( )` (each parsed as its own command); `VAR=value`
prefixes; heredocs and here-strings; redirections. It follows `cd`/`pushd` through the chain,
`git -C`, `--git-dir`/`--work-tree` and `GIT_DIR`/`GIT_WORK_TREE`, and the hook's `cwd`, to
find the repository each git command really targets (a directory that doesn't exist yet is
skipped). It looks inside `sh -c`/`bash -c`/`zsh -c` strings, `eval`, shell heredocs, and
ssh remote commands; it strips `sudo`, `env` (including `env -C`/`-S`), `nice`, `nohup`,
`time`, `timeout`, `command`, `exec`, `caffeinate`, `stdbuf`, `ionice`, `arch`, `xargs`.
Git aliases are expanded. If the command doesn't parse (an unbalanced quote), Wasabi falls
back to a conservative split that still finds every git, curl, pip, cmake, ssh or
interpreter word.

## Every pattern

### shared_tree (only in a shared tree; local commands only, never over ssh)

| Rule | Default | Commands |
|---|---|---|
| `stash` | deny | `git stash` in every form except `git stash list` and `git stash show` |
| `switch` | deny | `git switch ...`; `git checkout <branch>`, `-b`, `-B`, `--orphan`, `--detach`, `-`, and a lone argument that is a revision or not an existing path |
| `restore` | ask | `git checkout -- <paths>`, `git checkout <rev> -- <paths>`, `git checkout <existing path>`, `git checkout .`, `-p`, `-f`; `git restore` unless it is `--staged` only |
| `clean` | deny | `git clean` unless `-n`/`--dry-run` |
| `commit_all` | deny | `git commit -a`, `--all`, and clusters such as `-am` (`-m -a` is a message, not the flag) |
| `reset_hard` | deny | `git reset --hard`, `--merge`, `--keep` |
| `rebase` | deny | `git rebase` (except `--abort`, `--quit`, `--show-current-patch`); `git pull --rebase`/`-r` |
| `autostash` | deny | `--autostash` on `pull`, `merge`, `rebase` |
| `commit` | off | any other `git commit`; `git merge`, `cherry-pick`, `revert`, `am` |
| `reset` | off | `git reset` without `--hard`/`--merge`/`--keep` |
| `pull` | off | `git pull` without `--rebase` |

Always allowed: status, log, diff, show, fetch, branch, `worktree add`, `git commit <path>`,
`git restore --staged`, and `--help` on anything. `git checkout -- <file>` asks rather than
denies because it discards that file's uncommitted edits, which may be another agent's, but
may also be your own.

### crlf (any repo, local commands only)

On `git add` (except `-p`, `-i`, `-e`, `-n`, `-N`) and `git commit` (including `-a`, `-i` and
`git commit <paths>`), Wasabi lists what would be staged (`ls-files --modified [--others
--exclude-standard]` for the same pathspecs, or the index for a plain commit), skips binary
files, files over 32 MB and `crlf.allow_paths`, and counts files whose new content has CRLF
while the index (or `HEAD`) version has none. Git's own normalization is respected: a file
that `text`/`text=auto`/`eol`, `core.autocrlf` or `-c core.autocrlf=...` would convert to LF
is not counted. When an earlier command in the chain ran `source`, `.`, `export`, `eval`,
`set`, `unset`, `conda`, `nvm` or `pyenv`, the hook can't see the resulting git setup, so it judges by raw bytes and
only `.gitattributes` normalization counts. **Deny** when more than `crlf.max_files`
(default 20) files are introduced, or when any staged file with CRLF is pinned to `eol=lf`.

### fetch (local and over ssh; asks, never denies)

- `git clone` of a URL or `host:path` (not a local path or `file://`), unless every host is in
  `fetch.allow_hosts`; `git submodule update` and `git submodule add`.
- `curl` writing a file (`-o`, `-O`, `--output`, `--remote-name`, clusters like `-sSLo`), with
  stdout redirected to a file, or piped into `sh`/`bash`/`zsh`/`python`/`tar`/`unzip`/`sudo`;
  unless every URL host is allowed. `localhost`/`127.0.0.1`/`::1` are always allowed.
- `wget`, unless `--spider`, or output to stdout (`-O -`, `-qO-`) that is neither redirected to
  a file nor piped to an installer.
- Package installs, **only when `fetch.package_install` is `true`** (off by default): `pip`/`pip3`/`python -m pip` `install|download|wheel`; `uv add`, `uv sync`,
  `uv pip install|sync`, `uv tool install`, `uv python install`; `pipx install|inject|upgrade|
  reinstall|run`; `npm install|i|ci|add|update`; `yarn` (bare) `add|install|upgrade`;
  `pnpm add|install|i|update`; `bun add|install`; `brew install|reinstall|upgrade|tap|fetch|
  bundle`; `cargo install|fetch`; `go get|install`, `go mod download`; `gem install`;
  `bundle install|update`; `apt`/`apt-get install`; `port install`; `conda`/`mamba
  install|create`; `conan install`; `vcpkg install`. Skipped with `--offline` or `--no-index`.
- CMake configure (not `--build`, `--install`, `-E`, `-P`) whose source tree uses
  `FetchContent_Declare`, `FetchContent_MakeAvailable`, `FetchContent_Populate`,
  `ExternalProject_Add` or `CPMAddPackage`, unless `FETCHCONTENT_FULLY_DISCONNECTED` is ON on
  the command line or in the build directory's `CMakeCache.txt`, or every declared
  dependency has a `-DFETCHCONTENT_SOURCE_DIR_<NAME>=` override. Remote cmake runs are not
  scanned.
- `fetch.ask_commands` regexes add patterns (e.g. a build script that configures CMake);
  `fetch.allow_commands` regexes exempt a command (matched against its words joined by
  spaces, after wrappers are stripped).

### stdin_wait (warnings only)

Only when stdin is still the session's (not piped, redirected, a heredoc or here-string, not
backgrounded with `&`, not under `timeout`, not fed to a `while read` loop):

- `python`, `python3`, `python3.X`, `pypy` with no script, `-c` or `-m` (or with `-`); `node`
  without a script, `-e` or `-p`; `ruby`/`perl`/`php`/`lua`/`osascript` without a script or `-e`/`-r`.
- `bash`/`sh`/`zsh`/`dash`/`ksh`/`fish` with no `-c` and no script, or with `-s`.
- `irb`, `tr`, `bc`, `dc`; `cat`, `tac`, `sort`, `uniq`, `wc`, `head`, `tail`, `base64` with no
  file; `grep`, `sed`, `awk`, `jq` with a program but no file (not `grep -r`, not `jq -n`).
- `read` without `-t` or `-u`.
- `ssh host` with no remote command (and no `-N`, `-f`, `-n`, `-W`).
- Any of the above inside `bash -c '...'`, `$( )`, `eval`, or a remote command run by `ssh`
  without `-n` (stdin is forwarded), or by a configured ssh-like wrapper
  (`stdin_wait.remote_wrappers`, none by default; see below).
- Quoting split: `ssh`, or a remote wrapper, given several arguments of which one contains
  spaces, e.g. `ssh host bash -c 'python3 -c "print(1)"'` (or, with the example config,
  `m4run w1 -- bash -c 'python3 -c "print(1)"'`). ssh joins the words with
  spaces and the remote shell splits them again, so the remote `bash -c` gets only
  `python3` as its script. Pass the whole remote command as one quoted string.

### Your own ssh-like wrappers

A script that runs its arguments on another machine through ssh (joins them with spaces,
forwards stdin) gets the same stdin and quoting checks as `ssh` once you name it in
`stdin_wait.remote_wrappers`. The value says where the remote command starts:

- a number N: the command is everything after the first N arguments
  (`"onbuildhost": 1` for `onbuildhost <slot> <command...>`);
- `"--"`: the command is everything after the first `--`
  (`"runremote": "--"` for `runremote <slot> [options] -- <command...>`).

Set a name to `null` in a later config file to drop one inherited from an earlier file.
[`examples/wasabi.malkuth.json`](examples/wasabi.malkuth.json) is a complete example for the
Malkuth Software setup (`m4ws <workspace> <command...>` and `m4run <workspace> [options] --
<command...>`, plus the shared coordinator clone
`~/Developer/gui_forms` under `shared_tree.trees` with `"commit": "deny"`); copy it to `~/.sushi/wasabi.json`. Keys
starting with `_` in a config are notes and are ignored.

## Limits

Wasabi reads commands; it doesn't run them. It can't see what a script file does, what a
variable will expand to, or what a sourced file changes, and an agent determined to get
around it can (for example by writing a script and running that). It is a seatbelt against
habits and accidents, not a sandbox.

License: MIT.
