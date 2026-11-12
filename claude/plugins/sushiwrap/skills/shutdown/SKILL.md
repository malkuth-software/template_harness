---
name: shutdown
description: Prepare this session's seats for a host reset or planned stop - commit WIP to the seat branch, write the RESUME POINTER, push only if allowed, check with shutdown-check, and (coordinator) append a SHUTDOWN section with a numbered Resume order to STATE.md. Use when a SushiWrap T-30/T-15/T-5 notice arrives, when the user says the machine will reboot or reset, or when asked to wind down.
argument-hint: "[seat]"
---

# Shutdown before a host reset

The aim: everything a fresh session needs is in git and in the seat and track files, so
the reset loses nothing but the conversation.

Script: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushiwrap.py"` (called `sushiwrap` below).

## 0. How long is left?

`sushiwrap deadline show`. Under 5 minutes, do steps 1 and 2 only, as fast as you can.

## 1. Commit WIP to the seat branch

In the seat's worktree, on the seat branch: commit the files you changed, even half-done work,
by path (`git add <paths>` then `git commit -m "WIP: <what and where it stops>"`). Don't use
`git commit -a`: it also picks up other people's uncommitted edits. Never stash in a shared tree.

## 2. Write the RESUME POINTER

```bash
sushiwrap seat pointer <seat> --text "<done so far; branch @ head; uncommitted or unverified risks; exact next step>" --append-track
```

Write it for a stranger: a fresh model on another machine must be able to act on it alone.

## 3. Push only if allowed

Push the seat branch only if this project allows it for your role (`push_allowed` in the
SushiWrap config, and the project's own rules). Where workers never push, report the branch
and head instead.

## 4. Check

```bash
sushiwrap shutdown-check --seat <seat>
```

Fix every FAIL (a stale pointer, uncommitted changes, an incomplete memory mirror). Report
WARN lines to the user rather than fixing them silently (for example, a worktree on the
wrong branch).

## 5. Coordinator only: the SHUTDOWN section

After the workers report:

1. `sushiwrap shutdown-check` (all seats) and fix or report what fails.
2. `sushiwrap shutdown-template --reason "<why>"` prints a draft:
   `## SHUTDOWN <date> (<reason>)` and a numbered `Resume order:` built from the seats.
   Edit it (PRs in flight, open owner questions, what was cut off) and append it to the
   project's STATE.md yourself. The command never writes STATE.md.
3. Make sure the memory mirror is complete (shutdown-check's `[mirror]` line), then commit
   the coordination repo.
4. Tell the user it's safe to reset, in one or two lines.
