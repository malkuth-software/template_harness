"""The shared C++ AST reader of Phase E: one tree model, two ways to read it.

E2 (loop branches), E3 (const and restrict candidates) and the C3 codemod read
first-party declarations through this module. A caller names a compile
database entry and a `keep` test for file names; the reader parses the unit
with the pinned clang and returns each kept top-level declaration as a tree of
`Node`s. Two backends produce the same model:

  libclang   the clang.cindex bindings and libclang that ship with the host's
             pinned LLVM 22 (Windows CLANG64; Homebrew llvm@22 on macOS; on
             Linux apt.llvm.org's libclang1-22 with python3-clang-22). The
             default where it loads: it parses in process and walks only the
             kept declarations.
  json       the pinned clang's `-Xclang -ast-dump=json`, streamed: the
             fallback where the bindings are missing. One unit that includes
             the standard library dumps hundreds of megabytes, so the dump is
             split at its top-level declarations and only kept ones are parsed.

HARNESS_AST_READER=libclang or json chooses one; unset, libclang is used when
it loads and json otherwise (`reader_in_use` says which and why).

The model (`Node`) keeps what both backends can say the same way:

- `kind` is clang's AST class name (IfStmt, MemberExpr, CXXMethodDecl, ...).
  libclang's cursor kinds are mapped onto these names; a call is told apart
  as CallExpr, CXXMemberCallExpr, CXXOperatorCallExpr or CXXConstructExpr.
- Implicit nodes are left out: implicit casts, temporaries, cleanups,
  constant wrappers, implicit declarations, attributes, comments, type and
  template references and template parameters. A member reached through an
  implicit `this` keeps a CXXThisExpr object marked `implicit`. A template
  reads as its pattern (the declaration as written), never its
  instantiations.
- `children` are the remaining nodes. A call's children are its callee
  (absent for a constructor) followed by its arguments, so an overloaded
  operator's callee comes first whatever the source order.
- `roles` names a statement's or expression's parts (see ROLES), so no
  caller depends on either backend's child order; a condition variable is
  the VarDecl itself, not a DeclStmt around it.
- `file`, `begin` and `end` locate the node: an absolute path with forward
  slashes and byte offsets (a CRLF checkout keeps its two-byte line ends, so
  slice source as bytes). For a node a macro expansion produced, the offsets
  are the expansion's and `in_macro` is set.
- `identity` (a declaration) and `target` (what a reference or call names)
  are unit-local keys: equal within one unit's reading, meaningless across
  units. `qualified` is a declaration's or target's name with its enclosing
  namespaces and records, stable across units.

Where the two backends can differ, and why:

- `constant` on a member call: libclang asks the called method; the dump has
  no such flag, so the json backend reads a NoOp cast to const on the object
  (clang's own spelling of a const call).
- `writes` on call arguments: libclang reads each parameter's type (a
  non-const lvalue reference); the json backend reads an argument passed as
  an lvalue with no conversion, which is how clang spells a non-const
  reference binding.
- Dependent code in templates (unresolved calls and members) is described
  less precisely by libclang; findings there are approximate in both.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex
from typing import Callable, Iterator

# Roles a node may name, by kind. A role is absent when the source has no such part.
ROLES: dict[str, tuple[str, ...]] = {
    "IfStmt": ("init", "variable", "condition", "then", "else"),
    "SwitchStmt": ("init", "variable", "condition", "body"),
    "WhileStmt": ("variable", "condition", "body"),
    "DoStmt": ("body", "condition"),
    "ForStmt": ("init", "variable", "condition", "increment", "body"),
    "CXXForRangeStmt": ("init", "variable", "range", "body"),
    "ConditionalOperator": ("condition", "true", "false"),
    "BinaryOperator": ("left", "right"),
    "CompoundAssignOperator": ("left", "right"),
    "UnaryOperator": ("operand",),
    "ArraySubscriptExpr": ("base", "index"),
    "MemberExpr": ("object",),
    "CallExpr": ("callee",),
    "CXXMemberCallExpr": ("callee", "object"),
    "CXXOperatorCallExpr": ("callee",),
    "ReturnStmt": ("value",),
    "VarDecl": ("init",),
    "FieldDecl": ("init",),
}
CALLS: frozenset[str] = frozenset({"CallExpr", "CXXMemberCallExpr", "CXXOperatorCallExpr", "CXXConstructExpr"})
FUNCTIONS: frozenset[str] = frozenset({"FunctionDecl", "CXXMethodDecl", "CXXConstructorDecl", "CXXDestructorDecl",
                                       "CXXConversionDecl"})
RECORDS: frozenset[str] = frozenset({"CXXRecordDecl"})
READERS: tuple[str, ...] = ("libclang", "json")
# A const member function's type as clang writes it: "R (P) const", "R (P) const &".
CONST_FUNCTION_TYPE: re.Pattern[str] = re.compile(r"\)\s*const(\s|&|$)")
# Switches a front-end-only run drops from a compile command.
DROPPED_WITH_VALUE: tuple[str, ...] = ("-o", "-MT", "-MF", "-MQ")
DROPPED: tuple[str, ...] = ("-MD", "-MMD", "-c")
LAUNCHERS: tuple[str, ...] = ("ccache", "ccache.exe")


@dataclass(eq=False)
class Node:
    """One node of a kept declaration. Compared by identity (`is`), never by value."""
    kind: str
    file: str               # absolute, forward slashes; "" when the reader gives none
    begin: int              # byte offset of the first character, or -1
    end: int                # byte offset just past the last token, or -1
    in_macro: bool
    name: str = ""          # a declaration's name, a member's name, or the name a reference names
    identity: str = ""      # a declaration's unit-local key
    qualified: str = ""     # a declaration's (or a reference's target's) qualified name
    target: str = ""        # a reference's or call's declaration, as that declaration's identity
    target_kind: str = ""   # that declaration's kind
    opcode: str = ""        # an operator's spelling: "<", "+=", "++", "!", "[]" (an overloaded call's operator)
    postfix: bool = False   # a postfix ++ or --
    arrow: bool = False     # a member reached with ->
    value: str = ""         # a literal's spelling: "true", "0", "nullptr"
    type: str = ""          # a declaration's type as written (functions: the function type)
    constant: bool = False  # a const member function, or a call of one
    implicit: bool = False  # an implicit `this`
    access: str = ""        # public, protected or private, for a member declaration
    tag: str = ""           # struct, class or union, for a record
    definition: bool = False  # a record's complete definition or a function's body is here
    children: list[Node] = field(default_factory=list)
    roles: dict[str, Node] = field(default_factory=dict)
    arguments: list[Node] = field(default_factory=list)     # a call's arguments, in order
    writes: list[bool] = field(default_factory=list)        # per argument: it may be written through a reference


class ReadError(RuntimeError):
    """The front end reported an error, so the unit's model would be incomplete; nothing is yielded."""


@dataclass(frozen=True)
class UnitCommand:
    """One compile-database entry: where it runs, the command, and the source it compiles."""
    directory: str
    command: tuple[str, ...]    # the compiler and its arguments, launcher removed
    file: str


LAMBDA_TYPE: re.Pattern[str] = re.compile(r"\(lambda at [^)]*\)")


def written_type(text: str) -> str:
    """A type as the model keeps it: a lambda's closure type, which clang names by its file and line
    (relative or absolute as the command named the file), is "(lambda)"."""
    result: str = LAMBDA_TYPE.sub("(lambda)", text)
    return result


def compile_arguments(command: str) -> list[str]:
    """A compile command split into words, without its launcher, its outputs and its dependency files."""
    arguments: list[str] = shlex.split(command, posix=(os.name != "nt"))
    result: list[str] = []
    skip: bool = False
    argument: str
    for argument in arguments:
        argument = argument.strip('"')
        if skip:
            skip = False
            continue
        if argument in DROPPED_WITH_VALUE:
            skip = True
            continue
        if argument in DROPPED or Path(argument).name.lower() in LAUNCHERS:
            continue
        result.append(argument)
    return result


def unit_command(entry: dict) -> UnitCommand:
    """A compile-database entry (directory, command or arguments, file) as a UnitCommand."""
    words: list[str]
    if "arguments" in entry:
        words = compile_arguments(shlex.join(entry["arguments"]))
    else:
        words = compile_arguments(entry["command"])
    result: UnitCommand = UnitCommand(entry["directory"], tuple(words), entry["file"])
    return result


def absolute(directory: str, file: str) -> str:
    """A file name as an absolute path with forward slashes (clang writes it as the command named it)."""
    path: Path = Path(file)
    if not path.is_absolute():
        path = Path(directory) / path
    result: str = Path(os.path.normpath(str(path))).as_posix()
    return result


def reader_in_use() -> tuple[str, str]:
    """The backend this process reads with, and why: (name, reason)."""
    chosen: str = os.environ.get("HARNESS_AST_READER", "")
    if chosen and chosen not in READERS:
        raise ValueError(f"HARNESS_AST_READER={chosen!r}: expected one of {', '.join(READERS)}")
    if chosen == "json":
        return "json", "HARNESS_AST_READER=json"
    import clang_ast_libclang
    problem: str = clang_ast_libclang.load_problem()
    if not problem:
        return "libclang", "the pinned LLVM's clang.cindex loaded"
    if chosen == "libclang":
        raise RuntimeError("HARNESS_AST_READER=libclang, but libclang did not load: " + problem)
    return "json", "libclang did not load (" + problem + ")"


def read_unit(unit: UnitCommand, keep: Callable[[str], bool], reader: str = "") -> Iterator[Node]:
    """The unit's top-level declarations whose own location is in a file `keep` accepts, in source order.

    `keep` receives absolute paths with forward slashes. `reader` names a
    backend; empty means `reader_in_use()`.
    """
    if not reader:
        reader = reader_in_use()[0]
    if reader == "libclang":
        import clang_ast_libclang
        yield from clang_ast_libclang.read_unit(unit, keep)
    elif reader == "json":
        import clang_ast_json
        yield from clang_ast_json.read_unit(unit, keep)
    else:
        raise ValueError(f"unknown AST reader {reader!r}")


def walk(root: Node, visit: Callable[[Node, list[Node]], None]) -> None:
    """Calls visit(node, parents) for every node before its children; parents run from the root down."""
    parents: list[Node] = []
    walk_node(root, parents, visit)


def walk_node(node: Node, parents: list[Node], visit: Callable[[Node, list[Node]], None]) -> None:
    visit(node, parents)
    parents.append(node)
    child: Node
    for child in node.children:
        walk_node(child, parents, visit)
    parents.pop()


def stripped(node: Node) -> Node:
    """The expression inside any parentheses."""
    while node.kind == "ParenExpr" and node.children:
        node = node.children[0]
    return node


def assign_call_parts(node: Node, callee: Node | None, arguments: list[Node]) -> None:
    """A call's children (callee first), roles and arguments, as both backends lay them out."""
    node.children = ([callee] if callee is not None else []) + arguments
    node.arguments = arguments
    if callee is None:
        return
    if node.kind == "CallExpr" and callee.kind == "MemberExpr":
        # A dependent member call in a template: the dump calls it CallExpr, libclang a member call.
        node.kind = "CXXMemberCallExpr"
    node.roles["callee"] = callee
    if node.kind == "CXXMemberCallExpr" and callee.kind == "MemberExpr" and "object" in callee.roles:
        node.roles["object"] = callee.roles["object"]
    if not node.name:
        node.name = callee.name
    if not node.target:
        node.target = callee.target
        node.target_kind = callee.target_kind
        node.qualified = callee.qualified
    if node.kind == "CXXOperatorCallExpr" and callee.name.startswith("operator"):
        node.opcode = callee.name[len("operator"):].strip()


def assign_positional_roles(node: Node) -> None:
    """Roles of the kinds whose children both backends give in one fixed order."""
    names: tuple[str, ...] = ()
    if node.kind in ("BinaryOperator", "CompoundAssignOperator", "ArraySubscriptExpr", "ConditionalOperator",
                     "UnaryOperator", "ReturnStmt", "DoStmt"):
        names = ROLES[node.kind]
    elif node.kind in ("VarDecl", "FieldDecl") and node.children:
        # The initializer is the last child; nothing else in the model hangs off a variable.
        node.roles["init"] = node.children[-1]
        return
    elif node.kind == "MemberExpr" and node.children:
        node.roles["object"] = node.children[0]
        return
    if len(node.children) != len(names):
        return
    index: int
    for index in range(len(names)):
        node.roles[names[index]] = node.children[index]


class SourceText:
    """Source files read as bytes once, for slicing by node offsets and finding lines."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.line_starts: dict[str, list[int]] = {}

    def bytes_of(self, path: str) -> bytes:
        if path not in self.data:
            self.data[path] = Path(path).read_bytes()
        return self.data[path]

    def text(self, path: str, begin: int, end: int) -> str:
        if not path or begin < 0 or end < begin:
            return ""
        result: str = self.bytes_of(path)[begin:end].decode("utf-8", "replace")
        return result

    def node_text(self, node: Node) -> str:
        result: str = self.text(node.file, node.begin, node.end)
        return result

    def line(self, path: str, offset: int) -> int:
        """The 1-based line holding a byte offset."""
        if path not in self.line_starts:
            starts: list[int] = [0]
            data: bytes = self.bytes_of(path)
            index: int = data.find(b"\n")
            while index >= 0:
                starts.append(index + 1)
                index = data.find(b"\n", index + 1)
            self.line_starts[path] = starts
        lines: list[int] = self.line_starts[path]
        low: int = 0
        high: int = len(lines)
        while low + 1 < high:
            middle: int = (low + high) // 2
            if lines[middle] <= offset:
                low = middle
            else:
                high = middle
        return low + 1
