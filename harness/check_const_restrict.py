"""Read-only and restrict candidates (Phase E, E3).

Two queues, because read-only and no-alias are different promises.

const     A pointer or lvalue reference to a non-const object (a parameter,
          a local or a range-for variable) through which the function only
          reads. Declaring the object const is always safe, and the compiler
          checks it: a candidate that is wrong fails to compile, never
          misbehaves.
restrict  A function with a loop that writes through one pointer parameter
          (an output) beside other pointer parameters it reads (inputs), with
          no write or escape that would let the output reach the inputs.
          `restrict` is a promise the compiler cannot check, so a candidate
          is only a place to review: the owner's rule applies it wherever
          the promise can be shown, each use with a written overlap rule and
          an overlap assertion active in debug and sanitizer builds; public
          signatures wait for review. Span kernels (an output std::span
          beside input spans) are listed apart. The form that works for
          them is a wrapper that keeps the span signature, asserts the
          overlap rule and calls an internal-linkage helper whose pointer
          parameters are restricted: compilers act on restrict parameters,
          and a restricted local pointer is not relied on. Spell the
          qualifier as bfft's macro does: `__restrict` for MSVC proper
          (_MSC_VER without __clang__), `__restrict__` for GCC and Clang,
          clang-cl included; macOS's <sys/cdefs.h> defines `__restrict` as
          nothing in C++.

How a use is read, from the shared AST (clang_ast.py). A use of a pointer
follows its value up the expression: `*p`, `p[i]` and `p->m` reach the
object; comparisons, tests and `++p` only read the pointer; `p + n` is the
same pointer moved. A use of the object (or of a reference) is a write when
it is assigned, incremented, passed to a non-const lvalue or rvalue
reference parameter, iterated by a non-const reference, or calls a non-const
member function. Const is deep: a write through a pointer member the object
holds (an owned unique_ptr, say) is a write to the object, because the house
style asks whether the function changes what the object owns, not only its
bytes. It escapes when its address, or the pointer itself, is
stored, returned, cast, or passed where the parameter is not a pointer or
reference to const (or is unknown). Anything the reader does not recognise
escapes: the queue errs towards missing a candidate, not inventing one.

A function whose signature is fixed by something else is listed apart
(status signature_bound): a virtual function or override, a function whose
address is taken (a callback), and main.

Recorded decisions sit in a JSON file (the project's [const_restrict]
decisions, a path in the product checkout, or --decisions): each matches by
path, function, variable and kind, so renaming either raises it again. The
run never fails on findings; it is a review aid.
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

import check_loop_branches as branches
import check_state_machines as hunt
import clang_ast
from clang_ast import Node
import project

KINDS: tuple[str, ...] = ("const", "restrict", "restrict_span")
DECISIONS: tuple[str, ...] = ("apply", "keep", "false_shape")
LOOPS: frozenset[str] = frozenset({"ForStmt", "WhileStmt", "DoStmt", "CXXForRangeStmt"})
CASTS: frozenset[str] = frozenset({"CStyleCastExpr", "CXXStaticCastExpr", "CXXDynamicCastExpr",
                                   "CXXReinterpretCastExpr", "CXXConstCastExpr", "CXXFunctionalCastExpr"})
CONDITIONS: frozenset[str] = frozenset({"IfStmt", "WhileStmt", "DoStmt", "ForStmt", "SwitchStmt"})
UNEVALUATED: frozenset[str] = frozenset({"UnaryExprOrTypeTraitExpr", "CXXTypeidExpr", "CXXNoexceptExpr"})
COMPARISONS: frozenset[str] = frozenset({"==", "!=", "<", ">", "<=", ">=", "&&", "||", "<=>"})
ASSIGNMENTS: frozenset[str] = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="})
# Element types that may alias any object: restrict changes most for these.
CHAR_LIKE: frozenset[str] = frozenset({"char", "signed char", "unsigned char", "std::byte", "byte", "std::uint8_t",
                                       "uint8_t", "std::int8_t", "int8_t", "void"})
# Qualifiers on the pointer itself (after the last * or &), not on what it points to.
OWN_QUALIFIERS: re.Pattern[str] = re.compile(r"\s*(const|volatile|__restrict__|__restrict|restrict)\s*$")
SIGNATURE_WORDS: re.Pattern[str] = re.compile(r"\b(virtual|override|final)\b")
# Verdicts of a use, weakest first; a variable takes its worst use's.
READ: str = "read"
WRITE: str = "write"
ESCAPE: str = "escape"
RANK: dict[str, int] = {READ: 0, WRITE: 1, ESCAPE: 2}


# --- types ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Indirection:
    sigil: str          # "*" or "&"
    pointee: str        # the type pointed to, as written ("const Buffer", "float")
    restricted: bool    # the pointer already carries restrict


def indirection(type_text: str) -> Indirection | None:
    """A single-level pointer or lvalue reference to an object: not a pointer to a pointer, a function
    pointer, an array reference or an rvalue reference."""
    text: str = type_text.strip()
    restricted: bool = "restrict" in text
    while True:
        match: re.Match[str] | None = OWN_QUALIFIERS.search(text)
        if match is None:
            break
        head: str = text[:match.start()].rstrip()
        if not head or head[-1] not in "*&":
            # The qualifier belongs to the type itself ("int const"), not to a pointer.
            break
        text = head
    if not text or text[-1] not in "*&" or text.endswith("&&"):
        return None
    pointee: str = text[:-1].rstrip()
    if not pointee or pointee[-1] in "*&)]" or "(" in pointee:
        return None
    result: Indirection = Indirection(text[-1], pointee, restricted)
    return result


def const_pointee(pointee: str) -> bool:
    words: str = " " + pointee + " "
    result: bool = pointee.startswith("const ") or pointee.endswith(" const") or (
        " const " in words and "<" not in pointee)
    return result


def accepts_const(type_text: str, sigil: str) -> bool:
    """Whether a destination of this type accepts a pointer (sigil *) or an object (sigil &) declared const:
    a pointer or reference to const, a bool test of a pointer, or (for an object) a copy."""
    text: str = type_text.strip()
    if not text:
        return False
    if sigil == "*" and text == "bool":
        return True
    if text.endswith("&&"):
        return False
    target: Indirection | None = indirection(text)
    if target is None:
        # Not a pointer or reference: a pointer cannot go there unconverted; an object is copied.
        return sigil == "&" and not text.endswith("*") and not text.endswith("&")
    if sigil == "&" and target.sigil == "*":
        return False
    return const_pointee(target.pointee)


def element_type(pointee: str) -> str:
    """A pointee without const or volatile, for comparing element types."""
    words: list[str] = [word for word in pointee.split() if word not in ("const", "volatile")]
    result: str = " ".join(words)
    return result


def return_type(function_type: str) -> str:
    """The return type of a function type as clang writes it ("const int *(int, float) const")."""
    depth: int = 0
    index: int
    for index in range(len(function_type)):
        character: str = function_type[index]
        if character in "<[":
            depth += 1
        elif character in ">]":
            depth -= 1
        elif character == "(" and depth == 0:
            before: str = function_type[:index].rstrip()
            if before:
                return before
    return ""


def span_element(type_text: str) -> str | None:
    """A std::span parameter's element type ("const float"), or None."""
    text: str = type_text.strip()
    if text.startswith("const "):
        text = text[len("const "):]
    if not text.startswith("std::span<") or not text.endswith(">"):
        return None
    inner: str = text[len("std::span<"):-1]
    depth: int = 0
    index: int
    for index in range(len(inner)):
        if inner[index] == "<":
            depth += 1
        elif inner[index] == ">":
            depth -= 1
        elif inner[index] == "," and depth == 0:
            inner = inner[:index]
            break
    result: str = inner.strip()
    return result


# --- reading a use ---------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    verdict: str        # READ, WRITE or ESCAPE; "" to keep climbing
    mode: str           # when climbing: "pointer" (the expression is the pointer) or "object" (what it reaches)
    reason: str = ""


def call_argument(call: Node, child: Node) -> int:
    index: int
    for index in range(len(call.arguments)):
        if call.arguments[index] is child:
            return index
    return -1


def member_step(parent: Node, grandparent: Node | None) -> Step:
    """A member of the object: a method call decides by the method's constness; a data member is a part of
    the object, so the climb goes on."""
    if grandparent is not None and grandparent.kind == "CXXMemberCallExpr" and (
            grandparent.roles.get("callee") is parent):
        if grandparent.constant:
            return Step(READ, "")
        return Step(WRITE, "", f"calls non-const {parent.name}")
    return Step("", "object")


def pointer_step(parent: Node, child: Node, grandparent: Node | None, function: Node | None) -> Step:
    kind: str = parent.kind
    roles: dict[str, Node] = parent.roles
    if kind == "UnaryOperator":
        if parent.opcode == "*":
            return Step("", "object")
        if parent.opcode in ("!", "++", "--"):
            return Step(READ, "")
        return Step(ESCAPE, "", "its address is taken" if parent.opcode == "&" else "unary " + parent.opcode)
    if kind == "ArraySubscriptExpr":
        if roles.get("base") is child:
            return Step("", "object")
        return Step(READ, "")
    if kind == "MemberExpr":
        if parent.arrow and roles.get("object") is child:
            return member_step(parent, grandparent)
        return Step(ESCAPE, "", "member " + parent.name)
    if kind == "BinaryOperator":
        if parent.opcode in COMPARISONS or parent.opcode == ",":
            return Step(READ, "")
        if parent.opcode == "->*":
            return Step(ESCAPE, "", "reached through a member pointer")
        if parent.opcode in ("+", "-"):
            return Step("", "pointer")
        if parent.opcode == "=" and roles.get("left") is child:
            return Step(READ, "")
        return Step(ESCAPE, "", "stored by " + parent.opcode)
    if kind == "CompoundAssignOperator":
        if roles.get("left") is child:
            return Step(READ, "")
        return Step(ESCAPE, "", "stored by " + parent.opcode)
    if kind == "ConditionalOperator":
        if roles.get("condition") is child:
            return Step(READ, "")
        return Step("", "pointer")
    if kind in CONDITIONS and roles.get("condition") is child:
        return Step(READ, "")
    if kind == "VarDecl":
        if accepts_const(parent.type, "*"):
            return Step(READ, "")
        return Step(ESCAPE, "", f"stored in {parent.name} ({parent.type})")
    if kind == "ReturnStmt":
        returned: str = return_type(function.type) if function is not None else ""
        if accepts_const(returned, "*"):
            return Step(READ, "")
        return Step(ESCAPE, "", f"returned as {returned or 'an unknown type'}")
    if kind in clang_ast.CALLS:
        return call_step(parent, child, "*")
    if kind in UNEVALUATED or kind == "CompoundStmt":
        return Step(READ, "")
    if kind in CASTS:
        return Step(ESCAPE, "", "cast")
    if kind == "CXXDeleteExpr":
        return Step(ESCAPE, "", "deleted")
    return Step(ESCAPE, "", "used in " + kind)


def call_step(call: Node, child: Node, sigil: str) -> Step:
    """A pointer value (sigil *) or an object (sigil &) passed to a call."""
    if call.roles.get("callee") is child:
        if sigil == "&" and call.kind == "CallExpr":
            return Step(ESCAPE, "", "called")
        return Step(READ, "")
    index: int = call_argument(call, child)
    if index < 0:
        return Step(ESCAPE, "", "inside a call to " + (call.name or "a function"))
    parameter: str = call.parameters[index] if index < len(call.parameters) else ""
    writes: bool = call.writes[index] if index < len(call.writes) else True
    name: str = call.name or "a function"
    if sigil == "&" and call.kind == "CXXOperatorCallExpr" and index == 0 and not parameter:
        # A member operator's object: written unless the operator is const.
        if writes:
            return Step(WRITE, "", f"calls non-const {name}")
        return Step(READ, "")
    if not parameter:
        return Step(ESCAPE, "", f"passed to {name} (parameter unknown)")
    if sigil == "&" and parameter.endswith("&&"):
        return Step(WRITE, "", f"moved or forwarded to {name}")
    if sigil == "&" and writes:
        return Step(WRITE, "", f"passed to {name} as {parameter}")
    if accepts_const(parameter, sigil):
        return Step(READ, "")
    return Step(ESCAPE, "", f"passed to {name} as {parameter}")


def object_step(parent: Node, child: Node, grandparent: Node | None, function: Node | None) -> Step:
    kind: str = parent.kind
    roles: dict[str, Node] = parent.roles
    if kind == "MemberExpr" and roles.get("object") is child:
        # Through a pointer member too: const is asked of what the object reaches, not only of its bytes, so
        # a write through an owned pointer (a unique_ptr member) keeps the object from being called read-only.
        return member_step(parent, grandparent)
    if kind == "CXXOperatorCallExpr" and parent.arguments and parent.arguments[0] is child and (
            parent.opcode == "->" or (parent.opcode == "*" and len(parent.arguments) == 1)):
        return Step("", "object")
    if kind == "ArraySubscriptExpr":
        if roles.get("base") is child:
            return Step("", "object")
        return Step(READ, "")
    if kind == "UnaryOperator":
        if parent.opcode == "&":
            return Step(ESCAPE, "", "its address is taken")
        if parent.opcode in ("++", "--"):
            return Step(WRITE, "", parent.opcode)
        if parent.opcode == "*":
            # A pointer member dereferenced: what it reaches counts as the object's (see MemberExpr above).
            return Step("", "object")
        return Step(READ, "")
    if kind == "BinaryOperator":
        if parent.opcode == "=" and roles.get("left") is child:
            return Step(WRITE, "", "assigned")
        if parent.opcode in (".*", "->*"):
            return Step(ESCAPE, "", "reached through a member pointer")
        return Step(READ, "")
    if kind == "CompoundAssignOperator":
        if roles.get("left") is child:
            return Step(WRITE, "", parent.opcode)
        return Step(READ, "")
    if kind == "VarDecl":
        if accepts_const(parent.type, "&"):
            return Step(READ, "")
        return Step(ESCAPE, "", f"bound to {parent.name} ({parent.type})")
    if kind == "ReturnStmt":
        returned: str = return_type(function.type) if function is not None else ""
        if accepts_const(returned, "&"):
            return Step(READ, "")
        return Step(ESCAPE, "", f"returned as {returned or 'an unknown type'}")
    if kind in clang_ast.CALLS:
        return call_step(parent, child, "&")
    if kind == "CXXForRangeStmt" and roles.get("range") is child:
        variable: Node | None = roles.get("variable")
        element: Indirection | None = indirection(variable.type) if variable is not None else None
        if element is not None and element.sigil == "&" and not const_pointee(element.pointee):
            return Step(WRITE, "", "iterated by non-const reference")
        return Step(READ, "")
    if kind == "ConditionalOperator":
        if roles.get("condition") is child:
            return Step(READ, "")
        return Step("", "object")
    if kind in CONDITIONS and roles.get("condition") is child:
        return Step(READ, "")
    if kind in UNEVALUATED or kind in ("CompoundStmt", "CXXThrowExpr", "InitListExpr"):
        return Step(READ, "")
    if kind in ("CStyleCastExpr", "CXXStaticCastExpr", "CXXFunctionalCastExpr"):
        # A value conversion of the object (int(x), static_cast<float>(x)). A cast to a non-const reference
        # would not compile once the object is const, so the compiler catches the rare exception.
        return Step(READ, "")
    if kind in CASTS:
        return Step(ESCAPE, "", "cast")
    return Step(ESCAPE, "", "used in " + kind)


def use_verdict(use: Node, parents: list[Node], sigil: str, function: Node | None) -> Step:
    """What one use of a candidate does: climbs from the use until a step decides."""
    child: Node = use
    mode: str = "pointer" if sigil == "*" else "object"
    index: int = len(parents) - 1
    while index >= 0:
        parent: Node = parents[index]
        if parent.kind == "ParenExpr":
            child = parent
            index -= 1
            continue
        grandparent: Node | None = parents[index - 1] if index > 0 else None
        step: Step
        if mode == "pointer":
            step = pointer_step(parent, child, grandparent, function)
        else:
            step = object_step(parent, child, grandparent, function)
        if step.verdict:
            return step
        mode = step.mode
        child = parent
        index -= 1
    return Step(ESCAPE, "", "outside any statement")


# --- one declaration --------------------------------------------------------------------

@dataclass
class Candidate:
    node: Node                  # the ParmVarDecl or VarDecl
    function: Node
    function_name: str
    shape: Indirection
    parameter: bool
    verdict: str = READ
    reason: str = ""
    reason_line: int = 0
    uses: int = 0
    written_in_loop: bool = False


@dataclass(frozen=True)
class ConstFinding:
    kind: str                   # "const"
    path: str
    line: int
    offset: int
    function: str
    qualified: str              # the function's qualified name, for signature-bound checks across units
    variable: str
    type: str
    suggested: str
    parameter: bool
    uses: int
    signature_words: bool       # the function's own declaration says virtual, override or final


@dataclass(frozen=True)
class RestrictFinding:
    kind: str                   # "restrict" or "restrict_span"
    path: str
    line: int
    offset: int
    function: str
    qualified: str
    outputs: list[str]
    inputs: list[str]
    aliasing: str               # "may_alias" (same element type or a char-like type) or "distinct_types"
    tier: str                   # "private" (src) or "public" (an include tree)
    signature_words: bool


def function_signature_words(function: Node, source: clang_ast.SourceText) -> bool:
    body: Node | None = None
    child: Node
    for child in function.children:
        if child.kind == "CompoundStmt":
            body = child
    end: int = body.begin if body is not None else function.end
    text: str = source.text(function.file, function.begin, end)
    return SIGNATURE_WORDS.search(text) is not None


def has_loop(node: Node) -> bool:
    if node.kind in LOOPS:
        return True
    child: Node
    for child in node.children:
        if has_loop(child):
            return True
    return False


def suggested_type(shape: Indirection) -> str:
    result: str = "const " + shape.pointee + " " + shape.sigil
    return result


class UnitAnalysis:
    """The candidates of one unit's kept declarations, and the functions whose signatures are bound."""

    def __init__(self, root: Path, source: clang_ast.SourceText) -> None:
        self.root_text: str = root.as_posix().lower().rstrip("/") + "/"
        self.source: clang_ast.SourceText = source
        self.findings: list[dict] = []
        self.files: set[str] = set()
        self.bound_functions: set[str] = set()      # qualified names: virtual, override, final, address taken
        self.candidates: dict[str, Candidate] = {}
        self.tracked: dict[str, Candidate] = {}     # pointer parameters to const too, for restrict
        self.functions: list[tuple[Node, str]] = []  # definitions in this declaration, with their names

    def relative(self, file: str) -> str:
        if file.lower().startswith(self.root_text):
            return file[len(self.root_text):]
        return file

    def declaration(self, top: Node) -> None:
        self.candidates = {}
        self.tracked = {}
        self.functions = []
        clang_ast.walk(top, self.visit)
        self.finish()

    def visit(self, node: Node, parents: list[Node]) -> None:
        if node.file:
            self.files.add(node.file)
        kind: str = node.kind
        if kind in clang_ast.FUNCTIONS:
            if node.definition:
                self.functions.append((node, branches.enclosing_function(parents + [node])))
            if kind == "CXXMethodDecl" and function_signature_words(node, self.source):
                self.bound_functions.add(node.qualified or node.name)
            return
        if kind == "DeclRefExpr" and node.target_kind in clang_ast.FUNCTIONS:
            parent: Node | None = parents[-1] if parents else None
            if parent is None or parent.kind not in clang_ast.CALLS or parent.roles.get("callee") is not node:
                self.bound_functions.add(node.qualified or node.name)
            return
        if kind in ("ParmVarDecl", "VarDecl") and node.identity and node.name:
            self.declare(node, parents)
            return
        if kind == "DeclRefExpr" and node.target in self.tracked:
            self.use(node, parents)

    def declare(self, node: Node, parents: list[Node]) -> None:
        function: Node | None = None
        parent: Node
        for parent in reversed(parents):
            if parent.kind in clang_ast.FUNCTIONS:
                function = parent
                break
            if parent.kind == "LambdaExpr":
                return
        if function is None or not function.definition:
            return
        shape: Indirection | None = indirection(node.type)
        if shape is None:
            return
        is_parameter: bool = node.kind == "ParmVarDecl"
        candidate: Candidate = Candidate(node, function, branches.enclosing_function(parents), shape, is_parameter)
        self.tracked[node.identity] = candidate
        if not const_pointee(shape.pointee):
            self.candidates[node.identity] = candidate

    def use(self, node: Node, parents: list[Node]) -> None:
        candidate: Candidate = self.tracked[node.target]
        candidate.uses += 1
        step: Step = use_verdict(node, parents, candidate.shape.sigil, candidate.function)
        if step.verdict == WRITE and any(parent.kind in LOOPS for parent in parents):
            candidate.written_in_loop = True
        if RANK[step.verdict] > RANK[candidate.verdict]:
            candidate.verdict = step.verdict
            candidate.reason = step.reason
            candidate.reason_line = self.source.line(node.file, node.begin) if node.begin >= 0 else 0

    def finish(self) -> None:
        candidate: Candidate
        for candidate in self.candidates.values():
            if candidate.verdict != READ:
                continue
            node: Node = candidate.node
            function: Node = candidate.function
            self.findings.append(asdict(ConstFinding(
                "const", self.relative(node.file), self.source.line(node.file, node.begin), node.begin,
                candidate.function_name, function.qualified or function.name, node.name, node.type,
                suggested_type(candidate.shape), candidate.parameter, candidate.uses,
                function_signature_words(function, self.source))))
        function: Node
        name: str
        for function, name in self.functions:
            self.restrict(function, name)

    def restrict(self, function: Node, name: str) -> None:
        if not has_loop(function):
            return
        pointers: list[Candidate] = [candidate for candidate in self.tracked.values()
                                     if candidate.function is function and candidate.parameter
                                     and candidate.shape.sigil == "*"]
        outputs: list[Candidate] = [candidate for candidate in pointers if candidate.verdict == WRITE
                                    and not candidate.shape.restricted]
        inputs: list[Candidate] = [candidate for candidate in pointers if candidate.verdict == READ]
        signature: bool = function_signature_words(function, self.source)
        if any(candidate.written_in_loop for candidate in outputs) and len(outputs) + len(inputs) >= 2:
            elements: list[str] = [element_type(candidate.shape.pointee) for candidate in outputs + inputs]
            self.add_restrict("restrict", function, name, [candidate.node.name for candidate in outputs],
                              [candidate.node.name for candidate in inputs], elements, len(outputs), signature)
        spans: list[tuple[str, str]] = []
        child: Node
        for child in function.children:
            if child.kind == "ParmVarDecl":
                element: str | None = span_element(child.type)
                if element is not None:
                    spans.append((child.name, element))
        span_outputs: list[str] = [span[0] for span in spans if not const_pointee(span[1])]
        if span_outputs and len(spans) >= 2:
            span_elements: list[str] = [element_type(span[1]) for span in spans
                                        if not const_pointee(span[1])]
            span_elements += [element_type(span[1]) for span in spans if const_pointee(span[1])]
            self.add_restrict("restrict_span", function, name, span_outputs,
                              [span[0] for span in spans if const_pointee(span[1])], span_elements,
                              len(span_outputs), signature)

    def add_restrict(self, kind: str, function: Node, name: str, outputs: list[str], inputs: list[str],
                     elements: list[str], output_count: int, signature: bool) -> None:
        aliasing: str = "distinct_types"
        index: int
        for index in range(output_count):
            other: int
            for other in range(len(elements)):
                if other != index and (elements[other] == elements[index] or elements[other] in CHAR_LIKE
                                       or elements[index] in CHAR_LIKE):
                    aliasing = "may_alias"
        path: str = self.relative(function.file)
        tier: str = "public" if path.startswith("include/") or "/include/" in path else "private"
        self.findings.append(asdict(RestrictFinding(
            kind, path, self.source.line(function.file, function.begin), function.begin, name,
            function.qualified or function.name, outputs, inputs, aliasing, tier, signature)))


# --- one unit, in a worker process ------------------------------------------------------

@dataclass
class UnitResult:
    findings: list[dict] = field(default_factory=list)
    bound_functions: list[str] = field(default_factory=list)


def analyse_unit(job: branches.UnitJob) -> UnitResult:
    root: Path = Path(job.root)
    analysis: UnitAnalysis = UnitAnalysis(root, clang_ast.SourceText())
    top: Node
    for top in clang_ast.read_unit(clang_ast.unit_command(job.entry), branches.KeptFile(job), job.reader):
        analysis.declaration(top)
    seen: str
    for seen in analysis.files:
        branches.DONE.add(seen.lower())
    result: UnitResult = UnitResult(analysis.findings, sorted(analysis.bound_functions))
    return result


# --- decisions, statuses and the run ----------------------------------------------------

@dataclass(frozen=True)
class Decision:
    path: str
    function: str
    variable: str
    kind: str
    decision: str
    reason: str


def finding_variable(record: dict) -> str:
    if record["kind"] == "const":
        return record["variable"]
    result: str = ",".join(record["outputs"])
    return result


def load_decisions(path: Path | None) -> list[Decision]:
    """Recorded decisions; a missing file means none yet."""
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
        decisions.append(Decision(item["path"], item.get("function", ""), item["variable"], kind, decision,
                                  item.get("reason", "")))
    return decisions


def decided(record: dict, decisions: list[Decision]) -> Decision | None:
    key: tuple[str, str, str, str] = (record["path"], record["function"], finding_variable(record), record["kind"])
    decision: Decision
    for decision in decisions:
        if (decision.path, decision.function, decision.variable, decision.kind) == key:
            return decision
    return None


def status_of(record: dict, bound: set[str], decision: Decision | None) -> str:
    if decision is not None:
        return "decided"
    function: str = record["qualified"]
    bare: str = function.split("::")[-1]
    # A reference the json reader could not qualify is bound by its bare name.
    if record["signature_words"] or function in bound or bare in bound or bare == "main":
        return "signature_bound"
    return "open"


def summary(records: list[dict], units: int) -> str:
    lines: list[str] = []
    statuses: Counter[tuple[str, str]] = Counter()
    areas: dict[str, Counter[str]] = {}
    aliasing: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    record: dict
    for record in records:
        statuses[(record["kind"], record["status"])] += 1
        if record["status"] == "open":
            areas.setdefault(hunt.area_of(record["path"]), Counter())[record["kind"]] += 1
            if record["kind"] != "const":
                aliasing[record["aliasing"]] += 1
                tiers[record["tier"]] += 1
    lines.append(f"const and restrict: {units} units, {len(records)} candidates")
    kind: str
    for kind in KINDS:
        lines.append(f"{kind}: " + ", ".join(f"{status} {statuses[(kind, status)]}"
                                             for status in ("open", "signature_bound", "decided")))
    lines.append("open restrict: " + ", ".join(f"{name} {count}" for name, count in sorted(aliasing.items()))
                 + "; " + ", ".join(f"{name} {count}" for name, count in sorted(tiers.items())))
    lines.append("")
    lines.append("| Area | " + " | ".join(KINDS) + " |")
    lines.append("|---|" + "---|" * len(KINDS))
    area: str
    for area in sorted(areas):
        counts: Counter[str] = areas[area]
        lines.append(f"| {area} | " + " | ".join(str(counts[name]) for name in KINDS) + " |")
    result: str = "\n".join(lines) + "\n"
    return result


def queue_order(record: dict) -> tuple[int, int, str, int]:
    """Open first; restrict by aliasing benefit and tier; then by place."""
    status_rank: dict[str, int] = {"open": 0, "signature_bound": 1, "decided": 2}
    kind_rank: int = {"restrict": 0, "restrict_span": 1, "const": 2}[record["kind"]]
    return status_rank[record["status"]], kind_rank, record["path"], record["line"]


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__,
                                                              formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(project.current().source_root()), help="the product checkout")
    parser.add_argument("--build-dir", required=True, help="a configured ninja tree (its compile database)")
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--paths", nargs="*", default=[], help="only units under these checkout-relative paths")
    parser.add_argument("--decisions", help="recorded decisions (default: the project's [const_restrict] decisions)")
    parser.add_argument("--json", dest="json_path", help="write the review queue here")
    parser.add_argument("--report", help="write the short report here")
    args: argparse.Namespace = parser.parse_args()
    project.current().require_state_machines()
    root: Path = Path(args.root).resolve()
    decisions_path: Path | None = Path(args.decisions) if args.decisions else None
    configured: str = project.current().const_restrict_decisions
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
    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        result: UnitResult
        for result in pool.map(analyse_unit, jobs, chunksize=4):
            bound.update(result.bound_functions)
            record: dict
            for record in result.findings:
                unique[(record["path"], record["offset"], record["kind"], finding_variable(record))] = record
    queue: list[dict] = []
    for record in unique.values():
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
