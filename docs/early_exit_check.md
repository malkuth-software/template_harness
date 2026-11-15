# The early-exit integrity check

`harness/check_early_exit.py` runs a clang tool, `early_exit_check`
(`harness/early_exit/`), over a product's compile database. One
implementation serves GUI.Forms and STXlib. STXlib writes no checker of its
own.

The rules come from three notes decisions of 2026-11-15:
- `early-exit-integrity`: the rule, its two forms, and allocators do not
  unwind;
- `thread-local-exit-safety`: rule 1 as reworded, rule 2 (thread_locals)
  and rule 3 (exit).

> Every call to code we don't control is a possible early exit. At each such
> call, either the work that may panic is finished before an invariant breaks
> (form 1), or the broken state is safe to unwind through (form 2).
> Allocators must not unwind. Termination frees nothing twice: an owner lets
> go of a resource before releasing it.

**The vocabulary is STXlib's.** The annotation strings, the site markers,
the exit classes, rules R1 to R10 and patterns P1 to P13 are defined in
section 1 ("For the shared check") of STXlib's audit E,
`notes/audits/E-early-exit.md` on sb3's branch. This document says how the
check implements them, and where it differs.

The bug behind the rule was in GUI.Forms' `abi_class_bridge.cpp`. Its
`enter_class_frame` set the thread's `top_frame`, called a client's class
entry through a C function pointer, and then restored `top_frame` by hand.
A C++ client whose entry threw left `top_frame` pointing at a dead frame.
cp32 fixed it with a scope guard and a fail-fast call. The fixture
`r5_guardless_mutation.cpp` holds both the bug and the fix.

## 1. The vocabulary, as the check reads it

### Declaration marks

Each mark is `[[clang::annotate("early_exit.<name>")]]`, read from any
declaration of the entity, or from its template pattern.

| Mark | The check reads it on |
|---|---|
| `early_exit.foreign` | A function. Calls to it are foreign calls. A function marked `foreign` and also `cancel` or `no_exit` narrows the class to cancel or none. |
| `early_exit.cancel` | A function: see `foreign` above. |
| `early_exit.no_exit` | A function: see `foreign` above. |
| `early_exit.own` | A function-pointer typedef or alias, a variable or member of such a type, or (an extension, below) a class or virtual method. Calls through it are not foreign. |
| `early_exit.no_unwind` | A function: rule R6. |
| `early_exit.guard` | A class: an object of it may be named by a `Guarded` marker, and counts as a live guard for R5. |
| `early_exit.lock` | A class: a live object of it holds a lock (R2). `std::lock_guard`, `std::unique_lock`, `std::scoped_lock` and `std::shared_lock` count by name. |
| `early_exit.open` | A class: a live object of it holds an invariant broken (R4). |
| `early_exit.no_cancel` | A class: while an object of it is live, class cancel counts as none. |
| `early_exit.whole` | A function definition: function-level form 1 (R1, R4). |
| `early_exit.forced_unwind` | A function: R10. |

**Extension:** `early_exit.own` is also read on a class, where it covers
all the class's virtual methods, and on a virtual method. In both cases a
virtual call that reaches only our own overrides is not foreign. GUI.Forms
needs this for virtual dispatch inside its own class hierarchies. STXlib
may adopt it or leave it unused.

### Site markers

A site marker is a local variable whose type's qualified name ends in
`early_exit::Whole`, `early_exit::Guarded` or `early_exit::NoExit`. The
check matches only the last two name components, so each project keeps the
types in its own namespace.

- **Placement.** The marker must be the statement immediately before the
  call's statement, in the same block. It covers every foreign call in that
  one next statement.
- **The reason.** The reason is the first string literal in the marker's
  initializer, and an empty or missing reason is an R1 finding. For a
  `Guarded` marker, the guard is the first variable the initializer names.
- **NoExit.** Every `NoExit` marker is listed for review as a report, not a
  finding. One whose reason begins `terminate accepted` is tagged P13.

### Identifying foreign calls and their exit class

STXlib's section 1.4 defines foreign calls and exit classes. The check
applies them as follows.

1. **Declared in a system header, or under a configured foreign path.**
   STXlib's `third_party/` is such a path.
   - **The C++ standard library** sits in system headers. Under
     `standard_library = "foreign"` (STXlib) it is foreign like any host
     code. Under `"own"` (GUI.Forms, which uses the standard library as
     part of its own implementation) it is not foreign, except the names in
     `foreign_functions`: `std::function::operator()`,
     `std::move_only_function::operator()` and `std::invoke`.
2. **Indirect calls.** A call through a function pointer, a pointer to
   member function, or a virtual function (unqualified, not `final`) is
   foreign, unless an `own` mark covers it.
3. **Dependent callables.** A callee whose type was a template parameter is
   foreign. The check reads templates through their instantiations, and
   finds these calls by the substituted-template-parameter sugar on the
   callee's type: `f(...)` or `f.operator()` where `f` is declared as a
   template parameter type.
4. **Marked `early_exit.foreign`.**
5. **The project's `foreign_functions`.** Regular expressions over
   qualified names. Class template arguments are dropped, as in
   `gui_forms::Event::emit`. GUI.Forms lists its event and delegate
   invocations here until its declarations carry the mark.

The exit class of a foreign call:
- **none:**
  - the callee is in the project's `no_exit_functions`, or is marked
    `no_exit`;
  - or (the derived table) the callee is a direct foreign function declared
    `noexcept` or `nothrow`, with no parameter of function-pointer or
    callable type, and not a cancellation point.
- **cancel:**
  - the callee is in `cancellation_points`, or is marked `cancel`. The
    default list is POSIX.1-2024 XSH 2.9.5.2's required table, plus the
    may-occur entries glibc implements that the defaults name; extend it in
    the project file.
  - Inside a live `no_cancel` object, cancel becomes none.
- **terminate:** an indirect or dependent call whose function type is
  `noexcept`.
- **any:** every other foreign call.

When a function is on both lists, `no_exit_functions` wins. `fcntl` is
such a case: STXlib calls it with `F_SETFD` and `F_GETFD` only.

### Exception mode

Form 2 exists only where the frame has landing pads. The check takes each
unit's mode from clang's language options for that unit, which come from
its compile command: `-fno-exceptions`, or `/EHs-c-` through clang-cl.
- In a unit without exceptions, a `Guarded` marker is an R3 finding.
- A live guard does not cover R5.
- R10 does not apply, since no `catch` can be written.

## 2. The rules

Each finding prints as `path:line:column: Rn [Pn] function: message`.
Report-level entries print the same way under "for review" and never fail a
run.

| Rule | The check reports |
|---|---|
| R1 | A foreign call of class cancel, terminate or any, with no site marker and no `early_exit.whole` mark on the function. Also a site marker with an empty reason. |
| R2 | A foreign call of class cancel, terminate or any while a lock object is live, or between `x.lock()` and `x.unlock()` on the same object in one function. Markers do not cure it. |
| R3 | A `Guarded` marker in a unit without exceptions, a guard not of an `early_exit.guard` class, or a guard that is not live. |
| R4 | A foreign call of class cancel or any while an `open` object is live, without a valid `Guarded` marker. Also a `Whole` marker, or an `early_exit.whole` function, with an `open` or lock object live at a call. |
| R5 | A foreign call of class cancel or any between the two halves of a pair in one function, with no live guard (in a unit with exceptions). The pairs: <br>- `++x` and `--x`, `x += 1` and `x -= 1`, `x = x + 1` and `x = x - 1`;<br>- two different boolean constants assigned to `x`;<br>- calls `*_enter`/`*_leave`, `begin*`/`end*`, `acquire*`/`release*`, `push*`/`pop*` and `open*`/`close*` on the same object or first argument (`lock`/`unlock` is R2);<br>- **an extension, from GUI.Forms' `top_frame` bug:** state saved into another place, changed, and after the call assigned back from the saved copy.<br>These are syntactic pairs: a finding means "pair", not a proof. |
| R6 | In a function marked `early_exit.no_unwind`, a finding for each of the following:<br>- a declaration that is not `noexcept`;<br>- a `throw`, a `setjmp`/`longjmp` (also `_setjmp`, `sigsetjmp`, `siglongjmp`), a throwing `operator new`, or a call to `std::get_new_handler` or `std::set_new_handler`;<br>- a direct call to a project function that is neither marked `no_unwind` nor `noexcept`;<br>- a direct foreign call not in `allocator_allowlist`;<br>- an indirect or dependent call not through an `own` alias whose function type is `noexcept`. |
| R7 | A `thread_local`, `_Thread_local` or `__thread` variable whose type is not trivially destructible. Report-only: one with dynamic initialization (not `constinit`, not constant-initialized). |
| R8 | A call to `exit` or `quick_exit` (global or `std`). **Extension:** `at_quick_exit` too, as GUI.Forms' owner named it. Report-only: `_Exit`, `_exit`, `ExitProcess` and `TerminateProcess`. |
| R9 | **Release while held** (rule 1 as reworded). A release (`free(p)` and the project's `release_functions`, `delete p`, `std::destroy_at(p)`, an explicit destructor call, or `x.reset()` with no arguments) of non-local state (a member, a global or `thread_local`, anything through a pointer) is a finding when the owner was not let go of before it in the same function. Letting go is one of:<br>- an assignment of a null or constant mark to the owner;<br>- `std::exchange(owner, ...)`;<br>- a move of the owner into a local.<br>A local the function took the resource into needs nothing. **Where the check differs:** `reset()` on `std::unique_ptr` and `std::shared_ptr` is not reported, since the standard has them replace the pointer before deleting the old object. |
| R10 | In a unit with exceptions, a `catch (...)` handler that neither contains `throw;` nor begins (its first call in source order) with a call to an `early_exit.forced_unwind` function. |

**Liveness** is scope-based and syntactic, as STXlib defines it: an object
is live at a call when its declaration comes earlier in the same block, or
in an enclosing block of the same function.

**Patterns.** A finding is tagged with the STXlib pattern it matches, where
the check can tell:
- P1: R2 on a call of class cancel;
- P2: R2 on an indirect or dependent call;
- P3: R2 on a condition wait or join;
- P4: R4 or R5 on an indirect or dependent call;
- P6: R10;
- P7: R1 on an indirect call of class any;
- P8: R6;
- P9: R7;
- P10: R8;
- P12: R9;
- P13: a NoExit marker whose reason begins `terminate accepted`.

P5 (multi-step durable state) and P11 (a lock pair across functions) are
not mechanical.

## 3. Running it

```text
python3 -B harness/check_early_exit.py --build-dir <configured ninja tree> [--gate] [--jobs N]
python3 -B harness/check_early_exit.py --build-dir <tree> --write-allowance
python3 -B harness/check_early_exit.py --self-test
```

- **Building the tool.** The driver builds `early_exit_check` with CMake
  and Ninja against the pinned LLVM 22's Clang libraries. The prefix is
  `--llvm-prefix`, or `llvm-config --prefix` from the pinned toolchain on
  PATH:
  - macOS: Homebrew llvm@22;
  - Windows: CLANG64's clang package;
  - Linux: apt.llvm.org's libclang-22-dev.

  The build goes to `--tool-build-dir`, by default
  `<workspace>/.build/harness/early_exit_check`. `--tool PATH` uses a tool
  that is already built.
- **Running it.** The driver reads the tree's compile database
  (`ninja -t compdb`) and keeps the first-party units. It runs the tool in
  `--jobs` batches and keeps one copy of each finding: a header's finding
  repeats in every unit that includes it, and a template's in every
  instantiation.
- **Settings.** The project file's `[early_exit]` table (the defaults are in
  `check_early_exit.py`):
  - `first_party_dirs`, `foreign_paths` and `standard_library`;
  - `foreign_functions`, `no_exit_functions`, `cancellation_points`,
    `allocator_allowlist` and `release_functions`, regular expressions over
    qualified names;
  - `allowance`, the allowance file's path in the product checkout.
- **The allowance is shrink-only.** Each line is
  `<rule> <path>::<function> <count>`. A run fails on a finding beyond its
  line's count. It also fails on a line whose count is higher than the
  findings: the line is stale, so lower it or run `--write-allowance`.
  `--gate` makes any finding outside the allowance fail the run. A missing
  allowance file counts as empty.
- **The self-test** runs the tool over `harness/testdata/early_exit/`.
  - Each fixture names its compiler flags on a first line,
    `// flags: ...`.
  - Each line that should be reported carries `// expect: Rn`, with more
    than one rule separated by commas.
  - A missing finding, or one that was not expected, fails the self-test.
- **STXlib's census.** STXlib's census already runs the pinned clang per
  unit with the tree's flags. It can run this driver over the same compile
  database instead of passing JSON ASTs, since the tool reads each unit
  itself, with the unit's own exception mode.

## 4. What it cannot prove (stays for review)

- **That a statement is true.** The check verifies that a `Whole`, `NoExit`
  or `whole` statement is present and consistent with the live locks and
  open objects. It does not verify that the invariants really are whole, or
  that the callee really cannot exit.
- **What a guard restores.** A guard is checked for its class mark and its
  liveness, not for what its destructor does.
- **Invariants across functions.** A pair whose halves sit in different
  functions (P11), and an invariant that the code splits over members and
  never restores, are invisible. State is matched by its spelling, so the
  same state reached through two spellings is not matched.
- **Indirect reach.** A first-party function that calls foreign code is
  not foreign to its callers unless it is marked. R6 sees the direct
  callees of a no-unwind function, and the `noexcept` on a project callee,
  not what that callee calls in turn. Vendored allocators are audited by
  hand (STXlib's smi seat).
- **longjmp, never-returning callbacks and forced unwinds** are only as
  covered as the exit classes say. `cancellation_points` must name every
  cancellation point the code reaches.
- **Lambdas and blocks** are not analysed, since house style forbids
  lambdas. Uninstantiated templates are unchecked.
- **Implicit calls** (constructors, destructors, conversions) are not
  classified.
- **R9 on paths the check cannot see.** A release reached from an atexit
  handler, a static destructor or the fail-fast path is not traced.
- **Temporary files** created and renamed or unlinked later (R5's last
  pair, P5) are not recognised.
