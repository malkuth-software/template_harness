# SushiBuild

Hand a build to a sub-model and get one answer back. A worker that has finished a batch of
code calls the `sushi-build` agent (Haiku) with the build command. That agent runs the build,
reads the errors, and decides **small** (a local slip in one file) or **big**. Each small error
goes to the `sushi-fixer` agent (Sonnet), which may read and edit **only that one file**. The
fixup is logged and the build resumes. If anything looks big, or a cap is hit, the triage
agent stops. Either way it returns one `SUSHIBUILD RESULT` block: the outcome, every fixup,
and the failing log excerpt if it escalated. The worker hears nothing in between; you can
watch the subagent's transcript live (`/tasks` in the CLI, the subagent view in the desktop app).

```
worker ──Agent──▶ sushi-build (haiku) ──run──▶ your build command
                      │  small error in one file
                      ├──lock file──▶ sushi-fixer (sonnet)  Read/Edit that file only (hook-enforced)
                      ├──log fixup, unlock, run again
                      └──big / capped / success ──▶ SUSHIBUILD RESULT block to the worker
```

## Use

From a worker, follow the `sushibuild` skill: call the Agent tool with
`subagent_type: "sushibuild:sushi-build"` and the build command in the prompt.

**Swapping the decision model.** The triage model is the `model:` line of
`agents/sushi-build.md` (default `haiku`). Without editing the plugin you can:
- set `"triage_model"` in the project config; the skill tells the worker to pass it as the
  Agent call's `model` parameter (aliases only: `haiku`, `sonnet`, `opus`, `fable`);
- set `"fixer_model"` the same way for the fixer;
- or copy the agent file to `.claude/agents/` under a new name, change `model:` to anything,
  including a full model ID, and add that name to `build_agent_types`.

## Configure

Config files, later ones overriding earlier: `~/.sushi/sushibuild.json` (per machine: the build
wrapper's `build_command`, `path_map`), then `<project>/.sushi/sushibuild.json` (the project wins,
key by key). `$SUSHIBUILD_CONFIG` replaces both. Leave `build_command` out of a committed project
file when it differs per machine.

`<project>/.sushi/sushibuild.json` (all optional; a missing file means these defaults):

```json
{
  "build_command": null,
  "build_cwd": null,
  "build_timeout_s": 1800,
  "max_fixes": 8,
  "max_wall_minutes": 45,
  "max_small_files": 3,
  "max_errors_per_small_file": 6,
  "template_note_threshold": 3,
  "log_excerpt_lines": 40,
  "path_map": [],
  "triage_model": null,
  "fixer_model": null,
  "fixer_agent_types": ["sushi-fixer"],
  "build_agent_types": ["sushi-build"]
}
```

`build_command` is what runs when the caller gives none. It runs through `sh` in `build_cwd`
(relative to the project root; default the root). On a laptop that must not compile, make it
your remote wrapper, for example
`rsync -a --exclude .sushi ./ m4mini:/path/tree/ && ssh m4mini "cd /path/tree && nice -n 15 make"`.
Relative paths in the compiler output are resolved against `build_cwd`.

**`path_map`** handles builds that print the build host's absolute paths. It is a list of
`[remote_prefix, local_prefix]` pairs. The longest matching prefix wins, it matches only at a
path-component boundary, and `~` in the local prefix is expanded. It is applied to every
diagnostic path (file, include stack, notes, linker references) before the small/big
judgement, to `lock` and `log` arguments, and to the summary's log excerpt. A diagnostic file
that does not exist on this machine after mapping counts as "can't fix locally" and makes the
run **big**, with a reason naming the printed and local paths. Example for gui_forms workers
built on the M4 through `m4ws`, which rsyncs `~/Developer/gui_forms_workers/wN/` on the laptop
to `/Users/joshuahkuttenkuler/Developer/Projects/gui_forms_workers/wN/` on the M4:

```jsonc
{
  // one entry covers every worker tree wN
  "build_command": "m4ws w4 <your usual build arguments>",
  "path_map": [
    ["/Users/joshuahkuttenkuler/Developer/Projects/gui_forms_workers/",
     "/Users/ultimussecundai/Developer/gui_forms_workers/"]
  ]
}
```

(The real file must be plain JSON: drop the comment line.)

**Long builds.** One Bash call lasts at most 10 minutes. For longer builds the triage agent
uses `run --detach`, which starts the build in its own session and returns at once, then
`wait --max 540` repeatedly until it prints `"status": "done"`. A detached build that
exceeds `build_timeout_s` is stopped by `wait` (SIGTERM to the process group it started).

Caps: `max_fixes` fixer calls per run; `max_wall_minutes` for the whole run; the same error
twice ends the run (the script compares a normalized file + message signature).

## Script

`scripts/sushibuild.py` (Python 3.9+, standard library only) does the bookkeeping the agents call:

```bash
sushibuild.py begin --command "<cmd>"      # start a run
sushibuild.py run [--detach]              # run the build once, classify, check caps
sushibuild.py wait [--max 540]            # wait for a detached build: running | done + result
sushibuild.py classify build.log [--exit N] [--base DIR] [--compact] [--no-local-check]
sushibuild.py lock /abs/file.cpp --fix "line 42: cnout -> cout" [--error "..."]
sushibuild.py log --file F --error E [--fix D] [--diff-stat "+1 -1"] [--verdict ...]
sushibuild.py unlock
sushibuild.py status
sushibuild.py summary --outcome success|escalated|capped|failed [--reason R]
```

`classify` understands clang, gcc, MSVC (`file(line,col): error C2065: ...`, MSBuild
suffixes), Apple ld, GNU ld, lld and MSVC link diagnostics. It groups errors by file and
gives a preliminary verdict. It says **small** for one to three files whose errors are
recognized local kinds (undeclared identifier, typo with "did you mean", missing include or
`;`, wrong member, local conversion). It says **big** for linker errors, template
instantiation cascades, `static_assert`/constraints/deleted functions, errors in system
headers, more than three files, "too many errors", timeouts, or a failure with no
diagnostic. The triage agent makes the final call.

## What it does on your machine (disclosure)

- **One `PreToolUse` hook** on `Edit|Write|MultiEdit|NotebookEdit|Read|Bash|Agent|Task`. It
  runs a short Python process on each such call in every session where the plugin is
  enabled, and it decides by the hook input's `agent_type`:
  - **`sushi-fixer` agent** (`sushibuild:sushi-fixer`): **denies** (`permissionDecision:
    "deny"`) any Read, Edit, Write, MultiEdit or NotebookEdit of a path other than the
    locked file, with paths resolved relative to the session cwd and through symlinks. It
    denies all edits when no file is locked, and denies all Bash and Agent calls.
  - **`sushi-build` agent**: **denies** writes outside `.sushi/state/sushibuild/`. It
    denies Bash commands that run a build tool directly instead of `sushibuild.py run`
    (make, ninja, cmake, clang, gcc, xcodebuild, msbuild, ... also inside `ssh` or `sh -c`)
    or that edit files (`sed -i`, redirects into source files, `git checkout`/`reset`/...).
    It denies Agent calls to anything but the fixer, in the background, or with no lock
    held. The Bash check is best-effort pattern matching, not a sandbox.
  - **Fallback**: if a subagent call arrives with no `agent_type` while a lock is held, its
    edits are restricted to the locked file.
  - **Everyone else** (your main session, other subagents): no output, untouched.
  - The hook never approves anything (it emits no `allow`), so your normal permission
    prompts still apply, and a fixer edit in default mode will ask you.
- **Runs your build command** (only when the `sushi-build` agent calls `sushibuild.py run`)
  through `sh`, exactly as configured or as the caller passed it. With `run --detach` the
  build runs as a background process in its own session (pid, log and exit file recorded in
  state). If that build outlives `build_timeout_s`, `wait` sends SIGTERM to that process group,
  which SushiBuild started itself. It never signals any other process; `wait` checks whether
  the pid is alive with signal 0, which delivers nothing. SushiBuild adds no network
  access of its own; whatever your command does (ssh, rsync) is yours.
- **Writes** `.sushi/state/sushibuild/`: `run.json`, `fixlock.json`/`lastlock.json`,
  `locked.orig` (a copy of the locked file, for the diff), `fixups.jsonl`, `build-N.log`,
  `last-result.txt`, `build-N.exit`. `begin` deletes the previous run's copies of these files. It also writes
  a log at `.sushi/logs/sushibuild.log`. It writes nothing else; code changes come only from
  the fixer agent's own Edit/Write calls.
- It does not change permissions or settings. A hook error is caught and logged, and the
  call passes through.

## Limits

- Plugin agents cannot set `permissionMode` or ship hooks in frontmatter; enforcement is
  the plugin-level hook above.
- Nesting: orchestrator → worker → sushi-build → sushi-fixer uses all three subagent layers.
  A worker that is itself at depth 3 has no Agent tool; a sushi-build at the depth limit
  escalates instead of fixing.
- One Bash call may last at most 10 minutes, so plain `run` suits short builds only; long
  builds need `run --detach` + `wait`. That a detached build survives the end of the Bash
  call that started it has been tested in a shell, not yet inside a live Claude session.
- In `claude -p` / Agent SDK with fork mode turned off, nested subagents start in the
  background and the spawning subagent does not wait. The fixer would then race the next
  build. Run SushiBuild from an interactive session or with fork mode on.

License: MIT.
