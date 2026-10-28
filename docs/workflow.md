# The default workflow: local checkpoints

Every harness product works this way unless its owner says otherwise. Work
passes locally, accumulates locally from several workers, and reaches
GitHub only as one verified checkpoint. A remote CI run is the expensive
step, so a day should need only a handful of them: GUI.Forms budgets six to
eight. The checkpoint tool (`checkpoint/checkpoint.py`, configured by the
project file's `[checkpoint]` table) does the folding, the verification
record and the push; `docs/adopting.md` §6 sets it up.

GUI.Forms is the worked example throughout. It adopted this workflow on
2026-10-28, and each rule below names the problem it answers.

## 1. Roles

- **Workers** each own one long-running track and one workspace: a clone of
  the product with the harness beside it at the `harness.lock` pin. A
  worker is reused for its track rather than started per task, and never
  starts helpers of its own.
- **The coordinator** owns the integration workspace, folds the workers'
  branches, verifies, pushes, merges and brings decisions to the owner.
- **The build host** is wherever the product builds. When the machine the
  workers edit on cannot build (GUI.Forms' laptop has no LLVM 22 runtime),
  a second machine builds every workspace through one sync-and-run wrapper
  (§6).

## 2. Workers

- Commit on local branches in your own clone. Never push, open a pull
  request, merge, enable auto-merge or dispatch a workflow.
- Pass locally before reporting: the touched tests plain and under the
  sanitizers, the checkers (house style, lifetime rules), the generated
  docs, registry or schema checks you touched, and the state-machine gate
  over your change (`check_state_machines.py --gate --changed-from
  origin/main` on a configured tree).
- Report the branch, its head and what ran, then end your turn. The
  coordinator folds; you do not wait for anything.
- Stack later work on your own earlier local branches. When a checkpoint
  lands, rebase onto the new main and delete the branches `after-merge`
  lists as contained.
- Keep a track file with the state, the decisions in force and the next
  step, so a successor can resume from it alone.

## 3. The coordinator's cycle

```sh
export HARNESS_WORKSPACE=<integration workspace> HARNESS_PROJECT=<harness>/projects/<name>.toml
python3 template_harness/checkpoint/checkpoint.py fold <clone>:<branch> ... --title N=<subject> ...
python3 template_harness/checkpoint/checkpoint.py verify
python3 template_harness/checkpoint/checkpoint.py push
python3 template_harness/checkpoint/checkpoint.py after-merge     # once the PR has merged
```

- **Fold only what is ready:** branches whose worker reported a local pass
  and whose diff the coordinator has read. A checkpoint that waits a little
  for a second or third track costs nothing; an extra remote run does.
- **A stack folds as its top branch.** Folding each branch of a stack
  applies the lower branches' changes twice and stops the fold. Fold the
  top, and title it for everything it carries.
- **Two branches that append to the same file conflict** (two reports adding
  sections to one README did). Join them first, in a spare clone, keeping
  both, and fold the joined branch.
- **`push` opens one PR and turns on auto-merge**, which needs branch
  protection with required checks. A repository without protection (the
  harness itself) cannot auto-merge; the coordinator merges it once green.
- **When a checkpoint fails CI,** turn its auto-merge off, cancel its
  remaining runs (`gh run cancel`, by branch and by commit) so they stop
  spending the budget, have the owning worker fix it locally, refold, verify
  and push a new checkpoint. Close the old PR with a pointer to the new one.
- **After the merge:** run `after-merge`, tell every worker the new main,
  and merge the notes for what actually landed. Notes describing work that
  a later decision replaces wait.

## 4. What `verify` must run

`verify` is everything CI would reject, in order of cheapness, so the remote
run confirms rather than discovers. GUI.Forms' first checkpoint ran only the
test suites and failed CI on a state-machine finding; its verify now runs:

1. CI's static gates: the harness and tool tests, the registry, schema and
   generated-docs checks, house style, lifetime rules and the integrity
   gate;
2. the whole suite, plain;
3. the state-machine gate over the files changed since main, on the plain
   tree's compile commands;
4. the whole suite under the sanitizers.

Leave to CI only what cannot run locally: other operating systems, and
checks pinned to a host the build host is not (GUI.Forms' layout oracle
pins a linux64/win64 Chromium). A local tree may compile more than CI's
gate tree does (the macOS host and Skia sources), so it can find more;
fix those too.

## 5. Budget and evidence

- Count remote runs per day. A per-fix push, a per-worker PR, a manual
  rerun and a profile dispatch each count.
- A manual rerun is evidence only for a failure the coordinator has already
  diagnosed as outside the change; it is never how a flake is accepted.
- Reference data a CI check compares against is recorded on the host that
  checks it. GUI.Forms' text references captured on Windows drifted 1/16 px
  from CI's Linux capture; the fix was to adopt CI's own uploaded capture,
  not to widen the tolerance.

## 6. A remote build host

When workers edit on one machine and build on another, one wrapper does
both halves (GUI.Forms': `m4ws <workspace> <command>`, kept with the
coordination files). What it needed:

- **Sync by content, not by time:** `rsync --checksum --no-times`. Synced
  files keeping the editing machine's older mtimes left stale objects in
  earlier mirrors; with no times, a changed file is always newer than its
  object.
- **Never delete what lives only on the build host:** exclude the build
  trees, the compiler cache, `.ci/` and the fetched third-party sources from
  `--delete`.
- **One command at a time** on the host, behind a lock directory holding its
  owner's pid (stale when the owner is gone). Activated GUI tests in two
  trees take focus from each other, and two full-width builds only
  oversubscribe the cores.
- **Wait for an idle host before activated GUI tests:** the checkpoint
  verify starts only after a few minutes without keyboard or mouse input
  (`ioreg`'s `HIDIdleTime` on macOS). Someone using the machine takes focus
  from the tests.
- **The harness variables per workspace:** set `HARNESS_WORKSPACE` and
  `HARNESS_PROJECT` for the workspace being run, or the harness tools stop
  with "No project file".
- **One shared environment file,** sourced before every command:
  - the pinned compiler on `PATH` and the product's runtime variables
    (GUI.Forms: `GUI_FORMS_LLVM_RUNTIME`);
  - `CMAKE_C_COMPILER_LAUNCHER`, `CMAKE_CXX_COMPILER_LAUNCHER` and
    `CMAKE_OBJCXX_COMPILER_LAUNCHER=ccache`, which CI sets in its
    environment rather than in the build driver, so a local tree without
    them caches nothing;
  - one `CCACHE_DIR` with `CCACHE_BASEDIR` at the workspaces' common root,
    so every workspace hits the same entries (a second workspace built in
    24 s at 99% hits after a 271 s cold build);
  - on macOS, `SDKROOT="$(xcrun --show-sdk-path)"` (libclang, used by the
    AST reader, finds no system headers without it) and a resolved
    `TMPDIR` (`/var` is a link to `/private/var`, and path comparisons
    differ).
- **Fetch third-party sources once,** at their pins, and link them into
  every workspace. Fetch everything the install step reads, not only what
  the compiler reads (GUI.Forms' install copies miniaudio's `LICENSE`).

## 7. Agents

- Workers run as subagents of the coordinator's session, on the machine
  where they edit. Sessions on other machines cooperating by message proved
  unreliable and need someone to start each one; the build host only
  builds.
- Give the worker agent type a compact window of its own (Claude Code's
  agent definition field `autoCompactWindow`; GUI.Forms uses 400000) rather
  than a session-wide setting, which would also shrink the coordinator's.
- Reuse a worker for its track through messages. Replace a worker whose
  context has grown too large with one fresh worker on the same track file,
  never alongside it.

## 8. Pitfalls met in practice

- Apple's SDK defines `__restrict` as empty in C++. A restrict macro must
  spell `__restrict__` for Clang and GCC (and `__restrict` only for MSVC
  without Clang), as bfft's and stx_conv's macros already did; otherwise
  every use compiles to nothing on macOS and any timing taken there is not
  a restrict effect.
- A fixed delay before posting input to a window races its presentation.
  Poll for the state the input needs (key and visible), with a limit.
- A checker that scopes to changed files also counts every older finding in
  a file a change touches, and a changed header brings its includers in.
  Clearing a whole tree's findings once stops them reappearing in unrelated
  checkpoints.
