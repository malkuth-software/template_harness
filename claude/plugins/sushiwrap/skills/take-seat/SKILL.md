---
name: take-seat
description: Resume a worker seat in this session. A seat is a worker record - role, track file, branch, worktree, owned paths, open tickets and latest resume note - so a fresh session on any machine can pick the work up. Use when told to take or resume a seat (e.g. "take seat w4"), or after a host reset or migration.
argument-hint: "[seat]"
---

# Taking a seat

Resume from the seat file, the track file and git; no conversation carries over between
sessions or machines.

1. Claim the seat for this host and print a resume brief:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushiwrap.py" seat take $ARGUMENTS
   ```

   With no argument it uses `$SUSHI_SEAT`. If you don't know the seat name, run
   `seat list` with the same script and ask the user which one.

2. Read the brief in full. It has:
   - the seat header: role, track, branch, worktree for this host, owned paths, tickets;
   - the seat's resume note and the track file's latest resume note (use the newer one; if
     they disagree on something that matters, tell the user before acting);
   - lines from the latest shutdown entry in STATE.md that name this seat, when configured;
   - the time left before the host resets, if a deadline is set.

3. If the brief says another host claimed the seat, tell the user. Something may still be
   running there; don't work on the same branch from two places.

4. If the worktree is "not on this host", tell the user and offer the matching steps from
   the host manifest (`provision --manifest <ref>` prints them), or record a new path with
   `seat set <seat> --worktree PATH` once the user agrees.

5. Then continue from the resume note's next step, within the seat's owned paths and role.
   The project's role rules apply as usual.

6. Keep the seat current. At every milestone and before stopping:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushiwrap.py" seat pointer <seat> --text "<what is done, branch @ head, exact next step>" --append-track
   ```

   `--append-track` also appends a `## Resume note` section to the track file. Leave it off
   if the track file is written another way in this project.
