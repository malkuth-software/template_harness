#!/usr/bin/env python3
"""SushiWrap host probe: describe a build host as JSON. Read-only.

Runs locally (imported by sushiwrap.py) or remotely: sushiwrap.py sends this file's text
to `ssh HOST python3 -` on stdin, with a `PROBE_CONFIG = ...` line prepended, and reads
the JSON it prints. It must stay self-contained, standard library only, and compatible
with Python 3.9 (the M4's /usr/bin/python3).

What it runs: `--version` queries of a fixed tool list, `ccache -p`, `sysctl` reads
(macOS) and one login-shell `printf "$PATH"` (POSIX; optional). Repositories under the
configured workspace roots are read from their .git files only (branch, head, remotes).
It never writes, never uses the network, and redacts secrets at source. The default tool
directories and LLVM locations are overridable (survey.tool_dirs, survey.llvm_candidates).
"""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

PROBE_VERSION = 1

DEFAULT_TOOLS = ["clang", "clang++", "gcc", "g++", "cmake", "ninja", "ccache", "python3", "git",
                 "make", "rsync", "ssh"]
BREW_LLVM_CANDIDATES = ["/opt/homebrew/opt/llvm/bin/clang", "/usr/local/opt/llvm/bin/clang"]
DEFAULT_TOOL_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/homebrew/opt/llvm/bin"]
DEFAULT_ENV_NAMES = ["PATH", "SHELL", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL",
                     "CC", "CXX", "SDKROOT", "DEVELOPER_DIR", "MSYSTEM", "TERM_PROGRAM"]
DEFAULT_ENV_PREFIXES = ["CMAKE_", "CCACHE_", "HARNESS_", "GUI_FORMS_", "M4WS_", "SUSHI_", "LLVM_", "NINJA_"]
CCACHE_KEYS = ["cache_dir", "max_size", "base_dir", "compiler_check", "sloppiness", "compression",
               "hash_dir", "temporary_dir", "direct_mode", "depend_mode"]
SKIP_DIRS = {".git", "node_modules", "build", ".build", "out", "__pycache__", ".ccache", "toolchain",
             "sources", ".venv", "venv", "docs-venv"}
MACOS_SHIMS = {"clang", "clang++", "gcc", "g++", "cc", "c++", "git", "make", "python3"}

SECRET_NAME = re.compile(r"token|key|secret|passw|credential", re.IGNORECASE)
SECRET_VALUE = [
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),                       # credentials inside a URL
    re.compile(r"\b(ghp_|gho_|ghs_|github_pat_|glpat-|sk-|xox[abp]-|AKIA|AIza)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"^[A-Za-z0-9_\-+=]{32,}$"),                      # one long opaque token
]
REDACTED = "<redacted>"


def redact_value(name, value):
    """Return value, or REDACTED when the name or the value looks like a secret."""
    if name is not None and SECRET_NAME.search(name):
        return REDACTED
    for pattern in SECRET_VALUE:
        if pattern.search(value or ""):
            return REDACTED
    return value


def strip_url_credentials(url):
    return re.sub(r"://[^/\s@]+@", "://", url)


def run(argv, timeout=10):
    """Run argv, return (code, stdout) with stdout stripped. Never raises."""
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, timeout=timeout)
        return completed.returncode, completed.stdout.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)


def first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# ---------------------------------------------------------------- host basics

def boot_time_from_sysctl(text):
    match = re.search(r"sec\s*=\s*(\d+)", text or "")
    return int(match.group(1)) if match else None


def host_basics(errors):
    system = platform.system()
    info = {
        "hostname": platform.node().split(".")[0],
        "os": system,
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
    }
    if system == "Darwin":
        info["macos_version"] = platform.mac_ver()[0]
    cpu = {"logical": os.cpu_count()}
    memory = None
    boot = None
    if system == "Darwin":
        for key, name in (("hw.physicalcpu", "physical"), ("hw.perflevel0.physicalcpu", "performance"),
                          ("hw.perflevel1.physicalcpu", "efficiency")):
            code, out = run(["sysctl", "-n", key], timeout=5)
            if code == 0 and out.isdigit():
                cpu[name] = int(out)
        code, out = run(["sysctl", "-n", "hw.memsize"], timeout=5)
        if code == 0 and out.isdigit():
            memory = int(out)
        code, out = run(["sysctl", "-n", "kern.boottime"], timeout=5)
        if code == 0:
            boot = boot_time_from_sysctl(out)
    elif system == "Linux":
        try:
            with open("/proc/meminfo") as handle:
                for line in handle:
                    if line.startswith("MemTotal:"):
                        memory = int(line.split()[1]) * 1024
            with open("/proc/uptime") as handle:
                boot = int(time.time() - float(handle.read().split()[0]))
        except (OSError, ValueError, IndexError) as error:
            errors.append("linux basics: %s" % error)
    elif system == "Windows":
        try:
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.dwLength = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            memory = int(status.ullTotalPhys)
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            boot = int(time.time() - ctypes.windll.kernel32.GetTickCount64() / 1000.0)
        except Exception as error:  # ctypes differences across builds
            errors.append("windows basics: %s" % error)
    load = None
    if hasattr(os, "getloadavg"):
        try:
            load = [round(value, 2) for value in os.getloadavg()]
        except OSError:
            load = None
    return info, cpu, memory, boot, load


# ---------------------------------------------------------------- volumes

def windows_fixed_drives():
    drives = []
    try:
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for index in range(26):
            if mask & (1 << index):
                root = "%s:\\" % chr(65 + index)
                if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                    drives.append(root)
    except Exception:
        pass
    return drives


def volumes(candidates):
    seen = set()
    result = []
    for path in candidates:
        if not path or not os.path.exists(path):
            continue
        try:
            device = os.stat(path).st_dev
            if device in seen:
                continue
            seen.add(device)
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        result.append({"path": path, "total_bytes": usage.total, "free_bytes": usage.free,
                       "free_pct": round(100.0 * usage.free / usage.total, 1) if usage.total else None})
    return result


# ---------------------------------------------------------------- PATH, env, tools

def login_shell_path():
    shell = os.environ.get("SHELL")
    if os.name != "posix" or not shell or not os.path.exists(shell):
        return None
    code, out = run([shell, "-l", "-c", 'printf "\\n%s" "$PATH"'], timeout=8)
    if code != 0 or not out:
        return None
    lines = out.splitlines()
    return lines[-1].split(os.pathsep) if lines else None


def selected_env(names, prefixes):
    result = {}
    for name in sorted(os.environ):
        if name in names or any(name.startswith(prefix) for prefix in prefixes):
            if name == "PATH":
                continue  # reported separately, as a list
            result[name] = redact_value(name, os.environ[name])
    return result


def clt_present():
    if platform.system() != "Darwin":
        return True
    code, _ = run(["xcode-select", "-p"], timeout=5)
    return code == 0


VERSION_ARGS = {"ssh": ["-V"]}


def tool_version(path):
    name = os.path.basename(path).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    code, out = run([path] + VERSION_ARGS.get(name, ["--version"]), timeout=10)
    if code is None:
        return None
    return first_line(out)


def probe_tools(names, extra, search_dirs, errors, llvm_candidates=BREW_LLVM_CANDIDATES):
    have_clt = clt_present()
    search_path = os.pathsep.join(os.environ.get("PATH", "").split(os.pathsep) + search_dirs)
    tools = {}
    for name in names:
        path = shutil.which(name, path=search_path)
        if path is None:
            tools[name] = {"found": False}
            continue
        if not have_clt and os.path.dirname(path) == "/usr/bin" and name in MACOS_SHIMS:
            tools[name] = {"found": True, "path": path, "version": None,
                           "note": "Command Line Tools missing; shim not run (it would prompt to install)"}
            continue
        on_path = shutil.which(name) == path
        tools[name] = {"found": True, "path": path, "version": tool_version(path), "on_path": on_path}
    for candidate in llvm_candidates:
        if os.path.exists(candidate):
            tools["brew_llvm_clang"] = {"found": True, "path": candidate, "version": tool_version(candidate)}
            break
    else:
        tools["brew_llvm_clang"] = {"found": False}
    for name, path in sorted(extra.items()):
        path = os.path.expanduser(path)
        if os.path.exists(path):
            tools[name] = {"found": True, "path": path, "version": tool_version(path)}
        else:
            tools[name] = {"found": False, "path": path}
    if platform.system() == "Darwin" and have_clt:
        code, out = run(["xcode-select", "-p"], timeout=5)
        tools["xcode_select"] = {"found": True, "path": out}
        code, out = run(["xcrun", "--show-sdk-version"], timeout=10)
        if code == 0:
            tools["macos_sdk"] = {"found": True, "version": out}
    return tools


def parse_ccache_config(text):
    config = {}
    for line in (text or "").splitlines():
        match = re.match(r"\((\w+)\)\s+(\w+)\s*=\s*(.*)$", line.strip())
        if match and match.group(2) in CCACHE_KEYS:
            config[match.group(2)] = {"value": match.group(3), "origin": match.group(1)}
    return config


def probe_ccache(tools):
    entry = tools.get("ccache") or {}
    if not entry.get("found") or entry.get("version") is None:
        return None
    code, out = run([entry["path"], "-p"], timeout=10)
    return {"version": entry.get("version"), "config": parse_ccache_config(out) if code == 0 else {}}


# ---------------------------------------------------------------- repositories

def git_dirs(repo):
    """Return (git_dir, common_dir) for a repository or worktree, or (None, None)."""
    dot_git = os.path.join(repo, ".git")
    if os.path.isdir(dot_git):
        return dot_git, dot_git
    if os.path.isfile(dot_git):
        try:
            with open(dot_git) as handle:
                text = handle.read().strip()
        except OSError:
            return None, None
        if text.startswith("gitdir:"):
            git_dir = text[len("gitdir:"):].strip()
            if not os.path.isabs(git_dir):
                git_dir = os.path.normpath(os.path.join(repo, git_dir))
            common = git_dir
            common_file = os.path.join(git_dir, "commondir")
            if os.path.isfile(common_file):
                with open(common_file) as handle:
                    common = os.path.normpath(os.path.join(git_dir, handle.read().strip()))
            return git_dir, common
    return None, None


def resolve_ref(common_dir, git_dir, ref):
    for base in (git_dir, common_dir):
        path = os.path.join(base, *ref.split("/"))
        if os.path.isfile(path):
            with open(path) as handle:
                return handle.read().strip()
    packed = os.path.join(common_dir, "packed-refs")
    if os.path.isfile(packed):
        with open(packed) as handle:
            for line in handle:
                parts = line.strip().split(" ")
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
    return None


def read_repo(repo):
    git_dir, common_dir = git_dirs(repo)
    if git_dir is None:
        return None
    info = {"path": repo, "is_worktree": git_dir != common_dir, "branch": None, "head": None, "remotes": {}}
    try:
        with open(os.path.join(git_dir, "HEAD")) as handle:
            head = handle.read().strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            info["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            sha = resolve_ref(common_dir, git_dir, ref)
            info["head"] = sha[:12] if sha else None
        else:
            info["head"] = head[:12]
        with open(os.path.join(common_dir, "config")) as handle:
            remote = None
            for line in handle:
                section = re.match(r'\s*\[remote "([^"]+)"\]', line)
                if section:
                    remote = section.group(1)
                    continue
                if line.strip().startswith("["):
                    remote = None
                    continue
                url = re.match(r"\s*url\s*=\s*(.+)$", line)
                if remote and url:
                    info["remotes"][remote] = strip_url_credentials(url.group(1).strip())
    except OSError:
        pass
    return info


def scan_workspace(root, config):
    """Find git repositories (branch, head, remotes) under a workspace root. Reads .git files only."""
    root = os.path.expanduser(root)
    entry = {"root": root, "exists": os.path.isdir(root), "repos": []}
    if not entry["exists"]:
        return entry
    max_depth = int(config.get("repo_scan_depth", 2))
    queue = [(root, 0)]
    while queue:
        directory, depth = queue.pop(0)
        if os.path.exists(os.path.join(directory, ".git")):
            repo = read_repo(directory)
            if repo is not None and len(entry["repos"]) < 100:
                entry["repos"].append(repo)
            continue  # a repository's own contents are its business
        if depth >= max_depth:
            continue
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            full = os.path.join(directory, name)
            if os.path.isdir(full) and not os.path.islink(full) and not name.startswith(".") and name not in SKIP_DIRS:
                queue.append((full, depth + 1))
    return entry


# ---------------------------------------------------------------- traps

def detect_traps(manifest):
    traps = []
    login = manifest["path"].get("login_shell_entries")
    current = manifest["path"]["entries"]
    if login:
        missing = [item for item in login if item and item not in current]
        if missing:
            traps.append("This non-interactive process lacks %d PATH entries the login shell has (%s): "
                         "commands run over ssh may not find tools." % (len(missing), ", ".join(missing[:6])))
    for volume in manifest["volumes"]:
        free_gib = volume["free_bytes"] / float(1 << 30)
        if (volume["free_pct"] is not None and volume["free_pct"] < 10) or free_gib < 20:
            traps.append("Low disk space on %s: %.1f GiB free (%.0f%%)." % (volume["path"], free_gib, volume["free_pct"] or 0))
    if manifest["host"]["os"] == "Windows" and not os.environ.get("USERPROFILE"):
        traps.append("USERPROFILE is unset in this shell (MSYS2 sh drops it); ccache refuses to run without it.")
    ccache = manifest.get("ccache") or {}
    base_dir = ((ccache.get("config") or {}).get("base_dir") or {}).get("value") if ccache else None
    if ccache and not base_dir and not os.environ.get("CCACHE_BASEDIR"):
        if sum(len(ws.get("repos", [])) for ws in manifest["workspaces"]) > 1:
            traps.append("ccache base_dir is unset in this environment (an env script may set it): identical "
                         "sources in different workspaces share no cache hits without it.")
    return traps


# ---------------------------------------------------------------- main

def run_probe(config):
    errors = []
    started = time.time()
    host, cpu, memory, boot, load = host_basics(errors)
    home = os.path.expanduser("~")
    user = os.environ.get("USER") or os.environ.get("USERNAME") or os.path.basename(home)
    survey = config or {}
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    login_entries = login_shell_path() if survey.get("login_path", True) else None
    tool_dirs = [os.path.expanduser(d) for d in survey.get("tool_dirs", DEFAULT_TOOL_DIRS)]
    tool_dirs += [os.path.expanduser(d) for d in survey.get("extra_tool_dirs", [])]
    tools = probe_tools(DEFAULT_TOOLS + list(survey.get("tool_names", [])), survey.get("tools", {}), tool_dirs, errors,
                        [os.path.expanduser(p) for p in survey.get("llvm_candidates", BREW_LLVM_CANDIDATES)])
    ccache = probe_ccache(tools)
    workspaces = [scan_workspace(root, survey) for root in survey.get("workspace_roots", [])]
    candidates = [home, os.path.abspath(os.sep), os.environ.get("TMPDIR") or os.environ.get("TEMP")]
    candidates += [ws["root"] for ws in workspaces]
    if ccache and "cache_dir" in ccache["config"]:
        candidates.append(ccache["config"]["cache_dir"]["value"])
    candidates += [os.path.expanduser(v) for v in survey.get("volumes", [])]
    if host["os"] == "Windows":
        candidates += windows_fixed_drives()
    manifest = {
        "probe_version": PROBE_VERSION,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": host,
        "user": {"name": user, "home": home},
        "cpu": cpu,
        "memory_bytes": memory,
        "boot_time": boot,
        "load_avg": load,
        "volumes": volumes(candidates),
        "path": {"entries": path_entries, "login_shell_entries": login_entries,
                 "missing_dirs": [p for p in path_entries if p and not os.path.isdir(p)]},
        "env": selected_env(set(DEFAULT_ENV_NAMES + list(survey.get("env_names", []))),
                            DEFAULT_ENV_PREFIXES + list(survey.get("env_prefixes", []))),
        "tools": tools,
        "ccache": ccache,
        "workspaces": workspaces,
        "known_traps": list(survey.get("known_traps", [])),
        "errors": errors,
    }
    manifest["traps"] = detect_traps(manifest)
    manifest["probe_seconds"] = round(time.time() - started, 2)
    return manifest


if __name__ == "__main__":
    json.dump(run_probe(globals().get("PROBE_CONFIG") or {}), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
