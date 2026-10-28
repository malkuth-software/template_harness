"""Unit tests for check_copies.py: candidates once per AST reader backend, owning types and decisions.

As in test_loop_branches.py, the sample tests run once per backend that is
available; with HARNESS_AST_READER=libclang a missing libclang fails the run.
The sample declares its own small string, vector and list, without templates (the json reader
knows a member call's parameters only from a declaration it read), and needs no standard headers.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

import harness_testing
import check_copies as copies
import check_loop_branches as branches
import clang_ast_libclang

CLANG: str | None = shutil.which("clang++")
LIBCLANG_REQUIRED: bool = os.environ.get("HARNESS_AST_READER", "") == "libclang"
LIBCLANG_PROBLEM: str = clang_ast_libclang.load_problem() if CLANG is not None or LIBCLANG_REQUIRED else "no clang++"

SAMPLE: str = """
struct Item;
namespace std {
template <class T> T&& move(T& value) { return static_cast<T&&>(value); }
struct string {
    char* text;
    string();
    string(const char* from);
    string(const string& other);
    string(string&& other);
    string& operator=(const string& other);
    string& operator=(string&& other);
    string& operator+=(const string& other);
    unsigned long size() const;
};
struct vector {
    int* items;
    vector();
    vector(const vector& other);
    vector(vector&& other);
    vector& operator=(const vector& other);
    vector& operator=(vector&& other);
    void push_back(const int& value);
    void reserve(unsigned long count);
    unsigned long size() const;
};
struct list {
    Item* items;
    unsigned long size() const;
    const Item& operator[](unsigned long index) const;
    const Item* begin() const;
    const Item* end() const;
};
}
struct Item {
    std::string name;
    int id;
};
struct Point {
    int x;
    int y;
};
std::vector make_numbers();
void show(const std::string& text);
void take(std::string text);

int by_value(std::string label, std::string kept, Point point) {
    show(label);
    take(std::move(kept));
    return point.x;
}

int copies(const std::list& items, std::vector& numbers, const Item& first) {
    Item snapshot = first;
    show(snapshot.name);
    Item changed = first;
    changed.id = 2;
    numbers = make_numbers();
    std::vector grown;
    std::vector ready;
    ready.reserve(items.size());
    std::string joined;
    std::vector latest;
    std::vector picked;
    int total = 0;
    for (unsigned long index = 0; index < items.size(); ++index) {
        Item local = items[index];
        total += local.id;
        std::string fresh = "x";
        joined += fresh;
        grown.push_back(local.id);
        ready.push_back(local.id);
        if (local.id > 0) picked.push_back(local.id);
        latest = make_numbers();
        Point corner = {1, 2};
        total += corner.x;
    }
    for (Item item : items) total += item.id;
    return total + changed.id;
}
"""

EXPECTED: set[tuple[str, str]] = {
    ("by_value_read", "label"),
    ("copy_then_read", "snapshot"),
    ("returned_into_existing", "latest"),
    ("copy_then_read", "local"),
    ("allocation_in_loop", "fresh"),
    ("growth_without_reserve", "joined"),
    ("growth_without_reserve", "grown"),
    ("copy_in_loop", "item"),
}


class SampleChecks:
    reader: str = ""

    def test_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory).resolve()
            (root / "src").mkdir()
            source: Path = root / "src" / "sample.cpp"
            source.write_text(SAMPLE, encoding="utf-8")
            entry: dict = {"directory": str(root), "file": "src/sample.cpp",
                           "command": f"{CLANG or 'clang++'} -std=c++20 -c src/sample.cpp"}
            job: branches.UnitJob = branches.UnitJob(entry, root.as_posix(), ("src",), self.reader)
            branches.DONE.clear()
            result: copies.UnitResult = copies.analyse_unit(job)
        records: set[str] = copies.owning_records(result.record_fields)
        self.assertIn("Item", records)
        self.assertNotIn("Point", records)
        found: set[tuple[str, str]] = set()
        in_loop: dict[str, bool] = {}
        record: dict
        for record in result.findings:
            if copies.relevant(record, records):
                found.add((record["kind"], record["subject"]))
                in_loop[record["subject"]] = record["in_loop"]
        self.assertEqual(found, EXPECTED)
        self.assertTrue(in_loop["local"])
        self.assertFalse(in_loop["snapshot"])


@unittest.skipIf(CLANG is None, "needs clang++ on PATH")
class JsonSampleTests(SampleChecks, unittest.TestCase):
    reader = "json"


@unittest.skipIf(bool(LIBCLANG_PROBLEM) and not LIBCLANG_REQUIRED, "libclang: " + LIBCLANG_PROBLEM)
class LibclangSampleTests(SampleChecks, unittest.TestCase):
    reader = "libclang"


class TypeTests(unittest.TestCase):
    def test_owning_types(self) -> None:
        records: set[str] = copies.owning_records({"Item": ["std::string", "int"], "Box": ["Item"],
                                                   "Plain": ["int", "float"], "Link": ["Item *"]})
        self.assertEqual(records, {"Item", "Box"})
        self.assertTrue(copies.owns("std::vector<int>", set()))
        self.assertTrue(copies.owns("std::optional<Item>", records))
        self.assertFalse(copies.owns("const std::string &", set()))
        self.assertFalse(copies.owns("std::string_view", set()))
        self.assertFalse(copies.owns("std::shared_ptr<Item>", records))
        self.assertFalse(copies.owns("std::span<const Item>", records))
        self.assertFalse(copies.owns("const Item *const", records))
        self.assertFalse(copies.owns("std::vector<int>::const_iterator", set()))
        self.assertTrue(copies.owns("std::pair<int, std::string>", set()))
        self.assertFalse(copies.owns("Delegate<const Item &>", records))
        self.assertEqual(copies.base_name("const std::vector<int>"), "vector")


class DecisionTests(unittest.TestCase):
    def test_decisions_match_by_path_function_kind_and_subject(self) -> None:
        decisions: list[copies.Decision] = [copies.Decision("src/a.cpp", "f", "by_value_read", "label", "apply", "")]
        record: dict = {"path": "src/a.cpp", "function": "f", "kind": "by_value_read", "subject": "label"}
        self.assertIsNotNone(copies.decided(record, decisions))
        record["kind"] = "copy_then_read"
        self.assertIsNone(copies.decided(record, decisions))


if __name__ == "__main__":
    unittest.main()
