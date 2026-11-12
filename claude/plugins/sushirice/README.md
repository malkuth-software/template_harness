# SushiRice

Build slots and machine protection, for machines that crash when too much building runs at
once. It generalizes `build_slot.py` (Windows byte-range slots) and `m4ws` (M4 pid-file slot
dirs) into one cross-platform, standard-library Python tool: macOS first, Linux, Windows best
effort.

- **`sushirice slot -- <cmd>`**: runs a build once a slot is free **and** live headroom allows
  (1-minute load per core, memory pressure, how many compiler processes are running). For the
  life of the build, a **watchdog** inside the wrapper watches headroom and acts on **that
  build's own process tree only**. It pauses the newest compiler jobs under pressure, resumes
  them when pressure clears, and terminates the build after a sustained period.
- **`sushirice report`**: reuse analysis after a build. It reports objects rebuilt against
  sources changed, the longest edges, the ccache hit rate, and duration against a moving
  baseline.
- **`sushirice trees`**: census of git worktrees and configured CMake build trees, flagging
  duplicate trees and package dirs shared between trees.
- A **monitor** that reports to the orchestrator session only when something is wrong. It
  doesn't signal processes.
- A **hook** that adds a reminder when a build isn't slot-wrapped, or when a command
  is about to make a fresh worktree or build tree while reusable ones exist.

There is no daemon or OS service. Protection exists only while a build runs through
`sushirice slot`.

## What it does on your machine (disclosure)

- **The watchdog pauses and can terminate compiler processes of the build you wrapped.**
  - `sushirice slot` starts the command in a process group of its own. Every
    `watchdog.interval_s` (2 s) it reads `ps` and memory/load figures, and works on that build's
    tree (descendants of the build, plus its process group) and nothing else.
  - **Pause.** When the build's running compiler jobs exceed `watchdog.max_compilers` (default
    1.5 x cores), or memory pressure crosses `watchdog.pressure_level` / falls below
    `watchdog.min_free_memory_percent`, it sends `SIGSTOP` to the newest compiler jobs. It acts
    only on leaf compilers: a `clang++` driver waiting on its `clang -cc1` or `ld` child is
    neither counted nor paused. It only touches processes owned by your user.
  - **Resume.** It sends `SIGCONT` once pressure clears.
  - **Terminate.** If pressure lasts `watchdog.terminate_after_s` (default 600 s; `null` never
    terminates), it sends `SIGTERM` and then `SIGCONT` to the build's process groups and
    processes. Build tools such as ninja give each job a process group of its own, so the
    wrapper signals every group and process in the tree. It sends `SIGKILL` if the tree is still
    alive `kill_grace_s` (30 s) later.
  - The run record in `runs.jsonl` carries what it did: counts, whether the build was
    terminated, and up to 40 events. Each action also goes to stderr and to
    `<project>/.sushi/logs/sushirice.log`.
- **Default: `"watchdog_mode": "enforce"`**, because it is your own build. Use `slot --dry-run`
  or `"watchdog_mode": "dry-run"` to only report ("would pause ..."), and `"off"` to run builds
  unwatched. On Windows the watchdog is off (no SIGSTOP); slots and headroom still apply.
- **The watchdog is watched, and never leaves a paused tree.** The wrapper's main thread checks
  the watchdog thread's heartbeat. If the thread dies or is silent for `watchdog.stall_s`
  (30 s), the wrapper resumes every paused process, stops acting, and lets the build continue;
  the run record notes why. When the build ends, or the wrapper receives SIGINT, SIGTERM or
  SIGHUP, it continues every paused process first, so the job can act on the signal, and then
  forwards the signal to the build's group. A paused pid is checked by name and start time
  before it is resumed. A stopped process can't exit, so its pid can't be reused while
  stopped.
- **Side effect of the own process group:** the build is not the terminal's foreground group.
  Ctrl-C still reaches it, because the wrapper forwards it, but a build that reads from the
  terminal would stop.
- `sushirice guard` is only a diagnostic. It prints what the watchdog rules would do to your
  compilers right now and never signals anything.
- **Monitor** (`monitors/monitors.json`): starts with every interactive session. It exits at
  once when `SUSHI_ROLE` is set to a role not in `monitor_roles` (default `["orchestrator"]`).
  Every `monitor_interval_s` (10 s) it reads `ps`, `sysctl`, and `memory_pressure -Q` (Linux:
  `/proc/loadavg`, `/proc/meminfo`, `/proc/pressure/memory`). It prints a line only when
  something is wrong: machine pressure, a watchdog action recorded in a finished run, a slow or
  wasteful build, or duplicate build trees (census every `trees.every_s`, 600 s). It never
  signals processes.
- **Hook** (`PreToolUse` on `Bash`): parses the command locally, without running it. It
  reminds you, once per session per kind (again after `hook_remind_every_s`), about:
  - a build (`ninja`, `make`, `cmake --build`, `xcodebuild`, `msbuild`, `ci_build.py`,
    `fold*_build.sh`) not wrapped in `sushirice slot`, `build_slot.py`, `m4ws`, or `m4run`;
  - `git worktree add` in a repo that already has worktrees (named, with their build trees);
  - a CMake configure into a new build dir when configured trees for the same source exist.

  To check for existing trees, it runs `git worktree list` and lists directories up to 2 levels
  deep for `CMakeCache.txt`. `hook_mode` / `hook_trees_mode` can be set to `"deny"` (the call is
  refused with the same text) or `"off"`.
- **Slots** are lock directories holding the owner's pid, in a machine-wide dir (default
  `~/.cache/sushirice/`, Windows `%LOCALAPPDATA%\sushirice\`). A lock whose pid is dead is
  removed, and the removal is logged. Each run is appended to
  `<project>/.sushi/state/sushirice/runs.jsonl` (command, cwd, duration, exit, wait, watchdog).
- `report` runs `git ls-files` and `ccache --print-stats` / `ccache -s` if ccache is on `PATH`.
  It reads `.ninja_log` and `CMakeCache.txt`. It never starts a build.
- No network access. It doesn't change permissions, settings, or the files you edit.

## Configure

Config files, later ones overriding earlier and merged key by key: `~/.sushi/sushirice.json` (per
machine: slots, `foreign_slot_dirs`, headroom), then `<project>/.sushi/sushirice.json` (the project
wins). `$SUSHIRICE_CONFIG` replaces both.

`<project>/.sushi/sushirice.json` (every key optional; defaults shown for the common ones):

```json
{
  "max_slots": 2,
  "reserved_critical_slots": 1,
  "foreign_slot_dirs": ["~/Developer/Projects/gui_forms_workers/.m4ws.slot*"],
  "start_spacing_s": 15,
  "headroom": {"max_load_per_core": 0.9, "max_compilers": null,
               "min_free_memory_percent": 20, "block_pressure_level": "critical"},
  "compiler_names": ["clang", "clang++", "cc1", "cc1plus", "ld", "ld64", "lld", "cl.exe", "link.exe"],
  "watchdog_mode": "enforce",
  "watchdog": {"interval_s": 2, "max_compilers": null, "pressure_level": "critical",
               "min_free_memory_percent": 10, "terminate_after_s": 600, "kill_grace_s": 30,
               "stall_s": 30},
  "monitor_roles": ["orchestrator"],
  "hook_mode": "remind",
  "hook_trees_mode": "remind",
  "trees": {"roots": null, "max_per_source": 2}
}
```

- **Slots.** Slot `0 .. reserved_critical_slots-1` only serves critical runs:
  `--priority critical`, `SUSHIRICE_PRIORITY=critical`, or `GUI_FORMS_BUILD_PRIORITY=critical`.
  Critical runs skip the load and compiler-count gates but not the memory gate. A command
  inside a slot sees `SUSHIRICE_SLOT_HELD=1` and `GUI_FORMS_BUILD_SLOT_HELD=1`, so a nested
  `sushirice slot` or `build_slot.py` runs at once. Exit status is the command's own, or 75
  after `--timeout`. `foreign_slot_dirs` counts other tools' live pid-file lock dirs (such as
  `m4ws` slots) against `max_slots`.
- **Headroom.** `headroom.max_compilers` (gating a new slot, machine-wide) defaults to the core
  count. `watchdog.max_compilers` (running jobs within one build) defaults to 1.5 x cores. Pressure levels come from the macOS kernel
  (`kern.memorystatus_vm_pressure_level`: normal / warn / critical) or Linux PSI. Free memory
  comes from `memory_pressure -Q`, falling back to `vm_stat`, or `/proc/meminfo`.
- **Trees.** `trees.roots` defaults to the project root. `max_per_source` (2: one Release and
  one sanitizer tree) sets when "N build trees for one source" is reported.

## Commands

```bash
sushirice slot [--priority critical] [--timeout S] [--dry-run] -- cmake --build build
sushirice guard                       # diagnostic only, never signals
sushirice report [--build-dir build] [--json]
sushirice trees [--root DIR ...] [--json] [--gate]
sushirice status [--json]
```

Run it as `python3 <plugin>/scripts/sushirice.py`, or alias `sushirice` to that. (No top-level `bin/`:
the plugin directory refuses plugins that ship one.)

Tests: `python3 -m unittest discover -s plugins/sushirice/tests`. The watchdog tests signal only
`sleep` processes they spawn themselves, through a symlink named `fake-clang`.

License: MIT.
