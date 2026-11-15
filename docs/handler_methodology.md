# Dispatch throws and use after a handler: the methodology

Status: design proposal for the owner and the coordinator, 2026-11-15.
Nothing here is built yet. It extends the early-exit check
(`docs/early_exit_check.md`, `harness/early_exit/`) and uses its vocabulary
style, its driver and its self-test conventions.

The 2026-11-15 audit of GUI.Forms found two repeated mistakes behind 4 of
its 12 High findings and about 30 Medium and Low ones. The owner's reading:
"the dispatch throw and handler use-after are the only ones that need a
methodology". The other repeated patterns (a decoder trusting its input,
quadratic work, state released at exit) are fixed case by case or already
have a rule and a check.

This document proposes, for each of the two mistakes:
- the rule, worded for the house style (section 7 has the proposed text for
  house style section 5);
- a static check: new rules in the early-exit tool, named D1 to D3 and H1
  to H3, with the annotations they read;
- a test harness that exercises the same mistake at run time;
- fixtures taken from the audit's own findings, in
  `harness/testdata/handler_rules/`.

## 1. The two mistakes

### 1.1 A throw inside dispatch

GUI.Forms delivers host events (input, paint, focus, close) and UI timer
callbacks through STXlib's fail-fast gate:

- `HostSession::dispatch` calls `call_failing_fast("host_session.dispatch",
  &HostSession::deliver, ...)` (`src/core/host/session/host_session.cpp`);
- `WindowScheduler` calls `call_failing_fast("scheduler.ui_timer",
  &run_ui_timer, ...)` (`src/core/window/scheduler/window_scheduler.cpp`);
- the motion clock calls `call_failing_fast("motion_clock.listener", ...)`.

The gate exists so that an application's exception never unwinds into
AppKit, Win32, X11 or Wayland frames: it ends the process with a report.
That is right for an application's own exception. It is wrong for ours: when
GUI.Forms code expresses an ordinary refusal as a throw, an ordinary gesture
ends the program.

Audit instances:

| Finding | What threw | Reached by |
|---|---|---|
| A-gf-controls-12-F1 (High) | `SplitContainer::set_distance` refuses a negative distance with `std::invalid_argument` | Shift+Left with the seam below 40 |
| A-gf-controls-11-F8 (High) | a bound `Value` refuses a candidate (`RangeControl::validate_bound_value`, `std::out_of_range`) | dragging or wheeling one of two range controls bound to one value |
| A-gf-core-10-F1 (High) | the glyph run rasterizer, `std::length_error` past 16,384 device pixels | painting any long one-line text |
| A-gf-controls-06-F3 | `MenuStrip::open` for a menu whose commands are all hidden | a key or click on that menu |
| A-gf-controls-12-F2, F3 | `ResponsiveTrackPanel` and `CommandOverflowPanel` measure | an unbounded extent, more than 32 children |
| A-gf-controls-13-F1 | `TaskDialog::set_page` half built | a standard button id |
| A-gf-controls-14-F1 | the exception report dialog, invalid UTF-8 | an exception message that is not UTF-8 |
| A-gf-core-17-F3 | the 256th scheduled frame request | pointer dispatch, the band, the tooltip wake |

### 1.2 Use after a handler

An index, iterator, pointer or reference into a control's mutable state is
taken, then a call runs application handlers, then the value is used
without checking that it still names what it named. Handlers run
synchronously inside `request_focus` (validating, validated, lost focus,
got focus), inside every `Event::emit`, inside the `raise_*` helpers, and
inside every C ABI client callback. A handler may shrink a list, rebuild a
model, replace columns, close a menu, remove the control from its window,
or destroy the control or the window.

Audit instances:

| Finding | Taken before | The call | Used after |
|---|---|---|---|
| A-gf-controls-08-F1 (High) | `hit.index` from the layout | `request_focus` | `selected_[hit.index]`, `columns_[hit.index]` |
| A-gf-controls-06-F1 (High) | `this` (the ContextMenu and its Impl) | the item's command, `item_invoked_.emit` | `owner.is_alive()`, `close()` |
| A-gf-controls-08-F2 | `index`, checked against `check_states_.size()` | `item_checking` emit | `check_states_[index]` |
| A-gf-controls-10-F1 | TreeListView's hit index | `request_focus` | the rows |
| A-gf-controls-02-F6 | DataGridView's row position | its handlers | the rows |
| A-gf-controls-09-F1 | PropertyGrid's row maps | `edit_failed` handler | the maps |
| A-gf-controls-14-F3 | VirtualRowsView's row | `request_focus` | the selection |
| A-gf-core-01-F1 | `Binding::update_control`'s target | the format handlers | the target |
| A-gf-core-17-F1 | `SurfaceRoot` (`this`) | hover raisers, deferred input, drop | the window's fields |
| A-gf-core-17-F2 | the region count | focus handlers | the region list |
| A-gf-abi-03-F1 | the window pointer | `windows.request_help` callbacks | the window |
| A-gf-abi-03-F3 | `attached_surface_root()` | `request_focus` | `(*window).focused_control()` |
| A-gf-controls-01-F1 | the parent's live child list | radio group handlers | the iteration |

## 2. The rules

### Rule D (dispatch): refusals are results on dispatch paths

> Code reachable from input, paint or timer dispatch does not throw. A
> refusal there is a result: a `bool`, an enumeration, a `std::optional` or
> a status, and the caller decides what the user sees. A value that comes
> from a gesture, a model or a file is clamped or refused as "no change".

Details:
- **Which code.** Every function reachable from a fail-fast gate's target
  (section 3.2), except through an application handler (`Event::emit`, a
  `std::function` the application supplied, a C ABI client callback): an
  application's exception is the application's and ends the process by
  design.
- **API functions that throw on misuse** (a negative distance passed by the
  application) keep throwing when the application calls them. Dispatch code
  does not call them with values it has not proved valid: it clamps first,
  or it calls a non-throwing form (`set_distance` clamps for the user-input
  reasons; `set_value_from_input` returns false on a refusal).
- **Allocation failure** may still reach the gate. It ends the process by
  policy (allocators do not unwind), so `std::bad_alloc` from the standard
  library is not a finding.
- **Paint draws nothing rather than refusing.** A part that cannot be drawn
  is left undrawn and counted (as the glyph run's `oversized` result is
  counted). Paint never throws for content.

### Rule H (handlers): revalidate after a call that runs handlers

> After a call that can run application handlers, nothing taken before it
> from mutable control state is used until it has been revalidated: an
> index is checked against the container again (or looked up again by
> identity), an iterator or pointer is taken again, and the object itself
> is known to be alive through a strong reference or a lifetime token held
> across the call.

Details:
- **Calls that run handlers** are marked (section 3.1) and the mark is
  propagated: a function that calls one runs handlers too.
- **Revalidation forms the check accepts** (section 3.3): taking the value
  again after the call; comparing an index with the size of the container
  it indexes, after the call, on a path that leaves when it fails; a call
  to a function marked `handler.revalidates` that takes the value; a
  `Revalidated` site marker naming the value, with a reason.
- **The object itself.** A member access after the call needs `this` to be
  alive. Accepted proofs: a strong reference to `this` or to its owner
  held in a local across the call (a `Control::Ptr`, a `shared_ptr`, a
  retain), or a lifetime token taken before the call and tested after it
  (`SurfaceRoot`'s `lifetime_`, whose record outlives the window). A test of
  `is_alive()` on the object itself is not a proof: it reads the object
  that may be gone.
- **Prefer order over checks.** Where possible, run the handler-running
  call first and take the index after it (ListView: request focus, then
  hit-test). The check accepts that form without any marker.

## 3. The static check

### 3.1 Vocabulary

Declaration marks, read like the early-exit marks (any declaration, or the
template pattern):

| Mark | On | Meaning |
|---|---|---|
| `handler.dispatch_entry` | a function | a dispatch root, in addition to the gate targets found automatically |
| `handler.runs_handlers` | a function | calls to it run application handlers (seed of H1 to H3) |
| `handler.application` | a function, a function-pointer type, a class | calls through it are application code: D rules stop there; H rules treat the call as running handlers |
| `handler.no_handlers` | a function | it never runs handlers, whatever it calls (a documented exception to propagation, listed for review) |
| `handler.revalidates` | a function | a call taking a value revalidates it (H1) |
| `handler.keep_alive` | a class | a local of it held across a call keeps its referent alive (H2) |
| `handler.lifetime` | a class | a local of it taken before the call and tested after keeps `this` safe to read (H2) |
| `handler.refusal_throw` | a function | it throws as its documented refusal; reaching it from dispatch is D2 unless a converting `catch` covers the call |

Site markers, matched by the last two name components as the early-exit
markers are (each project keeps the types in its own namespace):
- `handler::Revalidated{value, "reason"}`: the next statement may use
  `value` after a handler-running call, for the reason given (review list);
- `handler::ThrowAccepted{"reason"}`: the next statement's throw or
  throwing call on a dispatch path is accepted (review list). Expected to be
  rare: allocation-sized refusals of absurd requests.

Project settings (`[handler_rules]` in the project file, with `_extra`
lists as in `[early_exit]`):
- `gates`: regular expressions over the qualified names of fail-fast gates;
  the function a gate call receives as its target is a dispatch root.
  GUI.Forms: `^gui_forms::detail::call_failing_fast$`.
- `runs_handlers`: names that run handlers, until the declarations carry
  the mark. GUI.Forms: `^gui_forms::Event::emit$`,
  `^gui_forms::SurfaceRoot::request_focus$`, `^gui_forms::.*::raise_.*$`,
  `^gui_forms::Control::emit$`.
- `application_calls`: names that are application code: GUI.Forms
  `^gui_forms::Event::emit$`, `^std::function::operator\(\)$` (when the
  function was supplied through a public API), and every early-exit foreign
  call through a C ABI client pointer.
- `throwing_library`: standard library members that throw on ordinary
  input and so count as throw sites for D: `^std::vector::at$`,
  `^std::basic_string::at$`, `^std::map::at$`, `^std::unordered_map::at$`,
  `^std::optional::value$`, `^std::sto.*$`, `^std::variant::get$`
  (through `std::get`), `^std::function::operator\(\)$` on an empty function.
  Allocation (`operator new`, container growth) is not listed (rule D).

### 3.2 Dispatch rules (D)

The tool already reads every function body in one walk (BodyWalker). The
dispatch rules add per-function facts and a whole-program step in the
driver, because reachability crosses translation units.

Facts the tool emits for each first-party function (a new record kind,
`"kind":"facts"`, in the same JSON lines):
- `throws`: each `throw` expression not caught inside the function by a
  handler that does not rethrow, with its line;
- `library_throws`: each call to a `throwing_library` name, likewise not
  caught locally;
- `calls`: each direct callee (qualified name and mangled name), each
  virtual call (the method and its class), and each call through a
  function pointer with its type, and whether the call site is inside a
  `try` whose handlers catch the thrown type without rethrowing (a
  converting catch);
- `gate_targets`: functions named as targets in calls to `gates`;
- `marks`: the handler marks on the function;
- `overrides`: for each virtual method, the methods it overrides, so the
  driver can resolve a virtual call to every first-party override.

The driver (`harness/check_handler_rules.py`, the sibling of
`check_early_exit.py`) joins the facts of all units:
1. Roots: every gate target and every `handler.dispatch_entry` function.
2. The call graph: direct calls by mangled name; a virtual call to every
   first-party override of the method (class hierarchy analysis); a call
   through a function pointer to every first-party function whose address
   is taken with that type (an over-approximation, listed as such). Calls
   marked `handler.application` or in `application_calls` are not followed.
   Calls inside a converting catch are followed but their throws do not
   escape.
3. A throw summary per function, by a fixed point over the graph: a
   function throws when it has a `throws` or `library_throws` entry not
   covered locally, or calls a throwing function outside a converting
   catch.
4. Findings, each with the shortest path from a root printed as the
   explanation (`deliver -> on_key -> on_key_preview -> set_distance`):

| Rule | The driver reports |
|---|---|
| D1 | A `throw` expression in a function reachable from a root, not caught on every path back to the root. |
| D2 | A call, in a reachable function, to a `throwing_library` name or a `handler.refusal_throw` function, not inside a converting catch. |
| D3 | A reachable function that is `noexcept` and calls a throwing function (the throw becomes `std::terminate` in our frame). Report-only. |

A `ThrowAccepted` marker covers the next statement and puts it on the
review list. The allowance file is shrink-only, as for the early-exit
check, so the existing findings can be listed once and only go down.

### 3.3 Handler rules (H)

These are per function and need no whole-program step except the
propagation of "runs handlers", which the driver computes from the same
call graph (a function runs handlers when it is marked, listed, an
application call, or calls one) and feeds back to the tool as a list of
mangled names (`--runs-handlers <file>`; a second pass, cheap because the
first pass's facts are cached).

In one function body, in source order, BodyWalker keeps, for each local:
- whether it is **derived from member state**: its initializer reads a
  member of `this` (directly or through a const member function of the
  same class), is an iterator, pointer or reference into a member, or is
  a raw pointer returned by a member function of a member or of a
  parameter object (`attached_surface_root()`); or it is a parameter that
  was compared with the size of a member container before the call (a
  validated index, as in `CheckedListBox::set_item_check_state`);
- the last handler-running call after its derivation, and whether a
  revalidation form (section 2, rule H) followed that call.

| Rule | The tool reports |
|---|---|
| H1 | A use of a member-derived local after a handler-running call, with no revalidation between the call and the use. Uses are reads of the local's value (indexing, dereference, comparison, passing it on), not its reassignment. |
| H2 | A member access through `this` (explicit or implicit), or a call of a member function on `this`, after a handler-running call, when no `handler.keep_alive` local holding `this` or its owner and no tested `handler.lifetime` local was live across the call. A function marked `handler.self_stable` (the class documents that its handlers cannot destroy it, for example because the window holds it strongly for the dispatch) is exempt and listed for review. |
| H3 | A range-for or iterator loop over a container that is a member of `this` or of a parameter object, whose body makes a handler-running call (A-gf-controls-01-F1: clearing a radio group while iterating the parent's live child list). |

Revalidation recognised for H1, between the call and the use:
- the local is assigned again;
- an `if` whose condition compares the local with `container.size()` (or
  `count()`, or a `*_count()` member) of the container the local indexes,
  whose failing branch leaves the function or the loop;
- a call to a `handler.revalidates` function with the local as an
  argument, whose result is tested;
- a `Revalidated` site marker naming the local.

H2's lifetime proof recognises a `handler.lifetime` local declared before
the call and named in an `if` condition after it whose failing branch
leaves the function, before the access.

### 3.4 What the static check cannot prove

- **Function-pointer reach** is over-approximated by type; virtual reach by
  the first-party class hierarchy. A throw reached only through an
  `std::function` the library stored for itself (not the application's) is
  not seen unless that function's target is marked or listed.
- **Value ranges.** D cannot tell that a clamp before `set_distance` makes
  its throw unreachable. The fix is to make the throwing callee non-throwing
  for the input paths (rule D), or a `ThrowAccepted` marker with the
  reason. The check prefers a false finding to a missed one.
- **Revalidation's truth.** H1 checks that a revalidation form is present,
  not that it compares the right container: a check against another
  container of the same size passes.
- **Aliasing.** A member reached through two spellings (a reference held
  in a local and the member) is matched by the spelling the local was
  derived from.
- **Lambdas** are not analysed (house style forbids them), as in the
  early-exit check.

## 4. The fixtures

`harness/testdata/handler_rules/` follows the early-exit conventions: a
first line `// flags: ...`, and `// expect: <rules>` on each line that must
be reported. Each fixture reduces one audit finding to the shape the check
must see, with the bad case first and the fixed case after it (the fixed
case carries no expectation, and its function's name ends in `_good` or
`_from_input`, or begins with `try_`). `harness/test_handler_rules.py` checks that
the fixtures are well formed today (known rule names, a bad and a good
function in each, every expectation inside a function); when the tool is
built, its self-test runs them.

| Fixture | Finding | Rules |
|---|---|---|
| `d1_split_container.cpp` | A-gf-controls-12-F1 | D1 through a key handler; good: clamped user-input form |
| `d2_range_binding.cpp` | A-gf-controls-11-F8 | D1 through a bound value's refusal; good: refusal as a result |
| `d3_glyph_run.cpp` | A-gf-core-10-F1 | D1 from paint; good: an oversized result counted |
| `d4_library_throw.cpp` | A-gf-controls-08-F1's `ensure_visible` (`std::out_of_range`) | D2 through `std::vector::at`; good: checked index |
| `h1_list_view.cpp` | A-gf-controls-08-F1 | H1; good: focus first, then hit-test |
| `h1_checked_list_box.cpp` | A-gf-controls-08-F2 | H1 on a validated parameter; good: size check after the emit |
| `h1_abi_focus.cpp` | A-gf-abi-03-F3 | H1 on a raw window pointer; good: taken again after |
| `h2_context_menu.cpp` | A-gf-controls-06-F1 | H2; good: a keep-alive held across the command |
| `h2_window_hover.cpp` | A-gf-core-17-F1 | H2; good: the lifetime token tested |
| `h3_radio_group.cpp` | A-gf-controls-01-F1 | H3; good: iterate a copy |

## 5. The run-time harnesses

The static check finds the shapes; the harnesses find what the shapes
miss (a throw behind a function pointer, a revalidation against the wrong
container). Both are GUI.Forms tests, sketched here for the coordinator to
assign.

### 5.1 Dispatch boundary harness (`tests/dispatch_boundary/`)

Pushes boundary inputs through the real dispatch: `SurfaceRoot`'s
`dispatch_*` entry points behind a `HostSession` in the headless host, so
the fail-fast gate is the one production uses.

- **Subjects:** every stock control the registry lists (the same list the
  stock-control conformance test walks), each in a window, focused.
- **Configurations at their edges:** minimum and maximum sizes, zero and
  one-pixel extents, an unbounded measure, ranges with `minimum ==
  maximum`, a value at each end, a SplitContainer at its first minimum,
  two range controls bound to one value with different ranges, 0, 1, 32
  and 33 children, all commands hidden, text of 0 bytes, one byte, a
  50,000-byte line, invalid UTF-8 through each text-taking API.
- **Inputs:** every navigation and editing key with each modifier set
  (Shift, Control, Alt, Command), held as repeats; wheel deltas of ±1 and
  the largest the host protocol carries; pointer presses, drags and
  releases at each edge, one pixel outside, and far outside; a drag
  released outside the window; double and triple clicks; the repeat timer
  of each control run for 300 ticks; a paint at scales 1, 1.5, 2 and 3.
- **Oracle:** the process must not end. Each control's script runs in a
  child process (the test spawns itself with `--subject <name>
  --configuration <n>`), so a fail-fast ending is recorded as a finding
  with its report (`site`, exception type, message) and the next subject
  runs. No test seam in the gate is needed.
- **Budget:** one subject and configuration per child; the whole run is
  bounded by the subject list times the configuration list, about 2,000
  children, sharded by CTest.

### 5.2 Hostile-handler harness (`tests/hostile_handlers/`)

Subscribes a handler to every event a control raises, and has it do
something destructive at a chosen moment. Runs under ASan and UBSan (the
sanitizer tree), where a use after free or an out-of-range write is a
report, and with invariant checks after each step.

- **Actions** a hostile handler performs, one per run:
  1. remove the item the input addressed;
  2. remove every item (`set_items({})`, `refresh_virtual_items(0)`);
  3. replace the model with a different count (one fewer, one more, ten
     fewer);
  4. replace the columns;
  5. close the control's menu, drop-down or overlay;
  6. remove the control from its parent and drop the last strong reference
     (the harness holds the control only weakly);
  7. close and destroy the window (the harness holds it only weakly);
  8. through the C ABI: dispose the window or the control from a client
     callback.
- **Moments:** each event the control raises during an input script
  (validating, validated, lost and got focus, selection and check changes,
  item invoked, command executed, value changed, hover enter and leave,
  drop), counted by an observer on a dry run, so a run is (subject, script,
  event ordinal, action).
- **Oracle:** no sanitizer report; the process does not end; after each
  step, the control's invariants hold (`focused_index < count`, selection
  size equals item count, anchors in range, no row painted past the model);
  a destroyed control or window is not touched (ASan proves it).
- **Size:** subjects times scripts times events times 8 actions; the
  dry-run counts bound it before the run starts. Sharded like 5.1.

## 6. Rollout

1. Land the vocabulary header in GUI.Forms (`include/gui_forms/detail/`
   or a private header) and mark the seeds: `Event::emit`,
   `SurfaceRoot::request_focus`, the `raise_*` helpers, the C ABI callback
   invokers, `call_failing_fast` as a gate.
2. Build the facts pass into `early_exit_check` (one tool, one compile
   database read) and the join in `check_handler_rules.py`; run the
   fixtures' self-test.
3. Run over GUI.Forms with `--write-allowance`; the audit's findings should
   all appear (a missing one is a check defect to fix before trusting it).
4. Fix the instances; the allowance only shrinks. The `house-style` CI job
   gains the check once the allowance is empty for D1 and H1.
5. The two harnesses land with the first fixes and run in the sanitizer
   tree on every stage gate.

## 7. Proposed addition to house style section 5 (callbacks and execution)

> **Dispatch does not throw.** Input, paint and timer dispatch run inside
> the fail-fast boundary, where any exception ends the process. On a path
> reachable from dispatch, a refusal is a result (`bool`, enumeration,
> `std::optional`, status), a value from a gesture, a model or a file is
> clamped or refused as no change, and paint leaves what it cannot draw
> undrawn and counts it. Functions that throw on misuse are called from
> dispatch only with values proved valid, or through a non-throwing form.
> Allocation failure is the one exception that may reach the boundary.
>
> **Revalidate after handlers.** A call that can run application handlers
> (an event emission, a focus request, a raise helper, a client callback)
> may change any state the application can reach, including destroying the
> caller. An index, iterator, pointer or reference taken from mutable state
> before such a call is taken again, or checked again against its
> container, before it is used after the call; prefer making the call
> first and taking the value after it. The object itself is kept alive
> across the call by a strong reference or checked through a lifetime token
> that outlives it; never by asking the possibly destroyed object whether
> it is alive. Do not iterate a container whose owner's handlers run inside
> the loop; iterate a copy of the identities and look each one up again.
