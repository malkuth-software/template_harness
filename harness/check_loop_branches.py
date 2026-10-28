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
               the interior.
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

The run reads the pinned clang's JSON AST (clang_ast.py) for every
first-party translation unit of a configured ninja tree's compile database,
keeps the first-party declarations, and analyses each header's declarations
once per worker process. Findings are deduplicated by file and offset.

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
import functools
import json
import os
from pathlib import Path
import re
import sys

import check_state_machines as hunt
import clang_ast
import project

LOOPS: tuple[str, ...] = ("ForStmt", "WhileStmt", "DoStmt", "CXXForRangeStmt")
FUNCTIONS: tuple[str, ...] = ("FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl",
                              "CXXConversionDecl")
CLASSES: tuple[str, ...] = ("invariant", "edge", "validation", "control", "data")
DECISIONS: tuple[str, ...] = ("keep", "hoist", "peel", "validate_up_front", "branch_free", "split", "false_shape")
# Member functions that only observe their object.
OBSERVERS: frozenset[str] = frozenset({"size", "empty", "data", "length", "capacity", "begin", "end", "cbegin",
                                       "cend", "has_value", "front", "back", "count", "size_bytes"})
COMPARISONS: frozenset[str] = frozenset({"==", "!=", "<", ">", "<=", ">="})
ASSIGNMENTS: frozenset[str] = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="})
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
    node: dict
    line: int
    kind: str
    changed: set[str]           # declaration ids changed anywhere in the loop
    changed_fields: set[str]    # field names changed through `this`
    induction: set[str]         # ids changed in the loop's increment, or a range-for's variable


# --- what a loop changes ---------------------------------------------------------

def stripped(node: dict) -> dict:
    """The expression under implicit casts and parentheses."""
    while node.get("kind") in ("ImplicitCastExpr", "ParenExpr", "ExprWithCleanups", "MaterializeTemporaryExpr",
                               "CXXBindTemporaryExpr", "ConstantExpr", "FullExpr") and node.get("inner"):
        node = node["inner"][0]
    return node


def referenced_id(node: dict) -> str:
    """The variable a DeclRefExpr names, or ""."""
    if node.get("kind") != "DeclRefExpr":
        return ""
    declaration: dict = node.get("referencedDecl", {})
    if declaration.get("kind") not in ("VarDecl", "ParmVarDecl", "BindingDecl"):
        return ""
    result: str = declaration.get("id", "")
    return result


def this_field(node: dict) -> str:
    """The field a MemberExpr reaches through `this`, or ""."""
    if node.get("kind") != "MemberExpr":
        return ""
    base: dict = stripped(node["inner"][0]) if node.get("inner") else {}
    if base.get("kind") != "CXXThisExpr":
        return ""
    result: str = node.get("name", "")
    return result


def root_object(node: dict) -> dict:
    """The variable or `this` field an lvalue expression reaches: a.b[i].c gives a."""
    node = stripped(node)
    while True:
        kind: str = node.get("kind", "")
        if kind == "MemberExpr" and not this_field(node) and node.get("inner"):
            node = stripped(node["inner"][0])
        elif kind == "ArraySubscriptExpr" and node.get("inner"):
            node = stripped(node["inner"][0])
        elif kind == "UnaryOperator" and node.get("opcode") == "*" and node.get("inner"):
            node = stripped(node["inner"][0])
        else:
            return node


def note_change(target: dict, loop: Loop) -> None:
    root: dict = root_object(target)
    identity: str = referenced_id(root)
    if identity:
        loop.changed.add(identity)
        return
    field: str = this_field(root)
    if field:
        loop.changed_fields.add(field)


def collect_changes(node: dict, loop: Loop) -> None:
    """Marks what one node of a loop changes, then its children."""
    kind: str = node.get("kind", "")
    inner: list[dict] = node.get("inner", [])
    if kind in ("BinaryOperator", "CompoundAssignOperator") and node.get("opcode") in ASSIGNMENTS and inner:
        note_change(inner[0], loop)
    elif kind == "UnaryOperator" and node.get("opcode") in ("++", "--") and inner:
        note_change(inner[0], loop)
    elif kind == "CXXOperatorCallExpr" and inner and len(inner) > 1:
        name: str = callee_name(inner[0])
        if name.startswith("operator") and name[len("operator"):] in ASSIGNMENTS | {"++", "--"}:
            note_change(inner[1], loop)
    elif kind == "VarDecl":
        loop.changed.add(node.get("id", ""))
    elif kind == "CXXMemberCallExpr" and inner:
        callee: dict = inner[0]
        if callee.get("kind") == "MemberExpr" and callee.get("inner"):
            base: dict = callee["inner"][0]
            # A const member function is called through a NoOp cast to const.
            constant: bool = base.get("kind") == "ImplicitCastExpr" and base.get("castKind") == "NoOp"
            if not constant and callee.get("name", "") not in OBSERVERS:
                note_change(base, loop)
    if kind in ("CallExpr", "CXXMemberCallExpr", "CXXOperatorCallExpr", "CXXConstructExpr"):
        argument: dict
        for argument in inner[1:] if kind != "CXXConstructExpr" else inner:
            # An lvalue argument with no conversion binds a reference: it may be written.
            if argument.get("valueCategory") == "lvalue" and argument.get("kind") in ("DeclRefExpr", "MemberExpr"):
                note_change(argument, loop)
    child: dict
    for child in inner:
        collect_changes(child, loop)


def callee_name(node: dict) -> str:
    node = stripped(node)
    if node.get("kind") == "DeclRefExpr":
        result: str = node.get("referencedDecl", {}).get("name", "")
        return result
    if node.get("kind") == "MemberExpr":
        result = node.get("name", "")
        return result
    return ""


def loop_parts(node: dict) -> tuple[list[dict], list[dict]]:
    """A loop's (increment-like parts, all parts) as clang orders its children."""
    inner: list[dict] = node.get("inner", [])
    kind: str = node.get("kind", "")
    if kind == "ForStmt" and len(inner) == 5:
        return [inner[3]], inner[1:]          # the init runs once, before the loop
    if kind == "CXXForRangeStmt":
        # Children: init, range, begin, end, condition, increment, the loop
        # variable's DeclStmt, body. The loop variable is the element, not an
        # index: changed each iteration, never an induction variable.
        return [], inner[-2:]
    return [], inner


def make_loop(node: dict, line: int) -> Loop:
    names: dict[str, str] = {"ForStmt": "for", "WhileStmt": "while", "DoStmt": "do", "CXXForRangeStmt": "range-for"}
    loop: Loop = Loop(node, line, names[node["kind"]], set(), set(), set())
    increments: list[dict]
    parts: list[dict]
    increments, parts = loop_parts(node)
    part: dict
    for part in parts:
        collect_changes(part, loop)
    probe: Loop = Loop(node, line, loop.kind, set(), set(), set())
    for part in increments:
        collect_changes(part, probe)
    loop.induction = probe.changed
    if loop.kind in ("while", "do"):
        # A while loop's index is a variable its condition reads and its body changes.
        inner: list[dict] = node.get("inner", [])
        condition: dict = inner[0] if loop.kind == "while" and inner else (inner[-1] if inner else {})
        reads: Reads = condition_reads(condition, loop)
        loop.induction = reads.variables & loop.changed
    return loop


# --- classifying a condition --------------------------------------------------------

@dataclass
class Reads:
    variables: set[str]
    fields: set[str]
    names: dict[str, str]       # id -> name, for reasons
    calls: list[str]            # calls that may change or depend on state
    elements: bool              # an element read: subscript or dereference


def reads_of(node: dict, loop: Loop, reads: Reads) -> None:
    kind: str = node.get("kind", "")
    identity: str = referenced_id(node)
    if identity:
        reads.variables.add(identity)
        reads.names[identity] = node["referencedDecl"].get("name", "")
    field: str = this_field(node)
    if field:
        reads.fields.add(field)
    if kind in ("ArraySubscriptExpr",) or (kind == "UnaryOperator" and node.get("opcode") == "*"):
        reads.elements = True
    if kind in ("CallExpr", "CXXMemberCallExpr", "CXXOperatorCallExpr"):
        name: str = callee_name(node["inner"][0]) if node.get("inner") else ""
        observer: bool = kind == "CXXMemberCallExpr" and name in OBSERVERS
        operator: bool = kind == "CXXOperatorCallExpr" and name in ("operator==", "operator!=", "operator<",
                                                                    "operator>", "operator<=", "operator>=",
                                                                    "operator!", "operator bool")
        subscript: bool = kind == "CXXOperatorCallExpr" and name in ("operator[]", "operator*", "operator->")
        if subscript:
            reads.elements = True
        elif not observer and not operator:
            reads.calls.append(name or "a call")
    child: dict
    for child in node.get("inner", []):
        reads_of(child, loop, reads)


def condition_reads(condition: dict, loop: Loop) -> Reads:
    reads: Reads = Reads(set(), set(), {}, [], False)
    reads_of(condition, loop, reads)
    return reads


def index_comparison(condition: dict, loop: Loop) -> bool:
    """A comparison of the loop's index (plus or minus a constant) with something the loop leaves alone."""
    node: dict = stripped(condition)
    if node.get("kind") == "BinaryOperator" and node.get("opcode") in ("&&", "||"):
        return all(index_comparison(side, loop) for side in node.get("inner", []))
    if node.get("kind") != "BinaryOperator" or node.get("opcode") not in COMPARISONS:
        return False
    sides: list[dict] = node.get("inner", [])
    if len(sides) != 2:
        return False
    left: Reads = condition_reads(sides[0], loop)
    right: Reads = condition_reads(sides[1], loop)
    left_index: bool = bool(left.variables & loop.induction)
    right_index: bool = bool(right.variables & loop.induction)
    if left_index == right_index:
        return False
    indexed: Reads = left if left_index else right
    other: Reads = right if left_index else left
    if indexed.elements or indexed.calls or (indexed.variables - loop.induction) & loop.changed:
        return False
    return not changes_seen(other, loop)


def changes_seen(reads: Reads, loop: Loop) -> bool:
    return bool(reads.variables & loop.changed or reads.fields & loop.changed_fields or reads.calls
                or (reads.elements and (reads.variables & loop.changed or reads.fields & loop.changed_fields)))


def leaves(statement: dict) -> str:
    """How a branch's statement leaves the loop: "failure", "found", "" (stays)."""
    node: dict = statement
    if node.get("kind") == "CompoundStmt":
        inner: list[dict] = node.get("inner", [])
        if not inner:
            return ""
        node = inner[-1]
    kind: str = node.get("kind", "")
    if kind == "CXXThrowExpr" or (kind == "ExprWithCleanups" and stripped(node).get("kind") == "CXXThrowExpr"):
        return "failure"
    if kind in ("BreakStmt", "GotoStmt"):
        return "found"
    if kind == "ReturnStmt":
        value: dict = stripped(node["inner"][0]) if node.get("inner") else {}
        return "failure" if failure_value(value) else "found"
    return ""


def failure_value(value: dict) -> bool:
    kind: str = value.get("kind", "")
    if kind == "CXXBoolLiteralExpr":
        return value.get("value") is False
    if kind in ("CXXNullPtrLiteralExpr", "GNUNullExpr"):
        return True
    if kind == "UnaryOperator" and value.get("opcode") == "-":
        return True
    names: list[str] = []
    collect_names(value, names)
    name: str
    for name in names:
        if FAILURE_NAME.search(name):
            return True
    return False


def collect_names(node: dict, names: list[str]) -> None:
    if node.get("kind") == "DeclRefExpr":
        names.append(node.get("referencedDecl", {}).get("name", ""))
    elif node.get("kind") == "MemberExpr":
        names.append(node.get("name", ""))
    child: dict
    for child in node.get("inner", []):
        collect_names(child, names)


def classify(condition: dict, loop: Loop, branch: dict | None) -> tuple[str, str]:
    """The class of a condition in a loop and the reason, in words."""
    reads: Reads = condition_reads(condition, loop)
    changed_variables: set[str] = reads.variables & loop.changed
    changed_fields: set[str] = reads.fields & loop.changed_fields
    if not changed_variables and not changed_fields and not reads.calls:
        read: list[str] = sorted(set(reads.names.values()) | reads.fields)
        return "invariant", "reads " + (", ".join(read) if read else "constants") + ", unchanged in the loop"
    if index_comparison(condition, loop):
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

def if_parts(node: dict) -> tuple[dict, dict]:
    """An IfStmt's condition and then-branch, after any init statement and condition variable."""
    inner: list[dict] = node.get("inner", [])
    index: int = (1 if node.get("hasInit") else 0) + (1 if node.get("hasVar") else 0)
    condition: dict = inner[index] if index < len(inner) else {}
    then: dict = inner[index + 1] if index + 1 < len(inner) else {}
    return condition, then


def switch_condition(node: dict) -> dict:
    inner: list[dict] = node.get("inner", [])
    index: int = (1 if node.get("hasInit") else 0) + (1 if node.get("hasVar") else 0)
    result: dict = inner[index] if index < len(inner) else {}
    return result


class UnitAnalysis:
    """Finds the branches in loops of one unit's kept declarations."""

    def __init__(self, root: Path, directory: str, source: clang_ast.SourceText) -> None:
        self.root: Path = root
        self.directory: str = directory
        self.root_text: str = root.as_posix().lower().rstrip("/") + "/"
        self.source: clang_ast.SourceText = source
        self.findings: list[Finding] = []
        self.files: set[str] = set()

    def relative(self, file: str) -> str:
        path: str = absolute(self.directory, file)
        if path.lower().startswith(self.root_text):
            return path[len(self.root_text):]
        return path

    def declaration(self, declaration: clang_ast.Declaration) -> None:
        walker: clang_ast.Walker = clang_ast.Walker(declaration.file_before, self.visit)
        self.function_stack: list[dict] = []
        self.loop_stack: list[Loop] = []
        self.condition_nodes: set[int] = set()
        walker.walk(declaration.node)

    def visit(self, node: dict, parents: list[dict], site: clang_ast.Site) -> None:
        if site.file:
            site = clang_ast.Site(absolute(self.directory, site.file), site.begin, site.end, site.in_macro)
            self.files.add(site.file)
        # Leave loops and functions the walk has moved past.
        while self.loop_stack and not any(parent is self.loop_stack[-1].node for parent in parents):
            self.loop_stack.pop()
        kind: str = node.get("kind", "")
        if kind in LOOPS and site.begin >= 0 and not node_is_implicit(parents):
            self.loop_stack.append(make_loop(node, self.source.line(site.file, site.begin)))
            self.mark_loop_condition(node)
            return
        if not self.loop_stack or site.begin < 0:
            return
        loop: Loop = self.loop_stack[-1]
        if kind == "IfStmt":
            condition: dict
            then: dict
            condition, then = if_parts(node)
            self.condition_nodes.add(id(condition))
            self.record(node, parents, site, loop, "if", condition, then)
        elif kind == "SwitchStmt":
            condition = switch_condition(node)
            self.condition_nodes.add(id(condition))
            self.record(node, parents, site, loop, "switch", condition, None)
        elif kind == "ConditionalOperator" and node.get("inner"):
            self.condition_nodes.add(id(node["inner"][0]))
            self.record(node, parents, site, loop, "conditional", node["inner"][0], None)
        elif kind == "BinaryOperator" and node.get("opcode") in ("&&", "||"):
            if not self.inside_condition(node, parents):
                branch: str = "and" if node["opcode"] == "&&" else "or"
                self.record(node, parents, site, loop, branch, node, None)

    def mark_loop_condition(self, node: dict) -> None:
        """A loop's own condition is the loop, not a branch inside it."""
        inner: list[dict] = node.get("inner", [])
        kind: str = node.get("kind", "")
        if kind == "ForStmt" and len(inner) == 5:
            self.condition_nodes.add(id(inner[2]))
        elif kind == "WhileStmt" and inner:
            self.condition_nodes.add(id(inner[0]))
        elif kind == "DoStmt" and len(inner) == 2:
            self.condition_nodes.add(id(inner[1]))

    def inside_condition(self, node: dict, parents: list[dict]) -> bool:
        """Whether a && or || is part of a condition already listed (or of the loop's own)."""
        if id(node) in self.condition_nodes:
            return True
        parent: dict
        for parent in reversed(parents):
            if id(parent) in self.condition_nodes:
                return True
            if parent is self.loop_stack[-1].node:
                return False
        return False

    def record(self, node: dict, parents: list[dict], site: clang_ast.Site, loop: Loop, branch: str,
               condition: dict, then: dict | None) -> None:
        if not condition:
            return
        cls: str
        reason: str
        cls, reason = classify(condition, loop, then)
        text: str = condition_text(self.source, site.file, condition)
        function: str = enclosing_function(parents)
        self.findings.append(Finding(self.relative(site.file), self.source.line(site.file, site.begin), site.begin,
                                     function, loop.line, loop.kind, branch, cls, text, reason, site.in_macro))


def node_is_implicit(parents: list[dict]) -> bool:
    parent: dict
    for parent in parents:
        if parent.get("isImplicit"):
            return True
    return False


def enclosing_function(parents: list[dict]) -> str:
    names: list[str] = []
    parent: dict
    for parent in parents:
        if parent.get("kind") in ("CXXRecordDecl", "NamespaceDecl") and parent.get("name"):
            names.append(parent["name"])
        if parent.get("kind") in FUNCTIONS:
            names.append(parent.get("name", ""))
    result: str = "::".join(names)
    return result


def condition_text(source: clang_ast.SourceText, file: str, condition: dict) -> str:
    extent: dict = condition.get("range", {})
    begin: int = clang_ast.offset_of(extent.get("begin", {}))
    end: int = clang_ast.end_offset_of(extent.get("end", {}))
    if not file or begin < 0 or end < 0:
        return ""
    text: str = " ".join(source.text(file, begin, end).split())
    return text[:200]


# --- one unit, in a worker process -----------------------------------------------

# Files whose declarations this worker has analysed; a header is analysed once per worker.
DONE: set[str] = set()


@dataclass(frozen=True)
class UnitJob:
    directory: str
    command: tuple[str, ...]
    root: str
    first_party: tuple[str, ...]


def absolute(directory: str, file: str) -> str:
    """A dump's file name as an absolute path with forward slashes (clang writes it as the command named it)."""
    path: Path = Path(file)
    if not path.is_absolute():
        path = Path(directory) / path
    result: str = Path(os.path.normpath(str(path))).as_posix()
    return result


def kept_file(job: UnitJob, file: str) -> bool:
    path: str = absolute(job.directory, file).lower()
    root: str = job.root.lower().rstrip("/") + "/"
    if not path.startswith(root) or path in DONE:
        return False
    relative: str = path[len(root):]
    if "/third_party/" in "/" + relative or "/_deps/" in "/" + relative:
        return False
    return relative.split("/", 1)[0] in job.first_party


def analyse_unit(job: UnitJob) -> list[dict]:
    root: Path = Path(job.root)
    analysis: UnitAnalysis = UnitAnalysis(root, job.directory, clang_ast.SourceText())
    keep: functools.partial[bool] = functools.partial(kept_file, job)
    declaration: clang_ast.Declaration
    for declaration in clang_ast.dump_declarations(job.directory, list(job.command), keep):
        analysis.declaration(declaration)
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
        command: list[str] = clang_ast.front_end_command(entry["command"], [])
        jobs.append(UnitJob(entry["directory"], tuple(command), root.as_posix(), hunt.FIRST_PARTY))
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
