"""Copies and allocations on paths that repeat (Phase E, E5, the static half).

Six kinds of review candidate, each about an owning type: a standard string
or container, a std::function, or a first-party record holding one (found
across the run from the records' fields).

copy_in_loop            an object copied (copy constructor or copy
                        assignment) inside a loop.
copy_then_read          a local initialized as a copy of another object and
                        then only read: a const reference may do, unless the
                        copy is a deliberate snapshot (house style: copy input
                        that may change under you).
by_value_read           a parameter taken by value and only read: a const
                        reference (or a view) may do.
allocation_in_loop      an owning local declared inside a loop body, a `new`,
                        or a make_unique or make_shared call inside a loop:
                        allocate before the loop and reuse (house style,
                        Repeated kernels).
growth_without_reserve  push_back, emplace_back, append, insert or += on
                        every iteration of a counted loop (for, range-for; no
                        branch between the loop and the call) on a vector,
                        string or unordered container declared before the
                        loop, with no reserve on it before the loop.
returned_into_existing  a standard container or string that keeps its storage
                        (a member, or a variable declared before the loop
                        it is assigned in) assigned a fresh one returned by a
                        call: the caller already owns storage the callee could
                        fill (house style, fill caller-supplied storage).

Handles and views (shared_ptr, weak_ptr, span, string_view) own nothing here:
copying one copies a pointer.

Reads and writes of a variable are judged as check_const_restrict.py judges
an object's uses; a variable that is returned or moved from is not only
read. A function whose signature is fixed (virtual, override, address taken,
main) lists its by_value_read candidates as signature_bound.

Recorded decisions sit in a JSON file (the project's [copies] decisions, a
path in the product checkout, or --decisions), matched by path, function,
kind and subject (the variable, or the type for an unnamed copy). The run
never fails on findings; the dynamic half (profiles) will rank them.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import sys

import check_const_restrict as reading
import check_loop_branches as branches
import check_state_machines as hunt
import clang_ast
from clang_ast import Node
import project

KINDS: tuple[str, ...] = ("copy_in_loop", "copy_then_read", "by_value_read", "allocation_in_loop",
                          "growth_without_reserve", "returned_into_existing")
DECISIONS: tuple[str, ...] = ("apply", "keep", "false_shape")
LOOPS: frozenset[str] = frozenset({"ForStmt", "WhileStmt", "DoStmt", "CXXForRangeStmt"})
COUNTED_LOOPS: frozenset[str] = frozenset({"ForStmt", "CXXForRangeStmt"})
# Standard types whose copy allocates or copies an owned buffer.
OWNING_STANDARD: re.Pattern[str] = re.compile(
    r"\b(basic_string|string|wstring|u8string|u16string|u32string|vector|deque|list|forward_list|map|multimap|"
    r"set|multiset|unordered_map|unordered_multimap|unordered_set|unordered_multiset|function|path)\b")
# Standard containers and strings: what returned_into_existing is about.
CONTAINERS: re.Pattern[str] = re.compile(
    r"\b(basic_string|string|wstring|u8string|u16string|u32string|vector|deque|list|forward_list|map|multimap|"
    r"set|multiset|unordered_map|unordered_multimap|unordered_set|unordered_multiset)\b")
# Handles and views: copying one copies a pointer (and maybe a count), never what it refers to.
NON_OWNING: frozenset[str] = frozenset({"shared_ptr", "weak_ptr", "unique_ptr", "span", "string_view",
                                        "basic_string_view", "reference_wrapper", "function_ref"})
# Standard holders that own what they hold by value.
HOLDERS: frozenset[str] = frozenset({"optional", "array", "pair", "tuple", "variant"})
RESERVABLE: re.Pattern[str] = re.compile(r"\b(basic_string|string|wstring|u8string|vector|unordered_\w+)\b")
BRANCHES: frozenset[str] = frozenset({"IfStmt", "SwitchStmt", "ConditionalOperator", "WhileStmt", "DoStmt",
                                      "ForStmt", "CXXForRangeStmt"})
GROWTH_CALLS: frozenset[str] = frozenset({"push_back", "emplace_back", "append", "insert", "emplace"})
ALLOCATING_CALLS: frozenset[str] = frozenset({"make_unique", "make_shared", "allocate_shared"})
COPY_PARAMETER: re.Pattern[str] = re.compile(r"^const (.+?) ?&$")
MOVE_PARAMETER: re.Pattern[str] = re.compile(r"^(.+?) ?&&$")


def without_arguments(type_text: str) -> str:
    """A type without its template arguments at any depth ("std::vector<int>::iterator" -> "std::vector::iterator")."""
    kept: list[str] = []
    depth: int = 0
    character: str
    for character in type_text:
        if character == "<":
            depth += 1
        elif character == ">":
            depth -= 1
        elif depth == 0:
            kept.append(character)
    result: str = "".join(kept)
    return result


def outer_arguments(type_text: str) -> str:
    """The text of a type's outermost template arguments ("std::optional<Item>" -> "Item"), or ""."""
    opening: int = type_text.find("<")
    closing: int = type_text.rfind(">")
    if opening < 0 or closing <= opening:
        return ""
    result: str = type_text[opening + 1:closing]
    return result


def base_name(type_text: str) -> str:
    """A type's own name: without qualifiers, namespaces and template arguments ("std::vector<int>" -> "vector",
    "std::vector<int>::const_iterator" -> "const_iterator")."""
    text: str = without_arguments(type_text).replace("const ", "").replace("volatile ", "").strip()
    result: str = text.split("::")[-1].strip()
    return result


def value_type(type_text: str) -> bool:
    """A declared type that holds its object: not a pointer or reference (whatever qualifies the pointer)."""
    text: str = type_text.strip()
    while text.endswith(" const") or text.endswith("*const"):
        text = text[:-len("const")].rstrip()
    result: bool = bool(text) and not text.endswith("*") and not text.endswith("&") and "(" not in text
    return result


def copied_type(node: Node) -> str:
    """The type a copy constructor or copy assignment copies, or ""."""
    if node.kind == "CXXConstructExpr" and len(node.arguments) == 1 and node.parameters:
        match: re.Match[str] | None = COPY_PARAMETER.match(node.parameters[0])
        if match is not None and base_name(match.group(1)) == base_name(node.name):
            return match.group(1)
    if node.kind == "CXXOperatorCallExpr" and node.opcode == "=" and len(node.parameters) == 2:
        match = COPY_PARAMETER.match(node.parameters[1])
        if match is not None:
            return match.group(1)
    return ""


def in_loop(parents: list[Node], node: Node, start: int) -> Node | None:
    """The innermost loop around `node` whose repeating part holds it, looking no further out than
    parents[start] (the function): a for statement's init and a range-for's range run once."""
    found: Node | None = None
    index: int
    for index in range(start, len(parents)):
        parent: Node = parents[index]
        if parent.kind not in LOOPS:
            continue
        child: Node = parents[index + 1] if index + 1 < len(parents) else node
        if child is parent.roles.get("init") or (parent.kind == "CXXForRangeStmt"
                                                 and child is parent.roles.get("range")):
            continue
        found = parent
    return found




@dataclass(frozen=True)
class Finding:
    kind: str
    path: str
    line: int
    offset: int
    function: str
    qualified: str
    subject: str            # the variable, or the copied type for an unnamed copy
    type: str
    in_loop: bool
    detail: str
    signature_words: bool


@dataclass
class FunctionState:
    node: Node
    name: str
    index: int                                          # the function's position in the parents list
    declared: dict[str, int] = field(default_factory=dict)      # variable identity -> declaration offset
    names: dict[str, str] = field(default_factory=dict)         # variable identity -> name
    types: dict[str, str] = field(default_factory=dict)         # variable identity -> declared type
    reserves: dict[str, list[int]] = field(default_factory=dict)  # root key -> offsets of reserve calls
    growth: list[tuple[str, str, Node, Node]] = field(default_factory=list)  # (root key, label, call, loop)


@dataclass
class Tracked:
    """A variable whose uses decide a candidate: a copied local, a by-value parameter or a local in a loop."""
    node: Node
    kind: str               # the candidate kind it may become
    state: FunctionState    # its function
    loop: Node | None       # the loop it was declared or copied in
    verdict: str = reading.READ
    moved: bool = False


class UnitAnalysis:
    def __init__(self, root: Path, source: clang_ast.SourceText) -> None:
        self.root_text: str = root.as_posix().lower().rstrip("/") + "/"
        self.source: clang_ast.SourceText = source
        self.findings: list[dict] = []
        self.files: set[str] = set()
        self.bound_functions: set[str] = set()
        self.record_fields: dict[str, list[str]] = {}   # record base name -> its fields' types
        self.field_types: dict[str, str] = {}           # field name -> type (last seen), for growth on fields
        self.tracked: dict[str, Tracked] = {}
        self.functions: dict[int, FunctionState] = {}   # id() of a function node -> its state

    def relative(self, file: str) -> str:
        if file.lower().startswith(self.root_text):
            return file[len(self.root_text):]
        return file

    def declaration(self, top: Node) -> None:
        self.tracked = {}
        self.functions = {}
        clang_ast.walk(top, self.visit)
        self.finish()

    def function_of(self, parents: list[Node]) -> FunctionState | None:
        index: int
        for index in range(len(parents) - 1, -1, -1):
            parent: Node = parents[index]
            if parent.kind in clang_ast.FUNCTIONS and parent.definition:
                state: FunctionState | None = self.functions.get(id(parent))
                if state is None:
                    state = FunctionState(parent, branches.enclosing_function(parents[:index + 1]), index)
                    self.functions[id(parent)] = state
                return state
            if parent.kind == "LambdaExpr":
                return None
        return None

    def emit(self, kind: str, node: Node, state: FunctionState, subject: str, type_text: str, loop: Node | None,
             detail: str) -> None:
        function: Node = state.node
        self.findings.append(asdict(Finding(
            kind, self.relative(node.file), self.source.line(node.file, node.begin), node.begin, state.name,
            function.qualified or function.name, subject, type_text, loop is not None, detail,
            reading.function_signature_words(function, self.source))))

    def visit(self, node: Node, parents: list[Node]) -> None:
        if node.file:
            self.files.add(node.file)
        kind: str = node.kind
        if kind == "CXXRecordDecl" and node.definition and node.name:
            self.record_fields[node.name] = [child.type for child in node.children if child.kind == "FieldDecl"]
        if kind == "FieldDecl" and node.name:
            self.field_types[node.name] = node.type
        if kind in clang_ast.FUNCTIONS:
            if kind == "CXXMethodDecl" and reading.function_signature_words(node, self.source):
                self.bound_functions.add(node.qualified or node.name)
            return
        if kind == "DeclRefExpr" and node.target_kind in clang_ast.FUNCTIONS:
            parent: Node | None = parents[-1] if parents else None
            if parent is None or parent.kind not in clang_ast.CALLS or parent.roles.get("callee") is not node:
                self.bound_functions.add(node.qualified or node.name)
            return
        state: FunctionState | None = self.function_of(parents)
        if state is None or node.begin < 0:
            return
        loop: Node | None = in_loop(parents, node, state.index)
        if kind in ("VarDecl", "ParmVarDecl") and node.identity:
            state.declared[node.identity] = node.begin
            state.names[node.identity] = node.name
            state.types[node.identity] = node.type
            self.declare(node, parents, state, loop)
            return
        if kind == "DeclRefExpr" and node.target in self.tracked:
            self.use(node, parents)
            return
        if kind in ("CXXConstructExpr", "CXXOperatorCallExpr"):
            self.construction(node, parents, state, loop)
        if kind == "CXXNewExpr" and loop is not None:
            self.emit("allocation_in_loop", node, state, "new", "", loop, "a new expression in the loop body")
        if kind == "CallExpr" and loop is not None and node.name in ALLOCATING_CALLS:
            self.emit("allocation_in_loop", node, state, node.name, "", loop, node.name + " in the loop body")
        if kind == "CXXMemberCallExpr" and "object" in node.roles:
            self.member_call(node, parents, state, loop)
        if kind == "CXXOperatorCallExpr" and node.opcode == "+=" and node.arguments and loop is not None:
            self.growth(node.arguments[0], "+=", node, parents, state, loop)

    def declare(self, node: Node, parents: list[Node], state: FunctionState, loop: Node | None) -> None:
        if not value_type(node.type) or node.type.startswith("static "):
            return
        if node.kind == "ParmVarDecl":
            if node.name:
                self.tracked[node.identity] = Tracked(node, "by_value_read", state, None)
            return
        holder: Node | None = parents[-1] if parents else None
        if holder is not None and holder.kind == "CXXForRangeStmt" and holder.roles.get("variable") is node:
            self.emit("copy_in_loop", node, state, node.name, node.type, holder, "range-for element taken by value")
            return
        initializer: Node | None = node.roles.get("init")
        copied: str = copied_type(initializer) if initializer is not None else ""
        if copied:
            self.tracked[node.identity] = Tracked(node, "copy_then_read", state, loop)
        elif loop is not None:
            self.tracked[node.identity] = Tracked(node, "allocation_in_loop", state, loop)

    def use(self, node: Node, parents: list[Node]) -> None:
        tracked: Tracked = self.tracked[node.target]
        index: int = len(parents) - 1
        while index >= 0 and parents[index].kind == "ParenExpr":
            index -= 1
        if index >= 0 and parents[index].kind == "ReturnStmt":
            # Returned by name: moved out, not only read.
            tracked.verdict = reading.WRITE
            tracked.moved = True
            return
        step: reading.Step = reading.use_verdict(node, parents, "&", tracked.state.node)
        if "moved or forwarded" in step.reason:
            tracked.moved = True
        if reading.RANK[step.verdict] > reading.RANK[tracked.verdict]:
            tracked.verdict = step.verdict

    def construction(self, node: Node, parents: list[Node], state: FunctionState, loop: Node | None) -> None:
        copied: str = copied_type(node)
        parent: Node | None = parents[-1] if parents else None
        if copied and loop is not None and (parent is None or parent.kind != "VarDecl"):
            label: str = "copy assignment" if node.kind == "CXXOperatorCallExpr" else "copy construction"
            self.emit("copy_in_loop", node, state, copied, copied, loop, label)
            return
        if node.kind == "CXXOperatorCallExpr" and node.opcode == "=" and len(node.parameters) == 2 and (
                len(node.arguments) == 2):
            moved: re.Match[str] | None = MOVE_PARAMETER.match(node.parameters[1])
            source: Node = clang_ast.stripped(node.arguments[1])
            if moved is not None and source.kind in ("CallExpr", "CXXMemberCallExpr") and (
                    source.name not in ("move", "forward", "exchange")) and self.kept_storage(
                    node.arguments[0], state, loop):
                target: str = " ".join(self.source.node_text(node.arguments[0]).split())[:120]
                self.emit("returned_into_existing", node, state, target, moved.group(1), loop,
                          f"assigned the result of {source.name or 'a call'}")

    def kept_storage(self, target: Node, state: FunctionState, loop: Node | None) -> bool:
        """Whether an assignment's target keeps its storage between assignments: a member (it outlives the call),
        or, in a loop, a variable declared before the loop."""
        key: str = self.root_key(target)
        if key.startswith("field:"):
            return True
        if not key or loop is None:
            return False
        declared_at: int = state.declared.get(key.split(":", 1)[1], -1)
        result: bool = 0 <= declared_at < loop.begin
        return result

    def member_call(self, node: Node, parents: list[Node], state: FunctionState, loop: Node | None) -> None:
        key: str = self.root_key(node.roles["object"])
        if not key:
            return
        if node.name == "reserve":
            state.reserves.setdefault(key, []).append(node.begin)
            return
        if node.name in GROWTH_CALLS and loop is not None:
            self.growth(node.roles["object"], node.name, node, parents, state, loop)

    def root_key(self, target: Node) -> str:
        root: Node = branches.root_object(target)
        identity: str = branches.referenced_id(root)
        if identity:
            return "variable:" + identity
        field_name: str = branches.this_field(root)
        if field_name:
            return "field:" + field_name
        return ""

    def growth(self, target: Node, label: str, call: Node, parents: list[Node], state: FunctionState,
               loop: Node) -> None:
        """Growth whose final size the loop's count gives: every iteration grows (no branch between the loop and
        the call) and the loop is counted."""
        key: str = self.root_key(target)
        if not key or loop.kind not in COUNTED_LOOPS:
            return
        inside: bool = False
        parent: Node
        for parent in parents:
            if parent is loop:
                inside = True
            elif inside and parent.kind in BRANCHES:
                return
        state.growth.append((key, label, call, loop))

    def finish(self) -> None:
        tracked: Tracked
        for tracked in self.tracked.values():
            node: Node = tracked.node
            state: FunctionState = tracked.state
            type_text: str = node.type
            if tracked.kind in ("copy_then_read", "by_value_read") and tracked.verdict == reading.READ:
                detail: str = "copied, then only read" if tracked.kind == "copy_then_read" else "only read"
                self.emit(tracked.kind, node, state, node.name, type_text, tracked.loop, detail)
            elif tracked.kind == "copy_then_read" and tracked.loop is not None:
                self.emit("copy_in_loop", node, state, node.name, type_text, tracked.loop, "copied into a local")
            elif tracked.kind == "allocation_in_loop" and not tracked.moved:
                self.emit("allocation_in_loop", node, state, node.name, type_text, tracked.loop,
                          "declared in the loop body")
        function_state: FunctionState
        for function_state in self.functions.values():
            self.finish_growth(function_state)

    def finish_growth(self, state: FunctionState) -> None:
        reported: set[tuple[str, int]] = set()
        key: str
        label: str
        call: Node
        loop: Node
        for key, label, call, loop in state.growth:
            if (key, id(loop)) in reported:
                continue
            reported.add((key, id(loop)))
            identity: str = key.split(":", 1)[1]
            declared_at: int = state.declared.get(identity, -1)
            if key.startswith("variable:"):
                type_text: str = state.types.get(identity, "")
                if declared_at < 0 or declared_at >= loop.begin:
                    continue
            else:
                type_text = self.field_types.get(identity, "")
            if not value_type(type_text) or RESERVABLE.search(type_text) is None:
                continue
            reserves: list[int] = state.reserves.get(key, [])
            if any(offset < loop.begin for offset in reserves):
                continue
            subject: str = identity if key.startswith("field:") else state.names.get(identity, identity)
            self.emit("growth_without_reserve", call, state, subject, type_text, loop, label + " in a counted loop")


# --- one unit, in a worker process ------------------------------------------------------

@dataclass
class UnitResult:
    findings: list[dict] = field(default_factory=list)
    bound_functions: list[str] = field(default_factory=list)
    record_fields: dict[str, list[str]] = field(default_factory=dict)


def analyse_unit(job: branches.UnitJob) -> UnitResult:
    root: Path = Path(job.root)
    analysis: UnitAnalysis = UnitAnalysis(root, clang_ast.SourceText())
    top: Node
    for top in clang_ast.read_unit(clang_ast.unit_command(job.entry), branches.KeptFile(job), job.reader):
        analysis.declaration(top)
    seen: str
    for seen in analysis.files:
        branches.DONE.add(seen.lower())
    result: UnitResult = UnitResult(analysis.findings, sorted(analysis.bound_functions), analysis.record_fields)
    return result


# --- owning types, decisions and the run ------------------------------------------------

def owning_records(record_fields: dict[str, list[str]]) -> set[str]:
    """First-party records that own something: a field of an owning standard type or of an owning record,
    to a fixed point."""
    owning: set[str] = set()
    changed: bool = True
    while changed:
        changed = False
        name: str
        types: list[str]
        for name, types in record_fields.items():
            if name in owning:
                continue
            type_text: str
            for type_text in types:
                if owns(type_text, owning):
                    owning.add(name)
                    changed = True
                    break
    return owning


def owns(type_text: str, records: set[str]) -> bool:
    """Whether a type held by value owns storage: an owning standard type or record named in it (a pointer or
    reference to one owns nothing)."""
    if not value_type(type_text):
        return False
    base: str = base_name(type_text)
    if base in NON_OWNING or "iterator" in base:
        return False
    if OWNING_STANDARD.fullmatch(base) is not None or base in records:
        return True
    if base in HOLDERS:
        # A holder owns what it holds by value: optional<std::string>, pair<int, std::vector<int>>.
        argument: str
        for argument in split_arguments(outer_arguments(type_text)):
            if owns(argument, records):
                return True
    return False


def split_arguments(text: str) -> list[str]:
    """Template arguments split at their top-level commas."""
    parts: list[str] = []
    current: list[str] = []
    depth: int = 0
    character: str
    for character in text:
        if character in "<([":
            depth += 1
        elif character in ">)]":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    if "".join(current).strip():
        parts.append("".join(current).strip())
    return parts


def relevant(record: dict, records: set[str]) -> bool:
    kind: str = record["kind"]
    if kind in ("allocation_in_loop",) and not record["type"]:
        return True         # a new expression or make_unique: allocation by itself
    if kind == "growth_without_reserve":
        return True
    if kind == "returned_into_existing":
        return owns(record["type"], records) and CONTAINERS.search(record["type"]) is not None
    return owns(record["type"], records)


@dataclass(frozen=True)
class Decision:
    path: str
    function: str
    kind: str
    subject: str
    decision: str
    reason: str


def load_decisions(path: Path | None) -> list[Decision]:
    if path is None or not path.exists():
        return []
    data: dict = json.loads(path.read_text(encoding="utf-8"))
    decisions: list[Decision] = []
    item: dict
    for item in data.get("decisions", []):
        decision: str = item.get("decision", "")
        kind: str = item.get("kind", "")
        if decision not in DECISIONS or kind not in KINDS:
            raise ValueError(f"{path}: unknown decision {decision!r} or kind {kind!r} for {item.get('path')}")
        decisions.append(Decision(item["path"], item.get("function", ""), kind, item["subject"], decision,
                                  item.get("reason", "")))
    return decisions


def decided(record: dict, decisions: list[Decision]) -> Decision | None:
    key: tuple[str, str, str, str] = (record["path"], record["function"], record["kind"], record["subject"])
    decision: Decision
    for decision in decisions:
        if (decision.path, decision.function, decision.kind, decision.subject) == key:
            return decision
    return None


def status_of(record: dict, bound: set[str], decision: Decision | None) -> str:
    if decision is not None:
        return "decided"
    if record["kind"] != "by_value_read":
        return "open"
    function: str = record["qualified"]
    bare: str = function.split("::")[-1]
    if record["signature_words"] or function in bound or bare in bound or bare == "main":
        return "signature_bound"
    return "open"


def summary(records: list[dict], units: int) -> str:
    lines: list[str] = []
    statuses: Counter[tuple[str, str]] = Counter()
    loops: Counter[str] = Counter()
    areas: dict[str, Counter[str]] = {}
    record: dict
    for record in records:
        statuses[(record["kind"], record["status"])] += 1
        if record["status"] == "open":
            if record["in_loop"]:
                loops[record["kind"]] += 1
            areas.setdefault(hunt.area_of(record["path"]), Counter())[record["kind"]] += 1
    lines.append(f"copies: {units} units, {len(records)} candidates")
    kind: str
    for kind in KINDS:
        lines.append(f"{kind}: " + ", ".join(f"{status} {statuses[(kind, status)]}"
                                             for status in ("open", "signature_bound", "decided"))
                     + f" (open in a loop {loops[kind]})")
    lines.append("")
    lines.append("| Area | " + " | ".join(KINDS) + " |")
    lines.append("|---|" + "---|" * len(KINDS))
    area: str
    for area in sorted(areas):
        counts: Counter[str] = areas[area]
        lines.append(f"| {area} | " + " | ".join(str(counts[name]) for name in KINDS) + " |")
    result: str = "\n".join(lines) + "\n"
    return result


def queue_order(record: dict) -> tuple[int, int, int, str, int]:
    status_rank: dict[str, int] = {"open": 0, "signature_bound": 1, "decided": 2}
    return (status_rank[record["status"]], 0 if record["in_loop"] else 1, KINDS.index(record["kind"]),
            record["path"], record["line"])


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__,
                                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(project.current().source_root()), help="the product checkout")
    parser.add_argument("--build-dir", required=True, help="a configured ninja tree (its compile database)")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--paths", nargs="*", default=[], help="only units under these checkout-relative paths")
    parser.add_argument("--decisions", help="recorded decisions (default: the project's [copies] decisions)")
    parser.add_argument("--json", dest="json_path", help="write the review queue here")
    parser.add_argument("--report", help="write the short report here")
    args: argparse.Namespace = parser.parse_args()
    project.current().require_state_machines()
    root: Path = Path(args.root).resolve()
    decisions_path: Path | None = Path(args.decisions) if args.decisions else None
    configured: str = project.current().copies_decisions
    if decisions_path is None and configured:
        decisions_path = root / configured
    decisions: list[Decision] = load_decisions(decisions_path)
    build_dir: Path = Path(args.build_dir)
    entries: list[dict] = hunt.compile_database(build_dir)
    jobs: list[branches.UnitJob] = branches.unit_jobs(entries, root, args.paths)
    if not jobs:
        parser.error("no first-party translation unit selected; an empty scope is not a pass")
    hunt.generate_headers(build_dir)
    unique: dict[tuple[str, int, str, str], dict] = {}
    bound: set[str] = set()
    record_fields: dict[str, list[str]] = {}
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        result: UnitResult
        for result in pool.map(analyse_unit, jobs, chunksize=4):
            bound.update(result.bound_functions)
            record_fields.update(result.record_fields)
            record: dict
            for record in result.findings:
                unique[(record["path"], record["offset"], record["kind"], record["subject"])] = record
    records: set[str] = owning_records(record_fields)
    queue: list[dict] = []
    for record in unique.values():
        if not relevant(record, records):
            continue
        decision: Decision | None = decided(record, decisions)
        record["status"] = status_of(record, bound, decision)
        if decision is not None:
            record["decision"] = decision.decision
            record["decision_reason"] = decision.reason
        queue.append(record)
    queue.sort(key=queue_order)
    report: str = summary(queue, len(jobs))
    sys.stdout.write(report)
    if args.json_path:
        Path(args.json_path).write_text(json.dumps(queue, indent=1) + "\n", encoding="utf-8")
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
