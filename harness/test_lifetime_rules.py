"""Fixtures for check_lifetime_rules.py: what each rule reports and accepts."""

from pathlib import Path
import re
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



# The function-head pattern before it was anchored and made possessive: the
# reference HEAD must match exactly as this does, start, end and groups.
OLD_HEAD: re.Pattern[str] = re.compile(
    r"(?P<ret>(?:\bconst\s+)?[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?(?:\s+const)?\s*[*&])\s*"
    r"(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^;{}()]*)\)\s*(?:const\s*)?(?:noexcept\s*)?"
    r"(?P<end>\{|=\s*delete|;)")
HEAD_FIXTURES: tuple[str, ...] = (
    FIND,
    "const T* at(const std::vector<T>& v, int i);\nconst T* at(std::vector<T>&& v, int i) = delete;\n",
    "::gui_forms::Node& root_of(const NodeSnapshot& snapshot) { return snapshot.root(); }\n",
    "std::map<std::string, Value>::const_iterator* x(const std::map<std::string, Value>& m);\n",
    "int 1u& f(const std::string& s); x2y& g(const std::vector<x2y>& v) noexcept;\n",
    "constant& c(const std::vector<constant>& v); const char* d(const std::string& s) const {}\n",
    "a_very_long_identifier_without_any_head_after_it_at_all + another::qualified::name * 3;\n",
    "Widget const& w(const WidgetSnapshot& s); std::unique_ptr<T>& p(const std::vector<T>& v);\n",
    "operator&(const A& a); T *ptr(const std::array<T, 4>& a) = delete;\n",
)


def head_matches(pattern: re.Pattern[str], text: str) -> list[tuple[int, int, tuple[str | None, ...]]]:
    result: list[tuple[int, int, tuple[str | None, ...]]] = []
    match: re.Match[str]
    for match in pattern.finditer(text):
        result.append((match.start(), match.end(), match.groups()))
    return result


class HeadPatternTests(unittest.TestCase):
    def test_the_anchored_possessive_head_matches_as_the_old_one(self) -> None:
        fixture: str
        for fixture in HEAD_FIXTURES:
            text: str = masked(fixture)
            self.assertEqual(head_matches(rules.HEAD, text), head_matches(OLD_HEAD, text), fixture)
        # The checker's own module and this file, read as text, give many more starts.
        source: Path
        for source in (Path(rules.__file__), Path(__file__)):
            text = source.read_text(encoding="utf-8")
            self.assertEqual(head_matches(rules.HEAD, text), head_matches(OLD_HEAD, text), source.name)

    def test_one_scan_per_file_gives_the_same_findings(self) -> None:
        text: str = masked(HEAD_FIXTURES[1] + FIND)
        heads: list[re.Match[str]] = rules.function_heads(text)
        self.assertEqual(rules.check_borrows(Path("f.cpp"), text, None, heads),
                         rules.check_borrows(Path("f.cpp"), text))
        self.assertEqual(rules.deleted_overloads(text, heads), rules.deleted_overloads(text))
        self.assertEqual(rules.deleted_overloads(text), {"at"})

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
