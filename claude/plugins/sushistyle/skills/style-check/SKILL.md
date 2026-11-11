---
name: style-check
description: Run the SushiStyle house rules over one or more C/C++ files and list every finding, including ones already reminded about. Use when asked for a style check, before handing work to the integrator, or when a SushiStyle note says there are more findings.
---

Run, from the project root:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sushistyle.py" check --all <files...>
```

Add `--gate` to exit non-zero on error-level findings (for CI or a pre-handoff check).

- `error` findings: fix them unless the construct is required by something outside the house
  (an external API name, a required signature). Then add `// sushi-allow: <rule-id> <reason>`
  on that line or the line above.
- `reminder` findings: consider them; keep the code if there is a reason.
- If the output says the clang tier was skipped (build host busy or unreachable), the regex
  tier still ran. Do not retry in a loop; mention it and continue.
