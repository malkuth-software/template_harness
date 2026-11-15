#!/usr/bin/env python3
"""SushiBuild: a build run by a triage subagent, with one-file fixups routed to a fixer.

Subcommands (all print JSON unless noted; run from anywhere inside the project)
  begin [--command CMD] [--cwd DIR]
                        Start a run. The build command is CMD, else build_command from
                        the config (below). Clears the previous run's fixups.
  run [--detach]        Run the run's build command (and nothing else), capture the log to
                        .sushi/state/sushibuild/build-N.log, classify it, check caps.
                        --detach starts it in the background and returns at once.
  wait [--max SECONDS]  Wait for a detached build (default 540 s, at most 590). Prints
                        status running, or done plus the same JSON as run.
  classify LOGFILE [--exit N] [--base DIR] [--no-local-check]
                        Parse clang/gcc/MSVC/ld diagnostics into JSON with a preliminary
                        small/big verdict. The triage agent makes the final call.
  lock ABS_FILE --fix "DESC" [--error "ERR"]
                        Lock the one file the fixer may edit and snapshot it. Refuses (exit 3)
                        when a cap is hit: max fixes, wall time.
  unlock                Remove the lock.
  log --file F --error E [--fix D] [--diff-stat S] [--verdict V]
                        Record a fixup in fixups.jsonl. The diff is computed from the lock
                        snapshot when it matches F. Reports repeats.
  status                Run state: elapsed, builds, fixes, caps.
  summary --outcome success|escalated|capped|failed [--reason R] [--log PATH]
                        Render fixups.jsonl as the return block (plain text, not JSON), and
                        end the run.
  result [FILE|-]       Check a return block (default: the last summary's) and print its fields
                        as JSON; exit 1 when it is malformed, so the caller asks for it again
                        instead of reading free text.
  hook                  PreToolUse hook: reads hook JSON on stdin. Enforces the one-file
                        lock for the fixer, keeps the triage agent's writes inside its state
                        directory, its Bash away from source edits and other build commands,
                        and its Agent calls to the fixer only.

Global: --project DIR, anywhere on the command line (default: the nearest ancestor with .sushi/
or .git). .sushi/ is not created in a tree wasabi marks shared.
One run at a time per project: begin refuses while another run is active (it has not printed
its summary and is within its wall-time cap); begin --takeover ends that run first.

Config: ~/.sushi/sushibuild.json overlaid by <project>/.sushi/sushibuild.json (the project wins;
$SUSHIBUILD_CONFIG replaces both).
State: <project>/.sushi/state/sushibuild/. Log: <project>/.sushi/logs/sushibuild.log.
Python 3.9+, standard library only.
"""

import argparse
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

DEFAULT_CONFIG = {
    "build_command": None,
    "build_cwd": None,             # relative to the project root; default: the root
    "build_timeout_s": 1800,
    "max_fixes": 8,
    "max_wall_minutes": 45,
    "max_small_files": 3,          # more files with errors than this => big
    "max_errors_per_small_file": 6,
    "template_note_threshold": 3,  # this many instantiation notes => template cascade => big
    "log_excerpt_lines": 40,
    "fixer_agent_types": ["sushi-fixer"],
    "build_agent_types": ["sushi-build"],
    "path_map": [],                # [{"remote": R, "local": L}, ...] (or legacy [R, L] pairs) for diagnostic paths
    "diagnostic_base": None,       # where relative diagnostic paths start when the log names no
                                   # directory (relative to the project root); default: the build cwd
    "triage_model": None,          # null: the sushi-build agent's own model (haiku)
    "fixer_model": None,           # null: the sushi-fixer agent's own model (sonnet)
}

STATE_PARTS = (".sushi", "state", "sushibuild")
SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hh", ".hpp", ".hxx", ".h++", ".ipp", ".inl",
    ".tpp", ".m", ".mm", ".cmake", ".py", ".json", ".vcxproj", ".props", ".targets",
    ".ninja", ".mk", ".swift", ".rs", ".cs",
}
BUILD_TOOLS = {
    "make", "gmake", "ninja", "cmake", "ctest", "xcodebuild", "msbuild", "msbuild.exe", "cl",
    "cl.exe", "clang", "clang++", "gcc", "g++", "c++", "cc", "ld", "lld", "ld.lld", "link",
    "link.exe", "bazel", "scons", "meson", "swift", "swiftc", "cargo", "devenv", "nmake",
    "jom", "b2", "xmake", "premake5",
}
COMMAND_PREFIXES = {"nice", "time", "env", "command", "exec", "nohup", "caffeinate", "sudo", "xcrun"}


# ---------------------------------------------------------------- project, config, state

def find_project_root(start):
    directory = os.path.abspath(start)
    if os.path.isfile(directory):
        directory = os.path.dirname(directory)
    while True:
        if os.path.isdir(os.path.join(directory, ".sushi")) or os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


# Set by main from a global --project option.
PROJECT_OVERRIDE = None
_SUSHITREE = None


def sushitree():
    """This plugin's copy of sushitree.py, loaded by path on first use."""
    global _SUSHITREE
    if _SUSHITREE is None:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sushitree.py")
        spec = importlib.util.spec_from_file_location("sushibuild_sushitree", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SUSHITREE = module
    return _SUSHITREE


def project_root_or_cwd(start=None):
    if PROJECT_OVERRIDE:
        return os.path.abspath(os.path.expanduser(PROJECT_OVERRIDE))
    start = start or os.getcwd()
    root = find_project_root(start)
    if root is None:
        env_root = os.environ.get("CLAUDE_PROJECT_DIR")
        if env_root and os.path.isdir(env_root):
            return os.path.abspath(env_root)
        return os.path.abspath(start)
    return root


def config_paths(root):
    """The config layers, later ones winning: ~/.sushi/sushibuild.json (per machine: a build wrapper,
    path_map), then <project>/.sushi/sushibuild.json. $SUSHIBUILD_CONFIG replaces both."""
    explicit = os.environ.get("SUSHIBUILD_CONFIG")
    if explicit is not None:
        return [explicit]
    paths = [os.path.join(os.path.expanduser("~"), ".sushi", "sushibuild.json")]
    if root:
        paths.append(os.path.join(root, ".sushi", "sushibuild.json"))
    return paths


def load_config(root):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for path in config_paths(root):
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                config.update(json.load(handle))
    return config


def state_dir(root, create=False):
    path = os.path.join(root, *STATE_PARTS)
    if create:
        sushitree().make_dirs(root, path)
    return path


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def append_log(root, message):
    try:
        directory = os.path.join(root, ".sushi", "logs")
        sushitree().make_dirs(root, directory)
        with open(os.path.join(directory, "sushibuild.log"), "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))
    except (OSError, Exception):  # includes SharedTreeError: no log in a shared tree
        pass


def read_fixups(root):
    path = os.path.join(state_dir(root), "fixups.jsonl")
    records = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
    except OSError:
        pass
    return records


def emit(data):
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


def format_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)


def caps_state(root, config, run=None):
    run = run if run is not None else read_json(os.path.join(state_dir(root), "run.json"), {}) or {}
    records = read_fixups(root)
    # Fixes with one cause (the same message in several files, e.g. one missing include) count as
    # one fix unit toward max_fixes; each still needs its own one-file fixer call.
    fixes = len(fix_units(records))
    started = run.get("started")
    elapsed = (time.time() - started) if started else 0.0
    max_fixes = int(run.get("max_fixes", config["max_fixes"]))
    max_wall_s = float(run.get("max_wall_s", config["max_wall_minutes"] * 60))
    hit = []
    if fixes >= max_fixes:
        hit.append("max_fixes (%d)" % max_fixes)
    if started and elapsed >= max_wall_s:
        hit.append("wall_time (%s)" % format_duration(max_wall_s))
    return {
        "fixes": fixes,
        "fixer_calls": len(records),
        "max_fixes": max_fixes,
        "elapsed_s": int(elapsed),
        "max_wall_s": int(max_wall_s),
        "builds": int(run.get("builds", 0)),
        "cap_hit": hit,
    }


# ---------------------------------------------------------------- classify

# Make and ninja name the directory the compiler runs in; diagnostics after it are relative to it.
RE_ENTERING = re.compile(r"^\S*?(?:make|ninja)(?:\[\d+\])?: Entering directory [`'\u2018\"](?P<dir>.+?)['\u2019\"]\s*$")
RE_LEAVING = re.compile(r"^\S*?make(?:\[\d+\])?: Leaving directory [`'\u2018\"](?P<dir>.+?)['\u2019\"]\s*$")
RE_INCLUDED_FROM = re.compile(r"^(?:In file included from|\s+from)\s+(?P<file>.+?):(?P<line>\d+)(?::\d+)?[:,]?\s*$")
RE_GCC = re.compile(
    r"^(?P<file>(?:[A-Za-z]:[\\/])?[^:\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<sev>fatal error|error|warning|note|remark):\s*(?P<msg>.*)$"
)
RE_GCC_CONTEXT = re.compile(
    r"^(?P<file>(?:[A-Za-z]:[\\/])?[^:\n]+?):(?:(?P<line>\d+):(?:\d+:)?)?\s+"
    r"(?P<msg>(?:required from|required by|recursively required|In instantiation of|"
    r"in instantiation of|instantiated from|In substitution of).*)$"
)
RE_GCC_FUNC_CONTEXT = re.compile(r"^(?P<file>(?:[A-Za-z]:[\\/])?[^:\n]+?):\s+(?P<msg>In (?:member )?function|In constructor|In destructor|In static member function|At global scope|In instantiation of|In lambda function).*$")
RE_MSVC = re.compile(
    r"^\s*(?P<file>(?:[A-Za-z]:)?[^():\n][^()\n]*?)\((?P<line>\d+)(?:,(?P<col>\d+))?\)\s*:\s*"
    r"(?P<sev>fatal error|error|warning|note)\s*(?P<code>[A-Z]+\d+)?\s*:\s*(?P<msg>.*)$"
)
RE_MSVC_LINK = re.compile(
    r"^\s*(?P<obj>[^\n]+?)\s*:\s*(?P<sev>fatal error|error|warning)\s+(?P<code>LNK\d+)\s*:\s*(?P<msg>.*)$"
)
RE_MSBUILD_SUFFIX = re.compile(r"\s*\[[^\]]+\.(?:vcxproj|csproj|vcproj|proj)\]\s*$")
RE_APPLE_UNDEF_HEADER = re.compile(r"^Undefined symbols for architecture (?P<arch>\S+):\s*$")
RE_APPLE_UNDEF_SYMBOL = re.compile(r'^\s+"(?P<sym>.+)", referenced from:\s*$')
RE_APPLE_REFERENCED = re.compile(r"^\s{4,}(?P<where>.+?) in (?P<obj>\S+)\s*$")
RE_APPLE_DUP = re.compile(r"^(?:ld: )?(?:\d+ )?duplicate symbols?(?: for architecture \S+)?:?\s*'?(?P<sym>[^']*)'?(?: in:)?\s*$")
RE_GNU_UNDEF = re.compile(r"(?:(?P<where>[^\s:][^:\n]*?):(?:\(.*?\)|\d+)?:\s*)?undefined reference to [`'‘](?P<sym>.+?)['’]\s*$")
RE_GNU_MULTI = re.compile(r"(?:(?P<where>[^\s:][^:\n]*?):(?:\(.*?\)|\d+)?:\s*)?multiple definition of [`'‘](?P<sym>.+?)['’]")
RE_LLD_UNDEF = re.compile(r"^(?:\S*ld(?:\.lld|64\.lld)?|lld-link): error: undefined symbol: (?P<sym>.+)$")
RE_LLD_DUP = re.compile(r"^(?:\S*ld(?:\.lld|64\.lld)?|lld-link): error: duplicate symbol: (?P<sym>.+)$")
RE_LLD_REF = re.compile(r"^>>> (?:referenced by|defined at) (?P<where>.+)$")
RE_LINK_FAILED = re.compile(
    r"(?:ld returned \d+ exit status|linker command failed|symbol\(s\) not found|ld: error|"
    r"error: ld returned|LNK1120|unresolved externals?)", re.I)
RE_BUILD_SYSTEM = re.compile(
    r"^(?:CMake Error.*|make(?:\[\d+\])?: \*\*\*.*|ninja: (?:build stopped|error).*|"
    r"\*\* BUILD FAILED \*\*|error MSB\d+.*|.*: error MSB\d+.*|FAILED: .*)$")
RE_TOO_MANY = re.compile(r"too many errors emitted|fatal error C1003|compilation terminated due to -fmax-errors", re.I)
RE_TEMPLATE_NOTE = re.compile(
    r"in instantiation of|required from|required by|instantiated from|recursively required|"
    r"see reference to (?:class|function|variable|alias) template instantiation|"
    r"while substituting|in substitution of|while checking constraint|"
    r"during template argument deduction", re.I)

SMALL_PATTERNS = [
    r"use of undeclared identifier", r"undeclared identifier", r"was not declared in this scope",
    r"unknown type name", r"does not name a type", r"did you mean", r"expected ['\"]?[;:,)\]}>]",
    r"expected (?:expression|unqualified-id|identifier|a type|primary-expression|declaration)",
    r"missing ['\"]?;", r"syntax error", r"file not found", r"no such file or directory",
    r"cannot open (?:include|source) file", r"no member named", r"has no member named",
    r"is not a member of", r"no matching (?:member )?function for call", r"too (?:few|many) arguments",
    r"cannot convert", r"invalid conversion", r"no viable conversion", r"cannot initialize",
    r"redefinition of", r"redeclared", r"implicit declaration of function",
    r"unused (?:variable|parameter|function)", r"extraneous closing brace", r"unterminated",
    r"control reaches end of non-void function", r"non-void function does not return",
    r"must return a value", r"identifier not found", r"undeclared", r"is not a type name",
    r"missing type specifier", r"expected a ['\"]?[;)}]", r"lvalue required", r"\[-Werror",
    r"is private within this context", r"is a private member", r"cannot assign to",
    r"discards qualifiers", r"passing .* discards", r"invalid operands", r"no operator",
    r"incompatible pointer", r"incomplete type", r"forward declaration of",
    r"no (?:template|type) named", r"is not a (?:class|namespace|template)", r"has not been declared",
    r"C2065", r"C2143", r"C2146", r"C3861", r"C2039", r"C1083", r"C2059", r"C2061", r"C4430",
    r"C2664", r"C2660", r"C2440", r"C2228", r"C2027", r"C2079",
]
BIG_PATTERNS = [
    r"static[_ ]assert", r"static assertion failed", r"constraints not satisfied",
    r"internal compiler error", r"ambiguous", r"use of deleted function", r"call to deleted",
    r"recursive template instantiation", r"template instantiation depth", r"out of memory",
    r"virtual memory exhausted", r"C1001", r"C1060", r"C1202", r"C2280",
]
RE_SMALL = re.compile("|".join(SMALL_PATTERNS), re.I)
RE_BIG = re.compile("|".join(BIG_PATTERNS), re.I)
RE_DID_YOU_MEAN = re.compile(r"did you mean ['\"‘]?(?P<what>[^'\"’?;]+)['\"’]?", re.I)
RE_INCLUDE_HINT = re.compile(r"(?:try to include|forget to ['\"‘]?#include)\s*['\"‘]?(?P<what><[^>]+>|\"[^\"]+\")", re.I)
SYSTEM_PATH_MARKERS = (
    "/usr/include/", "/usr/lib/", "/c++/v1/", "/include/c++/", "/Library/Developer/",
    "/Applications/Xcode", "/opt/homebrew/", "/usr/local/include/", "Microsoft Visual Studio",
    "Windows Kits", "\\VC\\Tools\\",
)


def is_system_path(path):
    return any(marker in path for marker in SYSTEM_PATH_MARKERS)


def strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def parse_log(text):
    """Return (diagnostics, linker, build_system, flags)."""
    diagnostics = []
    linker = []
    build_system = []
    flags = {"too_many_errors": False, "link_failed": False}
    include_stack = []
    pending_context = []
    directories = []
    last = None
    lines = strip_ansi(text).splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        line = RE_MSBUILD_SUFFIX.sub("", raw.rstrip())
        index += 1
        if not line.strip():
            continue
        if RE_TOO_MANY.search(line):
            flags["too_many_errors"] = True

        match = RE_ENTERING.match(line.strip())
        if match:
            directories.append(match.group("dir"))
            continue
        match = RE_LEAVING.match(line.strip())
        if match:
            if directories:
                directories.pop()
            continue

        match = RE_INCLUDED_FROM.match(line)
        if match:
            include_stack.append("%s:%s" % (match.group("file").strip(), match.group("line")))
            continue

        match = RE_APPLE_UNDEF_HEADER.match(line)
        if match:
            flags["link_failed"] = True
            while index < len(lines):
                symbol_match = RE_APPLE_UNDEF_SYMBOL.match(lines[index])
                if not symbol_match:
                    break
                index += 1
                refs = []
                while index < len(lines):
                    ref = RE_APPLE_REFERENCED.match(lines[index])
                    if not ref or RE_APPLE_UNDEF_SYMBOL.match(lines[index]):
                        break
                    refs.append("%s in %s" % (ref.group("where").strip(), ref.group("obj")))
                    index += 1
                linker.append({"kind": "undefined", "symbol": symbol_match.group("sym"), "referenced_from": refs})
            continue

        match = RE_APPLE_DUP.match(line)
        if match and "duplicate symbol" in line:
            flags["link_failed"] = True
            objects = []
            while index < len(lines) and re.match(r"^\s{2,}\S", lines[index]) and not RE_GCC.match(lines[index]):
                objects.append(lines[index].strip())
                index += 1
            linker.append({"kind": "duplicate", "symbol": match.group("sym").strip(), "referenced_from": objects})
            continue

        match = RE_LLD_UNDEF.match(line) or RE_LLD_DUP.match(line)
        if match:
            flags["link_failed"] = True
            refs = []
            while index < len(lines) and lines[index].strip().startswith(">>>"):
                ref = RE_LLD_REF.match(lines[index].strip())
                if ref:
                    refs.append(ref.group("where"))
                index += 1
            kind = "undefined" if "undefined" in line else "duplicate"
            linker.append({"kind": kind, "symbol": match.group("sym").strip(), "referenced_from": refs})
            continue

        match = RE_MSVC_LINK.match(line)
        if match:
            flags["link_failed"] = True
            code = match.group("code")
            if code not in ("LNK1120",):
                kind = "undefined" if code in ("LNK2019", "LNK2001") else ("duplicate" if code in ("LNK2005", "LNK1169") else "other")
                symbol = re.search(r'symbol\s+"([^"]+)"', match.group("msg")) or re.search(r"symbol\s+(\S+)", match.group("msg"))
                linker.append({
                    "kind": kind, "code": code,
                    "symbol": symbol.group(1) if symbol else match.group("msg"),
                    "referenced_from": [match.group("obj").strip()],
                    "message": match.group("msg").strip(),
                })
            continue

        match = RE_GNU_UNDEF.search(line) or RE_GNU_MULTI.search(line)
        if match and ("undefined reference" in line or "multiple definition" in line):
            flags["link_failed"] = True
            kind = "undefined" if "undefined reference" in line else "duplicate"
            where = (match.group("where") or "").strip()
            linker.append({"kind": kind, "symbol": match.group("sym"), "referenced_from": [where] if where else []})
            continue

        match = RE_MSVC.match(line)
        if match:
            last = add_diagnostic(diagnostics, match, include_stack, pending_context, last, code=match.group("code"),
                                  directory=directories[-1] if directories else None)
            include_stack = []
            continue

        match = RE_GCC.match(line)
        if match and not line.lstrip().startswith(("ld:", "clang:", "clang++:", "g++:", "gcc:", "collect2:")):
            last = add_diagnostic(diagnostics, match, include_stack, pending_context, last,
                                  directory=directories[-1] if directories else None)
            include_stack = []
            continue

        match = RE_GCC_CONTEXT.match(line) or RE_GCC_FUNC_CONTEXT.match(line)
        if match:
            text_msg = match.group("msg").strip()
            if RE_TEMPLATE_NOTE.search(text_msg):
                note = "%s: %s" % (match.group("file").strip(), text_msg)
                if text_msg.startswith(("required from", "required by", "recursively required")) and last is not None:
                    last["notes"].append(note)
                else:
                    pending_context.append(note)
            continue

        if RE_LINK_FAILED.search(line):
            flags["link_failed"] = True
            continue

        if RE_BUILD_SYSTEM.match(line.strip()):
            build_system.append(line.strip())
            continue
    return diagnostics, linker, build_system, flags


def add_diagnostic(diagnostics, match, include_stack, pending_context, last, code=None, directory=None):
    severity = match.group("sev")
    message = match.group("msg").strip()
    record = {
        "file": match.group("file").strip(),
        "line": int(match.group("line")),
        "col": int(match.group("col")) if match.group("col") else None,
        "severity": severity,
        "message": message,
        "notes": [],
    }
    if code:
        record["code"] = code
    if directory:
        record["dir"] = directory
    if include_stack:
        record["included_from"] = list(include_stack)
    if severity in ("note", "remark") and last is not None:
        last["notes"].append("%s:%d: %s" % (record["file"], record["line"], message))
        return last
    if pending_context:
        record["notes"].extend(pending_context)
        del pending_context[:]
    diagnostics.append(record)
    return record


def dedupe(diagnostics):
    seen = set()
    unique = []
    for record in diagnostics:
        key = (record["file"], record["line"], record["col"], record["severity"], record["message"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def path_map_entry(entry):
    """(remote, local) from {"remote": R, "local": L} (the FranSushi convention, shared with SushiStyle)
    or a legacy [remote, local] pair; None for anything else."""
    if isinstance(entry, dict):
        remote, local = entry.get("remote"), entry.get("local")
    elif isinstance(entry, (list, tuple)) and len(entry) == 2:
        remote, local = entry[0], entry[1]
    else:
        return None
    if not remote or local is None:
        return None
    return str(remote), os.path.expanduser(str(local))


def normalized_path_map(path_map):
    """path_map entries -> [(remote, local)], longest remote prefix first, ~ expanded."""
    pairs = []
    for entry in path_map or []:
        pair = path_map_entry(entry)
        if pair is not None:
            pairs.append(pair)
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


def map_path(path, pairs):
    """Apply the first matching prefix pair. A prefix matches only at a path-component boundary."""
    for remote, local in pairs:
        if path == remote.rstrip("/\\"):
            return local.rstrip("/\\") or local
        if path.startswith(remote) and (remote.endswith(("/", "\\")) or path[len(remote):len(remote) + 1] in ("/", "\\")):
            rest = path[len(remote):].lstrip("/\\")
            return os.path.join(local, rest) if rest else local
    return path


def map_text(text, pairs):
    """Rewrite remote prefixes inside free text (notes, log lines)."""
    for remote, local in pairs:
        if remote in text:
            local_prefix = local if remote.endswith(("/", "\\")) == local.endswith(("/", "\\")) else (
                local + "/" if remote.endswith(("/", "\\")) else local.rstrip("/"))
            text = text.replace(remote, local_prefix)
    return text


def apply_path_map(diagnostics, linker, pairs):
    for record in diagnostics:
        mapped = map_path(record["file"], pairs)
        if mapped != record["file"]:
            record["printed_file"] = record["file"]
            record["file"] = mapped
        if record.get("dir"):
            record["dir"] = map_path(record["dir"], pairs)
        if record.get("included_from"):
            record["included_from"] = [map_text(item, pairs) for item in record["included_from"]]
        record["notes"] = [map_text(note, pairs) for note in record["notes"]]
    for entry in linker:
        entry["referenced_from"] = [map_text(item, pairs) for item in entry["referenced_from"]]


def is_absolute(path):
    return os.path.isabs(path) or re.match(r"^[A-Za-z]:[\\/]", path) is not None


def resolve_diagnostic_path(record, base):
    """The local path of a diagnostic's file. A relative path starts in the directory make or ninja
    said it entered (itself relative to base when relative), else in base."""
    name = record["file"]
    if is_absolute(name):
        return os.path.normpath(name)
    directory = record.get("dir")
    if directory and not is_absolute(directory) and base:
        directory = os.path.join(base, directory)
    directory = directory or base
    return os.path.normpath(os.path.join(directory, name)) if directory else os.path.normpath(name)


def unit_key(path):
    """Units group by the file itself: two spellings of one file are one unit."""
    return os.path.realpath(path) if os.path.exists(path) else os.path.normpath(path)


# A missing include repeated in several files is one mistake with one fix per file; a repeated
# undeclared name is more often an API change across files, which stays big (the agent's rubric).
RE_INCLUDE_KIND = re.compile(r"file not found|no such file or directory|cannot open (?:include|source) file|"
                             r"C1083|forget to ['\"\u2018]?#include|try to include", re.I)


def error_cause(message, fix=None):
    """The cause of a missing-include error: its message without paths or numbers, so the same
    include missing in several files compares equal. None for any other error (each file counts)."""
    if not (RE_INCLUDE_KIND.search(message or "") or "#include" in (fix or "")):
        return None
    return error_signature("", message)


def fix_units(records):
    """The distinct causes among logged fixups (a record without a cause is its own unit)."""
    units = set()
    for record in records:
        units.add(record.get("cause") or ("#%s" % record.get("n")))
    return units


def inside(path, root):
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep)


def classify_text(text, exit_code=None, base=None, config=None, check_local=False, project_root=None):
    """Parse and judge a build log. check_local: a file that does not exist on this machine
    (after path_map) cannot be fixed here, so its unit is big. project_root: a file outside it is
    not fixed by this run, so its unit is big too."""
    config = config or DEFAULT_CONFIG
    pairs = normalized_path_map(config.get("path_map"))
    diagnostics, linker, build_system, flags = parse_log(text)
    apply_path_map(diagnostics, linker, pairs)
    diagnostics = dedupe(diagnostics)
    errors = [d for d in diagnostics if d["severity"] in ("error", "fatal error")]
    warnings = [d for d in diagnostics if d["severity"] == "warning"]
    template_notes = sum(1 for d in errors for n in d["notes"] if RE_TEMPLATE_NOTE.search(n))

    units = {}
    for record in errors:
        resolved = resolve_diagnostic_path(record, base)
        unit = units.setdefault(unit_key(resolved), {"file": record["file"], "abs_file": resolved, "errors": [],
                                                     "reasons": [], "spellings": []})
        if record["file"] not in unit["spellings"]:
            unit["spellings"].append(record["file"])
        unit["errors"].append(record)
    unit_list = []
    for unit in units.values():
        name = unit["file"]
        reasons = []
        if is_system_path(name) or is_system_path(unit["abs_file"]):
            reasons.append("error inside a system/toolchain header (usually a cascade from user code)")
        elif check_local and not os.path.isfile(unit["abs_file"]):
            printed = unit["errors"][0].get("printed_file", name)
            reasons.append("file not on this machine: %s (printed as %s); can't fix locally. "
                           "If the build host uses another path, add a path_map entry" % (unit["abs_file"], printed))
        elif project_root and not inside(unit["abs_file"], project_root):
            reasons.append("file outside the project root %s: %s" % (project_root, unit["abs_file"]))
        if len(unit["errors"]) > int(config["max_errors_per_small_file"]):
            reasons.append("%d errors in one file (cascade?)" % len(unit["errors"]))
        unit_template = sum(1 for d in unit["errors"] for n in d["notes"] if RE_TEMPLATE_NOTE.search(n))
        if unit_template >= int(config["template_note_threshold"]):
            reasons.append("template instantiation cascade (%d instantiation notes)" % unit_template)
        # The first error in a file decides its kind. Later unrecognized errors in the same file
        # are usually its fallout (a missing #include makes `std::vector<int> v(n)` parse as a
        # cast), so only big-kind messages count after the first. Live trial, 2026-11-11.
        for index, record in enumerate(unit["errors"]):
            if RE_BIG.search(record["message"]):
                reasons.append("big-kind error: %s" % record["message"][:120])
            elif index == 0 and not RE_SMALL.search(record["message"] + " " + record.get("code", "")):
                reasons.append("unrecognized error kind: %s" % record["message"][:120])
        unit["prelim"] = "big" if reasons else "small"
        unit["reasons"] = reasons
        first = unit["errors"][0]
        if RE_INCLUDE_KIND.search(first["message"] + " " + " ".join(first["notes"])):
            unit["cause"] = error_signature("", first["message"])
        else:
            unit["cause"] = "file:" + unit_key(unit["abs_file"])
        unit["first_error"] = "%s:%d%s: %s" % (
            first["file"], first["line"], (":%d" % first["col"]) if first["col"] else "", first["message"])
        context = first["message"] + " " + " ".join(first["notes"])
        include_hint = RE_INCLUDE_HINT.search(context)
        hint = RE_DID_YOU_MEAN.search(context)
        if include_hint:
            unit["hint"] = "add #include %s" % include_hint.group("what")
        elif hint:
            unit["hint"] = "did you mean %s" % hint.group("what").strip()
        unit_list.append(unit)

    reasons = []
    if linker or (flags["link_failed"] and not errors):
        symbols = sorted({entry["symbol"] for entry in linker})
        reasons.append("linker errors (%d symbol(s): %s)" % (len(symbols), ", ".join(symbols[:5]) or "unparsed"))
    if template_notes >= int(config["template_note_threshold"]):
        reasons.append("template instantiation cascade (%d instantiation notes)" % template_notes)
    if flags["too_many_errors"]:
        reasons.append("compiler gave up: too many errors")
    causes = {}
    for unit in unit_list:
        causes.setdefault(unit["cause"], []).append(unit)
    for unit in unit_list:
        unit["same_cause_files"] = len(causes[unit["cause"]])
    # Files that fail for one missing include are one fix unit: four files missing the same include
    # are one mistake repeated, not four. Other errors count per file.
    if len(causes) > int(config["max_small_files"]):
        reasons.append("errors in %d files with %d distinct causes (> max_small_files %d)" % (
            len(unit_list), len(causes), int(config["max_small_files"])))
    for unit in unit_list:
        if unit["prelim"] == "big":
            reasons.append("%s: %s" % (unit["file"], "; ".join(unit["reasons"][:2])))

    failed = bool(errors or linker or flags["link_failed"] or build_system) or (exit_code not in (None, 0))
    if not failed:
        prelim = "none"
    elif reasons:
        prelim = "big"
    elif not errors:
        prelim = "big"
        reasons.append("build failed with no parseable compiler diagnostic (build system or config?)")
    else:
        prelim = "small"

    return {
        "exit": exit_code,
        "prelim": prelim,
        "reasons": reasons,
        "counts": {
            "errors": len(errors), "warnings": len(warnings), "linker": len(linker),
            "files_with_errors": len(unit_list), "distinct_causes": len(causes), "template_notes": template_notes,
        },
        "units": unit_list,
        "linker": linker,
        "build_system": build_system[:10],
        "diagnostics": diagnostics,
    }


def compact_classification(result, max_units=6):
    """The part the triage agent needs, without the full diagnostics list."""
    units = []
    for unit in result["units"][:max_units]:
        entry = {
            "file": unit["file"], "abs_file": unit["abs_file"], "prelim": unit["prelim"],
            "error_count": len(unit["errors"]), "first_error": unit["first_error"],
            "errors": ["%d:%s %s" % (e["line"], (str(e["col"]) + ":") if e["col"] else "", e["message"]) for e in unit["errors"][:4]],
        }
        if unit["errors"][0].get("printed_file"):
            entry["printed_file"] = unit["errors"][0]["printed_file"]
        if len(unit.get("spellings") or []) > 1:
            entry["spellings"] = unit["spellings"]
        if unit.get("same_cause_files", 1) > 1:
            entry["same_cause_files"] = unit["same_cause_files"]
        if unit.get("hint"):
            entry["hint"] = unit["hint"]
        if unit["reasons"]:
            entry["reasons"] = unit["reasons"][:3]
        units.append(entry)
    return {
        "prelim": result["prelim"], "reasons": result["reasons"], "counts": result["counts"],
        "units": units, "linker": result["linker"][:8], "build_system": result["build_system"][:5],
    }


def error_signature(file_path, message):
    """File basename + message with any 'path:line:col: error:' prefix and all numbers removed."""
    message = re.sub(r"^\s*(?:[A-Za-z]:)?[^:\n]*:\d+(?::\d+)?:\s*", "", message or "")
    message = re.sub(r"^(?:fatal error|error)\s*(?:[A-Z]+\d+)?\s*:\s*", "", message)
    message = re.sub(r"\d+", "#", message).strip().lower()
    return "%s|%s" % (os.path.basename(file_path or ""), message[:200])


# ---------------------------------------------------------------- commands

def cmd_begin(args):
    root = project_root_or_cwd()
    config = load_config(root)
    command = args.command or config.get("build_command")
    if not command:
        emit({"ok": False, "error": "no build command: pass --command or set build_command in .sushi/sushibuild.json"})
        return 2
    cwd = args.cwd or config.get("build_cwd") or root
    if not os.path.isabs(cwd):
        cwd = os.path.join(root, cwd)
    try:
        directory = state_dir(root, create=True)
    except sushitree().SharedTreeError as error:
        emit({"ok": False, "error": str(error)})
        return 2
    active = active_run(root, config)
    if active and not args.takeover:
        emit({"ok": False, "error": (
            "another SushiBuild run is active in this project (started %s ago, command %r). Wait for its "
            "summary, run the second build from another workspace (--project), or pass --takeover to end it."
            % (format_duration(time.time() - active["started"]), active.get("command")))})
        return 4
    if active:
        append_log(root, "begin --takeover ends the run started %s" % format_duration(time.time() - active["started"]))
    for name in ("fixups.jsonl", "fixlock.json", "lastlock.json", "locked.orig", "last-result.txt"):
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass
    for name in os.listdir(directory):
        if re.match(r"build-\d+\.(?:log|exit)$", name):
            os.remove(os.path.join(directory, name))
    run = {
        "command": command, "cwd": os.path.abspath(cwd), "started": time.time(), "builds": 0,
        "max_fixes": int(config["max_fixes"]), "max_wall_s": int(config["max_wall_minutes"] * 60),
        "timeout_s": int(config["build_timeout_s"]), "last_log": None,
    }
    write_json(os.path.join(directory, "run.json"), run)
    append_log(root, "begin command=%r cwd=%s" % (command, run["cwd"]))
    emit({"ok": True, "project_root": root, "command": command, "cwd": run["cwd"],
          "max_fixes": run["max_fixes"], "max_wall_minutes": config["max_wall_minutes"],
          "fixer_model": config.get("fixer_model")})
    return 0


def active_run(root, config):
    """The current run.json when a run is active: begun, not yet summarized, and within its wall-time
    cap plus its build timeout (older runs were abandoned). A detached build still running is active."""
    run = read_json(os.path.join(state_dir(root), "run.json"))
    if not run or run.get("finished") or not run.get("started"):
        return None
    detached_pid = int((run.get("detached") or {}).get("pid") or 0)
    if detached_pid > 0 and pid_alive(detached_pid):
        return run
    limit = float(run.get("max_wall_s", config["max_wall_minutes"] * 60)) + float(
        run.get("timeout_s", config["build_timeout_s"]))
    if time.time() - float(run["started"]) > limit:
        return None
    return run


def prepare_build(root, config):
    """Common checks before a build. Returns (run, error_output, exit_code)."""
    directory = state_dir(root, create=True)
    run = read_json(os.path.join(directory, "run.json"))
    if not run:
        return None, {"ok": False, "error": "no run: call begin first"}, 2
    if os.path.exists(os.path.join(directory, "fixlock.json")):
        return None, {"ok": False, "error": "a fixer lock is still held: log the fixup and unlock first"}, 2
    if run.get("detached"):
        return None, {"ok": False, "error": "a detached build is still running: call wait",
                      "detached": run["detached"]}, 2
    caps = caps_state(root, config, run)
    if "wall_time" in " ".join(caps["cap_hit"]):
        return None, {"ok": False, "cap_hit": caps["cap_hit"], "caps": caps, "error": "wall time cap reached: escalate"}, 3
    run["builds"] = int(run.get("builds", 0)) + 1
    return run, None, 0


def finalize_build(root, config, run, log_path, exit_code, timed_out, elapsed):
    """Classify a finished build, record it in run.json, return the run JSON."""
    run["last_log"] = log_path
    write_json(os.path.join(state_dir(root), "run.json"), run)
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        text = ""
    base = run["cwd"]
    if config.get("diagnostic_base"):
        base = os.path.join(root, os.path.expanduser(config["diagnostic_base"]))
    result = classify_text(text, exit_code, base=base, config=config, check_local=True, project_root=root)
    if timed_out:
        result["prelim"] = "big"
        result["reasons"].insert(0, "build timed out after %ss" % run.get("timeout_s"))
    elif exit_code is None:
        result["prelim"] = "big"
        result["reasons"].insert(0, "the build process ended without an exit status (killed?)")
    fixed_signatures = {record.get("signature") for record in read_fixups(root)}
    repeats = []
    for unit in result["units"]:
        for record in unit["errors"]:
            if error_signature(record["file"], record["message"]) in fixed_signatures:
                repeats.append("%s:%d: %s" % (record["file"], record["line"], record["message"]))
    if repeats:
        result["prelim"] = "big"
        result["reasons"].insert(0, "same error came back after a fix: %s" % repeats[0])
    append_log(root, "build #%d exit=%s prelim=%s elapsed=%.1fs" % (run["builds"], exit_code, result["prelim"], elapsed))
    out = {
        "ok": exit_code == 0 and not timed_out, "build": run["builds"], "exit": exit_code,
        "timed_out": timed_out, "elapsed_s": round(elapsed, 1), "log": log_path,
        "repeat_errors": repeats, "caps": caps_state(root, config, run),
    }
    out.update(compact_classification(result))
    return out


def cmd_run(args):
    root = project_root_or_cwd()
    config = load_config(root)
    run, error, code = prepare_build(root, config)
    if error:
        emit(error)
        return code
    directory = state_dir(root)
    log_path = os.path.join(directory, "build-%d.log" % run["builds"])
    with open(log_path, "w", encoding="utf-8", errors="replace") as log_handle:
        log_handle.write("$ %s\n" % run["command"])
    if args.detach:
        return start_detached(root, run, log_path)
    started = time.time()
    timed_out = False
    with open(log_path, "a", encoding="utf-8", errors="replace") as log_handle:
        try:
            process = subprocess.run(
                run["command"], shell=True, cwd=run["cwd"], stdout=log_handle, stdin=subprocess.DEVNULL,
                stderr=subprocess.STDOUT, timeout=int(run.get("timeout_s", config["build_timeout_s"])))
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
    emit(finalize_build(root, config, run, log_path, exit_code, timed_out, time.time() - started))
    return 0


def start_detached(root, run, log_path):
    directory = state_dir(root)
    exit_file = os.path.join(directory, "build-%d.exit" % run["builds"])
    try:
        os.remove(exit_file)
    except OSError:
        pass
    # The wrapper appends the build's output to the log and writes the exit status atomically.
    wrapper = '( %s ) >> %s 2>&1 < /dev/null; rc=$?; printf "%%s" "$rc" > %s.tmp; mv %s.tmp %s' % (
        run["command"], shlex.quote(log_path), shlex.quote(exit_file), shlex.quote(exit_file), shlex.quote(exit_file))
    process = subprocess.Popen(
        ["/bin/sh", "-c", wrapper], cwd=run["cwd"], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    run["detached"] = {"pid": process.pid, "log": log_path, "exit_file": exit_file,
                       "started": time.time(), "build": run["builds"]}
    write_json(os.path.join(directory, "run.json"), run)
    append_log(root, "build #%d detached pid=%d" % (run["builds"], process.pid))
    emit({"ok": True, "detached": True, "build": run["builds"], "pid": process.pid, "log": log_path,
          "next": "call wait (repeat while status is running)"})
    return 0


def pid_alive(pid):
    try:
        os.kill(pid, 0)  # signal 0: existence check only
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a finished child we started may linger as a zombie; reap it if it is ours
        finished, _ = os.waitpid(pid, os.WNOHANG)
        return finished == 0
    except ChildProcessError:
        return True


def tail_lines(path, count):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return [strip_ansi(line.rstrip("\n"))[:200] for line in handle.readlines()[-count:]]
    except OSError:
        return []


def cmd_wait(args):
    root = project_root_or_cwd()
    config = load_config(root)
    directory = state_dir(root)
    run_path = os.path.join(directory, "run.json")
    run = read_json(run_path)
    if not run or not run.get("detached"):
        emit({"ok": False, "error": "no detached build is running (use run --detach first)"})
        return 2
    info = run["detached"]
    max_wait = max(0.0, min(float(args.max), 590.0))
    timeout_s = float(run.get("timeout_s", config["build_timeout_s"]))
    deadline = time.time() + max_wait
    timed_out = False
    exit_code = None
    while True:
        if os.path.isfile(info["exit_file"]):
            try:
                with open(info["exit_file"], encoding="utf-8") as handle:
                    exit_code = int(handle.read().strip() or "-1")
            except (OSError, ValueError):
                exit_code = None
            break
        if not pid_alive(info["pid"]):
            time.sleep(0.2)  # the exit file is written just before the wrapper exits
            if os.path.isfile(info["exit_file"]):
                continue
            break
        if time.time() - info["started"] > timeout_s:
            timed_out = True
            try:
                os.killpg(info["pid"], 15)  # the process group we started with start_new_session
            except OSError:
                pass
            append_log(root, "build #%d timed out after %ss; sent SIGTERM to its process group" % (info["build"], int(timeout_s)))
            break
        if time.time() >= deadline:
            emit({"ok": True, "status": "running", "build": info["build"], "pid": info["pid"],
                  "elapsed_s": int(time.time() - info["started"]), "log": info["log"],
                  "log_tail": tail_lines(info["log"], 5), "caps": caps_state(root, config, run),
                  "next": "call wait again"})
            return 0
        time.sleep(min(2.0, max(0.05, deadline - time.time())))
    elapsed = time.time() - info["started"]
    del run["detached"]
    out = finalize_build(root, config, run, info["log"], exit_code, timed_out, elapsed)
    out = dict([("status", "done")] + list(out.items()))
    emit(out)
    return 0


def cmd_classify(args):
    with open(args.logfile, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    root = find_project_root(os.getcwd())
    config = load_config(root) if root else DEFAULT_CONFIG
    result = classify_text(text, args.exit, base=args.base or os.getcwd(), config=config,
                           check_local=not args.no_local_check)
    emit(compact_classification(result) if args.compact else result)
    return 0


def cmd_lock(args):
    root = project_root_or_cwd()
    config = load_config(root)
    directory = state_dir(root, create=True)
    args.file = map_path(args.file, normalized_path_map(config.get("path_map")))
    if not os.path.isabs(args.file):
        emit({"ok": False, "error": "lock needs an absolute path"})
        return 2
    if not os.path.isfile(args.file):
        emit({"ok": False, "error": "no such file on this machine: %s (check path_map)" % args.file})
        return 2
    if not inside(args.file, root):
        emit({"ok": False, "error": "refusing to lock %s: its real path %s is outside the project root %s" % (
            args.file, os.path.realpath(args.file), os.path.realpath(root))})
        return 2
    shared = sushitree().shared_tree_reason(os.path.realpath(args.file))
    if shared:
        emit({"ok": False, "error": "refusing to lock %s: it is in a shared working tree (%s)" % (args.file, shared)})
        return 2
    caps = caps_state(root, config)
    if caps["cap_hit"]:
        emit({"ok": False, "cap_hit": caps["cap_hit"], "caps": caps, "error": "cap reached: escalate instead of fixing"})
        return 3
    lock_path = os.path.join(directory, "fixlock.json")
    existing = read_json(lock_path)
    if existing:
        emit({"ok": False, "error": "already locked: %s (unlock first)" % existing.get("path")})
        return 2
    shutil.copyfile(args.file, os.path.join(directory, "locked.orig"))
    lock = {
        "path": os.path.normpath(args.file), "realpath": os.path.realpath(args.file),
        "fix": args.fix, "error": args.error, "locked_at": time.time(),
    }
    write_json(lock_path, lock)
    append_log(root, "lock %s fix=%r" % (lock["path"], args.fix))
    emit({"ok": True, "locked": lock["path"], "caps": caps})
    return 0


def cmd_unlock(args):
    root = project_root_or_cwd()
    lock_path = os.path.join(state_dir(root), "fixlock.json")
    existed = os.path.exists(lock_path)
    try:
        # keep the last lock so a fixup can still be logged (and diffed) after unlock
        os.replace(lock_path, os.path.join(state_dir(root), "lastlock.json"))
    except OSError:
        pass
    append_log(root, "unlock (held=%s)" % existed)
    emit({"ok": True, "was_locked": existed})
    return 0


def compute_diff(root, file_path):
    directory = state_dir(root)
    snapshot = os.path.join(directory, "locked.orig")
    lock = read_json(os.path.join(directory, "fixlock.json")) or read_json(os.path.join(directory, "lastlock.json")) or {}
    if not os.path.isfile(snapshot) or not lock:
        return None, None
    if os.path.realpath(file_path) != lock.get("realpath"):
        return None, None
    with open(snapshot, encoding="utf-8", errors="replace") as handle:
        before = handle.readlines()
    try:
        with open(file_path, encoding="utf-8", errors="replace") as handle:
            after = handle.readlines()
    except OSError:
        return None, None
    diff = list(difflib.unified_diff(before, after, "a/" + os.path.basename(file_path), "b/" + os.path.basename(file_path), n=1))
    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    text = "".join(diff)
    if len(text) > 4000:
        text = text[:4000] + "\n... (diff truncated)\n"
    return "+%d -%d" % (added, removed), text


def cmd_log(args):
    root = project_root_or_cwd()
    config = load_config(root)
    directory = state_dir(root, create=True)
    file_path = os.path.abspath(map_path(args.file, normalized_path_map(config.get("path_map"))))
    diff_stat, diff_text = compute_diff(root, file_path)
    signature = error_signature(file_path, args.error)
    previous = read_fixups(root)
    repeat = any(record.get("signature") == signature for record in previous)
    verdict = args.verdict
    if verdict is None:
        verdict = "no-change" if diff_stat == "+0 -0" else "applied"
    record = {
        "n": len(previous) + 1, "time": time.strftime("%Y-%m-%dT%H:%M:%S"), "file": file_path,
        "error": args.error, "fix": args.fix, "diff_stat": diff_stat or args.diff_stat,
        "reported_diff_stat": args.diff_stat, "diff": diff_text, "verdict": verdict,
        "signature": signature, "cause": error_cause(args.error, args.fix), "repeat": repeat,
    }
    with open(os.path.join(directory, "fixups.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    append_log(root, "fixup #%d %s verdict=%s diff=%s repeat=%s" % (record["n"], file_path, verdict, record["diff_stat"], repeat))
    caps = caps_state(root, config)
    emit({"ok": True, "n": record["n"], "diff_stat": record["diff_stat"], "verdict": verdict,
          "repeat": repeat, "caps": caps,
          "next": "escalate" if (repeat or caps["cap_hit"] or verdict != "applied") else "unlock, then run"})
    return 0


def cmd_status(args):
    root = project_root_or_cwd()
    config = load_config(root)
    directory = state_dir(root)
    run = read_json(os.path.join(directory, "run.json"), {}) or {}
    emit({"project_root": root, "run": run, "lock": read_json(os.path.join(directory, "fixlock.json")),
          "caps": caps_state(root, config, run)})
    return 0


def log_excerpt(path, limit):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = [strip_ansi(line.rstrip("\n")) for line in handle]
    except OSError:
        return []
    interesting = re.compile(r"error|undefined|unresolved|required from|instantiation|referenced from|>>>|FAILED|\*\*\*", re.I)
    picked = []
    for number, line in enumerate(lines):
        if interesting.search(line):
            for neighbour in range(max(0, number - 1), min(len(lines), number + 2)):
                if neighbour not in picked:
                    picked.append(neighbour)
        if len(picked) >= limit:
            break
    if not picked:
        return lines[-limit:]
    return [lines[number] for number in sorted(picked)[:limit]]


def render_summary(root, outcome, reason=None, log_path=None, config=None):
    config = config or load_config(root)
    directory = state_dir(root)
    run = read_json(os.path.join(directory, "run.json"), {}) or {}
    fixups = read_fixups(root)
    elapsed = (time.time() - run["started"]) if run.get("started") else 0
    log_path = log_path or run.get("last_log")
    pairs = normalized_path_map(config.get("path_map"))
    out = ["=== SUSHIBUILD RESULT ===", "outcome: %s" % outcome]
    out.append("command: %s" % run.get("command", "?"))
    out.append("builds: %d   fixups: %d   elapsed: %s" % (int(run.get("builds", 0)), len(fixups), format_duration(elapsed)))
    if reason:
        out.append("reason: %s" % reason)
    out.append("fixups:" if fixups else "fixups: none")
    for record in fixups:
        relative = os.path.relpath(record["file"], root) if root else record["file"]
        out.append("  %d. %s [%s, %s]" % (record["n"], relative, record.get("verdict"), record.get("diff_stat") or "diff ?"))
        out.append("     error: %s" % map_text(record.get("error") or "?", pairs))
        if record.get("fix"):
            out.append("     fix:   %s" % record["fix"])
    out.append("fixups file: %s" % os.path.join(directory, "fixups.jsonl"))
    if log_path:
        out.append("log: %s" % log_path)
    if outcome != "success" and log_path:
        out.append("log excerpt:")
        for line in log_excerpt(log_path, int(config["log_excerpt_lines"])):
            out.append("  | " + map_text(line, pairs)[:300])
    out.append("=== END SUSHIBUILD ===")
    return "\n".join(out) + "\n"


def cmd_summary(args):
    root = project_root_or_cwd()
    text = render_summary(root, args.outcome, args.reason, args.log)
    try:
        with open(os.path.join(state_dir(root, create=True), "last-result.txt"), "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        pass
    run_path = os.path.join(state_dir(root), "run.json")
    run = read_json(run_path)
    if run:
        run["finished"] = time.time()
        write_json(run_path, run)
    append_log(root, "summary outcome=%s" % args.outcome)
    sys.stdout.write(text)
    return 0


RESULT_OUTCOMES = ("success", "escalated", "capped", "failed")
# The block's top-level fields, recognized at any indentation (render_summary writes them).
RESULT_KEYS = ("outcome", "command", "reason", "fixups", "fixups file", "log", "log excerpt")
RE_RESULT_START = re.compile(r"^\s*=+\s*SUSHIBUILD\s+RESULT\s*=+\s*$", re.I | re.M)
RE_RESULT_END = re.compile(r"^\s*=+\s*END\s+SUSHIBUILD\s*=+\s*$", re.I | re.M)
RE_RESULT_FIELD = re.compile(r"^\s*(?P<key>[A-Za-z][A-Za-z ]*?)\s*:\s*(?P<value>.*?)\s*$")


def parse_result_block(text):
    """The fields of one return block: (fields, None), or (None, why it is malformed).

    Tolerant of indentation, spacing, case of the markers and the order of the top-level fields;
    strict about what matters: one start and end marker, and an outcome from RESULT_OUTCOMES."""
    starts = list(RE_RESULT_START.finditer(text or ""))
    ends = list(RE_RESULT_END.finditer(text or ""))
    if len(starts) != 1 or len(ends) != 1 or ends[0].start() < starts[0].end():
        return None, "expected exactly one block from === SUSHIBUILD RESULT === to === END SUSHIBUILD ==="
    fields = {}
    fixups = []
    excerpt = []
    for line in text[starts[0].end():ends[0].start()].splitlines():
        if not line.strip():
            continue
        if line.lstrip().startswith("|"):
            excerpt.append(line.lstrip()[1:].strip())
            continue
        match = RE_RESULT_FIELD.match(line)
        if match and match.group("key").strip().lower() in RESULT_KEYS:
            fields[match.group("key").strip().lower()] = match.group("value")
            continue
        if line[:1] in (" ", "\t"):
            if re.match(r"^\s+\d+\.\s", line):
                fixups.append(line.strip())
            continue
        if line.strip().startswith("builds:"):
            for part in re.findall(r"(\w+):\s*(\S+)", line):
                fields[part[0].lower()] = part[1]
            continue
        match = RE_RESULT_FIELD.match(line)
        if match:
            fields[match.group("key").strip().lower()] = match.group("value")
    outcome = (fields.get("outcome") or "").strip().lower()
    if outcome not in RESULT_OUTCOMES:
        return None, "no valid outcome line (one of %s)" % ", ".join(RESULT_OUTCOMES)
    fields["outcome"] = outcome
    fields["fixup_lines"] = fixups
    if excerpt:
        fields["log_excerpt"] = excerpt
    return fields, None


def cmd_result(args):
    root = project_root_or_cwd()
    if args.file == "-":
        text = sys.stdin.read()
        source = "stdin"
    else:
        source = args.file or os.path.join(state_dir(root), "last-result.txt")
        try:
            with open(source, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError as error:
            emit({"ok": False, "source": source, "error": "cannot read the block: %s" % error,
                  "next": "ask the sushi-build agent for its result block again"})
            return 1
    fields, problem = parse_result_block(text)
    if problem:
        emit({"ok": False, "source": source, "error": "malformed result block: %s" % problem,
              "next": "ask the sushi-build agent for its result block again; don't read the outcome from free text"})
        return 1
    emit({"ok": True, "source": source, "fields": fields})
    return 0


# ---------------------------------------------------------------- hook

def agent_matches(agent_type, names):
    if not agent_type:
        return False
    for name in names:
        if agent_type == name or agent_type.endswith(":" + name):
            return True
    return False


def resolve_path(path, cwd):
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.realpath(os.path.normpath(path))


def same_file(candidate, lock):
    locked_real = lock.get("realpath") or os.path.realpath(lock.get("path", ""))
    if candidate == locked_real:
        return True
    try:
        return os.path.exists(candidate) and os.path.exists(locked_real) and os.path.samefile(candidate, locked_real)
    except OSError:
        return False


def is_within(path, directory):
    directory = os.path.realpath(directory)
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


OPERATOR_CHARS = set(";&|()<>")
SEPARATORS = {";", "&", "&&", "|", "||", "|&", "(", ")", "\n"}


def tokenize(command):
    """Shell-ish tokens: quoted strings stay one word; operators become their own tokens."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return command.split()


def command_segments(command):
    """Yield (words, redirect_targets) for each simple command."""
    words, targets = [], []
    tokens = tokenize(command)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token and all(ch in OPERATOR_CHARS for ch in token):
            if ">" in token:
                if index + 1 < len(tokens) and not all(ch in OPERATOR_CHARS for ch in tokens[index + 1]):
                    targets.append(tokens[index + 1])
                    index += 2
                    continue
            elif "<" in token:
                index += 2
                continue
            else:
                if words or targets:
                    yield words, targets
                words, targets = [], []
                index += 1
                continue
            index += 1
            continue
        words.append(token)
        index += 1
    if words or targets:
        yield words, targets


def first_program(words):
    index = 0
    while index < len(words):
        word = words[index]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            index += 1
            continue
        base = os.path.basename(word)
        if base in COMMAND_PREFIXES:
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 2 if words[index] in ("-n",) else 1
            continue
        return base, words[index + 1:]
    return None, []


def is_source_target(target):
    target = target.strip("'\"")
    if not target or target.startswith("/dev/") or "/.sushi/" in target or target.startswith(".sushi/"):
        return False
    base = os.path.basename(target)
    return base == "CMakeLists.txt" or os.path.splitext(base)[1].lower() in SOURCE_EXTENSIONS


GIT_MUTATING = {"checkout", "restore", "reset", "stash", "commit", "push", "apply", "clean", "rebase",
                "merge", "cherry-pick", "revert", "rm", "mv", "am", "switch", "pull"}


def segment_problem(words, targets, depth=0):
    for target in targets:
        if is_source_target(target):
            return "redirect into a source/build file (%s)" % target
    program, rest = first_program(words)
    if program is None:
        return None
    if program == "sushibuild.py" or (program.startswith("python") and rest and rest[0].endswith("sushibuild.py")):
        return None  # its arguments (e.g. begin --command '...') are data, not commands
    if program in ("sed", "perl", "ruby") and any(re.match(r"^-[A-Za-z]*i", w) or w.startswith("--in-place") for w in rest):
        return "in-place edit (%s -i)" % program
    if program == "git" and rest:
        sub = [w for w in rest if not w.startswith("-")][:1]
        if sub and sub[0] in GIT_MUTATING:
            return "git %s changes the tree" % sub[0]
    if program in ("patch", "truncate", "ed", "ex", "vi", "vim", "nano", "emacs"):
        return "%s edits files" % program
    if program in ("tee", "cp", "mv", "rm", "install", "ln", "touch") and any(
            is_source_target(w) for w in rest if not w.startswith("-")):
        return "%s on a source/build file" % program
    if program == "dd" and any(w.startswith("of=") and is_source_target(w[3:]) for w in rest):
        return "dd onto a source/build file"
    if program in BUILD_TOOLS:
        return "runs a build tool directly (%s); build only through sushibuild.py run" % program
    if program in ("ssh", "sh", "bash", "zsh") and depth < 2:
        if program == "ssh":
            remote = [w for w in rest if not w.startswith("-")][1:]
        else:
            remote = rest[rest.index("-c") + 1:rest.index("-c") + 2] if "-c" in rest else []
        for remote_words, remote_targets in command_segments(" ".join(remote)):
            problem = segment_problem(remote_words, remote_targets, depth + 1)
            if problem:
                return problem
    return None


def bash_problem_for_build_agent(command):
    """Best-effort: why this Bash command is not allowed for the triage agent, or None."""
    for words, targets in command_segments(command):
        problem = segment_problem(words, targets)
        if problem:
            return problem
    return None


def hook_decision(payload, env=None):
    """Return the hook output dict, or None to let the call pass untouched."""
    env = os.environ if env is None else env
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    agent_type = payload.get("agent_type")
    agent_id = payload.get("agent_id")
    cwd = payload.get("cwd") or env.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    root = find_project_root(cwd) or env.get("CLAUDE_PROJECT_DIR") or cwd
    config = load_config(root)
    is_fixer = agent_matches(agent_type, config["fixer_agent_types"])
    is_build = agent_matches(agent_type, config["build_agent_types"])
    lock = read_json(os.path.join(root, *STATE_PARTS + ("fixlock.json",)))
    # Fallback for hosts that send no agent_type: while a lock is held, any subagent edit is
    # treated as the fixer's.
    fallback = (not agent_type) and bool(agent_id) and bool(lock)

    if tool == "Read":
        if is_fixer:
            raw = tool_input.get("file_path") or ""
            if lock and raw and same_file(resolve_path(raw, cwd), lock):
                return None
            return deny("SushiBuild: the fixer reads only the locked file%s. If you need another file to "
                        "make the fix, stop and report status not-one-file."
                        % ((" " + lock.get("path")) if lock else ""))
        return None

    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
        if is_fixer or fallback:
            if not lock:
                return deny("SushiBuild: no file is locked for the fixer, so it may not edit anything.")
            if not raw:
                return deny("SushiBuild: the fixer may edit only %s." % lock.get("path"))
            target = resolve_path(raw, cwd)
            if same_file(target, lock):
                return None
            return deny("SushiBuild: the fixer may edit only the locked file %s, not %s. "
                        "If the fix needs another file, stop and report that it is not a one-file fix."
                        % (lock.get("path"), raw))
        if is_build:
            if raw and is_within(resolve_path(raw, cwd), os.path.join(root, *STATE_PARTS)):
                return None
            return deny("SushiBuild: the build triage agent does not edit code. Route small fixes to the "
                        "sushi-fixer agent; it may write only under .sushi/state/sushibuild/.")
        return None

    if tool == "Bash":
        if is_fixer:
            return deny("SushiBuild: the fixer has no shell. Make the one edit with Edit, then report.")
        if is_build:
            problem = bash_problem_for_build_agent(tool_input.get("command") or "")
            if problem:
                return deny("SushiBuild: not allowed for the build triage agent: %s. Use "
                            "`sushibuild.py run` to build and the sushi-fixer agent to change code." % problem)
        return None

    if tool in ("Agent", "Task"):
        if is_fixer:
            return deny("SushiBuild: the fixer does not start agents.")
        if is_build:
            subagent = tool_input.get("subagent_type") or ""
            if not agent_matches(subagent, config["fixer_agent_types"]):
                return deny("SushiBuild: the build triage agent may start only the sushi-fixer agent, not %r." % subagent)
            if tool_input.get("run_in_background"):
                return deny("SushiBuild: run the fixer in the foreground so the build waits for it.")
            if not lock:
                return deny("SushiBuild: lock the file first (`sushibuild.py lock <abs-file> --fix ...`), then call the fixer.")
        return None
    return None


def cmd_hook(args):
    root = None
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        root = find_project_root(payload.get("cwd") or os.getcwd())
        output = hook_decision(payload)
        if output is not None:
            if root:
                append_log(root, "hook deny tool=%s agent=%s: %s" % (
                    payload.get("tool_name"), payload.get("agent_type"),
                    output["hookSpecificOutput"]["permissionDecisionReason"][:200]))
            sys.stdout.write(json.dumps(output))
    except Exception as error:  # a hook must never break the session
        if root:
            append_log(root, "hook error: %r" % (error,))
    return 0


# ---------------------------------------------------------------- main

def main(argv=None):
    parser = argparse.ArgumentParser(prog="sushibuild")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("begin")
    p.add_argument("--command")
    p.add_argument("--cwd")
    p.add_argument("--takeover", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--detach", action="store_true")
    p = sub.add_parser("wait")
    p.add_argument("--max", type=float, default=540)
    p = sub.add_parser("classify")
    p.add_argument("logfile")
    p.add_argument("--exit", type=int)
    p.add_argument("--base")
    p.add_argument("--compact", action="store_true")
    p.add_argument("--no-local-check", action="store_true")
    p = sub.add_parser("lock")
    p.add_argument("file")
    p.add_argument("--fix", required=True)
    p.add_argument("--error")
    sub.add_parser("unlock")
    p = sub.add_parser("log")
    p.add_argument("--file", required=True)
    p.add_argument("--error", required=True)
    p.add_argument("--fix")
    p.add_argument("--diff-stat")
    p.add_argument("--verdict", choices=["applied", "no-change", "failed", "refused"])
    sub.add_parser("status")
    p = sub.add_parser("summary")
    p.add_argument("--outcome", required=True, choices=["success", "escalated", "capped", "failed"])
    p.add_argument("--reason")
    p.add_argument("--log")
    p = sub.add_parser("result")
    p.add_argument("file", nargs="?")
    sub.add_parser("hook")
    global PROJECT_OVERRIDE
    project, rest = sushitree().take_project_option(list(sys.argv[1:] if argv is None else argv))
    PROJECT_OVERRIDE = project or None
    args = parser.parse_args(rest)
    handlers = {
        "begin": cmd_begin, "run": cmd_run, "wait": cmd_wait, "classify": cmd_classify, "lock": cmd_lock,
        "unlock": cmd_unlock, "log": cmd_log, "status": cmd_status, "summary": cmd_summary,
        "result": cmd_result, "hook": cmd_hook,
    }
    if args.cmd not in handlers:
        parser.print_help()
        return 2
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
