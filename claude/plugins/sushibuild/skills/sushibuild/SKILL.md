---
name: sushibuild
description: Hand a build to the SushiBuild triage agent (Haiku), which runs the build command, routes small one-file compiler errors to a Sonnet fixer, and returns one SUSHIBUILD RESULT block. Use when you have finished a batch of code and your role allows building (for example the integrator), instead of running the build and fixing errors yourself.
---

# Building through SushiBuild

## Call it
Use the Agent tool once:

- `subagent_type`: `sushibuild:sushi-build`
- `description`: `SushiBuild: <short what>`
- `prompt`: the exact build command, plus anything the triage agent must know. For example:

      Build command: python3 dev/tools/build_slot.py -- sh dev/tools/fold23_build.sh
      Files I changed this batch: src/form_layout.cpp, src/form_layout.h
      Do not touch generated files under gen/.

  If the build may take more than about 8 minutes, add "This build is long: use detach."
  (the agent then runs it detached and polls, since one Bash call is capped at 10 minutes).
  If the project sets `build_command` in `.sushi/sushibuild.json`, you may write
  "Use the configured build command." instead.
- `model`: leave it out, unless `.sushi/sushibuild.json` sets `triage_model`; then pass that
  value (an alias such as `haiku`, `sonnet`, `opus`). This is how the decision model is
  swapped per project.

Then wait for it. Do not edit code while it runs: the fixer may be editing a file you would
touch. You get nothing from it until it finishes; that is the point. If your session started
it in the background, its result arrives as one completion notification.

## What comes back
One block, from `=== SUSHIBUILD RESULT ===` to `=== END SUSHIBUILD ===`:

- `outcome: success`: the build passed. `fixups:` lists every edit the fixer made (file,
  error, fix, diff size). Review those few lines; the full diffs are in the `fixups file`
  (`.sushi/state/sushibuild/fixups.jsonl`, field `diff`).
- `outcome: escalated`: the triage agent judged an error big (linker, cross-file, template
  cascade, design question, or unsure). `reason:` says why and `log excerpt:` shows the
  failing lines; the full log is at `log:`. The fixups made before that point are still in
  the tree. Fix the big problem yourself (or hand it on), then call SushiBuild again.
- `outcome: capped`: a cap was hit (fix count, wall time, or the same error twice). Treat it
  like escalated.
- `outcome: failed`: no build command, or the tooling broke. Read `reason:`.

## How the human watches
The triage agent's work is visible while it runs, without reaching you:
- CLI: `/tasks`, select the SushiBuild task, Enter to open its transcript (nested fixer runs
  show as a tree under it).
- Desktop app: the subagent view for the running task.
- On disk: `~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl`, and the
  plugin's own log at `.sushi/logs/sushibuild.log`.
