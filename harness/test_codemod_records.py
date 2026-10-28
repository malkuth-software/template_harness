"""Tests for codemod_records.py: the spec, the text edits, the generator and a whole run.

The whole run reads a small tree through each reader that is available,
rewrites it, and checks that the rewritten program compiles and prints what
the original printed. It needs clang++ on PATH; the libclang run needs the
pinned LLVM's bindings (HARNESS_AST_READER=libclang makes their absence a
failure instead of a skip).
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clang_ast_libclang  # noqa: E402
import codemod_records as codemod  # noqa: E402

CLANG: str | None = shutil.which("clang++")
LIBCLANG_REQUIRED: bool = os.environ.get("HARNESS_AST_READER", "") == "libclang"

HEADER: str = textwrap.dedent("""\
    #pragma once
    #include <optional>
    #include <utility>
    namespace demo {
    enum class Status : unsigned char { success, failed, unavailable };
    enum class Error : unsigned char { none, invalid, too_large };
    struct Payload final {
        explicit constexpr Payload(int given) noexcept : value(given) {}
        int value{};
    };
    struct Service final {
        Error error{Error::none};
        bool accepted() const noexcept { return error == Error::none; }
    };
    // A request's result.
    struct Result final {
        Status status{Status::unavailable};
        // The payload, for success.
        std::optional<Payload> payload{};
        int generation{};
    };
    struct Read final {
        Service status;
        std::optional<Payload> image;
        unsigned generation{};
    };
    Result build(int x);
    Result direct();
    Result temporary();
    Read read_image(bool ok, int size);
    int describe(const Result& result);
    int describe(const Read& read);
    }
    """)

USE: str = textwrap.dedent("""\
    #include "result.hpp"
    namespace demo {
    Result build(int x) {
        Result result{};
        if (x == 0) { return result; }
        if (x < 0) {
            result.status = Status::failed;
            return result;
        }
        result.status = Status::success;
        result.payload = Payload(x);
        return result;
    }
    Result direct() { return {Status::failed, std::nullopt, 0}; }
    Result temporary() { return Result{Status::failed}; }
    Read read_image(bool ok, int size) {
        Read result;
        result.status.error = Error::invalid;
        if (!ok) return result;
        if (size > 10) { result.status.error = Error::too_large; return result; }
        Payload image{size};
        result.image = std::move(image);
        result.status = {};
        return result;
    }
    int describe(const Result& r) {
        int total = static_cast<int>(r.status) * 100;
        if (r.status == Status::success && r.payload.has_value()) total += (*r.payload).value;
        if (r.payload) total += r.payload->value;
        if (!r.payload.has_value()) total -= 1;
        const Result local{Status::success, Payload{2}};
        return total + local.generation + (*local.payload).value;
    }
    int describe(const Read& read) {
        int total = static_cast<int>(read.status.error) * 100;
        if (read.status.accepted() && read.image.has_value()) total += (*read.image).value;
        return total + static_cast<int>(read.generation);
    }
    }
    """)

# A unit the codemod reads but cannot finish: a copy written to after it is read.
HAND: str = textwrap.dedent("""\
    #include "result.hpp"
    namespace demo {
    unsigned later() {
        Read copied = read_image(true, 3);
        copied.generation = 4;
        return copied.generation;
    }
    }
    """)

MAIN: str = textwrap.dedent("""\
    #include <cstdio>
    #include "result.hpp"
    int main() {
        std::printf("%d %d %d %d %d\\n", demo::describe(demo::build(0)), demo::describe(demo::build(-1)),
                    demo::describe(demo::build(5)), demo::describe(demo::direct()), demo::describe(demo::temporary()));
        std::printf("%d %d %d\\n", demo::describe(demo::read_image(false, 0)),
                    demo::describe(demo::read_image(true, 20)), demo::describe(demo::read_image(true, 3)));
        return 0;
    }
    """)

# Named in no compile command: the codemod lists it for a hand edit.
ADAPTER: str = "#include \"result.hpp\"\nint adapter(const demo::Result& r) { return r.generation; }\n"

SPEC: str = textwrap.dedent("""\
    [[record]]
    name = "demo::Result"
    selector = "status"
    queries = { status = "copy", payload = "pointer", generation = "copy" }
    [[record.factory]]
    name = "succeeded"
    selector = "Status::success"
    fields = ["payload", "generation"]
    comment = "A payload, and the generation it was read at."
    [[record.factory]]
    name = "refused"
    selector = "other"
    fields = []

    [[record]]
    name = "demo::Read"
    selector = "status.error"
    selector_type = "Error"
    selector_default = "Error::none"
    queries = { status = "copy", image = "pointer", generation = "copy" }
    [[record.factory]]
    name = "image_read"
    selector = "Error::none"
    fields = ["image", "generation"]
    [[record.factory]]
    name = "nothing_read"
    selector = "Error::none"
    fields = ["generation"]
    [[record.factory]]
    name = "failed"
    selector = "other"
    fields = []
    """)


def write_spec(directory: Path, text: str) -> list[codemod.RecordSpec]:
    path: Path = directory / "spec.toml"
    path.write_text(text, encoding="utf-8")
    result: list[codemod.RecordSpec] = codemod.load_spec(path)
    return result


class SpecTests(unittest.TestCase):
    def test_the_sample_spec_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            specs: list[codemod.RecordSpec] = write_spec(Path(directory), SPEC)
        self.assertEqual([spec.qualified for spec in specs], ["demo::Result", "demo::Read"])
        self.assertEqual(specs[1].selector, ("status", "error"))
        self.assertEqual([factory.name for factory in specs[1].factories], ["image_read", "nothing_read", "failed"])

    def test_contradictions_are_refused(self) -> None:
        cases: list[str] = [
            'queries = { status = "value" }\n[[record.factory]]\nname = "f"\nselector = "other"\n',
            'queries = { payload = "copy" }\n[[record.factory]]\nname = "f"\nselector = "other"\n',
            'queries = { status = "copy", payload = "copy" }\n[[record.factory]]\nname = "f"\nselector = "other"\n'
            'fields = ["status"]\n',
            'queries = { status = "copy" }\n',
        ]
        case: str
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(codemod.SpecError):
                    write_spec(Path(directory), '[[record]]\nname = "demo::Result"\nselector = "status"\n' + case)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(codemod.SpecError):
                write_spec(Path(directory), '[[record]]\nname = "demo::Read"\nselector = "status.error"\n'
                           'queries = { status = "copy" }\n[[record.factory]]\nname = "f"\nselector = "other"\n')


class TextTests(unittest.TestCase):
    def test_only_source_units_are_read(self) -> None:
        # A full `ninja -t compdb` lists custom commands whose "file" is an asset under the
        # checkout; their command is a shell, and running it as a compiler made directories.
        entries: list[dict] = [
            {"directory": "/r/build", "file": "/r/src/a.cpp", "command": "clang++ -c /r/src/a.cpp"},
            {"directory": "/r/build", "file": "/r/assets/fonts/table.json",
             "command": "cmd.exe /C \"cmake -E copy /r/assets/fonts/table.json out\""},
            {"directory": "/r/build", "file": "CMakeFiles/a.dir/src/a.cpp.obj", "command": "cmd.exe /C \"ld\""},
            {"directory": "/r/build", "file": "/r/src/b.mm", "command": "clang++ -c /r/src/b.mm"},
            {"directory": "/r/build", "file": "/r/src/a.cpp", "command": "clang++ -c /r/src/a.cpp"},
        ]
        units: list[dict] = codemod.first_party_units(entries, "/r")
        files: list[str] = []
        unit: dict
        for unit in units:
            files.append(unit["file"])
        self.assertEqual(files, ["/r/src/a.cpp", "/r/src/b.mm"])

    def test_enumerator_spellings(self) -> None:
        self.assertTrue(codemod.same_enumerator("Status::success", "demo::Status::success"))
        self.assertTrue(codemod.same_enumerator("demo::Status::success", "Status::success"))
        self.assertFalse(codemod.same_enumerator("Status::success", "demo::Status::failed"))
        self.assertFalse(codemod.same_enumerator("Other::success", "demo::Status::success"))

    def test_default_arguments(self) -> None:
        def field(type_text: str, default: str) -> codemod.FieldInfo:
            return codemod.FieldInfo("f", type_text, 0, 0, default, "", [])
        self.assertEqual(codemod.default_argument(field("Status", "{Status::unavailable}")), "Status::unavailable")
        self.assertEqual(codemod.default_argument(field("int", "= 3")), "3")
        self.assertEqual(codemod.default_argument(field("std::uint64_t", "{}")), "0")
        self.assertEqual(codemod.default_argument(field("bool", "")), "false")
        self.assertEqual(codemod.default_argument(field("std::optional<Payload>", "{}")), "std::nullopt")
        self.assertEqual(codemod.default_argument(field("Rect", "{}")), "{}")
        self.assertEqual(codemod.default_argument(field("Pair", "{1, 2}")), "{}")

    def test_statement_removal_takes_the_line_when_the_statement_is_alone(self) -> None:
        data: bytes = b"{\n    a.x = 1;\n    return a;\n}\n"
        begin: int = data.index(b"a.x")
        removal: codemod.Edit | None = codemod.statement_removal(data, begin, begin + len(b"a.x = 1"))
        assert removal is not None
        self.assertEqual(codemod.applied(data, [removal]), b"{\n    return a;\n}\n")
        crlf: bytes = data.replace(b"\n", b"\r\n")
        begin = crlf.index(b"a.x")
        removal = codemod.statement_removal(crlf, begin, begin + len(b"a.x = 1"))
        assert removal is not None
        self.assertEqual(codemod.applied(crlf, [removal]), b"{\r\n    return a;\r\n}\r\n")

    def test_statement_removal_inside_a_line(self) -> None:
        data: bytes = b"if (b) { a.x = 1; return a; }\n"
        begin: int = data.index(b"a.x")
        removal: codemod.Edit | None = codemod.statement_removal(data, begin, begin + len(b"a.x = 1"))
        assert removal is not None
        self.assertEqual(codemod.applied(data, [removal]), b"if (b) { return a; }\n")
        self.assertIsNone(codemod.statement_removal(b"a.x = 1 }", 0, 7))

    def test_overlapping_edits_are_found(self) -> None:
        apart: list[codemod.Edit] = [codemod.Edit("f", 0, 2, "x"), codemod.Edit("f", 2, 2, "y"),
                                     codemod.Edit("f", 5, 6, "")]
        self.assertIsNone(codemod.overlapping(apart))
        self.assertIsNotNone(codemod.overlapping([codemod.Edit("f", 0, 3, ""), codemod.Edit("f", 2, 4, "")]))
        self.assertIsNotNone(codemod.overlapping([codemod.Edit("f", 3, 3, "a"), codemod.Edit("f", 3, 3, "b")]))
        self.assertEqual(codemod.applied(b"abcdef", apart), b"xycde")


def reader_problem(reader: str) -> str:
    if CLANG is None:
        return "needs clang++ on PATH"
    if reader == "libclang":
        problem: str = clang_ast_libclang.load_problem()
        return "" if not problem or LIBCLANG_REQUIRED else "libclang: " + problem
    return ""


class WholeRun:
    """A run over the sample tree with one reader; subclasses name it."""
    reader: str = ""

    def setUp(self) -> None:
        problem: str = reader_problem(self.reader)
        if problem:
            self.skipTest(problem)
        self.directory: tempfile.TemporaryDirectory = tempfile.TemporaryDirectory()
        self.root: Path = Path(os.path.realpath(self.directory.name))
        files: dict[str, str] = {"result.hpp": HEADER, "use.cpp": USE, "hand.cpp": HAND, "main.cpp": MAIN,
                                 "adapter.mm": ADAPTER}
        name: str
        text: str
        for name, text in files.items():
            (self.root / name).write_bytes(text.encode("utf-8"))
        self.specs: list[codemod.RecordSpec] = write_spec(self.root, SPEC)
        self.entries: list[dict] = [
            {"directory": self.root.as_posix(), "command": f"clang++ -std=c++20 -c {name} -o {name}.o", "file": name}
            for name in ("use.cpp", "hand.cpp", "main.cpp")]
        self.previous: str | None = os.environ.get("HARNESS_AST_READER")
        os.environ["HARNESS_AST_READER"] = self.reader

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("HARNESS_AST_READER", None)
        else:
            os.environ["HARNESS_AST_READER"] = self.previous
        self.directory.cleanup()

    def program_output(self) -> str:
        executable: Path = self.root / "program.exe"
        subprocess.run([CLANG, "-std=c++20", "use.cpp", "main.cpp", "-o", str(executable)], cwd=self.root,
                       check=True, capture_output=True)
        completed: subprocess.CompletedProcess = subprocess.run([str(executable)], cwd=self.root, check=True,
                                                                capture_output=True, text=True)
        return completed.stdout

    def test_the_rewritten_program_prints_what_the_original_printed(self) -> None:
        before: str = self.program_output()
        run: codemod.Run = codemod.plan(self.specs, self.root, self.entries, 1)
        self.assertEqual(run.errors, [])
        self.assertEqual(run.refused, {})
        codemod.apply_run(run)
        self.assertEqual(self.program_output(), before)
        use: str = (self.root / "use.cpp").read_text(encoding="utf-8")
        self.assertIn("if (x == 0) { return Result::refused(Status::unavailable); }", use)
        self.assertIn("return Result::succeeded(Payload(x), 0);", use)
        self.assertIn("if (size > 10) { return Read::failed(Error::too_large); }", use)
        self.assertIn("return Read::image_read(std::move(image), 0);", use)
        self.assertIn("Result direct() { return Result::refused(Status::failed); }", use)
        self.assertIn("const Result local = Result::succeeded(Payload{2}, 0);", use)
        self.assertIn("if (r.payload() == nullptr) total -= 1;", use)
        self.assertIn("if (read.status().accepted() && read.image() != nullptr) total += (*read.image()).value;", use)
        self.assertNotIn("Read result;", use)
        header: str = (self.root / "result.hpp").read_text(encoding="utf-8")
        self.assertIn("class Result final {", header)
        self.assertIn("    // The payload, for success.\n    [[nodiscard]] const Payload* payload() const noexcept {",
                      header)
        self.assertIn("        Read result{Service{error}, std::nullopt, 0};", header)
        self.assertIn("private:\n    Result(Status status, std::optional<Payload> payload, int generation) noexcept",
                      header)

    def test_what_is_left_for_a_hand_edit_is_listed(self) -> None:
        run: codemod.Run = codemod.plan(self.specs, self.root, self.entries, 1)
        self.assertEqual(run.errors, [])
        hand: list[str] = [f"{Path(site.file).name}:{site.form}:{site.reason}" for site in run.sites if site.reason]
        self.assertEqual(hand, ["hand.cpp:build local write:the local is also read or passed"])
        self.assertEqual(list(run.unread), [(self.root / "adapter.mm").as_posix()])
        forms: set[str] = {site.form for site in run.sites if not site.reason}
        self.assertTrue({"build local", "build local write", "build local return", "build return {}",
                         "build temporary", "build local initializer", "read copy", "read optional has_value",
                         "read optional *", "read optional ->", "read optional bool", "privatise"} <= forms)


class JsonWholeRunTests(WholeRun, unittest.TestCase):
    reader = "json"


class LibclangWholeRunTests(WholeRun, unittest.TestCase):
    reader = "libclang"


if __name__ == "__main__":
    unittest.main()
