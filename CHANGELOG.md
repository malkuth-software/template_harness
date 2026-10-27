# Changelog

The harness's release notes. `docs/contract.md` lists what products run or
import; a change to it is recorded under "Breaking changes", naming the old
and new form. Provisional rows there may change without an entry.

## Unreleased

### Added
- `docs/adopting.md`: the adoption guide a product's own session follows to
  become a harness instance (project file, `harness.lock`, CI, migration off
  GUI.Forms' shims, local work, the gate, a checklist and the pitfalls).
- `harness/test_project_prefixes.py`: no two `projects/*.toml` files have
  overlapping names (project name, `env_prefix`, `build_name`, artifact and
  cache-asset prefixes); a Windows toolchain may be shared only exactly.
- `harness/lock.py` (owner decision 2): `show`, `checkout DIR` and `bump [--to REF]`
  for a product's `harness.lock`, which pins the exact harness commit.
- `.github/actions/harness-checkout`: a composite action that reads
  `harness.lock` and checks the pinned commit out beside the product.
- The bootstrap checks a locked product's harness out at
  `<workspace>/template_harness` and runs the restore and the smoke build
  from it, with its `projects/<name>.toml`.
- `bootstrap.sh` and `bootstrap.ps1` (provisional): set up a workspace for one
  product. They clone or update the product and the notes, with the project's
  notes branch as a worktree, and the private projects repository with
  `--in-house`. They then restore the toolchain and cache and run the
  project's smoke build. `--dry-run` prints every step.
- `harness/restore_local.py`: restores the published Windows toolchain and
  the newest usable compiler cache into a local workspace.
- `project.Project.checkpoint` and `project.Project.bootstrap`: the optional
  `[checkpoint]` and `[bootstrap]` tables, parsed and validated in
  `harness/project.py`.
- Contract rows: `ci_build_steps.SANITIZER_LINK_FLAGS`, the workspace layout
  under `CCACHE_BASEDIR`, the checkpoint tool and the bootstrap (both
  provisional). `docs/contract.md` notes that GUI.Forms keeps
  `tools/sanitizer/lsan.supp`.
- `example_app/`: a GUI.Forms window app built against the pinned GUI.Forms
  source SDK, which `example_app/fetch_sdk.py` fetches, verifies and builds
  for the host into a cache outside the source tree.
- Reusable workflows (`on: workflow_call`) in `.github/workflows/`:
  `harness-native.yml` (one host's toolchain, packages or macOS runtimes,
  compiler cache, cached source-built installs and the product's build
  command), `harness-house-style.yml` and `harness-state-machines.yml`;
  `example.yml` runs them over `example_app/`. A product pins the harness
  in its `harness.lock` (`[harness] repository, commit`).

### Changed
- `harness/windows_toolchain.py restore` reads the project's `[toolchain]
  repository`, falling back to `GITHUB_REPOSITORY` when it is empty.
- `checkpoint/checkpoint.py` reads its settings from `project.Project`
  instead of parsing `[checkpoint]` itself. The command line is unchanged.
- `projects/gui_forms.toml`: `harness.lock` and `tools/harness_link.py` join
  `[state_machines].full_tree_triggers`, so a harness bump checks the whole
  tree. The `[bootstrap]` smoke command is unchanged; GUI.Forms' `ci_build.py`
  now accepts `--tests` without `--sanitize` (a plain build of what the
  matching tests read, and those tests).

### Breaking changes
- None.

## 0.1.0 (2026-10-26, main at c5b8ff5)

The first version: GUI.Forms' generic build tools, moved and parameterised by
a project file.

### Added
- `harness/project.py` and `projects/gui_forms.toml`, which reproduces
  GUI.Forms' names byte for byte (`harness/test_project_names.py`).
- The toolchain tools:
  - `windows_toolchain.py` (and its two JSON files);
  - `compiler_identity.py`;
  - `cmake/llvm22.cmake`;
  - `install_linux_ci_packages.sh`;
  - `build_macos_runtimes.py`, `macos_runtime_manifest.py` and
    `audit_macos_minimum.py`.
- The compiler caches: `ci_cache.py`, `publish_cache.py`,
  `prune_cache_releases.py`, `cache_manifest.py` and `native_tests.py`.
- Exact-tree reuse and promotion: `ci_reuse.py`, `ci_promote.py`,
  `ci_tree_receipt.py` and `ci_sanitizer_seed.py`.
- `ci_build_steps.py`, the product-independent parts of GUI.Forms'
  `tools/ci_build.py`.
- Change selection and the checkers: `change_selection.py`,
  `check_house_style.py`, `check_lifetime_rules.py` and
  `check_state_machines.py`.
- `checkpoint/checkpoint.py`, the local checkpoint tool (provisional): `init`,
  `fold`, `verify`, `push` and `after-merge`.
- `docs/contract.md` and `harness/test_contract.py`, from GUI.Forms' parked
  consumer contract.

### Breaking changes
- None (the first release). For GUI.Forms consumers, the tools moved from
  `gui_forms/tools/` to `template_harness/harness/`, and `ci_build`'s
  imported helpers moved to `ci_build_steps`. GUI.Forms keeps its copies
  until it pins the harness.
