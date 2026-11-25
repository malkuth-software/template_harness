"""Fixtures for check_state_machines.py: what each rule reports and accepts."""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import harness_testing
import check_state_machines as hunt

CHECKER: Path = Path(__file__).resolve().parent / "check_state_machines.py"


def git(root: Path, *arguments: str) -> None:
    """Runs git in `root` with a fixed identity, failing the test on any error."""
    subprocess.run(["git", "-C", str(root), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", *arguments], check=True, capture_output=True)


def switches(source: str) -> list[hunt.Switch]:
    result: list[hunt.Switch] = hunt.find_switches(hunt.masked(source))
    return result


def candidates(source: str) -> list[hunt.Finding]:
    result: list[hunt.Finding] = hunt.source_candidates("src/core/fixture.cpp", source)
    return result


def rules_of(findings: list[hunt.Finding]) -> list[str]:
    rules: list[str] = []
    finding: hunt.Finding
    for finding in findings:
        rules.append(finding.rule)
    return rules


NESTED: str = """
int f(Mode mode, Kind kind) {
    switch (mode) {
    case Mode::a:
        switch (kind) { case Kind::x: return 1; default: return 2; }
    case Mode::b: { return 3; }
    default: return 0;
    }
}
"""


class SwitchParserTests(unittest.TestCase):
    def test_labels_of_a_nested_switch_belong_to_it(self) -> None:
        found: list[hunt.Switch] = switches(NESTED)
        self.assertEqual(len(found), 2)
        outer: hunt.Switch = found[0]
        texts: list[str] = []
        label: hunt.Label
        for label in outer.labels:
            texts.append(label.kind + ":" + label.text)
        self.assertEqual(texts, ["case:Mode::a", "case:Mode::b", "default:"])
        self.assertEqual(hunt.switch_enum(outer), "Mode")

    def test_qualified_labels_end_at_the_single_colon(self) -> None:
        found: list[hunt.Switch] = switches("void g(gui_forms::Role r) { switch (r) { case gui_forms::Role::x: break; } }")
        self.assertEqual(found[0].labels[0].text, "gui_forms::Role::x")
        self.assertEqual(hunt.switch_enum(found[0]), "Role")


class IgnoredInputTests(unittest.TestCase):
    def test_bare_break_without_a_comment_is_a_candidate(self) -> None:
        source: str = "void h(E e) { switch (e) { case E::a: run(); break; case E::b: break; default: break; } }"
        self.assertEqual(rules_of(candidates(source)), ["C1", "C1"])

    def test_a_comment_states_the_reason(self) -> None:
        source: str = """void h(E e) {
    switch (e) {
    case E::a: run(); break;
    // Moves need no work: hover is recomputed on paint.
    case E::b: break;
    default:
        break;   // Ignored: future kinds are not ours to handle.
    }
}"""
        self.assertEqual(candidates(source), [])

    def test_grouped_labels_share_the_next_body(self) -> None:
        source: str = "int h(E e) { switch (e) { case E::a: case E::b: return 1; default: return fail(); } }"
        self.assertEqual(candidates(source), [])


class ChainTests(unittest.TestCase):
    def test_enum_chain_without_else_is_a_candidate(self) -> None:
        source: str = """void k(State s) {
    if (s == State::idle) { start(); } else if (s == State::running) { stop(); }
}"""
        self.assertEqual(rules_of(candidates(source)), ["C2"])

    def test_a_final_else_or_a_mixed_chain_is_not(self) -> None:
        with_else: str = "void k(State s) { if (s == State::idle) { a(); } else if (s == State::run) { b(); } else { c(); } }"
        mixed: str = "void k(State s, int n) { if (s == State::idle) { a(); } else if (n == 3) { b(); } }"
        single: str = "void k(State s) { if (s == State::idle) { a(); } }"
        self.assertEqual(candidates(with_else), [])
        self.assertEqual(candidates(mixed), [])
        self.assertEqual(candidates(single), [])


class ShapeTests(unittest.TestCase):
    def test_flags_beside_values_antonyms_and_lifecycles(self) -> None:
        source: str = """
struct Sample {
    std::optional<int> value{};
    bool has_value{false};
    Rect bounds{};
    bool bounds_valid = false;
    bool is_open{};
    bool is_closed{};
    bool pending{};
    bool running{};
    bool failed{};
    void reset();
};"""
        details: list[str] = []
        finding: hunt.Finding
        for finding in candidates(source):
            self.assertEqual(finding.rule, "C3")
            details.append(finding.detail)
        joined: str = "\n".join(details)
        self.assertIn("has_value mirrors std::optional value", joined)
        self.assertIn("bounds_valid qualifies bounds", joined)
        self.assertIn("is_open and is_closed", joined)
        self.assertIn("lifecycle bools pending, running, failed", joined)

    def test_anatomy_declarations_and_different_subjects_are_not_candidates(self) -> None:
        states: str = "struct States final { bool enabled{}; bool disabled{}; bool open{}; bool closed{}; };"
        subjects: str = "struct Host { bool presentation_pending_{}; bool frame_capture_completed_{}; };"
        lifecycles: str = "struct Host { bool render_pending_{}; bool present_running_{}; bool capture_failed_{}; };"
        self.assertEqual(candidates(states), [])
        self.assertEqual(candidates(subjects), [])
        self.assertEqual(candidates(lifecycles), [])

    def test_independent_options_are_not_candidates(self) -> None:
        source: str = "struct Options { bool checked{}; bool enabled{true}; bool wraps{}; int count = 0; };"
        self.assertEqual(candidates(source), [])

    def test_status_beside_optional_payload(self) -> None:
        source: str = """
enum class LoadStatus { loaded, missing, failed };
struct NoteLoadResult final {
    LoadStatus status{LoadStatus::failed};
    std::optional<Note> note{};
};
struct ClipboardImageResult final {
    HostServiceStatus status;
    std::optional<Image> image;
    std::uint64_t generation{};
};"""
        details: list[str] = []
        finding: hunt.Finding
        for finding in candidates(source):
            self.assertEqual(finding.rule, "C3")
            details.append(finding.detail)
        self.assertEqual(details, [
            "NoteLoadResult: status status beside std::optional note; one std::variant of the states, built by "
            "named factories and read by named queries, cannot disagree",
            "ClipboardImageResult: status status beside std::optional image; one std::variant of the states, "
            "built by named factories and read by named queries, cannot disagree",
        ])

    def test_result_record_names_its_first_status_not_a_nested_result(self) -> None:
        # DockDragOutcome's shape: `end` says how the drag ended and `target`
        # exists only for `dropped`; `result` is a record payload, not a status.
        source: str = """
enum class DragEnd : std::uint8_t { dropped, escaped };
struct DragOutcome final {
    DragEnd end{DragEnd::escaped};
    std::optional<DropTarget> target{};
    TransactionResult result{};
};"""
        findings: list[hunt.Finding] = candidates(source)
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].detail.startswith("DragOutcome: status end beside std::optional target;"))
        self.assertEqual(findings[0].line, 4)

    def test_product_enumerations_reach_a_record_in_another_file(self) -> None:
        source: str = "struct MappingResult final { MappingState state; std::optional<int> position; };"
        self.assertEqual(candidates(source), [])
        findings: list[hunt.Finding] = hunt.source_candidates("src/core/fixture.cpp", source, {"MappingState"})
        self.assertEqual(rules_of(findings), ["C3"])

    def test_a_reviewed_shape_is_not_raised(self) -> None:
        # The reference project keeps ResolvedTextLayout (state_machines.reviewed_shapes).
        source: str = """
enum class TextResolutionStatus { exact, estimated };
struct ResolvedTextLayout final {
    std::optional<FontVerticalMetrics> design_metrics;
    TextResolutionStatus status{TextResolutionStatus::estimated};
};"""
        reviewed: str = "include/gui_forms/typography/types/typography_types.hpp"
        self.assertEqual(hunt.source_candidates(reviewed, source), [])
        self.assertEqual(rules_of(hunt.source_candidates("include/gui_forms/other.hpp", source)), ["C3"])

    def test_status_shapes_that_are_not_candidates(self) -> None:
        # A C struct's int error code (stb_vorbis in stx_vorbis's C adapter),
        # a class's private state, and a status with no optional payload.
        c_struct: str = "struct stb_vorbis { int error{}; std::optional<unsigned int> length{}; };"
        owner: str = """
enum class Status { idle, busy };
class Panel { public: void run(); private: Status status_{}; std::optional<int> cached_{}; };"""
        plain: str = "enum class Status { ok, failed }; struct WriteResult final { Status status{}; std::string text; };"
        self.assertEqual(candidates(c_struct), [])
        self.assertEqual(candidates(owner), [])
        self.assertEqual(candidates(plain), [])


MAPPING: str = """
int native_role(PartRole role) {
    switch (role) {
    case PartRole::thumb: return 1;
    case PartRole::track:
    case PartRole::face: return 2;
    default: return 0;   // Unmapped roles have no native counterpart.
    }
}
int paint(PartRole role) {
    switch (role) {
    case PartRole::thumb: draw(); return 1;
    default: return 0;   // Other roles paint nothing.
    }
}
"""


class CompilerFindingTests(unittest.TestCase):
    def test_diagnostics_parse_with_windows_paths(self) -> None:
        output: str = ("D:/w/gui_forms/src/core/a.cpp:12:5: warning: 'switch' missing 'default' label [-Wswitch-default]\n"
                       "src/core/a.cpp:20:13: warning: enumeration values 'x' and 'y' not explicitly handled in switch "
                       "[-Wswitch-enum]\n")
        found: list[hunt.Diagnostic] = hunt.parse_diagnostics(output)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0].path, "D:/w/gui_forms/src/core/a.cpp")
        self.assertEqual(found[0].line, 12)
        self.assertEqual(found[1].flag, "-Wswitch-enum")
        self.assertEqual(hunt.missing_names(found[1].message), ["x", "y"])

    def test_mapping_rule(self) -> None:
        text: str = hunt.masked(MAPPING)
        found: list[hunt.Switch] = hunt.find_switches(text)
        self.assertTrue(hunt.is_mapping(text, found[0], {"PartRole"}))
        self.assertFalse(hunt.is_mapping(text, found[1], {"PartRole"}))
        self.assertFalse(hunt.is_mapping(text, found[0], {"SemanticRole"}))

    def test_reasoned_defaults_and_named_failures(self) -> None:
        source: str = """int a(E e) {
    switch (e) {
    case E::x: return 1;
    default: return 0;
    }
}
int b(E e) {
    switch (e) {
    case E::x: return 1;
    default: throw std::logic_error("unknown");
    }
}"""
        text: str = hunt.masked(source)
        comments: set[int] = hunt.comment_lines(source)
        found: list[hunt.Switch] = hunt.find_switches(text)
        self.assertFalse(hunt.default_reason(text, comments, found[0]))
        self.assertTrue(hunt.default_reason(text, comments, found[1]))

    def test_syntax_command_keeps_the_compile_and_adds_only_switch_warnings(self) -> None:
        arguments: list[str] = hunt.syntax_command(
            "ccache clang++ -DX=1 -Iinclude -O2 -MD -MT a.o -MF a.o.d -o a.o -c src/a.cpp")
        self.assertEqual(arguments[:4], ["clang++", "-DX=1", "-Iinclude", "-O2"])
        self.assertIn("src/a.cpp", arguments)
        self.assertNotIn("-o", arguments)
        self.assertNotIn("-MF", arguments)
        self.assertIn("-fsyntax-only", arguments)
        self.assertIn("-Wswitch-default", arguments)

    def test_syntax_command_drops_warnings_as_errors(self) -> None:
        # With the build's -Werror the gate's -Wswitch-default warnings
        # would fail the unit, which would then go unchecked.
        arguments: list[str] = hunt.syntax_command(
            "clang++ -Wall -Werror -Werror=switch /WX -c src/a.cpp")
        self.assertNotIn("-Werror", arguments)
        self.assertNotIn("-Werror=switch", arguments)
        self.assertNotIn("/WX", arguments)
        self.assertIn("-Wall", arguments)
        self.assertIn("-Wswitch-default", arguments)

    def test_syntax_command_drops_the_cache_settings_of_a_precompiled_header_build(self) -> None:
        # CMake's precompiled-header commands pass ccache a setting before
        # the compiler; it is neither the program nor a compiler argument.
        arguments: list[str] = hunt.syntax_command(
            "ccache sloppiness=pch_defines,time_macros clang++ -DX=1 -include-pch a.pch -o a.o -c src/a.cpp")
        self.assertEqual(arguments[:2], ["clang++", "-DX=1"])
        self.assertNotIn("sloppiness=pch_defines,time_macros", arguments)
        self.assertIn("src/a.cpp", arguments)
        # A KEY=VALUE word after the compiler is the compiler's (a define
        # written without -D would be one) and is kept.
        kept: list[str] = hunt.syntax_command("clang++ name=value -c src/b.cpp")
        self.assertEqual(kept[:2], ["clang++", "name=value"])

    def test_syntax_command_keeps_a_string_define_whole(self) -> None:
        # CMake writes a string define with escaped quotes; the front end
        # must see both quotes, or the macro expands to an unterminated
        # string and the unit fails ("expected expression").
        arguments: list[str] = hunt.syntax_command('clang++ -DDIR=\\"/tmp/fonts\\" -c src/a.cpp')
        self.assertIn('-DDIR="/tmp/fonts"', arguments)

    def test_a_covered_switch_with_a_fallback_after_it_is_noted(self) -> None:
        source: str = """const char* name(Side side) {
    switch (side) {
    case Side::left: return "left";
    case Side::right: return "right";
    }
    return "unknown";
}
"""
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            (root / "src" / "core").mkdir(parents=True)
            (root / "src" / "core" / "side.cpp").write_text(source, encoding="utf-8")
            diagnostic: hunt.Diagnostic = hunt.Diagnostic("src/core/side.cpp", 2, 5, "-Wswitch-default",
                                                          "'switch' missing 'default' label")
            found: list[hunt.Finding] = hunt.compiler_findings([diagnostic], root, set())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].rule, "OK")
        self.assertIn("falls back after it (return \"unknown\")", found[0].detail)

    def test_a_fallback_not_directly_after_or_a_case_that_stays_is_still_f1(self) -> None:
        later: str = """int later(Side side, bool strict) {
    switch (side) {
    case Side::left: return 1;
    case Side::right: return 2;
    }
    if (strict) log_unexpected();
    return 0;
}
"""
        stays: str = """int stays(Side side) {
    switch (side) {
    case Side::left: return 1;
    case Side::right: note(); break;
    }
    return 0;
}
"""
        text: str
        for text in (later, stays):
            masked_text: str = hunt.masked(text)
            self.assertFalse(hunt.complete_with_fallback(masked_text, hunt.find_switches(masked_text)[0]))
        terminal: str = hunt.masked("void t(Side s) { switch (s) { case Side::left: std::abort(); } std::terminate(); }")
        self.assertTrue(hunt.complete_with_fallback(terminal, hunt.find_switches(terminal)[0]))

    def test_a_fallback_computed_then_returned_at_the_end_is_compliant(self) -> None:
        computed: str = """Size size(Relation relation, Size a) noexcept {
    switch (relation) {
    case Relation::beside: { const Size beside{a.width * 2.0, a.height}; return beside; }
    case Relation::over: return a;
    }
    // A relation outside the enumeration overlays the two.
    const Size overlaid{a.width, a.height};
    return overlaid;
}
"""
        ends_function: str = """void Box::filter(Edit& edit) {
    switch (edit.kind) {
    case Kind::typing: type(edit); return;
    case Kind::paste: paste(edit); return;
    }
    edit.verdict = Verdict::refuse;
}
"""
        text: str
        for text in (computed, ends_function):
            masked_text: str = hunt.masked(text)
            self.assertTrue(hunt.complete_with_fallback(masked_text, hunt.find_switches(masked_text)[0]))

    def test_a_tail_that_does_not_end_the_function_or_is_empty_is_not_a_fallback(self) -> None:
        inner_block: str = """void inner(Side side, bool on) {
    if (on) {
        switch (side) {
        case Side::left: left(); return;
        case Side::right: right(); return;
        }
        note();
    }
    after();
}
"""
        empty: str = """void empty(Side side) {
    switch (side) {
    case Side::left: left(); return;
    case Side::right: right(); return;
    }
}
"""
        text: str
        for text in (inner_block, empty):
            masked_text: str = hunt.masked(text)
            self.assertFalse(hunt.complete_with_fallback(masked_text, hunt.find_switches(masked_text)[0]))

    def test_a_break_inside_a_nested_loop_does_not_leave_the_case(self) -> None:
        source: str = """double sample(Kind kind, const double* stops, int count) {
    switch (kind) {
    case Kind::timed: {
        double last = 0.0;
        for (int index = 0; index < count; ++index) {
            if (stops[index] > 1.0) break;
            last = stops[index];
        }
        while (last > 2.0) { last = last - 1.0; if (last < 0.0) break; }
        return last;
    }
    case Kind::still: return 0.0;
    }
    return 0.0;
}
"""
        text: str = hunt.masked(source)
        switch: hunt.Switch = hunt.find_switches(text)[0]
        self.assertTrue(hunt.every_case_leaves(text, switch))
        self.assertTrue(hunt.complete_with_fallback(text, switch))
        stays: str = hunt.masked("int s(E e) { switch (e) { case E::a: { for (;;) { go(); } break; } "
                                 "case E::b: return 2; } return 0; }")
        self.assertFalse(hunt.every_case_leaves(stays, hunt.find_switches(stays)[0]))

    def test_a_case_may_fall_through_into_one_that_leaves(self) -> None:
        through: str = hunt.masked("int f(E e) { switch (e) { case E::a: note(); [[fallthrough]]; "
                                   "case E::b: return 2; } return 0; }")
        self.assertTrue(hunt.complete_with_fallback(through, hunt.find_switches(through)[0]))
        last: str = hunt.masked("int f(E e) { switch (e) { case E::a: return 1; "
                                "case E::b: note(); [[fallthrough]]; } return 0; }")
        self.assertFalse(hunt.every_case_leaves(last, hunt.find_switches(last)[0]))

    def test_preprocessor_lines_are_not_statements(self) -> None:
        source: str = """void replay(Operation operation) {
    switch (operation) {
#if defined(OPTIONAL_TEXT)
    case Operation::text: {
        draw_text();
        return;
    }
#endif
    case Operation::save: save(); return;
#define NOTE(x) \\
    note(x)
    case Operation::restore: restore(); return;
    }
    return;
}
"""
        text: str = hunt.masked(source)
        switch: hunt.Switch = hunt.find_switches(text)[0]
        self.assertTrue(hunt.every_case_leaves(text, switch))
        self.assertTrue(hunt.complete_with_fallback(text, switch))
        self.assertEqual(hunt.body_statements(text, switch.labels[1]), "save(); return;")

    def test_assigning_cases_do_not_make_the_code_after_a_fallback(self) -> None:
        source: str = "int t(E e) { int v = 0; switch (e) { case E::a: v = 1; break; case E::b: v = 2; break; } return v; }"
        text: str = hunt.masked(source)
        self.assertFalse(hunt.every_case_leaves(text, hunt.find_switches(text)[0]))
        self.assertEqual(hunt.missing_text("5 enumeration values not handled in switch: 'a', 'b', 'c'..."),
                         "5 enumerators (a, b, c, ...)")

    def test_the_changed_scope_follows_includes_by_name(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            (root / "include" / "g").mkdir(parents=True)
            (root / "src").mkdir()
            (root / "include" / "g" / "kind.hpp").write_text("enum class Kind { a, b };\n", encoding="utf-8")
            (root / "include" / "g" / "uses_kind.hpp").write_text('#include "g/kind.hpp"\n', encoding="utf-8")
            (root / "include" / "g" / "other.hpp").write_text("int other();\n", encoding="utf-8")
            switching: str = "int f(Kind k) { switch (k) { case Kind::a: return 1; case Kind::b: return 2; } return 0; }\n"
            (root / "src" / "a.cpp").write_text('#include "g/uses_kind.hpp"\n' + switching, encoding="utf-8")
            (root / "src" / "b.cpp").write_text('#include "g/other.hpp"\n' + switching, encoding="utf-8")
            (root / "src" / "c.cpp").write_text('#include "g/other.hpp"\nint c() { return 0; }\n', encoding="utf-8")
            entries: list[dict] = []
            name: str
            for name in ("a", "b", "c"):
                entries.append({"directory": str(root), "file": "src/" + name + ".cpp",
                                "command": "clang++ -c src/" + name + ".cpp -o " + name + ".o"})
            scope: hunt.Scope = hunt.changed_scope(root, ["include/g/kind.hpp"])
            self.assertEqual(scope.reached_headers, frozenset({"kind.hpp", "uses_kind.hpp"}))
            reached: list[hunt.Unit] = hunt.select_units(entries, root, scope)
            self.assertEqual(len(reached), 1)
            self.assertIn("src/a.cpp", reached[0].command)
            own: list[hunt.Unit] = hunt.select_units(entries, root, hunt.changed_scope(root, ["src/b.cpp"]))
            self.assertEqual(len(own), 1)
            self.assertIn("src/b.cpp", own[0].command)
            self.assertEqual(len(hunt.select_units(entries, root)), 2)
            self.assertEqual(hunt.uncompiled_sources(["src/d.cpp", "include/g/kind.hpp", "src/a.cpp"],
                                                     hunt.compiled_sources(entries, root)), ["src/d.cpp"])

    def test_changed_sources_come_from_git_since_the_merge_base(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            git(root, "init", "-q", "-b", "main")
            (root / "src").mkdir()
            (root / "tools").mkdir()
            (root / "docs").mkdir()
            (root / ".github/workflows").mkdir(parents=True)
            (root / "src" / "a.cpp").write_text("int a() { return 0; }\n", encoding="utf-8")
            (root / "src" / "gone.cpp").write_text("int gone() { return 0; }\n", encoding="utf-8")
            (root / "tools" / "check_state_machines.py").write_text("# checker\n", encoding="utf-8")
            (root / ".github/workflows/state-machines.yml").write_text("# gate\n", encoding="utf-8")
            (root / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "base")
            git(root, "tag", "base")
            # Only a document changed: nothing to check, so the gate passes without a tree.
            (root / "docs" / "notes.md").write_text("more notes\n", encoding="utf-8")
            git(root, "commit", "-q", "-am", "docs")
            self.assertEqual(hunt.changed_sources(root, "base"), [])
            # A changed source is checked; a deleted one is not.
            (root / "src" / "a.cpp").write_text("int a() { return 1; }\n", encoding="utf-8")
            (root / "src" / "gone.cpp").unlink()
            git(root, "commit", "-q", "-am", "code")
            self.assertEqual(hunt.changed_sources(root, "base"), ["src/a.cpp"])
            # The gate's workflow is not a trigger: its flags reach the unit cache's key
            # through the compile commands.
            (root / ".github/workflows/state-machines.yml").write_text("# tuned gate\n", encoding="utf-8")
            self.assertEqual(hunt.changed_sources(root, "base"), ["src/a.cpp"])
            # A change to the checker can change any verdict: the whole tree.
            (root / "tools" / "check_state_machines.py").write_text("# tuned checker\n", encoding="utf-8")
            self.assertIsNone(hunt.changed_sources(root, "base"))

    def test_a_scope_with_no_changed_source_passes_without_a_tree(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            git(root, "init", "-q", "-b", "main")
            (root / "README.md").write_text("readme\n", encoding="utf-8")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "base")
            (root / "README.md").write_text("changed\n", encoding="utf-8")
            completed: subprocess.CompletedProcess[str] = subprocess.run(
                [sys.executable, "-B", str(CHECKER), "--root", str(root), "--changed-from", "HEAD", "--gate",
                 "--build-dir", str(root / "no-such-tree")],
                capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("nothing to check", completed.stdout)
            listed: subprocess.CompletedProcess[str] = subprocess.run(
                [sys.executable, "-B", str(CHECKER), "--root", str(root), "--changed-from", "HEAD", "--list-changed"],
                capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
            self.assertEqual(listed.stdout, "")

    def test_the_gate_counts_only_f1_and_f2(self) -> None:
        found: list[hunt.Finding] = [hunt.Finding("a", 1, "F1", "core", ""), hunt.Finding("a", 2, "C1", "core", ""),
                                     hunt.Finding("a", 3, "F2", "core", ""), hunt.Finding("a", 4, "OK", "core", "")]
        self.assertEqual(hunt.gate_failures(found), 2)
        self.assertIn("tools/check_state_machines.py", hunt.CHECKER_FILES)
        self.assertNotIn(".github/workflows/state-machines.yml", hunt.CHECKER_FILES)
        self.assertNotIn("harness.lock", hunt.CHECKER_FILES)

    def test_areas_follow_ownership(self) -> None:
        self.assertEqual(hunt.area_of("src/controls/ribbon/ribbon.cpp"), "controls")
        self.assertEqual(hunt.area_of("include/gui_forms/controls/x.hpp"), "controls")
        self.assertEqual(hunt.area_of("include/gui_forms/types/rect.hpp"), "core")
        self.assertEqual(hunt.area_of("src/host/windows/host.cpp"), "host-windows")
        self.assertEqual(hunt.area_of("tests/a_tests.cpp"), "tests")

    def test_vocabulary_headers_are_read_from_the_project(self) -> None:
        # The reference project names GUI.Forms' headers; a fixture checkout holds one of them.
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            header: Path = root / hunt.VOCABULARY_HEADERS[0]
            header.parent.mkdir(parents=True)
            header.write_text("namespace gui_forms {\nenum class PartRole : unsigned char { a, b };\n}\n",
                              encoding="utf-8")
            names: set[str] = hunt.vocabulary_enums(root)
        self.assertEqual(names, {"PartRole"})

def lock_text(commit: str) -> str:
    result: str = '[harness]\nrepository = "owner/template_harness"\ncommit = "' + commit + '"\n'
    return result


def head_of(root: Path) -> str:
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, encoding="utf-8")
    return completed.stdout.strip()


class HarnessPinTests(unittest.TestCase):
    def test_a_pin_bump_widens_the_scope_only_when_the_checker_changed(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            harness: Path = Path(directory) / "harness_checkout"
            product: Path = Path(directory) / "product"
            (harness / "harness").mkdir(parents=True)
            product.mkdir()
            git(harness, "init", "-q", "-b", "main")
            (harness / "harness" / "check_state_machines.py").write_text("# checker 1\n", encoding="utf-8")
            (harness / "harness" / "unrelated.py").write_text("# tool 1\n", encoding="utf-8")
            git(harness, "add", "-A")
            git(harness, "commit", "-q", "-m", "first")
            first: str = head_of(harness)
            (harness / "harness" / "unrelated.py").write_text("# tool 2\n", encoding="utf-8")
            git(harness, "commit", "-q", "-am", "unrelated")
            unrelated: str = head_of(harness)
            (harness / "harness" / "check_state_machines.py").write_text("# checker 2\n", encoding="utf-8")
            git(harness, "commit", "-q", "-am", "checker")
            checker: str = head_of(harness)
            git(product, "init", "-q", "-b", "main")
            (product / "harness.lock").write_text(lock_text(first), encoding="utf-8")
            git(product, "add", "-A")
            git(product, "commit", "-q", "-m", "base")
            git(product, "tag", "base")
            saved: Path = hunt.HARNESS_ROOT
            hunt.HARNESS_ROOT = harness
            try:
                (product / "harness.lock").write_text(lock_text(unrelated), encoding="utf-8")
                self.assertFalse(hunt.checker_changed_between_pins(product, "base"))
                self.assertEqual(hunt.changed_sources(product, "base"), [])
                (product / "harness.lock").write_text(lock_text(checker), encoding="utf-8")
                self.assertTrue(hunt.checker_changed_between_pins(product, "base"))
                self.assertIsNone(hunt.changed_sources(product, "base"))
                # A pin the harness clone cannot show (and cannot fetch: no origin) counts as a change.
                (product / "harness.lock").write_text(lock_text("0" * 40), encoding="utf-8")
                self.assertTrue(hunt.checker_changed_between_pins(product, "base"))
                # So does a lock that cannot be read.
                (product / "harness.lock").write_text("not a lock\n", encoding="utf-8")
                self.assertTrue(hunt.checker_changed_between_pins(product, "base"))
            finally:
                hunt.HARNESS_ROOT = saved


# A stand-in front end that also logs each source it is given: it fails for a source named
# broken (as clang does for a missing precompiled header), and otherwise reports one
# switch warning in that source.
LOGGING_FRONT_END: str = """import sys
from pathlib import Path
log = Path(sys.argv[1])
source = sys.argv[2]
with log.open("a", encoding="utf-8") as stream:
    stream.write(source + "\\n")
if "broken" in source:
    sys.stderr.write("fatal error: PCH file 'cmake_pch.hxx.pch' not found\\n")
    raise SystemExit(1)
print(source + ":1:1: warning: switch missing default case [-Wswitch-default]")
"""


def no_generated_headers(build_dir: Path) -> list[Path]:
    """The fixture tree has no ninja and no generated headers."""
    return []


class UnitCacheTests(unittest.TestCase):
    def run_tree(self, root: Path, names: list[str], cache: Path) -> hunt.CompilerRun:
        entries: list[dict] = []
        name: str
        for name in names:
            entries.append({"directory": str(root), "file": "src/" + name + ".cpp",
                            "command": sys.executable + " " + str(root / "fake_front_end.py") + " "
                            + str(root / "log.txt") + " src/" + name + ".cpp -c -o " + name + ".o"})
        build: Path = root / "out"
        build.mkdir(exist_ok=True)
        run: hunt.CompilerRun = hunt.compiler_diagnostics(build, root, 1, entries, None, cache, "salt")
        return run

    def log_lines(self, root: Path) -> list[str]:
        log: Path = root / "log.txt"
        lines: list[str] = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        log.unlink(missing_ok=True)
        return lines

    def test_only_units_whose_inputs_changed_run_again(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            (root / "src").mkdir()
            (root / "include").mkdir()
            (root / "fake_front_end.py").write_text(LOGGING_FRONT_END, encoding="utf-8")
            (root / "include" / "kind.hpp").write_text("enum class Kind { a, b };\n", encoding="utf-8")
            (root / "include" / "uses_kind.hpp").write_text('#include "kind.hpp"\n', encoding="utf-8")
            (root / "include" / "other.hpp").write_text("int other();\n", encoding="utf-8")
            switching: str = "int f(int k) { switch (k) { case 1: return 1; } return 0; }\n"
            (root / "src" / "a.cpp").write_text('#include "uses_kind.hpp"\n' + switching, encoding="utf-8")
            (root / "src" / "b.cpp").write_text('#include "other.hpp"\n' + switching, encoding="utf-8")
            cache: Path = root / "cache" / "units.json"
            saved: object = hunt.generate_headers
            hunt.generate_headers = no_generated_headers
            try:
                cold: hunt.CompilerRun = self.run_tree(root, ["a", "b"], cache)
                self.assertEqual((cold.units, cold.reused, len(cold.diagnostics)), (2, 0, 2))
                self.assertEqual(sorted(self.log_lines(root)), ["src/a.cpp", "src/b.cpp"])
                warm: hunt.CompilerRun = self.run_tree(root, ["a", "b"], cache)
                self.assertEqual((warm.reused, len(warm.diagnostics)), (2, 2))
                self.assertEqual(self.log_lines(root), [])
                # A header reached through another header changes: only its includer runs.
                (root / "include" / "kind.hpp").write_text("enum class Kind { a, b, c };\n", encoding="utf-8")
                changed: hunt.CompilerRun = self.run_tree(root, ["a", "b"], cache)
                self.assertEqual((changed.reused, len(changed.diagnostics)), (1, 2))
                self.assertEqual(self.log_lines(root), ["src/a.cpp"])
                # A unit whose front end fails is reported, never stored, and runs again.
                (root / "src" / "broken.cpp").write_text(switching, encoding="utf-8")
                failed: hunt.CompilerRun = self.run_tree(root, ["a", "b", "broken"], cache)
                self.assertEqual(len(failed.failures), 1)
                self.assertIn("src/broken.cpp: the front end exited 1", failed.failures[0])
                self.assertIn("PCH file", failed.failures[0])
                self.assertEqual(self.log_lines(root), ["src/broken.cpp"])
                again: hunt.CompilerRun = self.run_tree(root, ["a", "b", "broken"], cache)
                self.assertEqual((again.reused, len(again.failures)), (2, 1))
                self.assertEqual(self.log_lines(root), ["src/broken.cpp"])
                # The store keeps this run's units and, under its bound, the earlier entry of
                # a.cpp from before its header changed.
                stored: dict[str, str] = hunt.load_cache(cache)
                self.assertEqual(len(stored), 3)
                saved_bound: int = hunt.CACHE_ENTRIES_PER_UNIT
                hunt.CACHE_ENTRIES_PER_UNIT = 1
                try:
                    self.run_tree(root, ["a", "b"], cache)
                finally:
                    hunt.CACHE_ENTRIES_PER_UNIT = saved_bound
                self.assertEqual(len(hunt.load_cache(cache)), 2)
            finally:
                hunt.generate_headers = saved

    def test_a_store_of_another_version_or_shape_is_empty(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            cache: Path = Path(directory) / "units.json"
            self.assertEqual(hunt.load_cache(cache), {})
            cache.write_text('{"version": 0, "entries": {"k": "v"}}', encoding="utf-8")
            self.assertEqual(hunt.load_cache(cache), {})
            cache.write_text("not json", encoding="utf-8")
            self.assertEqual(hunt.load_cache(cache), {})
            hunt.save_cache(cache, {"k": "v"})
            self.assertEqual(hunt.load_cache(cache), {"k": "v"})
        self.assertEqual(hunt.diagnostic_lines("noise\na.cpp:1:2: warning: w [-Wswitch]\nfatal error: x\n"),
                         "a.cpp:1:2: warning: w [-Wswitch]")


# A stand-in front end: it fails for a source named broken (as clang does for a
# missing precompiled header) and otherwise reports one switch warning there.
FAKE_FRONT_END: str = """import sys
source = sys.argv[1]
if "broken" in source:
    sys.stderr.write("fatal error: PCH file 'cmake_pch.hxx.pch' not found\\n")
    raise SystemExit(1)
print(source + ":1:1: warning: switch missing default case [-Wswitch-default]")
"""


class FrontEndFailureTests(unittest.TestCase):
    def test_a_unit_whose_front_end_fails_is_reported_not_dropped(self) -> None:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            (root / "src").mkdir()
            (root / "fake_front_end.py").write_text(FAKE_FRONT_END, encoding="utf-8")
            switching: str = "int f(int k) { switch (k) { case 1: return 1; } return 0; }\n"
            entries: list[dict] = []
            name: str
            for name in ("a", "broken"):
                (root / "src" / (name + ".cpp")).write_text(switching, encoding="utf-8")
                entries.append({"directory": str(root), "file": "src/" + name + ".cpp",
                                "command": sys.executable + " " + str(root / "fake_front_end.py") + " src/" + name
                                + ".cpp -c -o " + name + ".o"})
            saved: object = hunt.generate_headers
            hunt.generate_headers = no_generated_headers
            try:
                run: hunt.CompilerRun = hunt.compiler_diagnostics(root, root, 2, entries, None)
            finally:
                hunt.generate_headers = saved
        self.assertEqual((run.units, len(run.diagnostics), len(run.failures)), (2, 1, 1))
        self.assertIn("src/broken.cpp: the front end exited 1", run.failures[0])
        self.assertIn("PCH file", run.failures[0])


# A stand-in front end for units meant not to compile: it refuses a source whose
# name has "refused" and accepts every other, without a switch warning.
REFUSING_FRONT_END: str = """import sys
source = sys.argv[1]
if "refused" in source:
    sys.stderr.write(source + ":1:1: error: effect code may not use a 'while' loop\\n")
    raise SystemExit(1)
"""


class ExpectedCompileFailureTests(unittest.TestCase):
    def run_cases(self, names: list[str], cache: bool) -> hunt.CompilerRun:
        directory: str
        with tempfile.TemporaryDirectory() as directory:
            root: Path = Path(directory)
            (root / "tests" / "effect_pass").mkdir(parents=True)
            (root / "front_end.py").write_text(REFUSING_FRONT_END, encoding="utf-8")
            entries: list[dict] = []
            name: str
            for name in names:
                # No switch: an expected failure runs anyway.
                (root / "tests" / "effect_pass" / (name + ".cpp")).write_text("int f();\n", encoding="utf-8")
                entries.append({"directory": str(root), "file": "tests/effect_pass/" + name + ".cpp",
                                "command": sys.executable + " " + str(root / "front_end.py")
                                + " tests/effect_pass/" + name + ".cpp -c -o " + name + ".o"})
            store: Path | None = root / "units.json" if cache else None
            saved: object = hunt.generate_headers
            saved_expected: tuple[tuple[str, str], ...] = hunt.EXPECTED_FAILURES
            hunt.generate_headers = no_generated_headers
            hunt.EXPECTED_FAILURES = (("tests/effect_pass/refused_*.cpp", "the effect pass refuses it"),
                                      ("tests/effect_pass/stale.cpp", "it used to be refused"))
            try:
                run: hunt.CompilerRun = hunt.compiler_diagnostics(root, root, 2, entries, None, store, "salt")
                if store is not None:
                    self.assertEqual(hunt.load_cache(store), {})
            finally:
                hunt.generate_headers = saved
                hunt.EXPECTED_FAILURES = saved_expected
        return run

    def test_a_listed_unit_that_fails_is_counted_not_reported(self) -> None:
        run: hunt.CompilerRun = self.run_cases(["refused_while", "refused_new"], True)
        self.assertEqual((run.units, run.reused, run.expected_failures), (2, 0, 2))
        self.assertEqual((run.failures, run.compiled_unexpectedly, run.diagnostics), ([], [], []))

    def test_a_listed_unit_that_compiles_is_reported(self) -> None:
        run: hunt.CompilerRun = self.run_cases(["refused_while", "stale", "plain"], False)
        # plain has no switch and is not listed, so it is not a unit at all.
        self.assertEqual((run.units, run.expected_failures, run.failures), (2, 1, []))
        self.assertEqual(len(run.compiled_unexpectedly), 1)
        self.assertIn("tests/effect_pass/stale.cpp: compiled", run.compiled_unexpectedly[0])
        self.assertIn("tests/effect_pass/stale.cpp: it used to be refused", run.compiled_unexpectedly[0])

    def test_the_glob_matches_the_checkout_relative_path(self) -> None:
        saved_expected: tuple[tuple[str, str], ...] = hunt.EXPECTED_FAILURES
        hunt.EXPECTED_FAILURES = (("tests/*/refused.cpp", "why"),)
        try:
            self.assertEqual(hunt.expected_failure("tests/effect_pass/refused.cpp"), "tests/*/refused.cpp: why")
            self.assertEqual(hunt.expected_failure("tests/a/b/refused.cpp"), "tests/*/refused.cpp: why")
            self.assertEqual(hunt.expected_failure("src/refused.cpp"), "")
        finally:
            hunt.EXPECTED_FAILURES = saved_expected


if __name__ == "__main__":
    unittest.main()
