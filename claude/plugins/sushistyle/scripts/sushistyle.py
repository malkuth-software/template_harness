#!/usr/bin/env python3
"""SushiStyle: house rules as clang AST matchers, checked on a build host.

Subcommands
  hook                 PostToolUse hook: read hook JSON on stdin, add a short note to the context.
  check FILE... [--gate] [--all]
                       Check files now. --gate exits 1 on error-level findings.
                       --all reports findings already reminded about.
  test-rules           Run every rule against its fixtures on the build host.

Tiers
  regex tier   local, cheap, for rules marked "tier": "regex".
  clang tier   clang-query on the configured remote build host. It never runs on
               the machine hosting Claude unless "allow_local_clang" is true.

Config: ~/.sushi/sushistyle.json overlaid by <project>/.sushi/sushistyle.json (the project wins;
$SUSHISTYLE_CONFIG replaces both; see plugin README). Standard library only.
"""

import fnmatch
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CXX_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl", ".mm"}
MAX_REMINDERS = 8
ALLOW_COMMENT = re.compile(r"sushi-allow:\s*([\w\-]+(?:\s*,\s*[\w\-]+)*)")

DEFAULT_CONFIG = {
    "rule_packs": ["house"],
    "remote": {
        "host": None,
        "clang_query": "/opt/homebrew/opt/llvm/bin/clang-query",
        "args": ["-std=c++23"],
        "include_dirs": [],
        "path_map": [],
        "max_load_fraction": 0.7,
        "slots": 1,
        "timeout_s": 25,
        "nice": 15,
    },
    "allow_local_clang": False,
    "allow": [],
}


# ---------------------------------------------------------------- config

def find_project_root(start_path):
    directory = os.path.dirname(os.path.abspath(start_path)) if os.path.isfile(start_path) else os.path.abspath(start_path)
    while True:
        if os.path.isdir(os.path.join(directory, ".sushi")) or os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def config_paths(project_root):
    """The config layers, later ones winning: ~/.sushi/sushistyle.json (per machine), then
    <project>/.sushi/sushistyle.json. $SUSHISTYLE_CONFIG replaces both."""
    explicit = os.environ.get("SUSHISTYLE_CONFIG")
    if explicit is not None:
        return [explicit]
    paths = [os.path.join(os.path.expanduser("~"), ".sushi", "sushistyle.json")]
    if project_root is not None:
        paths.append(os.path.join(project_root, ".sushi", "sushistyle.json"))
    return paths


def load_config(project_root):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for path in config_paths(project_root):
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                layer = json.load(handle)
            remote_layer = layer.pop("remote", {})
            config.update(layer)
            config["remote"].update(remote_layer)
    return config


def is_pack_path(pack):
    """A rule-pack entry naming a file (a path, or a name ending in .json) rather than a pack name."""
    return pack.endswith(".json") or "/" in pack or os.sep in pack or os.path.isabs(pack)


def pack_candidates(pack, project_root):
    """Where a rule-pack entry may live, first match wins. A path is taken relative to the project root
    (or as given when absolute, ~ expanded); a name is <project>/.sushi/rules/<name>.json, then the
    plugin's own rules/<name>.json."""
    if is_pack_path(pack):
        path = os.path.expanduser(pack)
        if not os.path.isabs(path):
            path = os.path.join(project_root or os.getcwd(), path)
        return [os.path.normpath(path)]
    candidates = [os.path.join(PLUGIN_ROOT, "rules", pack + ".json")]
    if project_root is not None:
        candidates.insert(0, os.path.join(project_root, ".sushi", "rules", pack + ".json"))
    return candidates


def load_rules(config, project_root):
    rules = []
    for pack in config["rule_packs"]:
        candidates = pack_candidates(pack, project_root)
        for candidate in candidates:
            if os.path.isfile(candidate):
                with open(candidate, encoding="utf-8") as handle:
                    pack_data = json.load(handle)
                base = os.path.dirname(candidate)
                enabled_override = config.get("enable_rules", [])
                disabled_override = config.get("disable_rules", [])
                for rule in pack_data["rules"]:
                    rule["_base"] = base
                    enabled = rule.get("enabled", True)
                    if rule["id"] in enabled_override:
                        enabled = True
                    if rule["id"] in disabled_override:
                        enabled = False
                    if enabled or os.environ.get("SUSHISTYLE_ALL_RULES") == "1":
                        rules.append(rule)
                break
    return rules


# ---------------------------------------------------------------- findings

class Finding:
    def __init__(self, rule, line, column, text):
        self.rule = rule
        self.line = line
        self.column = column
        self.text = text
        self.unverified = False

    def key(self, path):
        digest = hashlib.sha1(self.text.strip().encode("utf-8")).hexdigest()[:12]
        return "%s|%s|%s" % (path, self.rule["id"], digest)


def is_allowed(finding, path, lines, config, project_root):
    for index in (finding.line - 1, finding.line - 2):
        if 0 <= index < len(lines):
            if index == finding.line - 2 and not lines[index].lstrip().startswith("//"):
                continue  # the line above counts only when it is a comment of its own
            match = ALLOW_COMMENT.search(lines[index])
            if match is not None:
                ids = [part.strip() for part in match.group(1).split(",")]
                if finding.rule["id"] in ids or "all" in ids:
                    return True
    relative = os.path.relpath(path, project_root) if project_root else path
    for entry in config.get("allow", []):
        if entry.get("rule") not in (finding.rule["id"], "*"):
            continue
        if fnmatch.fnmatch(relative, entry.get("files", "*")):
            pattern = entry.get("text")
            if pattern is None or re.search(pattern, finding.text):
                return True
    return False


# ---------------------------------------------------------------- regex tier

def run_regex_tier(rules, lines):
    findings = []
    for rule in rules:
        if rule.get("tier") != "regex":
            continue
        compiled = re.compile(rule["regex"])
        for number, text in enumerate(lines, start=1):
            stripped = text.lstrip()
            if stripped.startswith("//"):
                continue
            match = compiled.search(text)
            if match is not None:
                findings.append(Finding(rule, number, match.start() + 1, text))
    return findings


# ---------------------------------------------------------------- clang tier

def map_remote_path(local_path, path_map):
    for local_prefix, remote_prefix in path_map:
        if local_path.startswith(local_prefix):
            return remote_prefix + local_path[len(local_prefix):]
    return None


def build_payload(source_text, file_name, rules, config, project_root, gate, source_path=None):
    remote = config["remote"]
    language = "objective-c++" if file_name.endswith(".mm") else "c++"
    include_flags = []
    if source_path is not None:
        # The file is parsed from a temp dir, so point quoted includes at its real directory
        # on the host; otherwise `#include "../x.hpp"` fails and the parse is partial.
        own_dir = map_remote_path(os.path.dirname(os.path.abspath(source_path)), remote["path_map"])
        if own_dir is not None:
            include_flags.append("-iquote" + own_dir)
    for include_dir in remote["include_dirs"]:
        local_dir = include_dir if os.path.isabs(include_dir) else os.path.join(project_root or "", include_dir)
        mapped = map_remote_path(os.path.abspath(local_dir), remote["path_map"])
        if mapped is not None:
            include_flags.append("-I" + mapped)
    query_lines = ["set output diag", "set bind-root false"]
    for rule in rules:
        if rule.get("tier", "clang") == "clang":
            query_lines.append('match %s.bind("%s")' % (main_file_only(rule["matcher"]), rule["id"]))
    source_delimiter = "__SUSHI_SRC_%s__" % secrets.token_hex(8)
    query_delimiter = "__SUSHI_Q_%s__" % secrets.token_hex(8)
    diag_flags = sorted({rule["diag_flag"] for rule in rules if rule.get("tier") == "clang-diag" and rule.get("diag_flag")})
    args = " ".join(shell_quote(arg) for arg in remote["args"] + diag_flags + include_flags)
    wait_seconds = 30 if gate else 3
    script = """
set -u
max_fraction=%(max_fraction)s
ncpu=$(sysctl -n hw.ncpu)
load=$(sysctl -n vm.loadavg | awk '{print $2}')
if [ "$(echo "$load > $ncpu * $max_fraction" | bc)" = "1" ]; then echo "SUSHI_SKIP busy load=$load ncpu=$ncpu"; exit 0; fi
slot=""
deadline=$(( $(date +%%s) + %(wait)d ))
while [ -z "$slot" ]; do
  i=0
  while [ $i -lt %(slots)d ]; do
    lock=/tmp/sushistyle.slot$i
    if mkdir "$lock" 2>/dev/null; then echo $$ > "$lock/pid"; slot=$lock; break; fi
    owner=$(cat "$lock/pid" 2>/dev/null)
    if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then rm -rf "$lock"; fi
    i=$((i+1))
  done
  [ -n "$slot" ] && break
  [ $(date +%%s) -ge $deadline ] && { echo "SUSHI_SKIP slots"; exit 0; }
  sleep 1
done
work=$(mktemp -d /tmp/sushistyle.XXXXXX)
trap 'rm -rf "$work" "$slot"' EXIT
cat > "$work/%(name)s" <<'%(sd)s'
%(source)s
%(sd)s
cat > "$work/q.txt" <<'%(qd)s'
%(query)s
%(qd)s
sdk=$(xcrun --show-sdk-path 2>/dev/null)
sysroot=""
[ -n "$sdk" ] && sysroot="-isysroot $sdk"
nice -n %(nice)d %(clang_query)s -f "$work/q.txt" "$work/%(name)s" -- -x %(lang)s %(args)s $sysroot 2>&1
echo "SUSHI_DONE"
""" % {
        "max_fraction": remote["max_load_fraction"],
        "wait": wait_seconds,
        "slots": int(remote["slots"]),
        "name": file_name,
        "sd": source_delimiter,
        "source": source_text,
        "qd": query_delimiter,
        "query": "\n".join(query_lines),
        "nice": int(remote["nice"]),
        "clang_query": shell_quote(remote["clang_query"]),
        "lang": language,
        "args": args,
    }
    return script


def main_file_only(matcher):
    """Restrict the outermost node matcher to the checked file, so headers are not matched.

    `lambdaExpr()` becomes `lambdaExpr(isExpansionInMainFile())`; `varDecl(a, b)` becomes
    `varDecl(isExpansionInMainFile(), a, b)`. isExpansionInMainFile is polymorphic over
    Decl, Stmt and TypeLoc, which covers every top-level matcher kind clang-query accepts here.
    """
    opening = matcher.find("(")
    if opening < 0:
        return matcher
    inner = matcher[opening + 1:].lstrip()
    separator = "" if inner.startswith(")") else ", "
    return matcher[:opening + 1] + "isExpansionInMainFile()" + separator + matcher[opening + 1:]


def shell_quote(text):
    if re.fullmatch(r"[\w@%+=:,./\-]+", text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


def run_clang_tier(path, source_text, rules, config, project_root, gate):
    """Returns (findings, status). status is 'ok', 'skipped: ...', or 'error: ...'."""
    clang_rules = [rule for rule in rules if rule.get("tier", "clang") in ("clang", "clang-diag")]
    if not clang_rules:
        return [], "ok"
    remote = config["remote"]
    host = remote.get("host")
    if not host:
        return [], "skipped: no remote host configured"
    if host in ("localhost", "127.0.0.1") and not config.get("allow_local_clang"):
        return [], "skipped: local clang disabled"
    file_name = "unit" + os.path.splitext(path)[1]
    payload = build_payload(source_text, file_name, clang_rules, config, project_root, gate, source_path=path)
    command = ["ssh", "-o", "ConnectTimeout=6", "-o", "BatchMode=yes", host, "sh -s"]
    try:
        completed = subprocess.run(command, input=payload, capture_output=True, text=True,
                                   timeout=float(remote["timeout_s"]))
    except subprocess.TimeoutExpired:
        return [], "skipped: timeout"
    output = completed.stdout + completed.stderr
    if "SUSHI_SKIP" in output:
        reason = output.split("SUSHI_SKIP", 1)[1].strip().splitlines()[0]
        return [], "skipped: " + reason
    if "Matcher not found" in output or re.search(r"^\d+:\d+: ", output, re.M):
        return [], "error: clang-query rejected a matcher: " + output.strip().splitlines()[0][:300]
    if "SUSHI_DONE" not in output:
        return [], "error: " + output.strip()[-400:]
    error_lines = re.findall(r"/" + re.escape(file_name) + r":\d+:\d+: (?:fatal )?error: ([^\n]*)", output)
    if error_lines:
        # A partial parse: matches still count, but say so (the hook logs it, check prints it).
        partial = "partial parse (%d compile errors; first: %s)" % (len(error_lines), error_lines[0][:120])
    else:
        partial = None
    by_id = {rule["id"]: rule for rule in clang_rules}
    pattern = re.compile(r"/" + re.escape(file_name) + r':(\d+):(\d+): note: "([^"]+)" binds here')
    lines = source_text.splitlines()
    findings = []
    seen = set()
    for match in pattern.finditer(output):
        line, column, rule_id = int(match.group(1)), int(match.group(2)), match.group(3)
        rule = by_id.get(rule_id)
        if rule is None or (rule_id, line, column) in seen:
            continue
        seen.add((rule_id, line, column))
        text = lines[line - 1] if 0 < line <= len(lines) else ""
        findings.append(Finding(rule, line, column, text))
    diag_pattern = re.compile(r"/" + re.escape(file_name) + r":(\d+):(\d+): warning: ([^\n]*)")
    for match in diag_pattern.finditer(output):
        line, column, message = int(match.group(1)), int(match.group(2)), match.group(3)
        for rule in clang_rules:
            if rule.get("tier") == "clang-diag" and re.search(rule["diag_regex"], message):
                if (rule["id"], line, column) in seen:
                    continue
                seen.add((rule["id"], line, column))
                text = lines[line - 1] if 0 < line <= len(lines) else ""
                findings.append(Finding(rule, line, column, text))
    return findings, ("ok" if partial is None else "ok; " + partial)


# ---------------------------------------------------------------- state

def state_path(project_root):
    directory = os.path.join(project_root or os.path.expanduser("~"), ".sushi", "state")
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "sushistyle_reminded.json")


def load_state(project_root):
    path = state_path(project_root)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except ValueError:
            return {}
    return {}


def save_state(project_root, state):
    path = state_path(project_root)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    os.replace(temporary, path)


def log(project_root, message):
    directory = os.path.join(project_root or os.path.expanduser("~"), ".sushi", "logs")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "sushistyle.log"), "a", encoding="utf-8") as handle:
        handle.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))


# ---------------------------------------------------------------- check

def check_file(path, config, project_root, rules, gate, show_unverified=False):
    with open(path, encoding="utf-8", errors="replace") as handle:
        source_text = handle.read()
    lines = source_text.splitlines()
    findings = run_regex_tier(rules, lines)
    clang_findings, status = run_clang_tier(path, source_text, rules, config, project_root, gate)
    if "partial parse" in status:
        # Unresolved headers turn initializers and member references into error-recovery
        # nodes, which semantic rules misread. Keep clang findings out of reminders; `check`
        # shows them marked unverified.
        for finding in clang_findings:
            finding.unverified = True
        if not show_unverified:
            clang_findings = []
    findings.extend(clang_findings)
    findings = [finding for finding in findings if not is_allowed(finding, path, lines, config, project_root)]
    findings.sort(key=lambda finding: (finding.line, finding.column))
    return findings, status


def format_finding(path, finding):
    rule = finding.rule
    note = " (unverified: partial parse)" if finding.unverified else ""
    return "%s:%d:%d %s [%s] %s%s" % (path, finding.line, finding.column, rule["severity"], rule["id"], rule["message"], note)


def command_check(arguments):
    gate = "--gate" in arguments
    show_all = "--all" in arguments or gate
    paths = [argument for argument in arguments if not argument.startswith("--")]
    error_count = 0
    for path in paths:
        project_root = find_project_root(path)
        config = load_config(project_root)
        rules = load_rules(config, project_root)
        findings, status = check_file(path, config, project_root, rules, gate, show_unverified=True)
        if status != "ok":
            print("%s: clang tier %s" % (path, status), file=sys.stderr)
            if gate:
                error_count += 1  # the gate can't pass a file it couldn't fully check
        for finding in findings:
            if show_all:
                print(format_finding(path, finding))
            if finding.rule["severity"] == "error" and not finding.unverified:
                error_count += 1
    return 1 if (gate and error_count) else 0


def command_hook():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_input = event.get("tool_input") or {}
    path = tool_input.get("file_path") or ""
    if not path or os.path.splitext(path)[1].lower() not in CXX_EXTENSIONS or not os.path.isfile(path):
        return 0
    project_root = find_project_root(path)
    config = load_config(project_root)
    rules = load_rules(config, project_root)
    findings, status = check_file(path, config, project_root, rules, gate=False)
    if status != "ok":
        log(project_root, "%s clang tier %s" % (path, status))
    state = load_state(project_root)
    fresh = [finding for finding in findings if finding.key(path) not in state]
    if not fresh:
        return 0
    for finding in fresh:
        state[finding.key(path)] = int(time.time())
    save_state(project_root, state)
    relative = os.path.relpath(path, project_root) if project_root else path
    lines = ["Style findings in %s:" % relative]
    for finding in fresh[:MAX_REMINDERS]:
        lines.append("- line %d [%s] %s" % (finding.line, finding.rule["id"], finding.rule["message"]))
    if len(fresh) > MAX_REMINDERS:
        lines.append("- ...and %d more; run the style-check skill for the full list." % (len(fresh) - MAX_REMINDERS))
    lines.append("If a construct must stay (an external API name, a required signature), add "
                 "`// sushi-allow: <rule-id> <reason>` on that line or the line above.")
    output = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n".join(lines)}}
    print(json.dumps(output))
    return 0


# ---------------------------------------------------------------- fixtures

EXPECT = re.compile(r"expect:\s*([\w\-]+(?:\s*,\s*[\w\-]+)*)")


def command_test_rules(arguments):
    project_root = find_project_root(os.getcwd())
    config = load_config(project_root)
    if "--host" in arguments:
        config["remote"]["host"] = arguments[arguments.index("--host") + 1]
    rules = load_rules(config, project_root)
    failures = 0
    for rule in rules:
        for kind in ("fail", "pass"):
            fixture = rule.get("fixtures", {}).get(kind)
            if fixture is None:
                print("MISSING %s %s fixture" % (rule["id"], kind))
                failures += 1
                continue
            fixture_path = os.path.join(rule["_base"], fixture)
            with open(fixture_path, encoding="utf-8") as handle:
                text = handle.read()
            lines = text.splitlines()
            expected = set()
            for number, line in enumerate(lines, start=1):
                match = EXPECT.search(line)
                if match is not None and rule["id"] in [part.strip() for part in match.group(1).split(",")]:
                    expected.add(number)
            single = [rule]
            findings = run_regex_tier(single, lines)
            clang_findings, status = run_clang_tier(fixture_path, text, single, config, project_root, gate=True)
            if not status.startswith("ok"):
                print("ERROR %s %s: %s" % (rule["id"], kind, status))
                failures += 1
                continue
            findings.extend(clang_findings)
            actual = {finding.line for finding in findings}
            if kind == "pass" and expected:
                print("BAD   %s pass fixture contains expect markers" % rule["id"])
                failures += 1
            elif actual != expected:
                print("FAIL  %s %s: expected lines %s, matched %s" % (rule["id"], kind, sorted(expected), sorted(actual)))
                failures += 1
            else:
                print("ok    %s %s (%d)" % (rule["id"], kind, len(actual)))
    print("%d rule(s), %d failure(s)" % (len(rules), failures))
    return 1 if failures else 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    command = sys.argv[1]
    if command == "hook":
        try:
            return command_hook()
        except Exception as error:  # a hook must never break the session
            log(None, "hook error: %r" % (error,))
            return 0
    if command == "check":
        return command_check(sys.argv[2:])
    if command == "test-rules":
        return command_test_rules(sys.argv[2:])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
