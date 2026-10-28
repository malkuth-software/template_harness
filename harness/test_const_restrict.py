"""Unit tests for check_const_restrict.py: candidates once per AST reader backend, types and decisions.

As in test_loop_branches.py, the sample tests run once per backend that is
available; with HARNESS_AST_READER=libclang a missing libclang fails the run.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import unittest

import harness_testing
import check_const_restrict as candidates
import check_loop_branches as branches
import clang_ast_libclang

CLANG: str | None = shutil.which("clang++")
LIBCLANG_REQUIRED: bool = os.environ.get("HARNESS_AST_READER", "") == "libclang"
LIBCLANG_PROBLEM: str = clang_ast_libclang.load_problem() if CLANG is not None or LIBCLANG_REQUIRED else "no clang++"

SAMPLE: str = """
namespace std {
template <class T> struct span {
    T* data_;
    unsigned long size_;
    T& operator[](unsigned long index) const { return data_[index]; }
    unsigned long size() const { return size_; }
};
}
struct Buffer {
    int count;
    int values[4];
    int size() const { return count; }
    void clear() { count = 0; }
};
void consume(const Buffer& buffer);
void fill(Buffer& buffer);
void keep(int* pointer);
void keep_float(float* pointer);
void look(const int* pointer);

int reads(Buffer& buffer, int* values, const int* fixed, Buffer* other) {
    int total = buffer.size() + buffer.values[0] + values[1] + *values + fixed[0] + other->count;
    consume(buffer);
    look(values);
    if (values == nullptr) return 0;
    for (const int& item : buffer.values) total += item;
    return total;
}

void writes(Buffer& assigned, Buffer& cleared, Buffer& filled, int* stepped, int* kept, Buffer& iterated,
            Buffer& addressed, Buffer* called) {
    assigned.count = 1;
    cleared.clear();
    fill(filled);
    ++stepped[0];
    keep(kept);
    for (int& item : iterated.values) item = 0;
    int* inner = &addressed.count;
    (void)inner;
    called->clear();
}

struct Holder {
    Buffer* target;
    void (Buffer::*action)();
};
int deep(Holder& written, Holder& read, Holder& acted) {
    written.target->count = 1;
    (acted.target->*acted.action)();
    return read.target->count + (*read.target).size();
}

int locals(Buffer& source) {
    Buffer& alias = source;
    int* cursor = source.values;
    return alias.count + cursor[0];
}

struct Base {
    virtual int peek(Buffer& buffer) { return buffer.count; }
};
struct Derived : Base {
    int peek(Buffer& buffer) override { return buffer.count + 1; }
};
int callback(Buffer& buffer) { return buffer.count; }
int (*registered)(Buffer&) = &callback;

void scale(float* output, const float* input, int count, float factor) {
    for (int index = 0; index < count; ++index) output[index] = input[index] * factor;
}
void mix(int* output, const float* left, int count) {
    for (int index = 0; index < count; ++index) output[index] = int(left[index]);
}
void escapes(float* output, const float* input, int count) {
    keep_float(output);
    for (int index = 0; index < count; ++index) output[index] = input[index];
}
void add_spans(std::span<float> output, std::span<const float> input) {
    for (unsigned long index = 0; index < output.size(); ++index) output[index] += input[index];
}
"""

# (function, variable) of every const candidate; the virtual and callback ones are listed, then bound.
EXPECTED_CONST: set[tuple[str, str]] = {
    ("reads", "buffer"), ("reads", "values"), ("reads", "other"),
    ("locals", "alias"), ("locals", "cursor"), ("deep", "read"),
    ("Base::peek", "buffer"), ("Derived::peek", "buffer"), ("callback", "buffer"),
}
EXPECTED_BOUND: set[tuple[str, str]] = {("Base::peek", "buffer"), ("Derived::peek", "buffer"),
                                        ("callback", "buffer")}


class SampleChecks:
    """SAMPLE's candidates through one backend (`reader`)."""
    reader: str = ""

    def run_sample(self) -> candidates.UnitResult:
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory).resolve()
            (root / "src").mkdir()
            source: Path = root / "src" / "sample.cpp"
            source.write_text(SAMPLE, encoding="utf-8")
            entry: dict = {"directory": str(root), "file": "src/sample.cpp",
                           "command": f"{CLANG or 'clang++'} -std=c++20 -c src/sample.cpp"}
            job: branches.UnitJob = branches.UnitJob(entry, root.as_posix(), ("src",), self.reader)
            branches.DONE.clear()
            result: candidates.UnitResult = candidates.analyse_unit(job)
        return result

    def test_const_candidates_and_bound_signatures(self) -> None:
        result: candidates.UnitResult = self.run_sample()
        bound: set[str] = set(result.bound_functions)
        found: set[tuple[str, str]] = set()
        listed_bound: set[tuple[str, str]] = set()
        record: dict
        for record in result.findings:
            if record["kind"] != "const":
                continue
            key: tuple[str, str] = (record["function"], record["variable"])
            found.add(key)
            if candidates.status_of(record, bound, None) == "signature_bound":
                listed_bound.add(key)
            self.assertEqual(record["path"], "src/sample.cpp")
        self.assertEqual(found, EXPECTED_CONST)
        self.assertEqual(listed_bound, EXPECTED_BOUND)
        suggested: dict[str, str] = {record["variable"]: record["suggested"] for record in result.findings
                                     if record["kind"] == "const" and record["function"] == "reads"}
        self.assertEqual(suggested["values"], "const int *")
        self.assertEqual(suggested["buffer"], "const Buffer &")

    def test_restrict_candidates(self) -> None:
        result: candidates.UnitResult = self.run_sample()
        found: dict[str, dict] = {record["function"]: record for record in result.findings
                                  if record["kind"] != "const"}
        self.assertEqual(set(found), {"scale", "mix", "add_spans"})
        self.assertEqual(found["scale"]["kind"], "restrict")
        self.assertEqual(found["scale"]["outputs"], ["output"])
        self.assertEqual(found["scale"]["inputs"], ["input"])
        self.assertEqual(found["scale"]["aliasing"], "may_alias")
        self.assertEqual(found["scale"]["tier"], "private")
        self.assertEqual(found["mix"]["aliasing"], "distinct_types")
        self.assertEqual(found["add_spans"]["kind"], "restrict_span")
        self.assertEqual(found["add_spans"]["outputs"], ["output"])
        self.assertEqual(found["add_spans"]["inputs"], ["input"])


@unittest.skipIf(CLANG is None, "needs clang++ on PATH")
class JsonSampleTests(SampleChecks, unittest.TestCase):
    reader = "json"


@unittest.skipIf(bool(LIBCLANG_PROBLEM) and not LIBCLANG_REQUIRED, "libclang: " + LIBCLANG_PROBLEM)
class LibclangSampleTests(SampleChecks, unittest.TestCase):
    reader = "libclang"


class TypeTests(unittest.TestCase):
    def test_indirection(self) -> None:
        shape: candidates.Indirection | None = candidates.indirection("const float *const")
        self.assertIsNotNone(shape)
        self.assertEqual((shape.sigil, shape.pointee), ("*", "const float"))
        restricted: candidates.Indirection | None = candidates.indirection("float *__restrict")
        self.assertTrue(restricted is not None and restricted.restricted)
        self.assertEqual(candidates.indirection("Buffer &").pointee, "Buffer")
        self.assertIsNone(candidates.indirection("int **"))
        self.assertIsNone(candidates.indirection("Buffer &&"))
        self.assertIsNone(candidates.indirection("int (*)(int)"))
        self.assertIsNone(candidates.indirection("int const"))

    def test_constness(self) -> None:
        self.assertTrue(candidates.const_pointee("const Buffer"))
        self.assertTrue(candidates.const_pointee("Buffer const"))
        self.assertFalse(candidates.const_pointee("std::vector<const int>"))
        self.assertTrue(candidates.accepts_const("const int *", "*"))
        self.assertTrue(candidates.accepts_const("bool", "*"))
        self.assertFalse(candidates.accepts_const("int *", "*"))
        self.assertTrue(candidates.accepts_const("Buffer", "&"))
        self.assertFalse(candidates.accepts_const("Buffer &&", "&"))
        self.assertFalse(candidates.accepts_const("", "&"))

    def test_return_and_span_types(self) -> None:
        self.assertEqual(candidates.return_type("const int *(int, float) const"), "const int *")
        self.assertEqual(candidates.return_type("std::vector<int> (const Buffer &)"), "std::vector<int>")
        self.assertEqual(candidates.span_element("std::span<const float>"), "const float")
        self.assertEqual(candidates.span_element("std::span<std::uint8_t, 4>"), "std::uint8_t")
        self.assertIsNone(candidates.span_element("float *"))


class DecisionTests(unittest.TestCase):
    def test_decisions_match_by_path_function_variable_and_kind(self) -> None:
        decisions: list[candidates.Decision] = [candidates.Decision("src/a.cpp", "f", "values", "const", "apply",
                                                                    "")]
        record: dict = {"path": "src/a.cpp", "function": "f", "variable": "values", "kind": "const"}
        self.assertIsNotNone(candidates.decided(record, decisions))
        record["variable"] = "items"
        self.assertIsNone(candidates.decided(record, decisions))
        kernel: dict = {"path": "src/a.cpp", "function": "f", "outputs": ["values"], "kind": "restrict"}
        self.assertIsNone(candidates.decided(kernel, decisions))


if __name__ == "__main__":
    unittest.main()
