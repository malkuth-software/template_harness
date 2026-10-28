# Changelog

The harness's release notes. `docs/contract.md` lists what products run or
import; a change to it is recorded under "Breaking changes", naming the old
and new form. Provisional rows there may change without an entry.

## Unreleased

### Added
- `harness/clang_ast.py`: the shared AST reader. One tree model (clang AST
  kinds, named roles, unit-local identities, semantic qualified names,
  written arguments) read through the pinned LLVM's libclang, with the
  `-ast-dump=json` reader as the fallback behind the same interface
  (`HARNESS_AST_READER`). On two GUI.Forms units libclang read in a quarter
  of the time (3.4 s against 14.6 s, 4.3 s against 16.9 s). The
  state-machine gate's job installs `python3-clang-22` from apt.llvm.org for
  it, and the toolkit's own run of that job tests both backends.
- `harness/check_loop_branches.py` (Phase E, E2): every conditional inside a
  loop body (`if`, `switch`, `?:`, a standalone `&&` or `||`), classed by
  what its condition reads against the innermost loop: invariant, edge,
  validation, control or data, each with the house style's repair. It reads
  the AST through `clang_ast.py` over a ninja tree's compile database, writes a
  JSON review queue (`--json`) and a short report (`--report`), and matches
  decisions recorded in `[loop_branches] decisions` (optional) by path,
  function and condition text. It never fails a run.
- `harness/check_const_restrict.py` (Phase E, E3): two review queues from the
  same AST. `const`: a pointer or lvalue reference to a non-const object (a
  parameter, local or range-for variable) through which the function only
  reads. `restrict`: a function with a loop that writes through a pointer
  parameter beside other pointer parameters it only reads, none of them
  escaping, ranked by whether the element types may alias; span kernels are
  listed apart (`restrict_span`). Virtual functions, overrides, functions
  whose address is taken and `main` are listed as `signature_bound`.
  Decisions are recorded in `[const_restrict] decisions` (optional) by path,
  function, variable and kind. It never fails a run.
- `clang_ast.Node.parameters`: per call argument, its parameter's type as
  written ("" where unknown), from both readers.
- `.github/workflows/bootstrap.yml`: the bootstrap proof. On Windows, macOS and
  Linux it bootstraps GUI.Forms from an empty directory through the entry a
  person uses, runs the administrator step the bootstrap prints, passes the
  smoke build, and checks that two more bootstraps clone and download nothing
  (`harness/bootstrap_proof.py`, the harness's own CI tool). The job summary
  times each step.
- The bootstrap proof covers every project whose file has a `[bootstrap]`
  table, not only GUI.Forms. `bootstrap_proof.py matrix` skips a project, with
  the reason in the summary, until its notes branch exists and its smoke
  script is on its default branch. A product without a `harness.lock`
  bootstraps with this harness. The downloaded Ubuntu packages are kept in an
  Actions cache, because the Ubuntu mirror once took 34 minutes. The plan
  skips one host, with the reason, when it cannot work there yet, for
  example Linux, when the bootstrap would install the wrong package profile.
- `[bootstrap] linux_packages` (optional): the `[linux_packages]` profile a
  workspace installs on Linux. Without it the bootstrap uses `toolkit`, else
  the project's only profile (`Project.workspace_linux_profile()`). A project
  with several profiles and none named `toolkit` (Pocket Library, Games) must
  name one.
- `harness/lockstep.py`: the local lockstep GUI.Forms. One clone under
  `D:/malkuth` (or `HARNESS_LOCKSTEP_ROOT`) is built once per (commit,
  target, toolchain, features) with `tools/build_sdk.py --features all` into
  `cache/lockstep/gui-forms-<commit>-<target>/`, with one shared ccache in
  CI's layout, and `current-<target>.json` records the active install for
  product builds. `example_app/ci_build.py --gui-forms-prefix DIR` builds
  against such an install. See "Local lockstep builds" in
  `docs/adopting.md`.
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
- `projects/gui_forms.toml` names GUI.Forms' loop-branch decisions
  (`[loop_branches] decisions = "tools/loop_branch_decisions.json"`), so
  `check_loop_branches.py` reads them without `--decisions`.
- The bootstrap checks the product's submodules out at the commits it
  records (`git submodule update --init --recursive`), so File Manager's
  `gui_forms/` and `backend/` are there for its smoke build. It does this
  after a clone or a fast-forward only; a checkout left as it is keeps its
  submodules as they are.
- The bootstrap's warning for a root-layout product also checks that the
  checkout ignores `.toolchain/` and `.bootstrap/`. restore_local creates
  them for the Windows toolchain and its downloads
  (`restore_local.TOOLCHAIN_DIRECTORY`, `DOWNLOAD_DIRECTORY`).
- On Linux, `restore_local.py toolchain` (and so the bootstrap) prints the
  Ubuntu package command with `HARNESS_PROJECT=<project file>` in front,
  because `install_linux_ci_packages.sh` reads the package set from it.
  The printed line runs as it stands once the apt directory is filled in.
  It names the project's own package profile: the one `[bootstrap]
  linux_packages` names, else `toolkit` when the project has one, else its only profile (Plan Paint's `paint`). A project with
  several and no `toolkit` gets a placeholder listing them, and one with
  none gets a note.
- `harness/windows_toolchain.py restore` reads the project's `[toolchain]
  repository`, falling back to `GITHUB_REPOSITORY` when it is empty.
- A product may be the workspace root (`source_dir = "."`, File Manager's
  layout): `project.ROOT_SOURCE_DIR`, `Project.at_workspace_root()`, and
  `source_root()` is the workspace itself. The bootstrap clones such a
  product into `DIR` itself and warns about each of `template_harness/`,
  `notes/`, `notes-<branch>/`, `projects/`, `.build/`, `.ccache/` and `.ci/`
  the checkout does not ignore; `checkpoint init` with `tree = "."` clones
  into the workspace in place beside the harness (removing its `.git` again
  if an entry would be overwritten); `lock.py checkout` and the
  harness-checkout action (`product-path: .`) put the harness inside the
  checkout. Other `source_dir` values may no longer hold `.`, `..` or empty
  parts. Tests: `harness/test_root_layout.py`, `harness/test_bootstrap_root.py`,
  the root-layout checkpoint and lock tests.
- Optional project-file fields: `ci.cache_asset_prefix`,
  `ci.pinned_cache_revisions`, `tests.areas_file` and the `[state_machines]`
  table (`Project.has_state_machines`). A tool that needs a missing one
  raises `project.MissingField`, "project <name> has no <field>"; every tool
  still imports (`windows_toolchain.CACHE_NAME` is None without caches), and
  the bootstrap skips the cache restore. `test_project_prefixes` skips an
  absent prefix. GUI.Forms' names are unchanged (`test_project_names`).
- `ci.native_job` may be a template with `{platform}` that a job's whole name
  must equal (`"{platform}"` for a matrix job named by its platform);
  without `{platform}` it is the job-name prefix as before.
  `Project.native_job_matches(job_name, platform)` is what `ci_reuse.py`
  uses.
- `docs/adopting.md`: the root layout in sections 1, 3, 5 and 6; section 5's
  harness placement for a submodule consumer (always inside its checkout),
  `package_programs.py` needing no harness, `HARNESS_PROJECT` set per call;
  reusing GUI.Forms' Windows toolchain through `[toolchain] repository`
  (and, for a harness before PR #1, `GITHUB_REPOSITORY=malkuth-software/gui_forms`);
  the optional fields and
  both `native_job` forms. `docs/contract.md` rows follow.
- `checkpoint/checkpoint.py` reads its settings from `project.Project`
  instead of parsing `[checkpoint]` itself. The command line is unchanged.
- `projects/gui_forms.toml`: `harness.lock` and `tools/harness_link.py` join
  `[state_machines].full_tree_triggers`, so a harness bump checks the whole
  tree. The `[bootstrap]` smoke command is unchanged; GUI.Forms' `ci_build.py`
  now accepts `--tests` without `--sanitize` (a plain build of what the
  matching tests read, and those tests).
- `harness/lockstep.py` no longer clones with `core.autocrlf=true` on
  Windows. GUI.Forms' `.gitattributes` (`* text=auto eol=lf`) checks every
  text file out with LF on every host, so the clone has CI's bytes without it;
  an existing clone's setting is overridden by the attribute and is harmless.
  `docs/adopting.md` follows.
- The prebuilt Windows Skia archive is gone from GUI.Forms (Skia is built
  from source on every host): `docs/adopting.md` and `harness/ci_build_steps.py`
  stop citing it, and `projects/gui_forms.toml` drops its Windows `skia = false`
  cache-manifest override.

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
