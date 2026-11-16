# The object surface check

`harness/check_object_surface.py` proves, from a component's compiled
objects, what the component can reach outside itself. A component that
states "I call only these host functions" (an allocator, a line breaker, a
parser) gets that statement checked on what runs, on every host family the
CI builds, with a stable exit code for a gate.

## 1. What it proves, and what it does not

It runs `nm -P` on every object of the component, takes the symbols those
objects leave undefined (`U`, `w`, `v`), and removes the ones another object
of the component defines. What remains are the call graph's leaves: the only
ways out of the component's code. Each leaf must be

- on the component's list for the host family being built, or
- a toolchain helper (a call the compiler emits on its own: `memset`,
  thread-local access, the stack protector), or
- in a sanitizer build only (`--sanitized`), a sanitizer runtime entry.

Anything else fails: `malloc`, `getenv`, a stream, a lock of the C library,
the exception runtime, an unexpected thread-local initialiser.

Each family also names a **must-see** leaf, one the component's host layer
always calls (`mmap`, `VirtualAlloc`). A scan that does not find it fails as
`BLIND`: the objects were not the right ones, or the toolchain hides host
calls (link-time optimisation bitcode, say), and a pass would mean nothing.

Not covered: calls through function pointers handed in from outside, and
which use of an allowed symbol is made. Symbol names are compared after
stripping leading underscores, the `__imp_` import prefix and an ELF
`@version`, so one list serves Mach-O, ELF, COFF and wasm objects.

## 2. The component file

One TOML file per component; every allowed entry carries a reason, and a
missing reason is a configuration error. The worked example is
`harness/testdata/object_surface/stx_minmallocpp.toml`.

```toml
[component]
name = "stx_minmallocpp"

[objects]
targets = ["stxlib_stx_minmallocpp_checked"]  # <build>/CMakeFiles/<target>.dir
directories = []                               # or paths under --build-dir
under = ""                                     # optional path prefix filter

[families.posix]
must_see = "mmap"
allowed = [
  { symbol = "mmap", reason = "reserves and commits address space (os.hpp)" },
]

[toolchain]                                    # every family
allowed = [
  { symbol = "memset", reason = "the compiler lowers zeroing to it" },
  { pattern = "emutls_v.*", reason = "emulated thread-local control blocks" },
]

[sanitized]                                    # only with --sanitized
allowed = [{ pattern = "asan_*", reason = "AddressSanitizer runtime" }]
```

Families are named by the component; `posix`, `windows` and `web` are the
house's. A `pattern` is a shell glob on the stripped name. Globals that are
an ABI rather than calls (the wasm `__stack_pointer`) go on the one family
that has them, so they are refused elsewhere. `--list` prints every entry
with its reason, for review.

## 3. Running it

```sh
python3 -I harness/check_object_surface.py --config C.toml --family posix \
    --nm "$CMAKE_NM" --build-dir build [--sanitized] [--allowance A.txt]
```

`--objects DIR` (with `--under PREFIX`) names an object directory instead of
the file's targets. Pass the toolchain's own `nm` (`CMAKE_NM`, `llvm-nm`):
the host `nm` may not read another platform's objects. In CMake the usual
form is a test per component, guarded on a Ninja or Makefiles generator
(which keep objects at `CMakeFiles/<target>.dir`):

```cmake
add_test(NAME my_component_surface COMMAND ${Python3_EXECUTABLE} -I
  ${harness}/check_object_surface.py --config ${CMAKE_CURRENT_SOURCE_DIR}/tools/object_surface/my_component.toml
  --family ${family} --nm ${CMAKE_NM} --build-dir ${CMAKE_CURRENT_BINARY_DIR})
```

**Exit status:** 0 pass; 1 findings; 2 a usage, configuration or tool error
(a bad file, no objects found, `nm` failed). A gate fails on anything but 0.

**Output**, one line each, with stable prefixes:

| Line | Meaning |
|---|---|
| `LEAF <object>: <symbol>` | an unlisted leaf, not in the allowance |
| `OVER <family> <symbol> <seen> <allowed>` | more objects leave it undefined than the allowance permits |
| `STALE <family> <symbol> <allowed> <seen>` | the allowance permits more than is seen; lower the line |
| `BLIND <family> <symbol>` | the must-see leaf was not found |
| `ERROR <text>` | exit 2 |
| `leaves: ...` | every leaf seen, allowed or not |
| `SUMMARY <component> <family>: PASS\|FAIL (<n> objects, <m> leaves)` | last line |

`--json OUT` writes the same report as JSON (`component`, `family`,
`passed`, `objects`, `leaves`, `findings`).

## 4. The allowance: shrink-only

As with the early-exit check (`docs/early_exit_check.md`), a component that
adopts the check with leaves it is still removing lists them in an
allowance file instead of on the permanent list:

```text
# <family> <symbol> <count>: at most <count> objects may leave it undefined
posix getenv 1
```

A leaf beyond its count fails (`OVER`); a line whose count is above what is
seen fails as `STALE`, so the file cannot hide progress. `--write-allowance`
rewrites the file for the family being checked, only lowering counts and
dropping lines that reach zero; it never adds a line. A new allowance line
is a hand edit, made in review. A missing file counts as empty.

## 5. Adopters

- **STXlib, stx_minmallocpp** (first adopter): `tools/object_surface/stx_minmallocpp.toml`,
  run as the ctest `stxlib_stx_minmallocpp_surface` on every host family.
- **Next:** STXlib's new line breaker and bidi code should adopt it next,
  each with its own component file stating the leaves its design allows.
