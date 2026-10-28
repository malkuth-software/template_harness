"""Unit tests for clang_ast.py and its two backends (clang_ast_json.py, clang_ast_libclang.py).

The pure parts (the dump splitter, compile commands, statement headers,
function types, line numbers) always run. The model tests run once per
backend that is available: json needs clang++ on PATH, libclang the pinned
LLVM's bindings. With HARNESS_AST_READER=libclang a missing libclang fails
the run instead of skipping it (the CI job that installs the bindings sets
it). When both backends are available, the two models of one source must
agree apart from the documented differences.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

import harness_testing
import clang_ast
import clang_ast_json
import clang_ast_libclang

CLANG: str | None = shutil.which("clang++")
LIBCLANG_REQUIRED: bool = os.environ.get("HARNESS_AST_READER", "") == "libclang"

# A pretty-printed dump in clang's layout: four top-level declarations. The
# second is in a skipped header; the third names no file, so it is in the
# file the previous location left in effect (the header's range ended in
# a.cpp); the fourth's location is a macro expansion whose expansion is in
# the skipped header although its spelling is in a.cpp.
DUMP: str = """{
  "id": "0x1",
  "kind": "TranslationUnitDecl",
  "loc": {},
  "range": {
    "begin": {},
    "end": {}
  },
  "inner": [
    {
      "id": "0x2",
      "kind": "VarDecl",
      "loc": {
        "offset": 4,
        "file": "/w/p/src/a.cpp",
        "line": 1,
        "col": 5,
        "tokLen": 1
      },
      "range": {
        "begin": {
          "offset": 0,
          "col": 1,
          "tokLen": 3
        },
        "end": {
          "offset": 4,
          "col": 5,
          "tokLen": 1
        }
      },
      "name": "a"
    },
    {
      "id": "0x3",
      "kind": "VarDecl",
      "loc": {
        "offset": 4,
        "file": "/usr/include/b.h",
        "line": 1,
        "col": 5,
        "tokLen": 1,
        "includedFrom": {
          "file": "/w/p/src/a.cpp"
        }
      },
      "range": {
        "begin": {
          "offset": 0,
          "col": 1,
          "tokLen": 3,
          "includedFrom": {
            "file": "/w/p/src/a.cpp"
          }
        },
        "end": {
          "offset": 4,
          "file": "/w/p/src/a.cpp",
          "line": 2,
          "col": 5,
          "tokLen": 1
        }
      },
      "name": "b"
    },
    {
      "id": "0x4",
      "kind": "VarDecl",
      "loc": {
        "offset": 12,
        "line": 3,
        "col": 5,
        "tokLen": 1
      },
      "range": {
        "begin": {
          "offset": 8,
          "col": 1,
          "tokLen": 3
        },
        "end": {
          "offset": 12,
          "col": 5,
          "tokLen": 1
        }
      },
      "name": "c"
    },
    {
      "id": "0x5",
      "kind": "VarDecl",
      "loc": {
        "spellingLoc": {
          "offset": 20,
          "line": 4,
          "col": 5,
          "tokLen": 1
        },
        "expansionLoc": {
          "offset": 40,
          "file": "/usr/include/b.h",
          "line": 9,
          "col": 1,
          "tokLen": 6
        }
      },
      "range": {
        "begin": {
          "offset": 40,
          "col": 1,
          "tokLen": 6
        },
        "end": {
          "offset": 40,
          "col": 1,
          "tokLen": 6
        }
      },
      "name": "d"
    }
  ]
}
"""


def keep_project(file: str) -> bool:
    return file.startswith("/w/p/")


class SplitterTests(unittest.TestCase):
    def test_kept_declarations_and_the_file_carried_through_skipped_text(self) -> None:
        kept: list[clang_ast_json.Declaration] = list(
            clang_ast_json.declarations(iter(DUMP.splitlines(True)), keep_project))
        names: list[str] = []
        declaration: clang_ast_json.Declaration
        for declaration in kept:
            names.append(declaration.node["name"])
        # c names no file: the skipped declaration's range ended in a.cpp. d is in a macro expanded in b.h.
        self.assertEqual(names, ["a", "c"])
        self.assertEqual(kept[1].file_before, "/w/p/src/a.cpp")


class CommandTests(unittest.TestCase):
    def test_compile_arguments_drop_outputs_dependency_files_and_the_launcher(self) -> None:
        words: list[str] = clang_ast.compile_arguments("ccache clang++ -O2 -MD -MT x.o -MF x.d -o x.o -c x.cpp")
        self.assertEqual(words, ["clang++", "-O2", "x.cpp"])

    def test_a_unit_from_either_form_of_entry(self) -> None:
        listed: clang_ast.UnitCommand = clang_ast.unit_command(
            {"directory": "/b", "arguments": ["clang++", "-c", "a.cpp", "-o", "a.o"], "file": "a.cpp"})
        self.assertEqual(listed.command, ("clang++", "a.cpp"))
        written: clang_ast.UnitCommand = clang_ast.unit_command(
            {"directory": "/b", "command": "clang++ -DX=1 -c a.cpp", "file": "a.cpp"})
        self.assertEqual(written.command, ("clang++", "-DX=1", "a.cpp"))

    def test_windows_command_lines_split_as_the_c_runtime_splits_them(self) -> None:
        # Ninja on Windows writes a string define as -DNAME=\\"text\\" (gui_forms' version string).
        command: str = 'C:\\llvm\\bin\\clang++.exe -DVERSION=\\"0.2.0\\" "-IC:/a b/include" -c C:\\src\\a.cpp'
        self.assertEqual(clang_ast.split_command(command, True),
                         ["C:\\llvm\\bin\\clang++.exe", '-DVERSION="0.2.0"', "-IC:/a b/include", "-c",
                          "C:\\src\\a.cpp"])
        self.assertEqual(clang_ast.split_command('a "" b\\\\"c d" "e""f"', True), ["a", "", 'b\\c d', 'e"f'])
        self.assertEqual(clang_ast.split_command("clang++ -DVERSION=\\\"0.2.0\\\" -c a.cpp", False),
                         ["clang++", '-DVERSION="0.2.0"', "-c", "a.cpp"])

    def test_listed_arguments_keep_their_quotes(self) -> None:
        listed: clang_ast.UnitCommand = clang_ast.unit_command(
            {"directory": "/b", "arguments": ["clang++", '-DVERSION="0.2.0"', "-c", "a.cpp"], "file": "a.cpp"})
        self.assertEqual(listed.command, ("clang++", '-DVERSION="0.2.0"', "a.cpp"))

    def test_an_unknown_reader_is_refused(self) -> None:
        previous: str | None = os.environ.get("HARNESS_AST_READER")
        os.environ["HARNESS_AST_READER"] = "regex"
        try:
            with self.assertRaises(ValueError):
                clang_ast.reader_in_use()
        finally:
            if previous is None:
                del os.environ["HARNESS_AST_READER"]
            else:
                os.environ["HARNESS_AST_READER"] = previous


class TextTests(unittest.TestCase):
    def test_lines_from_byte_offsets_in_a_crlf_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / "a.cpp"
            path.write_bytes(b"int a;\r\nint b;\r\n")
            text: clang_ast.SourceText = clang_ast.SourceText()
            self.assertEqual(text.line(str(path), 0), 1)
            self.assertEqual(text.line(str(path), 8), 2)
            self.assertEqual(text.text(str(path), 8, 13), "int b")

    def test_statement_headers(self) -> None:
        source: bytes = b'if (int a = f(";)"); a > 0) { b(); }'
        header: clang_ast_libclang.Header = clang_ast_libclang.statement_header(source, 0)
        self.assertEqual(header.semicolons, (source.index(b"; a"),))
        self.assertEqual(header.close, source.index(b") {"))
        commented: bytes = b"for (/* ; ) */ int i = 1'000; i < n; ++i) x();"
        header = clang_ast_libclang.statement_header(commented, 0)
        self.assertEqual(len(header.semicolons), 2)
        self.assertEqual(header.close, commented.index(b") x"))

    def test_literal_tokens(self) -> None:
        data: bytes = b"x = 1.5e+3f; y = 'a'; z = \"q\\\"r\";"
        self.assertEqual(data[4:clang_ast_libclang.token_end(data, 4)], b"1.5e+3f")
        self.assertEqual(data[17:clang_ast_libclang.token_end(data, 17)], b"'a'")
        self.assertEqual(data[26:clang_ast_libclang.token_end(data, 26)], b'"q\\"r"')

    def test_function_types(self) -> None:
        self.assertEqual(clang_ast_json.parameter_types("bool (const X &, int &)"), ["const X &", "int &"])
        self.assertEqual(clang_ast_json.parameter_types("const T &() const &"), [])
        self.assertEqual(clang_ast_json.parameter_types("void (int) noexcept(true)"), ["int"])
        self.assertEqual(clang_ast_json.parameter_types("std::pair<int, int> (std::map<int, int> &)"),
                         ["std::map<int, int> &"])
        self.assertEqual(clang_ast_json.parameter_writes("CXXOperatorCallExpr", "X &(int &) const",
                                                         ["int &"], 2), [False, True])


SAMPLE: str = """#define REQUIRE(x) if (!(x)) return -1
namespace demo {
enum class Mode { plain, scaled };
enum Shade { light, dark };
struct Pair { int first; int second; };
struct Grid { int cells[2]; };
class Counter {
public:
    explicit Counter(int start) : count_(start), step_{1} {}
    int count() const { return count_; }
    void add(int by);
    Counter& operator+=(int by) { count_ += by; return *this; }
    friend bool operator==(const Counter& left, const Counter& right);
private:
    int count_;
    int step_;
};
void Counter::add(int by) { count_ += by * step_; }
bool operator==(const Counter& left, const Counter& right) { return left.count() == right.count(); }
void bump(int& value, const int& limit, int* pointer);

int run(Counter& counter, int limit, Mode mode) {
    int total = 0;
    int items[3] = {1, 2, 3};
    Pair pair = {.first = 1, .second = 2};
    Grid grid = {4, 5};
    for (int index = 0; index < limit; ++index) {
        if (int seen = items[index % 3]; seen > limit) return -1;
        if (bool more = index < 2) total += 1; else total -= 1;
        switch (mode) { case Mode::plain: total++; break; default: break; }
        total += index > 1 ? 1 : 0;
        bump(total, limit, &total);
        counter.add(1);
        counter += 2;
        REQUIRE(total < 100);
    }
    for (int value : items) {
        total += value;
    }
    while (bool going = total > 0) { total -= going ? 1 : 0; }
    do { --limit; } while (limit > 0);
    auto twice = [&total](int by) { total += 2 * by; };
    twice(3);
    return total + pair.first + grid.cells[1] + (counter == counter ? 1 : 0) + light;
}
}
"""


class ModelChecks:
    """Checks of one backend's model of SAMPLE; a subclass names the backend."""
    reader: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory()
        root: Path = Path(cls.directory.name).resolve()
        source: Path = root / "sample.cpp"
        source.write_text(SAMPLE, encoding="utf-8", newline="\n")
        unit: clang_ast.UnitCommand = clang_ast.UnitCommand(str(root), (CLANG or "clang++", "-std=c++20", "-c",
                                                                        "sample.cpp"), "sample.cpp")
        keeper: KeepFile = KeepFile(source.as_posix())
        cls.roots: list[clang_ast.Node] = list(clang_ast.read_unit(unit, keeper.keep, cls.reader))
        cls.source_path: str = source.as_posix()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def all_nodes(self) -> list[clang_ast.Node]:
        collector: Collector = Collector()
        root: clang_ast.Node
        for root in self.roots:
            clang_ast.walk(root, collector.visit)
        return collector.nodes

    def named(self, kind: str, name: str) -> clang_ast.Node:
        node: clang_ast.Node
        for node in self.all_nodes():
            if node.kind == kind and node.name == name:
                return node
        raise AssertionError(f"no {kind} named {name}")

    def function(self, name: str) -> clang_ast.Node:
        node: clang_ast.Node
        for node in self.all_nodes():
            if node.kind in clang_ast.FUNCTIONS and node.name == name and node.definition:
                return node
        raise AssertionError(f"no definition of {name}")

    def test_statement_roles(self) -> None:
        run: clang_ast.Node = self.function("run")
        loops: list[clang_ast.Node] = [node for node in self.all_nodes() if node.kind == "ForStmt"]
        loop: clang_ast.Node = loops[0]
        self.assertEqual(set(loop.roles), {"init", "condition", "increment", "body"})
        self.assertEqual(loop.roles["increment"].opcode, "++")
        statements: list[clang_ast.Node] = loop.roles["body"].children
        first: clang_ast.Node = statements[0]
        self.assertEqual(first.kind, "IfStmt")
        self.assertEqual(set(first.roles), {"init", "condition", "then"})
        second: clang_ast.Node = statements[1]
        self.assertEqual(set(second.roles), {"variable", "condition", "then", "else"})
        self.assertEqual(second.roles["variable"].kind, "VarDecl")
        switch: clang_ast.Node = statements[2]
        self.assertEqual(set(switch.roles), {"condition", "body"})
        ranged: clang_ast.Node = [node for node in self.all_nodes() if node.kind == "CXXForRangeStmt"][0]
        self.assertEqual(set(ranged.roles), {"variable", "range", "body"})
        self.assertEqual(ranged.roles["variable"].children, [])
        self.assertEqual(ranged.roles["range"].name, "items")
        waiting: clang_ast.Node = [node for node in self.all_nodes() if node.kind == "WhileStmt"][0]
        self.assertEqual(set(waiting.roles), {"variable", "condition", "body"})
        doing: clang_ast.Node = [node for node in self.all_nodes() if node.kind == "DoStmt"][0]
        self.assertEqual(doing.roles["condition"].opcode, ">")
        self.assertTrue(run.definition)

    def test_operators_and_literals(self) -> None:
        unary: list[clang_ast.Node] = [node for node in self.all_nodes() if node.kind == "UnaryOperator"]
        spelled: set[tuple[str, bool]] = {(node.opcode, node.postfix) for node in unary}
        self.assertTrue({("++", False), ("++", True), ("--", False), ("&", False), ("-", False),
                         ("!", False)} <= spelled)
        values: set[str] = {node.value for node in self.all_nodes() if node.kind == "IntegerLiteral"}
        self.assertTrue({"0", "1", "2", "3", "100"} <= values)

    def test_calls(self) -> None:
        bump: clang_ast.Node = self.named("CallExpr", "bump")
        self.assertEqual(bump.writes, [True, False, False])
        self.assertEqual(bump.parameters, ["int &", "const int &", "int *"])
        self.assertEqual(bump.roles["callee"].kind, "DeclRefExpr")
        self.assertEqual(len(bump.arguments), 3)
        add: clang_ast.Node = self.named("CXXMemberCallExpr", "add")
        self.assertFalse(add.constant)
        self.assertEqual(add.roles["object"].name, "counter")
        count: clang_ast.Node = self.named("CXXMemberCallExpr", "count")
        self.assertTrue(count.constant)
        compound: clang_ast.Node = self.named("CXXOperatorCallExpr", "operator+=")
        self.assertEqual(compound.opcode, "+=")
        self.assertEqual(compound.writes, [True, False])
        self.assertEqual(compound.parameters, ["", "int"])
        self.assertEqual(compound.arguments[0].name, "counter")
        equal: clang_ast.Node = self.named("CXXOperatorCallExpr", "operator==")
        self.assertEqual(equal.writes, [False, False])
        self.assertEqual(equal.parameters, ["const Counter &", "const Counter &"])
        self.assertEqual(equal.qualified, "demo::operator==")

    def test_members_and_declarations(self) -> None:
        member: clang_ast.Node = self.named("MemberExpr", "count_")
        self.assertTrue(member.arrow)
        self.assertEqual(member.roles["object"].kind, "CXXThisExpr")
        self.assertTrue(member.roles["object"].implicit)
        self.assertEqual(member.qualified, "demo::Counter::count_")
        field: clang_ast.Node = self.named("FieldDecl", "count_")
        self.assertEqual(field.access, "private")
        self.assertEqual(field.type, "int")
        first: clang_ast.Node = self.named("FieldDecl", "first")
        self.assertEqual(first.access, "public")
        counter: clang_ast.Node = self.named("CXXRecordDecl", "Counter")
        self.assertEqual(counter.tag, "class")
        self.assertTrue(counter.definition)
        out_of_line: clang_ast.Node = self.function("add")
        self.assertEqual(out_of_line.qualified, "demo::Counter::add")
        self.assertEqual(out_of_line.access, "public")
        self.assertEqual(self.function("operator==").qualified, "demo::operator==")
        self.assertEqual(self.named("EnumConstantDecl", "scaled").qualified, "demo::Mode::scaled")
        self.assertEqual(self.named("EnumConstantDecl", "dark").qualified, "demo::dark")
        constructor: clang_ast.Node = self.function("Counter")
        initializers: list[str] = [child.name for child in constructor.children
                                   if child.kind == "CXXCtorInitializer"]
        self.assertEqual(initializers, ["count_", "step_"])

    def test_initializers_as_written(self) -> None:
        items: clang_ast.Node = self.named("VarDecl", "items")
        self.assertEqual(len(items.children), 1)
        self.assertEqual(items.roles["init"].kind, "InitListExpr")
        grid: clang_ast.Node = self.named("VarDecl", "grid")
        self.assertEqual([child.value for child in grid.roles["init"].children], ["4", "5"])
        pair: clang_ast.Node = self.named("VarDecl", "pair")
        self.assertEqual([child.value for child in pair.roles["init"].children], ["1", "2"])

    def test_macro_nodes(self) -> None:
        conditions: list[clang_ast.Node] = [node for node in self.all_nodes() if node.kind == "IfStmt"
                                            and node.in_macro]
        self.assertEqual(len(conditions), 1)
        self.assertEqual(conditions[0].roles["condition"].opcode, "!")
        self.assertEqual(conditions[0].roles["then"].kind, "ReturnStmt")

    def test_lambda_reads_as_its_body(self) -> None:
        lambdas: list[clang_ast.Node] = [node for node in self.all_nodes() if node.kind == "LambdaExpr"]
        self.assertEqual([child.kind for child in lambdas[0].children], ["CompoundStmt"])


class KeepFile:
    def __init__(self, path: str) -> None:
        self.path: str = path.lower()

    def keep(self, file: str) -> bool:
        return file.lower() == self.path


class Collector:
    def __init__(self) -> None:
        self.nodes: list[clang_ast.Node] = []

    def visit(self, node: clang_ast.Node, parents: list[clang_ast.Node]) -> None:
        self.nodes.append(node)


@unittest.skipIf(CLANG is None, "needs clang++ on PATH")
class JsonModelTests(ModelChecks, unittest.TestCase):
    reader = "json"


LIBCLANG_PROBLEM: str = clang_ast_libclang.load_problem() if CLANG is not None or LIBCLANG_REQUIRED else "no clang++"


@unittest.skipIf(bool(LIBCLANG_PROBLEM) and not LIBCLANG_REQUIRED, "libclang: " + LIBCLANG_PROBLEM)
class LibclangModelTests(ModelChecks, unittest.TestCase):
    reader = "libclang"


class UnderDirectory:
    def __init__(self, path: str) -> None:
        self.path: str = path.lower().rstrip("/") + "/"

    def keep(self, file: str) -> bool:
        return file.lower().startswith(self.path)


@unittest.skipIf(bool(LIBCLANG_PROBLEM) and not LIBCLANG_REQUIRED, "libclang: " + LIBCLANG_PROBLEM)
class LibclangUnitsTests(unittest.TestCase):
    def test_each_unit_names_its_own_files(self) -> None:
        # A CXFile handle belongs to its translation unit, and a freed unit's addresses are reused by the next
        # one: file names cached across units once named one unit's nodes after another unit's file. Reading
        # two units in turn, many times, catches such a cache.
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory).resolve()
            names: list[str] = ["first.cpp", "second.cpp"]
            name: str
            for name in names:
                (root / name).write_text(f"int {name[:-4]}_value(int x) {{ return x + 1; }}\n", encoding="utf-8")
            keeper: UnderDirectory = UnderDirectory(root.as_posix())
            round_number: int
            for round_number in range(8):
                name = names[round_number % 2]
                unit: clang_ast.UnitCommand = clang_ast.UnitCommand(str(root), (CLANG or "clang++", "-std=c++20",
                                                                                "-c", name), name)
                collector: Collector = Collector()
                top: clang_ast.Node
                for top in clang_ast.read_unit(unit, keeper.keep, "libclang"):
                    clang_ast.walk(top, collector.visit)
                files: set[str] = {Path(node.file).name for node in collector.nodes if node.file}
                self.assertEqual(files, {name}, f"round {round_number}")


def described(node: clang_ast.Node, depth: int, role: str, lines: list[str]) -> None:
    """One line per node with every field both backends must agree on."""
    # A reference's qualified target is compared only for declarations: the json backend knows a target's
    # qualified name only when it read the declaration (documented).
    qualified: str = node.qualified if node.identity else ""
    fields: list[str] = [node.kind, node.name, qualified, node.target_kind, node.opcode, node.value,
                         node.type, node.access, node.tag]
    flags: list[str] = []
    name: str
    for name in ("postfix", "arrow", "constant", "implicit", "in_macro", "definition"):
        if getattr(node, name):
            flags.append(name)
    # Documented differences: a macro node's end, and where a constructor initializer is.
    located: str = "" if node.kind == "CXXCtorInitializer" else str(node.begin)
    if not node.in_macro and node.kind != "CXXCtorInitializer":
        located += "," + str(node.end)
    writes: str = "".join("w" if write else "-" for write in node.writes)
    parameters: str = ";".join(node.parameters)
    lines.append("  " * depth + role + " " + "|".join(fields) + " " + ",".join(flags) + " " + writes + " " + parameters
                 + " " + located)
    roles: dict[int, str] = {id(child): key for key, child in node.roles.items()}
    child: clang_ast.Node
    for child in node.children:
        described(child, depth + 1, roles.get(id(child), ""), lines)


@unittest.skipIf(CLANG is None or bool(LIBCLANG_PROBLEM), "needs both backends")
class ParityTests(unittest.TestCase):
    def test_both_backends_read_the_sample_alike(self) -> None:
        texts: list[str] = []
        reader: str
        for reader in ("json", "libclang"):
            checks: type = type("Read" + reader, (ModelChecks,), {"reader": reader})
            checks.setUpClass()
            try:
                lines: list[str] = []
                root: clang_ast.Node
                for root in checks.roots:
                    described(root, 0, "", lines)
                texts.append("\n".join(lines))
            finally:
                checks.tearDownClass()
        self.maxDiff = None
        self.assertEqual(texts[0], texts[1])


if __name__ == "__main__":
    unittest.main()
