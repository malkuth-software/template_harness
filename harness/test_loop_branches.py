"""Unit tests for check_loop_branches.py: the classes, once per AST reader backend, and decisions.

The class tests run once per backend that is available (json needs clang++
on PATH, libclang the pinned LLVM's bindings); with
HARNESS_AST_READER=libclang a missing libclang fails the run instead of
skipping it, as in test_clang_ast.py.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

import harness_testing
import check_loop_branches as branches
import clang_ast_libclang

CLANG: str | None = shutil.which("clang++")
LIBCLANG_REQUIRED: bool = os.environ.get("HARNESS_AST_READER", "") == "libclang"
LIBCLANG_PROBLEM: str = clang_ast_libclang.load_problem() if CLANG is not None or LIBCLANG_REQUIRED else "no clang++"

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
        if (index % 2 == 1) total += 1;
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


EXPECTED: dict[str, str] = {
    "scaled": "invariant",
    "index == 0": "edge",
    "index % 2 == 1": "edge",
    "buffer.values[index] > limit": "validation",
    "buffer.values[index] == 7": "control",
    "buffer.values[index] > 3": "data",
    "total > 2 && buffer.values[index] < 9": "data",
    "buffer.values[index]": "data",
    "accept(index)": "data",
    "ok": "data",
    "at + 1 == limit": "edge",
    "value == limit": "data",
}


class ClassChecks:
    """The classes of SAMPLE's branches through one backend (`reader`)."""
    reader: str = ""

    def test_each_class(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory).resolve()
            (root / "src").mkdir()
            source: Path = root / "src" / "sample.cpp"
            source.write_text(SAMPLE, encoding="utf-8")
            entry: dict = {"directory": str(root), "file": "src/sample.cpp",
                           "command": f"{CLANG or 'clang++'} -std=c++20 -c src/sample.cpp"}
            job: branches.UnitJob = branches.UnitJob(entry, root.as_posix(), ("src",), self.reader)
            branches.DONE.clear()
            records: list[dict] = branches.analyse_unit(job)
        found: dict[str, str] = {}
        reasons: dict[str, str] = {}
        record: dict
        for record in records:
            found[record["condition"]] = record["cls"]
            reasons[record["condition"]] = record["reason"]
            self.assertEqual(record["path"], "src/sample.cpp")
            self.assertEqual(record["function"], "sum")
        self.assertEqual(found, EXPECTED)
        self.assertIn("stride", reasons["index % 2 == 1"])
        self.assertIn("fixed bounds", reasons["index == 0"])


@unittest.skipIf(CLANG is None, "needs clang++ on PATH")
class JsonClassTests(ClassChecks, unittest.TestCase):
    reader = "json"


@unittest.skipIf(bool(LIBCLANG_PROBLEM) and not LIBCLANG_REQUIRED, "libclang: " + LIBCLANG_PROBLEM)
class LibclangClassTests(ClassChecks, unittest.TestCase):
    reader = "libclang"


class DecisionTests(unittest.TestCase):
    def test_decisions_match_by_path_function_and_condition(self) -> None:
        decisions: list[branches.Decision] = [branches.Decision("src/a.cpp", "f", "x > 0", "keep", "the point")]
        record: dict = {"path": "src/a.cpp", "function": "f", "condition": "x  >  0"}
        self.assertIsNotNone(branches.decided(record, decisions))
        record["condition"] = "x > 1"
        self.assertIsNone(branches.decided(record, decisions))


if __name__ == "__main__":
    unittest.main()
