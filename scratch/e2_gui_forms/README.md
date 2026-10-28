# E2 repair queue on GUI.Forms: working files

Not for merging. This branch keeps the state machines worker's scratch files from the E2 repair queue, so the work can
resume on another machine. The authoritative state is the track file (`tracks/state_machines.md` in the coordinator's
workers folder) and the gui_forms PRs #433 (core), #434 (controls), #435 (hosts) and #437 (stx_*).

- `e2_queue.json`, `e2_report.md`, `e2_run.log`: the first whole-tree run of `harness/check_loop_branches.py` on
  gui_forms, before any repair.
- `area.py`: counts invariant branches by area, excluding any path with a `tests`, `tools` or `fuzz` segment (318 at
  the start: controls 151, core 102, stx_png 24, stx_vorbis 15, stx_zlib 9, host-windows 8, stx_conv 3, demo 2,
  render 2, audio 1, host-linux 1).
- `ctx.py`, `ctx_sites.py`: dump each area's invariant sites with source context (`*_ctx.txt`, numbered `#1..`).
- `<area>_edits.py`: the hoisting edits, as exact text replacements that keep each file's line endings.
- `core_decisions.py`, `controls_decisions.py` (site number to decision) with `build_decisions.py <area> <module>
  <out>`; `hosts_decide.py`, `stx_decide.py` (keyed by path and condition): write `tools/loop_branch_decisions.json`.
- `*_now.json` / `*_after*.json` / `.md`: per-area E2 reruns before and after the repairs.
- `vorbis_tests.sh`: builds stx_vorbis's own tests standalone, plain and with `STX_VORBIS_SANITIZERS=ON`.

Per-area rerun (Git Bash, from this directory):

```
export PATH=<llvm22>/bin:$PATH HARNESS_PROJECT=<template_harness>/projects/gui_forms.toml HARNESS_AST_READER=libclang
python -B <template_harness>/harness/check_loop_branches.py --root <gui_forms> --build-dir <gui_forms build tree> \
  --jobs 4 --paths <dirs> --decisions <gui_forms>/tools/loop_branch_decisions.json --json X.json --report X.md
```
