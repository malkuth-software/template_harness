"""Report breaches of two mechanical house-style rules (sections 5 and 9).

borrow-refuses-temporaries (section 5): a function that returns a pointer or
reference borrowed from a container or snapshot argument must delete its
rvalue overload, so a borrow of a temporary cannot compile. The check finds
functions whose return type is a pointer or reference to the argument's
element type (or anything, for a *Snapshot argument) taken as `const T&`,
and looks for a deleted overload of the same name taking `&&` in the same
file.

foreign-type-from-declaration (section 9): a function pointer obtained by
name (GetProcAddress, dlsym) must have a type taken from the function's
declaration: an alias spelled with `decltype`, or a
`static_assert(std::is_same_v<Alias, decltype(...)>)` in the same file. The
check finds every lookup: a call to a template helper whose body calls
GetProcAddress or dlsym, a `reinterpret_cast` of such a call's result, and a
variable declared just after a raw lookup.

Both are heuristics over source with comments and literals masked; they are
review aids like check_house_style.py, not proofs. A destruction-order lint
(a borrower declared after what it borrows) is not attempted: deciding which
local reports into which needs the types and the data flow.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
import re

from check_house_style import EXCLUDED, EXTENSIONS, LEXEMES, mask_literal


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    detail: str


CONTAINERS: re.Pattern[str] = re.compile(
    r"^(?:std\s*::\s*)?(vector|deque|list|array|map|unordered_map|multimap|set|unordered_set|basic_string|string)\b")
HEAD: re.Pattern[str] = re.compile(
    r"(?P<ret>(?:\bconst\s+)?[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?(?:\s+const)?\s*[*&])\s*"
    r"(?P<name>[A-Za-z_]\w*)\s*\((?P<params>[^;{}()]*)\)\s*(?:const\s*)?(?:noexcept\s*)?"
    r"(?P<end>\{|=\s*delete|;)")
LOOKUP: re.Pattern[str] = re.compile(r"\b(?:GetProcAddress|dlsym)\s*\(")
HELPER: re.Pattern[str] = re.compile(r"template\s*<\s*typename\s+(\w+)\s*>\s*(?:\[\[nodiscard\]\]\s*)?\w+\s+(\w+)\s*\(")
FUNCTION_ALIAS: re.Pattern[str] = re.compile(r"\busing\s+(\w+)\s*=\s*([^;]*\(\s*(?:\w+\s*)?\*\s*\)[^;]*);")


# Included implementation fragments (.inc) hold host code too.
SOURCES: tuple[str, ...] = EXTENSIONS


def collect(paths: list[str]) -> list[Path]:
    selected: set[Path] = set()
    value: str = ""
    for value in paths:
        path: Path = Path(value)
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_file():
            selected.add(path)
            continue
        candidate: Path
        for candidate in path.rglob("*"):
            if candidate.suffix not in SOURCES or not candidate.is_file():
                continue
            relative: Path = candidate.relative_to(path)
            if any(part in EXCLUDED for part in relative.parts):
                continue
            selected.add(candidate)
    result: list[Path] = sorted(selected)
    return result


def masked_source(path: Path) -> str:
    source: str = path.read_text(encoding="utf-8-sig")
    result: str = LEXEMES.sub(mask_literal, source)
    return result


def line_of(text: str, offset: int) -> int:
    result: int = text.count("\n", 0, offset) + 1
    return result


def split_parameters(params: str) -> list[str]:
    parts: list[str] = []
    depth: int = 0
    current: str = ""
    character: str = ""
    for character in params:
        if character == "<":
            depth += 1
        elif character == ">":
            depth -= 1
        if character == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += character
    if current.strip():
        parts.append(current.strip())
    return parts


def template_arguments(text: str) -> list[str]:
    start: int = text.find("<")
    end: int = text.rfind(">")
    if start < 0 or end <= start:
        return []
    result: list[str] = split_parameters(text[start + 1:end])
    return result


def base_type(text: str) -> str:
    stripped: str = re.sub(r"\bconst\b", " ", text)
    stripped = stripped.replace("*", " ").replace("&", " ")
    result: str = re.sub(r"\s+", "", stripped)
    return result


def same_type(left: str, right: str) -> bool:
    # A qualified and an unqualified spelling of one name match:
    # gui_forms::SemanticNode and SemanticNode.
    if left == right:
        return True
    shorter: str = left if len(left) < len(right) else right
    longer: str = right if shorter is left else left
    result: bool = longer.endswith("::" + shorter)
    return result


def borrowed_parameter(return_type: str, parameter: str) -> str:
    """The parameter's type when the return value can borrow from it."""
    match: re.Match[str] | None = re.match(r"^const\s+(?P<type>.+?)\s*&\s*\w*$", parameter)
    if match is None:
        return ""
    argument: str = match.group("type").strip()
    returned: str = base_type(return_type)
    if re.search(r"\w*Snapshot$", argument) is not None:
        return argument
    container: re.Match[str] | None = CONTAINERS.match(argument)
    if container is None:
        return ""
    kind: str = container.group(1)
    if kind in ("string", "basic_string"):
        if returned == "char":
            return argument
        return ""
    elements: list[str] = template_arguments(argument)
    if not elements:
        return ""
    candidates: list[str] = [base_type(elements[0])]
    if kind in ("map", "unordered_map", "multimap") and len(elements) > 1:
        candidates.append(base_type(elements[1]))
    candidate: str
    for candidate in candidates:
        if same_type(candidate, returned):
            return argument
    return ""


def deleted_overloads(text: str) -> set[str]:
    deleted: set[str] = set()
    match: re.Match[str]
    for match in HEAD.finditer(text):
        if match.group("end").startswith("=") and "&&" in match.group("params"):
            deleted.add(match.group("name"))
    return deleted


def included_names(source: str) -> set[str]:
    names: set[str] = set()
    match: re.Match[str]
    for match in re.finditer(r'#\s*include\s*"([^"]+)"', source):
        names.add(Path(match.group(1)).name)
    return names


def check_borrows(path: Path, text: str, inherited: set[str] | None = None) -> list[Finding]:
    """inherited: deleted overloads declared in headers the file includes."""
    findings: list[Finding] = []
    deleted: set[str] = deleted_overloads(text)
    if inherited is not None:
        deleted |= inherited
    match: re.Match[str]
    reported: set[str] = set()
    for match in HEAD.finditer(text):
        name: str = match.group("name")
        if match.group("end").startswith("=") or name in reported or name in ("operator",):
            continue
        parameter: str
        for parameter in split_parameters(match.group("params")):
            argument: str = borrowed_parameter(match.group("ret"), parameter)
            if not argument or name in deleted:
                continue
            reported.add(name)
            findings.append(Finding(str(path), line_of(text, match.start("name")), "borrow-refuses-temporaries",
                                    name + " returns a borrow of `" + argument + "` without a deleted && overload"))
            break
    return findings


def matching_brace(text: str, opening: int) -> int:
    depth: int = 0
    index: int
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def lookup_helpers(texts: dict[Path, str]) -> set[str]:
    """Template functions whose body looks a function up by name."""
    helpers: set[str] = set()
    text: str
    for text in texts.values():
        match: re.Match[str]
        for match in HELPER.finditer(text):
            opening: int = text.find("{", match.end())
            if opening < 0:
                continue
            body: str = text[opening:matching_brace(text, opening) + 1]
            if LOOKUP.search(body) is not None:
                helpers.add(match.group(2))
    return helpers


def derived_aliases(text: str) -> set[str]:
    derived: set[str] = set()
    match: re.Match[str]
    for match in re.finditer(r"\busing\s+(\w+)\s*=\s*decltype\b", text):
        derived.add(match.group(1))
    for match in re.finditer(r"static_assert\s*\(\s*std\s*::\s*is_same(?:_v)?\s*<\s*(\w+)\s*,\s*decltype\b", text):
        derived.add(match.group(1))
    return derived


def check_foreign_types(path: Path, text: str, helpers: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    derived: set[str] = derived_aliases(text)
    aliases: set[str] = set()
    match: re.Match[str]
    for match in FUNCTION_ALIAS.finditer(text):
        if "decltype" not in match.group(2):
            aliases.add(match.group(1))
    uses: list[tuple[int, str]] = []
    helper: str
    for helper in helpers:
        for match in re.finditer(r"\b" + re.escape(helper) + r"\s*<\s*([^<>;()]+?)\s*>\s*\(", text):
            uses.append((match.start(), match.group(1).strip()))
    lines: list[str] = text.split("\n")
    lookup_lines: set[int] = set()
    for match in LOOKUP.finditer(text):
        lookup_lines.add(line_of(text, match.start()))
    for match in re.finditer(r"reinterpret_cast\s*<\s*([^<>;]+?)\s*>\s*\(", text):
        line: int = line_of(text, match.start())
        near: bool = any(line - 3 <= lookup <= line for lookup in lookup_lines)
        if near:
            uses.append((match.start(), match.group(1).strip()))
    # A raw lookup copied into a variable of an alias type a few lines on.
    for lookup in lookup_lines:
        index: int
        for index in range(lookup, min(lookup + 4, len(lines))):
            declared: re.Match[str] | None = re.match(r"\s*(?:const\s+)?(\w+)\s+\w+\s*(?:\{\}|=)", lines[index])
            if declared is not None and declared.group(1) in aliases:
                offset: int = sum(len(item) + 1 for item in lines[:index])
                uses.append((offset, declared.group(1)))
    seen: set[tuple[int, str]] = set()
    offset_and_type: tuple[int, str]
    for offset_and_type in uses:
        offset: int = offset_and_type[0]
        type_text: str = offset_and_type[1]
        line = line_of(text, offset)
        if (line, type_text) in seen:
            continue
        seen.add((line, type_text))
        if "decltype" in type_text or type_text in derived or type_text in ("Function", "FARPROC", "void*"):
            continue
        if type_text in helpers:
            continue
        findings.append(Finding(str(path), line, "foreign-type-from-declaration",
                                "`" + type_text + "` is looked up by name but not taken from a declaration"))
    return findings


def inspect_paths(paths: list[Path]) -> list[Finding]:
    texts: dict[Path, str] = {}
    path: Path
    for path in paths:
        texts[path] = masked_source(path)
    helpers: set[str] = lookup_helpers(texts)
    # A definition's deleted overload may sit in a header it includes (by
    # quoted include, matched on the file name).
    deleted_by_header: dict[str, set[str]] = {}
    for path in paths:
        if path.suffix in (".hpp", ".hh", ".h", ".inc"):
            deleted_by_header.setdefault(path.name, set()).update(deleted_overloads(texts[path]))
    findings: list[Finding] = []
    for path in paths:
        inherited: set[str] = set()
        name: str
        for name in included_names(path.read_text(encoding="utf-8-sig")):
            inherited |= deleted_by_header.get(name, set())
        findings.extend(check_borrows(path, texts[path], inherited))
        findings.extend(check_foreign_types(path, texts[path], helpers))
    return findings


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    arguments: argparse.Namespace = parser.parse_args()
    paths: list[Path] = collect(arguments.paths)
    if not paths:
        parser.error("No C++ files selected; empty scope is not a pass.")
    findings: list[Finding] = inspect_paths(paths)
    finding: Finding
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.rule}: {finding.detail}")
    print(f"{len(paths)} files; {len(findings)} lifetime-rule findings. Semantic review still required.")
    if findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
