# The harness contract

Products build with this repository's tools instead of copies: GUI.Forms
first, then File Manager, SwiftEdit and others. The tools, commands and
names below are therefore a public contract, in the same way as a library's
headers. `harness/test_contract.py` imports each named function and
constant and checks that each script, command and project-file field still
exists, so a silent break fails this repository's tests before it reaches a
product.

## The contract

| Tool | What products use |
|---|---|
| `harness/project.py` | The project file (TOML, `schema = 1`), found through `HARNESS_PROJECT` or `harness.toml` at the workspace root (`HARNESS_WORKSPACE`, else the current directory). Commands: `show`, `linux-packages <profile>`, `variable <NAME>`. Imported: `current()` and `load(path)`. `source_dir = "."` puts the product at the workspace root (`Project.at_workspace_root()`). `ci.cache_asset_prefix`, `ci.pinned_cache_revisions`, `tests.areas_file` and the `[state_machines]` table are optional: a tool that needs one raises `project.MissingField` ("project <name> has no <field>"). `ci.native_job` is a job-name prefix or a template with `{platform}` (`Project.native_job_matches(job_name, platform)`). |
| `harness/ci_build_steps.py` | Imported by a product's build driver: `configure_tree(toolkit, configure)`, `sanitizer_options(host, lsan_suppressions, debug_info='lines')`, and `SANITIZER_LINK_FLAGS` (a string of linker flags; GUI.Forms' `ci_build.SANITIZER_LINK_FLAGS` before the move). |
| `harness/windows_toolchain.py` | `restore`: restores the published Windows CLANG64 toolchain from the project's `[toolchain] repository` (else `GITHUB_REPOSITORY`). |
| `harness/compiler_identity.py` | Run as a script: identifies and verifies the LLVM release compiler. |
| `harness/install_linux_ci_packages.sh` | `<profile> <archive directory>`: installs one of the project file's Ubuntu package profiles. |
| `harness/macos_runtime_manifest.py` | Run with a runtime prefix; imported: `verify(prefix)`. |
| `harness/build_macos_runtimes.py` | Run as a script: builds the minimum-compatible macOS runtimes. |
| `harness/audit_macos_minimum.py` | Run with directories to audit. |
| `harness/ci_cache.py` | `evict` (drop the compiler-cache entries a job did not use) and `prune` (delete superseded cache generations). |
| `harness/check_house_style.py` | Run with the directories to check. |
| `harness/check_lifetime_rules.py` | Run with the directories to check. |
| `harness/cmake/llvm22.cmake` | The CMake toolchain file for LLVM 22. On macOS it reads the runtime prefix from `<HARNESS_ENV_PREFIX>_LLVM_RUNTIME`, else `HARNESS_LLVM_RUNTIME`. |
| The workspace layout | The workspace root holds the product checkout `<source_dir>/` beside its build trees `.build/native-<platform>/<build_name>/` (`-sanitize` for the sanitized tree), and is `CCACHE_BASEDIR`, with `CCACHE_DIR=<workspace>/.ccache` and `CCACHE_COMPILERCHECK=content`. A compiler cache hits only for a product built in this layout; `project.Project.build_tree(platform, sanitize)` names the trees. With `source_dir = "."` the product's checkout is the workspace root itself, holding `template_harness/`, `.build/`, `.ccache/` and `.ci/`, which it must ignore. |
| `checkpoint/checkpoint.py` | **Provisional**: may change without a breaking entry until it is marked stable here. `init [--from URL]`, `fold <clone>:<branch> ... [--title [N=]TEXT]`, `verify`, `push [--dry-run]`, `after-merge`; settings in the project file's `[checkpoint]` table; `tree = "."` is the workspace root, which `init` clones into in place. |
| `harness/lock.py` and a product's `harness.lock` | The pin (owner decision 2): `[harness] repository = "owner/name"`, `commit = "<40 hex>"` on one line each. Commands: `show`, `checkout DIR` (the exact commit, detached; refuses a dirty DIR), `bump [--to REF]`; options `--lock PATH`, `--github-base URL`. |
| `.github/actions/harness-checkout` | A composite action: inputs `product-path` (holding `harness.lock`) and `path` (default `template_harness`); outputs `repository` and `commit`. It checks the locked commit out beside the product and verifies it; with `product-path: .` the harness lands inside a root-layout product at `template_harness/`. |
| `bootstrap.sh`, `bootstrap.ps1`, `harness/bootstrap.py`, `harness/restore_local.py` | **Provisional**, as above. `bootstrap.sh --workspace DIR --project NAME [--in-house] [--dry-run] [--platform P] [--no-restore] [--no-smoke]`; settings in the project file's `[bootstrap]` table. With `source_dir = "."` the product is cloned into `DIR` itself and the bootstrap warns about any of its entries the checkout does not ignore. |
| `harness/lockstep.py` | **Provisional**, as above. `init`, `advance --to REF [--from REMOTE]`, `build [--target T]`, `prefix [--target T] [--json]`, `status`, all with `--root` (else `HARNESS_LOCKSTEP_ROOT`); product builds read `prefix` or the record `cache/lockstep/current-<target>.json` (schema 1, fields in `docs/adopting.md`). |

The names a project file produces are part of the contract too: a product's
caches, receipts, artifacts and toolchain releases carry them.
`harness/test_project_names.py` holds GUI.Forms' names byte for byte.

GUI.Forms keeps its own contract for what stays there, such as
`tools/package_programs.py`'s `WINDOWS_SYSTEM_DLLS` and
`WINDOWS_FORBIDDEN_RUNTIME_DLL` (File Manager imports them;
`package_programs.py` imports nothing from the harness, so they import
without one, while `tools/ci_build.py` needs it), its `tools/ci_build.py` driver, and
`tools/sanitizer/lsan.supp`, the LeakSanitizer suppressions File Manager
passes to `sanitizer_options`: they name the system libraries GUI.Forms'
hosts load and must never name a GUI.Forms frame, so they stay with
GUI.Forms and its first-party check.

## Changing the contract

Renaming, moving or reshaping anything above (a script, a command, a
function's parameters, a constant's name or type, or a project-file field)
is a breaking change: record it under "Breaking changes" in
`CHANGELOG.md`, naming the old and new form, and update this page and
`harness/test_contract.py` in the same change. A project-file change that
breaks existing files also raises `schema`. Adding a tool or a field is not
breaking, but it still goes on this page and into the test.

Behaviour inside these tools (which packages a profile installs, how a
cache is evicted) may change without an entry, as long as the documented
call still works.
