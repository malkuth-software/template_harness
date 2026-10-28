"""Unit tests for clang_ast.py (the streaming reader) and check_loop_branches.py (the classes)."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest

import harness_testing
import check_loop_branches as branches
import clang_ast

CLANG: str | None = shutil.which("clang++")

# A pretty-printed dump in clang's layout: three top-level declarations, the
# second in a skipped file, the third in the kept file again without naming
# it, because the previous location written was already in that file.
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
    }
  ]
}
"""


def keep_project(file: str) -> bool:
    return file.startswith("/w/p/")


class ReaderTests(unittest.TestCase):
    def test_kept_declarations_and_the_file_carried_through_skipped_text(self) -> None:
        kept: list[clang_ast.Declaration] = list(clang_ast.declarations(iter(DUMP.splitlines(True)), keep_project))
        names: list[str] = []
        declaration: clang_ast.Declaration
        for declaration in kept:
            names.append(declaration.node["name"])
        self.assertEqual(names, ["a", "c"])
        # The skipped declaration's range ended in a.cpp, so c (naming no file) is in a.cpp.
        self.assertEqual(kept[1].file_before, "/w/p/src/a.cpp")

    def test_an_included_from_file_does_not_change_the_file(self) -> None:
        visited: list[tuple[str, int]] = []

        walker: clang_ast.Walker = clang_ast.Walker("", RecordSites(visited).visit)
        node: dict = json.loads(DUMP)["inner"][1]
        walker.walk(node)
        self.assertEqual(visited, [("/usr/include/b.h", 0)])

    def test_front_end_command_drops_outputs_and_the_launcher(self) -> None:
        command: list[str] = clang_ast.front_end_command("ccache clang++ -O2 -MD -MT x.o -MF x.d -o x.o -c x.cpp", [])
        self.assertEqual(command[:3], ["clang++", "-O2", "x.cpp"])
        self.assertIn("-ast-dump=json", command)

    def test_lines_from_byte_offsets_in_a_crlf_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path: Path = Path(directory) / "a.cpp"
            path.write_bytes(b"int a;\r\nint b;\r\n")
            text: clang_ast.SourceText = clang_ast.SourceText()
            self.assertEqual(text.line(str(path), 0), 1)
            self.assertEqual(text.line(str(path), 8), 2)
            self.assertEqual(text.text(str(path), 8, 13), "int b")


class RecordSites:
    def __init__(self, visited: list[tuple[str, int]]) -> None:
        self.visited: list[tuple[str, int]] = visited

    def visit(self, node: dict, parents: list[dict], site: clang_ast.Site) -> None:
        self.visited.append((site.file, site.begin))


SAMPLE: str = """
struct Buffer {
    int* values;
    int count;
    int size() const { return count; }
};
bool accept(int value);

int sum(Buffer& buffer, bool scaled, int limit) {
    int total = 0;
    for (int index = 0; index < buffer.size(); ++index) {
        if (scaled) total += 2;
        if (index == 0) continue;
        if (buffer.values[index] > limit) return -1;
        if (buffer.values[index] == 7) break;
        total += buffer.values[index] > 3 ? 1 : 0;
        bool ok = total > 2 && buffer.values[index] < 9;
        switch (buffer.values[index]) { case 1: total += 1; break; default: break; }
        if (accept(index)) total += 1;
        if (ok) total += 1;
    }
    int at = 0;
    while (at < limit) {
        if (at + 1 == limit) total += 1;
        ++at;
    }
    int items[3] = {1, 2, 3};
    for (int value : items) {
        if (value == limit) total += value;
    }
    return total;
}
"""


@unittest.skipIf(CLANG is None, "needs clang++ on PATH")
class ClassTests(unittest.TestCase):
    def test_each_class(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory).resolve()
            (root / "src").mkdir()
            source: Path = root / "src" / "sample.cpp"
            source.write_text(SAMPLE, encoding="utf-8")
            command: list[str] = clang_ast.front_end_command(f"{CLANG} -std=c++20 -c src/sample.cpp", [])
            job: branches.UnitJob = branches.UnitJob(str(root), tuple(command), root.as_posix(), ("src",))
            branches.DONE.clear()
            records: list[dict] = branches.analyse_unit(job)
        found: dict[str, str] = {}
        record: dict
        for record in records:
            found[record["condition"]] = record["cls"]
            self.assertEqual(record["path"], "src/sample.cpp")
            self.assertEqual(record["function"], "sum")
        self.assertEqual(found, {
            "scaled": "invariant",
            "index == 0": "edge",
            "buffer.values[index] > limit": "validation",
            "buffer.values[index] == 7": "control",
            "buffer.values[index] > 3": "data",
            "total > 2 && buffer.values[index] < 9": "data",
            "buffer.values[index]": "data",
            "accept(index)": "data",
            "ok": "data",
            "at + 1 == limit": "edge",
            "value == limit": "data",
        })

    def test_decisions_match_by_path_function_and_condition(self) -> None:
        decisions: list[branches.Decision] = [branches.Decision("src/a.cpp", "f", "x > 0", "keep", "the point")]
        record: dict = {"path": "src/a.cpp", "function": "f", "condition": "x  >  0"}
        self.assertIsNotNone(branches.decided(record, decisions))
        record["condition"] = "x > 1"
        self.assertIsNone(branches.decided(record, decisions))


if __name__ == "__main__":
    unittest.main()
