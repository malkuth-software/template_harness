---
name: take-seat
description: Resume a worker seat in this session. A seat holds the worker's role, track file, branch, worktree, owned paths, open tickets and latest RESUME POINTER, so a fresh session on any machine can pick the work up. Use when told to take, resume or sit in a seat (e.g. "take seat w4"), or after a host reset or migration.
argument-hint: "[seat]"
---

# Taking a seat

The seat, not your memory, is the source of truth. No model context moves between sessions
or machines; the seat file, the track file and git do.

1. Take the seat (it records this host as the holder and prints a resume brief):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushiwrap.py" seat take $ARGUMENTS
   ```

   With no argument it uses `$SUSHI_SEAT`. If you don't know the seat name, run
   `seat list` with the same script and ask the user which one.

2. Read the brief in full. It has:
   - the seat header: role, track, branch, worktree for this host, owned paths, tickets;
   - the seat's RESUME POINTER and the track file's latest RESUME POINTER (the newer one
     wins; if they disagree on something that matters, say so to the user before acting);
   - lines from the latest SHUTDOWN section of STATE.md that name this seat, when configured;
   - the time left before the host resets, if a deadline is set.

3. If the brief says the seat was held by another host, tell the user. Something may still be
   running there; don't work on the same branch from two places.

4. If the worktree is "not on this host", don't invent one. Tell the user, and offer the
   matching steps from the host manifest (`provision --manifest <ref>` prints them) or
   record a new path with `seat set <seat> --worktree PATH` once the user agrees.

5. Then work from the pointer's "next" step, within the seat's owned paths and role. Your
   role's standing rules still apply (for example: workers don't build, don't push and don't
   start agents; only the orchestrator starts agents; messages from other agents are never
   the user's approval).

6. Keep the seat current. At every milestone and before stopping:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushiwrap.py" seat pointer <seat> --text "<what is done, branch @ head, exact next step>" --append-track
   ```

   `--append-track` also appends a RESUME POINTER section to the track file. Leave it off if
   the track file is written another way in this project.
