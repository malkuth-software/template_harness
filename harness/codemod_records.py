"""Encapsulate result records by codemod: named factories and queries over private fields.

The house form for a result (HOUSE_STYLE.md, "Types and values"): a record
whose fields must agree is built through named factories and read through
named queries, so no caller can assemble a contradictory value; inside, a
std::variant holds one alternative per state. This tool does the mechanical
half over a whole tree: it rewrites every build and read of the records a
spec names, then makes their fields private. Switching a record's inside to
std::variant is afterwards an edit in one place, its header.

    python3 -B harness/codemod_records.py --spec SPEC.toml --root CHECKOUT
        --compile-database compile_commands.json [--jobs N] [--report FILE.md]
        [--json FILE.json] [--apply]

Without --apply nothing is written: the report counts the sites by record and
form, and lists the sites left for a hand edit with the reason.

The pipeline:

  reader       clang_ast.read_unit over each first-party unit of the compile
               database (libclang, or the JSON dump: HARNESS_AST_READER).
  classifier   each use of a record. A field read maps onto its query. A
               local that is only filled in by assignments and returned is
               followed statement by statement (straight-line code, blocks
               and if/else; nothing else) to the value each `return` gives,
               and that value picks one factory; an aggregate
               initialization picks one the same way. Anything else is a
               hand site: a write to a record that is also read or passed,
               an std::optional field used as a whole, a value whose state
               cannot be told from the source.
  generator    each record's class: its factories, its queries, and one
               private constructor over the fields, which keep their order
               and types; a field's comment moves to its query.
  rewriter     byte-range edits on each file read as bytes (a CRLF checkout
               keeps its line ends). A file whose edits overlap is refused
               whole: it is left as it was and its sites are reported.
  privatise    the record's definition is replaced last. A site the reader
               never saw (a file outside the compile database, such as an
               Objective-C++ or Linux adapter on a Windows tree) then no
               longer compiles, so CI on that host finds it.

The spec (TOML), one [[record]] table per record:

  name              the record's qualified name ("gui_forms::DocumentRequestResult")
  selector          the field whose value picks the state ("status"), or one
                    field inside it ("status.error")
  selector_type     for a dotted selector: the inner field's type
  selector_default  for a dotted selector: the inner field's default value
  queries           per field: "copy" (returned by value), "reference" (a
                    const reference) or "pointer" (a const pointer, null when
                    the state has none: an empty std::optional, or a field
                    that only the listed factories set)
  [[record.factory]]
    name            the factory's name
    selector        the selector value it builds ("DocumentViewStatus::success"),
                    or "other": any value no other factory names, which the
                    factory then takes as its first parameter
    fields          the fields it takes, in parameter order (an std::optional
                    field is taken as its value); the others keep their defaults
    comment         optional; written above the factory
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
import sys
import time
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clang_ast  # noqa: E402
from clang_ast import Node  # noqa: E402

OTHER: str = "other"
QUERY_FORMS: tuple[str, ...] = ("copy", "reference", "pointer")
OPTIONAL_PREFIXES: tuple[str, ...] = ("std::optional<", "optional<")
# Member calls that change a field; any other call on a field reads it (a
# mutation through a query's const result then fails to compile).
MUTATING_METHODS: frozenset[str] = frozenset({
    "reset", "emplace", "swap", "clear", "push_back", "emplace_back", "insert", "erase", "assign", "resize",
    "append", "pop_back", "operator=", "operator+=", "operator-=",
})
ASSIGNMENTS: frozenset[str] = frozenset({"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="})
CONSTANT_KINDS: frozenset[str] = frozenset({
    "IntegerLiteral", "FloatingLiteral", "CXXBoolLiteralExpr", "CXXNullPtrLiteralExpr", "CharacterLiteral",
    "StringLiteral",
})
# Parents under which `x() != nullptr` needs no parentheses.
LOOSE_PARENTS: frozenset[str] = frozenset({
    "IfStmt", "WhileStmt", "DoStmt", "ForStmt", "ReturnStmt", "VarDecl", "ParenExpr", "InitListExpr",
    "CallExpr", "CXXMemberCallExpr", "CXXConstructExpr", "CompoundStmt",
})
INTEGER_TYPE: re.Pattern[str] = re.compile(
    r"^(?:std::)?(?:u?int(?:8|16|32|64)_t|size_t|ptrdiff_t)$|^(?:unsigned |signed )?(?:char|short|int|long|long long)$"
    r"|^unsigned$")
SOURCE_SUFFIXES: tuple[str, ...] = (".cpp", ".cc", ".hpp", ".h", ".mm", ".m", ".inl", ".ipp")
# Translation units the run reads. A full `ninja -t compdb` also lists link and
# custom-command steps (a shell running cmake over an asset); their "file" is
# no source, and their command must never be run as a compiler.
UNIT_SUFFIXES: tuple[str, ...] = (".cpp", ".cc", ".cxx", ".c", ".mm", ".m")
EXCLUDED_PARTS: tuple[str, ...] = ("/third_party/", "/_deps/", "/.build/", "/build/")
LINE_LIMIT: int = 110


# ----------------------------------------------------------------------------- the spec

@dataclass(frozen=True)
class Factory:
    name: str
    selector: str               # an enumerator as written ("Enum::value"), or OTHER
    fields: tuple[str, ...]     # parameters after the selector (when OTHER), in order
    comment: str


@dataclass(frozen=True)
class RecordSpec:
    qualified: str              # "gui_forms::DocumentRequestResult"
    selector: tuple[str, ...]   # ("status",) or ("status", "error")
    selector_type: str          # the inner field's type, for a dotted selector
    selector_default: str       # the inner field's default, for a dotted selector
    queries: dict[str, str]     # field -> one of QUERY_FORMS
    factories: tuple[Factory, ...]


class SpecError(ValueError):
    """The spec contradicts itself or the record it names."""


def record_name(spec: RecordSpec) -> str:
    result: str = spec.qualified.rsplit("::", 1)[-1]
    return result


def record_namespace(spec: RecordSpec) -> str:
    result: str = spec.qualified.rsplit("::", 1)[0] if "::" in spec.qualified else ""
    return result


def load_spec(path: Path) -> list[RecordSpec]:
    with path.open("rb") as handle:
        data: dict = tomllib.load(handle)
    specs: list[RecordSpec] = []
    table: dict
    for table in data.get("record", []):
        name: str = table["name"]
        selector: tuple[str, ...] = tuple(str(table["selector"]).split("."))
        if len(selector) > 2:
            raise SpecError(f"{name}: a selector is a field or one field inside it, not {table['selector']!r}")
        queries: dict[str, str] = dict(table.get("queries", {}))
        form: str
        for form in queries.values():
            if form not in QUERY_FORMS:
                raise SpecError(f"{name}: query form {form!r} is not one of {', '.join(QUERY_FORMS)}")
        if selector[0] not in queries:
            raise SpecError(f"{name}: the selector field {selector[0]!r} needs a query")
        factories: list[Factory] = []
        entry: dict
        for entry in table.get("factory", []):
            fields: tuple[str, ...] = tuple(entry.get("fields", []))
            unknown: list[str] = [item for item in fields if item not in queries]
            if unknown or selector[0] in fields:
                raise SpecError(f"{name}.{entry['name']}: fields {fields} name no query or the selector")
            factories.append(Factory(entry["name"], entry["selector"], fields, entry.get("comment", "")))
        if not factories:
            raise SpecError(f"{name}: no factories")
        if len(selector) == 2 and not (table.get("selector_type") and table.get("selector_default")):
            raise SpecError(f"{name}: a dotted selector needs selector_type and selector_default")
        specs.append(RecordSpec(name, selector, table.get("selector_type", ""), table.get("selector_default", ""),
                                queries, tuple(factories)))
    return specs


def same_enumerator(left: str, right: str) -> bool:
    """Whether two spellings name one enumerator: the shorter's components end the longer's."""
    left_parts: list[str] = left.replace(" ", "").split("::")
    right_parts: list[str] = right.replace(" ", "").split("::")
    count: int = min(len(left_parts), len(right_parts))
    result: bool = count > 0 and left_parts[-count:] == right_parts[-count:]
    return result


# ----------------------------------------------------------------------------- what a unit reports

@dataclass(frozen=True)
class Edit:
    file: str
    begin: int                  # byte offsets; begin == end inserts
    end: int
    text: str


@dataclass
class Site:
    file: str
    offset: int
    record: str                 # the record's qualified name
    form: str                   # "read copy", "build local", "write", ...
    reason: str                 # "" when the edits make the site; otherwise why it is left for a hand edit
    edits: list[Edit] = field(default_factory=list)


@dataclass
class FieldInfo:
    name: str
    type: str                   # as written
    begin: int
    end: int
    default: str                # the initializer as written ("{Status::unavailable}", "= 0"), or ""
    default_enum: str           # the default's enumerator, qualified, when it is one
    comment: list[str]          # the // lines just above the field, without indentation


@dataclass
class RecordInfo:
    qualified: str
    file: str
    begin: int                  # the definition, from `struct` to its closing brace
    end: int
    fields: list[FieldInfo]


@dataclass
class UnitResult:
    unit: str
    seconds: float
    error: str
    sites: list[Site]
    records: list[RecordInfo]
    files: list[str]            # every file the unit's kept declarations came from


# ----------------------------------------------------------------------------- values a builder tracks

@dataclass(frozen=True)
class Value:
    text: str
    node: Node | None
    constant: bool              # a literal, an enumerator, {} or std::nullopt: the same wherever evaluated
    simple: bool                # a name, a member of one, std::move of one: no side effects
    fresh: bool                 # nothing ran between the assignment and the point it is read at
    enums: tuple[str, ...]      # the enumerators it can be, qualified, when the source says
    reset: bool                 # `{}`: the field's default
    conflict: bool = False      # the paths reaching this point disagree


CONFLICT: Value = Value("", None, False, False, False, (), False, True)


class Bail(Exception):
    """A builder or aggregate the codemod cannot follow; the message is the reason."""


def strip_type(text: str) -> str:
    result: str = re.sub(r"^(?:const|volatile|struct|class)\s+", "", text.strip())
    result = re.sub(r"\s+(?:const|volatile)$", "", result)
    return result.strip()


def is_optional(type_text: str) -> bool:
    stripped_type: str = strip_type(type_text)
    result: bool = stripped_type.startswith(OPTIONAL_PREFIXES)
    return result


def optional_element(type_text: str) -> str:
    stripped_type: str = strip_type(type_text)
    result: str = stripped_type[stripped_type.index("<") + 1:stripped_type.rindex(">")].strip()
    return result


def top_level_comma(text: str) -> bool:
    depth: int = 0
    character: str
    for character in text:
        if character in "([{<":
            depth += 1
        elif character in ")]}>":
            depth -= 1
        elif character == "," and depth == 0:
            return True
    return False


def default_argument(info: FieldInfo) -> str:
    """The field's default as an argument expression."""
    text: str = info.default.strip()
    if text.startswith("="):
        return text[1:].strip()
    if text.startswith("{") and text.endswith("}"):
        inner: str = text[1:-1].strip()
        if inner and not top_level_comma(inner):
            return inner
    if is_optional(info.type):
        return "std::nullopt"
    base: str = strip_type(info.type)
    if base == "bool":
        return "false"
    if INTEGER_TYPE.match(base):
        return "0"
    return "{}"


# ----------------------------------------------------------------------------- source edits

def statement_removal(data: bytes, begin: int, end: int) -> Edit | None:
    """The edit deleting one statement: its text, its `;`, and its line when nothing else is on it."""
    stop: int = end
    if data[stop - 1:stop] != b";":
        while stop < len(data) and data[stop:stop + 1] in (b" ", b"\t"):
            stop += 1
        if data[stop:stop + 1] != b";":
            return None
        stop += 1
    line_start: int = data.rfind(b"\n", 0, begin) + 1
    after: int = stop
    while after < len(data) and data[after:after + 1] in (b" ", b"\t"):
        after += 1
    alone_before: bool = data[line_start:begin].strip(b" \t") == b""
    if alone_before and data[after:after + 2] == b"\r\n":
        return Edit("", line_start, after + 2, "")
    if alone_before and data[after:after + 1] == b"\n":
        return Edit("", line_start, after + 1, "")
    return Edit("", begin, after, "")


def overlapping(edits: list[Edit]) -> tuple[Edit, Edit] | None:
    """The first two edits that touch the same bytes, or two insertions at one point; None when none do."""
    ordered: list[Edit] = sorted(edits, key=edit_order)
    index: int
    for index in range(1, len(ordered)):
        previous: Edit = ordered[index - 1]
        current: Edit = ordered[index]
        if previous.end > current.begin or previous.begin == current.begin:
            return previous, current
    return None


def edit_order(edit: Edit) -> tuple[int, int]:
    result: tuple[int, int] = (edit.begin, edit.end)
    return result


def applied(data: bytes, edits: list[Edit]) -> bytes:
    """`data` with non-overlapping edits applied, from the last offset back."""
    result: bytes = data
    edit: Edit
    for edit in sorted(edits, key=edit_order, reverse=True):
        result = result[:edit.begin] + edit.text.encode("utf-8") + result[edit.end:]
    return result


# ----------------------------------------------------------------------------- one unit

class UnitAnalysis:
    """Classifies one unit's uses of the spec's records. Lives in one worker for one unit."""

    def __init__(self, specs: list[RecordSpec], source: clang_ast.SourceText) -> None:
        self.specs: dict[str, RecordSpec] = {}
        spec: RecordSpec
        for spec in specs:
            self.specs[spec.qualified] = spec
        self.source: clang_ast.SourceText = source
        self.records: dict[str, RecordInfo] = {}
        self.fields: dict[str, tuple[RecordSpec, str]] = {}     # "ns::R::f" -> (spec, "f")
        self.declarations: dict[str, Node] = {}                 # identity -> declaration
        self.sites: list[Site] = []
        self.consumed: set[int] = set()                          # id() of nodes a build site has taken
        self.files: set[str] = set()

    # -- the first pass: records, fields and declarations

    def collect(self, top: Node) -> None:
        clang_ast.walk(top, self.collect_node)

    def collect_node(self, node: Node, parents: list[Node]) -> None:
        if node.file:
            self.files.add(node.file)
        if node.identity:
            self.declarations[node.identity] = node
        if node.kind != "CXXRecordDecl" or not node.definition or node.qualified not in self.specs:
            return
        if node.qualified in self.records:
            return
        spec: RecordSpec = self.specs[node.qualified]
        infos: list[FieldInfo] = []
        child: Node
        for child in node.children:
            if child.kind == "FieldDecl":
                infos.append(self.field_info(child))
        names: list[str] = [info.name for info in infos]
        if sorted(names) != sorted(spec.queries):
            raise SpecError(f"{spec.qualified}: fields {names} but queries for {sorted(spec.queries)}")
        self.records[node.qualified] = RecordInfo(node.qualified, node.file, node.begin, node.end, infos)
        info: FieldInfo
        for info in infos:
            self.fields[node.qualified + "::" + info.name] = (spec, info.name)

    def field_info(self, node: Node) -> FieldInfo:
        text: str = self.source.node_text(node)
        found: re.Match[str] | None = re.search(r"\b" + re.escape(node.name) + r"\b\s*(?=[{=]|$)", text)
        type_text: str = text[:found.start()].strip() if found is not None else node.type
        default: str = text[found.end():].strip() if found is not None else ""
        default_enum: str = ""
        init: Node | None = node.roles.get("init")
        if init is not None and init.kind == "InitListExpr" and len(init.children) == 1:
            init = init.children[0]
        if init is not None and init.kind == "DeclRefExpr" and init.target_kind == "EnumConstantDecl":
            default_enum = init.qualified
        data: bytes = self.source.bytes_of(node.file)
        comment: list[str] = []
        line_start: int = data.rfind(b"\n", 0, node.begin) + 1
        while line_start > 0:
            previous_start: int = data.rfind(b"\n", 0, line_start - 1) + 1
            line: str = data[previous_start:line_start].decode("utf-8", "replace").strip()
            if not line.startswith("//"):
                break
            comment.insert(0, line)
            line_start = previous_start
        result: FieldInfo = FieldInfo(node.name, type_text, node.begin, node.end, default, default_enum, comment)
        return result

    # -- types and spelling

    def record_of_type(self, type_text: str) -> RecordSpec | None:
        """The spec record a value type names, or None (references, pointers and templates name none)."""
        base: str = strip_type(type_text)
        if not base or any(character in base for character in "&*<"):
            return None
        spec: RecordSpec
        for spec in self.specs.values():
            if base == spec.qualified or base == record_name(spec) or spec.qualified.endswith("::" + base):
                return spec
        return None

    def spelling(self, spec: RecordSpec, function: Node | None, written: str) -> str:
        """How a site names the record: as its own code wrote it, else short inside the record's namespace."""
        if written:
            return strip_type(written)
        namespace: str = record_namespace(spec)
        if function is not None and namespace and function.qualified.startswith(namespace + "::"):
            return record_name(spec)
        return spec.qualified

    def expression_type(self, node: Node) -> str:
        """An expression's type as declared, when the unit says; "" otherwise."""
        node = clang_ast.stripped(node)
        if node.kind == "CallExpr" and node.name == "move" and len(node.arguments) == 1:
            return self.expression_type(node.arguments[0])
        if node.kind in ("DeclRefExpr", "MemberExpr"):
            declaration: Node | None = self.declarations.get(node.target)
            return declaration.type if declaration is not None else ""
        if node.kind == "CXXFunctionalCastExpr":
            # `T(x)` or `T{x}`. With a constructor the cast's child is the construction, which
            # starts where the cast does, so the type is read up to the opening bracket.
            written: str = self.source.node_text(node)
            cut: int = len(written)
            bracket: str
            for bracket in ("(", "{"):
                found: int = written.find(bracket)
                if 0 <= found < cut:
                    cut = found
            return written[:cut].strip()
        if node.kind == "CXXConstructExpr":
            return node.name
        if node.kind in ("CallExpr", "CXXMemberCallExpr"):
            called: Node | None = self.declarations.get(node.target)
            if called is not None and "(" in called.type:
                return called.type[:called.type.index("(")].strip()
        return ""

    # -- values

    def value_of(self, node: Node, fresh: bool) -> Value:
        if self.reads_a_field(node):
            raise Bail("a value that reads a record field: " + self.source.node_text(node))
        inner: Node = clang_ast.stripped(node)
        text: str = self.source.node_text(node)
        if inner.kind == "InitListExpr" and not inner.children:
            return Value(text, node, True, True, fresh, (), True)
        if inner.kind == "DeclRefExpr" and inner.target_kind == "EnumConstantDecl":
            return Value(text, node, True, True, fresh, (inner.qualified,), False)
        if text.strip() in ("std::nullopt", "nullopt"):
            return Value(text, node, True, True, fresh, (), False)
        if inner.kind in CONSTANT_KINDS:
            return Value(text, node, True, True, fresh, (), False)
        if inner.kind == "ConditionalOperator":
            arms: list[Node] = [clang_ast.stripped(inner.roles[name]) for name in ("true", "false")
                                if name in inner.roles]
            enums: tuple[str, ...] = tuple(arm.qualified for arm in arms
                                           if arm.kind == "DeclRefExpr" and arm.target_kind == "EnumConstantDecl")
            known: tuple[str, ...] = enums if len(enums) == 2 else ()
            return Value(text, node, False, False, fresh, known, False)
        simple: bool = self.is_simple(inner)
        return Value(text, node, False, simple, fresh, (), False)

    def reads_a_field(self, node: Node) -> bool:
        if node.kind == "MemberExpr" and node.qualified in self.fields:
            return True
        child: Node
        for child in node.children:
            if self.reads_a_field(child):
                return True
        return False

    def is_simple(self, node: Node) -> bool:
        node = clang_ast.stripped(node)
        if node.kind == "DeclRefExpr" or node.kind == "CXXThisExpr":
            return True
        if node.kind == "MemberExpr" and node.children:
            return self.is_simple(node.children[0])
        if node.kind == "CallExpr" and node.name == "move" and len(node.arguments) == 1:
            return self.is_simple(node.arguments[0])
        return False

    def braced_child(self, node: Node) -> Node | None:
        """The one element of `{x}` or `T{x}`, else None."""
        inner: Node = clang_ast.stripped(node)
        if inner.kind == "CXXFunctionalCastExpr" and len(inner.children) == 1:
            inner = inner.children[0]
        if inner.kind == "InitListExpr" and len(inner.children) == 1:
            return inner.children[0]
        return None

    def assign(self, spec: RecordSpec, state: dict[str, Value], path: tuple[str, ...], node: Node,
               fresh: bool) -> dict[str, Value]:
        """The state after `local.<path> = node`."""
        result: dict[str, Value] = dict(state)
        value: Value = self.value_of(node, fresh)
        key: str = ".".join(path)
        dotted: bool = len(spec.selector) == 2
        if dotted and path == spec.selector[:1]:
            self.clear_below(result, key)
            child: Node | None = self.braced_child(node)
            if value.reset:
                result[key] = value
            elif child is not None:
                result[key] = replace(value, reset=True)
                result[".".join(spec.selector)] = self.value_of(child, fresh)
            else:
                result[key] = value
            return result
        if dotted and path == spec.selector:
            parent: Value | None = result.get(spec.selector[0])
            if parent is not None and not parent.reset:
                raise Bail(f"{key} is written after {spec.selector[0]} was assigned as a whole")
            result[key] = value
            return result
        if len(path) > 1:
            raise Bail(f"a write inside the field {path[0]}")
        self.clear_below(result, key)
        result[key] = value
        return result

    def clear_below(self, state: dict[str, Value], key: str) -> None:
        below: list[str] = [name for name in state if name.startswith(key + ".")]
        name: str
        for name in below:
            del state[name]

    # -- choosing a factory for a state

    def selector_value(self, spec: RecordSpec, record: RecordInfo, state: dict[str, Value]) -> Value:
        key: str = ".".join(spec.selector)
        value: Value | None = state.get(key)
        if len(spec.selector) == 2 and value is None:
            parent: Value | None = state.get(spec.selector[0])
            if parent is not None and parent.conflict:
                raise Bail("the paths reaching this point disagree about " + spec.selector[0])
            if parent is not None and not parent.reset:
                raise Bail(f"{spec.selector[0]} is assigned as a whole, so its {spec.selector[1]} is not known")
            return Value(spec.selector_default, None, True, True, True, (spec.selector_default,), False)
        if value is None or value.reset:
            info: FieldInfo = self.field_named(record, spec.selector[0])
            if not info.default_enum:
                raise Bail(f"the selector {key} has no enumerator default")
            return Value(default_argument(info), None, True, True, True, (info.default_enum,), False)
        if value.conflict:
            raise Bail(f"the paths reaching this point disagree about {key}")
        if not value.enums:
            raise Bail(f"the selector {key} is not given as an enumerator: {value.text}")
        return value

    def field_named(self, record: RecordInfo, name: str) -> FieldInfo:
        info: FieldInfo
        for info in record.fields:
            if info.name == name:
                return info
        raise SpecError(f"{record.qualified} has no field {name}")

    def presence(self, info: FieldInfo, value: Value | None) -> str:
        """For an std::optional field: "yes", "no" or "unknown"."""
        if value is None or value.reset:
            return "no"
        if value.conflict:
            return "unknown"
        if value.text.strip() in ("std::nullopt", "nullopt"):
            return "no"
        written: str = self.expression_type(value.node) if value.node is not None else ""
        if not written or is_optional(written):
            return "unknown"
        return "yes"

    def is_default_value(self, info: FieldInfo, value: Value | None) -> bool:
        if value is None or value.reset:
            return True
        default: str = default_argument(info)
        if value.text.strip() == default:
            return True
        result: bool = default in ("0", "false") and value.constant and value.text.strip() in ("0", "0U", "0u",
                                                                                               "false")
        return result

    def factory_call(self, spec: RecordSpec, record: RecordInfo, state: dict[str, Value], spelled: str) -> str:
        """`R::factory(args)` building the record in `state`; raises Bail when no factory fits."""
        selector: Value = self.selector_value(spec, record, state)
        chosen_names: set[tuple[str, ...]] = set()
        enumerator: str
        for enumerator in selector.enums:
            exact: tuple[str, ...] = tuple(factory.name for factory in spec.factories
                                           if factory.selector != OTHER and same_enumerator(factory.selector,
                                                                                            enumerator))
            others: tuple[str, ...] = tuple(factory.name for factory in spec.factories if factory.selector == OTHER)
            chosen_names.add(exact if exact else others)
        if len(chosen_names) != 1:
            raise Bail(f"the selector's values {selector.text} pick different factories")
        candidates: tuple[str, ...] = chosen_names.pop()
        if not candidates:
            raise Bail(f"no factory builds {selector.text}")
        present: set[str] = set()
        given: set[str] = set()
        info: FieldInfo
        for info in record.fields:
            if info.name == spec.selector[0]:
                continue
            value: Value | None = state.get(info.name)
            if value is not None and value.conflict:
                raise Bail(f"the paths reaching this point disagree about {info.name}")
            if is_optional(info.type):
                presence: str = self.presence(info, value)
                if presence == "unknown":
                    raise Bail(f"cannot tell whether {info.name} holds a value: {value.text if value else ''}")
                if presence == "yes":
                    present.add(info.name)
            elif not self.is_default_value(info, value):
                given.add(info.name)
        factory: Factory
        for factory in spec.factories:
            if factory.name not in candidates:
                continue
            optional_fields: set[str] = {name for name in factory.fields
                                         if is_optional(self.field_named(record, name).type)}
            if optional_fields == present and given <= set(factory.fields):
                return self.call_text(spec, record, state, factory, selector, spelled)
        raise Bail(f"no factory of {', '.join(candidates)} takes {sorted(present | given) or 'no fields'}")

    def call_text(self, spec: RecordSpec, record: RecordInfo, state: dict[str, Value], factory: Factory,
                  selector: Value, spelled: str) -> str:
        used: list[Value] = []
        arguments: list[str] = []
        if factory.selector == OTHER:
            arguments.append(selector.text)
            used.append(selector)
        name: str
        for name in factory.fields:
            value: Value | None = state.get(name)
            if value is None or value.reset:
                arguments.append(default_argument(self.field_named(record, name)))
                continue
            arguments.append(value.text)
            used.append(value)
        value_used: Value
        side_effects: int = 0
        for value_used in used:
            if value_used.constant:
                continue
            if not value_used.fresh:
                raise Bail(f"{value_used.text} is computed before other statements run")
            if not value_used.simple:
                side_effects += 1
        if side_effects > 1:
            raise Bail("two computed arguments: a call would not keep their order")
        result: str = f"{spelled}::{factory.name}({', '.join(arguments)})"
        return result

    # -- the second pass

    def analyse(self, top: Node) -> None:
        clang_ast.walk(top, self.analyse_function_node)
        clang_ast.walk(top, self.analyse_node)

    def analyse_function_node(self, node: Node, parents: list[Node]) -> None:
        if node.kind not in clang_ast.FUNCTIONS or not node.definition:
            return
        body: Node | None = None
        child: Node
        for child in node.children:
            if child.kind == "CompoundStmt":
                body = child
        if body is not None:
            FunctionAnalysis(self, node, body).run()

    def analyse_node(self, node: Node, parents: list[Node]) -> None:
        if id(node) in self.consumed:
            return
        if node.kind == "MemberExpr" and node.qualified in self.fields:
            self.field_use(node, parents)
        elif node.kind == "FieldDecl" and parents and parents[-1].qualified not in self.specs:
            spec: RecordSpec | None = self.record_of_type(node.type)
            if spec is not None:
                self.hand(spec, node, "member", "a member of another record needs a default state")
        elif node.kind == "ReturnStmt" and "value" in node.roles:
            value: Node = clang_ast.stripped(node.roles["value"])
            function: Node | None = enclosing_function(parents)
            if value.kind == "InitListExpr" and function is not None and id(value) not in self.consumed:
                spec = self.record_of_type(function.type.split("(")[0])
                if spec is not None:
                    self.aggregate(spec, value, value, function, "", "build return {}")
        elif node.kind == "CXXFunctionalCastExpr" and len(node.children) == 1 and \
                node.children[0].kind == "InitListExpr":
            written: str = self.source.text(node.file, node.begin, node.children[0].begin).strip()
            spec = self.record_of_type(written)
            if spec is not None:
                self.aggregate(spec, node, node.children[0], enclosing_function(parents), written,
                               "build temporary")

    def hand(self, spec: RecordSpec, node: Node, form: str, reason: str) -> None:
        self.sites.append(Site(node.file, node.begin, spec.qualified, form, reason))

    def aggregate(self, spec: RecordSpec, replaced: Node, initializer: Node, function: Node | None, written: str,
                  form: str) -> None:
        """A braced initialization of the record: replaced whole by a factory call."""
        record: RecordInfo | None = self.records.get(spec.qualified)
        if record is None:
            self.hand(spec, replaced, form, "the record's definition is not in this unit")
            return
        try:
            if replaced.in_macro:
                raise Bail("inside a macro expansion")
            state: dict[str, Value] = self.aggregate_state(spec, record, initializer)
            text: str = self.factory_call(spec, record, state, self.spelling(spec, function, written))
        except Bail as reason:
            self.hand(spec, replaced, form, str(reason))
            return
        self.consumed.add(id(initializer))
        self.sites.append(Site(replaced.file, replaced.begin, spec.qualified, form, "",
                               [Edit(replaced.file, replaced.begin, replaced.end, text)]))

    def aggregate_state(self, spec: RecordSpec, record: RecordInfo, initializer: Node) -> dict[str, Value]:
        if len(initializer.children) > len(record.fields):
            raise Bail("more initializers than fields")
        state: dict[str, Value] = {}
        index: int
        for index in range(len(initializer.children)):
            element: Node = initializer.children[index]
            if element.kind.startswith("Designated"):
                raise Bail("a designated initializer")
            state = self.assign(spec, state, (record.fields[index].name,), element, True)
        return state

    def field_use(self, member: Node, parents: list[Node]) -> None:
        spec: RecordSpec
        name: str
        spec, name = self.fields[member.qualified]
        query: str = spec.queries[name]
        record: RecordInfo | None = self.records.get(spec.qualified)
        if member.in_macro:
            self.hand(spec, member, "read", "inside a macro expansion")
            return
        optional: bool = record is not None and is_optional(self.field_named(record, name).type)
        parent: Node | None = parents[-1] if parents else None
        grand: Node | None = parents[-2] if len(parents) >= 2 else None
        text: str = self.source.node_text(member)
        if query == "pointer" and not optional and parent is not None and parent.kind == "UnaryOperator" and                 parent.opcode == "&":
            self.read_site(spec, member, "read pointer address",
                           [Edit(member.file, parent.begin, parent.end, text + "()")])
            return
        written: str = write_through(member, parents)
        if written:
            self.hand(spec, member, "write", written)
            return
        if query in ("copy", "reference"):
            self.read_site(spec, member, "read " + query, [Edit(member.file, member.end, member.end, "()")])
            return
        if not optional:
            self.read_site(spec, member, "read pointer", [Edit(member.file, member.begin, member.begin, "(*"),
                                                           Edit(member.file, member.end, member.end, "())")])
            return
        if parent is not None and parent.kind == "CXXOperatorCallExpr" and parent.opcode in ("*", "->") and \
                len(parent.children) >= 2 and parent.children[1] is member:
            self.read_site(spec, member, "read optional " + parent.opcode,
                           [Edit(member.file, member.end, member.end, "()")])
            return
        if parent is not None and parent.kind == "MemberExpr" and grand is not None and \
                grand.kind == "CXXMemberCallExpr" and grand.roles.get("callee") is parent:
            self.optional_call(spec, member, text, parent.name, grand, parents[:-2])
            return
        self.hand(spec, member, "read optional whole",
                  f"the std::optional itself is used (under {parent.kind if parent else 'nothing'})")

    def optional_call(self, spec: RecordSpec, member: Node, text: str, method: str, call: Node,
                      above: list[Node]) -> None:
        if method == "operator bool":
            self.read_site(spec, member, "read optional bool", [Edit(member.file, member.end, member.end, "()")])
            return
        if method == "value":
            self.read_site(spec, member, "read optional value",
                           [Edit(member.file, call.begin, call.end, "(*" + text + "())")])
            return
        if method != "has_value":
            self.hand(spec, member, "read optional " + method, f"std::optional::{method} has no query form")
            return
        user: Node | None = above[-1] if above else None
        replaced: Node = call
        comparison: str = " != nullptr"
        if user is not None and user.kind == "UnaryOperator" and user.opcode == "!":
            replaced = user
            comparison = " == nullptr"
            above = above[:-1]
            user = above[-1] if above else None
        expression: str = text + "()" + comparison
        if not loose_under(user, replaced):
            expression = "(" + expression + ")"
        self.read_site(spec, member, "read optional has_value",
                       [Edit(member.file, replaced.begin, replaced.end, expression)])

    def read_site(self, spec: RecordSpec, member: Node, form: str, edits: list[Edit]) -> None:
        self.sites.append(Site(member.file, member.begin, spec.qualified, form, "", edits))


def loose_under(user: Node | None, node: Node) -> bool:
    """Whether `x != nullptr` in place of `node` needs no parentheses under `user`."""
    if user is None or user.kind in LOOSE_PARENTS:
        return True
    if user.kind == "BinaryOperator" and user.opcode in ("&&", "||", "="):
        return True
    if user.kind == "ConditionalOperator" and user.roles.get("condition") is node:
        return True
    return False


def write_through(member: Node, parents: list[Node]) -> str:
    """Why the use of `member` writes the field ("" for a read): the syntactic writes only."""
    current: Node = member
    index: int = len(parents) - 1
    while index >= 0:
        parent: Node = parents[index]
        if parent.kind == "ParenExpr":
            current = parent
            index -= 1
            continue
        if parent.kind == "MemberExpr" and parent.roles.get("object") is current:
            call: Node | None = parents[index - 1] if index >= 1 else None
            if call is not None and call.kind == "CXXMemberCallExpr" and call.roles.get("callee") is parent:
                return f"calls {parent.name} on the field" if parent.name in MUTATING_METHODS else ""
            current = parent
            index -= 1
            continue
        break
    if index < 0:
        return ""
    user: Node = parents[index]
    if user.kind in ("BinaryOperator", "CompoundAssignOperator") and user.opcode in ASSIGNMENTS and \
            user.roles.get("left") is current:
        return "assigns the field"
    if user.kind == "CXXOperatorCallExpr" and user.opcode in ASSIGNMENTS and len(user.children) >= 2 and \
            user.children[1] is current:
        return "assigns the field"
    if user.kind == "UnaryOperator" and user.opcode in ("++", "--"):
        return "increments the field"
    if user.kind == "UnaryOperator" and user.opcode == "&":
        return "takes the address of the field"
    if user.kind in clang_ast.CALLS and current in user.arguments:
        position: int = user.arguments.index(current)
        if position < len(user.writes) and user.writes[position]:
            return "passes the field to a non-const reference"
    return ""


def enclosing_function(parents: list[Node]) -> Node | None:
    index: int
    for index in range(len(parents) - 1, -1, -1):
        if parents[index].kind in clang_ast.FUNCTIONS:
            return parents[index]
    return None


class FunctionAnalysis:
    """The record locals of one function body: builders, aggregate locals and the rest."""

    def __init__(self, unit: UnitAnalysis, function: Node, body: Node) -> None:
        self.unit: UnitAnalysis = unit
        self.function: Node = function
        self.body: Node = body
        self.locals: list[tuple[Node, list[Node], RecordSpec]] = []
        self.references: dict[str, list[tuple[Node, list[Node]]]] = {}

    def run(self) -> None:
        clang_ast.walk(self.body, self.visit)
        local: Node
        parents: list[Node]
        spec: RecordSpec
        for local, parents, spec in self.locals:
            self.local(local, parents, spec)

    def visit(self, node: Node, parents: list[Node]) -> None:
        if node.kind == "VarDecl" and node.identity:
            spec: RecordSpec | None = self.unit.record_of_type(node.type)
            if spec is not None:
                self.locals.append((node, list(parents), spec))
                self.references[node.identity] = []
        elif node.kind == "DeclRefExpr" and node.target in self.references:
            self.references[node.target].append((node, list(parents)))

    def local(self, local: Node, parents: list[Node], spec: RecordSpec) -> None:
        unit: UnitAnalysis = self.unit
        record: RecordInfo | None = unit.records.get(spec.qualified)
        writes: dict[int, tuple[tuple[str, ...], Node, Node, Node]] = {}   # statement -> (path, value, member, stmt)
        returns: dict[int, Node] = {}
        others: int = 0
        ancestors: set[int] = set()
        reference: Node
        chain: list[Node]
        for reference, chain in self.references[local.identity]:
            ancestor: Node
            for ancestor in chain:
                ancestors.add(id(ancestor))
            ancestors.add(id(reference))
            classified: tuple | None = classify_reference(reference, chain, spec, unit)
            if classified is None:
                others += 1
            elif classified[0] == "write":
                writes[id(classified[4])] = classified[1:5]
            else:
                returns[id(classified[1])] = classified[1]
        init: Node | None = local.roles.get("init")
        default: bool = init is None or (init.kind in ("InitListExpr", "CXXConstructExpr") and not init.children)
        aggregate: bool = init is not None and init.kind == "InitListExpr" and bool(init.children)
        built: bool = default or aggregate
        if not built and not writes:
            return      # a copy of another result, never written: nothing to change
        if record is None:
            unit.hand(spec, local, "build local", "the record's definition is not in this unit")
            return
        if not writes and not returns:
            self.local_initializer(spec, record, local, init, default)
            return
        if others or not built:
            reason: str = "the local is also read or passed" if others else "the local starts as a copy"
            self.give_up(spec, local, writes, reason, built)
            return
        try:
            builder: Builder = Builder(self, spec, record, local, parents, writes, returns, ancestors)
            builder.run(init if aggregate else None)
        except Bail as reason:
            self.give_up(spec, local, writes, str(reason), True)

    def local_initializer(self, spec: RecordSpec, record: RecordInfo, local: Node, init: Node | None,
                          default: bool) -> None:
        """A local never written: its initializer becomes a factory call in place."""
        unit: UnitAnalysis = self.unit
        try:
            if local.in_macro:
                raise Bail("inside a macro expansion")
            state: dict[str, Value] = {} if default else unit.aggregate_state(spec, record, init)
            call: str = unit.factory_call(spec, record, state, unit.spelling(spec, self.function, local.type))
        except Bail as reason:
            unit.hand(spec, local, "build local initializer", str(reason))
            return
        data: bytes = unit.source.bytes_of(local.file)
        edit: Edit
        if init is None or (init.kind == "CXXConstructExpr" and init.end <= init.begin + len(local.name)):
            # `R r;`: the default construction spans the name.
            end: int = init.end if init is not None else local.end
            edit = Edit(local.file, end, end, " = " + call)
        else:
            before: bytes = data[:init.begin].rstrip(b" \t")
            equals: bool = before.endswith(b"=")
            edit = Edit(local.file, init.begin, init.end, call if equals else " = " + call)
            if init.kind == "InitListExpr":
                unit.consumed.add(id(init))
        unit.sites.append(Site(local.file, local.begin, spec.qualified, "build local initializer", "", [edit]))

    def give_up(self, spec: RecordSpec, local: Node, writes: dict, reason: str, built: bool) -> None:
        """Leaves the local as it is: its writes, and its construction when the codemod built it, are hand sites."""
        unit: UnitAnalysis = self.unit
        if built:
            unit.hand(spec, local, "build local", reason)
        entry: tuple
        for entry in writes.values():
            member: Node = entry[2]
            unit.consumed.add(id(member))
            unit.hand(spec, member, "build local write", reason)


def classify_reference(reference: Node, chain: list[Node], spec: RecordSpec,
                       unit: UnitAnalysis) -> tuple | None:
    """("write", path, value, member, statement), ("return", statement), or None for any other use."""
    index: int = len(chain) - 1
    current: Node = reference
    path: list[str] = []
    top_member: Node | None = None
    while index >= 0 and chain[index].kind == "MemberExpr" and chain[index].roles.get("object") is current:
        member: Node = chain[index]
        if top_member is None:
            if member.qualified not in unit.fields:
                return None
            top_member = member
        if index >= 1 and chain[index - 1].kind == "CXXMemberCallExpr" and chain[index - 1].roles.get("callee") is member:
            return None
        path.append(member.name)
        current = member
        index -= 1
    if top_member is None:
        if index >= 0 and chain[index].kind == "CXXConstructExpr" and len(chain[index].children) == 1 and                 chain[index].name == record_name(spec):
            current = chain[index]
            index -= 1
        if index >= 0 and chain[index].kind == "ReturnStmt" and chain[index].roles.get("value") is current:
            return ("return", chain[index])
        return None
    if index < 1:
        return None
    user: Node = chain[index]
    value: Node | None = None
    if user.kind == "BinaryOperator" and user.opcode == "=" and user.roles.get("left") is current:
        value = user.roles.get("right")
    elif user.kind == "CXXOperatorCallExpr" and user.opcode == "=" and len(user.children) == 3 and \
            user.children[1] is current:
        value = user.children[2]
    if value is None or chain[index - 1].kind != "CompoundStmt":
        return None
    return ("write", tuple(path), value, top_member, user)


class Builder:
    """Follows one local from its declaration to each `return` of it, and rewrites them as factory calls."""

    def __init__(self, function: FunctionAnalysis, spec: RecordSpec, record: RecordInfo, local: Node,
                 parents: list[Node], writes: dict, returns: dict, ancestors: set[int]) -> None:
        self.function: FunctionAnalysis = function
        self.unit: UnitAnalysis = function.unit
        self.spec: RecordSpec = spec
        self.record: RecordInfo = record
        self.local: Node = local
        self.parents: list[Node] = parents
        self.writes: dict = writes
        self.returns: dict = returns
        self.ancestors: set[int] = ancestors
        self.exits: list[tuple[Node, dict[str, Value]]] = []

    def run(self, aggregate: Node | None) -> None:
        if len(self.parents) < 2 or self.parents[-1].kind != "DeclStmt" or self.parents[-2].kind != "CompoundStmt":
            raise Bail("the local is not declared alone in a block")
        declaration: Node = self.parents[-1]
        block: Node = self.parents[-2]
        if len(declaration.children) != 1 or declaration.in_macro:
            raise Bail("the declaration declares more than the local")
        state: dict[str, Value] = {}
        if aggregate is not None:
            state = self.unit.aggregate_state(self.spec, self.record, aggregate)
        position: int = next(index for index in range(len(block.children)) if block.children[index] is declaration)
        self.simulate(block.children[position + 1:], state)
        unit: UnitAnalysis = self.unit
        spelled: str = unit.spelling(self.spec, self.function.function, self.local.type)
        calls: list[tuple[Node, str]] = []
        statement: Node
        exit_state: dict[str, Value]
        for statement, exit_state in self.exits:
            value: Node = statement.roles["value"]
            if value.in_macro:
                raise Bail("a return inside a macro expansion")
            calls.append((statement, unit.factory_call(self.spec, self.record, exit_state, spelled)))
        data: bytes = unit.source.bytes_of(self.local.file)
        removal: Edit | None = statement_removal(data, declaration.begin, declaration.end)
        if removal is None:
            raise Bail("the declaration's end is not found")
        sites: list[Site] = [Site(self.local.file, self.local.begin, self.spec.qualified, "build local", "",
                                  [replace(removal, file=declaration.file)])]
        entry: tuple
        for entry in self.writes.values():
            assignment: Node = entry[3]
            if assignment.in_macro:
                raise Bail("a write inside a macro expansion")
            removal = statement_removal(unit.source.bytes_of(assignment.file), assignment.begin, assignment.end)
            if removal is None:
                raise Bail("a write's `;` is not found")
            sites.append(Site(assignment.file, assignment.begin, self.spec.qualified, "build local write", "",
                              [replace(removal, file=assignment.file)]))
            unit.consumed.add(id(entry[2]))
        call: str
        for statement, call in calls:
            value = statement.roles["value"]
            sites.append(Site(statement.file, statement.begin, self.spec.qualified, "build local return", "",
                              [Edit(value.file, value.begin, value.end, call)]))
        unit.sites.extend(sites)

    def contains_use(self, node: Node | None) -> bool:
        result: bool = node is not None and id(node) in self.ancestors
        return result

    def staled(self, state: dict[str, Value]) -> dict[str, Value]:
        result: dict[str, Value] = {}
        key: str
        value: Value
        for key, value in state.items():
            result[key] = value if value.constant or value.conflict else replace(value, fresh=False)
        return result

    def simulate(self, statements: list[Node], state: dict[str, Value] | None) -> dict[str, Value] | None:
        """The state after `statements`, or None when every path through them returned."""
        statement: Node
        for statement in statements:
            identity: int = id(statement)
            if state is None:
                if self.contains_use(statement):
                    raise Bail("a use after every path returned")
                continue
            if identity in self.writes:
                path: tuple[str, ...]
                value: Node
                path, value = self.writes[identity][0], self.writes[identity][1]
                if self.contains_use(value):
                    raise Bail("a write that reads the local")
                state = self.unit.assign(self.spec, state, path, value, True)
                continue
            if identity in self.returns:
                self.exits.append((statement, state))
                state = None
                continue
            if self.contains_use(statement):
                state = self.compound(statement, state)
                continue
            if statement.kind == "ReturnStmt":
                state = None
                continue
            state = self.staled(state)
        return state

    def compound(self, statement: Node, state: dict[str, Value]) -> dict[str, Value] | None:
        if statement.kind == "CompoundStmt":
            return self.simulate(statement.children, state)
        if statement.kind != "IfStmt":
            raise Bail(f"a use inside {statement.kind}")
        role: str
        for role in ("init", "variable", "condition"):
            if self.contains_use(statement.roles.get(role)):
                raise Bail("the local is used in an if statement's condition")
        before: dict[str, Value] = self.staled(state)
        taken: dict[str, Value] | None = self.branch(statement.roles.get("then"), before)
        other: Node | None = statement.roles.get("else")
        skipped: dict[str, Value] | None = self.branch(other, before) if other is not None else before
        return self.merge(taken, skipped)

    def branch(self, node: Node | None, state: dict[str, Value]) -> dict[str, Value] | None:
        if node is None:
            return state
        if id(node) in self.writes:
            raise Bail("a write as an unbraced branch")
        if node.kind == "CompoundStmt":
            return self.simulate(node.children, state)
        return self.simulate([node], state)

    def merge(self, left: dict[str, Value] | None, right: dict[str, Value] | None) -> dict[str, Value] | None:
        if left is None:
            return right
        if right is None:
            return left
        result: dict[str, Value] = {}
        key: str
        for key in set(left) | set(right):
            one: Value | None = left.get(key)
            two: Value | None = right.get(key)
            if one is not None and two is not None and (one is two or (one.constant and two.constant and
                                                                       one.text == two.text)):
                result[key] = one
            else:
                result[key] = CONFLICT
        return result


# ----------------------------------------------------------------------------- one unit, in a worker

class FirstParty:
    def __init__(self, root: str) -> None:
        self.root: str = root.rstrip("/").lower() + "/"

    def __call__(self, file: str) -> bool:
        lowered: str = file.replace("\\", "/").lower()
        result: bool = lowered.startswith(self.root) and not any(part in lowered[len(self.root) - 1:]
                                                                 for part in EXCLUDED_PARTS)
        return result


def analyse_unit(entry: dict, specs: list[RecordSpec], root: str) -> UnitResult:
    started: float = time.perf_counter()
    unit: clang_ast.UnitCommand = clang_ast.unit_command(entry)
    source: clang_ast.SourceText = clang_ast.SourceText()
    analysis: UnitAnalysis = UnitAnalysis(specs, source)
    name: str = clang_ast.absolute(unit.directory, unit.file)
    try:
        tops: list[Node] = list(clang_ast.read_unit(unit, FirstParty(root)))
    except (clang_ast.ReadError, OSError) as error:
        return UnitResult(name, time.perf_counter() - started, str(error), [], [], [])
    top: Node
    for top in tops:
        analysis.collect(top)
    for top in tops:
        analysis.analyse(top)
    names: dict[str, str] = {}
    sites: list[Site] = []
    site: Site
    for site in analysis.sites:
        edits: list[Edit] = [replace(edit, file=canonical(edit.file, names)) for edit in site.edits]
        sites.append(replace(site, file=canonical(site.file, names), edits=edits))
    records: list[RecordInfo] = [replace(info, file=canonical(info.file, names)) for info in analysis.records.values()]
    files: list[str] = sorted({canonical(file, names) for file in analysis.files})
    result: UnitResult = UnitResult(name, time.perf_counter() - started, "", sites, records, files)
    return result


def canonical(file: str, names: dict[str, str]) -> str:
    """One spelling per file: units may name a header with different case or through different paths."""
    if file not in names:
        names[file] = Path(os.path.realpath(file)).as_posix() if file else file
    return names[file]


# ----------------------------------------------------------------------------- the generator

def wrapped(prefix: str, items: list[str], suffix: str, indent: str) -> list[str]:
    """`prefix item, item, ...suffix` as lines no longer than LINE_LIMIT, continued under `indent`."""
    lines: list[str] = []
    current: str = prefix
    index: int
    for index in range(len(items)):
        piece: str = items[index] + (", " if index + 1 < len(items) else "")
        if len(current) + len(piece.rstrip()) > LINE_LIMIT and current.strip() and current != prefix:
            lines.append(current.rstrip())
            current = indent
        current += piece
    current += suffix
    lines.append(current)
    return lines


def generated_record(spec: RecordSpec, record: RecordInfo) -> str:
    """The record as a class: factories, queries, a private constructor and private fields."""
    name: str = record_name(spec)
    indent: str = "    "
    fields: dict[str, FieldInfo] = {}
    info: FieldInfo
    for info in record.fields:
        fields[info.name] = info
    selector_field: FieldInfo = fields[spec.selector[0]]
    dotted: bool = len(spec.selector) == 2
    lines: list[str] = [f"class {name} final {{", "public:"]
    factory: Factory
    for factory in spec.factories:
        if factory.comment:
            lines.extend(indent + "// " + line for line in comment_lines(factory.comment))
        parameters: list[str] = []
        if factory.selector == OTHER:
            selector_type: str = spec.selector_type if dotted else selector_field.type
            parameters.append(f"{selector_type} {spec.selector[-1]}")
        field_name: str
        for field_name in factory.fields:
            parameters.append(f"{parameter_type(fields[field_name])} {field_name}")
        lines.extend(wrapped(f"{indent}[[nodiscard]] static {name} {factory.name}(", parameters, ") noexcept {",
                             indent * 3))
        arguments: list[str] = []
        for info in record.fields:
            arguments.append(constructor_argument(spec, factory, info, dotted))
        lines.extend(wrapped(f"{indent * 2}{name} result{{", arguments, "};", indent * 3))
        lines.append(f"{indent * 2}return result;")
        lines.append(f"{indent}}}")
    lines.append("")
    for info in record.fields:
        lines.extend(indent + line for line in info.comment)
        lines.extend(query_lines(spec, record, info, indent))
    lines.append("")
    lines.append("private:")
    parameters = [f"{info.type} {info.name}" for info in record.fields]
    lines.extend(wrapped(f"{indent}{name}(", parameters, ") noexcept", indent + " " * (len(name) + 1)))
    initializers: list[str] = []
    for info in record.fields:
        moved: bool = spec.queries[info.name] != "copy"
        initializers.append(f"{info.name}_({'std::move(' + info.name + ')' if moved else info.name})")
    lines.extend(wrapped(f"{indent * 2}: ", initializers, " {}", indent * 2 + "  "))
    lines.append("")
    for info in record.fields:
        lines.append(f"{indent}{info.type} {info.name}_;")
    lines.append("}")
    result: str = "\n".join(lines)
    return result


def comment_lines(text: str) -> list[str]:
    words: list[str] = text.split()
    lines: list[str] = []
    current: str = ""
    word: str
    for word in words:
        if current and len(current) + 1 + len(word) > LINE_LIMIT - 8:
            lines.append(current)
            current = word
        else:
            current = (current + " " + word).strip()
    if current:
        lines.append(current)
    return lines


def parameter_type(info: FieldInfo) -> str:
    result: str = optional_element(info.type) if is_optional(info.type) else info.type
    return result


def constructor_argument(spec: RecordSpec, factory: Factory, info: FieldInfo, dotted: bool) -> str:
    if info.name == spec.selector[0]:
        value: str = spec.selector[-1] if factory.selector == OTHER else factory.selector
        return f"{info.type}{{{value}}}" if dotted else value
    if info.name in factory.fields:
        return info.name if spec.queries[info.name] == "copy" else f"std::move({info.name})"
    return default_argument(info)


def query_lines(spec: RecordSpec, record: RecordInfo, info: FieldInfo, indent: str) -> list[str]:
    form: str = spec.queries[info.name]
    head: str = f"{indent}[[nodiscard]] "
    if form == "copy":
        return [f"{head}{info.type} {info.name}() const noexcept {{", f"{indent * 2}return {info.name}_;",
                f"{indent}}}"]
    if form == "reference":
        return [f"{head}const {info.type}& {info.name}() const noexcept {{", f"{indent * 2}return {info.name}_;",
                f"{indent}}}"]
    if is_optional(info.type):
        element: str = optional_element(info.type)
        return [f"{head}const {element}* {info.name}() const noexcept {{",
                f"{indent * 2}const {element}* const found = {info.name}_.has_value() ? &(*{info.name}_) : nullptr;",
                f"{indent * 2}return found;", f"{indent}}}"]
    owners: list[str] = [factory.selector for factory in spec.factories if info.name in factory.fields]
    if not owners or OTHER in owners:
        raise SpecError(f"{spec.qualified}.{info.name}: a pointer query on a plain field needs factories with "
                        "named selectors")
    selector: str = spec.selector[0] + "_" + ("." + spec.selector[1] if len(spec.selector) == 2 else "")
    condition: str = " || ".join(f"{selector} == {owner}" for owner in owners)
    return [f"{head}const {info.type}* {info.name}() const noexcept {{",
            f"{indent * 2}const bool present = {condition};",
            f"{indent * 2}const {info.type}* const found = present ? &{info.name}_ : nullptr;",
            f"{indent * 2}return found;", f"{indent}}}"]


# ----------------------------------------------------------------------------- the run

@dataclass
class Run:
    sites: list[Site]
    records: dict[str, RecordInfo]
    errors: list[str]
    refused: dict[str, str]          # file -> why its edits were refused
    unread: dict[str, list[int]]     # first-party files naming a record that no unit read -> lines
    seconds: float
    units: int


def first_party_units(entries: list[dict], root: str) -> list[dict]:
    keep: FirstParty = FirstParty(root)
    seen: set[str] = set()
    result: list[dict] = []
    entry: dict
    for entry in entries:
        file: str = clang_ast.absolute(entry["directory"], entry["file"])
        if not file.lower().endswith(UNIT_SUFFIXES) or not keep(file) or file.lower() in seen:
            continue
        seen.add(file.lower())
        result.append(entry)
    return result


def merged_sites(results: list[UnitResult]) -> list[Site]:
    """One site per (file, offset, form); a site whose units disagree about its edits becomes a hand site."""
    by_key: dict[tuple[str, int, str], Site] = {}
    result: UnitResult
    for result in results:
        site: Site
        for site in result.sites:
            key: tuple[str, int, str] = (site.file.lower(), site.offset, site.form)
            known: Site | None = by_key.get(key)
            if known is None:
                by_key[key] = site
            elif (known.reason, known.edits) != (site.reason, site.edits) and not known.reason:
                by_key[key] = Site(site.file, site.offset, site.record, site.form,
                                   "units disagree about this site's edits")
    ordered: list[Site] = sorted(by_key.values(), key=site_order)
    return ordered


def site_order(site: Site) -> tuple[str, int, str]:
    result: tuple[str, int, str] = (site.file.lower(), site.offset, site.form)
    return result


def unread_files(root: Path, specs: list[RecordSpec], read: set[str]) -> dict[str, list[int]]:
    names: re.Pattern[str] = re.compile(r"\b(?:" + "|".join(re.escape(record_name(spec)) for spec in specs) + r")\b")
    keep: FirstParty = FirstParty(root.as_posix())
    found: dict[str, list[int]] = {}
    path: Path
    for path in sorted(root.rglob("*")):
        file: str = path.as_posix()
        if path.suffix not in SOURCE_SUFFIXES or not keep(file) or file.lower() in read:
            continue
        try:
            text: str = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines: list[int] = [number + 1 for number, line in enumerate(text.split("\n")) if names.search(line)]
        if lines:
            found[file] = lines
    return found


def plan(specs: list[RecordSpec], root: Path, entries: list[dict], jobs: int) -> Run:
    started: float = time.perf_counter()
    units: list[dict] = first_party_units(entries, root.as_posix())
    results: list[UnitResult] = []
    entry: dict
    if jobs <= 1:
        # In this process: HARNESS_AST_READER as this process has it, and no workers to start.
        for entry in units:
            results.append(analyse_unit(entry, specs, root.as_posix()))
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futures: list = [pool.submit(analyse_unit, entry, specs, root.as_posix()) for entry in units]
            future: object
            for future in futures:
                results.append(future.result())
    errors: list[str] = [result.unit + ": " + result.error for result in results if result.error]
    records: dict[str, RecordInfo] = {}
    read: set[str] = set()
    result: UnitResult
    for result in results:
        info: RecordInfo
        for info in result.records:
            records.setdefault(info.qualified, info)
        read.update(file.lower() for file in result.files)
    sites: list[Site] = merged_sites(results)
    spec: RecordSpec
    for spec in specs:
        if spec.qualified not in records:
            errors.append(f"{spec.qualified}: no unit read its definition")
            continue
        info = records[spec.qualified]
        sites.append(Site(info.file, info.begin, spec.qualified, "privatise", "",
                          [Edit(info.file, info.begin, info.end, generated_record(spec, info))]))
    refused: dict[str, str] = refused_files(sites)
    unread: dict[str, list[int]] = unread_files(root, specs, read)
    run: Run = Run(sites, records, errors, refused, unread, time.perf_counter() - started, len(units))
    return run


def refused_files(sites: list[Site]) -> dict[str, str]:
    by_file: dict[str, list[Edit]] = {}
    site: Site
    for site in sites:
        edit: Edit
        for edit in site.edits:
            by_file.setdefault(edit.file, []).append(edit)
    refused: dict[str, str] = {}
    file: str
    edits: list[Edit]
    for file, edits in by_file.items():
        clash: tuple[Edit, Edit] | None = overlapping(edits)
        if clash is not None:
            refused[file] = f"edits at bytes {clash[0].begin}-{clash[0].end} and {clash[1].begin}-{clash[1].end} overlap"
    return refused


def apply_run(run: Run) -> list[str]:
    """Writes every file that has edits and was not refused; returns the files written."""
    by_file: dict[str, list[Edit]] = {}
    site: Site
    for site in run.sites:
        edit: Edit
        for edit in site.edits:
            by_file.setdefault(edit.file, []).append(edit)
    written: list[str] = []
    file: str
    edits: list[Edit]
    for file, edits in sorted(by_file.items()):
        if file in run.refused:
            continue
        path: Path = Path(file)
        data: bytes = path.read_bytes()
        path.write_bytes(applied(data, edits))
        written.append(file)
    return written


def report_text(run: Run, root: Path) -> str:
    source: clang_ast.SourceText = clang_ast.SourceText()
    lines: list[str] = [f"# Record codemod: {run.units} units in {run.seconds:.0f} s", ""]
    by_record: dict[str, dict[str, list[int]]] = {}
    site: Site
    for site in run.sites:
        if site.form == "privatise":
            continue
        forms: dict[str, list[int]] = by_record.setdefault(site.record, {})
        counts: list[int] = forms.setdefault(site.form, [0, 0])
        counts[0 if not site.reason else 1] += 1
    lines.append("| Record | Form | Mechanical | Hand |")
    lines.append("|---|---|---|---|")
    total: list[int] = [0, 0]
    record: str
    forms: dict[str, list[int]]
    for record, forms in sorted(by_record.items()):
        form: str
        counts: list[int]
        for form, counts in sorted(forms.items()):
            lines.append(f"| {record.rsplit('::', 1)[-1]} | {form} | {counts[0]} | {counts[1]} |")
            total[0] += counts[0]
            total[1] += counts[1]
    lines.append(f"| all | | {total[0]} | {total[1]} |")
    lines.append("")
    lines.append("## Hand sites")
    lines.append("")
    for site in run.sites:
        if site.reason:
            relative: str = relative_to(site.file, root)
            lines.append(f"- {relative}:{source.line(site.file, site.offset)} {site.record.rsplit('::', 1)[-1]} "
                         f"{site.form}: {site.reason}")
    if run.refused:
        lines.extend(["", "## Files refused (overlapping edits; left as they were)", ""])
        file: str
        reason: str
        for file, reason in sorted(run.refused.items()):
            lines.append(f"- {relative_to(file, root)}: {reason}")
    if run.unread:
        lines.extend(["", "## Files no unit read that name a record (fix by hand; CI on their host fails)", ""])
        numbers: list[int]
        for file, numbers in sorted(run.unread.items()):
            lines.append(f"- {relative_to(file, root)}: lines {', '.join(str(number) for number in numbers)}")
    if run.errors:
        lines.extend(["", "## Errors", ""])
        error: str
        for error in run.errors:
            lines.append("- " + error)
    result: str = "\n".join(lines) + "\n"
    return result


def relative_to(file: str, root: Path) -> str:
    prefix: str = root.as_posix().rstrip("/") + "/"
    result: str = file[len(prefix):] if file.lower().startswith(prefix.lower()) else file
    return result


def run_json(run: Run, root: Path) -> dict:
    sites: list[dict] = []
    site: Site
    for site in run.sites:
        sites.append({"file": relative_to(site.file, root), "offset": site.offset, "record": site.record,
                      "form": site.form, "reason": site.reason,
                      "edits": [{"begin": edit.begin, "end": edit.end, "text": edit.text} for edit in site.edits]})
    result: dict = {"units": run.units, "seconds": round(run.seconds, 1), "errors": run.errors,
                    "refused": {relative_to(file, root): reason for file, reason in run.refused.items()},
                    "unread": {relative_to(file, root): lines for file, lines in run.unread.items()},
                    "sites": sites}
    return result


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path, help="the checkout whose first-party files change")
    parser.add_argument("--compile-database", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--apply", action="store_true", help="write the edits (without it, only report)")
    args: argparse.Namespace = parser.parse_args()
    specs: list[RecordSpec] = load_spec(args.spec)
    entries: list[dict] = json.loads(args.compile_database.read_text(encoding="utf-8"))
    root: Path = Path(os.path.realpath(clang_ast.absolute(os.getcwd(), str(args.root))))
    run: Run = plan(specs, root, entries, args.jobs)
    report: str = report_text(run, root)
    if args.report is not None:
        args.report.write_text(report, encoding="utf-8")
    else:
        sys.stdout.write(report)
    if args.json is not None:
        args.json.write_text(json.dumps(run_json(run, root), indent=1), encoding="utf-8")
    if args.apply:
        written: list[str] = apply_run(run)
        print(f"wrote {len(written)} files; {len(run.refused)} refused")
    return 1 if run.errors else 0


if __name__ == "__main__":
    sys.exit(main())
