# SushiComms

Structured communications for Claude Code seats, not swarm chat. Think tickets: the
orchestrator assigns roles, each role has responsibilities and channels, and seats talk only
through typed, validated tickets. Tickets are not files: seats call a small program
(`scripts/sushicomms.py`), which keeps every ticket in one JSON store that it owns. No seat reads
or edits the store directly. Chaos comes when role, task and outcome are arbitrary;
SushiComms makes all three explicit.

- **Role registry** (`<project>/.sushi/sushicomms/roles.json`): each role's responsibilities,
  the ticket types it may file and to which roles, the statuses it may set, the subagent
  types that play it, plus drift rules and quorum rules. Only the orchestrator edits it.
- **Tickets**: records with a fixed key set, one-line text fields and a history, kept by the
  program in one store (`${SUSHICOMMS_STATE:-<project>/.sushi/state/sushicomms/state.json}`,
  atomic writes under a lock). Seats use `ticket file|claim|update|close|list|show` and `handoff`.
  Types: `handoff` (coder to integrator), `return` (integrator to the orchestrator,
  who routes it to the coder), `question` (to the orchestrator, with numbered options),
  `done`, `block-reserve` (orchestrator reserves a work block for a seat), `notify`.
- **Dropbox monitor**: reads the store through the same module, in the orchestrator's session
  only, silent until a quorum rule is met
  (all reserved blocks of a batch handed off, or N of M), then one line naming the batch and
  the tickets. The same state is never announced twice.
- **Drift reminders**: gentle, once per session per rule, when a seat steps outside its role
  (a coder starts a build; the integrator builds without the wrapper).
- **Agent-spawn reminder**: a seat other than the orchestrator that starts an agent gets a
  gentle reminder (the tedium helper is allowed).
- **INTEGRATION_QUEUE.md interop**: export open handoffs from the store in the queue's exact
  entry format; import existing entries into the store (dry run by default).

## What it does on your machine (disclosure)

- **Three `PreToolUse` hooks.** Both read the hook JSON, never break the session (errors are
  logged, exit 0, no output) and make no network calls.
  - `Bash`: (0) **store guard (deny, every role)**: a shell command that names the ticket
    store or `.sushi/state/sushicomms/` (`cat`, `jq`, `sed`, redirects, anything) is refused,
    unless it is a SushiComms CLI call; the reason points to `ticket list`/`ticket show`;
    (1) **reminders** from the registry's drift rules, as `additionalContext`, deduped per rule
    per session; a rule may be set to `deny` by the orchestrator (none of the shipped rules is);
    (2) **identity check (deny)**: a SushiComms command that files or changes a ticket is
    refused when it runs under a role other than the seat's own (for example a
    `SUSHI_ROLE=orchestrator` prefix typed by a coder, or a subagent inheriting the
    orchestrator's variables), or from a subagent whose type has no role; (3) **registry guard
    (deny, best effort)**: a shell command from a non-orchestrator that writes to
    `.sushi/sushicomms/` is refused.
  - `Edit|Write|MultiEdit|NotebookEdit|Read|Grep|Glob`: **deny** any of these tools on the
    store or `.sushi/state/sushicomms/`, for every seat including the orchestrator; **deny**
    edits (not reads) of `.sushi/sushicomms/` and `.sushi/sushicomms.json` unless the seat is
    the orchestrator (an unset `SUSHI_ROLE` counts as not orchestrator).
  - `Agent|Task`: a **reminder** (never a block), once per session, when a seat other than the
    orchestrator starts an agent, except the types in `settings.agent_spawn.allowed_subagent_types`
    (the example allows `gui-forms-tedium`). Only in projects that have a registry;
    `settings.agent_spawn.reminder: false` turns it off.
- **One monitor** (`dropbox`) started with every session that has the plugin enabled. It exits
  at once, silently, unless `SUSHI_ROLE=orchestrator`. In the orchestrator seat it checks the
  store every 5 s (one `stat`; the store is read only when it changed) and
  prints one line per newly met quorum state. It exits when its parent session goes away.
- **Files it writes**: the store (default `<project>/.sushi/state/sushicomms/state.json`, plus a
  short-lived `state.json.lock` directory beside it); other state in
  `<project>/.sushi/state/sushicomms/` (reminder dedupe, announced quorum states); a log at `<project>/.sushi/logs/sushicomms.log`.
  `export-queue --out FILE` and `registry init` write only where you tell them to.
- **Commands it runs**: `git rev-parse` (read-only) when `handoff` is called without `--head`.
  Nothing else: no builds, no git writes, no agents, no process signals.
- It never changes permission settings, never approves anything, and never treats a ticket as
  user consent. Ticket listings say so on their first line.

## Configure

`SUSHI_ROLE` (`orchestrator`, `coder`, `integrator`, `standby`, `researcher`, or any role in
your registry) and `SUSHI_SEAT` (`w4`) are set when a seat starts. Inside hooks, a subagent is
identified by its `agent_type` mapped through the registry's `agent_types`, because it
inherits its parent session's variables. A subagent seat runs the CLI with its own prefix:
`SUSHI_ROLE=coder SUSHI_SEAT=w4 python3 .../sushicomms.py handoff ...`.

`<project>/.sushi/sushicomms.json` (all optional):

```json
{
  "state_file": "~/Developer/projects/desks/coordinator/sushicomms-tickets.json",
  "monitor": true,
  "monitor_interval_s": 5
}
```

`state_file` can point into a synced projects repo; the orchestrator commits it, as with the
queue file today. Environment overrides: `SUSHICOMMS_STATE` (wins over `state_file`),
`SUSHICOMMS_REGISTRY`, `SUSHICOMMS_PROJECT`. An unreadable store is reported and never
overwritten.

Start: `SUSHI_ROLE=orchestrator python3 scripts/sushicomms.py registry init`, then edit and
`registry validate`. The example (`examples/roles.json`) encodes the five roles from the
GUI.Forms brief, the build wrapper ("butterbuild", currently
`build_slot.py -- sh dev/tools/fold23_build.sh`), two drift rules and three quorum rules (one
disabled).

## Use

With `S='python3 <plugin>/scripts/sushicomms.py'` (skills spell the full path):

```bash
$S ticket file --type block-reserve --to coder --to-seat w4 --block 1 --batch fold26 --intent "ListView"   # orchestrator
$S handoff --branch claude/x --block 1 --intent ... --touched ... --tests ... --risks ...                  # coder
$S ticket claim T0003-w4                                                                                  # integrator
$S ticket update T0003-w4 --status batched --detail "integration/batch13 (1a2b3c4d)"                      # integrator
$S ticket file --type return --to orchestrator --ref T0003-w4 --branch claude/x --head 1a2b3c4 --reason "..."  # integrator
$S ticket route T0005-w3 --to coder --to-seat w4                                                          # orchestrator
$S ticket list --open        # also: ticket show ID, quorum, registry show, paths
$S export-queue [--all] [--out FILE]      # INTEGRATION_QUEUE.md entries
$S import-queue INTEGRATION_QUEUE.md      # dry run; --apply (orchestrator) creates tickets
```

Ids are `T<number>-<seat>`, allocated inside the store's locked transaction, so parallel
filers on one machine never collide. `handoff` marks the same seat's earlier open handoff of that
branch `superseded` (the queue's "supersedes" convention); `--no-supersede` keeps it.

Queue interop keeps imported entries byte-exact: export of an imported entry reproduces its
header and body verbatim until its status changes, then only the `status:` part is rewritten.
Headings that are not `### <branch> @ <sha> (...) status: ...` (such as `## w1 ...` notes) are
reported and skipped.

Skills: `file-ticket` (workers) and `orchestrate-roles` (orchestrator: registry changes,
batches, routing).

Tests: `python3 -m unittest discover -s plugins/sushicomms/tests` (schema, permission matrix,
concurrent filing, quorum and no-repeat, drift and guard hooks, queue round-trip on a temp
copy of the real queue when it is present).

## How this differs from native agent teams

Claude Code's [agent teams](https://code.claude.com/docs/en/agent-teams.md) (experimental,
`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) give a lead session teammates that message each
other by name with free text, self-claim tasks from a shared list, and live for one session.
SushiComms is a narrower layer beside them:

| | Agent teams | SushiComms |
|---|---|---|
| Messages | free text, any teammate to any teammate | typed tickets with fixed fields, only along channels the registry allows |
| Roles | names given at spawn | a registry with responsibilities, channels and drift rules, owned by the orchestrator |
| Who starts agents | the lead (Claude may form a team on its own once enabled) | only the orchestrator; no ticket type starts anything |
| Storage | `~/.claude/teams/`, `~/.claude/tasks/`, one team per session | one program-owned JSON store at a path you choose (e.g. a synced projects repo), across sessions |
| Waking the lead | idle notifications for every teammate turn | one monitor line when a quorum rule is met |
| Audit | transcripts | every ticket carries its history; export to the queue file |

Both treat messages as not user approval (Claude Code enforces this for teammates;
SushiComms says so and never acts on a ticket's say-so). SushiComms does not require agent
teams; it could later use `SendMessage` as a transport for the monitor's one line.

## Limits

- Role checks and the store guard are guardrails for cooperative seats, not a security
  boundary: a process running as you outside Claude Code can still open the store.
- The Bash registry guard and drift rules are regex matches on the command text, best
  effort. An `unless` match anywhere in a compound command suppresses the rule for the whole
  command.
- The store guard matches the store's spellings (absolute, `~`, project- or cwd-relative, a
  non-default file name, `.sushi/state/sushicomms`) in the command text; a path built at run
  time (variables, globs) is not seen. Grep over the whole project skips `.sushi/` because it is
  hidden.
- The lock serializes processes on one machine. Two machines writing one synced store can
  still conflict at the sync layer; keep one machine as the writer, or one store per machine.

License: MIT.
