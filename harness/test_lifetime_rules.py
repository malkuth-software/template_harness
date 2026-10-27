"""Fixtures for check_lifetime_rules.py: what each rule reports and accepts."""

from pathlib import Path
import unittest

from check_house_style import LEXEMES, mask_literal
import check_lifetime_rules as rules


def masked(source: str) -> str:
    result: str = LEXEMES.sub(mask_literal, source)
    return result


def borrows(source: str) -> list[rules.Finding]:
    result: list[rules.Finding] = rules.check_borrows(Path("fixture.cpp"), masked(source))
    return result


def foreign(source: str) -> list[rules.Finding]:
    text: str = masked(source)
    helpers: set[str] = rules.lookup_helpers({Path("fixture.cpp"): text})
    result: list[rules.Finding] = rules.check_foreign_types(Path("fixture.cpp"), text, helpers)
    return result


FIND: str = """
const SemanticNode* find_node(const std::vector<SemanticNode>& nodes, std::string_view id) {
    for (const SemanticNode& node : nodes) { if (node.stable_id == id) return &node; }
    return nullptr;
}
"""


class BorrowTests(unittest.TestCase):
    def test_borrow_without_deleted_overload(self) -> None:
        findings: list[rules.Finding] = borrows(FIND)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "borrow-refuses-temporaries")
        self.assertEqual(findings[0].line, 2)

    def test_deleted_overload_accepts(self) -> None:
        source: str = FIND + "const SemanticNode* find_node(std::vector<SemanticNode>&& nodes, std::string_view id) = delete;\n"
        self.assertEqual(borrows(source), [])

    def test_qualified_element_type(self) -> None:
        source: str = "const Row& row_named(const std::vector<gui_forms::Row>& rows, int id) { return rows[0]; }\n"
        self.assertEqual(len(borrows(source)), 1)

    def test_snapshot_argument(self) -> None:
        source: str = "const Overlay* find_overlay(const VisualInspectionSnapshot& snapshot) { return nullptr; }\n"
        self.assertEqual(len(borrows(source)), 1)

    def test_string_returning_characters(self) -> None:
        source: str = "const char* text_of(const std::string& value) { return value.c_str(); }\n"
        self.assertEqual(len(borrows(source)), 1)

    def test_unrelated_return_type_is_not_a_borrow(self) -> None:
        source: str = "Control* find(Window& window, const std::vector<std::string>& names) { return nullptr; }\n"
        self.assertEqual(borrows(source), [])

    def test_value_return_is_not_a_borrow(self) -> None:
        source: str = "SemanticNode copy_node(const std::vector<SemanticNode>& nodes) { return nodes[0]; }\n"
        self.assertEqual(borrows(source), [])

    def test_deleted_overload_in_an_included_header(self) -> None:
        source: str = FIND.replace("find_node", "find_descriptor")
        self.assertEqual(len(borrows(source)), 1)
        findings: list[rules.Finding] = rules.check_borrows(Path("fixture.cpp"), masked(source), {"find_descriptor"})
        self.assertEqual(findings, [])

    def test_comments_are_ignored(self) -> None:
        source: str = "// const Row* first(const std::vector<Row>& rows) { return nullptr; }\n"
        self.assertEqual(borrows(source), [])


HELPER: str = """
template <typename Function>
Function load_function(HMODULE module, const char* name) noexcept {
    const FARPROC raw = GetProcAddress(module, name);
    Function function{};
    std::memcpy(&function, &raw, sizeof(function));
    return function;
}
"""


class ForeignTypeTests(unittest.TestCase):
    def test_hand_written_alias_through_helper(self) -> None:
        source: str = HELPER + """
using GetDpi = UINT(WINAPI*)(HWND);
const GetDpi get = load_function<GetDpi>(user32, "GetDpiForWindow");
"""
        findings: list[rules.Finding] = foreign(source)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule, "foreign-type-from-declaration")

    def test_decltype_alias_accepts(self) -> None:
        source: str = HELPER + """
using GetDpi = decltype(&::GetDpiForWindow);
const GetDpi get = load_function<GetDpi>(user32, "GetDpiForWindow");
"""
        self.assertEqual(foreign(source), [])

    def test_static_assertion_accepts(self) -> None:
        source: str = HELPER + """
using Submit = gf_result (*)(std::uintptr_t);
static_assert(std::is_same_v<Submit, decltype(&gf_submit_v1)>);
const Submit submit = load_function<Submit>(module, "gf_submit_v1");
"""
        self.assertEqual(foreign(source), [])

    def test_reinterpret_cast_of_a_lookup(self) -> None:
        source: str = """
using Fake = int (*)(Display*, int);
const Fake fake = reinterpret_cast<Fake>(dlsym(library, "XTestFakeMotionEvent"));
"""
        self.assertEqual(len(foreign(source)), 1)

    def test_raw_lookup_copied_into_an_alias(self) -> None:
        source: str = """
using GetDpiForMonitorFunction = HRESULT(WINAPI*)(HMONITOR, int, UINT*, UINT*);
GetDpiForMonitorFunction load() {
    const FARPROC symbol = GetProcAddress(library, "GetDpiForMonitor");
    GetDpiForMonitorFunction function{};
    std::memcpy(&function, &symbol, sizeof(function));
    return function;
}
"""
        self.assertEqual(len(foreign(source)), 1)

    def test_callbacks_without_lookup_are_not_reported(self) -> None:
        source: str = "using Thunk = void (*)(void*, int);\nThunk thunk = &run;\n"
        self.assertEqual(foreign(source), [])


if __name__ == "__main__":
    unittest.main()
