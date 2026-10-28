"""The libclang backend of clang_ast.py: the pinned LLVM's clang.cindex, in process.

Where the bindings come from: the host's pinned LLVM 22, never a package
index. The module imports `clang.cindex` as it is; when that fails it looks
beside the pinned clang++ found on PATH (the LLVM prefix is the directory
above its bin/):

  <prefix>/lib/python3*/site-packages     Windows CLANG64; Homebrew llvm@22
  <prefix>/lib/python3/dist-packages      apt.llvm.org python3-clang-22
  /usr/lib/python3/dist-packages          the same package, Debian's path

and loads libclang from the same prefix (bin/libclang.dll, lib/libclang.dylib,
lib/libclang.so.1). The clang driver inside libclang does not know where it
is installed, so every parse passes `-ccc-install-dir <the unit compiler's
bin>` and that compiler's `-resource-dir`: the standard library and builtin
headers resolve exactly as the build's own compiler resolves them.

Locations: libclang reports a node's expansion location, which is what the
model's `begin` and `end` are. The spelling location (where the text really
is: a macro's definition or argument) is read through clang_getSpellingLocation
for operator spellings, literal values and statement headers, and a node
whose spelling and expansion differ is `in_macro`. Both are read through a
second handle on the same library, so cindex's own registrations stay as they
are.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import glob
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Callable, Iterator

from clang_ast import (CONST_FUNCTION_TYPE, FUNCTIONS, Node, ReadError, SourceText, UnitCommand, absolute,
                       assign_call_parts, assign_positional_roles, written_type)

KIND_NAMES: dict[str, str] = {
    "IF_STMT": "IfStmt", "SWITCH_STMT": "SwitchStmt", "WHILE_STMT": "WhileStmt", "DO_STMT": "DoStmt",
    "FOR_STMT": "ForStmt", "CXX_FOR_RANGE_STMT": "CXXForRangeStmt", "COMPOUND_STMT": "CompoundStmt",
    "DECL_STMT": "DeclStmt", "RETURN_STMT": "ReturnStmt", "BREAK_STMT": "BreakStmt",
    "CONTINUE_STMT": "ContinueStmt", "GOTO_STMT": "GotoStmt", "LABEL_STMT": "LabelStmt", "CASE_STMT": "CaseStmt",
    "DEFAULT_STMT": "DefaultStmt", "NULL_STMT": "NullStmt", "CXX_TRY_STMT": "CXXTryStmt",
    "CXX_CATCH_STMT": "CXXCatchStmt", "CXX_THROW_EXPR": "CXXThrowExpr", "BINARY_OPERATOR": "BinaryOperator",
    "COMPOUND_ASSIGNMENT_OPERATOR": "CompoundAssignOperator", "UNARY_OPERATOR": "UnaryOperator",
    "CONDITIONAL_OPERATOR": "ConditionalOperator", "ARRAY_SUBSCRIPT_EXPR": "ArraySubscriptExpr",
    "MEMBER_REF_EXPR": "MemberExpr", "DECL_REF_EXPR": "DeclRefExpr", "CXX_THIS_EXPR": "CXXThisExpr",
    "CXX_BOOL_LITERAL_EXPR": "CXXBoolLiteralExpr", "CXX_NULL_PTR_LITERAL_EXPR": "CXXNullPtrLiteralExpr",
    "GNU_NULL_EXPR": "GNUNullExpr", "INTEGER_LITERAL": "IntegerLiteral", "FLOATING_LITERAL": "FloatingLiteral",
    "STRING_LITERAL": "StringLiteral", "CHARACTER_LITERAL": "CharacterLiteral", "PAREN_EXPR": "ParenExpr",
    "INIT_LIST_EXPR": "InitListExpr", "CSTYLE_CAST_EXPR": "CStyleCastExpr",
    "CXX_STATIC_CAST_EXPR": "CXXStaticCastExpr", "CXX_DYNAMIC_CAST_EXPR": "CXXDynamicCastExpr",
    "CXX_REINTERPRET_CAST_EXPR": "CXXReinterpretCastExpr", "CXX_CONST_CAST_EXPR": "CXXConstCastExpr",
    "CXX_FUNCTIONAL_CAST_EXPR": "CXXFunctionalCastExpr", "CXX_NEW_EXPR": "CXXNewExpr",
    "CXX_DELETE_EXPR": "CXXDeleteExpr", "LAMBDA_EXPR": "LambdaExpr", "CXX_UNARY_EXPR": "UnaryExprOrTypeTraitExpr",
    "VAR_DECL": "VarDecl", "PARM_DECL": "ParmVarDecl", "FIELD_DECL": "FieldDecl", "FUNCTION_DECL": "FunctionDecl",
    "CXX_METHOD": "CXXMethodDecl", "CONSTRUCTOR": "CXXConstructorDecl", "DESTRUCTOR": "CXXDestructorDecl",
    "CONVERSION_FUNCTION": "CXXConversionDecl", "STRUCT_DECL": "CXXRecordDecl", "CLASS_DECL": "CXXRecordDecl",
    "UNION_DECL": "CXXRecordDecl", "CLASS_TEMPLATE": "CXXRecordDecl",
    "CLASS_TEMPLATE_PARTIAL_SPECIALIZATION": "CXXRecordDecl", "NAMESPACE": "NamespaceDecl",
    "ENUM_DECL": "EnumDecl", "ENUM_CONSTANT_DECL": "EnumConstantDecl", "TYPEDEF_DECL": "TypedefDecl",
    "TYPE_ALIAS_DECL": "TypeAliasDecl", "TYPE_ALIAS_TEMPLATE_DECL": "TypeAliasDecl",
    "USING_DECLARATION": "UsingDecl", "USING_DIRECTIVE": "UsingDirectiveDecl", "STATIC_ASSERT": "StaticAssertDecl",
    "FRIEND_DECL": "FriendDecl", "CXX_ACCESS_SPEC_DECL": "AccessSpecDecl", "LINKAGE_SPEC": "LinkageSpecDecl",
    "NAMESPACE_ALIAS": "NamespaceAliasDecl", "CXX_TYPEID_EXPR": "CXXTypeidExpr",
    "CXX_NOEXCEPT_EXPR": "CXXNoexceptExpr", "PACK_EXPANSION_EXPR": "PackExpansionExpr",
    "SIZE_OF_PACK_EXPR": "SizeOfPackExpr", "CONCEPT_DECL": "ConceptDecl",
    # Left out of the model, but named as what a reference refers to:
    "TEMPLATE_TYPE_PARAMETER": "TemplateTypeParmDecl", "TEMPLATE_NON_TYPE_PARAMETER": "NonTypeTemplateParmDecl",
    "TEMPLATE_TEMPLATE_PARAMETER": "TemplateTemplateParmDecl",
}
# Declarations whose expression children are part of the model (an initializer, a value, an assertion);
# any other declaration's expressions belong to its type (template arguments, array bounds) and are left out.
EXPRESSION_OWNERS: frozenset[str] = frozenset({"VarDecl", "ParmVarDecl", "FieldDecl", "EnumConstantDecl",
                                               "StaticAssertDecl"})
# Cursor kinds the model leaves out, with everything under them (references are left out by is_reference()).
DROPPED_KINDS: frozenset[str] = frozenset({
    "TEMPLATE_TYPE_PARAMETER", "TEMPLATE_NON_TYPE_PARAMETER", "TEMPLATE_TEMPLATE_PARAMETER", "CXX_BASE_SPECIFIER",
    "MACRO_DEFINITION", "MACRO_INSTANTIATION", "INCLUSION_DIRECTIVE", "OVERLOADED_DECL_REF"})
RECORD_KINDS: frozenset[str] = frozenset({"STRUCT_DECL", "CLASS_DECL", "UNION_DECL", "CLASS_TEMPLATE",
                                          "CLASS_TEMPLATE_PARTIAL_SPECIALIZATION"})
SCOPE_KINDS: frozenset[str] = RECORD_KINDS | {"NAMESPACE"}
TAGS: dict[str, str] = {"STRUCT_DECL": "struct", "CLASS_DECL": "class", "UNION_DECL": "union"}
LITERALS: frozenset[str] = frozenset({"IntegerLiteral", "FloatingLiteral", "StringLiteral", "CharacterLiteral",
                                      "CXXBoolLiteralExpr", "CXXNullPtrLiteralExpr", "GNUNullExpr"})
REFERENCE_EXPRESSIONS: frozenset[str] = frozenset({"DeclRefExpr", "MemberExpr", "ArraySubscriptExpr"})


class Library:
    """The loaded bindings and the second handle for the location calls cindex does not register."""

    def __init__(self, cindex: Any, path: str) -> None:
        self.cindex: Any = cindex
        handle: ctypes.CDLL = ctypes.CDLL(path)
        self.extent: Any = handle.clang_getCursorExtent
        self.extent.argtypes = [cindex.Cursor]
        self.extent.restype = cindex.SourceRange
        self.range_start: Any = handle.clang_getRangeStart
        self.range_start.argtypes = [cindex.SourceRange]
        self.range_start.restype = cindex.SourceLocation
        self.range_end: Any = handle.clang_getRangeEnd
        self.range_end.argtypes = [cindex.SourceRange]
        self.range_end.restype = cindex.SourceLocation
        self.cursor_location: Any = handle.clang_getCursorLocation
        self.cursor_location.argtypes = [cindex.Cursor]
        self.cursor_location.restype = cindex.SourceLocation
        location_arguments: list[Any] = [cindex.SourceLocation, ctypes.POINTER(ctypes.c_void_p),
                                         ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
                                         ctypes.POINTER(ctypes.c_uint)]
        self.expansion: Any = handle.clang_getExpansionLocation
        self.expansion.argtypes = location_arguments
        self.expansion.restype = None
        self.spelling: Any = handle.clang_getSpellingLocation
        self.spelling.argtypes = location_arguments
        self.spelling.restype = None
        self.file_name: Any = handle.clang_getFileName
        self.file_name.argtypes = [ctypes.c_void_p]
        self.file_name.restype = cindex._CXString

    def point(self, location: Any, spelling: bool, names: dict[int, str]) -> tuple[str, int]:
        """A location's file name and byte offset: its expansion's, or with `spelling` its spelling's.

        `names` caches file names by CXFile handle for ONE translation unit:
        a handle is the unit's own pointer, and once the unit is freed the
        next unit's files may reuse its address, so a cache shared across
        units can name a file after another unit's file.
        """
        file: ctypes.c_void_p = ctypes.c_void_p()
        line: ctypes.c_uint = ctypes.c_uint()
        column: ctypes.c_uint = ctypes.c_uint()
        offset: ctypes.c_uint = ctypes.c_uint()
        reader: Any = self.spelling if spelling else self.expansion
        reader(location, ctypes.byref(file), ctypes.byref(line), ctypes.byref(column), ctypes.byref(offset))
        handle: int = file.value or 0
        if handle not in names:
            names[handle] = self.cindex._CXString.from_result(self.file_name(handle)) if handle else ""
        result: tuple[str, int] = (names[handle], int(offset.value) if handle else -1)
        return result


LOADED: list[Library] = []
PROBLEM: list[str] = []


def pinned_prefix() -> Path | None:
    """The LLVM prefix of the clang++ on PATH (the directory above its bin/)."""
    found: str | None = shutil.which("clang++")
    if found is None:
        return None
    result: Path = Path(found).resolve().parent.parent
    return result


def binding_directories(prefix: Path) -> list[str]:
    patterns: list[str] = [str(prefix / "lib" / "python3*" / "site-packages"), str(prefix / "lib" / "python3" /
                                                                                 "dist-packages")]
    if sys.platform.startswith("linux"):
        patterns.append("/usr/lib/python3/dist-packages")
    directories: list[str] = []
    pattern: str
    for pattern in patterns:
        directory: str
        for directory in sorted(glob.glob(pattern)):
            if Path(directory, "clang", "cindex.py").is_file():
                directories.append(directory)
    return directories


def library_file(prefix: Path) -> str:
    candidates: list[Path] = [prefix / "bin" / "libclang.dll", prefix / "lib" / "libclang.dylib",
                              prefix / "lib" / "libclang.so.1", prefix / "lib" / "libclang.so"]
    candidate: Path
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def load_problem() -> str:
    """Loads the bindings once; "" when they loaded, or what went wrong."""
    if LOADED:
        return ""
    if PROBLEM:
        return PROBLEM[0]
    prefix: Path | None = pinned_prefix()
    try:
        cindex: Any = import_cindex(prefix)
        path: str = library_file(prefix) if prefix is not None else ""
        if not cindex.Config.loaded and path:
            cindex.Config.set_library_file(path)
        index: Any = cindex.Index.create()
        del index
        if not path:
            path = cindex.conf.get_filename()
        LOADED.append(Library(cindex, path))
    except Exception as error:      # an import, load or symbol failure is this module's answer, not a crash
        PROBLEM.append(f"{type(error).__name__}: {error}")
        return PROBLEM[0]
    return ""


def import_cindex(prefix: Path | None) -> Any:
    try:
        import clang.cindex as cindex
        return cindex
    except ImportError:
        if prefix is None:
            raise ImportError("no clang++ on PATH, and clang.cindex is not importable") from None
    directories: list[str] = binding_directories(prefix)
    if not directories:
        raise ImportError(f"no clang.cindex beside the pinned LLVM at {prefix}")
    sys.path.insert(0, directories[0])
    import clang.cindex as found
    return found


# --- the driver's view of the build compiler ---------------------------------------------

RESOURCE_DIRECTORIES: dict[str, str] = {}


def compiler_path(unit: UnitCommand) -> str:
    compiler: str = unit.command[0]
    if not Path(compiler).is_absolute():
        found: str | None = shutil.which(compiler)
        if found is not None:
            compiler = found
    result: str = Path(compiler).resolve().as_posix()
    return result


def resource_directory(compiler: str) -> str:
    if compiler not in RESOURCE_DIRECTORIES:
        printed: str = subprocess.check_output([compiler, "-print-resource-dir"], text=True).strip()
        RESOURCE_DIRECTORIES[compiler] = Path(printed).as_posix()
    return RESOURCE_DIRECTORIES[compiler]


def parse_arguments(unit: UnitCommand) -> list[str]:
    """The unit's arguments for libclang: no compiler, no source, and the compiler's install and resource dirs."""
    source: str = absolute(unit.directory, unit.file).lower()
    arguments: list[str] = []
    argument: str
    for argument in unit.command[1:]:
        if not argument.startswith("-") and absolute(unit.directory, argument).lower() == source:
            continue
        arguments.append(argument)
    compiler: str = compiler_path(unit)
    arguments.extend(["-Wno-everything", "-ccc-install-dir", str(Path(compiler).parent.as_posix()),
                      "-resource-dir", resource_directory(compiler)])
    return arguments


# --- statement headers ------------------------------------------------------------------------

@dataclass(frozen=True)
class Header:
    """The byte offsets of a statement header's depth-one semicolons and its closing parenthesis."""
    semicolons: tuple[int, ...]
    close: int


def statement_header(data: bytes, start: int) -> Header:
    """Scans `if (...)`, `for (...)` or `switch (...)` from the statement's first byte; close is -1 if unseen."""
    opening: int = data.find(b"(", start)
    if opening < 0:
        return Header((), -1)
    semicolons: list[int] = []
    depth: int = 0
    index: int = opening
    length: int = len(data)
    while index < length:
        byte: int = data[index]
        if byte in b"([{":
            depth += 1
        elif byte in b")]}":
            depth -= 1
            if depth == 0:
                return Header(tuple(semicolons), index)
        elif byte == ord(";") and depth == 1:
            semicolons.append(index)
        elif byte == ord('"') or (byte == ord("'") and not digit_separator(data, index)):
            index = literal_end(data, index)
        elif data.startswith(b"//", index):
            index = data.find(b"\n", index)
            if index < 0:
                break
        elif data.startswith(b"/*", index):
            index = data.find(b"*/", index + 2)
            if index < 0:
                break
            index += 1
        index += 1
    return Header(tuple(semicolons), -1)


def token_end(data: bytes, start: int) -> int:
    """The end of the literal token at `start`: a quoted literal, or a run of number or identifier characters."""
    if start >= len(data):
        return start
    index: int = start
    if data[index] in b"\"'" or (index + 1 < len(data) and data[index + 1] in b"\"'"):
        quote: int = data.find(b'"', start) if b'"' in data[start:start + 4] else data.find(b"'", start)
        return literal_end(data, quote) + 1 if quote >= 0 else start
    previous: int = 0
    while index < len(data):
        byte: int = data[index]
        sign: bool = byte in b"+-" and previous in b"eEpP"
        if not (chr(byte).isalnum() or byte in b"_.'" or sign):
            break
        previous = byte
        index += 1
    return index


def digit_separator(data: bytes, index: int) -> bool:
    before: bool = index > 0 and chr(data[index - 1]).isalnum()
    after: bool = index + 1 < len(data) and chr(data[index + 1]).isalnum()
    return before and after


def literal_end(data: bytes, index: int) -> int:
    """The offset of a string or character literal's closing quote."""
    quote: int = data[index]
    index += 1
    while index < len(data) and data[index] != quote:
        if data[index] == ord("\\"):
            index += 1
        index += 1
    return index


# --- conversion -------------------------------------------------------------------------------

@dataclass
class Spelled:
    """Where a node's text is: its spelling file and the byte offsets of its first and last token ends."""
    file: str
    begin: int
    end: int


class Converter:
    def __init__(self, library: Library, unit: UnitCommand, source: SourceText) -> None:
        self.library: Library = library
        self.cindex: Any = library.cindex
        self.directory: str = unit.directory
        self.source: SourceText = source
        # id(node) -> (node, where its text is). The node is held so that its id
        # cannot pass to a later node while this converter lives.
        self.spelled: dict[int, tuple[Node, Spelled]] = {}
        self.qualified_names: dict[str, str] = {}   # identity -> qualified name
        self.file_names: dict[int, str] = {}        # CXFile handle -> name, for this unit only (Library.point)

    # --- locations ---------------------------------------------------------------------



    def spelled_of(self, node: Node) -> Spelled | None:
        entry: tuple[Node, Spelled] | None = self.spelled.get(id(node))
        result: Spelled | None = entry[1] if entry is not None and entry[0] is node else None
        return result
    def point(self, location: Any, spelling: bool) -> tuple[str, int]:
        result: tuple[str, int] = self.library.point(location, spelling, self.file_names)
        return result
    def file(self, name: str) -> str:
        return absolute(self.directory, name) if name else ""

    def located(self, kind: str, cursor: Any) -> Node:
        extent: Any = self.library.extent(cursor)
        start: Any = self.library.range_start(extent)
        stop: Any = self.library.range_end(extent)
        begin_file: str
        begin: int
        end_file: str
        end: int
        begin_file, begin = self.point(start, False)
        end_file, end = self.point(stop, False)
        spelling_file: str
        spelling_begin: int
        spelling_end_file: str
        spelling_end: int
        spelling_file, spelling_begin = self.point(start, True)
        spelling_end_file, spelling_end = self.point(stop, True)
        in_macro: bool = (spelling_file, spelling_begin) != (begin_file, begin) or (
            (spelling_end_file, spelling_end) != (end_file, end))
        node: Node = Node(kind, self.file(begin_file), begin, end, in_macro)
        if spelling_begin >= 0:
            # A macro's tokens end where the expansion says, not where their spelling does: no end then.
            stop: int = spelling_end if not in_macro and spelling_file == spelling_end_file else -1
            self.spelled[id(node)] = (node, Spelled(self.file(spelling_file), spelling_begin, stop))
        return node

    def identity(self, cursor: Any) -> str:
        """A declaration's unit-local key: its kind and the expansion location of its name."""
        file: str
        offset: int
        file, offset = self.point(self.library.cursor_location(cursor), False)
        result: str = cursor.kind.name + "@" + file + ":" + str(offset)
        return result

    def qualified(self, cursor: Any, identity: str) -> str:
        if identity in self.qualified_names:
            return self.qualified_names[identity]
        names: list[str] = [cursor.spelling]
        parent: Any = cursor.semantic_parent
        while parent is not None and (parent.kind.name in SCOPE_KINDS or parent.kind.name == "ENUM_DECL"):
            # A scoped enumeration qualifies its enumerators; an unscoped one leaves them in the enclosing scope.
            if parent.kind.name != "ENUM_DECL" or parent.is_scoped_enum():
                names.append(parent.spelling if parent.spelling else "(anonymous)")
            parent = parent.semantic_parent
        names.reverse()
        result: str = "::".join(names)
        self.qualified_names[identity] = result
        return result

    def text(self, node: Node) -> str:
        spelled: Spelled | None = self.spelled_of(node)
        if spelled is None:
            return ""
        result: str = self.source.text(spelled.file, spelled.begin, spelled.end)
        return result

    # --- nodes -------------------------------------------------------------------------

    def kind_of(self, cursor: Any) -> str:
        name: str = cursor.kind.name
        if name in KIND_NAMES:
            return KIND_NAMES[name]
        if name == "FUNCTION_TEMPLATE":
            parent: Any = cursor.semantic_parent
            if parent is not None and parent.kind.name in RECORD_KINDS:
                return "CXXConstructorDecl" if cursor.spelling == parent.spelling else "CXXMethodDecl"
            return "FunctionDecl"
        words: list[str] = name.split("_")
        result: str = "".join(word if word == "CXX" else word.capitalize() for word in words)
        return result

    def convert(self, cursor: Any) -> Node | None:
        name: str = cursor.kind.name
        if name in DROPPED_KINDS or cursor.kind.is_reference() or cursor.kind.is_attribute():
            return None
        if name == "UNEXPOSED_EXPR":
            return self.unexposed(cursor)
        kind: str = self.kind_of(cursor)
        node: Node = self.located(kind, cursor)
        self.describe(node, cursor)
        if name == "CALL_EXPR":
            self.call(node, cursor)
        elif kind == "MemberExpr":
            self.member(node, cursor)
        elif kind == "CXXConstructorDecl":
            self.constructor(node, cursor)
        elif kind == "LambdaExpr":
            self.lambda_body(node, cursor)
        else:
            types_only: bool = cursor.kind.is_declaration() and kind not in EXPRESSION_OWNERS
            self.convert_children(node, cursor, types_only)
            if kind in ("VarDecl", "ParmVarDecl", "FieldDecl"):
                self.initializer_only(node, cursor)
            self.assign_roles(node)
        return node

    def initializer_only(self, node: Node, cursor: Any) -> None:
        """Keeps a variable's initializer and drops the expressions of its declarator (array bounds,
        a bit-field width), as the dump does: an initializer follows the name directly or after `=`."""
        if node.in_macro:
            return
        file: str
        offset: int
        file, offset = self.point(self.library.cursor_location(cursor), True)
        name_end: int = offset + len(cursor.spelling.encode("utf-8"))
        kept: list[Node] = []
        child: Node
        for child in node.children:
            spelled: Spelled | None = self.spelled_of(child)
            if spelled is None or spelled.file != self.file(file):
                kept.append(child)
                continue
            if spelled.begin < offset:
                continue                # an expression of the type, before the name
            if spelled.begin <= name_end:
                kept.append(child)      # a direct initialization spelled from the name: `T name(...)`
                continue
            gap: str = self.source.text(spelled.file, name_end, spelled.begin).strip()
            if not gap or gap.endswith("="):
                kept.append(child)
        node.children = kept

    def unexposed(self, cursor: Any) -> Node | None:
        """An implicit expression: its one child stands in its place; with none it is left out."""
        children: list[Node] = []
        child: Any
        for child in cursor.get_children():
            converted: Node | None = self.convert(child)
            if converted is not None:
                children.append(converted)
        if not children:
            return None
        if len(children) == 1:
            return children[0]
        node: Node = self.located("UnexposedExpr", cursor)
        node.children = children
        return node

    def convert_children(self, node: Node, cursor: Any, types_only: bool = False) -> None:
        """The children's model; with `types_only`, expressions (which belong to a declaration's type) are
        left out."""
        child: Any
        for child in cursor.get_children():
            if types_only and child.kind.is_expression():
                continue
            if child.kind.name == "PARM_DECL" and node.kind not in FUNCTIONS:
                continue                # a parameter of a function type written in a declaration's type
            converted: Node | None = self.convert(child)
            if converted is not None:
                node.children.append(converted)

    def describe(self, node: Node, cursor: Any) -> None:
        name: str = cursor.kind.name
        kind: str = node.kind
        if cursor.kind.is_declaration():
            node.name = cursor.spelling
            node.identity = self.identity(cursor)
            if node.name:
                node.qualified = self.qualified(cursor, node.identity)
            if kind in ("VarDecl", "ParmVarDecl", "FieldDecl") or kind in FUNCTIONS:
                node.type = written_type(cursor.type.spelling)
            access: str = cursor.access_specifier.name.lower()
            parent: Any = cursor.semantic_parent
            member: bool = parent is not None and parent.kind.name in RECORD_KINDS
            if access in ("public", "protected", "private") and kind != "FriendDecl" and member:
                node.access = access
            if kind in FUNCTIONS or name in RECORD_KINDS:
                node.definition = bool(cursor.is_definition())
        if kind in FUNCTIONS and name in ("CXX_METHOD", "CONVERSION_FUNCTION"):
            node.constant = bool(cursor.is_const_method())
        elif kind in ("CXXMethodDecl", "CXXConversionDecl") and name == "FUNCTION_TEMPLATE":
            # libclang answers is_const_method only for a method, not a member function template.
            node.constant = CONST_FUNCTION_TYPE.search(node.type) is not None
        if name in RECORD_KINDS:
            template_kind: str = name
            if name in ("CLASS_TEMPLATE", "CLASS_TEMPLATE_PARTIAL_SPECIALIZATION"):
                template_kind = self.cindex.CursorKind.from_id(
                    self.cindex.conf.lib.clang_getTemplateCursorKind(cursor)).name
            node.tag = TAGS.get(template_kind, "")
        if kind == "DeclRefExpr":
            self.reference(node, cursor)
        if kind in ("BinaryOperator", "CompoundAssignOperator"):
            node.opcode = cursor.spelling

    def reference(self, node: Node, cursor: Any) -> None:
        referenced: Any = cursor.referenced
        if referenced is None:
            node.name = cursor.spelling
            return
        node.name = referenced.spelling
        node.target = self.identity(referenced)
        node.target_kind = self.kind_of(referenced)
        node.qualified = self.qualified(referenced, node.target)

    def assign_roles(self, node: Node) -> None:
        kind: str = node.kind
        if kind in LITERALS:
            spelled: Spelled | None = self.spelled_of(node)
            if spelled is not None:
                data: bytes = self.source.bytes_of(spelled.file)
                node.value = data[spelled.begin:token_end(data, spelled.begin)].decode("utf-8", "replace")
            return
        if kind == "UnaryOperator" and len(node.children) == 1:
            self.unary(node)
        if kind in ("IfStmt", "SwitchStmt", "ForStmt"):
            self.header_roles(node)
        elif kind == "WhileStmt":
            names: list[str] = ["condition", "body"]
            if len(node.children) == 3:
                names = ["variable", "condition", "body"]
            self.name_in_order(node, names)
        elif kind == "CXXForRangeStmt":
            self.range_for(node)
        else:
            assign_positional_roles(node)

    def name_in_order(self, node: Node, names: list[str]) -> None:
        index: int
        for index in range(min(len(names), len(node.children))):
            node.roles[names[index]] = node.children[index]

    def unary(self, node: Node) -> None:
        operand: Node = node.children[0]
        mine: Spelled | None = self.spelled_of(node)
        theirs: Spelled | None = self.spelled_of(operand)
        if mine is not None and theirs is not None and mine.file == theirs.file:
            before: str = self.source.text(mine.file, mine.begin, theirs.begin).strip()
            after: str = ""
            if theirs.end >= 0 and mine.end >= 0:
                after = self.source.text(mine.file, theirs.end, mine.end).strip()
            node.opcode = before if before else after
            node.postfix = not before and bool(after)
        node.roles["operand"] = operand

    def header_roles(self, node: Node) -> None:
        """The parts of an if, switch or for by where each child starts against the header's ; and )."""
        kind: str = node.kind
        spelled: Spelled | None = self.spelled_of(node)
        header: Header = Header((), -1)
        if not node.in_macro and spelled is not None:
            header = statement_header(self.source.bytes_of(spelled.file), spelled.begin)
        if header.close < 0:
            self.header_roles_by_kind(node)
            return
        after: list[str] = ["then", "else"] if kind == "IfStmt" else ["body"]
        child: Node
        for child in node.children:
            role: str
            if child.begin > header.close:
                role = after.pop(0) if after else ""
            elif kind == "ForStmt":
                regions: list[str] = ["init", "condition", "increment"]
                role = regions[min(2, sum(1 for mark in header.semicolons if mark < child.begin))]
                if role == "condition" and child.kind == "VarDecl":
                    role = "variable"
            elif header.semicolons and child.begin < header.semicolons[0]:
                role = "init"
            elif child.kind == "VarDecl":
                role = "variable"
            else:
                role = "condition"
            if role:
                node.roles[role] = child

    def header_roles_by_kind(self, node: Node) -> None:
        """Roles when the header cannot be read (a statement a macro wrote): no init statement is assumed."""
        children: list[Node] = list(node.children)
        if node.kind == "ForStmt":
            if children:
                node.roles["body"] = children[-1]
            return
        if children and children[0].kind == "VarDecl" and len(children) >= 3:
            node.roles["variable"] = children.pop(0)
        names: list[str] = ["condition", "then", "else"] if node.kind == "IfStmt" else ["condition", "body"]
        index: int
        for index in range(min(len(names), len(children))):
            node.roles[names[index]] = children[index]

    def range_for(self, node: Node) -> None:
        """init?, the loop variable (its implicit initializer left out), the range, the body."""
        position: int = -1
        index: int
        for index in range(len(node.children)):
            if node.children[index].kind == "VarDecl":
                position = index
                break
        if position < 0 or position + 2 >= len(node.children):
            return
        variable: Node = node.children[position]
        variable.children = []
        variable.roles = {}
        if position > 0:
            node.roles["init"] = node.children[position - 1]
        node.roles["variable"] = variable
        node.roles["range"] = node.children[position + 1]
        node.roles["body"] = node.children[-1]

    def member(self, node: Node, cursor: Any) -> None:
        node.name = cursor.spelling
        if not node.name:
            # A dependent member libclang does not name: the identifier at its location.
            file: str
            offset: int
            file, offset = self.point(self.library.cursor_location(cursor), True)
            if file and offset >= 0:
                data: bytes = self.source.bytes_of(self.file(file))
                node.name = data[offset:token_end(data, offset)].decode("utf-8", "replace")
        referenced: Any = cursor.referenced
        if referenced is not None:
            node.target = self.identity(referenced)
            node.target_kind = self.kind_of(referenced)
            node.qualified = self.qualified(referenced, node.target)
        self.convert_children(node, cursor)
        if not node.children:
            # The object is an implicit `this`.
            this: Node = Node("CXXThisExpr", node.file, node.begin, node.end, node.in_macro, implicit=True)
            node.children.append(this)
            node.arrow = True
        else:
            node.arrow = self.reached_with_arrow(node, cursor)
        node.roles["object"] = node.children[0]

    def reached_with_arrow(self, node: Node, cursor: Any) -> bool:
        object_spelled: Spelled | None = self.spelled_of(node.children[0])
        if object_spelled is None:
            return False
        file: str
        offset: int
        file, offset = self.point(self.library.cursor_location(cursor), True)
        if self.file(file) != object_spelled.file or offset < object_spelled.end:
            return False
        result: bool = "->" in self.source.text(object_spelled.file, object_spelled.end, offset)
        return result

    def call(self, node: Node, cursor: Any) -> None:
        raw: list[Any] = list(cursor.get_children())
        converted: list[Node] = []
        bound: list[bool] = []          # per converted child: an lvalue reference expression with no conversion
        child: Any
        for child in raw:
            result: Node | None = self.convert(child)
            if result is None:
                continue
            converted.append(result)
            bound.append(child.kind.name != "UNEXPOSED_EXPR" and result.kind in REFERENCE_EXPRESSIONS)
        referenced: Any = cursor.referenced
        callee: Node | None = None
        arguments: list[Node] = converted
        writes: list[bool] = bound
        if referenced is not None and referenced.kind.name == "CONSTRUCTOR":
            node.kind = "CXXConstructExpr"
            # The type as written (a typedef's name, not the class template's), as the dump names it.
            type_name: str = cursor.type.spelling
            node.name = type_name.split("<", 1)[0].split("::")[-1].strip() or referenced.spelling
        elif converted:
            operator_index: int = self.operator_callee(converted, referenced)
            position: int = operator_index
            if position < 0 and (node.in_macro or converted[0].begin == node.begin):
                # The callee starts the call; a dependent call whose callee libclang leaves out has none.
                position = 0
            if position >= 0:
                callee = converted[position]
                arguments = converted[:position] + converted[position + 1:]
                writes = bound[:position] + bound[position + 1:]
            if operator_index >= 0:
                node.kind = "CXXOperatorCallExpr"
            elif callee is not None and callee.kind == "MemberExpr":
                node.kind = "CXXMemberCallExpr"
            else:
                node.kind = "CallExpr"
        if referenced is not None:
            node.target = self.identity(referenced)
            node.target_kind = self.kind_of(referenced)
            node.qualified = self.qualified(referenced, node.target)
            if node.kind in ("CXXMemberCallExpr", "CXXOperatorCallExpr") and referenced.kind.name in (
                    "CXX_METHOD", "CONVERSION_FUNCTION"):
                node.constant = bool(referenced.is_const_method())
            writes = self.parameter_writes(node, referenced, arguments, writes)
            node.parameters = self.argument_parameters(node, referenced, arguments)
        else:
            node.parameters = [""] * len(arguments)
        node.writes = writes
        assign_call_parts(node, callee, arguments)

    def operator_callee(self, converted: list[Node], referenced: Any) -> int:
        """The index of an overloaded operator's callee, written as the operator itself; -1 for other calls."""
        if referenced is None or not referenced.spelling.startswith("operator"):
            return -1
        index: int
        for index in range(len(converted)):
            child: Node = converted[index]
            if child.kind == "DeclRefExpr" and child.name == referenced.spelling:
                if not self.text(child).startswith("operator"):
                    return index
        return -1

    def parameter_writes(self, node: Node, referenced: Any, arguments: list[Node], fallback: list[bool]) -> list[bool]:
        """Per argument: its parameter is a non-const lvalue reference (the object of a member operator: the
        operator is not const). Falls back to the bound-lvalue reading when the parameters are unknown."""
        parameters: list[Any] = [parameter for parameter in referenced.get_arguments() if parameter is not None]
        if not parameters and referenced.kind.name not in ("FUNCTION_DECL", "CXX_METHOD", "CONSTRUCTOR",
                                                            "CONVERSION_FUNCTION"):
            return fallback
        writes: list[bool] = []
        offset: int = 0
        if node.kind == "CXXOperatorCallExpr" and referenced.kind.name == "CXX_METHOD" and arguments:
            writes.append(not referenced.is_const_method())
            offset = 1
        reference: Any = self.cindex.TypeKind.LVALUEREFERENCE
        index: int
        for index in range(offset, len(arguments)):
            position: int = index - offset
            if position >= len(parameters):
                writes.append(False)
                continue
            parameter_type: Any = parameters[position].type
            writes.append(parameter_type.kind == reference and not parameter_type.get_pointee().is_const_qualified())
        return writes

    def argument_parameters(self, node: Node, referenced: Any, arguments: list[Node]) -> list[str]:
        """Per argument: its parameter's type as spelled, or "" for a member operator's object, past the
        declared parameters, and for a callee whose parameters are unknown."""
        parameters: list[Any] = [parameter for parameter in referenced.get_arguments() if parameter is not None]
        types: list[str] = []
        offset: int = 0
        if node.kind == "CXXOperatorCallExpr" and referenced.kind.name == "CXX_METHOD" and arguments:
            types.append("")
            offset = 1
        index: int
        for index in range(offset, len(arguments)):
            position: int = index - offset
            if position >= len(parameters):
                types.append("")
                continue
            types.append(written_type(parameters[position].type.spelling))
        return types

    def constructor(self, node: Node, cursor: Any) -> None:
        """A constructor: parameters, member and base initializers (each with its expression), body."""
        pending: Node | None = None
        name_file: str
        name_offset: int
        name_file, name_offset = self.point(self.library.cursor_location(cursor), False)
        child: Any
        for child in cursor.get_children():
            name: str = child.kind.name
            # An initializer's member or base comes after the constructor's name (a qualifier, Type::, before it).
            after_name: bool = self.point(self.library.cursor_location(child), False)[1] > name_offset
            if (name in ("MEMBER_REF", "TYPE_REF") and pending is None and after_name
                    and not self.body_seen(node)):
                pending = self.located("CXXCtorInitializer", child)
                referenced: Any = child.referenced
                pending.name = referenced.spelling if referenced is not None else child.spelling
                if referenced is not None:
                    pending.target = self.identity(referenced)
                    pending.target_kind = self.kind_of(referenced)
                    pending.qualified = self.qualified(referenced, pending.target)
                continue
            converted: Node | None = self.convert(child)
            if converted is None:
                continue
            if pending is not None and child.kind.is_expression():
                pending.children.append(converted)
                node.children.append(pending)
                pending = None
                continue
            if child.kind.is_expression():
                continue                # an expression of the constructor's type (a noexcept operand)
            if pending is not None:
                node.children.append(pending)
                pending = None
            node.children.append(converted)
        if pending is not None:
            node.children.append(pending)

    def body_seen(self, node: Node) -> bool:
        return any(child.kind == "CompoundStmt" for child in node.children)

    def lambda_body(self, node: Node, cursor: Any) -> None:
        """A lambda reads as its body: captures and parameters are left out."""
        child: Any
        for child in cursor.get_children():
            if child.kind.name == "COMPOUND_STMT":
                converted: Node | None = self.convert(child)
                if converted is not None:
                    node.children.append(converted)


def read_unit(unit: UnitCommand, keep: Callable[[str], bool]) -> Iterator[Node]:
    problem: str = load_problem()
    if problem:
        raise RuntimeError("libclang is not available: " + problem)
    library: Library = LOADED[0]
    cindex: Any = library.cindex
    index: Any = cindex.Index.create()
    source: str = absolute(unit.directory, unit.file)
    previous: str = os.getcwd()
    os.chdir(unit.directory)
    try:
        translation_unit: Any = index.parse(source, args=parse_arguments(unit))
    finally:
        os.chdir(previous)
    errors: list[str] = []
    diagnostic: Any
    for diagnostic in translation_unit.diagnostics:
        if diagnostic.severity >= cindex.Diagnostic.Error:
            errors.append(str(diagnostic))
    if errors:
        raise ReadError(f"{source}: {len(errors)} front-end errors; first: {errors[0]}")
    converter: Converter = Converter(library, unit, SourceText())
    top: Any
    for top in translation_unit.cursor.get_children():
        file: str
        offset: int
        file, offset = converter.point(library.cursor_location(top), False)
        if not file or not keep(absolute(unit.directory, file)):
            continue
        converted: Node | None = converter.convert(top)
        if converted is not None:
            yield converted
