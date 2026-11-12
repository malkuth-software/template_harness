# SushiStyle

House coding rules for C and C++, written as tested clang AST matchers. After every
`Edit`/`Write` of a C/C++ file, SushiStyle checks the file and, if it finds something new,
gives Claude a short note in its context (not a user turn, and not a denial).
The same rules run as a hard gate in CI with `check --gate`.

## What it does on your machine (disclosure)

- Registers one `PostToolUse` hook on `Edit|Write|MultiEdit`. It ignores non-C/C++ files.
- **Regex-tier rules** run locally in Python (cheap).
- **Clang-tier rules** run `clang-query` on the **build host you configure**, over SSH:
  the edited file's text is sent over stdin into a temporary directory on that host, parsed
  once at `nice 15`, then deleted. The host check is skipped (silently, logged) when the
  host's 1-minute load exceeds `max_load_fraction` of its cores or its SushiStyle slots are
  busy. With no host configured, the clang tier is off. It never runs clang on the machine
  hosting Claude unless you set `allow_local_clang: true`.
- Writes reminder state to `<project>/.sushi/state/` (so each finding is reminded once) and a
  log to `<project>/.sushi/logs/sushistyle.log`.
- It does not change permissions, settings, or files you are editing. No network access other
  than the SSH connection to your own host.

## Configure

Config files, later ones overriding earlier: `~/.sushi/sushistyle.json` (per machine: the build
host, `clang_query`, `path_map`), then `<project>/.sushi/sushistyle.json` (the project wins; `remote`
merges key by key). `$SUSHISTYLE_CONFIG` replaces both.

A `rule_packs` entry is a pack name (`<project>/.sushi/rules/<name>.json`, then the plugin's own
`rules/<name>.json`) or a path to a pack file, relative to the project root or absolute, such as a
harness's shared pack: `"rule_packs": ["../template_harness/claude/rules/house.json"]`. Fixtures
resolve beside the pack file.

`<project>/.sushi/sushistyle.json`:

```json
{
  "rule_packs": ["house"],
  "remote": {
    "host": "m4mini",
    "clang_query": "/opt/homebrew/opt/llvm/bin/clang-query",
    "args": ["-std=c++23"],
    "include_dirs": ["include", "src"],
    "path_map": [{"local": "/Users/me/Developer/", "remote": "/Users/me/Developer/Projects/"}],
    "max_load_fraction": 0.7,
    "slots": 1,
    "timeout_s": 25
  },
  "allow": [
    {"rule": "HS-vague-name", "files": "third_party/*", "text": null}
  ]
}
```

`path_map` translates local include directories to where the same tree lives on the host.
Entries are `{"local": prefix, "remote": prefix}`, the form SushiBuild uses too; a legacy
`[local, remote]` pair still works (SushiBuild's legacy pairs run the other way, which is why
named keys are the convention).
The host needs SSH key access (BatchMode) and LLVM's `clang-query`.

Per-line exceptions: `// sushi-allow: <rule-id>[, <rule-id>] <reason>` at the end of the line,
or as a comment line of its own directly above.

## Rules

Rule packs are JSON files: the bundled `rules/house.json`, or your own in
`<project>/.sushi/rules/<pack>.json` (a project pack overrides a bundled pack of the same
name). Every rule needs a fail fixture (lines marked `// expect: <id>`) and a pass fixture.

```bash
python3 scripts/sushistyle.py test-rules            # prove every rule against its fixtures
python3 scripts/sushistyle.py check --all FILE...   # list findings
python3 scripts/sushistyle.py check --gate FILE...  # exit 1 on error-level findings
```

Skills: `author-rule` (turn a rule you describe into a tested matcher) and `style-check`.

## Bundled house pack

| id | severity | rule |
|---|---|---|
| HS-auto | error | spell types; no `auto` variables (incl. `auto&`, `auto*`) |
| HS-trailing-return | error | no trailing return types |
| HS-lambda | error | no lambdas; name executable behavior |
| HS-switch-default | error | every switch has a default |
| HS-arrow | error | `(*p).m`, not `p->m` (`this->` is fine) |
| HS-structured-binding | error | no structured bindings |
| HS-std-any | error | no `std::any` |
| HS-defaulted-comparison | error | no defaulted comparison operators |
| HS-ranges | error | no ranges/views (regex tier) |
| HS-vague-name | reminder | names match mechanism (`process`, `handle`, `helper`, ...) |

License: MIT.
