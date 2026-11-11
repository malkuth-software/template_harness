---
name: author-rule
description: Turn a coding rule the user describes into a tested SushiStyle rule (clang AST matcher or regex, plus fail and pass fixtures) and register it in a rule pack. Use when the user wants to add, change, or tighten a house coding rule.
---

# Authoring a SushiStyle rule

A rule is accepted only when its fixtures prove it: the fail fixture matches exactly the
lines marked `// expect: <id>`, and the pass fixture matches nothing.

## 1. Pin the rule down in dialogue
Ask until these are unambiguous, then restate them in one short paragraph for the user to confirm:
- **What is forbidden or required**, as a construct in the code (not as an intent).
- **The house alternative**: what the code should look like instead.
- **Edges**: what must not be flagged (e.g. `this->x` for the arrow rule, implicit
  compiler-generated declarations, external API names).
- **Severity**: `error` (gate fails) or `reminder` (light note only).

## 2. Write fixtures first
In the pack's `fixtures/` folder (project packs live in `<project>/.sushi/rules/`, the
bundled pack in the plugin's `rules/`):
- `<id>.fail.cpp`: every violating form you can think of, one per line, each line ending
  `// expect: <id>`. Include the sneaky forms (references, pointers, qualifiers, macros if relevant).
- `<id>.pass.cpp`: the same programs written the house way, plus the edges from step 1.
Fixtures must compile standalone with standard headers only.

## 3. Choose the representation
- Prefer a **clang AST matcher** (`"tier": "clang"`): it sees types and semantics, not text.
  Explore matchers with clang-query on the build host against the fixture.
- Use `"tier": "regex"` only when the rule is purely lexical and a regex cannot misfire on
  comments or strings in practice.
- Keep the matcher as narrow as the rule. Exclude implicit declarations with
  `unless(isImplicit())` where compiler-generated code would match.

## 4. Register and prove
Add the rule to the pack JSON (`id`, `severity`, `section`, `tier`, `matcher` or `regex`,
`message`, `fixtures`). The message is one short imperative clause that says what to write
instead. Then run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushistyle.py" test-rules
```

Iterate on the matcher (not the fixtures) until every rule reports `ok`. If a fixture was
wrong, say so to the user before changing it.

## 5. Report
Tell the user the rule id, the final matcher, and the fixture results.
