"""Report C++ spelling violations; this is a review aid, not semantic certification.

Explicit paths define scope. Comments and literals are excluded. Lambda brackets
are review candidates because distinguishing subscripts requires a C++ parser.
Ownership, lifetimes, initialization and kernel design require source review.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True)
class Finding:
    line: int
    rule: str


LEXEMES: re.Pattern[str] = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|'
    # Consume digit-separated numeric literals before recognizing character
    # literals. Otherwise two separators can conceal executable source.
    r"\b[0-9][0-9A-Fa-fxXbB]*(?:'[0-9A-Fa-f]+)+(?:[uUlLfF]*)|"
    r'(?:u8|u|U|L)?R"([^\s()\\]{0,16})\([\s\S]*?\)\1"|'
    r'(?:u8|u|U|L)?"(?:\\[\s\S]|[^"\\])*"|'
    r"(?:u8|u|U|L)?'(?:\\[\s\S]|[^'\\])*'"
)
RULES: tuple[tuple[str, str], ...] = (
    ("explicit-type", r"\bauto\b"),
    ("arrow-or-trailing-return", r"->"),
    ("coroutine", r"\bco_(?:await|yield|return)\b"),
    ("ranges-or-views", r"\bstd\s*::\s*(?:ranges|views)\s*::"),
    ("defaulted-comparison", r"\boperator\s*(?:==|!=|<=>|<=|>=|<|>)\s*\([^;{}]*?=\s*default\b"),
    # Objective-C block literals (^{ ... }, ^(arguments) { ... } and
    # ^Return(arguments) { ... }) are anonymous callbacks, which section 2
    # forbids (owner decision V2-1). Checked in every file: C++ exclusive-or
    # never puts a brace, or a parameter list and then a brace, after `^`, so
    # in C++ sources the rule finds nothing; the macOS host's run file is
    # Objective-C++ in a ".inc".
    ("objective-c-block", r"\^\s*\{|\^\s*\([^()]*\)\s*\{|\^\s*[A-Za-z_]\w*\s*\*?\s*\([^()]*\)\s*\{"),
    ("lambda-review", r"\[(?:\s*|[^\[\];]*?)\]\s*(?:<[^;{}]+>\s*)?(?:\([^;{}]*?\)\s*)?(?:mutable\s*)?(?:noexcept\s*)?(?:->[^{};]+)?\{"),
)
# ".inc" holds C++ included into a translation unit (the hosts' run files).
EXTENSIONS: tuple[str, ...] = (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h", ".mm", ".m", ".inc")
EXCLUDED: tuple[str, ...] = (".git", ".build", "build", "third_party", "vendor", "node_modules")


def mask_literal(match: re.Match[str]) -> str:
    result: str = re.sub(r"[^\n]", " ", match.group(0))
    return result


def inspect(source: str) -> list[Finding]:
    masked: str = LEXEMES.sub(mask_literal, source)
    findings: list[Finding] = []
    rule: str = ""
    expression: str = ""
    for rule, expression in RULES:
        pattern: re.Pattern[str] = re.compile(expression)
        match: re.Match[str]
        for match in pattern.finditer(masked):
            if rule == "lambda-review":
                prefix: str = masked[:match.start()].rstrip()
                # An identifier immediately before brackets names an array or
                # subscript. A return keyword can instead precede a lambda.
                previous: re.Match[str] | None = re.search(r"[A-Za-z_][A-Za-z_0-9]*$", prefix)
                if previous is not None and previous.group(0) != "return":
                    continue
            line: int = masked.count("\n", 0, match.start()) + 1
            findings.append(Finding(line, rule))
    return findings


def collect(paths: list[str]) -> list[Path]:
    selected: set[Path] = set()
    value: str = ""
    for value in paths:
        path: Path = Path(value)
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_file():
            if path.suffix in EXTENSIONS:
                selected.add(path)
            else:
                raise ValueError("Expected C++ source: " + str(path))
            continue
        candidate: Path
        for candidate in path.rglob("*"):
            if candidate.suffix not in EXTENSIONS or not candidate.is_file():
                continue
            relative: Path = candidate.relative_to(path)
            excluded: bool = False
            part: str = ""
            for part in relative.parts:
                if part in EXCLUDED:
                    excluded = True
                    break
            if not excluded:
                selected.add(candidate)
    result: list[Path] = sorted(selected)
    return result


def main() -> int:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+")
    arguments: argparse.Namespace = parser.parse_args()
    paths: list[Path] = collect(arguments.paths)
    if not paths:
        parser.error("No C++ files selected; empty scope is not a pass.")
    count: int = 0
    path: Path
    for path in paths:
        source: str = path.read_text(encoding="utf-8-sig")
        findings: list[Finding] = inspect(source)
        finding: Finding
        for finding in findings:
            print(f"{path}:{finding.line}: {finding.rule}")
        count += len(findings)
    print(f"{len(paths)} files; {count} spelling findings/review candidates. Semantic review still required.")
    if count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
