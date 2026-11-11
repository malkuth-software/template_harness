# template_harness

The shared build harness for Malkuth products: the pinned toolchain, the
compiler caches, exact-tree reuse of CI results, and the house-style
checkers, written once and parameterised by a project file. A product (or an
outsider) clones this repository first.

## Layout

| Path | What it holds | State |
|---|---|---|
| `harness/` | The generic tools, moved from GUI.Forms and parameterised: toolchain restore, caches, reuse and promotion, the CI build helpers, change selection, the checkers. Each tool keeps its tests (`harness/test_*.py`). | here |
| `harness/project.py` | Reads the project file; every product-specific name comes from it. | here |
| `projects/` | Project files. `projects/gui_forms.toml` reproduces GUI.Forms' names exactly, so its cache and reuse history stays valid. | here |
| `docs/contract.md` | What consumers run or import, and how it may change. | here |
| `docs/workflow.md` | The default workflow: workers commit locally and never push; the coordinator folds their branches, verifies everything CI would reject, and pushes one checkpoint. Also the build-host wrapper and agent setup it needs, and the pitfalls met in practice. | here |
| `docs/adopting.md` | How a product becomes a harness instance: its project file, `harness.lock`, CI, local work, the gate and a checklist, with GUI.Forms as the worked example. | here |
| `CHANGELOG.md` | The release notes; contract changes are recorded under "Breaking changes". | here |
| `bootstrap.(sh\|ps1)` | Clone or update the product and its notes (and, `--in-house`, the projects repository), restore the toolchain and caches (`harness/restore_local.py`), run the smoke build: `sh bootstrap.sh --workspace DIR --project NAME [--dry-run]` (`harness/bootstrap.py` has the options). Settings: `[bootstrap]`. | here |
| `harness/lockstep.py` | The local lockstep GUI.Forms: one clone per machine, built once per commit into an installed prefix that every product's local build uses (`init`, `advance`, `build`, `prefix`, `status`); see "Local lockstep builds" in `docs/adopting.md`. | here |
| `checkpoint/` | The local checkpoint tool (`checkpoint/checkpoint.py`): `init` marks a dedicated integration tree, `fold` squashes each worker branch into one commit on `origin/main`, `regenerate` records a branch of regenerated files built on the folded head, `verify` runs the project's verification and records it against the tree, `push` opens one auto-merging PR from a verified tree, `after-merge` lists the folded branches main now contains. Settings: the project file's `[checkpoint]` table. | here |
| `workflows/` | Reusable GitHub workflows (`on: workflow_call`) a product calls: `harness-native.yml` (one host's build: the Windows CLANG64 toolchain, the project's Linux packages or the macOS runtimes, the compiler cache, then the product's build command), `harness-house-style.yml` and `harness-state-machines.yml`. GitHub runs reusable workflows only from `.github/workflows/`, so they live there; `example.yml` is the harness's own use of them. Docs are not yet covered. | here |
| `agents/` | Agent configuration: hats, worker counts, brief, verification layers, merge and line-ending rules. | later |
| `example_app/` | A small GUI.Forms window app (a label, a text box, a button and a list) built with `find_package(GUIForms)` against the GUI.Forms source SDK. `gui_forms.lock` pins the release and its source archive's digest; `fetch_sdk.py` downloads and verifies the archive into a cache outside the source tree and builds and installs GUI.Forms from it for a target (today the host) with the harness toolchain, reusing the install while the archive, target and toolchain match; `ci_build.py` builds and tests the app; `projects/example_app.toml` is its project file. | here |

## The project file

A product describes itself in TOML (see `projects/gui_forms.toml` and the
`Project` record in `harness/project.py`): its checkout directory, build
directory name, environment-variable prefix, artifact and cache-asset name
prefixes, the Windows toolchain's asset prefix and publisher, Ubuntu package
profiles, its own configure variables, the native options its cache
manifest records and the state-machine checker's areas. The tools load the
file named by `HARNESS_PROJECT`, or `harness.toml` at the workspace root.
The workspace root is `HARNESS_WORKSPACE`, or the current directory, and
holds the product's checkout.

Python 3.11 or later is required (`tomllib`); CI uses 3.12 and 3.14.

## Efficient builds

Measured on GUI.Forms in November 2026; the workspace layout and the
remote build host are in `docs/contract.md` and `docs/workflow.md` §6.

- **ccache:** the compiler launchers set in the build environment
  (`CMAKE_C/CXX/OBJCXX_COMPILER_LAUNCHER=ccache`), `CCACHE_BASEDIR` at
  the common root of your workspaces, `CCACHE_COMPILERCHECK=content`, and
  `CCACHE_SLOPPINESS=pch_defines,time_macros` wherever a precompiled
  header is used (without it ccache refused 596 compiles). `CCACHE_BASEDIR`
  does not rewrite absolute paths inside other flags, so pass plugin roots
  and similar arguments relative to the build directory. Link
  reproducibly: link-time stamps change every output hash.
- **One shared cache, one cap.** Point every workspace at one `CCACHE_DIR`
  and set `max_size` once in its `ccache.conf`; a per-shell
  `CCACHE_MAXSIZE` trims the cache for everyone. With these settings a
  touch-everything GUI.Forms rebuild went from 2,751 s to 982 s.
- **Shared SDK, one prefix per options set.** Build GUI.Forms once per
  commit into an installed prefix (`harness/lockstep.py`,
  `example_app/fetch_sdk.py`) and build products against it. Never
  reconfigure a shared prefix with different options; give each options
  set its own prefix. Keep one reused build tree per base: fresh trees
  were 75% of one worker's cmake time.
- **CPU budget:** one priority build at full `-j`, every other at `-j2`.
  Parallel full-width builds put 28 compilers on 8 cores and slowed all of
  them.
- **Look iteration:** load a theme sheet at run time instead of rebuilding
  (GUI.Forms' `--theme-sheet`; a universal `--theme` option is coming).
  Edit the sheet and rerun.
- **Check real exit codes:** `make ...; echo rc=$?` exits 0 even when the
  build failed.

## Tests

    python -B -m unittest discover -s harness -p "test_*.py"
    python -B -m unittest discover -s checkpoint -p "test_*.py"
    python -B -m unittest discover -s example_app -p "test_*.py"

The tests use `projects/gui_forms.toml` as their reference project and a
temporary workspace whose `gui_forms/` checkout is a one-commit git
repository (`harness/harness_testing.py`).

## License

MIT; see `LICENSE`.
