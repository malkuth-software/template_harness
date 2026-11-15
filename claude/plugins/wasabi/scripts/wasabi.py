#!/usr/bin/env python3
"""Wasabi: guard hooks for Bash commands.

Subcommands
  hook                     PreToolUse hook on Bash: read hook JSON on stdin and deny, ask or
                           warn according to the guards below. Never crashes; exit 0.
  check [--cwd DIR] CMD    Run the guards on CMD as if Claude ran it in DIR; print the result.
  status [DIR]             Show which guards are active for the repo at DIR and why.
  parse CMD                Print how Wasabi splits CMD into commands (debugging).
  mark-shared [--unmark] [DIR]
                           Mark (or unmark) the repo at DIR as a shared working tree by creating
                           <git-dir>/sushi-shared-tree. Each clone and worktree is marked on its
                           own, and the marker can never be committed.

Guards (each can be switched off in <project>/.sushi/wasabi.json)
  shared_tree   deny stash/switch/clean/commit -a/reset --hard/rebase in a shared working tree
  crlf          deny git add/commit that would stage CRLF line endings in many files
  fetch         ask before commands that download dependencies (clone, curl -o, pip install,
                CMake configure with FetchContent and no offline flag, ...)
  stdin_wait    warn when a command will block forever waiting on stdin

Standard library only; Python 3.9+.
"""

import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD_NAMES = ("shared_tree", "crlf", "fetch", "stdin_wait")

DEFAULT_CONFIG = {
    "guards": {"shared_tree": True, "crlf": True, "fetch": True, "stdin_wait": True},
    "shared_tree": {
        "paths": [],
        "rules": {
            "stash": "deny",
            "switch": "deny",
            "clean": "deny",
            "commit_all": "deny",
            "reset_hard": "deny",
            "rebase": "deny",
            "autostash": "deny",
            "restore": "ask",
            "commit": "off",
            "reset": "off",
            "pull": "off",
        },
        "note": "",
    },
    "crlf": {"max_files": 20, "allow_paths": [], "max_scan_files": 20000},
    "fetch": {"allow_hosts": [], "allow_commands": [], "ask_commands": [], "package_install": False},
    "stdin_wait": {"remote_wrappers": {}, "dedupe_minutes": 30},
}

GIT_DIR_MARKER = "sushi-shared-tree"
LEGACY_MARKER = os.path.join(".sushi", "shared-tree")
GIT_TIMEOUT_S = 10
HOOK_BUDGET_S = 20


# ======================================================================== config

def deep_merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def find_project_root(start):
    directory = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(directory, ".sushi")) or os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def user_config_path():
    return os.path.join(os.path.expanduser("~"), ".sushi", "wasabi.json")


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("top level is not an object")
    return value


def load_config(project_root):
    """Defaults, then ~/.sushi/wasabi.json, then <project>/.sushi/wasabi.json.

    WASABI_CONFIG (a path) replaces both files. Returns (config, sources, errors).
    """
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    sources, errors = [], []
    override = os.environ.get("WASABI_CONFIG")
    if override:
        paths = [override]
    else:
        paths = [user_config_path()]
        if project_root:
            project_path = os.path.join(project_root, ".sushi", "wasabi.json")
            if os.path.abspath(project_path) != os.path.abspath(paths[0]):
                paths.append(project_path)
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            deep_merge(config, read_json(path))
            sources.append(path)
        except (OSError, ValueError) as error:
            errors.append("%s: %s" % (path, error))
    return config, sources, errors


def guard_enabled(config, name):
    return bool((config.get("guards") or {}).get(name, True))


# ======================================================================== parsing

class ParseError(Exception):
    pass


ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
RESERVED_LEADING = {"!", "{", "if", "then", "else", "elif", "do", "while", "until"}
CLOSERS = {"}", "fi", "done", "esac"}
HEADERS = {"for", "select", "case", "function", "[[", "[", "test"}
INPUT_REDIRECTS = {"<", "<>", "<&"}
HEREDOC_REDIRECTS = {"<<", "<<-", "<<<"}


class Command(object):
    """One simple command, after quote removal."""

    def __init__(self):
        self.words = []
        self.assignments = []
        self.redirects = []
        self.heredocs = []
        self.substitutions = []
        self.pipe_in = False
        self.pipe_out = False
        self.background = False
        self.closer = False
        self.header = False

    def is_empty(self):
        return not (self.words or self.assignments or self.redirects or self.closer or self.substitutions)


class Lexer(object):
    """A small POSIX-shell lexer: words, quotes, $( ), backticks, operators, redirects, heredocs.

    It does not expand variables or globs; `$VAR` stays as literal text. Substitutions are
    collected as raw text so the caller can parse them recursively.
    """

    def __init__(self, text):
        self.text = text
        self.n = len(text)
        self.i = 0
        self.commands = []
        self.cur = Command()
        self.word = None
        self.word_start = 0
        self.word_quoted = False
        self.word_subs = []
        self.pending_redirect = None
        self.pending_heredocs = []

    # ---------------------------------------------------------------- words

    def start_word(self):
        if self.word is None:
            self.word = []
            self.word_start = self.i
            self.word_quoted = False
            self.word_subs = []

    def end_word(self):
        if self.word is None:
            return
        text = "".join(self.word)
        raw = self.text[self.word_start:self.i]
        quoted = self.word_quoted
        subs = self.word_subs
        self.word = None
        self.cur.substitutions.extend(subs)
        cur = self.cur
        if self.pending_redirect is not None:
            op = self.pending_redirect
            self.pending_redirect = None
            cur.redirects.append((op, text))
            if op in ("<<", "<<-"):
                self.pending_heredocs.append((text, op == "<<-", cur))
            return
        if not cur.words and not cur.header:
            if ASSIGNMENT.match(raw):
                name, _, value = text.partition("=")
                cur.assignments.append((name.rstrip("+"), value))
                return
            if not quoted and not cur.assignments:
                if text in RESERVED_LEADING:
                    return
                if text in CLOSERS:
                    cur.closer = True
                    return
                if text in HEADERS:
                    cur.header = True
        cur.words.append(text)

    def finish(self, op):
        self.end_word()
        self.pending_redirect = None
        cur = self.cur
        nxt = Command()
        if cur.is_empty():
            nxt.pipe_in = cur.pipe_in or op == "|"
        else:
            if op == "|":
                cur.pipe_out = True
            if op == "&":
                cur.background = True
            self.commands.append(cur)
            nxt.pipe_in = op == "|"
        self.cur = nxt

    # ---------------------------------------------------------------- scanners

    def skip_single(self, j):
        k = self.text.find("'", j)
        if k < 0:
            raise ParseError("unterminated single quote")
        return k + 1

    def skip_backtick(self, j):
        while j < self.n:
            ch = self.text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                return j + 1
            j += 1
        raise ParseError("unterminated backtick")

    def skip_double(self, j):
        while j < self.n:
            ch = self.text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == '"':
                return j + 1
            if ch == "$" and self.text.startswith("(", j + 1):
                _, j = self.read_paren(j + 2)
                continue
            if ch == "`":
                j = self.skip_backtick(j + 1)
                continue
            j += 1
        raise ParseError("unterminated double quote")

    def read_paren(self, start):
        """Return (inner, index after the matching ')') for text starting just inside '('."""
        depth = 1
        j = start
        while j < self.n:
            ch = self.text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "'":
                j = self.skip_single(j + 1)
                continue
            if ch == '"':
                j = self.skip_double(j + 1)
                continue
            if ch == "`":
                j = self.skip_backtick(j + 1)
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return self.text[start:j], j + 1
            j += 1
        raise ParseError("unterminated $(")

    def read_brace(self, start):
        depth = 1
        j = start
        while j < self.n:
            ch = self.text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        raise ParseError("unterminated ${")

    def read_backtick(self, start):
        end = self.skip_backtick(start)
        inner = self.text[start:end - 1]
        inner = re.sub(r"\\([`\\$])", r"\1", inner)
        return inner, end

    def read_dollar(self, in_double):
        """At a '$': append the raw expansion text to the word; record command substitutions."""
        text, i = self.text, self.i
        nxt = text[i + 1] if i + 1 < self.n else ""
        if nxt == "(":
            if text.startswith("((", i + 1):
                _, end = self.read_paren(i + 2)
                self.word.append(text[i:end])
                self.i = end
                return
            inner, end = self.read_paren(i + 2)
            self.word_subs.append(inner)
            self.word.append(text[i:end])
            self.i = end
            return
        if nxt == "{":
            end = self.read_brace(i + 2)
            self.word.append(text[i:end])
            self.i = end
            return
        if nxt == "'" and not in_double:
            self.word_quoted = True
            self.i = i + 2
            self.read_ansi_c()
            return
        if nxt == '"' and not in_double:
            self.i = i + 1
            return
        self.word.append("$")
        self.i = i + 1

    def read_ansi_c(self):
        escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "a": "\a",
                   "b": "\b", "e": "\x1b", "f": "\f", "v": "\v", "0": "\0"}
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == "\\" and self.i + 1 < self.n:
                self.word.append(escapes.get(self.text[self.i + 1], "\\" + self.text[self.i + 1]))
                self.i += 2
                continue
            if ch == "'":
                self.i += 1
                return
            self.word.append(ch)
            self.i += 1
        raise ParseError("unterminated $'")

    def read_double(self):
        self.word_quoted = True
        while self.i < self.n:
            ch = self.text[self.i]
            if ch == '"':
                self.i += 1
                return
            if ch == "\\" and self.i + 1 < self.n:
                nxt = self.text[self.i + 1]
                if nxt in '$`"\\':
                    self.word.append(nxt)
                elif nxt != "\n":
                    self.word.append("\\" + nxt)
                self.i += 2
                continue
            if ch == "$":
                self.read_dollar(in_double=True)
                continue
            if ch == "`":
                inner, end = self.read_backtick(self.i + 1)
                self.word_subs.append(inner)
                self.word.append(self.text[self.i:end])
                self.i = end
                continue
            self.word.append(ch)
            self.i += 1
        raise ParseError("unterminated double quote")

    def read_heredocs(self):
        for delimiter, strip_tabs, command in self.pending_heredocs:
            lines = []
            while self.i < self.n:
                j = self.text.find("\n", self.i)
                line = self.text[self.i:] if j < 0 else self.text[self.i:j]
                self.i = self.n if j < 0 else j + 1
                check = line.lstrip("\t") if strip_tabs else line
                if check == delimiter:
                    break
                lines.append(line)
            command.heredocs.append("\n".join(lines))
        self.pending_heredocs = []

    # ---------------------------------------------------------------- main loop

    def parse(self):
        text = self.text
        while self.i < self.n:
            ch = text[self.i]
            two = text[self.i:self.i + 2]
            if ch in " \t\r":
                self.end_word()
                self.i += 1
            elif ch == "\n":
                self.finish(";")
                self.i += 1
                if self.pending_heredocs:
                    self.read_heredocs()
            elif ch == "#" and self.word is None:
                j = text.find("\n", self.i)
                self.i = self.n if j < 0 else j
            elif ch == "\\":
                if two == "\\\n":
                    self.i += 2
                    continue
                self.start_word()
                self.word_quoted = True
                self.word.append(text[self.i + 1:self.i + 2])
                self.i += 2
            elif ch == "'":
                self.start_word()
                self.word_quoted = True
                end = self.skip_single(self.i + 1)
                self.word.append(text[self.i + 1:end - 1])
                self.i = end
            elif ch == '"':
                self.start_word()
                self.i += 1
                self.read_double()
            elif ch == "`":
                self.start_word()
                inner, end = self.read_backtick(self.i + 1)
                self.word_subs.append(inner)
                self.word.append(text[self.i:end])
                self.i = end
            elif ch == "$":
                self.start_word()
                self.read_dollar(in_double=False)
            elif ch in "<>":
                if two in ("<(", ">("):
                    self.end_word()
                    inner, end = self.read_paren(self.i + 2)
                    self.start_word()
                    self.word_subs.append(inner)
                    self.word.append("/dev/fd/63")
                    self.i = end
                    continue
                if self.word is not None and not self.word_quoted and "".join(self.word).isdigit():
                    self.word = None  # a file descriptor prefix such as 2>
                else:
                    self.end_word()
                three = text[self.i:self.i + 3]
                if three in ("<<<", "<<-"):
                    op = three
                elif two in ("<<", ">>", ">|", "<>", "<&", ">&"):
                    op = two
                else:
                    op = ch
                self.i += len(op)
                self.pending_redirect = op
            elif ch == "&":
                if two == "&&":
                    self.finish("&&")
                    self.i += 2
                elif text.startswith("&>>", self.i):
                    self.end_word()
                    self.pending_redirect = "&>>"
                    self.i += 3
                elif two == "&>":
                    self.end_word()
                    self.pending_redirect = "&>"
                    self.i += 2
                else:
                    self.finish("&")
                    self.i += 1
            elif ch == "|":
                if two == "||":
                    self.finish("||")
                    self.i += 2
                elif two == "|&":
                    self.finish("|")
                    self.i += 2
                else:
                    self.finish("|")
                    self.i += 1
            elif ch == ";":
                self.finish(";")
                self.i += 1
            elif ch in "()":
                self.finish(";")
                self.i += 1
            else:
                self.start_word()
                self.word.append(ch)
                self.i += 1
        self.finish(None)
        if self.pending_heredocs:
            self.read_heredocs()
        return self.commands


INTERESTING = {"git", "curl", "wget", "pip", "pip3", "uv", "npm", "yarn", "pnpm", "brew", "cmake", "ssh",
               "python", "python3", "bash", "sh", "zsh", "cargo", "go", "gem"}


def fallback_parse(text):
    """Conservative split for text the lexer rejects (unbalanced quotes and the like).

    Splits on operators, drops quote characters, and starts an extra command at every word
    that names a command Wasabi cares about, so a guarded command is never missed.
    """
    commands = []
    for segment in re.split(r"&&|\|\||[;|&\n()`]|\$\(", text):
        words = [word.strip("'\"") for word in segment.split()]
        words = [word for word in words if word]
        starts = [0] + [k for k, word in enumerate(words) if k and os.path.basename(word) in INTERESTING]
        for start in starts:
            command = Command()
            rest = words[start:]
            while rest and ASSIGNMENT.match(rest[0]):
                name, _, value = rest.pop(0).partition("=")
                command.assignments.append((name, value))
            command.words = rest
            if not command.is_empty():
                commands.append(command)
    return commands


def parse_script(text):
    """Return (commands, degraded)."""
    try:
        return Lexer(text).parse(), False
    except ParseError:
        return fallback_parse(text), True


# ======================================================================== analysis

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "mksh", "fish"}
WRAPPER_ARG_OPTIONS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "-R"},
    "doas": {"-u", "-C"},
    "nice": {"-n"},
    "nohup": set(),
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "time": set(),
    "gtime": set(),
    "chronic": set(),
    "unbuffer": set(),
    "caffeinate": {"-t", "-w"},
    "stdbuf": {"-i", "-o", "-e"},
    "ionice": {"-c", "-n", "-p", "-P", "-u"},
    "arch": set(),
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "gtimeout": {"-s", "-k", "--signal", "--kill-after"},
    "xargs": {"-I", "-L", "-n", "-P", "-s", "-E", "-d", "-a", "-J", "-R", "-S"},
}
SSH_ARG_LETTERS = set("bcDEeFIiJLlmOopQRSWwB")


class Invocation(object):
    """A command that will run, with wrappers stripped and its context resolved."""

    def __init__(self, argv, cwd, stdin, remote=None):
        self.argv = argv
        self.env = {}
        self.cwd = cwd
        self.stdin = stdin          # open | piped | redirected | heredoc
        self.remote = remote        # host name when the command runs on another machine
        self.bounded = False        # under timeout(1)
        self.env_changed = False    # an earlier command sourced or exported something
        self.redirects = []
        self.heredocs = []
        self.pipe_to = None
        self.degraded = False
        self.wrappers = []
        self.remote_words = None
        self.remote_flags = set()
        self.remote_kind = None

    @property
    def name(self):
        return os.path.basename(self.argv[0]) if self.argv else ""

    def text(self, limit=120):
        joined = " ".join(shlex.quote(word) for word in self.argv)
        return joined if len(joined) <= limit else joined[:limit - 3] + "..."

    def __repr__(self):
        return "Invocation(%r, cwd=%r, stdin=%r, remote=%r)" % (self.argv, self.cwd, self.stdin, self.remote)


def resolve_path(base, value):
    if value is None:
        return None
    path = os.path.expanduser(value)
    if "$" in path:
        path = os.path.expandvars(path)
    if "$" in path or "`" in path:
        return None
    if not os.path.isabs(path):
        if base is None:
            return None
        path = os.path.join(base, path)
    return os.path.normpath(path)


def resolve_cd(base, args):
    target = None
    for arg in args:
        if arg in ("-L", "-P", "-e", "-@", "--"):
            continue
        target = arg
        break
    if target is None:
        return os.path.expanduser("~")
    if target == "-":
        return None
    return resolve_path(base, target)


def strip_options(argv, arg_options):
    """Drop a wrapper's own options; return the index of the wrapped command."""
    k = 1
    while k < len(argv):
        arg = argv[k]
        if arg == "--":
            return k + 1
        if not arg.startswith("-") or arg == "-":
            return k
        k += 2 if arg in arg_options else 1
    return k


def unwrap(inv):
    """Strip sudo/env/nice/timeout/xargs/... in place."""
    for _ in range(12):
        if not inv.argv:
            return
        name = inv.name
        if name == "env":
            argv, k = inv.argv, 1
            while k < len(argv):
                arg = argv[k]
                if arg in ("-i", "-0", "-v", "-", "--ignore-environment", "--null"):
                    k += 1
                elif arg in ("-u", "--unset", "-P"):
                    k += 2
                elif arg in ("-C", "--chdir"):
                    inv.cwd = resolve_path(inv.cwd, argv[k + 1] if k + 1 < len(argv) else None)
                    k += 2
                elif arg.startswith("--chdir="):
                    inv.cwd = resolve_path(inv.cwd, arg.split("=", 1)[1])
                    k += 1
                elif arg in ("-S", "--split-string") and k + 1 < len(argv):
                    try:
                        split = shlex.split(argv[k + 1])
                    except ValueError:
                        split = argv[k + 1].split()
                    argv = argv[:k] + split + argv[k + 2:]
                elif arg == "--":
                    k += 1
                    break
                elif ASSIGNMENT.match(arg):
                    key, _, value = arg.partition("=")
                    inv.env[key] = value
                    k += 1
                elif arg.startswith("-"):
                    k += 1
                else:
                    break
            inv.argv = argv[k:]
            inv.wrappers.append(name)
            continue
        if name not in WRAPPER_ARG_OPTIONS:
            return
        argv = inv.argv
        if name == "command" and any(arg in ("-v", "-V") for arg in argv[1:3]):
            inv.argv = []
            return
        k = strip_options(argv, WRAPPER_ARG_OPTIONS[name])
        if name in ("timeout", "gtimeout"):
            k += 1  # the duration
            inv.bounded = True
        if name == "xargs":
            inv.stdin = "redirected"
            if k >= len(argv):
                inv.argv = ["echo"]
                return
        inv.argv = argv[k:]
        inv.wrappers.append(name)


def parse_shell(argv):
    """Classify a shell invocation: ('c', script) | ('stdin', None) | ('file', path) | ('info', None)."""
    k, has_c, reads_stdin = 1, False, False
    while k < len(argv):
        arg = argv[k]
        if arg == "--":
            k += 1
            break
        if arg in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            k += 2
            continue
        if arg.startswith("--"):
            if arg in ("--version", "--help"):
                return "info", None
            k += 1
            continue
        if len(arg) > 1 and arg[0] in "-+":
            letters = arg[1:]
            has_c = has_c or "c" in letters
            reads_stdin = reads_stdin or "s" in letters
            k += 1
            continue
        break
    rest = argv[k:]
    if has_c:
        return "c", rest[0] if rest else ""
    if reads_stdin or not rest:
        return "stdin", None
    return "file", rest[0]


def parse_ssh(argv):
    """Return (host, remote_words, flags) or None."""
    k, flags = 1, set()
    while k < len(argv):
        arg = argv[k]
        if arg == "--":
            k += 1
            break
        if arg.startswith("-") and len(arg) > 1:
            j = 1
            consumed_next = False
            while j < len(arg):
                letter = arg[j]
                if letter in SSH_ARG_LETTERS:
                    consumed_next = j + 1 == len(arg)
                    break
                flags.add(letter)
                j += 1
            k += 2 if consumed_next else 1
            continue
        break
    if k >= len(argv):
        return None
    return argv[k], argv[k + 1:], flags


def remote_wrapper_words(argv, spec):
    if spec == "--":
        if "--" not in argv:
            return []
        return argv[argv.index("--") + 1:]
    try:
        skip = int(spec)
    except (TypeError, ValueError):
        return []
    return argv[1 + skip:]


def input_state(command):
    for op, _ in command.redirects:
        if op in HEREDOC_REDIRECTS:
            return "heredoc"
    for op, target in command.redirects:
        if op in INPUT_REDIRECTS and target != "-":
            return "redirected"
    return None


class Analyzer(object):
    def __init__(self, remote_wrappers=None, max_depth=6):
        self.remote_wrappers = {name: spec for name, spec in (remote_wrappers or {}).items() if spec is not None}
        self.max_depth = max_depth
        self.degraded = False

    def run(self, text, cwd):
        out = []
        self.walk(text, cwd, "open", None, 0, out, False, False)
        return out

    def walk(self, text, cwd, stdin, remote, depth, out, bounded, env_changed):
        if depth > self.max_depth:
            return env_changed
        commands, degraded = parse_script(text)
        self.degraded = self.degraded or degraded
        loop_fed = any(command.closer and input_state(command) for command in commands)
        state_cwd = cwd
        for index, command in enumerate(commands):
            if command.background:
                own_stdin = "redirected"
            else:
                own_stdin = input_state(command) or ("piped" if command.pipe_in else stdin)
            if loop_fed and own_stdin == "open":
                own_stdin = "redirected"
            for inner in command.substitutions:
                self.walk(inner, state_cwd, stdin, remote, depth + 1, out, bounded, env_changed)
            if command.header or not command.words:
                continue
            inv = Invocation(list(command.words), state_cwd, own_stdin, remote)
            inv.env = dict(command.assignments)
            inv.bounded = bounded
            inv.env_changed = env_changed
            inv.redirects = list(command.redirects)
            inv.heredocs = list(command.heredocs)
            inv.degraded = degraded
            if command.pipe_out and index + 1 < len(commands) and commands[index + 1].words:
                inv.pipe_to = os.path.basename(commands[index + 1].words[0])
            unwrap(inv)
            if not inv.argv:
                continue
            out.append(inv)
            name = inv.name
            if name in ("cd", "pushd") and not command.pipe_in and not command.pipe_out:
                state_cwd = resolve_cd(state_cwd, inv.argv[1:]) if remote is None else None
            elif name in ("source", ".", "export", "eval", "set", "unset", "conda", "nvm", "pyenv"):
                env_changed = True
            self.nest(inv, depth, out)
        return env_changed

    def nest(self, inv, depth, out):
        name = inv.name
        if name in SHELLS:
            mode, script = parse_shell(inv.argv)
            if mode == "c" and script:
                self.walk(script, inv.cwd, inv.stdin, inv.remote, depth + 1, out, inv.bounded, inv.env_changed)
            elif mode == "stdin" and inv.heredocs:
                for body in inv.heredocs:
                    self.walk(body, inv.cwd, "redirected", inv.remote, depth + 1, out, inv.bounded, inv.env_changed)
        elif name == "eval" and len(inv.argv) > 1:
            self.walk(" ".join(inv.argv[1:]), inv.cwd, inv.stdin, inv.remote, depth + 1, out, inv.bounded,
                      inv.env_changed)
        elif name == "ssh":
            parsed = parse_ssh(inv.argv)
            if parsed is None:
                return
            host, words, flags = parsed
            inv.remote_words, inv.remote_flags, inv.remote_kind = words, flags, "ssh"
            if words:
                stdin = "redirected" if flags & set("nfN") else inv.stdin
                self.walk(" ".join(words), None, stdin, host, depth + 1, out, inv.bounded, False)
        elif name in self.remote_wrappers:
            words = remote_wrapper_words(inv.argv, self.remote_wrappers[name])
            inv.remote_words, inv.remote_kind = words, name
            if words:
                self.walk(" ".join(words), None, inv.stdin, name, depth + 1, out, inv.bounded, False)


def analyze(text, cwd, remote_wrappers=None):
    return Analyzer(remote_wrappers).run(text, cwd)


# ======================================================================== git helpers

KNOWN_GIT = set("""add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore
checkout cherry cherry-pick citool clean clone commit commit-tree config count-objects describe diff
diff-files diff-index diff-tree difftool fetch filter-branch for-each-ref format-patch fsck gc grep gui
hash-object help init instaweb log ls-files ls-remote ls-tree maintenance merge merge-base mergetool mv
name-rev notes prune pull push range-diff read-tree rebase reflog remote repack replace request-pull reset
restore rev-list rev-parse revert rm send-email shortlog show show-ref sparse-checkout stash status
submodule switch symbolic-ref tag update-index update-ref var verify-commit verify-tag version whatchanged
worktree write-tree lfs""".split())

# -c settings Wasabi passes through to its own read-only git queries. Anything else (fsmonitor,
# hooksPath, aliases) could run a program before the user has approved the command.
SAFE_GIT_CONFIG = re.compile(r"^(core\.(autocrlf|eol|safecrlf|attributesfile|quotepath)|i18n\.\w+)=", re.I)
SAFE_GIT_ENV = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}


class GitCall(object):
    def __init__(self, argv, cwd):
        self.argv = argv
        self.cwd = cwd           # directory git runs in, after -C
        self.configs = []
        self.git_dir = None
        self.work_tree = None
        self.sub = None
        self.args = []
        self.env = {}

    @property
    def repo_dir(self):
        if self.work_tree:
            return self.work_tree
        if self.git_dir:
            return os.path.dirname(self.git_dir) if os.path.basename(self.git_dir) == ".git" else None
        return self.cwd


def parse_git(inv, deadline=None):
    argv = inv.argv
    call = GitCall(argv, inv.cwd)
    call.env = {key: value for key, value in inv.env.items() if key in SAFE_GIT_ENV}
    k = 1
    while k < len(argv):
        arg = argv[k]
        value = argv[k + 1] if k + 1 < len(argv) else None
        if arg == "-C":
            call.cwd = resolve_path(call.cwd, value)
            k += 2
        elif arg.startswith("-C") and len(arg) > 2:
            call.cwd = resolve_path(call.cwd, arg[2:])
            k += 1
        elif arg == "-c":
            if value is not None:
                call.configs.append(value)
            k += 2
        elif arg in ("--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"):
            if arg == "--git-dir":
                call.git_dir = resolve_path(call.cwd, value)
            elif arg == "--work-tree":
                call.work_tree = resolve_path(call.cwd, value)
            k += 2
        elif arg.startswith("--git-dir="):
            call.git_dir = resolve_path(call.cwd, arg.split("=", 1)[1])
            k += 1
        elif arg.startswith("--work-tree="):
            call.work_tree = resolve_path(call.cwd, arg.split("=", 1)[1])
            k += 1
        elif arg.startswith("-"):
            k += 1
        else:
            break
    if "GIT_WORK_TREE" in call.env:
        call.work_tree = resolve_path(call.cwd, call.env["GIT_WORK_TREE"])
    elif "GIT_DIR" in call.env and not call.git_dir:
        call.git_dir = resolve_path(call.cwd, call.env["GIT_DIR"])
    if k < len(argv):
        call.sub = argv[k]
        call.args = argv[k + 1:]
    if call.sub and call.sub not in KNOWN_GIT:
        expand_alias(call, deadline)
    return call


def expand_alias(call, deadline):
    directory = call.repo_dir
    if not directory or not os.path.isdir(directory):
        return
    result = run_git(["config", "--get", "alias." + call.sub], directory, call, deadline=deadline)
    if result is None or result.returncode != 0:
        return
    text = result.stdout.decode("utf-8", "replace").strip()
    if not text or text.startswith("!"):
        return
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    if words:
        call.sub = words[0]
        call.args = words[1:] + call.args


def run_git(args, cwd, call=None, data=None, deadline=None):
    configs = ["-c", "core.fsmonitor=false"]
    env = dict(os.environ)
    if call is None:
        for key in SAFE_GIT_ENV:
            env.pop(key, None)
    else:
        for setting in call.configs:
            if SAFE_GIT_CONFIG.match(setting):
                configs += ["-c", setting]
        env.update(call.env)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    timeout = GIT_TIMEOUT_S
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0.2:
            return None
    try:
        return subprocess.run(["git", "--no-pager"] + configs + args, cwd=cwd, input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


def repo_root_for(directory):
    if not directory or not os.path.isdir(directory):
        return None
    current = os.path.abspath(directory)
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def git_dir_for(root, deadline=None):
    """The repo's own git directory (per worktree), via `git rev-parse --absolute-git-dir`."""
    result = run_git(["rev-parse", "--absolute-git-dir"], root, deadline=deadline)
    if result is not None and result.returncode == 0:
        path = result.stdout.decode("utf-8", "surrogateescape").strip()
        if path and os.path.isdir(path):
            return path
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return dot
    try:
        with open(dot, encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return None
    if line.startswith("gitdir:"):
        path = resolve_path(root, line[len("gitdir:"):].strip())
        if path and os.path.isdir(path):
            return path
    return None


def legacy_marker_tracked(root):
    """True when .sushi/shared-tree is tracked by git (so every clone would count as shared)."""
    result = run_git(["ls-files", "-z", "--", LEGACY_MARKER], root)
    return result is not None and result.returncode == 0 and bool(result.stdout.strip(b"\0"))


def scan_options(args, short_with_arg="", long_with_arg=(), short_rest=""):
    """Split git-style arguments into (short letters, long names, positionals, after_dashdash, saw_dashdash).

    short_with_arg: letters whose value is the rest of the cluster or the next argument.
    short_rest:     letters whose optional value can only be attached (e.g. commit -S<key>).
    """
    letters, longs, positionals, after = set(), set(), [], []
    saw_dashdash = False
    k = 0
    while k < len(args):
        arg = args[k]
        if arg == "--":
            saw_dashdash = True
            after = args[k + 1:]
            break
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            longs.add(name)
            if "=" not in arg and name in long_with_arg:
                k += 1
        elif arg.startswith("-") and len(arg) > 1:
            j = 1
            while j < len(arg):
                letter = arg[j]
                letters.add(letter)
                if letter in short_with_arg:
                    if j + 1 == len(arg):
                        k += 1
                    break
                if letter in short_rest:
                    break
                j += 1
        else:
            positionals.append(arg)
        k += 1
    return letters, longs, positionals, after, saw_dashdash


# ======================================================================== findings

class Finding(object):
    def __init__(self, guard, action, message):
        self.guard = guard
        self.action = action  # deny | ask | warn
        self.message = message

    def as_dict(self):
        return {"guard": self.guard, "action": self.action, "message": self.message}

    def __repr__(self):
        return "Finding(%s, %s, %r)" % (self.guard, self.action, self.message)


class Context(object):
    def __init__(self, cwd, config, project_root, deadline=None):
        self.cwd = cwd
        self.config = config
        self.project_root = project_root
        self.deadline = deadline if deadline is not None else time.monotonic() + HOOK_BUDGET_S
        self._repo_configs = {}
        self._git_dirs = {}

    def git_dir(self, root):
        if root not in self._git_dirs:
            self._git_dirs[root] = git_dir_for(root, self.deadline)
        return self._git_dirs[root]

    def repo_config(self, root):
        """The shared_tree section from the target repo's own .sushi/wasabi.json, merged over ours."""
        if root not in self._repo_configs:
            merged = json.loads(json.dumps(self.config.get("shared_tree") or {}))
            path = os.path.join(root, ".sushi", "wasabi.json")
            source = None
            own = self.project_root and os.path.abspath(path) == os.path.abspath(
                os.path.join(self.project_root, ".sushi", "wasabi.json"))
            if os.path.isfile(path) and not own and not os.environ.get("WASABI_CONFIG"):
                try:
                    section = read_json(path).get("shared_tree")
                    if isinstance(section, dict):
                        deep_merge(merged, section)
                        source = path
                except (OSError, ValueError):
                    pass
            self._repo_configs[root] = (merged, source)
        return self._repo_configs[root]


def short_path(path):
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


# ======================================================================== guard: shared tree

SHARED_REASONS = {
    "stash": "`git stash` takes other agents' uncommitted work off disk",
    "switch": "switching branches changes the files other agents are editing",
    "restore": "this discards uncommitted edits to those files, which may be another agent's",
    "clean": "`git clean` deletes untracked files, which may be another agent's live work",
    "commit_all": "`commit -a` commits other agents' uncommitted edits",
    "commit": "this tree's rules allow no commits here",
    "reset_hard": "a hard reset discards everyone's uncommitted work",
    "reset": "resetting changes what other agents staged",
    "rebase": "a rebase rewrites the checked-out files under other agents",
    "autostash": "--autostash stashes other agents' uncommitted work",
    "pull": "pulling moves the branch under other agents",
}
SHARED_INSTEAD = {
    "commit_all": "Commit only your own paths: `git commit <path>...`.",
    "restore": "",
}
DEFAULT_INSTEAD = "Do this in your own clone or worktree (`git worktree add ../<name> <branch>`); read and fetch only here."


def shared_status(root, ctx):
    """Return (is_shared, why, rules)."""
    section, source = ctx.repo_config(root)
    rules = dict(DEFAULT_CONFIG["shared_tree"]["rules"])
    rules.update(section.get("rules") or {})
    real = os.path.realpath(root)
    listed_tree = False
    trees = section.get("trees")
    for path, tree in (trees.items() if isinstance(trees, dict) else ()):
        if isinstance(path, str) and isinstance(tree, dict) \
                and os.path.realpath(os.path.expanduser(path)) == real:
            listed_tree = True
            rules.update(tree.get("rules") or {})
            if tree.get("note"):
                section = dict(section, note=tree["note"])
    git_dir = ctx.git_dir(root)
    if git_dir and os.path.exists(os.path.join(git_dir, GIT_DIR_MARKER)):
        shown = os.path.relpath(git_dir, root) if git_dir.startswith(root + os.sep) else short_path(git_dir)
        return True, "marked by %s" % os.path.join(shown, GIT_DIR_MARKER), rules, section
    if os.path.exists(os.path.join(root, LEGACY_MARKER)):
        return True, "marked by .sushi/shared-tree", rules, section
    if listed_tree:
        return True, "listed in shared_tree.trees", rules, section
    for listed in section.get("paths") or []:
        if not isinstance(listed, str):
            continue
        if os.path.realpath(os.path.expanduser(listed)) == real:
            return True, "listed in shared_tree.paths", rules, section
    return False, "", rules, section


def is_rev(call, directory, name, deadline):
    result = run_git(["rev-parse", "--verify", "--quiet", "--end-of-options", name + "^{commit}"],
                     directory, call, deadline=deadline)
    return result is not None and result.returncode == 0


def classify_checkout(call, deadline):
    letters, longs, positionals, after, saw_dashdash = scan_options(
        call.args, short_with_arg="bB", long_with_arg=("--orphan", "--conflict", "--pathspec-from-file"))
    if "b" in letters or "B" in letters or longs & {"--orphan", "--detach"}:
        return "switch"
    if "p" in letters or "--patch" in longs or "--pathspec-from-file" in longs:
        return "restore"
    if saw_dashdash:
        if after:
            return "restore"
        return "switch" if positionals else None
    if not positionals:
        return "restore" if ("f" in letters or "--force" in longs) else None
    if len(positionals) > 1:
        return "restore"
    target = positionals[0]
    if target in (".", ":/", "*"):
        return "restore"
    directory = call.cwd
    if target != "-" and is_rev(call, directory, target, deadline):
        return "switch"
    if directory and (os.path.exists(os.path.join(directory, target)) or any(c in target for c in "*?[")):
        return "restore"
    return "switch"


def classify_shared(call, deadline):
    """Return the shared-tree rule ids this git call falls under."""
    sub, args = call.sub, call.args
    if not sub or (args and args[0] in ("-h", "--help")):
        return []
    if sub == "stash":
        return [] if args and args[0] in ("list", "show") else ["stash"]
    if sub == "switch":
        return ["switch"]
    if sub == "checkout":
        rule = classify_checkout(call, deadline)
        return [rule] if rule else []
    if sub == "restore":
        letters, longs, _, _, _ = scan_options(args, short_with_arg="s", long_with_arg=("--source",))
        staged = "S" in letters or "--staged" in longs
        worktree = "W" in letters or "--worktree" in longs
        return [] if staged and not worktree else ["restore"]
    if sub == "clean":
        letters, longs, _, _, _ = scan_options(args, short_with_arg="e", long_with_arg=("--exclude",))
        return [] if ("n" in letters or "--dry-run" in longs) else ["clean"]
    if sub == "commit":
        letters, longs, _, _, _ = scan_options(
            args, short_with_arg="mFCct", short_rest="Su",
            long_with_arg=("--message", "--file", "--author", "--date", "--reuse-message", "--reedit-message",
                           "--fixup", "--squash", "--template", "--trailer", "--cleanup", "--pathspec-from-file"))
        if "a" in letters or "--all" in longs:
            return ["commit_all"]
        return ["commit"]
    if sub == "reset":
        _, longs, _, _, _ = scan_options(args)
        if longs & {"--hard", "--merge", "--keep"}:
            return ["reset_hard"]
        return ["reset"]
    if sub == "rebase":
        _, longs, _, _, _ = scan_options(args)
        if longs & {"--abort", "--quit", "--show-current-patch"}:
            return []
        return ["autostash", "rebase"] if "--autostash" in longs else ["rebase"]
    if sub == "pull":
        letters, longs, _, _, _ = scan_options(args)
        rules = []
        if "--autostash" in longs:
            rules.append("autostash")
        rebase_long = any(arg.startswith("--rebase") and arg not in ("--rebase=false", "--no-rebase")
                          for arg in args)
        rules.append("rebase" if ("r" in letters or rebase_long) else "pull")
        return rules
    if sub in ("merge", "cherry-pick", "revert", "am"):
        _, longs, _, _, _ = scan_options(args)
        if longs & {"--abort", "--quit", "--skip"}:
            return []
        return ["autostash", "commit"] if "--autostash" in longs else ["commit"]
    return []


def guard_shared_tree(invocations, ctx):
    findings = []
    for inv in invocations:
        if inv.remote or inv.name != "git":
            continue
        call = parse_git(inv, ctx.deadline)
        if not call.sub:
            continue
        root = repo_root_for(call.repo_dir)
        if not root:
            continue
        shared, why, rules, section = shared_status(root, ctx)
        if not shared:
            continue
        rule_ids = classify_shared(call, ctx.deadline)
        chosen = None
        for rule in rule_ids:
            action = rules.get(rule, "off")
            if action == "deny":
                chosen = (rule, "deny")
                break
            if action == "ask" and chosen is None:
                chosen = (rule, "ask")
        if chosen is None:
            continue
        rule, action = chosen
        instead = SHARED_INSTEAD.get(rule, DEFAULT_INSTEAD)
        note = section.get("note") or ""
        message = "Wasabi: %s is a shared working tree (%s); %s. %s%s" % (
            short_path(root), why, SHARED_REASONS[rule], instead, (" " + note) if note else "")
        if action == "ask":
            message = "Wasabi: `%s` in the shared working tree %s: %s. Allow?" % (
                inv.text(80), short_path(root), SHARED_REASONS[rule])
        findings.append(Finding("shared_tree", action, message.strip()))
    return findings


# ======================================================================== guard: CRLF

def ls_files(call, directory, args, deadline):
    result = run_git(["ls-files", "-z", "--full-name"] + args, directory, call, deadline=deadline)
    if result is None or result.returncode != 0:
        return None
    return [path for path in result.stdout.decode("utf-8", "surrogateescape").split("\0") if path]


def blob_crlf(call, root, specs, deadline):
    """Map each spec (`:path`, `HEAD:path`) to True (text with CRLF), False, or None (missing)."""
    answer = {}
    usable = [spec for spec in specs if "\n" not in spec]
    if not usable:
        return answer
    data = ("\n".join(usable) + "\n").encode("utf-8", "surrogateescape")
    result = run_git(["cat-file", "--batch"], root, call, data=data, deadline=deadline)
    if result is None or result.returncode != 0:
        return answer
    out, pos = result.stdout, 0
    for spec in usable:
        end = out.find(b"\n", pos)
        if end < 0:
            break
        header = out[pos:end].decode("utf-8", "replace")
        pos = end + 1
        parts = header.split()
        if len(parts) != 3 or parts[1] in ("missing", "ambiguous"):
            answer[spec] = None
            continue
        size = int(parts[2])
        content = out[pos:pos + size]
        pos += size + 1
        answer[spec] = parts[1] == "blob" and b"\0" not in content[:8000] and b"\r\n" in content
    return answer


def hash_paths(call, root, paths, filters, deadline):
    args = ["hash-object", "--stdin-paths"] + ([] if filters else ["--no-filters"])
    data = ("\n".join(paths) + "\n").encode("utf-8", "surrogateescape")
    result = run_git(args, root, call, data=data, deadline=deadline)
    if result is None or result.returncode != 0:
        return None
    hashes = result.stdout.decode("ascii", "replace").split()
    return hashes if len(hashes) == len(paths) else None


def check_attrs(call, root, paths, deadline):
    """Map path -> {"eol": value, "text": value}."""
    attrs = {}
    if not paths:
        return attrs
    data = "\0".join(paths).encode("utf-8", "surrogateescape") + b"\0"
    result = run_git(["check-attr", "-z", "--stdin", "eol", "text"], root, call, data=data, deadline=deadline)
    if result is None or result.returncode != 0:
        return attrs
    fields = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    for k in range(0, len(fields) - 2, 3):
        attrs.setdefault(fields[k], {})[fields[k + 1]] = fields[k + 2]
    return attrs


def normalizes_by_attr(attr):
    return attr.get("text") in ("set", "auto") or attr.get("eol") in ("lf", "crlf")


def file_has_crlf(path):
    try:
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > 32 * 1024 * 1024:
            return False
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return False
    return b"\0" not in data[:8000] and b"\r\n" in data


def crlf_candidates(call, deadline):
    """Return (worktree_paths, index_paths) the git call would stage, or None when unknown."""
    directory = call.cwd
    if call.sub == "add":
        letters, longs, positionals, after, _ = scan_options(
            call.args, long_with_arg=("--chmod", "--pathspec-from-file"))
        if letters & set("pienN") or longs & {"--patch", "--interactive", "--edit", "--dry-run",
                                                 "--intent-to-add", "--pathspec-from-file"}:
            return None
        update = "u" in letters or "--update" in longs
        everything = "A" in letters or longs & {"--all", "--no-ignore-removal"}
        force = "f" in letters or "--force" in longs
        specs = positionals + after
        if not specs:
            if not (update or everything):
                return [], []
            specs = [":/"]
        args = ["--modified"]
        if not update:
            args += ["--others"] + ([] if force else ["--exclude-standard"])
        paths = ls_files(call, directory, args + ["--"] + specs, deadline)
        return (paths, []) if paths is not None else None
    if call.sub == "commit":
        letters, longs, positionals, after, _ = scan_options(
            call.args, short_with_arg="mFCct", short_rest="Su",
            long_with_arg=("--message", "--file", "--author", "--date", "--reuse-message", "--reedit-message",
                           "--fixup", "--squash", "--template", "--trailer", "--cleanup", "--pathspec-from-file"))
        if letters & {"p"} or longs & {"--patch", "--interactive", "--dry-run"}:
            return None
        specs = positionals + after
        include = "i" in letters or "--include" in longs
        worktree, index = [], []
        if specs:
            paths = ls_files(call, directory, ["--modified", "--"] + specs, deadline)
            if paths is None:
                return None
            worktree += paths
        if not specs or include:
            result = run_git(["diff", "--cached", "--name-only", "-z", "--no-renames", "--diff-filter=AM"],
                             directory, call, deadline=deadline)
            if result is not None and result.returncode == 0:
                index = [p for p in result.stdout.decode("utf-8", "surrogateescape").split("\0") if p]
            if "a" in letters or "--all" in longs:
                paths = ls_files(call, directory, ["--modified", "--", ":/"], deadline)
                if paths is not None:
                    worktree += paths
        return worktree, index
    return None


def crlf_check(call, inv, root, ctx):
    settings = ctx.config.get("crlf") or {}
    max_files = int(settings.get("max_files", 20))
    allow = [pattern for pattern in settings.get("allow_paths") or [] if isinstance(pattern, str)]
    cap = int(settings.get("max_scan_files", 20000))
    deadline = ctx.deadline
    candidates = crlf_candidates(call, deadline)
    if candidates is None:
        return None
    worktree, index = candidates

    def allowed(path):
        return any(fnmatch.fnmatch(path, pattern) for pattern in allow)

    worktree = [p for p in dict.fromkeys(worktree) if not allowed(p) and "\n" not in p][:cap]
    index = [p for p in dict.fromkeys(index) if not allowed(p) and "\n" not in p][:cap]
    introduced, staged_crlf = [], []

    crlf_paths = [p for p in worktree if file_has_crlf(os.path.join(root, p))]
    if crlf_paths:
        raw_mode = inv.env_changed
        if raw_mode:
            attrs = check_attrs(call, root, crlf_paths, deadline)
            stored = [p for p in crlf_paths if not normalizes_by_attr(attrs.get(p, {}))]
        else:
            filtered = hash_paths(call, root, crlf_paths, True, deadline)
            unfiltered = hash_paths(call, root, crlf_paths, False, deadline)
            if filtered is None or unfiltered is None:
                stored = []
            else:
                stored = [p for p, a, b in zip(crlf_paths, filtered, unfiltered) if a == b]
        old = blob_crlf(call, root, [":" + p for p in stored], deadline)
        introduced += [p for p in stored if old.get(":" + p) is not True]
        staged_crlf += stored

    if index:
        new = blob_crlf(call, root, [":" + p for p in index], deadline)
        old = blob_crlf(call, root, ["HEAD:" + p for p in index], deadline)
        for path in index:
            if new.get(":" + path):
                staged_crlf.append(path)
                if old.get("HEAD:" + path) is not True:
                    introduced.append(path)

    introduced = list(dict.fromkeys(introduced))
    attrs = check_attrs(call, root, list(dict.fromkeys(staged_crlf)), deadline)
    pinned = [p for p in dict.fromkeys(staged_crlf) if attrs.get(p, {}).get("eol") == "lf"]
    verb = "`git %s`" % call.sub
    if pinned:
        return Finding("crlf", "deny",
                       "Wasabi: %s would put CRLF line endings into %d file(s) that .gitattributes pins to "
                       "eol=lf (%s). Convert them to LF first, or restage from a fresh shell without the "
                       "toolchain environment." % (verb, len(pinned), ", ".join(pinned[:5])))
    if len(introduced) > max_files:
        return Finding("crlf", "deny",
                       "Wasabi: %s would stage CRLF line endings in %d files that are LF or new in the index "
                       "(e.g. %s). This usually means a shell with a Windows toolchain environment or an "
                       "autocrlf mismatch. Convert them to LF or restage from a fresh shell; if CRLF is "
                       "intended, add the paths to crlf.allow_paths in .sushi/wasabi.json."
                       % (verb, len(introduced), ", ".join(introduced[:5])))
    return None


def guard_crlf(invocations, ctx):
    findings = []
    for inv in invocations:
        if inv.remote or inv.name != "git":
            continue
        call = parse_git(inv, ctx.deadline)
        if call.sub not in ("add", "commit"):
            continue
        if call.cwd is None or not os.path.isdir(call.cwd):
            continue
        root = repo_root_for(call.repo_dir)
        if not root:
            continue
        finding = crlf_check(call, inv, root, ctx)
        if finding:
            findings.append(finding)
    return findings


# ======================================================================== guard: fetch

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?(\[[^\]]+\]|[^/:?#]+)(?::\d+)?([^?#]*)")
SCP = re.compile(r"^(?:[^@/:]+@)?([A-Za-z0-9.-]+):(?!//)(.*)$")
CURL_ARG_LETTERS = set("dHXuAeFTbcKmrwxyYzECDPQUto")
PACKAGE_VERBS = {
    "pip": {"install", "download", "wheel"},
    "pipx": {"install", "inject", "upgrade", "reinstall", "run"},
    "npm": {"install", "i", "ci", "add", "update", "upgrade", "isntall"},
    "yarn": {"add", "install", "upgrade", ""},
    "pnpm": {"add", "install", "i", "update", "up"},
    "bun": {"add", "install", "i"},
    "brew": {"install", "reinstall", "upgrade", "tap", "fetch", "bundle"},
    "cargo": {"install", "fetch"},
    "go": {"get", "install"},
    "gem": {"install"},
    "bundle": {"install", "update"},
    "apt": {"install"},
    "apt-get": {"install"},
    "port": {"install"},
    "conda": {"install", "create"},
    "mamba": {"install", "create"},
    "conan": {"install"},
    "vcpkg": {"install"},
}
OFFLINE_FLAGS = {"--offline", "--no-index", "--frozen-offline"}
FETCH_DECLARE = re.compile(r"^\s*(FetchContent_Declare|FetchContent_MakeAvailable|FetchContent_Populate|"
                           r"ExternalProject_Add|CPMAddPackage)\s*\(\s*([A-Za-z0-9_.+-]*)", re.I | re.M)
TRUE_VALUES = {"on", "true", "1", "yes", "y"}


def url_hosts(words):
    hosts = []
    for word in words:
        match = URL.match(word)
        if match:
            hosts.append((match.group(1).lower(), match.group(2) or ""))
            continue
        match = SCP.match(word)
        if match and "." in match.group(1):
            hosts.append((match.group(1).lower(), "/" + match.group(2)))
    return hosts


def host_allowed(host, path, allow):
    if host in LOCAL_HOSTS:
        return True
    for entry in allow:
        if not isinstance(entry, str) or not entry:
            continue
        entry = re.sub(r"^[a-z]+://", "", entry.lower())
        if "/" in entry:
            if (host + path).lower().startswith(entry):
                return True
        elif host == entry or host.endswith("." + entry):
            return True
    return False


def all_hosts_allowed(words, allow):
    hosts = url_hosts(words)
    return bool(hosts) and all(host_allowed(host, path, allow) for host, path in hosts)


def stdout_to_file(inv):
    for op, target in inv.redirects:
        if op in (">", ">>", ">|", "&>", "&>>") and target not in ("/dev/null", "/dev/stderr", "/dev/stdout"):
            return True
    return False


def piped_to_installer(inv):
    return inv.pipe_to in SHELLS or inv.pipe_to in ("python", "python3", "tar", "sudo", "unzip", "bsdtar")


def curl_writes_file(argv):
    for arg in argv[1:]:
        if arg in ("--output", "--remote-name", "--remote-name-all", "--output-dir", "-o", "-O"):
            return True
        if arg.startswith("--output=") or arg.startswith("--output-dir="):
            return True
        if arg.startswith("-") and not arg.startswith("--"):
            for letter in arg[1:]:
                if letter == "O":
                    return True
                if letter in CURL_ARG_LETTERS:
                    if letter == "o":
                        return True
                    break
    return False


def wget_to_stdout(argv):
    for k, arg in enumerate(argv[1:], 1):
        if arg in ("-O", "--output-document") and k + 1 < len(argv) and argv[k + 1] == "-":
            return True
        if arg in ("--output-document=-", "-O-") or (arg.startswith("-") and not arg.startswith("--")
                                                      and arg.endswith("O-")):
            return True
    return False


def cmake_configure_dirs(argv, cwd):
    """Return (source_dir, build_dir, defines) for a cmake configure run, or None if not a configure."""
    source = build = None
    defines = {}
    positionals = []
    k = 1
    while k < len(argv):
        arg = argv[k]
        value = argv[k + 1] if k + 1 < len(argv) else None
        if arg in ("--build", "--install", "-E", "-P", "--version", "--help", "-h", "--find-package", "-N",
                   "--system-information", "--open"):
            return None
        if arg == "-S":
            source, k = value, k + 2
            continue
        if arg.startswith("-S") and len(arg) > 2:
            source = arg[2:]
        elif arg == "-B":
            build, k = value, k + 2
            continue
        elif arg.startswith("-B") and len(arg) > 2:
            build = arg[2:]
        elif arg == "-D":
            if value:
                key, _, val = value.partition("=")
                defines[key.split(":")[0].upper()] = val
            k += 2
            continue
        elif arg.startswith("-D"):
            key, _, val = arg[2:].partition("=")
            defines[key.split(":")[0].upper()] = val
        elif arg in ("-G", "-T", "-A", "-C", "-U", "--toolchain", "--install-prefix", "--graphviz", "--preset"):
            k += 2
            continue
        elif not arg.startswith("-"):
            positionals.append(arg)
        k += 1
    source = resolve_path(cwd, source) if source else None
    build = resolve_path(cwd, build) if build else None
    for item in positionals:
        path = resolve_path(cwd, item)
        if not path:
            continue
        cache = os.path.join(path, "CMakeCache.txt")
        if os.path.isfile(cache) and source is None:
            build = build or path
            source = cmake_cache_value(cache, "CMAKE_HOME_DIRECTORY")
        elif source is None:
            source = path
    if source is None:
        if build is None:
            return None
        source = cwd
    if build is None:
        build = cwd
    return source, build, defines


def cmake_cache_value(cache, key):
    try:
        with open(cache, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                name, _, value = line.partition("=")
                if name.split(":")[0].strip().upper() == key.upper():
                    return value.strip()
    except OSError:
        pass
    return None


def scan_fetchcontent(source, limit=3000, max_dirs=4000):
    """Return the dependency names declared through FetchContent/ExternalProject/CPM, or [] if none.

    Bounded: at most `limit` CMake files and `max_dirs` directories are visited.
    """
    names, seen, found, dirs_seen = [], 0, False, 0
    skip = {"_deps", "node_modules", "__pycache__"}
    for directory, dirs, files in os.walk(source):
        dirs_seen += 1
        if dirs_seen > max_dirs:
            break
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".") and not d.startswith("build")
                   and not d.startswith("cmake-build")
                   and not os.path.isfile(os.path.join(directory, d, "CMakeCache.txt"))]
        for name in files:
            if name != "CMakeLists.txt" and not name.endswith(".cmake"):
                continue
            seen += 1
            if seen > limit:
                return names if found else []
            try:
                with open(os.path.join(directory, name), encoding="utf-8", errors="replace") as handle:
                    text = handle.read(2 * 1024 * 1024)
            except OSError:
                continue
            for match in FETCH_DECLARE.finditer(text):
                found = True
                if match.group(1).lower() in ("fetchcontent_declare", "cpmaddpackage", "externalproject_add") \
                        and match.group(2):
                    names.append(match.group(2))
    if not found:
        return []
    return list(dict.fromkeys(names)) or ["(unnamed)"]


def cmake_finding(inv):
    if inv.remote or inv.cwd is None:
        return None
    dirs = cmake_configure_dirs(inv.argv, inv.cwd)
    if dirs is None:
        return None
    source, build, defines = dirs
    if not source or not os.path.isfile(os.path.join(source, "CMakeLists.txt")):
        return None
    disconnected = defines.get("FETCHCONTENT_FULLY_DISCONNECTED")
    if disconnected is None:
        disconnected = cmake_cache_value(os.path.join(build, "CMakeCache.txt"), "FETCHCONTENT_FULLY_DISCONNECTED")
    if disconnected is not None and disconnected.strip().lower() in TRUE_VALUES:
        return None
    names = scan_fetchcontent(source)
    if not names:
        return None
    missing = [n for n in names if "FETCHCONTENT_SOURCE_DIR_" + n.upper() not in defines]
    if not missing:
        return None
    return ("Wasabi: this CMake configure may download dependencies from the network: %s uses FetchContent "
            "(%s) and FETCHCONTENT_FULLY_DISCONNECTED is not ON. Allow? (To stay offline: "
            "-DFETCHCONTENT_FULLY_DISCONNECTED=ON with local FETCHCONTENT_SOURCE_DIR_<NAME> paths.)"
            % (short_path(source), ", ".join(missing[:6])))


def package_finding(inv):
    argv, name = inv.argv, inv.name
    if any(arg in OFFLINE_FLAGS for arg in argv):
        return None
    tool, rest = name, argv[1:]
    if re.match(r"^pip[0-9.]*$", name):
        tool = "pip"
    elif re.match(r"^(python|python[23](\.\d+)?)$", name) and len(argv) > 2 and argv[1] == "-m" \
            and re.match(r"^pip[0-9.]*$", argv[2]):
        tool, rest = "pip", argv[3:]
    elif name == "uv":
        words = [w for w in rest if not w.startswith("-")]
        if words[:1] in (["add"], ["sync"]) or words[:2] in (["pip", "install"], ["pip", "sync"],
                                                             ["tool", "install"], ["python", "install"]):
            return "Wasabi: `%s` downloads packages from the network. Allow?" % inv.text(100)
        return None
    if tool not in PACKAGE_VERBS:
        return None
    verbs = [w for w in rest if not w.startswith("-")]
    verb = verbs[0] if verbs else ""
    if tool == "go" and verbs[:2] == ["mod", "download"]:
        verb = "get"
    if verb in PACKAGE_VERBS[tool]:
        return "Wasabi: `%s` downloads packages from the network. Allow?" % inv.text(100)
    return None


def fetch_reason(inv, settings):
    allow_hosts = settings.get("allow_hosts") or []
    argv, name = inv.argv, inv.name
    if name == "git":
        call = parse_git(inv)
        if call.sub == "clone":
            _, _, words, after, _ = scan_options(
                call.args, short_with_arg="bocuj",
                long_with_arg=("--depth", "--branch", "--origin", "--config", "--reference", "--reference-if-able",
                               "--separate-git-dir", "--upload-pack", "--template", "--shallow-since",
                               "--shallow-exclude", "--jobs", "--filter", "--bundle-uri", "--server-option"))
            words = words + after
            if not words:
                return None
            source = words[0]
            if not URL.match(source) and not SCP.match(source):
                return None  # a local path
            if source.startswith("file://") or all_hosts_allowed([source], allow_hosts):
                return None
            return "Wasabi: `%s` downloads a repository from the network. Allow?" % inv.text(100)
        if call.sub == "submodule" and call.args[:1] in (["update"], ["add"]):
            return "Wasabi: `%s` may clone submodules from the network. Allow?" % inv.text(100)
        return None
    if name == "curl":
        if not (curl_writes_file(argv) or stdout_to_file(inv) or piped_to_installer(inv)):
            return None
        if all_hosts_allowed(argv[1:], allow_hosts):
            return None
        return "Wasabi: `%s` downloads a file from the network. Allow?" % inv.text(100)
    if name == "wget":
        if "--spider" in argv:
            return None
        if wget_to_stdout(argv) and not stdout_to_file(inv) and not piped_to_installer(inv):
            return None
        if all_hosts_allowed(argv[1:], allow_hosts):
            return None
        return "Wasabi: `%s` downloads a file from the network. Allow?" % inv.text(100)
    if name == "cmake":
        return cmake_finding(inv)
    if settings.get("package_install") is True:
        return package_finding(inv)
    return None


def matches_any(patterns, text):
    for pattern in patterns:
        try:
            if isinstance(pattern, str) and re.search(pattern, text):
                return True
        except re.error:
            continue
    return False


def guard_fetch(invocations, ctx):
    settings = ctx.config.get("fetch") or {}
    findings = []
    for inv in invocations:
        if not inv.argv:
            continue
        joined = " ".join(inv.argv)
        if matches_any(settings.get("allow_commands") or [], joined):
            continue
        reason = fetch_reason(inv, settings)
        if reason is None and matches_any(settings.get("ask_commands") or [], joined):
            reason = "Wasabi: `%s` matches fetch.ask_commands (may download dependencies). Allow?" % inv.text(100)
        if reason:
            if inv.remote:
                reason = reason.replace("Allow?", "(on %s) Allow?" % inv.remote)
            findings.append(Finding("fetch", "ask", reason))
    return findings


# ======================================================================== guard: stdin wait

PYTHON = re.compile(r"^(python|python[23](\.\d+)?|pypy3?)$")
INTERPRETERS = {
    # name: (flags that carry the program, flags that take a value, flags that just print and exit)
    "python": ({"-c", "-m"}, {"-W", "-X", "-Q"}, {"-V", "-VV", "--version", "-h", "--help"}),
    "node": ({"-e", "--eval", "-p", "--print"}, {"-r", "--require", "--import", "--loader", "--input-type"},
             {"-v", "--version", "-h", "--help"}),
    "ruby": ({"-e"}, {"-r", "-I"}, {"-v", "--version", "-h", "--help"}),
    "perl": ({"-e", "-E"}, {"-I", "-M"}, {"-v", "-V", "--version", "-h"}),
    "php": ({"-r"}, {"-d", "-c"}, {"-v", "--version", "-h", "-i", "-m"}),
    "lua": ({"-e"}, {"-l"}, {"-v"}),
    "osascript": ({"-e"}, {"-l"}, set()),
}
ALWAYS_STDIN = {"irb", "tr", "bc", "dc"}
# filters: (number of non-file arguments the tool needs, options that take a value, options that mean
# "the program came from an option", options that mean "no stdin")
FILTERS = {
    "cat": (0, set(), set(), set()),
    "tac": (0, set(), set(), set()),
    "sort": (0, {"-k", "-t", "-o", "-S", "-T"}, set(), set()),
    "uniq": (0, {"-f", "-s", "-w"}, set(), set()),
    "wc": (0, set(), set(), set()),
    "head": (0, {"-n", "-c"}, set(), set()),
    "tail": (0, {"-n", "-c"}, set(), set()),
    "base64": (0, {"-b", "-w", "-i", "-o"}, set(), set()),
    "grep": (1, {"-A", "-B", "-C", "-m", "-d", "-D", "--include", "--exclude", "--color"}, {"-e", "-f"},
             {"-r", "-R", "--recursive", "-V", "--version", "--help"}),
    "egrep": (1, {"-A", "-B", "-C", "-m"}, {"-e", "-f"}, {"-r", "-R"}),
    "fgrep": (1, {"-A", "-B", "-C", "-m"}, {"-e", "-f"}, {"-r", "-R"}),
    "sed": (1, set(), {"-e", "-f"}, {"--version", "--help"}),
    "awk": (1, {"-F", "-v"}, {"-f"}, {"--version"}),
    "jq": (1, {"-f", "--indent", "--tab"}, set(), {"-n", "--null-input", "--version", "--help", "-h"}),
}


def interpreter_waits(inv):
    name = inv.name
    key = "python" if PYTHON.match(name) else name
    program_flags, value_flags, info_flags = INTERPRETERS[key]
    argv, k = inv.argv, 1
    while k < len(argv):
        arg = argv[k]
        if arg in info_flags:
            return False
        if arg in program_flags:
            return False
        if key == "python" and arg.startswith("-") and not arg.startswith("--") and len(arg) > 2 \
                and ("c" in arg[1:] or "m" in arg[1:]):
            return False
        if arg == "-":
            return True
        if arg in value_flags:
            k += 2
            continue
        if arg.startswith("-"):
            k += 1
            continue
        return False  # a script file
    return True


def filter_waits(inv):
    needed, value_flags, program_flags, no_stdin = FILTERS[inv.name]
    argv, k, positionals = inv.argv, 1, []
    while k < len(argv):
        arg = argv[k]
        if arg in no_stdin:
            return False
        if arg in program_flags:
            needed = 0
            k += 2
            continue
        if inv.name == "jq" and arg in ("--arg", "--argjson", "--slurpfile", "--rawfile"):
            k += 3
            continue
        if arg in value_flags:
            k += 2
            continue
        if arg == "-":
            positionals.append(arg)
            k += 1
            continue
        if arg.startswith("-") and arg != "-":
            if inv.name == "grep" and not arg.startswith("--") and set(arg[1:]) & {"r", "R"}:
                return False
            k += 1
            continue
        positionals.append(arg)
        k += 1
    files = positionals[needed:]
    return not files or files == ["-"]


def stdin_reason(inv):
    """Return a reason string if inv will block reading stdin, else None."""
    name, argv = inv.name, inv.argv
    if PYTHON.match(name) or name in INTERPRETERS:
        if interpreter_waits(inv):
            return "`%s` has no script and nothing on stdin, so it will wait for input forever" % inv.text(60)
        return None
    if name in SHELLS:
        mode, _ = parse_shell(argv)
        if mode == "stdin":
            return "`%s` with no -c string or script reads commands from stdin and will wait forever" % inv.text(60)
        return None
    if name in ALWAYS_STDIN:
        return "`%s` reads stdin, and nothing is piped or redirected into it" % inv.text(60)
    if name in FILTERS:
        if filter_waits(inv):
            return "`%s` has no input file and nothing piped in, so it will wait on stdin" % inv.text(60)
        return None
    if name == "read":
        if any(arg.startswith("-t") or arg.startswith("-u") for arg in argv[1:]):
            return None
        return "`read` here has nothing piped or redirected into it and will wait on stdin"
    if name == "ssh":
        parsed = parse_ssh(argv)
        if parsed is not None and not parsed[1] and not parsed[2] & set("NfnW"):
            return "`%s` has no remote command, so it opens a remote shell that waits on stdin" % inv.text(60)
    return None


def quoting_reason(inv):
    words = inv.remote_words or []
    if len(words) < 2:
        return None
    split = [w for w in words if re.search(r"\s", w)]
    if not split:
        return None
    detail = ""
    for k, word in enumerate(words[:-2]):
        if os.path.basename(word) in SHELLS and words[k + 1].startswith("-") and "c" in words[k + 1][1:] \
                and re.search(r"\s", words[k + 2]):
            first = words[k + 2].split()[0]
            detail = " (here the remote `%s -c` would run only `%s`)" % (word, first)
            break
    return ("`%s` joins its arguments with spaces and the remote shell splits them again, so `%s` will not "
            "arrive as one argument%s. Pass the whole remote command as one quoted string, or drop the "
            "inner `bash -c`" % (inv.remote_kind or inv.name, split[0][:60], detail))


def guard_stdin_wait(invocations, ctx):
    findings = []
    for inv in invocations:
        if inv.remote_words is not None:
            reason = quoting_reason(inv)
            if reason:
                findings.append(Finding("stdin_wait", "warn", "Wasabi: " + reason + "."))
        if inv.stdin != "open" or inv.bounded:
            continue
        reason = stdin_reason(inv)
        if reason:
            where = " on %s (stdin is forwarded through ssh)" % inv.remote if inv.remote else ""
            findings.append(Finding(
                "stdin_wait", "warn",
                "Wasabi: %s%s. Give it a script or -c, a heredoc, a pipe, `< /dev/null`%s, or wrap it in "
                "`timeout`." % (reason, where, " (or ssh -n)" if inv.remote else "")))
    return findings


# ======================================================================== evaluation

GUARDS = (
    ("shared_tree", guard_shared_tree),
    ("crlf", guard_crlf),
    ("fetch", guard_fetch),
    ("stdin_wait", guard_stdin_wait),
)


class Result(object):
    def __init__(self):
        self.findings = []
        self.errors = []
        self.invocations = []
        self.degraded = False
        self.project_root = None

    def decision(self):
        actions = {finding.action for finding in self.findings}
        for action in ("deny", "ask"):
            if action in actions:
                return action
        return "warn" if actions else None


def evaluate(command, cwd, config=None, project_root=None, deadline=None):
    result = Result()
    if project_root is None:
        project_root = find_project_root(cwd) if cwd and os.path.isdir(cwd) else None
    result.project_root = project_root
    if config is None:
        config, _, errors = load_config(project_root)
        result.errors.extend(errors)
    ctx = Context(cwd, config, project_root, deadline)
    wrappers = (config.get("stdin_wait") or {}).get("remote_wrappers") or {}
    analyzer = Analyzer(wrappers if isinstance(wrappers, dict) else {})
    result.invocations = analyzer.run(command, cwd)
    result.degraded = analyzer.degraded
    for name, guard in GUARDS:
        if not guard_enabled(config, name):
            continue
        try:
            result.findings.extend(guard(result.invocations, ctx))
        except Exception as error:  # one broken guard must not take the others down
            result.errors.append("%s: %r" % (name, error))
    seen, unique = set(), []
    for finding in result.findings:
        key = (finding.action, finding.message)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    result.findings = unique
    return result


def build_output(findings):
    denies = [f.message for f in findings if f.action == "deny"]
    asks = [f.message for f in findings if f.action == "ask"]
    warns = [f.message for f in findings if f.action == "warn"]
    output = {"hookEventName": "PreToolUse"}
    if denies:
        output["permissionDecision"] = "deny"
        output["permissionDecisionReason"] = "\n".join(denies)
    elif asks:
        output["permissionDecision"] = "ask"
        output["permissionDecisionReason"] = "\n".join(asks)
        warns = ["Wasabi asked the user to approve this command: " + " ".join(asks)] + warns
    if warns:
        output["additionalContext"] = "\n".join(warns)
    if len(output) == 1:
        return None
    return {"hookSpecificOutput": output}


# ======================================================================== state and logs

def sushi_dir(project_root, *parts):
    directory = os.path.join(project_root or os.path.expanduser("~"), ".sushi", *parts)
    os.makedirs(directory, exist_ok=True)
    return directory


def log(project_root, message):
    try:
        path = os.path.join(sushi_dir(project_root, "logs"), "wasabi.log")
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))
    except OSError:
        pass


def dedupe_warnings(findings, project_root, session_id, minutes):
    """Drop warnings already given in this session within `minutes`."""
    warns = [f for f in findings if f.action == "warn"]
    if not warns or not minutes:
        return findings
    path = os.path.join(sushi_dir(project_root, "state", "wasabi"), "warned.json")
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state, dict):
            state = {}
    except (OSError, ValueError):
        state = {}
    now = time.time()
    state = {key: stamp for key, stamp in state.items()
             if isinstance(stamp, (int, float)) and now - stamp < minutes * 60}
    kept = []
    for finding in findings:
        if finding.action != "warn":
            kept.append(finding)
            continue
        key = hashlib.sha1(("%s\0%s" % (session_id or "", finding.message)).encode("utf-8")).hexdigest()
        if key in state:
            continue
        state[key] = now
        kept.append(finding)
    try:
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(temporary, path)
    except OSError:
        pass
    return kept


# ======================================================================== commands

def command_hook():
    try:
        event = json.loads(sys.stdin.read() or "null")
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return 0
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return 0
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else os.getcwd()
    result = evaluate(command, cwd)
    for error in result.errors:
        log(result.project_root, "error: %s" % error)
    minutes = 0
    try:
        config, _, _ = load_config(result.project_root)
        minutes = float((config.get("stdin_wait") or {}).get("dedupe_minutes", 30))
    except Exception:
        minutes = 30
    findings = dedupe_warnings(result.findings, result.project_root, event.get("session_id"), minutes)
    output = build_output(findings)
    if output is None:
        return 0
    for finding in findings:
        log(result.project_root, "%s %s: %s | %s" % (finding.action, finding.guard, command[:200].replace("\n", " "),
                                                     finding.message))
    print(json.dumps(output))
    return 0


def command_check(arguments):
    cwd = os.getcwd()
    if arguments[:1] == ["--cwd"] and len(arguments) > 1:
        cwd = os.path.abspath(arguments[1])
        arguments = arguments[2:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        print("usage: wasabi.py check [--cwd DIR] COMMAND", file=sys.stderr)
        return 2
    command = " ".join(arguments)
    result = evaluate(command, cwd)
    print("decision: %s" % (result.decision() or "none"))
    for finding in result.findings:
        print("- [%s/%s] %s" % (finding.guard, finding.action, finding.message))
    for error in result.errors:
        print("! %s" % error)
    if result.degraded:
        print("(the command did not parse cleanly; Wasabi used its conservative fallback)")
    return 0


def command_parse(arguments):
    cwd = os.getcwd()
    config, _, _ = load_config(find_project_root(cwd))
    wrappers = (config.get("stdin_wait") or {}).get("remote_wrappers") or {}
    for inv in analyze(" ".join(arguments), cwd, wrappers if isinstance(wrappers, dict) else {}):
        print(json.dumps({"argv": inv.argv, "cwd": inv.cwd, "stdin": inv.stdin, "remote": inv.remote,
                          "env": inv.env, "wrappers": inv.wrappers}))
    return 0


def command_mark_shared(arguments):
    unmark = "--unmark" in arguments
    rest = [argument for argument in arguments if argument != "--unmark"]
    directory = os.path.abspath(rest[0]) if rest else os.getcwd()
    root = repo_root_for(directory)
    git_dir = git_dir_for(root) if root else None
    if not git_dir:
        print("wasabi: %s is not inside a git repository" % short_path(directory), file=sys.stderr)
        return 1
    marker = os.path.join(git_dir, GIT_DIR_MARKER)
    if unmark:
        if os.path.exists(marker):
            os.remove(marker)
            print("unmarked %s (removed %s)" % (short_path(root), short_path(marker)))
        else:
            print("%s was not marked in its git dir" % short_path(root))
        if os.path.exists(os.path.join(root, LEGACY_MARKER)):
            print("note: %s still exists and keeps the tree shared" % os.path.join(short_path(root), LEGACY_MARKER))
        return 0
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write("Marked as a shared working tree by wasabi mark-shared, %s.\n"
                     "Remove with: wasabi.py mark-shared --unmark\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("marked %s as a shared working tree (%s)" % (short_path(root), short_path(marker)))
    return 0


def describe_rules(rules):
    groups = {}
    for rule, action in sorted(rules.items()):
        groups.setdefault(action, []).append(rule)
    return "; ".join("%s: %s" % (action, ", ".join(names)) for action, names in sorted(groups.items()))


def command_status(arguments):
    directory = os.path.abspath(arguments[0]) if arguments else os.getcwd()
    project_root = find_project_root(directory)
    config, sources, errors = load_config(project_root)
    ctx = Context(directory, config, project_root)
    lines = ["Wasabi status for %s" % short_path(directory)]
    lines.append("project root: %s" % (short_path(project_root) if project_root else "(none)"))
    lines.append("config: %s" % (", ".join(short_path(s) for s in sources) if sources else "(defaults only)"))
    for error in errors:
        lines.append("config error (ignored): %s" % error)
    root = repo_root_for(directory)
    for name in GUARD_NAMES:
        enabled = guard_enabled(config, name)
        lines.append("")
        lines.append("[%s] %s" % (name, "on" if enabled else "off"))
        if not enabled:
            continue
        if name == "shared_tree":
            if not root:
                lines.append("  not in a git repository")
                continue
            shared, why, rules, section = shared_status(root, ctx)
            if shared:
                lines.append("  %s is SHARED (%s)" % (short_path(root), why))
                lines.append("  rules: %s" % describe_rules(rules))
            else:
                lines.append("  %s is not shared (no %s marker in its git dir, no .sushi/shared-tree, not in "
                             "shared_tree.paths); mark it with `wasabi.py mark-shared`"
                             % (short_path(root), GIT_DIR_MARKER))
            if os.path.exists(os.path.join(root, LEGACY_MARKER)) and legacy_marker_tracked(root):
                lines.append("  WARNING: .sushi/shared-tree is tracked by git, so every clone of this repo "
                             "counts as shared. Prefer `wasabi.py mark-shared` (a marker in the git dir) and "
                             "stop tracking the file.")
            listed = section.get("paths") or []
            if listed:
                lines.append("  shared_tree.paths: %s" % ", ".join(listed))
        elif name == "crlf":
            settings = config.get("crlf") or {}
            lines.append("  deny git add/commit introducing CRLF in more than %s files, or any eol=lf file"
                         % settings.get("max_files", 20))
            if settings.get("allow_paths"):
                lines.append("  allow_paths: %s" % ", ".join(settings["allow_paths"]))
            if root:
                staged = evaluate("git commit", directory, config=config, project_root=project_root)
                hits = [f for f in staged.findings if f.guard == "crlf"]
                lines.append("  index now: %s" % (hits[0].message if hits else "no CRLF problem staged"))
        elif name == "fetch":
            settings = config.get("fetch") or {}
            lines.append("  ask before: git clone, git submodule update/add, curl/wget to a file or a shell, "
                         "CMake configure with FetchContent and no FETCHCONTENT_FULLY_DISCONNECTED")
            lines.append("  package installs (pip/uv/npm/yarn/pnpm/bun/brew/cargo/go/gem/apt): %s"
                         % ("ask (fetch.package_install)" if settings.get("package_install") is True
                            else "not asked (fetch.package_install is off)"))
            for key in ("allow_hosts", "allow_commands", "ask_commands"):
                if settings.get(key):
                    lines.append("  %s: %s" % (key, ", ".join(settings[key])))
            if os.path.isfile(os.path.join(directory, "CMakeLists.txt")):
                names = scan_fetchcontent(directory)
                lines.append("  CMakeLists here: %s" % ("FetchContent for " + ", ".join(names) if names
                                                         else "no FetchContent"))
        elif name == "stdin_wait":
            settings = config.get("stdin_wait") or {}
            wrappers = settings.get("remote_wrappers") or {}
            lines.append("  warn (never block) on commands that will wait on stdin")
            lines.append("  remote wrappers treated like ssh: %s" % (", ".join(sorted(wrappers)) or "(none)"))
    print("\n".join(lines))
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    command = sys.argv[1]
    if command == "hook":
        try:
            return command_hook()
        except Exception as error:  # a hook must never break the session
            try:
                log(None, "hook error: %r" % (error,))
            except Exception:
                pass
            return 0
    if command == "check":
        return command_check(sys.argv[2:])
    if command == "status":
        return command_status(sys.argv[2:])
    if command == "parse":
        return command_parse(sys.argv[2:])
    if command == "mark-shared":
        return command_mark_shared(sys.argv[2:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
