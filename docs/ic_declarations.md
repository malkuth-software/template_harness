# Behavioural constraint declarations (IC)

`harness/ic_declarations.py` reads one declaration file per component and
generates everything else that states the component's constraints. Each
behavioural constraint is written once, beside the header: what a call
requires, what it does when that does not hold and what it leaves behind,
the object's states and the calls each state admits, the thread a call may
run on, and who owns what a call returns or takes. Four generators then
write the forms other tools and readers use:

| Output | Read by |
|---|---|
| contract lines in the header, `// ic: ...`, above each declared call | readers of the header |
| an entry-check header: one predicate and one message per `requires` clause | the code, which calls the predicates, so the stated condition and the check are one text |
| a member-manifest column, `constraints`, one row per call | the member manifest (GUI.Forms' `manifests/members.json`) and reviews |
| fuzz block stubs (JSON), one legal block per call and admitted state, one misuse block per refusal | the block fuzzer (phase IC2) |
| proof obligations (JSON), one per clause, open until discharged | the deduction layer and the proof report (phase IC4) |

The design is the IC study's (notes, `gui_forms` branch,
`planning/stages/IC-integrity-constraints/STUDY.md`, sections 6 to 9,
decided by the owner on 2026-11-22). This is phase IC1: the format, its
parser and the generators. The engine that runs the blocks is IC2.

What it is not: a proof. `check` proves only that the generated outputs
match the declarations and that every generated predicate is called; whether
the code does what a clause says is for the tests, the fuzzer and, where it
pays, a deduction (section 6 of the study).

## 1. Commands

```
python3 -B harness/ic_declarations.py generate --config P.toml [--root DIR]
python3 -B harness/ic_declarations.py check    --config P.toml [--root DIR]
python3 -B harness/ic_declarations.py list     --config P.toml [--root DIR]
```

`--root` is the repository root (default: the current directory); every
path in the project file and in a declaration is relative to it.
`generate` writes every output; `check` writes nothing and reports. Exit
status: 0 everything current; 1 findings (`STALE <path>`: an output differs
from what the declarations generate; `UNUSED <path>: <predicate>`: a
generated predicate no listed source calls); 2 a configuration or
declaration error (`ERROR <path>: <message>`). The last line is `SUMMARY`.

## 2. The project file

```toml
schema = 1
declarations = ["include/stxlib/*.ic.toml"]  # globs, relative to the root
manifest_column = "tools/ic/members_constraints.json"
blocks = "tests/ic/blocks"                    # <id>.blocks.json per component
obligations = "proofs/ic"                     # <id>.obligations.json per component
generator = "tools/ic_declarations.py"        # how generated text names its generator
column_limit = 120                            # width of the header contract lines
```

A component's `<id>` is its declaration's file name without `.ic.toml`.

## 3. The declaration

One TOML file per component, named `<id>.ic.toml`, beside its header. Every
table and key below is checked; an unknown key is an error, so a misspelt
clause cannot pass silently.

```toml
schema = 1
component = "File"                    # the C++ name the header declares
namespace = "stxlib"
header = "include/stxlib/files.hpp"
anchor = "class File final {"         # the component's contract lines go above it
keys = { manifest = "stxlib::File" }  # registry, manifest, ABI and surface names
thread = "owner"                      # the calls' default thread
states = ["closed", "reading", "writing", "appending"]
initial = "closed"
state_query = { call = "is_open", observes = { closed = "false", reading = "true", writing = "true", appending = "true" } }
identity = { kind = "handle", stale = "failure:disposed:closed_handle" }

[terms]                               # the object's own values a clause may name
count = "usize"

[constants]                           # named limits a clause may name
largest_offset = { value = "2^63 - 1", type = "u64" }   # emitted in the checks header
alignment_limit = { cpp = "alignment_limit", type = "usize" }  # an existing C++ name

[checks]                              # optional: the entry-check header
path = "src/core/files_ic_checks.hpp"
namespace = "stxlib::detail::ic::files"
includes = ["stxlib/types.hpp"]
used_in = ["src/core/files.cpp"]      # where the predicates must be called (default: the header)

[[call]]
name = "read_at"                      # the member or function name
id = "read_at"                        # default: name; must be a C++ identifier
declarations = ["[[nodiscard]] OperationResult<usize> read_at(u64 offset, MutableView<Byte> output) noexcept;"]
kind = "member"                       # member, static, constructor or free
thread = "owner"                      # default: the component's
in = ["reading", "writing", "appending"]   # admitting states (default: all)
outside = "failure:disposed:closed_handle" # or a table: state -> refusal
moves = "closed"                      # the state after success (default: unchanged)
parameters = { offset = "u64" }
requires = [
  { holds = "offset <= largest_offset", refuses = "failure:limit_exceeded:too_long", check = "read_offset", leaves = "unchanged" },
]
fails = [{ failure = "io_failure", when = "the host refuses the read", leaves = "unchanged" }]
returns = { type = "usize" }          # or owns = "caller", state = "...", borrows = "self", until = [...], revokes = "..."
transfers = [{ what = "address", from = "caller", to = "allocator" }]

[[obligation]]                        # hand-stated obligations (the deduction layer's)
id = "page_protocol"
statement = "..."
method = "..."
premises = ["..."]
```

**Keys.** `keys.manifest` (default `namespace::component`) is the type's
name in the member manifest; `abi`, `registry` and `surface` name it in the
C ABI tables, the control registry and an object surface component file.

**Declarations.** Each entry must match the start of exactly one line of the
header (leading space ignored); the first of a call's entries carries its
contract lines, the others (overloads with the same contract) are only
checked to exist. Contract lines go directly above the matched line, or
above a `template <...>` line just before it.

**States.** `in`, `outside` and `moves` need `states`; `outside` must cover
every state not in `in`, and nothing else. A `free` call has no states.
`state_query` names the public query that observes the state, and what it
returns in each; a state no query tells apart is still declared (the study's
IC3 adds the missing queries).

**Refusals** (`refuses`, `outside`, `identity.stale`) are written as:

| Text | Meaning |
|---|---|
| `requirement` | a requirement of the caller: the process stops with `message` (STXlib's `STXLIB_REQUIRE`); `message` is required |
| `failure:<kind>` or `failure:<kind>:<code>` | a failure result of that kind and host code; `failure:any` where the kind is the host's |
| `null` | a null result |
| `value:<C++ value>` | that value returned, such as `value:Release::refused` |

`leaves` says what a refusal or failure leaves behind: `unchanged` (the
default), `partial` or `unspecified`.

**Threads.** `any`, `owner` (whichever one thread holds the object at a
time), `ui` or `dispatcher`.

## 4. Clauses

A `holds` clause is a condition over the call's parameters, the object's
terms and the constants:

```
condition  := disjunct ('or' disjunct)*
disjunct   := negation ('and' negation)*
negation   := 'not' negation | relation
relation   := sum (('<' | '<=' | '>' | '>=' | '==' | '!=') sum)?
            | sum 'in' ('[' | '(') sum ',' sum (']' | ')')
            | predicate '(' sum ')'
sum        := product (('+' | '-') product)*
product    := factor ('*' factor)*
factor     := number | number '^' number | name | '(' condition ')'
predicate  := power_of_two | not_null | finite
```

A name is an identifier, optionally with members (`bounds.width`); its first
part must be a parameter, a term or a constant. Numbers are decimal or `0x`
hexadecimal; `2^63` is a power. Conditions nest at most 32 deep. In C++,
`and`, `or` and `not` become `&&`, `||` and `!`, both operands of an `and`
are evaluated left to right with short-circuit, so `first <= count and
length <= count - first` never subtracts past zero.

Each clause becomes one predicate in the checks header,
`admits_<check>(names...)`, whose parameters are the clause's names: the
call's parameters first, in the order `parameters` declares them (write it in
the C++ signature's order, so a call site's arguments read like the call's),
then the terms; constants are not parameters. A requirement also gets
`<check>_message`. `check`
defaults to the call's `id` when the call has one clause; a call with
several clauses names each.

## 5. Outputs

**Contract lines.** `generate` removes every line of the header that starts
with `// ic:` and writes the current ones; a hand comment never starts that
way. The component's lines go above `anchor`: the generator, the states and
their query, the default thread, the identity. Each call's lines say, in
order: the admitting states and the state after success, what a call
outside them does, each clause and its refusal, the failures, a thread other
than the default, what is returned and who owns it, and what is transferred.

**The checks header.** `#pragma once`, the includes, then in `namespace`
the constants written as values and one `[[nodiscard]] constexpr bool
admits_<check>(...) noexcept` per clause, with its message beside it. No
`std::` name; usable from a public header.

**The member-manifest column.** One file per project: `{"schema": 1,
"column": "constraints", "rows": [...]}`, each row `type` (the manifest key),
`member`, `constraints` (the call's contract in one line) and
`declaration`, sorted by type and member. A manifest merges it by type and
member.

**Fuzz block stubs.** Per component: the typed inputs of each block (the
object in its state, the parameters with the clauses that bound them), the
clauses, the state after success, the declared failures and, for misuse
blocks, the violated clause or the state outside `in` and the refusal and
leftovers expected. IC2 writes the block bodies; the stubs say what they
must do.

**Proof obligations.** Per component: each clause as an entry-check
obligation (and, where a refusal leaves the object unchanged, a second
one), each transition and each typestate refusal, then the hand-stated
obligations. Each starts at `level` `none` and `status` `open`, with `tool`
null and no premises, in the record shape of STXlib's verification
programme (section 6: level, tool, premises, source hash); the file records
the declaration's SHA-256. A deduction (IC4) discharges them; the method
for counters and bit sets is sxv's (finite sums with three elementary laws,
popcount by its algebra; the allocator study of 2026-11-18, credited in
STXlib's notes), reused, not copied.

## 6. Adopting it

1. Write the project file and a declaration beside each component's header.
2. Run `generate`, call each `admits_<check>` where the code refuses (with
   `<check>_message` for a requirement), and commit the outputs.
3. Run `check` in CI (a test or a gate), so a declaration and its outputs
   cannot drift apart and no predicate goes uncalled.

STXlib is the first adopter: its allocator, containers and files
(`tools/ic/project.toml` there).
