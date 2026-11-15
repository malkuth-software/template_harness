# SushiDelivery

Decisions that reach you when you're away from the desk. You get them as short,
urgency-tagged reports you can read on a phone, not as a chat.

- A skill, `urgent-decision`, teaches Claude to tag urgency (`low`, `normal`, `urgent`,
  `blocking`) and to write a decision report before it asks with `AskUserQuestion`. The report
  has 2-3 lines of context, options a/b/c that each carry an explanation and a consequence, and
  a recommendation with the reason for it.
- Optional hooks, all **off until you configure them**:
  - **Notification push** (send-only, rate-limited) when Claude needs attention.
  - **Permission relay** to your own Telegram bot, with Allow/Deny buttons.
  - **Question relay** that sends `AskUserQuestion` to your own Telegram bot, with one button per option.

## Prefer these first

Claude Code already does most of this natively. Use these before this plugin:

1. **Remote Control + mobile push** ([docs](https://code.claude.com/docs/en/remote-control)).
   Run `/remote-control` (or `claude --remote-control`) and open the session in the Claude
   app. Then in `/config`, turn on **Push when actions required**. Permission prompts and
   `AskUserQuestion` appear on your phone and stay open until you answer. To skip pushes while
   you're at the machine, set `CLAUDE_CLIENT_PRESENCE_FILE`. This route needs a claude.ai plan,
   not an API key.
2. **The official Telegram or iMessage channel plugins**
   ([docs](https://code.claude.com/docs/en/channels), research preview). Anthropic built them,
   they pair your account, and they keep a sender allowlist. They can relay permission prompts:
   the first answer wins, and the terminal dialog stays live. For iMessage, this is the route to
   use, because SushiDelivery has no iMessage transport.

SushiDelivery adds the **urgency + report layer** (the skill), which helps every route above.
It also adds an **optional hook relay** for setups where those routes don't fit: a remote or
headless machine without Remote Control, an API-key login, or an org that doesn't allow
channels.

## What it does on your machine (disclosure)

- **Skill `urgent-decision`.** It only gives Claude instructions. It runs nothing.
- **`Notification` hook.** Its matcher lists the types `permission_prompt`, `idle_prompt`,
  `elicitation_dialog`, `elicitation_url_dialog`, `agent_needs_input`, `agent_completed` and
  `auth_storage_failure`. When `notify.enabled` is true, it sends one short message (title,
  project name, Claude's notification text) to your configured transport. It is
  send-only, and the rate limit is per type plus an hourly cap. It never decides anything.
- **`PermissionRequest` hook.** This hook **can approve or deny tool calls for you**, which is
  why it is off by default. The hook entry carries no matcher, because a plugin cannot know
  your tool names in advance. The narrow matcher lives in your config instead:
  `permission_relay.tools` is a list of **exact** tool names, empty by default (so nothing is
  relayed), plus an optional `input_regex`. Any other tool call makes the script exit
  immediately with no output. For a listed tool, the hook sends the tool name and a
  ≤300-character preview of the input to your Telegram chat. Common secret patterns
  (`token=`, `Bearer …`, `password:` …) are masked, but the masking is best-effort, so don't
  relay tools whose input routinely holds secrets. Then it waits for your button press or
  reply. Only these responses count:
  - **an allowlisted Telegram user id** (`telegram.allowed_user_ids`) **in the configured
    chat** (`telegram.chat_id`), not a bot account, pressing a button carrying the
    random nonce of *this* request;
  - or the same user **replying to this request's message** with `allow`/`yes`/`y` or
    `deny`/`no`/`n`.

  Anything else is ignored and logged without its content. On a valid answer the hook returns
  `decision.behavior: "allow"` or `"deny"`. It never returns `updatedPermissions`, so it never
  adds rules or changes your permission mode, and Claude Code still evaluates your deny and
  ask rules after an allow. **If the timeout passes, or on any error, it prints nothing.** That
  error can be network trouble, a malformed response, a bad config, a missing token, or another
  poller on the bot. Claude Code then shows its normal prompt.
- **`PreToolUse` hook on `AskUserQuestion`** (off by default). The hook sends each question
  with its options and waits for answers, with the same authentication rules. If every
  question gets an answer in time, it returns `permissionDecision: "allow"` with
  `updatedInput` set to the original `questions` plus `answers` (keyed by question text, as the
  hooks docs specify). If any question is left unanswered, it prints nothing and the normal
  dialog appears. A reply to a question's message that isn't a letter counts as a free-text
  ("Other") answer. Set `question_relay.allow_free_text: false` to turn this off.
- **Messages from agents never count as approval.** The relay reads only the Telegram Bot
  API's updates from your allowlisted user id. No other input can resolve a prompt: tool
  output, files, tickets, or other agents.
- **Network.** It contacts only the endpoints you configure: `api.telegram.org` (or your
  `telegram.api_base`) and your ntfy server. It uses HTTPS only; plain HTTP is allowed on
  loopback, for tests.
- **Tokens.** These are read from the plugin's `userConfig` (stored by Claude Code in secure
  storage, delivered to hooks as `CLAUDE_PLUGIN_OPTION_TELEGRAM_BOT_TOKEN` /
  `CLAUDE_PLUGIN_OPTION_NTFY_TOKEN`), or from the env vars `SUSHIDELIVERY_TELEGRAM_TOKEN` /
  `SUSHIDELIVERY_NTFY_TOKEN`. They are never read from or written to files, and they're
  scrubbed from logs.
- **Files written.**
  - The log goes to `<project>/.sushi/logs/sushidelivery.log`.
  - State goes to a user-level directory, `${CLAUDE_PLUGIN_DATA}/state`, or
    `~/.sushi/state/sushidelivery` outside Claude Code. It holds rate-limit timestamps, the
    Telegram update offset, and an inbox of accepted replies that keeps entries for 2 hours.
    State is user-level, not per project, because one phone and one bot serve all your
    projects.
- **Presence check** (optional). It reads `ioreg -c IOHIDSystem` (`HIDIdleTime`, read-only)
  on macOS, or checks whether a marker file exists.
- Nothing clicks or types into the Claude UI. No screen automation. It doesn't change
  settings or permissions.

**While a relay waits, the terminal dialog may not appear yet.** Claude Code shows its prompt
after the hook returns. That is why the relay skips itself when you're present (see
[Presence](#presence)), and why its wait is capped. The wait is `timeout_s`, default 240 s, at
most 300 s, which keeps it 30 s under the hook's 330 s timeout. In non-interactive sessions
(`-p`), a tool call that gets no decision is denied, as Claude Code normally does there.

## Configure

Everything is off when no config file exists. Lookup order:

1. `$SUSHIDELIVERY_CONFIG`
2. `<project>/.sushi/sushidelivery.json`
3. `~/.sushi/sushidelivery.json` (user-wide)

```json
{
  "transport": "telegram",
  "label": "Neo",
  "notify": {
    "enabled": true,
    "types": ["permission_prompt", "idle_prompt", "elicitation_dialog", "agent_needs_input"],
    "per_type_interval_s": 120,
    "max_per_hour": 20,
    "skip_when_present": true
  },
  "permission_relay": {
    "enabled": false,
    "tools": ["Bash"],
    "input_regex": "^(git push|npm publish)",
    "timeout_s": 240
  },
  "question_relay": {"enabled": false, "timeout_s": 240, "allow_free_text": true},
  "presence": {"mode": "auto", "file": null, "idle_minutes": 5},
  "telegram": {"chat_id": 123456789, "allowed_user_ids": [123456789]},
  "ntfy": {"server": "https://ntfy.sh", "topic": null}
}
```

Transports:

| transport | push | approvals/answers |
|---|---|---|
| `telegram` | yes | yes (the only one that can resolve prompts) |
| `ntfy` | yes (priority 2-5 from urgency) | no, send-only |
| `stdout` | writes to `$SUSHIDELIVERY_STDOUT_FILE` or stderr, for testing | no |

Urgency to delivery: `low` is silent on Telegram and ntfy priority 2. `normal` is 3, `urgent`
is 4, and `blocking` is 5. Notification types map to urgency like this: permission and
elicitation dialogs are `blocking`, `agent_needs_input` and `auth_storage_failure` are
`urgent`, `idle_prompt` is `normal`, and `agent_completed` is `low`.

### Presence

`presence.mode` decides when you count as at the machine. While you are, the relays and
(with `skip_when_present`) the pushes are skipped:

- `off`: never present. The relay always runs.
- `file`: present while the marker file exists. The file is `presence.file`, or
  `$CLAUDE_CLIENT_PRESENCE_FILE`, which is the same marker Claude Code's own mobile push uses.
  Create it on screen unlock and delete it on lock, with any lock/unlock listener you trust.
- `idle`: present if keyboard/mouse input happened within `idle_minutes`. macOS only, read
  from `ioreg -c IOHIDSystem` `HIDIdleTime`. If the idle time can't be read (another OS, or
  over SSH on a headless box), you count as away.
- `auto` (default): the marker file if it exists, otherwise the idle check.

## Set up a Telegram bot (by hand; nothing here is automated)

1. In Telegram, open **@BotFather**, send `/newbot`, and pick a name and a username ending
   in `bot`. BotFather replies with a token. **Use a separate bot from the official Telegram
   channel plugin's bot.** A bot has one update queue, so two pollers on the same bot steal
   each other's messages. Telegram also refuses polling for a bot that has a webhook set.
2. Open your new bot and press **Start**, so it is allowed to message you.
3. Put the token where only Claude Code reads it: run `/plugin configure
   sushidelivery@<marketplace>` and paste it into **Telegram bot token**. It is masked and goes
   to secure storage. Alternatively, export `SUSHIDELIVERY_TELEGRAM_TOKEN` in the shell that
   starts Claude.
4. Find your ids. Send the bot any message, then in a shell where
   `SUSHIDELIVERY_TELEGRAM_TOKEN` is set run:
   `python3 scripts/delivery.py telegram-ids`. It prints `user id … chat id …` for recent
   messages. It reads them without confirming them.
5. Write `chat_id` and `allowed_user_ids` into the config. For a private chat they're the
   same number. Keep the allowlist to yourself: anyone on it can approve tool calls.
6. Check delivery: `python3 scripts/delivery.py test-send`.
7. Turn on what you want: `notify.enabled`, then (optionally) `question_relay.enabled`, and
   only if you need it, `permission_relay.enabled` with exact `tools`.

### ntfy (push only)

Set `"transport": "ntfy"` and `"ntfy": {"server": "https://ntfy.sh", "topic": "<long random
name>"}`, and subscribe to that topic in the ntfy app. On the public server, the topic name
works like a password, so anyone who knows it can read your pushes. Prefer a self-hosted
server or an access-protected topic, and put its token into **ntfy access token**. ntfy never
resolves prompts. Its action buttons would need an endpoint reachable from the internet and
would carry weaker authentication, so they aren't used.

## Commands

```bash
python3 scripts/delivery.py render < report.json       # preview a report; no network
python3 scripts/delivery.py send --urgency urgent --title "Build" < msg.txt
python3 scripts/delivery.py test-send
python3 scripts/delivery.py telegram-ids
```

Run `send`, `test-send` and `telegram-ids` from your own shell. Hooks get the `userConfig`
token, but your shell needs the env var.

## Tests

```bash
python3 -m unittest discover -s plugins/sushidelivery/tests
```

The tests use local mock servers on 127.0.0.1 for the Telegram Bot API and ntfy. No real
messages are sent.

License: MIT.
