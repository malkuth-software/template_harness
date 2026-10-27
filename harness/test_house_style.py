"""Focused scanner tests: excluded literals, physical lines, and forbidden forms."""

import tempfile
import unittest
from pathlib import Path
from check_house_style import Finding, collect, inspect


class HouseStyleTests(unittest.TestCase):
    def test_literals_and_comments(self) -> None:
        source: str = '// auto p->x;\nconst char* s = "auto ->"; /* auto */\n'
        self.assertEqual(inspect(source), [])

    def test_raw_literal(self) -> None:
        source: str = 'const char* s = R"tag(auto " -> [](){})tag";'
        self.assertEqual(inspect(source), [])

    def test_physical_line(self) -> None:
        source: str = '/* ignored\n auto */\nauto value = 1;'
        self.assertEqual(inspect(source), [Finding(3, "explicit-type")])

    def test_digit_separators_do_not_hide_execution(self) -> None:
        source: str = "int size = 1'000; auto first = size;\nint next = 2'000; auto last = next;"
        self.assertEqual(inspect(source), [Finding(1, "explicit-type"), Finding(2, "explicit-type")])

    def test_hexadecimal_separators_and_character_literals(self) -> None:
        source: str = "int bits = 0xFF'00; char quote = '\\''; auto value = bits;"
        self.assertEqual(inspect(source), [Finding(1, "explicit-type")])

    def test_callbacks_and_member_access(self) -> None:
        source: str = 'void f() { run([this](int n) { p->work(n); }); }'
        findings: list[Finding] = inspect(source)
        self.assertIn(Finding(1, "arrow-or-trailing-return"), findings)
        self.assertIn(Finding(1, "lambda-review"), findings)

    def test_defaulted_comparison(self) -> None:
        source: str = 'bool operator==(const Item&) const = default;'
        self.assertEqual(inspect(source), [Finding(1, "defaulted-comparison")])

    def test_named_execution(self) -> None:
        source: str = 'void Listener::run(int count) { (*owner).work(count); }'
        self.assertEqual(inspect(source), [])

    def test_array_initialization(self) -> None:
        source: str = 'const int values[]{1, 2}; int fixed[2]{0, 0};'
        self.assertEqual(inspect(source), [])

    def test_returned_captureless_callback(self) -> None:
        source: str = 'return [] { work(); };'
        self.assertEqual(inspect(source), [Finding(1, "lambda-review")])

    def test_generic_callback(self) -> None:
        source: str = 'use([]<typename Value>(Value value) { work(value); });'
        self.assertEqual(inspect(source), [Finding(1, "lambda-review")])

    def test_defaulted_spaceship(self) -> None:
        source: str = 'Order operator<=>(const Item&) const\n noexcept = default;'
        self.assertEqual(inspect(source), [Finding(1, "defaulted-comparison")])

    def test_objective_c_blocks(self) -> None:
        source: str = ('dispatch_async(queue, ^{ [view draw]; });\n'
                       'set(^(NSTimer* timer) { stop(); });\n'
                       'run(^BOOL(id value) { return YES; });\n')
        self.assertEqual(inspect(source), [Finding(1, "objective-c-block"),
                                           Finding(2, "objective-c-block"),
                                           Finding(3, "objective-c-block")])

    def test_no_block_is_admitted(self) -> None:
        source: str = ('watch(^NSEvent* (NSEvent* event) { // house-style: admitted block (block-only API)\n'
                       '    return event; });\n')
        self.assertEqual(inspect(source), [Finding(1, "objective-c-block")])

    def test_exclusive_or_is_not_a_block(self) -> None:
        source: str = 'if ((flags ^ mask) != 0) { bits = a ^ (b & c); }\n// ^{ in a comment\n'
        self.assertEqual(inspect(source), [])

    def test_included_sources_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run: Path = Path(directory) / "host_run.inc"
            run.write_text("auto x = 1;\n", encoding="utf-8")
            note: Path = Path(directory) / "notes.txt"
            note.write_text("auto\n", encoding="utf-8")
            self.assertEqual(collect([directory]), [run])


if __name__ == "__main__":
    unittest.main()
