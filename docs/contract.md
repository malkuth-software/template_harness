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
| `harness/project.py` | The project file (TOML, `schema = 1`), found through `HARNESS_PROJECT` or `harness.toml` at the workspace root (`HARNESS_WORKSPACE`, else the current directory). Commands: `show`, `linux-packages <profile>`, `variable <NAME>`. Imported: `current()` and `load(path)`. |
| `harness/ci_build_steps.py` | Imported by a product's build driver: `configure_tree(toolkit, configure)` and `sanitizer_options(host, lsan_suppressions, debug_info='lines')`. |
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

The names a project file produces are part of the contract too: a product's
caches, receipts, artifacts and toolchain releases carry them.
`harness/test_project_names.py` holds GUI.Forms' names byte for byte.

GUI.Forms keeps its own contract for what stays there, such as
`tools/package_programs.py`'s `WINDOWS_SYSTEM_DLLS` and
`WINDOWS_FORBIDDEN_RUNTIME_DLL` and its `tools/ci_build.py` driver.

## Changing the contract

Renaming, moving or reshaping anything above (a script, a command, a
function's parameters, a constant's name or type, or a project-file field)
is a breaking change: record it under "Breaking changes" in the harness's
release notes, naming the old and new form, and update this page and
`harness/test_contract.py` in the same change. A project-file change that
breaks existing files also raises `schema`. Adding a tool or a field is not
breaking, but it still goes on this page and into the test.

Behaviour inside these tools (which packages a profile installs, how a
cache is evicted) may change without an entry, as long as the documented
call still works.
