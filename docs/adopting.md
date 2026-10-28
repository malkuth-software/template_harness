# Adopting the harness

This guide is for a product's own session. It turns the product into a
harness instance: a project file here, a `harness.lock` in the product, CI
that runs the pinned harness, local work through the checkpoint tool, and a
bootstrap that sets the product up on a clean machine. GUI.Forms was the
first product to adopt the harness (its PR #408) and is the worked example
throughout. `docs/contract.md` lists what may be relied on.

## 1. Prerequisites, and what the harness provides

You need:

- a GitHub repository for the product. The tools read artifacts,
  releases and runs of the running repository through `gh` with
  `GH_TOKEN`;
- Python 3.11 or later locally (the tools use `tomllib`). CI uses 3.12,
  and 3.14 for the Windows toolchain;
- a CMake and Ninja build that runs with the harness toolchain (LLVM
  22.1.8 with ccache on every host) and builds in the workspace layout below;
- a published Windows CLANG64 toolchain release, if you build on Windows
  (see "The toolchain" in section 2).

The harness provides:

- **The toolchain:** `windows_toolchain.py` restores the Windows toolchain,
  `build_macos_runtimes.py` builds the macOS 14 runtimes, and
  `install_linux_ci_packages.sh` installs the Ubuntu packages and
  apt.llvm.org's clang-22. `compiler_identity.py` verifies the compiler,
  and `cmake/llvm22.cmake` is the CMake toolchain file.
- **Caches and reuse:** `ci_cache.py`, `ci_reuse.py`, `ci_promote.py`,
  `ci_sanitizer_seed.py`, `ci_tree_receipt.py`, `publish_cache.py`,
  `prune_cache_releases.py` and `cache_manifest.py`.
- **Build helpers:** `ci_build_steps.py` (`configure_tree`,
  `sanitizer_options`), `change_selection.py` and `native_tests.py`.
- **Checkers:** `check_house_style.py`, `check_lifetime_rules.py`,
  `check_state_machines.py`, `check_loop_branches.py`, `check_const_restrict.py` and
  `check_copies.py`.
- **The AST reader:** `clang_ast.py` reads a translation unit from its
  compile command into one tree model, for the checkers and codemods that
  need more than tokens. It reads through the pinned LLVM's libclang
  (`clang_ast_libclang.py`: CLANG64's bindings on Windows, Homebrew
  `llvm@22`'s on macOS, apt.llvm.org's `python3-clang-22` on Linux) and falls
  back to `clang++ -Xclang -ast-dump=json` (`clang_ast_json.py`) when the
  bindings are missing. `HARNESS_AST_READER=libclang` or `json` chooses one;
  `test_clang_ast.py` checks that both give the same model and lists what
  only libclang knows.
- **The pin:** `lock.py` and `.github/actions/harness-checkout`.
- **Local work:** `bootstrap.sh`/`bootstrap.ps1` and `restore_local.py`,
  and `checkpoint/checkpoint.py`.

The harness does not provide your build driver, packaging or release
workflow. GUI.Forms keeps `tools/ci_build.py`, `tools/package_programs.py`,
its SDK tools and `release.yml`.

The workspace layout is part of the contract. A compiler cache hits only
for a product built in it:

```text
<workspace>/<source_dir>        the product checkout
<workspace>/template_harness    the harness, at the commit harness.lock pins
<workspace>/.build/native-<platform>/<build_name>[-sanitize]
<workspace>/.ccache, .ci        CCACHE_DIR (CCACHE_BASEDIR is the workspace) and CI state
```

A product can instead be the workspace root itself (`source_dir = "."`).
File Manager is built this way. The harness, the build trees, the CI state
and the bootstrap's notes then all sit inside the product's checkout, so
its `.gitignore` must list them:

```text
<workspace>/                    the product checkout (source_dir ".")
<workspace>/template_harness    inside the checkout: ignored
<workspace>/.build, .ccache, .ci   inside the checkout: ignored
<workspace>/notes, notes-<branch>  the bootstrap's notes, inside the checkout: ignored
```

Choose the layout once, like the names in section 2: moving a product
between layouts changes every compiler-cache path, so nothing hits.

## 2. Writing `projects/<product>.toml`

The project file lives in this repository. Add it in a harness PR, which
the coordinating session reviews. Your product can pin the harness only
after that PR merges, because the pinned commit must contain the file. The
tools find the file through `HARNESS_PROJECT`, or `harness.toml` at the
workspace root (`HARNESS_WORKSPACE`, else the current directory).
`projects/gui_forms.toml` is a complete example.

Every field is a name on disk or on GitHub. Changing one later starts a
new history of caches and receipts, so choose the names once.

| Table and field | What it controls | Required |
|---|---|---|
| `schema` | `1` | yes |
| `[project] name` | The bootstrap's `--project NAME`, the default notes branch, and the reusable workflows' cache-key prefix | yes |
| `[project] repository` | `owner/name`; the bootstrap clones it and the manifests record it | yes |
| `[project] source_dir` | The checkout under the workspace root: a relative directory without `.`, `..` or a trailing slash, or `"."` for a product that is the workspace root (section 1) | yes |
| `[project] build_name` | The build tree `.build/native-<platform>/<build_name>` | yes |
| `[project] env_prefix` | Every environment variable the tools read or set: `<env_prefix>_LLVM_RUNTIME`, `_PLATFORM`, `_COMPILER_ID`, `_RUNNER`, `_SANITIZER_DEBUG_INFO`, `_PACKAGE_WINDOWS_TOOLCHAIN` | yes |
| `[ci] artifact_prefix` | Every workflow artifact: `<prefix>validated-…`, `inputs-…`, `tree-…`, `sanitize-ccache-…` | yes |
| `[ci] cache_asset_prefix` | The compiler-cache release asset `<prefix><platform>.tar.gz` | no: leave it out if you publish no compiler caches (the bootstrap then restores none) |
| `[ci] native_workflow`, `native_job` | The workflow file and job whose runs may produce receipts and caches; reuse trusts no other. `native_job` has two forms, described below | yes |
| `[ci] pinned_cache_revisions` | A JSON file under the source root listing the cache releases the pruner keeps | no; only `prune_cache_releases.py` reads it |
| `[toolchain] repository` | The Windows toolchain's publisher, and where `restore_local.py` reads it from | yes |
| `[toolchain] asset_prefix` | `<prefix><platform>.tar.zst` and its manifest | yes |
| `[tests] areas_file` | The test-area rules `change_selection.py` reads | no; only change selection reads it |
| `[tests] name_prefixes` | `tests/X.cpp` names the tests `X` and `<prefix>X` | yes, may be `[]` |
| `[linux_packages.<profile>] packages`, `extends` | The package sets `install_linux_ci_packages.sh <profile>` installs; `extends` names one other profile, one level deep | the table is required; define the profiles your jobs use |
| `[state_machines] first_party_dirs`, `vocabulary_headers`, `areas`, `full_tree_triggers` | The state-machine checker's scope, its atom vocabulary, `[prefix, area]` ownership pairs (most specific first), and the paths whose change widens a changed-code run to the full tree | no: without the table, `check_state_machines.py` refuses to run; with it, all four are required and the lists may be empty |
| `[loop_branches] decisions` | The loop-branch review's recorded decisions, a path in the product checkout | no; without it, every branch in a loop is listed as open |
| `[copies] decisions` | The copies review's recorded decisions, a path in the product checkout | no; without it, every candidate is listed as open or signature-bound |
| `[const_restrict] decisions` | The const and restrict review's recorded decisions, a path in the product checkout | no; without it, every candidate is listed as open or signature-bound |
| `[build] configure_variables` | Your own variables, without the prefix, that change what a configure records | yes, may be `[]` |
| `[cache_manifest] profile`, `native_options` | What `cache-manifest.json` records beside each cache | yes |
| `[cache_manifest.platform_options.<platform>]` | Per-platform overrides; each key must already be in `native_options` | no |
| `[checkpoint]` | Section 6; `tree` and `verify` are required, while `base` (`main`), `remote` (`origin`), `branch_prefix` (`checkpoint/`) and `merge_method` (`merge`) have defaults | no |
| `[bootstrap]` | Section 6; every field has a default | no |

An optional field is only needed by the tools that read it. A tool that
needs a field your file leaves out stops with `project <name> has no
<field>` (for example `project file_manager has no ci.cache_asset_prefix`).
Every other tool still imports and runs.

**`native_job`, two forms.** `ci_reuse.py lookup` trusts a receipt only
from a run of `native_workflow` with exactly one job matching
`native_job` for the platform (`project.Project.native_job_matches`):

- **A prefix** (GUI.Forms: `"native"`). It matches a job named
  `<native_job> (<matrix values>)`, optionally followed by ` / <job>`,
  whose matrix values include `, <platform>,`. For example
  `native (ubuntu-24.04, linux-x64, bash) / build`.
- **A template containing `{platform}`.** The job's whole name must equal
  the template with the platform substituted. A matrix job named by its
  platform (`name: ${{ matrix.label || matrix.platform }}`) is
  `"{platform}"`, which matches `linux-x64`. The same job inside a called
  workflow is `"<calling job> / {platform}"`. The platform is the one the
  product passes to `ci_reuse.py identity --platform`. A matrix entry
  whose label is not its platform therefore never matches; such an entry
  produces no receipts.

Put `harness.lock` and every file that forwards to the harness in
`full_tree_triggers`. A harness bump can change any verdict, so it must
check the whole tree; GUI.Forms lists `harness.lock` and
`tools/harness_link.py`.

**Prefixes that cannot collide.** Artifacts, Actions caches and release
assets share one namespace per repository. Two products share a repository
when one runs the other's tools there. File Manager running GUI.Forms'
tools is one case, and this repository building the example app is
another. Follow these rules:

- Use the product name: `artifact_prefix = "<name>-"`,
  `cache_asset_prefix = "<name>-cache-"` and
  `asset_prefix = "<name>-toolchain-"`.
- End every prefix with `-`.
- No prefix may begin with another project's prefix, and none may equal
  another project's prefix followed by a kind (`validated-`, `inputs-`,
  `tree-`, `sanitize-`). `gui-`, `gui-forms-cache-` and
  `gui-forms-toolchain-` are GUI.Forms' historical names; a new product
  must not start with `gui-`.
- Keep `env_prefix` and `build_name` distinct for the same reason. Two
  products in one workspace share `.build/` and `.ccache/`.

The compiler-cache release tag `build-<commit>` is shared by design. The
asset names inside it carry the prefix.

`harness/test_project_prefixes.py` holds these rules for every file in
`projects/`, and skips a prefix a file leaves out. The one shared name it
allows is a Windows toolchain: two projects may share one only when they
name exactly the same `[toolchain] repository` and `asset_prefix`.

**The toolchain.** A product that builds on Windows can reuse GUI.Forms'
published CLANG64 toolchain instead of publishing its own:

- Set `[toolchain] repository = "malkuth-software/gui_forms"` and
  `asset_prefix = "gui-forms-toolchain-"`.
- The current harness reads the toolchain from the project's
  `[toolchain] repository`, through CI's `windows_toolchain.py restore`
  and the bootstrap's `restore_local.py` alike. An empty `repository`
  falls back to the running repository (`GITHUB_REPOSITORY`).
- **A harness pinned before PR #1** (before main 7f773d9) reads only the
  running repository's releases in CI. With such a pin, name GUI.Forms'
  repository for that one call. The asset names still come from your
  `[toolchain] asset_prefix`:

  ```sh
  GITHUB_REPOSITORY=malkuth-software/gui_forms python template_harness/harness/windows_toolchain.py restore
  ```

  Bumping `harness.lock` past PR #1 removes the need for it.

Reading another repository's releases needs a token that can read them.
The job's own `GITHUB_TOKEN` reads a public repository's releases.

## 3. `harness.lock`

Add it at the product root:

```toml
[harness]
repository = "malkuth-software/template_harness"
commit = "<40 lowercase hexadecimal characters>"
```

The format is exactly one `repository` line and one `commit` line. Use
`lock.py` (`--lock` defaults to `./harness.lock`):

```sh
python3 template_harness/harness/lock.py --lock <product>/harness.lock show
python3 template_harness/harness/lock.py --lock <product>/harness.lock checkout template_harness
python3 template_harness/harness/lock.py --lock <product>/harness.lock bump --to <branch, tag or commit>
```

- `checkout` makes the directory that exact commit, detached. It clones
  when the directory is missing and refuses uncommitted changes.
- `bump` rewrites only the commit line.

Commit the lock like any other change. A bump changes the product's tree,
so CI validates it afresh.

**The harness-checkout step in CI.** There are two forms:

- **The reusable action**,
  `uses: malkuth-software/template_harness/.github/actions/harness-checkout@<commit>`
  with `product-path`. It reads the lock, checks the commit out at
  `template_harness/` and verifies HEAD. The `@<commit>` in the `uses:`
  line is a second pin, and it must equal `harness.lock`. Use this form
  when you already call the reusable workflows at the same commit (which
  check the harness out themselves), or for a few jobs. The action does
  not set `HARNESS_PROJECT` or `HARNESS_WORKSPACE`; set them in the job's
  `env`.
- **The inline form** follows `harness.lock` alone. It is a composite
  action in your own repository (GUI.Forms'
  `.github/actions/harness-checkout`) or the same three steps inline:
  - read the two lines with `sed`;
  - `actions/checkout` the repository at the commit into
    `template_harness`, with `persist-credentials: false`;
  - compare `git rev-parse HEAD`, then append `HARNESS_PROJECT` and
    `HARNESS_WORKSPACE` to `GITHUB_ENV`.

  Use this form when the lock should be the only pin: a bump is then
  one line.

Either way, check the product out under its `source_dir` (`path:
<source_dir>`), so that the harness lands beside it.

**A product at the workspace root** (`source_dir = "."`) gets the
harness inside its checkout, at `<product root>/template_harness`:

- **In CI**, check the product out at the workspace root (no `path:`)
  first. `actions/checkout` cleans the directory it checks out into, so it
  must come before anything else. Then run the harness-checkout step with
  `product-path: .`. GUI.Forms' stx jobs work this way.
- **Locally**, from the product root:
  `python3 template_harness/harness/lock.py checkout template_harness`
  (`--lock` defaults to `./harness.lock`). The harness is its own clone,
  which the product's `.gitignore` must list (`/template_harness/`).

## 4. CI

There are two ways to run the harness in CI.

**The reusable workflows**, in this repository since PR #1:

- `harness-native.yml`: one host's build per call, with inputs `runner`,
  `platform`, `shell`, `product_path`, `project` and `build_command`.
  Optional inputs are `linux_profile`, `linux_display`, `macos_runtimes`,
  `install_cache_paths` and `install_cache_key_files`. It restores the
  toolchain, the macOS runtimes, the Linux packages and the ccache
  directory. It then runs your build command from the workspace root and
  saves the caches on a push.
- `harness-house-style.yml`: inputs `product_path`, `paths`, and
  optionally `lifetime_paths`.
- `harness-state-machines.yml`: inputs `product_path`, `project`,
  `configure_command` (a Ninja tree with compile commands) and
  `build_dir`. It checks what a change can affect, and the whole tree on a
  full-tree trigger. It is the one job that installs `python3-clang-22`
  (with `libclang-22-dev`), for the AST reader.

Call each one as
`uses: malkuth-software/template_harness/.github/workflows/<file>@<commit>`,
with the lock's commit. The workflows read `harness.lock` for their own
harness checkout, so keep the two pins equal and bump them together.
`example.yml` calls all three over `example_app/`. This is the path for a new
product.

**Your own workflows, calling the tools directly.** GUI.Forms works this
way, because its native job does more than one build command: exact-tree
reuse, seeding, sanitizers, packaging and release. Each job:

1. checks the product out at `<source_dir>`;
2. runs the harness-checkout step;
3. calls the tools by their harness path, with `HARNESS_PROJECT` and
   `HARNESS_WORKSPACE` set. For example:

   ```yaml
   - uses: ./gui_forms/.github/actions/harness-checkout
   - run: python template_harness/harness/ci_reuse.py lookup
   - run: sh template_harness/harness/install_linux_ci_packages.sh toolkit "$RUNNER_TEMP/apt-archives"
   ```

The harness has no CI of its own yet. Until it does, GUI.Forms'
house-style job also runs the pinned harness's whole test suite
(`python3 -B -m unittest discover -s ../template_harness/harness -p "test_*.py"`),
so a bump is tested against the harness it pins.

**Cache keys that name harness files** must hash the harness's copies,
never a shim or an old path:

- The Linux packages key:
  `hashFiles('template_harness/harness/install_linux_ci_packages.sh', 'template_harness/projects/<name>.toml')`.
- The macOS runtimes key:
  `hashFiles('template_harness/harness/build_macos_runtimes.py', 'template_harness/harness/macos_runtime_manifest.py')`.

`hashFiles` hashes content, not paths. Because the moved files were
byte-identical, GUI.Forms' runtime key did not change. The Linux key did
change once, because the package sets moved from the script into the
project file.

## 5. Migrating a product that copies or imports `gui_forms/tools`

This is File Manager's case: GUI.Forms as a submodule, with commands and
imports that name `gui_forms/tools/...`. Since GUI.Forms #408, those paths
are forwarding shims (`tools/harness_link.py` lists them):

- run, a shim runs the harness tool with the same arguments;
- imported, it is the harness module;
- without the harness, it exits 1 naming the commit
  `gui_forms/harness.lock` pins.

`tools/ci_build.py` is not a shim. It still exports `configure_tree`,
`sanitizer_options` and `SANITIZER_LINK_FLAGS`, but it now imports
`tools/harness_link.py`, so importing it needs the harness too.
`tools/package_programs.py` is GUI.Forms' own and imports nothing from
the harness. `WINDOWS_SYSTEM_DLLS` and `WINDOWS_FORBIDDEN_RUNTIME_DLL`
therefore import without one, before and after the bump.

**Bumping the submodule past #408** needs the harness beside the
submodule:

- **Place it at `<submodule's parent>/template_harness`.** The shims look
  there: `harness_link.WORKSPACE` is the submodule's parent directory.
  That directory always lies inside your checkout, so add the harness to
  your `.gitignore` (`/template_harness/`):
  - **The product at the workspace root** (File Manager, submodule at
    `./gui_forms`): `<workspace>/template_harness`. It is the same place
    your own harness checkout goes (section 3), so one checkout serves
    both.
  - **The product at `<workspace>/<product>`**:
    `<workspace>/<product>/template_harness`. This is not where your own
    harness checkout goes (`<workspace>/template_harness`), so while you
    use the shims you need both. Moving off the shims (below) removes the
    second.
- **Check it out at the submodule's pin:**
  `lock.py --lock <submodule>/harness.lock checkout <that directory>`.
  In CI, use `uses: ./<submodule>/.github/actions/harness-checkout` with
  `product-path: <submodule>`, or the inline steps of section 3. The
  shims do not verify the commit; a CI step that compares the harness's
  HEAD with the submodule's lock catches a mismatch.
- **The names:** unless `HARNESS_PROJECT` is set, the shims use
  GUI.Forms' project file and names (`gui-…`) in your repository. They
  also default `HARNESS_WORKSPACE` to the submodule's parent. Set
  `HARNESS_WORKSPACE` once for the job. Set `HARNESS_PROJECT` per call,
  to the file whose names that call reads or writes:
  - **the provider's file** for the provider's artifacts. For example, a
    toolchain restore reading `gui-forms-toolchain-*` assets can use
    `template_harness/projects/gui_forms.toml`, or your own file if its
    `[toolchain]` names them (section 2);
  - **your own file** for your artifacts, receipts and caches.
- **The apt cache key:** if it hashes
  `gui_forms/tools/install_linux_ci_packages.sh`, it now hashes a shim
  and no longer follows package changes. Hash the harness script and the
  project file, as in section 4. The shim installs the profile from
  whichever project file `HARNESS_PROJECT` names.

**Moving off the shims** to the harness directly, once you have your own
project file and `harness.lock`:

1. Replace each `python <submodule>/tools/<tool>.py` with
   `python template_harness/harness/<tool>.py`, and
   `sh <submodule>/tools/install_linux_ci_packages.sh` with the harness
   script.
2. For imports, put `template_harness/harness` on `sys.path` and import
   the module by name. Import `configure_tree`, `sanitizer_options` and
   `SANITIZER_LINK_FLAGS` from `ci_build_steps`, not `ci_build`. Keep
   passing GUI.Forms' `tools/sanitizer/lsan.supp` by path; it stays in
   GUI.Forms.
3. Set `HARNESS_PROJECT` to your own project file. Your artifacts, caches
   and receipts then carry your names. That starts one fresh history, so
   the first runs miss.
4. Keep your `harness.lock` equal to the submodule's
   `gui_forms/harness.lock`, and bump them together. One harness checkout
   serves both: yours and any GUI.Forms tool you still run, such as its
   `ci_build.py`.

## 6. Local work

**The bootstrap** sets a workspace up from nothing:

```sh
sh bootstrap.sh --workspace DIR --project <name> --dry-run   # print every step first
sh bootstrap.sh --workspace DIR --project <name> [--in-house] [--platform P] [--no-restore] [--no-smoke]
```

It runs these steps:

1. clones the product into `DIR/<source_dir>`. When the product has a
   `harness.lock`, it checks that harness commit out at
   `DIR/template_harness` and runs the remaining steps from it;
2. clones the notes, with the notes branch as a worktree;
3. clones the private projects repository, with `--in-house` only;
4. restores the toolchain and the newest usable compiler cache. The cache
   is skipped when the project has no `ci.cache_asset_prefix`;
5. runs the smoke command from `DIR`.

With `source_dir = "."`, step 1 clones the product into `DIR` itself.
`DIR` must be empty or missing, or already that clone, which a re-run
updates. Steps 1 to 3 then put `template_harness/`, `notes/`,
`notes-<branch>/` and `projects/` inside the product's checkout, and the
tools later add `.build/`, `.ccache/`, `.ci/`, `.toolchain/` (the Windows
toolchain) and `.bootstrap/` (downloads) there. After the clones,
the bootstrap asks git whether the checkout ignores each of these
(`git check-ignore`) and prints one warning naming any that are not.
List them in the product's `.gitignore`.

It runs `bootstrap.ps1` from PowerShell. Re-running it updates; it never
resets.

The `[bootstrap]` fields:

| Field | Default | Meaning |
|---|---|---|
| `notes_repository` | `<owner>/notes` | the notes clone |
| `notes_branch` | the project name | the worktree `DIR/notes-<branch>` |
| `projects_repository` | `<owner>/projects` | cloned with `--in-house` |
| `smoke` | none | the smoke command, run from `DIR`; `{python}` becomes the bootstrap's interpreter |

Pick a smoke command that is quick and needs only what the restore
supplies. GUI.Forms uses
`["{python}", "gui_forms/tools/ci_build.py", "--tests", "^gui_forms_core_tests$"]`:
a plain build of what one test reads, then that test, with nothing
installed. Its build driver was changed to accept that command; check
that yours accepts the command you name.

**The checkpoint tool** folds workers' local branches into one verified PR.
It is the default way work reaches GitHub: workers never push, and the
coordinator pushes one checkpoint after a full local pass. `docs/workflow.md`
describes the whole workflow, what `verify` must run, and the build-host and
agent setup it needs. Set the tool up once, in a dedicated workspace:

```sh
export HARNESS_WORKSPACE=<integration workspace> HARNESS_PROJECT=<harness>/projects/<name>.toml
python3 template_harness/checkpoint/checkpoint.py init            # clone and mark <workspace>/<tree>
python3 template_harness/checkpoint/checkpoint.py fold <clone>:<branch> ...
python3 template_harness/checkpoint/checkpoint.py verify          # runs [checkpoint] verify from the workspace root
python3 template_harness/checkpoint/checkpoint.py push --dry-run
```

In `[checkpoint]`:

- `tree` is the integration clone (`init` marks it, and every other
  command refuses an unmarked tree). For a product at the workspace root
  it is `"."`, the integration workspace itself. `init` then clones in
  place, beside the harness already there. If an entry there would be
  overwritten, it stops and removes the `.git` it made. An existing clone
  at the root is adopted as usual;
- `verify` is your whole local verification, run from the workspace root
  with `HARNESS_CHECKPOINT_TREE` set. It must run everything CI would
  reject: the static gates first, then the whole suite plain, the
  state-machine gate over the change, and the whole suite under the
  sanitizers (`docs/workflow.md` §4). GUI.Forms uses `["sh", "build.sh",
  "all"]`, a script in its integration workspace that does exactly that on
  its build host;
- `push` refuses unless `verify` passed on exactly the current tree, then
  opens one PR with auto-merge on (which needs branch protection; without
  it, the coordinator merges on green).

### Local lockstep builds

On a development machine, every product builds against one local GUI.Forms:
the lockstep clone, which `harness/lockstep.py` builds once per commit into
an installed prefix. GUI.Forms is then compiled once for all the products
instead of once per product. The coordinator advances the clone, either to
GUI.Forms main or to a checkpoint fold head, and builds it. Product workers
only read the prefix. Pushing a product change that needs a GUI.Forms change
therefore waits until that GUI.Forms change is merged. The product PR then
pins that commit. CI still builds each product's pinned, pushed GUI.Forms.

Everything lives under one root: `--root`, else `HARNESS_LOCKSTEP_ROOT`,
else `D:/malkuth` on Windows and `~/malkuth` elsewhere.

| Path under the root | What |
|---|---|
| `gui_forms_lockstep/gui_forms/` | the clone, detached at the lockstep commit |
| `gui_forms_lockstep/template_harness/` | the harness, at the commit in the clone's `harness.lock` |
| `gui_forms_lockstep/.build/native-<target>/` | the build trees, reused from commit to commit |
| `cache/ccache/` | the compiler cache every local build may share |
| `cache/lockstep/gui-forms-<commit>-<target>/` | one install per built commit; older ones stay |
| `cache/lockstep/current-<target>.json` | the record of the active install |

The workspace is laid out like CI's, with `gui_forms/` beside
`.build/native-<target>/gui-forms` and `CCACHE_BASEDIR` at the workspace. On
Windows the build runs with `LANG=en_US.UTF-8`, as CI's Windows jobs do.
GUI.Forms' `.gitattributes` checks every text file out with LF on every host,
so the clone has CI's bytes whatever the local git configuration says. ccache
keys every object on the bytes and paths it reads and on the locale, so these
settings let the local cache match the per-commit caches that GUI.Forms
publishes.

The coordinator's commands:

```sh
python3 template_harness/harness/lockstep.py init --seed <a GUI.Forms checkout> [--seed <a source SDK>] [--reference <a clone>]
python3 template_harness/harness/lockstep.py advance --to main            # or --to <branch> --from <a local clone>
python3 template_harness/harness/lockstep.py build                        # with the pinned toolchain on PATH
python3 template_harness/harness/lockstep.py status
```

`--seed` copies the untracked sources that a build reads
(`third_party/harfbuzz`, `freetype`, `libunibreak` and `miniaudio`) from
trees that already have them, so the first build downloads nothing.
GUI.Forms' fetch scripts still fetch any pinned revision that the seeds do
not have.

`init` and `advance` also check template_harness out at
`gui_forms_lockstep/template_harness/`, beside the clone, at the commit in
the clone's `harness.lock`. They use `harness/lock.py`'s checkout, and
`--github-base` replaces `https://github.com`. GUI.Forms' own
`tools/ci_build.py` imports from that checkout. `build` and `prefix` refuse
when it is missing or at another commit; `init` puts it back.

`build` always runs `tools/build_sdk.py --features all`. On top of the
default (Audio with the stx Vorbis decoder, the HarfBuzz/FreeType text
engine and the host's Application), this adds every optional component a
product may link: `GUIForms::TextMasks` and `GUIForms::AudioLoopTransport`.
One install therefore serves every product. A GUI.Forms commit whose
`build_sdk.py` has no `--features` is refused.

`build` does nothing when the install already records the same commit,
target, toolchain identity and features. When several product workers run it at once,
one builds while the rest wait on `cache/lockstep/.lockstep.lock`, then find
the record. A failed build leaves `current-<target>.json` as it was.

**A product built with its own CMake** passes the prefix as
`CMAKE_PREFIX_PATH`:

```sh
cmake -S . -B build -DCMAKE_PREFIX_PATH="$(python3 template_harness/harness/lockstep.py prefix)" ...
python3 template_harness/example_app/ci_build.py --gui-forms-prefix "$(python3 template_harness/harness/lockstep.py prefix)"
```

`prefix` fails, naming the command to run, when the clone's HEAD has not
been built. A product therefore never builds against a stale install by
accident.

**A product that builds GUI.Forms as a submodule** (File Manager builds it
through `gui_forms/tools/ci_build.py`) needs an override that skips that
build. Everything it would otherwise take from its submodule then comes from
the record, which `lockstep.py prefix --json` prints. The record is also the
file `cache/lockstep/current-<target>.json`:

| Field | Meaning |
|---|---|
| `schema` | `1` |
| `commit` | the GUI.Forms commit that was built; the clone's HEAD while it is current |
| `target` | `windows-x64`, `macos-arm64`, `linux-x64` or `linux-arm64` |
| `toolchain_identity` | `HARNESS_TOOLCHAIN_IDENTITY`, else a digest of the clang binary, its version and the host |
| `features` | the `build_sdk.py --features` set it was built with: `all` |
| `compiler` | the clang that built it (absolute path); the product must use the same one |
| `prefix` | the install: `CMAKE_PREFIX_PATH` |
| `package_dir` | `<prefix>/lib/cmake/GUIForms`: `GUIForms_DIR` |
| `source` | the lockstep clone: the toolchain file, `tools/ci_build.py`'s `configure_tree`, the sanitizer options and `tools/sanitizer/lsan.supp` come from here, and `git -C <source> rev-parse HEAD` is `commit` |
| `harness` | the template_harness commit checked out beside `source` (`<ccache_basedir>/template_harness`), taken from `source`'s `harness.lock`; `""` for a GUI.Forms commit without one |
| `toolchain_file` | `<source>/cmake/llvm22.cmake`, the toolchain file the prefix was built with |
| `font_dir` | `<prefix>/share/GUIForms/fonts`: `GUI_FORMS_FONT_DIR` |
| `llvm_runtime` | the build's `GUI_FORMS_LLVM_RUNTIME` on macOS, else `""` |
| `ccache_dir` | the shared compiler cache, which the product may use too |
| `ccache_basedir` | the lockstep workspace (a product sets its own `CCACHE_BASEDIR`) |
| `lang` | the `LANG` the build ran with (`""` when it was left alone) |

Before it uses a record, the override checks that `source`'s HEAD is
`commit`. `prefix` makes the same check.

## 7. The gate for each product

A product has adopted the harness when all three of these hold:

1. **CI is green through the harness.** Every job that used a copied
   tool now runs the pinned harness: the reusable workflows, or your own
   workflows calling `template_harness/harness/…`. GUI.Forms passed this
   with #408.
2. **The bootstrap sets the product up on a clean machine.** Run
   `bootstrap.sh --workspace <empty directory> --project <name>` there,
   and record what it restored and the smoke command's result.
3. **The names are checked.** `harness/test_project_prefixes.py` already
   fails when your prefixes overlap another project's. Also add a test beside
   `harness/test_project_names.py` that loads `projects/<name>.toml` with
   `project.load` and asserts each derived name literally:
   - `variable()`;
   - `artifact()` for each kind;
   - `cache_asset()` for each platform;
   - `build_tree()`;
   - the toolchain asset names;
   - the package profiles.

   A later edit that would rename a cache then fails here first. A
   product migrating existing caches copies its current names into the
   test before switching, as GUI.Forms did.

## 8. Checklist

- [ ] The layout chosen once: `<workspace>/<source_dir>`, or the product at the workspace root (`source_dir = "."`, section 1).
- [ ] `projects/<name>.toml` merged in the harness, with prefixes per section 2 and the names test (section 7).
- [ ] `harness.lock` in the product, pinning a commit that contains the project file.
- [ ] `harness.lock` and every forwarding file listed in `[state_machines] full_tree_triggers`, and in your own change selection.
- [ ] Every CI job checks the product out at `<source_dir>` (at the root for `"."`), then the harness, then calls harness paths.
- [ ] At the workspace root: `.gitignore` lists `template_harness/`, `notes/`, `notes-*/`, `projects/`, `.build/`, `.ccache/`, `.ci/`, `.toolchain/` and `.bootstrap/`; the bootstrap warns about any it misses.
- [ ] Any `uses: …@<commit>` pin equals `harness.lock`.
- [ ] Cache keys hash harness files and the project file, never shims.
- [ ] Windows: a published toolchain release the restore can read, your own or GUI.Forms' (section 2).
- [ ] Release and packaging jobs updated too, including ones no PR run reaches.
- [ ] Anything shipped to consumers runs without the harness.
- [ ] Bootstrap on a clean machine; smoke command passes.
- [ ] `[checkpoint]` set and `checkpoint.py verify` passes in the integration workspace.

**Pitfalls GUI.Forms hit:**

- **Line endings.** This repository checks out LF everywhere except
  `*.ps1` and `*.cmd` (`.gitattributes`). Without a `.gitattributes`
  of its own, a product's line endings in a checkout follow that
  machine's git configuration (`core.autocrlf`). A hash over file bytes
  can then differ between hosts, and between a product copy and the
  harness's copy of the same file. Set your own `.gitattributes`, as the
  harness does; don't rely on the runner's git config. GUI.Forms
  sets `* text=auto eol=lf`, so its checkouts are byte-identical on
  every host.
- **Compiler-cache keys on Windows: line endings and locale.** ccache
  keys every object on the exact bytes of every file the compile reads,
  and on the locale. Before its `.gitattributes`, GUI.Forms' Windows CI
  checked its sources out with CRLF (the runner's git has
  `core.autocrlf=true`) and ran under MSYS2 with `LANG=en_US.UTF-8`. A
  local build from LF sources with no `LANG` produced byte-identical
  objects under different keys: it hit 3 of 526 compilations against
  CI's published cache. Matching both hit 525 of 526 (2026-10-27). With
  LF checkouts everywhere only the locale is left to match, and the
  lockstep build sets the record's `lang`. When a product's checkouts
  become LF everywhere, as GUI.Forms' did, every Windows cache restarts
  once.
- **Identities over moved files.** An identity you compute by hashing
  files that later move into the harness keeps its value only while the
  harness copies are byte-identical under their old names. A harness bump
  that changes those files then changes the identity.
- **`hashFiles` on shims.** A key over a shim's path hashes the shim.
  It changes once, then never follows the real tool again. Point keys at
  `template_harness/…` (section 4).
- **The macOS runtime key.** Repoint it to the harness files in every
  workflow that builds the runtimes: GUI.Forms has it in both `native-platform.yml` and
  `release.yml`. It stays unchanged only while the files are
  byte-identical.
- **The release workflow's prepare job.** It checked GUI.Forms out at
  the workspace root, so there was no harness beside it, and it still
  ran a test that had moved to the harness. PR CI never runs it. Check
  the product out at `<source_dir>`, set the job's default
  `working-directory`, and run moved tests from the harness:
  `unittest discover -s ../template_harness/harness -p test_prune_cache_releases.py`.
  From Python 3.12, `unittest discover` exits nonzero when it finds no
  tests, so a step that names a deleted test fails.
- **`build_sdk.py` stays harness-free.** GUI.Forms' source SDK archive
  carries `tools/` but no harness, and its build entry runs on the
  consumer's machine. Its configure code therefore lives in
  `tools/toolkit_configure.py`, which imports nothing from the harness.
  `tools/test_harness_shims.py` runs the entry from a directory that has
  no harness, to prove it. Split anything you ship to consumers the same
  way.
