"""The json backend of clang_ast.py: the pinned clang's JSON AST dump, streamed.

It runs the clang driver of the unit's own command with `-fsyntax-only
-Xclang -ast-dump=json` and builds `clang_ast.Node` trees for the kept
top-level declarations, without holding the whole dump.

The dump format it relies on (clang 18 to 22):

- the dump is pretty-printed, two spaces per level, so each top-level
  declaration of the TranslationUnitDecl starts at a line holding exactly
  `    {` and ends at `    }` or `    },`;
- a location names its file only when the file differs from the previous
  location written, anywhere in the dump; a location's `includedFrom` names
  the including file and does not change it;
- a location inside a macro expansion holds `spellingLoc` and
  `expansionLoc` instead of an offset;
- offsets are byte offsets into the file as read.

A declaration is parsed (json.loads) only when its own location is in a file
`keep` accepts. The file in effect is tracked through skipped text and
through every node left out of the model, so each node's file is right.

A reference's `qualified` name is known only when its declaration was read in
the same unit (kept, and in the declarations this reading yields); the
libclang backend has no such limit.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import subprocess
from typing import Callable, Iterator, TextIO

from clang_ast import (CONST_FUNCTION_TYPE, FUNCTIONS, Node, ReadError, SourceText, UnitCommand, absolute,
                       assign_call_parts, assign_positional_roles, written_type)

FILE_LINE: re.Pattern[str] = re.compile(r'^\s*"file": "((?:[^"\\]|\\.)*)",?$')
TOP_START: str = "    {"
TOP_ENDS: tuple[str, ...] = ("    }", "    },")
# A top-level declaration's keys sit at six spaces; its range follows its location.
TOP_RANGE: str = '      "range"'
DUMP_SWITCHES: tuple[str, ...] = ("-fsyntax-only", "-Wno-everything", "-Xclang", "-ast-dump=json")

# Nodes the model leaves out together with everything under them.
DROPPED_KINDS: frozenset[str] = frozenset({
    "TemplateTypeParmDecl", "NonTypeTemplateParmDecl", "TemplateTemplateParmDecl", "CXXDefaultArgExpr",
    "CXXDefaultInitExpr", "ImplicitValueInitExpr", "NoInitExpr", "ArrayInitIndexExpr", "OpaqueValueExpr",
    "TemplateArgument", "UnresolvedLookupExpr"})
# Nodes the model sees through: their single child stands in their place.
TRANSPARENT_KINDS: frozenset[str] = frozenset({
    "ImplicitCastExpr", "ExprWithCleanups", "MaterializeTemporaryExpr", "CXXBindTemporaryExpr", "ConstantExpr",
    "FullExpr", "SubstNonTypeTemplateParmExpr", "ArrayInitLoopExpr", "CXXRewrittenBinaryOperator",
    "CXXStdInitializerListExpr"})
# A template reads as its pattern: the child of these kinds.
TEMPLATE_PATTERNS: dict[str, frozenset[str]] = {
    "FunctionTemplateDecl": FUNCTIONS,
    "ClassTemplateDecl": frozenset({"CXXRecordDecl"}),
    "VarTemplateDecl": frozenset({"VarDecl"}),
    "TypeAliasTemplateDecl": frozenset({"TypeAliasDecl"}),
}
RENAMED_KINDS: dict[str, str] = {
    "ClassTemplateSpecializationDecl": "CXXRecordDecl",
    "ClassTemplatePartialSpecializationDecl": "CXXRecordDecl",
    "CXXTemporaryObjectExpr": "CXXConstructExpr",
    "CXXDependentScopeMemberExpr": "MemberExpr",
    "UnresolvedMemberExpr": "MemberExpr",
}
LITERALS: frozenset[str] = frozenset({"IntegerLiteral", "FloatingLiteral", "StringLiteral", "CharacterLiteral",
                                      "CXXBoolLiteralExpr", "CXXNullPtrLiteralExpr", "GNUNullExpr"})
SCOPES: frozenset[str] = frozenset({"NamespaceDecl", "CXXRecordDecl"})
TYPED_DECLARATIONS: frozenset[str] = frozenset({"VarDecl", "ParmVarDecl", "FieldDecl"})
REFERENCE_EXPRESSIONS: frozenset[str] = frozenset({"DeclRefExpr", "MemberExpr", "ArraySubscriptExpr"})


@dataclass(frozen=True)
class Declaration:
    """One kept top-level declaration of a dump and the file in effect just before it."""
    file_before: str
    node: dict


def dump_command(unit: UnitCommand) -> list[str]:
    result: list[str] = list(unit.command) + list(DUMP_SWITCHES)
    return result


def unquoted(escaped: str) -> str:
    result: str = json.loads('"' + escaped + '"')
    return result


def declarations(lines: Iterator[str], keep: Callable[[str], bool]) -> Iterator[Declaration]:
    """The top-level declarations of a dump whose location is in a kept file, in dump order.

    `keep` receives file names as the dump writes them.
    """
    current: str = ""           # the file of the last location written
    previous: str = ""          # the previous line, to tell an includedFrom file apart
    buffer: list[str] = []
    phase: str = "outside"      # outside, deciding (location not yet read), keeping, skipping
    before: str = ""
    raw: str
    for raw in lines:
        line: str = raw.rstrip("\r\n")
        if '"file"' in line:
            match: re.Match[str] | None = FILE_LINE.match(line)
            if match is not None and not previous.lstrip().startswith('"includedFrom"'):
                current = unquoted(match.group(1))
        previous = line
        if phase == "outside":
            if line == TOP_START:
                phase = "deciding"
                buffer = [line]
            continue
        if phase != "skipping":
            buffer.append(line)
        if line in TOP_ENDS:
            if phase == "keeping":
                yield Declaration(before, json.loads("\n".join(buffer).rstrip(",")))
            phase = "outside"
            buffer = []
            before = current
            continue
        if phase == "deciding" and line.startswith(TOP_RANGE):
            # The declaration's location has been read: the file in effect is its file, or for a location
            # in a macro expansion the expansion's (clang writes expansionLoc after spellingLoc).
            if keep(current):
                phase = "keeping"
            else:
                phase = "skipping"
                buffer = []


@dataclass
class Scope:
    """A declaration the conversion is inside: a namespace, a record or a function."""
    name: str
    kind: str       # namespace, record or function
    access: str     # in a record: the access its next member has unless the member says otherwise


@dataclass(frozen=True)
class Place:
    """Where one location points: the file and offset written, and the spelling's when a macro moved it."""
    file: str
    offset: int
    token_length: int
    spelling_file: str
    spelling_offset: int
    spelling_length: int
    in_macro: bool


class Converter:
    """Builds the model from one unit's kept declarations, tracking the file through every location."""

    def __init__(self, directory: str, source: SourceText) -> None:
        self.directory: str = directory
        self.source: SourceText = source
        self.file: str = ""
        self.scopes: list[Scope] = []
        self.declared: dict[str, str] = {}          # identity -> qualified name
        self.declared_kinds: dict[str, str] = {}    # identity -> kind
        self.declared_access: dict[str, str] = {}   # identity -> a member's access
        self.declared_types: dict[str, str] = {}    # identity -> a function's type, for member calls' parameters
        self.references: list[Node] = []            # nodes whose target is described at the end
        self.calls: list[Node] = []                 # calls that take their target from the callee at the end
        self.template_begin: int = -1               # the template header's offset, for its pattern

    # --- locations -----------------------------------------------------------------

    def note_plain(self, location: dict) -> None:
        if "file" in location:
            self.file = location["file"]

    def place(self, location: dict) -> Place:
        """Takes up the files a location writes, in the order clang writes them, and says where it points."""
        if "spellingLoc" in location or "expansionLoc" in location:
            spelling: dict = location.get("spellingLoc", {})
            self.note_plain(spelling)
            spelling_file: str = self.file
            expansion: dict = location.get("expansionLoc", {})
            self.note_plain(expansion)
            result: Place = Place(self.file, int(expansion.get("offset", -1)), int(expansion.get("tokLen", 0)),
                                  spelling_file, int(spelling.get("offset", -1)), int(spelling.get("tokLen", 0)),
                                  True)
            return result
        self.note_plain(location)
        offset: int = int(location.get("offset", -1))
        length: int = int(location.get("tokLen", 0))
        result = Place(self.file, offset, length, self.file, offset, length, False)
        return result

    def skim(self, node: dict) -> None:
        """Takes up the files of a node left out of the model, and of everything under it."""
        if "loc" in node:
            self.place(node["loc"])
        if "range" in node:
            self.place(node["range"].get("begin", {}))
            self.place(node["range"].get("end", {}))
        child: dict
        for child in node.get("inner", []):
            if child:
                self.skim(child)

    def located(self, kind: str, node: dict) -> tuple[Node, Place, Place]:
        """A model node for `node` at its range, with the range's two ends."""
        if "loc" in node:
            self.place(node["loc"])
        empty: Place = Place(self.file, -1, 0, self.file, -1, 0, False)
        begin: Place = empty
        end: Place = empty
        if "range" in node:
            begin = self.place(node["range"].get("begin", {}))
            end = self.place(node["range"].get("end", {}))
        file: str = absolute(self.directory, begin.file) if begin.file else ""
        stop: int = end.offset + end.token_length if end.offset >= 0 else -1
        result: Node = Node(kind, file, begin.offset, stop, begin.in_macro or end.in_macro)
        return result, begin, end

    # --- conversion ------------------------------------------------------------------

    def convert(self, node: dict) -> Node | None:
        """The model node for one dump node, or None when the model leaves it out (its files are still read)."""
        if not node:
            return None
        kind: str = node.get("kind", "")
        if (kind in DROPPED_KINDS or kind.endswith("Attr") or kind.endswith("Type") or "Comment" in kind
                or (node.get("isImplicit") and kind.endswith("Decl"))):
            self.skim(node)
            return None
        if kind in TRANSPARENT_KINDS or (kind == "ParenListExpr" and len(node.get("inner", [])) == 1):
            return self.transparent(node)
        if kind == "ParenListExpr":
            kind = "UnexposedExpr"
        if kind in TEMPLATE_PATTERNS:
            return self.template(node, TEMPLATE_PATTERNS[kind])
        kind = RENAMED_KINDS.get(kind, kind)
        result: Node
        begin: Place
        end: Place
        result, begin, end = self.located(kind, node)
        self.describe(result, node, begin, end)
        # A scoped enumeration qualifies its enumerators; an unscoped one leaves them in the enclosing scope.
        entered: bool = (kind in SCOPES or kind in FUNCTIONS or kind == "FriendDecl"
                         or (kind == "EnumDecl" and "scopedEnumTag" in node))
        if entered:
            self.scopes.append(scope_of(result))
        if kind == "CXXForRangeStmt":
            self.range_for(result, node)
        elif kind in ("IfStmt", "SwitchStmt", "WhileStmt", "ForStmt"):
            self.statement(result, node)
        elif kind in ("CallExpr", "CXXMemberCallExpr", "CXXOperatorCallExpr", "CXXConstructExpr"):
            self.call(result, node)
        elif kind == "CXXUnresolvedConstructExpr":
            # A dependent construction in a template: a call with no callee, as libclang reads it.
            result.kind = "CallExpr"
            arguments: list[Node] = []
            child: dict
            for child in node.get("inner", []):
                converted: Node | None = self.convert(child)
                if converted is not None:
                    arguments.append(converted)
                    result.writes.append(False)
            assign_call_parts(result, None, arguments)
        elif kind == "LambdaExpr":
            self.lambda_body(result, node)
        else:
            self.convert_children(result, node.get("inner", []))
            if kind == "FriendDecl":
                # `friend class X;` names a record libclang reads as a type reference: left out.
                result.children = [child for child in result.children if child.kind != "CXXRecordDecl"]
            if kind == "InitListExpr":
                self.syntactic_list(result)
            assign_positional_roles(result)
        if entered:
            self.scopes.pop()
        return result

    def syntactic_list(self, result: Node) -> None:
        """An initializer list as written, which is how libclang reads it: the dump's semantic form adds the
        members the braces leave out (listed at the closing brace), a list per elided pair of braces, and
        each element's implicit converting construction."""
        if result.in_macro or result.begin < 0 or not result.file:
            return
        opening: bytes = self.source.bytes_of(result.file)[result.begin:result.begin + 1]
        result.implicit = opening != b"{"
        written: list[Node] = []
        child: Node
        for child in result.children:
            if result.end > 0 and child.begin >= result.end - 1 and not result.implicit:
                continue
            if child.kind == "InitListExpr" and child.implicit:
                written.extend(child.children)
                continue
            if (child.kind == "CXXConstructExpr" and len(child.arguments) == 1
                    and (child.arguments[0].begin, child.arguments[0].end) == (child.begin, child.end)):
                written.append(child.arguments[0])
                continue
            written.append(child)
        result.children = written

    def convert_children(self, result: Node, inner: list[dict]) -> None:
        child: dict
        for child in inner:
            converted: Node | None = self.convert(child)
            if converted is not None:
                result.children.append(converted)

    def transparent(self, node: dict) -> Node | None:
        """The one child that stands for a transparent node; others are only read for their files."""
        if "loc" in node:
            self.place(node["loc"])
        if "range" in node:
            self.place(node["range"].get("begin", {}))
            self.place(node["range"].get("end", {}))
        inner: list[dict] = node.get("inner", [])
        result: Node | None = None
        child: dict
        for child in inner:
            if result is None:
                result = self.convert(child)
            else:
                self.skim(child)
        return result

    def template(self, node: dict, patterns: frozenset[str]) -> Node | None:
        if "loc" in node:
            self.place(node["loc"])
        self.template_begin = -1
        if "range" in node:
            self.template_begin = self.place(node["range"].get("begin", {})).offset
            self.place(node["range"].get("end", {}))
        begin: int = self.template_begin
        result: Node | None = None
        child: dict
        for child in node.get("inner", []):
            if result is None and child.get("kind") in patterns:
                result = self.convert(child)
            else:
                self.skim(child)
        if result is not None and begin >= 0 and not result.in_macro:
            # The pattern's range starts after `template <...>`; the template's (libclang's) at `template`.
            result.begin = begin
        return result

    def describe(self, result: Node, node: dict, begin: Place, end: Place) -> None:
        """The fields of a node that come from its own keys."""
        kind: str = result.kind
        if kind.endswith("Decl"):
            result.identity = node.get("id", "")
        if "name" in node and kind != "MemberExpr":
            result.name = node["name"]
        if kind.endswith("Decl") and result.name:
            result.qualified = self.qualified(result.name, node.get("parentDeclContextId", ""))
            self.declared[result.identity] = result.qualified
        if kind.endswith("Decl") and result.identity:
            self.declared_kinds[result.identity] = kind
        if (kind.endswith("Decl") and kind not in ("FriendDecl", "StaticAssertDecl") and self.scopes
                and self.scopes[-1].kind == "record"):
            # A member: its access as written, or the access in effect in the record.
            if kind == "AccessSpecDecl":
                self.scopes[-1].access = node.get("access", self.scopes[-1].access)
            result.access = node.get("access", self.scopes[-1].access)
        elif kind.endswith("Decl") and node.get("previousDecl", "") in self.declared_access:
            # An out-of-line definition has the access of the member it defines.
            result.access = self.declared_access[node["previousDecl"]]
        if result.access and result.identity:
            self.declared_access[result.identity] = result.access
        if kind == "CXXCtorInitializer":
            initialized: dict = node.get("anyInit", {})
            result.name = initialized.get("name", "") if initialized else node.get("baseInit", {}).get("qualType", "")
            result.target = initialized.get("id", "")
            result.target_kind = initialized.get("kind", "")
            self.references.append(result)
        if "type" in node and (kind in TYPED_DECLARATIONS or kind in FUNCTIONS):
            result.type = written_type(node["type"].get("qualType", ""))
        if kind in FUNCTIONS:
            result.constant = CONST_FUNCTION_TYPE.search(result.type) is not None
            if result.identity:
                self.declared_types[result.identity] = result.type
            result.definition = any(child.get("kind") == "CompoundStmt" for child in node.get("inner", []))
        if kind == "CXXRecordDecl":
            result.tag = node.get("tagUsed", "")
            result.definition = bool(node.get("completeDefinition"))
        if kind == "DeclRefExpr":
            referenced: dict = node.get("referencedDecl", {})
            result.name = referenced.get("name", "")
            result.target = referenced.get("id", "")
            result.target_kind = referenced.get("kind", "")
            self.references.append(result)
        if kind == "MemberExpr":
            result.name = node.get("name", node.get("member", ""))
            result.target = node.get("referencedMemberDecl", "")
            result.arrow = bool(node.get("isArrow"))
            self.references.append(result)
        if kind == "CXXThisExpr":
            result.implicit = bool(node.get("implicit"))
        if kind in ("BinaryOperator", "CompoundAssignOperator", "UnaryOperator"):
            result.opcode = node.get("opcode", "")
            result.postfix = bool(node.get("isPostfix"))
        if kind in LITERALS:
            result.value = self.literal(kind, node, begin, end)

    def qualified(self, name: str, parent: str) -> str:
        """A declaration's name with its namespaces and records; a function's locals keep their bare name."""
        scope: Scope
        for scope in self.scopes:
            if scope.kind == "function":
                return name
        if parent in self.declared:
            # An out-of-line definition: qualified by the record it belongs to, as libclang qualifies it.
            return self.declared[parent] + "::" + name
        names: list[str] = []
        for scope in self.scopes:
            if scope.kind == "friend":
                # A friend declares a name of the enclosing namespace, not of the befriending record.
                names = [enclosing.name for enclosing in self.scopes if enclosing.kind == "namespace"]
                break
            names.append(scope.name)
        names.append(name)
        result: str = "::".join(names)
        return result

    def literal(self, kind: str, node: dict, begin: Place, end: Place) -> str:
        if kind == "CXXBoolLiteralExpr":
            return "true" if node.get("value") else "false"
        if kind == "CXXNullPtrLiteralExpr":
            return "nullptr"
        if begin.spelling_offset < 0 or not begin.spelling_file:
            return str(node.get("value", ""))
        file: str = absolute(self.directory, begin.spelling_file)
        stop: int = end.spelling_offset + end.spelling_length
        result: str = self.source.text(file, begin.spelling_offset, stop)
        return result

    def statement(self, result: Node, node: dict) -> None:
        """An if, switch, while or for statement: its parts by the dump's flags and fixed slots."""
        inner: list[dict] = node.get("inner", [])
        kind: str = result.kind
        names: list[str] = []
        if kind == "ForStmt":
            names = ["init", "variable", "condition", "increment", "body"]
        else:
            if node.get("hasInit"):
                names.append("init")
            if node.get("hasVar"):
                names.append("variable")
            names.append("condition")
            names.append("then" if kind == "IfStmt" else "body")
            if kind == "IfStmt" and node.get("hasElse"):
                names.append("else")
        index: int
        for index in range(len(inner)):
            role: str = names[index] if index < len(names) else ""
            child: dict = inner[index]
            if role == "variable" and child.get("kind") == "DeclStmt":
                # The condition variable is the VarDecl itself.
                self.located("DeclStmt", child)
                declared: list[dict] = child.get("inner", [])
                child = declared[0] if declared else {}
                for extra in declared[1:]:
                    self.skim(extra)
            converted: Node | None = self.convert(child)
            if converted is None:
                continue
            result.children.append(converted)
            if role:
                result.roles[role] = converted

    def range_for(self, result: Node, node: dict) -> None:
        """A range-for: init, the loop variable (its implicit initializer left out), the range, the body.

        The dump's slots: init, the range's DeclStmt, begin, end, condition,
        increment, the loop variable's DeclStmt, body.
        """
        inner: list[dict] = node.get("inner", [])
        if len(inner) != 8:
            self.convert_children(result, inner)
            return
        init: Node | None = self.convert(inner[0])
        range_statement: dict = inner[1]
        self.located("DeclStmt", range_statement)
        range_declarations: list[dict] = range_statement.get("inner", [])
        range_expression: Node | None = None
        if range_declarations:
            range_variable: dict = range_declarations[0]
            self.located("VarDecl", range_variable)
            range_inner: list[dict] = range_variable.get("inner", [])
            if range_inner:
                range_expression = self.convert(range_inner[-1])
        slot: dict
        for slot in inner[2:6]:
            self.skim(slot)
        variable: Node | None = None
        variable_statement: dict = inner[6]
        self.located("DeclStmt", variable_statement)
        variables: list[dict] = variable_statement.get("inner", [])
        if variables:
            variable_node: dict = variables[0]
            variable, begin, end = self.located("VarDecl", variable_node)
            self.describe(variable, variable_node, begin, end)
            for implicit in variable_node.get("inner", []):
                self.skim(implicit)
        body: Node | None = self.convert(inner[7])
        parts: list[tuple[str, Node | None]] = [("init", init), ("variable", variable), ("range", range_expression),
                                                ("body", body)]
        role: str
        part: Node | None
        for role, part in parts:
            if part is not None:
                result.children.append(part)
                result.roles[role] = part

    def call(self, result: Node, node: dict) -> None:
        inner: list[dict] = node.get("inner", [])
        kind: str = result.kind
        callee: Node | None = None
        raw_arguments: list[dict] = inner
        function_type: str = ""
        if kind != "CXXConstructExpr":
            if inner:
                raw_callee: dict = inner[0]
                if kind == "CXXMemberCallExpr":
                    result.constant = const_object(raw_callee)
                function_type = named_function_type(raw_callee)
                callee = self.convert(raw_callee)
            raw_arguments = inner[1:]
        else:
            type_name: str = node.get("type", {}).get("qualType", "")
            result.name = type_name.split("<", 1)[0].split("::")[-1].strip()
            function_type = node.get("ctorType", {}).get("qualType", "")
        arguments: list[Node] = []
        raw: dict
        for raw in raw_arguments:
            converted: Node | None = self.convert(raw)
            if converted is None:
                continue
            arguments.append(converted)
            result.writes.append(raw.get("valueCategory") == "lvalue" and (
                raw.get("kind") in REFERENCE_EXPRESSIONS
                or (raw.get("kind") == "UnaryOperator" and raw.get("opcode") == "*")))
        parameters: list[str] | None = parameter_types(function_type)
        result.parameters = [""] * len(arguments)
        if parameters is not None:
            result.writes = parameter_writes(kind, function_type, parameters, len(arguments))
            result.parameters = argument_parameters(kind, parameters, len(arguments))
            if kind == "CXXOperatorCallExpr" and len(arguments) == len(parameters) + 1:
                result.constant = CONST_FUNCTION_TYPE.search(function_type) is not None
        assign_call_parts(result, callee, arguments)
        self.calls.append(result)

    def lambda_body(self, result: Node, node: dict) -> None:
        """A lambda reads as its body: the closure class and captures are left out."""
        inner: list[dict] = node.get("inner", [])
        child: dict
        for child in inner[:-1]:
            self.skim(child)
        if inner:
            body: Node | None = self.convert(inner[-1])
            if body is not None:
                result.children.append(body)

    def resolve(self) -> None:
        """Describes the targets of references whose declarations this unit read, then the calls through them."""
        node: Node
        for node in self.references:
            if not node.qualified:
                node.qualified = self.declared.get(node.target, "")
            if not node.qualified and node.target_kind == "ParmVarDecl":
                node.qualified = node.name      # a parameter is always local: its bare name
            if not node.target_kind:
                node.target_kind = self.declared_kinds.get(node.target, "")
        for node in self.calls:
            callee: Node | None = node.roles.get("callee")
            if callee is None:
                continue
            node.target = node.target or callee.target
            node.target_kind = node.target_kind or callee.target_kind
            node.qualified = node.qualified or callee.qualified
            if node.kind == "CXXMemberCallExpr" and node.target in self.declared_types and not any(node.parameters):
                # A member call's callee carries no function type; the method's declaration, when read, does.
                parameters: list[str] | None = parameter_types(self.declared_types[node.target])
                if parameters is not None:
                    node.parameters = argument_parameters(node.kind, parameters, len(node.arguments))


def scope_of(node: Node) -> Scope:
    if node.kind == "FriendDecl":
        return Scope("", "friend", "")
    if node.kind in ("NamespaceDecl", "EnumDecl"):
        return Scope(node.name if node.name else "(anonymous)", "namespace", "")
    if node.kind == "CXXRecordDecl":
        default_access: str = "private" if node.tag == "class" else "public"
        return Scope(node.name if node.name else "(anonymous)", "record", default_access)
    return Scope(node.name, "function", "")


def named_function_type(raw_callee: dict) -> str:
    """The function type of a callee that names a function (through its implicit decay), or ""."""
    node: dict = raw_callee
    while node.get("kind") == "ImplicitCastExpr" and node.get("inner"):
        node = node["inner"][0]
    if node.get("kind") != "DeclRefExpr":
        return ""
    result: str = node.get("referencedDecl", {}).get("type", {}).get("qualType", "")
    return result


def parameter_types(function_type: str) -> list[str] | None:
    """The parameter types of a function type as clang writes it ("R (P1, P2) const &"), or None if unreadable.

    The parameter list is the last top-level parenthesized group that is not a noexcept or throw operand.
    """
    groups: list[tuple[int, int]] = []
    depth: int = 0
    opening: int = -1
    index: int
    for index in range(len(function_type)):
        character: str = function_type[index]
        if character in "<[":
            depth += 1
        elif character in ">]":
            depth -= 1
        elif character == "(":
            if depth == 0 and opening < 0:
                opening = index
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0 and opening >= 0:
                groups.append((opening, index))
                opening = -1
    chosen: tuple[int, int] | None = None
    group: tuple[int, int]
    for group in groups:
        before: str = function_type[:group[0]].rstrip()
        if not before.endswith("noexcept") and not before.endswith("throw"):
            chosen = group
    if chosen is None:
        return None
    parameters: list[str] = []
    current: list[str] = []
    depth = 0
    for index in range(chosen[0] + 1, chosen[1]):
        character = function_type[index]
        if character in "<([":
            depth += 1
        elif character in ">)]":
            depth -= 1
        elif character == "," and depth == 0:
            parameters.append("".join(current).strip())
            current = []
            continue
        current.append(character)
    last: str = "".join(current).strip()
    if last and last != "void":
        parameters.append(last)
    return parameters


def parameter_writes(kind: str, function_type: str, parameters: list[str], count: int) -> list[bool]:
    """Per argument: its parameter is a non-const lvalue reference; for a member operator, the object comes
    first and is written unless the operator is const."""
    writes: list[bool] = []
    if kind == "CXXOperatorCallExpr" and count == len(parameters) + 1:
        writes.append(CONST_FUNCTION_TYPE.search(function_type) is None)
    position: int
    for position in range(count - len(writes)):
        if position >= len(parameters) or parameters[position] == "...":
            writes.append(False)
            continue
        parameter: str = parameters[position]
        writes.append(parameter.endswith("&") and not parameter.endswith("&&") and not parameter.startswith("const "))
    return writes


def argument_parameters(kind: str, parameters: list[str], count: int) -> list[str]:
    """Per argument: its parameter's type, or "" for a member operator's object and past the declared
    parameters (a variadic call's extra arguments)."""
    types: list[str] = []
    if kind == "CXXOperatorCallExpr" and count == len(parameters) + 1:
        types.append("")
    position: int
    for position in range(count - len(types)):
        if position >= len(parameters) or parameters[position] == "...":
            types.append("")
            continue
        types.append(written_type(parameters[position]))
    return types


def const_object(raw_callee: dict) -> bool:
    """Whether a member call calls a const member function: its object reaches it through a NoOp cast to
    const, or the object is const already (only a const member function can be called on it)."""
    if raw_callee.get("kind") != "MemberExpr":
        return False
    objects: list[dict] = raw_callee.get("inner", [])
    if not objects:
        return False
    target: dict = objects[0]
    if target.get("kind") == "ImplicitCastExpr" and target.get("castKind") == "NoOp":
        return True
    object_type: str = target.get("type", {}).get("qualType", "")
    if raw_callee.get("isArrow"):
        result: bool = object_type.startswith("const ") and object_type.rstrip().endswith("*")
        return result
    result = object_type.startswith("const ")
    return result


def read_unit(unit: UnitCommand, keep: Callable[[str], bool]) -> Iterator[Node]:
    """Runs one front-end dump and yields the model of its kept declarations; the dump never reaches disk."""
    process: subprocess.Popen[str] = subprocess.Popen(dump_command(unit), cwd=unit.directory, stdout=subprocess.PIPE,
                                                      stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                                      errors="replace", bufsize=1 << 20)
    stream: TextIO | None = process.stdout
    if stream is None:
        raise RuntimeError("clang produced no output stream")
    converter: Converter = Converter(unit.directory, SourceText())
    kept: list[Node] = []
    keeper: KeepAbsolute = KeepAbsolute(unit.directory, keep)
    try:
        declaration: Declaration
        for declaration in declarations(stream, keeper.keep):
            converter.file = declaration.file_before
            converter.scopes = []
            converted: Node | None = converter.convert(declaration.node)
            if converted is not None:
                kept.append(converted)
    finally:
        stream.close()
        process.wait()
    if process.returncode != 0:
        raise ReadError(f"{absolute(unit.directory, unit.file)}: clang exited with {process.returncode}")
    converter.resolve()
    yield from kept


class KeepAbsolute:
    """A caller's keep test, given absolute paths although the dump writes names as the command did."""

    def __init__(self, directory: str, keep: Callable[[str], bool]) -> None:
        self.directory: str = directory
        self.test: Callable[[str], bool] = keep

    def keep(self, file: str) -> bool:
        if not file:
            return False
        result: bool = self.test(absolute(self.directory, file))
        return result
