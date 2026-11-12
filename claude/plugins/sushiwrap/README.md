# SushiWrap

SushiWrap keeps each worker's role, track, branch, worktree and latest resume note in a
worker record (a **seat** file), so a host that resets on a timer, or a move to another
machine, loses only the conversation.

- **Seats**: one Markdown file per seat with a JSON header (role, track file, branch,
  worktree per host, owned paths, open tickets, host manifest, and `holder`: the host that
  claimed it) and the latest resume note. `seat take w4` claims the seat for this host and
  prints a resume brief a fresh session can act on.
- **Deadline**: when this host resets (`--at`, `--in 45m`, or `--from-boot 8h`). A monitor
  prints a shutdown checklist 30, 15 and 5 minutes before the reset. A `PreToolUse` hook gives
  busy sessions and subagents an "N min" note once per 5-minute bucket in the last 30 minutes.
- **shutdown-check**: lints that every seat's resume note is fresh, its worktree has no
  uncommitted changes to tracked files (commit them by path), and a memory mirror is complete.
  Untracked files are not its business.
- **survey / provision**: records a build host as a JSON + Markdown manifest (OS, cores,
  memory, disk, toolchain versions, ccache, PATH, selected env with secrets redacted, and
  the git repositories under your workspace roots with branch, head and remote), locally or
  over ssh, and prints a step plan to rebuild it. Provision never executes anything.

The model's context does not migrate; there is no cross-machine transcript sync. Seat state
does, through files you keep in git.

## What it does on your machine (disclosure)

- **`PreToolUse` hook on every tool** (`matcher: "*"`). It reads one small config file and
  the deadline file, and returns nothing unless a deadline for this host is under 30 minutes
  away. Then it adds a one-line `additionalContext` reminder, at most once per 5-minute
  bucket per session and per subagent (deduped with marker files under
  `<project>/.sushi/state/sushiwrap/hook/`). It doesn't deny, ask or approve, starts no
  process and uses no network. About 30 ms per call on Apple silicon.
- **`SessionStart` hook**: reads the system boot time (`sysctl kern.boottime` on macOS,
  `/proc/uptime` on Linux, `GetTickCount64`, then `wmic`, then PowerShell on Windows). If a
  boot-based deadline is stale because the host rebooted, it recomputes it (and creates one
  if you set `auto_from_boot`), writing the deadline file. It adds one line saying when the
  host resets, if a deadline is set.
- **Monitor** `host-reset-deadline` (interactive sessions only; Claude Code does not start
  monitors with `-p`): a Python loop that re-reads the config and deadline file once a minute
  (and the boot time, as above) and prints only 30, 15 and 5 minutes before the reset and
  when the reset time passes.
- **Writes**: seat files (in `seats_dir`), the deadline file, hook markers, and a log at
  `<project>/.sushi/logs/sushiwrap.log`. `seat pointer --append-track` appends to the seat's
  track file, and only when you pass that flag. Nothing else is modified: it doesn't commit,
  push, stash or write STATE.md (`shutdown-template` prints a draft to stdout).
- **Runs** `git` read-only commands (`rev-parse`, `log -1`, `status`, `rev-list`) in seat
  worktrees for `seat take`, `shutdown-check` and `shutdown-template`.
- **`survey`** runs `--version` queries of a fixed tool list, `ccache -p`, `sysctl` reads and
  one login-shell `printf "$PATH"`. It reads repositories under the workspace roots you
  configure from their `.git` files only (branch, head, remotes); it lists no other files. On macOS without the Command Line Tools it does not run
  the `/usr/bin` shims (they would pop up an installer). It reads environment variables you
  select and **redacts** any whose name matches token/key/secret/password/credential, and any
  value that looks like a credential (URL user:pass, `ghp_`/`sk-`-style tokens, long opaque
  strings). Remote URLs are stripped of embedded credentials. `--anonymize` replaces the home
  directory, user name and host name with placeholders.
- **`survey --host H`** connects with `ssh -o BatchMode=yes H nice -n 10 python3 -` and sends
  `scripts/sushiwrap_probe.py` on stdin (no shell string is built from your input). The probe
  is read-only on the remote host and leaves no files there.
- The hooks only add a note; they don't approve, change settings or signal processes.

## Configure

Optional. A missing config means: seats in `<project>/.sushi/seats/`, deadline in
`<project>/.sushi/state/sushiwrap/deadline.json`, thresholds 30/15/5, no pushing. Every
path SushiWrap uses comes from config (or is relative to the project root); nothing
machine-specific is built in, so the plugin can ship inside a harness repository as is.

Config files, later ones overriding earlier: `~/.sushi/sushiwrap.json`, then
`<project>/.sushi/sushiwrap.json`. `$SUSHIWRAP_CONFIG` replaces both. Relative paths are
relative to the project root; `~` expands; `{claude_memory}` means this project's Claude
Code memory folder.

**Recommended:** keep seats in the synced coordination repo, set once per machine in
`~/.sushi/sushiwrap.json` so every worktree and harnessed project sees the same seats:

```json
{"seats_dir": "~/Developer/projects/desks/seats"}
```

A fuller example (all keys optional):

```json
{
  "seats_dir": "~/Developer/projects/desks/seats",
  "tracks_root": "~/Developer/projects",
  "state_file": "~/Developer/projects/desks/coordinator/STATE.md",
  "deadline_file": "~/Developer/projects/desks/coordinator/deadline.json",
  "auto_from_boot": "8h",
  "thresholds_min": [30, 15, 5],
  "pointer_max_age_min": 45,
  "push_allowed": false,
  "memory_mirror": [["{claude_memory}", "~/Developer/projects/desks/coordinator/memory"]],
  "survey": {
    "workspace_roots": ["~/Developer/gui_forms_workers"],
    "tools": {"clang_query": "/opt/homebrew/opt/llvm/bin/clang-query"},
    "tool_dirs": ["/opt/homebrew/bin", "/usr/local/bin"],
    "llvm_candidates": ["/opt/homebrew/opt/llvm/bin/clang", "/usr/local/opt/llvm/bin/clang"],
    "env_names": [],
    "env_prefixes": [],
    "known_traps": ["rsync by content, not mtime", "rsync --delete must exclude fetched sources and symlinks"]
  },
  "hosts": {"m4mini": {"python": "python3", "nice": 10,
                       "survey": {"workspace_roots": ["~/Developer/Projects/gui_forms_workers"]}}}
}
```

A deadline is **per host** by default: it carries the host name that set it and other hosts
ignore it, so a deadline file in a synced repo warns only the machine that resets (the
recommended default). Use
`deadline set ... --scope all` to warn every host. Boot-based deadlines renew themselves
after a reboot (on session start and in the monitor).

A seat's `worktree` is a path, or a table of short host names (lower case, as `seat take`
prints them) to paths with `"*"` as the fallback; `--worktree HOST=PATH` adds a table entry.

`survey.tool_dirs` (extra places to look for tools beyond PATH) and `survey.llvm_candidates`
default to the usual Homebrew locations shown above; override them for other layouts.

Seat identity for hooks comes from `SUSHI_SEAT` / `SUSHI_ROLE` in the session environment.

## Commands

```bash
S="python3 plugins/sushiwrap/scripts/sushiwrap.py"
$S seat init w4 --role coder --track tracks/gui_forms/hot_paths.md --branch claude/x \
   --worktree "$HOME/Developer/gui_forms_workers/w4/gui_forms"
$S seat set w4 --worktree shadow-pc=D:/gui_forms_w4/gui_forms   # per-host entry (short host name)
$S seat take w4            # claims the seat for this host; prints the resume brief
$S seat pointer w4 --text "Done A; claude/x @ abc123; next: B" --append-track
$S seat list | show w4 | set w4 --branch claude/y | release w4
$S deadline set --from-boot 8h      # or --in 45m, or --at 2026-11-11T18:00
$S deadline show | clear
$S shutdown-check [--seat w4] [--max-age 30]   # exit 1 on any FAIL
$S shutdown-template --reason "Shadow 8-hour reset"
$S survey [--host m4mini] [--out manifests/m4] [--anonymize] [--json]
$S provision --manifest manifests/m4.json
```

Skills: `take-seat` (resume from a seat) and `shutdown` (the shutdown procedure).

Track files: the latest resume note is the last heading containing `Resume note` (the
`pointer_heading` setting) or the older `RESUME POINTER` that doesn't say
Previous/superseded/old; it runs to the next heading of the same level. Seat files written
with the older `## RESUME POINTER` heading are still read; new writes use `## Resume note`.

## Tests

```bash
python3 -m unittest discover -s plugins/sushiwrap/tests
SUSHIWRAP_TEST_HOST=m4mini python3 -m unittest discover -s plugins/sushiwrap/tests   # + read-only ssh probe
```

`tests/samples/` holds two real, anonymized survey outputs (a laptop and an M4 mini), each
limited to one workspace of public repositories.

License: MIT.
