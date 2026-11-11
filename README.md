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
| `docs/new_app.md` | The `new_app` guide: the repositories it writes, its steps, the generated app and how its pins move together. | here |
| `docs/adopting.md` | How a product becomes a harness instance: its project file, `harness.lock`, CI, local work, the gate and a checklist, with GUI.Forms as the worked example. | here |
| `CHANGELOG.md` | The release notes; contract changes are recorded under "Breaking changes". | here |
| `bootstrap.(sh\|ps1)` | Clone or update the product and its notes (and, `--in-house`, the projects repository), restore the toolchain and caches (`harness/restore_local.py`), run the smoke build: `sh bootstrap.sh --workspace DIR --project NAME [--dry-run]` (`harness/bootstrap.py` has the options). Settings: `[bootstrap]`. | here |
| `harness/lockstep.py` | The local lockstep GUI.Forms: one clone per machine, built once per commit into an installed prefix that every product's local build uses (`init`, `advance`, `build`, `prefix`, `status`); see "Local lockstep builds" in `docs/adopting.md`. | here |
| `checkpoint/` | The local checkpoint tool (`checkpoint/checkpoint.py`): `init` marks a dedicated integration tree, `fold` squashes each worker branch into one commit on `origin/main`, `regenerate` records a branch of regenerated files built on the folded head, `verify` runs the project's verification and records it against the tree, `push` opens one auto-merging PR from a verified tree, `after-merge` lists the folded branches main now contains. Settings: the project file's `[checkpoint]` table. | here |
| `workflows/` | Reusable GitHub workflows (`on: workflow_call`) a product calls: `harness-native.yml` (one host's build: the Windows CLANG64 toolchain, the project's Linux packages or the macOS runtimes, the compiler cache, then the product's build command), `harness-house-style.yml` and `harness-state-machines.yml`. GitHub runs reusable workflows only from `.github/workflows/`, so they live there; `example.yml` is the harness's own use of them. Docs are not yet covered. | here |
| `new_app.(sh\|ps1)`, `harness/new_app.py`, `templates/` | One command to a harnessed GUI.Forms app: `sh new_app.sh --name APP --workspace DIR [--create-repo] [--dry-run]` adds `projects/APP.toml` here, creates the app repository from `templates/app/` pinned to that commit, the notes branch and the projects tracks and seats, bootstraps the workspace and runs a first verify. Every step is idempotent; `docs/new_app.md` is the guide. `templates/` is also MIT-0 (`templates/LICENSE`), so generated apps owe the harness nothing. | here |
| `claude/` | The `fransushi` plugin marketplace apps enable from their `.claude/settings.json`: the seven FranSushi plugins, copied from the private development repository by `harness/sync_fransushi.py` (`claude/FRANSUSHI.lock` records the source commit and versions), and `claude/rules/house.json`, the single house rule pack, each rule citing its HOUSE_STYLE section. `.github/workflows/plugins.yml` runs `claude plugin validate` on it. | here |
| `agents/` | Agent configuration: hats, worker counts, brief, verification layers, merge and line-ending rules. Project agents for a new app are rendered from `templates/app/.claude/agents/`. | later |
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

## Tests

    python -B -m unittest discover -s harness -p "test_*.py"
    python -B -m unittest discover -s checkpoint -p "test_*.py"
    python -B -m unittest discover -s example_app -p "test_*.py"

The tests use `projects/gui_forms.toml` as their reference project and a
temporary workspace whose `gui_forms/` checkout is a one-commit git
repository (`harness/harness_testing.py`).

## License

MIT; see `LICENSE`.
