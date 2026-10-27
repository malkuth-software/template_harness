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
| `bootstrap.(sh\|ps1)` | Clone the product and its notes, restore the toolchain and caches, smoke build. | later |
| `checkpoint/` | The local checkpoint tool (`checkpoint/checkpoint.py`): `init` marks a dedicated integration tree, `fold` squashes each worker branch into one commit on `origin/main`, `verify` runs the project's verification and records it against the tree, `push` opens one auto-merging PR from a verified tree, `after-merge` lists the folded branches main now contains. Settings: the project file's `[checkpoint]` table. | here |
| `workflows/` | Reusable GitHub workflows: native build per host, docs, house style, state machines. | later |
| `agents/` | Agent configuration: hats, worker counts, brief, verification layers, merge and line-ending rules. | later |
| `example_app/` | A small GUI.Forms window app that consumes the installed package. | later |

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

The tests use `projects/gui_forms.toml` as their reference project and a
temporary workspace whose `gui_forms/` checkout is a one-commit git
repository (`harness/harness_testing.py`).

## License

MIT; see `LICENSE`.
