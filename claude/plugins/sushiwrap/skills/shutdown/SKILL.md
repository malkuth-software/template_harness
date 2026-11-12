---
name: shutdown
description: Prepare this session's seats for a host reset or planned stop - commit work in progress to the seat branch by path, update the resume note, push only if allowed, check with shutdown-check, and (coordinator) add a shutdown entry with the resume order to STATE.md. Use when a SushiWrap reset reminder arrives (30, 15 or 5 minutes before the reset), when the user says the machine will reboot or reset, or when asked to wind down.
argument-hint: "[seat]"
---

# Shutdown before a host reset

Goal: everything a fresh session needs is in git and in the seat and track files.

Script: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushiwrap.py"` (called `sushiwrap` below).

## 0. How long is left?

`sushiwrap deadline show`. Under 5 minutes, do steps 1 and 2 only.

## 1. Commit work in progress to the seat branch

In the seat's worktree, on the seat branch: commit the files you changed, even half-done work,
by path (`git add <paths>` then `git commit -m "WIP: <what and where it stops>"`). Don't use
`git commit -a`: it also picks up other people's uncommitted edits. Don't stash in a shared tree.

## 2. Update the resume note

```bash
sushiwrap seat pointer <seat> --text "<done so far; branch @ head; uncommitted or unverified risks; exact next step>" --append-track
```

Include enough for a new session on another machine to continue from it alone.

## 3. Push only if allowed

Push the seat branch only if this project allows it for your role (`push_allowed` in the
SushiWrap config, and the project's own rules). Where workers don't push, report the branch
and head instead.

## 4. Check

```bash
sushiwrap shutdown-check --seat <seat>
```

Fix every FAIL (a stale resume note, uncommitted changes, an incomplete memory mirror). Report
WARN lines to the user rather than fixing them silently (for example, a worktree on the
wrong branch).

## 5. Coordinator only: the shutdown entry

After the workers report:

1. `sushiwrap shutdown-check` (all seats) and fix or report what fails.
2. `sushiwrap shutdown-template --reason "<why>"` prints a draft:
   `## SHUTDOWN <date> (<reason>)` and a numbered `Resume order:` built from the seats.
   Keep the `SHUTDOWN` heading word: `seat take` finds the entry by it. Edit the draft (PRs
   in flight, open owner questions, what was cut off) and append it to the project's
   STATE.md yourself. The command doesn't write STATE.md.
3. Make sure the memory mirror is complete (shutdown-check's `[mirror]` line), then commit
   the coordination repo by path.
4. Tell the user it's safe to reset, in one or two lines.
