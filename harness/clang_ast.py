"""Read a translation unit's first-party declarations from clang's JSON AST dump.

The shared AST reader of Phase E (E2 loop branches, E3 const and restrict
candidates, the C3 codemod). It runs the pinned clang's front end over one
compile-database entry with `-Xclang -ast-dump=json` and keeps only the
declarations a caller wants, without holding the whole dump: one unit that
includes the standard library dumps hundreds of megabytes.

The dump format it relies on (clang 18 to 22):

- the dump is pretty-printed, two spaces per level, so each top-level
  declaration of the TranslationUnitDecl starts at a line holding exactly
  `    {` and ends at `    }` or `    },`;
- a location names its file only when the file differs from the previous
  location written, anywhere in the dump; a location's `includedFrom` names
  the including file and does not change it;
- a location inside a macro expansion holds `spellingLoc` and
  `expansionLoc` instead of an offset;
- offsets are byte offsets into the file as read, so a caller slicing source
  text reads the file as bytes (a CRLF checkout keeps its two-byte line ends).

A declaration is parsed (json.loads) only when its own location is in a file
`keep` accepts. The file in effect is tracked through skipped text too, so a
kept declaration's walk starts from the right file.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Callable, Iterator, TextIO

FILE_LINE: re.Pattern[str] = re.compile(r'^\s*"file": "((?:[^"\\]|\\.)*)",?$')
TOP_START: str = "    {"
TOP_ENDS: tuple[str, ...] = ("    }", "    },")
# The location of a top-level declaration: its keys sit at six spaces, a
# location's own keys at eight.
TOP_RANGE: str = '      "range"'
TOP_LOC_FILE: str = '        "file"'
# Switches a front-end-only dump drops from a compile command.
DROPPED_WITH_VALUE: tuple[str, ...] = ("-o", "-MT", "-MF", "-MQ")
DROPPED: tuple[str, ...] = ("-MD", "-MMD", "-c")
LAUNCHERS: tuple[str, ...] = ("ccache", "ccache.exe")


@dataclass(frozen=True)
class Declaration:
    """One kept top-level declaration and the file in effect just before it."""
    file_before: str
    node: dict


@dataclass(frozen=True)
class Site:
    """Where a node is: its file and byte range, and whether a macro expansion produced it.

    For a node from a macro, the range is the expansion's (where the macro is
    used); `in_macro` says the text there is not the node's own spelling.
    """
    file: str
    begin: int          # byte offset of the first character, or -1 when the dump gives none
    end: int            # byte offset just past the last token, or -1
    in_macro: bool


def front_end_command(command: str, extra: list[str]) -> list[str]:
    """A compile command turned into a front-end-only run with `extra` appended."""
    arguments: list[str] = shlex.split(command, posix=(os.name != "nt"))
    result: list[str] = []
    skip: bool = False
    argument: str
    for argument in arguments:
        argument = argument.strip('"')
        if skip:
            skip = False
            continue
        if argument in DROPPED_WITH_VALUE:
            skip = True
            continue
        if argument in DROPPED or Path(argument).name.lower() in LAUNCHERS:
            continue
        result.append(argument)
    result.extend(["-fsyntax-only", "-Wno-everything", "-Xclang", "-ast-dump=json"])
    result.extend(extra)
    return result


def unquoted(escaped: str) -> str:
    result: str = json.loads('"' + escaped + '"')
    return result


def declarations(lines: Iterator[str], keep: Callable[[str], bool]) -> Iterator[Declaration]:
    """The top-level declarations of a dump whose location is in a kept file, in dump order."""
    current: str = ""           # the file of the last location written
    previous: str = ""          # the previous line, to tell an includedFrom file apart
    buffer: list[str] = []
    phase: str = "outside"      # outside, deciding (location not yet read), keeping, skipping
    own_file: str = ""          # the declaration's own location file, when it names one
    before: str = ""
    raw: str
    for raw in lines:
        line: str = raw.rstrip("\r\n")
        if '"file"' in line:
            match: re.Match[str] | None = FILE_LINE.match(line)
            if match is not None and not previous.lstrip().startswith('"includedFrom"'):
                current = unquoted(match.group(1))
                if phase == "deciding" and line.startswith(TOP_LOC_FILE):
                    own_file = current
        previous = line
        if phase == "outside":
            if line == TOP_START:
                phase = "deciding"
                buffer = [line]
                own_file = ""
            continue
        if phase != "skipping":
            buffer.append(line)
        if line in TOP_ENDS:
            if phase == "keeping":
                yield Declaration(before, json.loads("\n".join(buffer).rstrip(",")))
            phase = "outside"
            buffer = []
            before = current
            continue
        if phase == "deciding" and line.startswith(TOP_RANGE):
            located: str = own_file if own_file else before
            if keep(located):
                phase = "keeping"
            else:
                phase = "skipping"
                buffer = []


def dump_declarations(directory: str, command: list[str], keep: Callable[[str], bool]) -> Iterator[Declaration]:
    """Runs one front-end dump and yields its kept declarations; the dump never reaches disk."""
    process: subprocess.Popen[str] = subprocess.Popen(command, cwd=directory, stdout=subprocess.PIPE,
                                                      stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                                      errors="replace", bufsize=1 << 20)
    stream: TextIO | None = process.stdout
    if stream is None:
        raise RuntimeError("clang produced no output stream")
    try:
        yield from declarations(stream, keep)
    finally:
        stream.close()
        process.wait()


class Walker:
    """Visits a declaration's nodes in dump order, keeping the file each location is in.

    `visit(node, parents, site)` is called for every node before its
    children; `parents` runs from the declaration down to the node's parent.
    """

    def __init__(self, file_before: str, visit: Callable[[dict, list[dict], Site], None]) -> None:
        self.file: str = file_before
        self.visit_node: Callable[[dict, list[dict], Site], None] = visit

    def note(self, location: dict) -> None:
        """Takes up the files a location writes, in the order clang writes them."""
        if "spellingLoc" in location:
            self.note(location["spellingLoc"])
        if "expansionLoc" in location:
            self.note(location["expansionLoc"])
            return
        if "file" in location:
            self.file = location["file"]

    def walk(self, root: dict) -> None:
        parents: list[dict] = []
        self.walk_node(root, parents)

    def walk_node(self, node: dict, parents: list[dict]) -> None:
        if "loc" in node:
            self.note(node["loc"])
        site: Site = Site(self.file, -1, -1, False)
        if "range" in node:
            extent: dict = node["range"]
            begin: dict = extent.get("begin", {})
            end: dict = extent.get("end", {})
            self.note(begin)
            begin_file: str = self.file
            self.note(end)
            in_macro: bool = "expansionLoc" in begin or "expansionLoc" in end
            site = Site(begin_file, offset_of(begin), end_offset_of(end), in_macro)
        self.visit_node(node, parents, site)
        parents.append(node)
        child: dict
        for child in node.get("inner", []):
            self.walk_node(child, parents)
        parents.pop()


def offset_of(location: dict) -> int:
    """A location's byte offset; for a macro expansion, the expansion's."""
    if "expansionLoc" in location:
        location = location["expansionLoc"]
    result: int = int(location.get("offset", -1))
    return result


def end_offset_of(location: dict) -> int:
    """The byte offset just past the token a range's end location starts."""
    if "expansionLoc" in location:
        location = location["expansionLoc"]
    if "offset" not in location:
        return -1
    result: int = int(location["offset"]) + int(location.get("tokLen", 0))
    return result


class SourceText:
    """Source files read as bytes once, for slicing by dump offsets and finding lines."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.line_starts: dict[str, list[int]] = {}

    def bytes_of(self, path: str) -> bytes:
        if path not in self.data:
            self.data[path] = Path(path).read_bytes()
        return self.data[path]

    def text(self, path: str, begin: int, end: int) -> str:
        if begin < 0 or end < begin:
            return ""
        result: str = self.bytes_of(path)[begin:end].decode("utf-8", "replace")
        return result

    def line(self, path: str, offset: int) -> int:
        """The 1-based line holding a byte offset."""
        if path not in self.line_starts:
            starts: list[int] = [0]
            data: bytes = self.bytes_of(path)
            index: int = data.find(b"\n")
            while index >= 0:
                starts.append(index + 1)
                index = data.find(b"\n", index + 1)
            self.line_starts[path] = starts
        lines: list[int] = self.line_starts[path]
        low: int = 0
        high: int = len(lines)
        while low + 1 < high:
            middle: int = (low + high) // 2
            if lines[middle] <= offset:
                low = middle
            else:
                high = middle
        return low + 1
