---
name: file-ticket
description: File, claim or update a SushiComms ticket as a worker seat (coder, integrator, standby, researcher). Use when handing a finished branch to the integrator, returning a batch problem, asking the orchestrator a decision, reporting done, or checking tickets addressed to you.
---

# Filing a SushiComms ticket

File handoffs, returns and questions as tickets: records with fixed fields, kept by the
SushiComms program in its store (not files you open).

The CLI is:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushicomms.py" <command>
```

## 1. Know your seat
Your role and seat come from `SUSHI_ROLE` and `SUSHI_SEAT`. If you are a subagent started
inside the orchestrator's session, those variables belong to the orchestrator: prefix every
filing command with your own, e.g. `SUSHI_ROLE=coder SUSHI_SEAT=w4 python3 ...`. A hook refuses
a filing whose role does not match your agent type. To see what your role may file and to whom:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushicomms.py" registry show
```

## 2. Pick the ticket type
| You are | Situation | Command |
|---|---|---|
| coder | branch finished | `handoff --branch B --intent ... --touched ... --tests ... --risks ... [--base SHA] [--block K]` (files the handoff to the integrator and a notify to the orchestrator; `--head` defaults to the branch tip) |
| any worker | a decision that is the orchestrator's | `ticket file --type question --to orchestrator --question "..." --option "..." --option "..." --recommend 1` |
| any worker | assigned item done (no branch to hand off) | `ticket file --type done --to orchestrator --summary "..."` |
| integrator | substantive problem in a batch | `ticket file --type return --to orchestrator --ref <handoff id> --branch B --head H --reason "..." --failing "<test or log path>"`, then `ticket update <handoff id> --status returned --detail "(reason)"` |
| integrator | taking and folding work | `ticket claim ID`, `ticket update ID --status batched --detail "integration/batchN (sha)"`, later `fixed`/`folded` |

Field rules: every text field is one line. `intent` says what changed in one line; `touched`
lists files or areas; `tests` names the tests that should cover it; `risks` says what could not
be checked without a build. Keep the queue's Minor/Substantive split: Minor breakage the
integrator fixes itself, Substantive goes back as a `return`.

## 3. Check what is addressed to you

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushicomms.py" ticket list --mine --open
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushicomms.py" ticket show <ID>
```

## 4. Avoid
- Reading or editing the ticket store yourself (`cat`, `jq`, `Read`, redirects): hooks refuse it for every seat.
  Use `ticket list` and `ticket show`. The registry is edited by the orchestrator only.
- Starting agents (a hook notes it; the `gui-forms-tedium` helper is allowed), pushing, or asking another worker to do something: send it to the orchestrator as a ticket.
- Polling `ticket list` in a loop. File, report in your final message, and end your turn.

If a filing is refused, the message names the rule. If the rule looks wrong, ask the
orchestrator with a `question` ticket.
