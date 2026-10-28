"""Branches inside loops, sorted by the house style's own repair (Phase E, E2).

Every conditional inside a loop body is a candidate: an `if` (including a
conditional break, continue or return), a `switch`, a `?:`, and a `&&` or
`||` that stands outside those conditions. Each is classed by what its
condition reads, against the innermost loop around it (house style,
Repeated kernels):

  invariant    reads nothing the loop changes: choose the mode and the
               implementation before the loop.
  edge         reads only the loop's index, compared with fixed bounds
               (index == 0, index + 1 == count): handle the edges apart from
               the interior. A parity or stride test of the index
               (index % 2 == 0) is edge too, but its repair is to unroll or
               split the loop by the stride, not to peel; the reason says
               which.
  validation   reads each element and leaves with a failure (throw, or a
               return of false, null, a negative number or a name of
               failure): validate the whole buffer up front.
  control      reads each element and ends the loop's work (break, goto, or
               a return of a found value): a search or early exit is the
               loop's purpose; usually kept, listed for completeness.
  data         reads each element and stays in the loop: review it (keep,
               make it branch-free, or split the data).

What the loop changes is read from the AST: a variable or field assigned,
incremented or declared inside the loop (its condition, increment and body),
an object whose non-const member function the loop calls, and a variable
passed by non-const reference. A call in a condition counts as changing
unless it is a known observer (size, empty, data, ...) of an unchanged
object, so a condition that calls a helper is never classed invariant on
trust. Conditions inside called inline helpers are not followed yet.

The run reads every first-party translation unit of a configured ninja
tree's compile database through the shared AST reader (clang_ast.py, with
libclang or the pinned clang's JSON dump), keeps the first-party
declarations, and analyses each header's declarations once per worker
process. Findings are deduplicated by file and offset.

Decisions a person records sit beside the findings in a JSON file (the
project's [loop_branches] decisions, a path in the product checkout, or
--decisions): each matches a finding by path, function and condition text,
so editing the condition raises it again. The review queue (--json) holds
every finding with its class, reason and decision; --report writes a short
summary. The run never fails on findings; it is a review aid.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys

import check_state_machines as hunt
import clang_ast
from clang_ast import Node
import project

LOOPS: tuple[str, ...] = ("ForStmt", "WhileStmt", "DoStmt", "CXXForRangeStmt")
LOOP_NAMES: dict[str, str] = {"ForStmt": "for", "WhileStmt": "while", "DoStmt": "do", "CXXForRangeStmt": "range-for"}
VARIABLES: tuple[str, ...] = ("VarDecl", "ParmVarDecl", "BindingDecl")
CLASSES: tuple[str, ...] = ("invariant", "edge", "validation", "control", "data")
DECISIONS: tuple[str, ...] = ("keep", "hoist", "peel", "validate_up_front", "branch_free", "split", "false_shape")
# Member functions that only observe their object.
OBSERVERS: frozenset[str] = frozenset({"size", "empty", "data", "length", "capacity", "begin", "end", "cbegin",
                                       "cend", "has_value", "front", "back", "count", "size_bytes"})
COMPARISONS: frozenset[str] = frozenset({"==", "!=", "<", ">", "<=", ">="})
ASSIGNMENTS: frozenset[str] = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="})
STEPS: frozenset[str] = frozenset({"++", "--"})
# Overloaded operators (by the reader's opcode) that compare, and that reach an element.
COMPARING_OPERATORS: frozenset[str] = frozenset({"==", "!=", "<", ">", "<=", ">=", "!"})
ELEMENT_OPERATORS: frozenset[str] = frozenset({"[]", "->"})
FAILURE_NAME: re.Pattern[str] = re.compile(
    r"(?i)error|invalid|fail|refus|reject|corrupt|malformed|bad|overflow|exhaust|too_|unsupported|stale|"
    r"truncat|mismatch|unknown|out_of")


@dataclass(frozen=True)
class Finding:
    path: str               # checkout-relative
    line: int
    offset: int             # byte offset of the conditional, for deduplication and tools
    function: str
    loop_line: int
    loop: str               # for, while, do, range-for
    branch: str             # if, switch, conditional, and, or
    cls: str                # one of CLASSES
    condition: str          # the condition's source text, whitespace collapsed
    reason: str
    in_macro: bool


@dataclass(frozen=True)
class Decision:
    path: str
    function: str
    condition: str
    decision: str
    reason: str


@dataclass
class Loop:
    """A loop being walked: what it changes, and its induction variables."""
    node: Node
    line: int
    kind: str
    changed: set[str]           # declaration identities changed anywhere in the loop
    changed_fields: set[str]    # field names changed through `this`
    induction: set[str]         # identities changed in the loop's increment, or a while loop's index


# --- what a loop changes ---------------------------------------------------------

def referenced_id(node: Node) -> str:
    """The variable a DeclRefExpr names (its unit-local identity), or ""."""
    if node.kind != "DeclRefExpr" or node.target_kind not in VARIABLES:
        return ""
    return node.target


def this_field(node: Node) -> str:
    """The field a MemberExpr reaches through `this` (written or implicit), or ""."""
    if node.kind != "MemberExpr" or "object" not in node.roles:
        return ""
    if clang_ast.stripped(node.roles["object"]).kind != "CXXThisExpr":
        return ""
    return node.name


def root_object(node: Node) -> Node:
    """The variable or `this` field an lvalue expression reaches: a.b[i].c gives a."""
    node = clang_ast.stripped(node)
    while True:
        if node.kind == "MemberExpr" and not this_field(node) and "object" in node.roles:
            node = clang_ast.stripped(node.roles["object"])
        elif node.kind == "ArraySubscriptExpr" and "base" in node.roles:
            node = clang_ast.stripped(node.roles["base"])
        elif node.kind == "UnaryOperator" and node.opcode == "*" and "operand" in node.roles:
            node = clang_ast.stripped(node.roles["operand"])
        else:
            return node


def note_change(target: Node, loop: Loop) -> None:
    root: Node = root_object(target)
    identity: str = referenced_id(root)
    if identity:
        loop.changed.add(identity)
        return
    field: str = this_field(root)
    if field:
        loop.changed_fields.add(field)


def collect_changes(node: Node, loop: Loop) -> None:
    """Marks what one node of a loop changes, then its children."""
    kind: str = node.kind
    if kind in ("BinaryOperator", "CompoundAssignOperator") and node.opcode in ASSIGNMENTS and "left" in node.roles:
        note_change(node.roles["left"], loop)
    elif kind == "UnaryOperator" and node.opcode in STEPS and "operand" in node.roles:
        note_change(node.roles["operand"], loop)
    elif kind == "CXXOperatorCallExpr" and (node.opcode in ASSIGNMENTS or node.opcode in STEPS) and node.arguments:
        note_change(node.arguments[0], loop)
    elif kind == "VarDecl" and node.identity:
        loop.changed.add(node.identity)
    elif kind == "CXXMemberCallExpr" and "object" in node.roles:
        if not node.constant and node.name not in OBSERVERS:
            note_change(node.roles["object"], loop)
    if kind in clang_ast.CALLS:
        # An argument bound to a non-const reference may be written (the reader's `writes`).
        index: int
        for index in range(min(len(node.arguments), len(node.writes))):
            if node.writes[index]:
                note_change(node.arguments[index], loop)
    child: Node
    for child in node.children:
        collect_changes(child, loop)


def loop_parts(node: Node) -> tuple[list[Node], list[Node]]:
    """A loop's (increment parts, every part that runs each iteration)."""
    names: tuple[str, ...] = ()
    increments: list[Node] = []
    if node.kind == "ForStmt":
        # The init runs once, before the loop.
        names = ("variable", "condition", "increment", "body")
        if "increment" in node.roles:
            increments.append(node.roles["increment"])
    elif node.kind == "CXXForRangeStmt":
        # The loop variable is the element, not an index: changed each iteration, never an induction variable.
        names = ("variable", "body")
    else:
        return increments, list(node.children)
    parts: list[Node] = []
    name: str
    for name in names:
        if name in node.roles:
            parts.append(node.roles[name])
    return increments, parts


def make_loop(node: Node, line: int) -> Loop:
    loop: Loop = Loop(node, line, LOOP_NAMES[node.kind], set(), set(), set())
    increments: list[Node]
    parts: list[Node]
    increments, parts = loop_parts(node)
    part: Node
    for part in parts:
        collect_changes(part, loop)
    probe: Loop = Loop(node, line, loop.kind, set(), set(), set())
    for part in increments:
        collect_changes(part, probe)
    loop.induction = probe.changed
    if loop.kind in ("while", "do") and "condition" in node.roles:
        # A while loop's index is a variable its condition reads and its body changes.
        reads: Reads = condition_reads(node.roles["condition"])
        loop.induction = reads.variables & loop.changed
    return loop


# --- classifying a condition --------------------------------------------------------

@dataclass
class Reads:
    variables: set[str]
    fields: set[str]
    names: dict[str, str]       # identity -> name, for reasons
    calls: list[str]            # calls that may change or depend on state
    elements: bool              # an element read: subscript or dereference


def reads_of(node: Node, reads: Reads) -> None:
    kind: str = node.kind
    identity: str = referenced_id(node)
    if identity:
        reads.variables.add(identity)
        reads.names[identity] = node.name
    field: str = this_field(node)
    if field:
        reads.fields.add(field)
    if kind == "ArraySubscriptExpr" or (kind == "UnaryOperator" and node.opcode == "*"):
        reads.elements = True
    if kind in ("CallExpr", "CXXMemberCallExpr", "CXXOperatorCallExpr"):
        observer: bool = kind == "CXXMemberCallExpr" and (node.name in OBSERVERS or node.name == "operator bool")
        operator: bool = kind == "CXXOperatorCallExpr" and node.opcode in COMPARING_OPERATORS
        # An overloaded unary * dereferences; a binary * multiplies.
        dereference: bool = node.opcode == "*" and len(node.arguments) == 1
        if kind == "CXXOperatorCallExpr" and (node.opcode in ELEMENT_OPERATORS or dereference):
            reads.elements = True
        elif not observer and not operator:
            reads.calls.append(node.name or "a call")
    child: Node
    for child in node.children:
        reads_of(child, reads)


def condition_reads(condition: Node) -> Reads:
    reads: Reads = Reads(set(), set(), {}, [], False)
    reads_of(condition, reads)
    return reads


def index_comparison(condition: Node, loop: Loop) -> bool:
    """A comparison of the loop's index (plus or minus a constant) with something the loop leaves alone."""
    node: Node = clang_ast.stripped(condition)
    if node.kind != "BinaryOperator" or "left" not in node.roles or "right" not in node.roles:
        return False
    if node.opcode in ("&&", "||"):
        return index_comparison(node.roles["left"], loop) and index_comparison(node.roles["right"], loop)
    if node.opcode not in COMPARISONS:
        return False
    left: Reads = condition_reads(node.roles["left"])
    right: Reads = condition_reads(node.roles["right"])
    left_index: bool = bool(left.variables & loop.induction)
    right_index: bool = bool(right.variables & loop.induction)
    if left_index == right_index:
        return False
    indexed: Reads = left if left_index else right
    other: Reads = right if left_index else left
    if indexed.elements or indexed.calls or (indexed.variables - loop.induction) & loop.changed:
        return False
    return not changes_seen(other, loop)


def strided(node: Node) -> bool:
    """Whether a condition takes a remainder or a low-bit mask (index % 2, index & 1)."""
    if node.kind == "BinaryOperator" and node.opcode in ("%", "&"):
        return True
    child: Node
    for child in node.children:
        if strided(child):
            return True
    return False


def changes_seen(reads: Reads, loop: Loop) -> bool:
    return bool(reads.variables & loop.changed or reads.fields & loop.changed_fields or reads.calls)


def leaves(statement: Node) -> str:
    """How a branch's statement leaves the loop: "failure", "found", "" (stays)."""
    node: Node = statement
    if node.kind == "CompoundStmt":
        if not node.children:
            return ""
        node = node.children[-1]
    if node.kind == "CXXThrowExpr":
        return "failure"
    if node.kind in ("BreakStmt", "GotoStmt"):
        return "found"
    if node.kind == "ReturnStmt":
        if "value" in node.roles and failure_value(clang_ast.stripped(node.roles["value"])):
            return "failure"
        return "found"
    return ""


def failure_value(value: Node) -> bool:
    if value.kind == "CXXBoolLiteralExpr":
        return value.value == "false"
    if value.kind in ("CXXNullPtrLiteralExpr", "GNUNullExpr"):
        return True
    if value.kind == "UnaryOperator" and value.opcode == "-":
        return True
    names: list[str] = []
    collect_names(value, names)
    name: str
    for name in names:
        if FAILURE_NAME.search(name):
            return True
    return False


def collect_names(node: Node, names: list[str]) -> None:
    if node.kind in ("DeclRefExpr", "MemberExpr"):
        names.append(node.name)
    child: Node
    for child in node.children:
        collect_names(child, names)


def classify(condition: Node, loop: Loop, branch: Node | None) -> tuple[str, str]:
    """The class of a condition in a loop and the reason, in words."""
    reads: Reads = condition_reads(condition)
    changed_variables: set[str] = reads.variables & loop.changed
    changed_fields: set[str] = reads.fields & loop.changed_fields
    if not changed_variables and not changed_fields and not reads.calls:
        read: list[str] = sorted(set(reads.names.values()) | reads.fields)
        return "invariant", "reads " + (", ".join(read) if read else "constants") + ", unchanged in the loop"
    if index_comparison(condition, loop):
        if strided(condition):
            return "edge", "tests the loop index's stride: unroll or split by the stride"
        return "edge", "compares the loop index with fixed bounds"
    why: str = describe(reads, changed_variables, changed_fields)
    if branch is not None:
        exit: str = leaves(branch)
        if exit == "failure":
            return "validation", why + " and leaves with a failure"
        if exit == "found":
            return "control", why + " and ends the loop's work"
    return "data", why


def describe(reads: Reads, variables: set[str], fields: set[str]) -> str:
    parts: list[str] = []
    named: list[str] = sorted(reads.names[identity] for identity in variables)
    if named or fields:
        parts.append("reads " + ", ".join(named + sorted(fields)) + ", changed in the loop")
    if reads.calls:
        parts.append("calls " + ", ".join(sorted(set(reads.calls))))
    if reads.elements:
        parts.append("reads elements")
    result: str = "; ".join(parts) if parts else "depends on the iteration"
    return result


# --- walking a translation unit -------------------------------------------------

class UnitAnalysis:
    """Finds the branches in loops of one unit's kept declarations."""

    def __init__(self, root: Path, source: clang_ast.SourceText) -> None:
        self.root: Path = root
        self.root_text: str = root.as_posix().lower().rstrip("/") + "/"
        self.source: clang_ast.SourceText = source
        self.findings: list[Finding] = []
        self.files: set[str] = set()
        self.loop_stack: list[Loop] = []
        # id() of conditions already listed; their nodes stay alive in the declaration being walked.
        self.condition_nodes: set[int] = set()

    def relative(self, file: str) -> str:
        if file.lower().startswith(self.root_text):
            return file[len(self.root_text):]
        return file

    def declaration(self, top: Node) -> None:
        self.loop_stack = []
        self.condition_nodes = set()
        clang_ast.walk(top, self.visit)

    def visit(self, node: Node, parents: list[Node]) -> None:
        if node.file:
            self.files.add(node.file)
        # Leave loops the walk has moved past.
        while self.loop_stack and not self.inside(self.loop_stack[-1], parents):
            self.loop_stack.pop()
        kind: str = node.kind
        if kind in LOOPS and node.begin >= 0:
            self.loop_stack.append(make_loop(node, self.source.line(node.file, node.begin)))
            if "condition" in node.roles:
                # A loop's own condition is the loop, not a branch inside it.
                self.condition_nodes.add(id(node.roles["condition"]))
            return
        if not self.loop_stack or node.begin < 0:
            return
        loop: Loop = self.loop_stack[-1]
        condition: Node | None = node.roles.get("condition")
        if kind == "IfStmt" and condition is not None:
            self.condition_nodes.add(id(condition))
            self.record(node, parents, loop, "if", condition, node.roles.get("then"))
        elif kind == "SwitchStmt" and condition is not None:
            self.condition_nodes.add(id(condition))
            self.record(node, parents, loop, "switch", condition, None)
        elif kind == "ConditionalOperator" and condition is not None:
            self.condition_nodes.add(id(condition))
            self.record(node, parents, loop, "conditional", condition, None)
        elif kind == "BinaryOperator" and node.opcode in ("&&", "||"):
            if not self.inside_condition(node, parents):
                branch: str = "and" if node.opcode == "&&" else "or"
                self.record(node, parents, loop, branch, node, None)

    def inside(self, loop: Loop, parents: list[Node]) -> bool:
        parent: Node
        for parent in parents:
            if parent is loop.node:
                return True
        return False

    def inside_condition(self, node: Node, parents: list[Node]) -> bool:
        """Whether a && or || is part of a condition already listed (or of the loop's own)."""
        if id(node) in self.condition_nodes:
            return True
        parent: Node
        for parent in reversed(parents):
            if id(parent) in self.condition_nodes:
                return True
            if parent is self.loop_stack[-1].node:
                return False
        return False

    def record(self, node: Node, parents: list[Node], loop: Loop, branch: str, condition: Node,
               then: Node | None) -> None:
        cls: str
        reason: str
        cls, reason = classify(condition, loop, then)
        text: str = " ".join(self.source.node_text(condition).split())[:200]
        function: str = enclosing_function(parents)
        line: int = self.source.line(node.file, node.begin)
        self.findings.append(Finding(self.relative(node.file), line, node.begin, function, loop.line, loop.kind,
                                     branch, cls, text, reason, node.in_macro))


def enclosing_function(parents: list[Node]) -> str:
    names: list[str] = []
    parent: Node
    for parent in parents:
        if parent.kind in ("CXXRecordDecl", "NamespaceDecl") and parent.name:
            names.append(parent.name)
        if parent.kind in clang_ast.FUNCTIONS:
            names.append(parent.name)
    result: str = "::".join(names)
    return result


# --- one unit, in a worker process -----------------------------------------------

# Files whose declarations this worker has analysed; a header is analysed once per worker.
DONE: set[str] = set()


@dataclass(frozen=True)
class UnitJob:
    entry: dict                     # the compile-database entry (directory, file, command or arguments)
    root: str
    first_party: tuple[str, ...]
    reader: str = ""                # a backend name, or "" for the reader's own choice (HARNESS_AST_READER)


class KeptFile:
    """The reader's `keep` test for one job: first-party files this worker has not analysed yet."""

    def __init__(self, job: UnitJob) -> None:
        self.root: str = job.root.lower().rstrip("/") + "/"
        self.first_party: tuple[str, ...] = job.first_party

    def __call__(self, file: str) -> bool:
        path: str = file.lower()
        if not path.startswith(self.root) or path in DONE:
            return False
        relative: str = path[len(self.root):]
        if "/third_party/" in "/" + relative or "/_deps/" in "/" + relative:
            return False
        return relative.split("/", 1)[0] in self.first_party


def analyse_unit(job: UnitJob) -> list[dict]:
    root: Path = Path(job.root)
    analysis: UnitAnalysis = UnitAnalysis(root, clang_ast.SourceText())
    top: Node
    for top in clang_ast.read_unit(clang_ast.unit_command(job.entry), KeptFile(job), job.reader):
        analysis.declaration(top)
    seen: str
    for seen in analysis.files:
        DONE.add(seen.lower())
    records: list[dict] = []
    finding: Finding
    for finding in analysis.findings:
        records.append(asdict(finding))
    return records


# --- decisions, selection and the run ---------------------------------------------

def normalized_condition(text: str) -> str:
    return " ".join(text.split())


def load_decisions(path: Path | None) -> list[Decision]:
    """Recorded decisions; a missing file means none yet."""
    if path is None or not path.exists():
        return []
    data: dict = json.loads(path.read_text(encoding="utf-8"))
    decisions: list[Decision] = []
    item: dict
    for item in data.get("decisions", []):
        decision: str = item.get("decision", "")
        if decision not in DECISIONS:
            raise ValueError(f"{path}: unknown decision {decision!r} for {item.get('path')}")
        decisions.append(Decision(item["path"], item.get("function", ""), normalized_condition(item["condition"]),
                                  decision, item.get("reason", "")))
    return decisions


def decided(record: dict, decisions: list[Decision]) -> Decision | None:
    key: tuple[str, str, str] = (record["path"], record["function"], normalized_condition(record["condition"]))
    decision: Decision
    for decision in decisions:
        if (decision.path, decision.function, decision.condition) == key:
            return decision
    return None


def unit_jobs(entries: list[dict], root: Path, paths: list[str]) -> list[UnitJob]:
    """One job per first-party translation unit, or per unit under the given checkout-relative paths."""
    jobs: list[UnitJob] = []
    seen: set[str] = set()
    entry: dict
    for entry in entries:
        source: Path = hunt.normalized(entry["file"], entry["directory"])
        relative: str = hunt.first_party(source, root)
        if not relative or relative in seen:
            continue
        if paths and not any(relative == path or relative.startswith(path.rstrip("/") + "/") for path in paths):
            continue
        seen.add(relative)
        jobs.append(UnitJob(entry, root.as_posix(), hunt.FIRST_PARTY))
    return jobs


def summary(records: list[dict], units: int) -> str:
    lines: list[str] = []
    classes: Counter[str] = Counter()
    branches: Counter[str] = Counter()
    areas: dict[str, Counter[str]] = {}
    statuses: Counter[str] = Counter()
    record: dict
    for record in records:
        classes[record["cls"]] += 1
        branches[record["branch"]] += 1
        statuses[record["status"]] += 1
        areas.setdefault(hunt.area_of(record["path"]), Counter())[record["cls"]] += 1
    lines.append(f"loop branches: {units} units, {len(records)} branches in loops "
                 f"({statuses['open']} open, {statuses['decided']} decided)")
    lines.append("by class: " + ", ".join(f"{name} {classes[name]}" for name in CLASSES))
    lines.append("by branch: " + ", ".join(f"{name} {count}" for name, count in branches.most_common()))
    lines.append("")
    lines.append("| Area | " + " | ".join(CLASSES) + " |")
    lines.append("|---|" + "---|" * len(CLASSES))
    area: str
    for area in sorted(areas):
        counts: Counter[str] = areas[area]
        lines.append(f"| {area} | " + " | ".join(str(counts[name]) for name in CLASSES) + " |")
    result: str = "\n".join(lines) + "\n"
    return result


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__,
                                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(project.current().source_root()), help="the product checkout")
    parser.add_argument("--build-dir", required=True, help="a configured ninja tree (its compile database)")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--paths", nargs="*", default=[], help="only units under these checkout-relative paths")
    parser.add_argument("--decisions", help="recorded decisions (default: the project's [loop_branches] decisions)")
    parser.add_argument("--json", dest="json_path", help="write the review queue here")
    parser.add_argument("--report", help="write the short report here")
    args: argparse.Namespace = parser.parse_args()
    project.current().require_state_machines()
    root: Path = Path(args.root).resolve()
    decisions_path: Path | None = Path(args.decisions) if args.decisions else None
    configured: str = project.current().loop_branch_decisions
    if decisions_path is None and configured:
        decisions_path = root / configured
    decisions: list[Decision] = load_decisions(decisions_path)
    build_dir: Path = Path(args.build_dir)
    entries: list[dict] = hunt.compile_database(build_dir)
    jobs: list[UnitJob] = unit_jobs(entries, root, args.paths)
    if not jobs:
        parser.error("no first-party translation unit selected; an empty scope is not a pass")
    hunt.generate_headers(build_dir)
    unique: dict[tuple[str, int, str], dict] = {}
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        records: list[dict]
        for records in pool.map(analyse_unit, jobs, chunksize=4):
            record: dict
            for record in records:
                unique[(record["path"], record["offset"], record["branch"])] = record
    queue: list[dict] = []
    for record in sorted(unique.values(), key=queue_order):
        decision: Decision | None = decided(record, decisions)
        record["status"] = "decided" if decision is not None else "open"
        if decision is not None:
            record["decision"] = decision.decision
            record["decision_reason"] = decision.reason
        queue.append(record)
    report: str = summary(queue, len(jobs))
    sys.stdout.write(report)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(queue, indent=1) + "\n", encoding="utf-8")
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    return 0


def queue_order(record: dict) -> tuple[int, str, int]:
    """Open classes the house style repairs first: invariant, edge, validation, then data and control."""
    rank: dict[str, int] = {"invariant": 0, "edge": 1, "validation": 2, "data": 3, "control": 4}
    return rank[record["cls"]], record["path"], record["line"]


if __name__ == "__main__":
    raise SystemExit(main())
