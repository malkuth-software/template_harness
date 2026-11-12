#!/usr/bin/env python3
"""SushiRice: build slots and machine protection for machines that crash under build load.

Subcommands
  slot [--priority critical] [--timeout S] [--dry-run] -- CMD...
        Run CMD while holding one of this machine's build slots. A slot is granted when
        one is free AND live headroom allows (1-min load per core, memory pressure,
        running compiler processes). Exit status: CMD's own, or 75 if no slot was
        obtained within --timeout seconds. Nested calls (SUSHIRICE_SLOT_HELD=1 or
        GUI_FORMS_BUILD_SLOT_HELD=1) run at once without taking a second slot.
        The build runs in its own process group under a watchdog thread that acts on
        that build's tree only: it pauses (SIGSTOP) the newest compiler children under
        pressure, resumes (SIGCONT) them when it clears, and terminates the build's group
        after a sustained period. If the watchdog dies or stalls, the wrapper resumes
        everything and lets the build continue. --dry-run only reports.
  guard
        Diagnostic: what the watchdog rules would do to this user's compilers now.
        Never signals anything.
  report [--build-dir D] [--json]
        Reuse analysis: files rebuilt in the last ninja build vs. sources changed,
        longest edges, ccache hit rate, and duration vs. a moving baseline.
  trees [--root DIR...] [--json] [--gate]
        Census of git worktrees and configured CMake build trees, grouped by source dir;
        flags duplicate trees (same source and configuration) and output/package dirs
        shared across trees. --gate exits 1 when there are warnings.
  status [--json]
        One line of live headroom.
  monitor
        Long-running status for the orchestrator session: prints a short line only when
        something is wrong (machine pressure, a watchdog action, a slow or wasteful build,
        duplicate build trees). Never signals anything. Used by monitors/monitors.json.
  hook
        PreToolUse hook on Bash: reminds (or denies) when a build is not slot-wrapped,
        on `git worktree add` when the repo already has worktrees, and on a CMake
        configure into a new build dir when configured trees for that source exist.

Config: ~/.sushi/sushirice.json overlaid by <project>/.sushi/sushirice.json (the project wins;
$SUSHIRICE_CONFIG replaces both; see README). Python 3.9+, standard library only.
"""

import fnmatch
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.abspath(__file__)
TIMED_OUT = 75
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

PRESSURE_LEVELS = {"normal": 1, "warn": 2, "critical": 4}

DEFAULT_CONFIG = {
    # ---- slots
    "max_slots": 2,
    "reserved_critical_slots": 1,       # slot 0..n-1 only for critical runs (ignored when max_slots == 1)
    "lock_dir": None,                   # machine-wide; default ~/.cache/sushirice (Windows: %LOCALAPPDATA%)
    "foreign_slot_dirs": [],            # globs of other tools' pid-file lock dirs counted as held slots
    "poll_s": 3.0,
    "report_wait_s": 60.0,
    "start_spacing_s": 15.0,            # load average lags: no two grants closer than this
    "nesting_vars": ["SUSHIRICE_SLOT_HELD", "GUI_FORMS_BUILD_SLOT_HELD"],
    "priority_vars": ["SUSHIRICE_PRIORITY", "GUI_FORMS_BUILD_PRIORITY"],
    # ---- headroom (gates a new slot)
    "headroom": {
        "enabled": True,
        "max_load_per_core": 0.9,
        "max_compilers": None,          # default: number of cores
        "min_free_memory_percent": 20,
        "block_pressure_level": "critical",
        "critical_ignores_load": True,  # critical runs skip the load and compiler gates, not memory
    },
    "compiler_names": ["clang", "clang++", "cc1", "cc1plus", "ld", "ld64", "lld",
                       "cl.exe", "link.exe"],
    # ---- watchdog (inside `slot`, on the wrapped build's own process tree)
    "watchdog_mode": "enforce",         # "off" | "dry-run" | "enforce"
    "watchdog": {
        "interval_s": 2.0,
        "max_compilers": None,          # default: 1.5 x cores
        "pressure_level": "critical",   # pause when macOS/Linux pressure reaches this level
        "min_free_memory_percent": 10,  # ...or free memory falls below this
        "resume_margin_percent": 10,    # resume only once free memory is this much above the minimum
        "pause_step": 2,
        "resume_step": 2,
        "min_running": 1,               # never pause the last N running compilers for memory alone
        "terminate_after_s": 600,       # null: never terminate
        "kill_grace_s": 30,             # SIGKILL the group if it outlives SIGTERM by this much
        "stall_s": 30,                  # supervisor releases everything if the watchdog is silent this long
        "exclude_names": [],
    },
    # ---- monitor
    "monitor_roles": ["orchestrator"],  # SUSHI_ROLE values that run the monitor (unset role: runs)
    "monitor_interval_s": 10.0,
    "monitor_status_every_s": 300.0,
    # ---- report
    "report": {
        "rebuild_ratio": 10,            # warn when objects rebuilt > max(min_objects, ratio x changed)
        "min_objects": 20,
        "ccache_min_hit_rate": 0.5,
        "baseline_runs": 10,
        "slow_factor": 1.5,
        "longest_edges": 8,
    },
    # ---- hook
    "hook_mode": "remind",              # "off" | "remind" | "deny"
    "hook_remind_every_s": 1800,
    "hook_build_programs": ["ninja", "make", "gmake", "xcodebuild", "msbuild", "msbuild.exe"],
    "hook_build_scripts": ["ci_build.py", "fold*_build.sh"],
    "hook_wrappers": ["sushirice", "sushirice.py", "build_slot.py", "m4ws", "m4run"],
    "hook_trees_mode": "remind",        # "off" | "remind" | "deny": worktree add / configure into a new tree
    # ---- trees census
    "trees": {
        "roots": None,                  # default: the project root
        "max_depth": 3,
        "max_per_source": 2,            # one Release + one sanitizer tree
        "max_worktrees_per_repo": None,
        "hook_depth": 2,
        "every_s": 600,                 # monitor census interval
    },
}

SOURCE_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".m", ".mm", ".h", ".hh", ".hpp", ".hxx",
                     ".ipp", ".inl", ".inc", ".swift", ".rs"}
HEADER_EXTENSIONS = {".h", ".hh", ".hpp", ".hxx", ".ipp", ".inl", ".inc"}


# ================================================================ config / paths

def find_project_root(start_path):
    directory = os.path.abspath(start_path)
    if os.path.isfile(directory):
        directory = os.path.dirname(directory)
    while True:
        if os.path.isdir(os.path.join(directory, ".sushi")) or os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def merge(base, user):
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def config_paths(project_root):
    """The config layers, later ones winning: ~/.sushi/sushirice.json (per machine), then
    <project>/.sushi/sushirice.json. $SUSHIRICE_CONFIG replaces both."""
    explicit = os.environ.get("SUSHIRICE_CONFIG")
    if explicit is not None:
        return [explicit]
    paths = [os.path.join(os.path.expanduser("~"), ".sushi", "sushirice.json")]
    if project_root is not None:
        paths.append(os.path.join(project_root, ".sushi", "sushirice.json"))
    return paths


def load_config(project_root):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for path in config_paths(project_root):
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                merge(config, json.load(handle))
    return config


def base_dir(project_root):
    return project_root or os.path.expanduser("~")


def state_dir(project_root, create=True):
    directory = os.path.join(base_dir(project_root), ".sushi", "state", "sushirice")
    if create:
        os.makedirs(directory, exist_ok=True)
    return directory


def lock_dir(config):
    directory = os.environ.get("SUSHIRICE_LOCK_DIR") or config.get("lock_dir")
    if directory:
        directory = os.path.expanduser(directory)
    elif IS_WINDOWS:
        directory = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "sushirice")
    else:
        directory = os.path.join(os.path.expanduser("~"), ".cache", "sushirice")
    os.makedirs(directory, exist_ok=True)
    return directory


def log(project_root, message):
    try:
        directory = os.path.join(base_dir(project_root), ".sushi", "logs")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "sushirice.log"), "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))
    except OSError:
        pass


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    temporary = "%s.%d.tmp" % (path, os.getpid())
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(temporary, path)


def run_quiet(argv, timeout=10, env=None):
    """Run a short read-only command; return stdout text or None."""
    try:
        completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   timeout=timeout, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.decode("utf-8", "replace")


# ================================================================ processes

def pid_alive(pid):
    if pid is None or pid <= 0:
        return False
    if IS_WINDOWS:
        # os.kill(pid, 0) on Windows sends CTRL_C_EVENT; never use it as a probe there.
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def parse_etime(text):
    """ps etime: [[dd-]hh:]mm:ss -> seconds."""
    text = text.strip()
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        days = int(day_text)
    parts = [int(part) for part in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_ps(text):
    """Parse `ps -A -o pid=,ppid=,pgid=,uid=,stat=,etime=,comm=` output."""
    processes = []
    for line in text.splitlines():
        fields = line.split(None, 6)
        if len(fields) < 7:
            continue
        try:
            processes.append({
                "pid": int(fields[0]),
                "ppid": int(fields[1]),
                "pgid": int(fields[2]),
                "uid": int(fields[3]),
                "stat": fields[4],
                "age_s": parse_etime(fields[5]),
                "name": os.path.basename(fields[6].strip()),
                "path": fields[6].strip(),
            })
        except ValueError:
            continue
    return processes


def parse_tasklist(text):
    processes = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('"'):
            continue
        cells = [cell.strip('"') for cell in line.split('","')]
        if len(cells) >= 2:
            try:
                processes.append({"pid": int(cells[1]), "ppid": None, "pgid": None, "uid": None, "stat": "", "age_s": 0,
                                  "name": cells[0], "path": cells[0]})
            except ValueError:
                continue
    return processes


def list_processes():
    if IS_WINDOWS:
        return parse_tasklist(run_quiet(["tasklist", "/FO", "CSV", "/NH"]) or "")
    return parse_ps(run_quiet(["ps", "-A", "-o", "pid=,ppid=,pgid=,uid=,stat=,etime=,comm="]) or "")


def name_matches(name, patterns):
    lowered = name.lower()
    for pattern in patterns:
        pattern = pattern.lower()
        if lowered == pattern:
            return True
        if lowered.startswith(pattern + "-") and lowered[len(pattern) + 1:len(pattern) + 2].isdigit():
            return True  # versioned: clang-18, clang++-17
    return False


def compiler_processes(processes, config, uid=None):
    names = config["compiler_names"]
    found = [process for process in processes if name_matches(process["name"], names)]
    if uid is not None:
        found = [process for process in found if process["uid"] == uid]
    return found


# ================================================================ headroom

def parse_loadavg_sysctl(text):
    numbers = re.findall(r"[\d.]+", text or "")
    return float(numbers[0]) if numbers else None


def parse_proc_loadavg(text):
    try:
        return float(text.split()[0])
    except (IndexError, ValueError, AttributeError):
        return None


def parse_memory_pressure(text):
    """`memory_pressure -Q` -> free percent."""
    match = re.search(r"free percentage:\s*(\d+)%", text or "")
    return int(match.group(1)) if match else None


def parse_vm_stat(text, total_bytes=None):
    """vm_stat -> approximate free percent (free + inactive + speculative + purgeable)."""
    if not text:
        return None
    page_match = re.search(r"page size of (\d+) bytes", text)
    page_size = int(page_match.group(1)) if page_match else 4096
    values = {}
    for line in text.splitlines():
        match = re.match(r'\s*"?([^:"]+)"?:\s+(\d+)\.?', line)
        if match:
            values[match.group(1).strip().lower()] = int(match.group(2))
    available = sum(values.get(key, 0) for key in
                    ("pages free", "pages inactive", "pages speculative", "pages purgeable"))
    if total_bytes:
        total_pages = total_bytes // page_size
    else:
        total_pages = sum(values.get(key, 0) for key in
                          ("pages free", "pages active", "pages inactive", "pages speculative",
                           "pages wired down", "pages occupied by compressor"))
    if not total_pages:
        return None
    return int(round(100.0 * available / total_pages))


def parse_pressure_level_sysctl(text):
    """kern.memorystatus_vm_pressure_level: 1 normal, 2 warn, 4 critical."""
    try:
        value = int((text or "").strip().split()[-1])
    except (IndexError, ValueError):
        return None
    return {1: "normal", 2: "warn", 4: "critical"}.get(value, "normal" if value < 2 else "critical")


def parse_meminfo(text):
    values = {}
    for line in (text or "").splitlines():
        match = re.match(r"(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2))
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if available is None and total:
        available = values.get("MemFree", 0) + values.get("Cached", 0) + values.get("Buffers", 0)
    if not total:
        return None
    return int(round(100.0 * available / total))


def parse_psi(text):
    """/proc/pressure/memory -> level. some avg10 >= 20 warn; full avg10 >= 10 or some >= 50 critical."""
    some = full = None
    for line in (text or "").splitlines():
        match = re.search(r"avg10=([\d.]+)", line)
        if not match:
            continue
        if line.startswith("some"):
            some = float(match.group(1))
        elif line.startswith("full"):
            full = float(match.group(1))
    if some is None:
        return None
    if (full or 0) >= 10 or some >= 50:
        return "critical"
    if some >= 20:
        return "warn"
    return "normal"


def read_text(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def windows_memory_free_percent():
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
        return 100 - int(status.dwMemoryLoad)
    except Exception:
        return None


def measure_headroom(config, processes=None):
    """Live readings. Any value may be None when this platform can't tell."""
    cores = os.cpu_count() or 1
    load = None
    free_percent = None
    level = None
    if IS_MAC:
        load = parse_loadavg_sysctl(run_quiet(["sysctl", "-n", "vm.loadavg"]))
        level = parse_pressure_level_sysctl(run_quiet(["sysctl", "kern.memorystatus_vm_pressure_level"]))
        free_percent = parse_memory_pressure(run_quiet(["memory_pressure", "-Q"]))
        if free_percent is None:
            total = run_quiet(["sysctl", "-n", "hw.memsize"])
            free_percent = parse_vm_stat(run_quiet(["vm_stat"]),
                                         int(total) if total and total.strip().isdigit() else None)
    elif IS_LINUX:
        load = parse_proc_loadavg(read_text("/proc/loadavg"))
        free_percent = parse_meminfo(read_text("/proc/meminfo"))
        level = parse_psi(read_text("/proc/pressure/memory"))
    elif IS_WINDOWS:
        free_percent = windows_memory_free_percent()
    else:
        try:
            load = os.getloadavg()[0]
        except (AttributeError, OSError):
            load = None
    if processes is None:
        processes = list_processes()
    compilers = len(compiler_processes(processes, config))
    return {"cores": cores, "load": load, "free_percent": free_percent,
            "pressure": level, "compilers": compilers}


def level_at_least(level, threshold):
    if level is None or threshold is None:
        return False
    return PRESSURE_LEVELS.get(level, 1) >= PRESSURE_LEVELS.get(threshold, 99)


def headroom_allows(headroom, config, critical):
    """(allowed, reason)."""
    settings = config["headroom"]
    if not settings.get("enabled", True):
        return True, "headroom checks off"
    cores = headroom["cores"]
    if level_at_least(headroom.get("pressure"), settings.get("block_pressure_level")):
        return False, "memory pressure %s" % headroom["pressure"]
    free_percent = headroom.get("free_percent")
    if free_percent is not None and free_percent < settings.get("min_free_memory_percent", 0):
        return False, "memory %d%% free" % free_percent
    if critical and settings.get("critical_ignores_load", True):
        return True, "ok (critical)"
    load = headroom.get("load")
    if load is not None and load / cores > settings.get("max_load_per_core", 1e9):
        return False, "load %.1f on %d cores" % (load, cores)
    max_compilers = settings.get("max_compilers") or cores
    if headroom.get("compilers", 0) >= max_compilers:
        return False, "%d compilers running" % headroom["compilers"]
    return True, "ok"


def memory_text(headroom):
    memory = "memory"
    if headroom.get("pressure"):
        memory += " " + headroom["pressure"]
    if headroom.get("free_percent") is not None:
        memory += " %d%% free" % headroom["free_percent"]
    return memory


def format_headroom(headroom):
    parts = []
    if headroom.get("load") is not None:
        parts.append("load %.1f/%d cores" % (headroom["load"], headroom["cores"]))
    if headroom.get("free_percent") is not None or headroom.get("pressure"):
        parts.append(memory_text(headroom))
    parts.append("%d compilers" % headroom.get("compilers", 0))
    return ", ".join(parts)


# ================================================================ lock directories

def read_owner(directory):
    data = read_json(os.path.join(directory, "owner.json"), None)
    if data is None:
        pid_text = read_text(os.path.join(directory, "pid"))
        if pid_text and pid_text.strip().isdigit():
            data = {"pid": int(pid_text.strip())}
    return data


def lock_is_stale(directory, grace_s=10.0):
    owner = read_owner(directory)
    if owner is None or "pid" not in owner:
        try:
            age = time.time() - os.stat(directory).st_mtime
        except OSError:
            return False
        return age > grace_s  # a just-created dir may not have its pid file yet
    host = owner.get("host")
    if host and host != socket.gethostname():
        return False  # can't probe another machine's pid
    return not pid_alive(int(owner["pid"]))


def remove_tree(directory):
    try:
        for name in os.listdir(directory):
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass
        os.rmdir(directory)
        return True
    except OSError:
        return False


def try_take(directory, owner):
    try:
        os.mkdir(directory)
    except OSError:
        return False
    with open(os.path.join(directory, "pid"), "w", encoding="utf-8") as handle:
        handle.write(str(owner["pid"]))
    write_json(os.path.join(directory, "owner.json"), owner)
    return True


def clean_stale(directories, project_root):
    removed = []
    for directory in directories:
        if os.path.isdir(directory) and lock_is_stale(directory):
            owner = read_owner(directory) or {}
            if remove_tree(directory):
                removed.append(directory)
                log(project_root, "removed stale lock %s (pid %s)" % (directory, owner.get("pid")))
    return removed


def foreign_holders(config):
    import glob
    held = []
    for pattern in config.get("foreign_slot_dirs") or []:
        for directory in glob.glob(os.path.expanduser(pattern)):
            if os.path.isdir(directory) and not lock_is_stale(directory, grace_s=1e9):
                owner = read_owner(directory)
                if owner is not None:
                    held.append(directory)
    return held


# ================================================================ slot

def slot_dirs(config):
    directory = os.path.join(lock_dir(config), "slots")
    os.makedirs(directory, exist_ok=True)
    return [os.path.join(directory, "slot%d" % index) for index in range(int(config["max_slots"]))]


def candidate_slots(config, critical):
    count = int(config["max_slots"])
    reserved = min(int(config.get("reserved_critical_slots", 0)), max(count - 1, 0))
    if critical:
        return list(range(count))
    return list(range(reserved, count))


def describe_holders(directories):
    holders = []
    for index, directory in enumerate(directories):
        owner = read_owner(directory) if os.path.isdir(directory) else None
        if owner:
            holders.append("[%d] pid %s %s" % (index, owner.get("pid"), (owner.get("cmd") or "")[:60]))
    return "; ".join(holders) or "none"


def acquire_slot(config, critical, timeout_s, command_text, project_root, measure=None, now=time.time,
                 sleep=time.sleep, err=sys.stderr):
    """Return (slot_index, slot_dir, waited_s) or (None, None, waited_s) on timeout."""
    measure = measure or (lambda: measure_headroom(config))
    directories = slot_dirs(config)
    candidates = candidate_slots(config, critical)
    started = now()
    last_report = None
    while True:
        clean_stale(directories, project_root)
        held = [d for d in directories if os.path.isdir(d)]
        foreign = foreign_holders(config)
        free = [index for index in candidates if not os.path.isdir(directories[index])]
        reason = None
        if not free or len(held) + len(foreign) >= int(config["max_slots"]):
            reason = "all %d slots busy" % int(config["max_slots"])
        else:
            spacing = float(config.get("start_spacing_s", 0))
            newest = max([os.stat(d).st_mtime for d in held if os.path.isdir(d)] or [0])
            if held and spacing > 0 and now() - newest < spacing:
                reason = "a slot was granted %.0fs ago" % (now() - newest)
            else:
                allowed, why = headroom_allows(measure(), config, critical)
                if not allowed:
                    reason = "headroom: " + why
        if reason is None:
            for index in free:
                owner = {"pid": os.getpid(), "host": socket.gethostname(), "cmd": command_text,
                         "cwd": os.getcwd(), "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
                         "priority": "critical" if critical else "normal"}
                if try_take(directories[index], owner):
                    return index, directories[index], now() - started
            continue  # lost a race; re-evaluate at once
        waited = now() - started
        if timeout_s is not None and timeout_s >= 0 and waited >= timeout_s:
            err.write("sushirice: no slot after %ds (%s); holders: %s\n"
                      % (int(waited), reason, describe_holders(directories)))
            err.flush()
            return None, None, waited
        if last_report is None or waited - last_report >= float(config.get("report_wait_s", 60)):
            err.write("sushirice: waiting (%s); holders: %s\n" % (reason, describe_holders(directories)))
            err.flush()
            last_report = waited
        sleep(float(config.get("poll_s", 3.0)))


def detect_build_dir(command):
    for index, word in enumerate(command):
        if word == "--build" and index + 1 < len(command):
            return os.path.abspath(command[index + 1])
        if word == "-C" and index + 1 < len(command) and os.path.basename(command[0]) in ("ninja", "make", "gmake"):
            return os.path.abspath(command[index + 1])
    return None


def record_run(project_root, record):
    try:
        with open(os.path.join(state_dir(project_root), "runs.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except OSError as error:
        log(project_root, "could not record run: %r" % (error,))


def resolve_watchdog_mode(config, force_dry_run=False):
    mode = config.get("watchdog_mode", "enforce")
    if mode not in ("off", "dry-run", "enforce"):
        mode = "dry-run"
    if force_dry_run and mode == "enforce":
        mode = "dry-run"
    if IS_WINDOWS:
        mode = "off"  # no SIGSTOP/SIGCONT; the build runs unwatched
    return mode


def start_build(command, env):
    """Start the build in its own process group (POSIX) so the watchdog can address exactly its tree."""
    if IS_WINDOWS:
        return subprocess.Popen(command, env=env)
    return subprocess.Popen(command, env=env, preexec_fn=os.setpgrp)


def run_child(command, env, config=None, project_root=None, mode="off", err=sys.stderr):
    """Run the build under a watchdog. Returns (exit code, watchdog summary or None)."""
    try:
        child = start_build(command, env)
    except OSError as error:
        err.write("sushirice: cannot run %s: %s\n" % (command[0], error))
        return 127, None
    watchdog = None
    if mode != "off" and config is not None:
        watchdog = Watchdog(config, project_root, child.pid, mode, err=err)
    return supervise(child, watchdog, err), (watchdog.summary() if watchdog else None)


def signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
        return True
    except OSError:
        return False


def supervise(child, watchdog, err=sys.stderr, poll_s=0.5):
    """Wait for the build; forward signals to its group; watch the watchdog."""
    previous = {}
    grouped = not IS_WINDOWS
    received = []

    def forward(signum, frame):
        received.append(signum)
        if watchdog is not None:
            watchdog.continue_paused_now()  # stopped compilers can't act on the signal until continued
        if grouped:
            signal_group(child.pid, signum)
        else:
            try:
                child.send_signal(signum)
            except OSError:
                pass

    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        if hasattr(signal, name):
            try:
                previous[name] = signal.signal(getattr(signal, name), forward)
            except ValueError:
                pass  # not the main thread (tests)
    if watchdog is not None:
        watchdog.start()
    killed = False
    try:
        while True:
            try:
                code = child.wait(timeout=poll_s)
                break
            except subprocess.TimeoutExpired:
                pass
            if watchdog is None:
                continue
            if received and not watchdog.released:
                # stopped compilers can't act on the signal until continued
                watchdog.release("build received signal %d" % received[0])
            problem = watchdog.problem()
            if problem and not watchdog.released:
                watchdog.release(problem)
                err.write("sushirice: watchdog %s; resumed paused compilers, build continues unwatched\n" % problem)
                err.flush()
            if (watchdog.terminated_at and not killed
                    and time.time() - watchdog.terminated_at >= watchdog.kill_grace_s):
                watchdog.signal_tree(signal.SIGKILL)
                watchdog.note_event("kill", None, "build group still alive %ds after SIGTERM" % watchdog.kill_grace_s)
                killed = True
    finally:
        if watchdog is not None:
            watchdog.stop()
            if not watchdog.released:
                watchdog.release(None)  # never leave a paused tree behind
        for name, handler in previous.items():
            signal.signal(getattr(signal, name), handler)
    if code < 0:
        code = 128 - code
    return code


def tree_of(processes, root_pid):
    """The build's processes: descendants of root_pid plus members of its process group."""
    children = {}
    for process in processes:
        children.setdefault(process.get("ppid"), []).append(process)
    found = {}
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        for process in children.get(pid, []):
            if process["pid"] not in found:
                found[process["pid"]] = process
                stack.append(process["pid"])
    for process in processes:
        if process.get("pgid") == root_pid:
            found.setdefault(process["pid"], process)
    return list(found.values())


class Watchdog:
    """Watches one build's process tree for the life of the build.

    A thread ticks every interval: it measures machine headroom and applies watchdog_decide to
    the compilers in the build's tree: pause the newest under pressure, resume when it clears,
    terminate the build's group after a sustained period. The supervising thread checks the
    heartbeat; if the watchdog thread dies or stalls it calls release(), which resumes
    everything and disables further action.
    """

    def __init__(self, config, project_root, root_pid, mode, err=sys.stderr, list_fn=None, measure_fn=None,
                 signal_fn=None, group_signal_fn=None):
        settings = config["watchdog"]
        self.config = config
        self.project_root = project_root
        self.root_pid = root_pid
        self.mode = mode
        self.err = err
        self.list_fn = list_fn or list_processes
        self.measure_fn = measure_fn or (lambda processes: measure_headroom(config, processes))
        self.signal_fn = signal_fn or send_signal
        self.group_signal_fn = group_signal_fn or signal_group
        self.interval = float(settings.get("interval_s", 2.0))
        self.stall_s = max(float(settings.get("stall_s", 30.0)), 3 * self.interval)
        self.kill_grace_s = float(settings.get("kill_grace_s", 30.0))
        self.uid = None if IS_WINDOWS else os.getuid()
        self.state = {"paused": {}, "triggered_since": None}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.heartbeat = time.time()
        self.failure = None
        self.released = None
        self.terminated_at = None
        self.events = []
        self.counts = {"pause": 0, "resume": 0, "terminate": 0}

    # ---- thread
    def start(self):
        self.heartbeat = time.time()
        self.thread = threading.Thread(target=self.run, name="sushirice-watchdog", daemon=True)
        self.thread.start()

    def run(self):
        try:
            while not self.stop_event.is_set():
                self.tick()
                self.stop_event.wait(self.interval)
        except Exception as error:  # the supervisor sees the dead thread and releases
            self.failure = repr(error)
            log(self.project_root, "watchdog failed: %r" % (error,))

    def stop(self):
        self.stop_event.set()

    def problem(self):
        if self.thread is None or self.released:
            return None
        if not self.thread.is_alive() and not self.stop_event.is_set():
            return "thread died (%s)" % (self.failure or "unknown")
        if time.time() - self.heartbeat > self.stall_s:
            return "stalled for %ds" % int(time.time() - self.heartbeat)
        return None

    # ---- one step
    def tick(self, now=None):
        processes = self.list_fn()
        headroom = self.measure_fn(processes)
        now = time.time() if now is None else now
        tree = tree_of(processes, self.root_pid)
        actions, new_state = watchdog_decide(tree, headroom, self.state, self.config, now, self.uid)
        done = {"pause": [], "resume": [], "terminate": []}
        with self.lock:
            if self.released or self.terminated_at:
                self.heartbeat = time.time()
                return done
            for verb, process, reason in actions:
                if verb == "terminate":
                    if self.terminated_at is None:
                        self.terminate_group(reason, now)
                        done["terminate"].append(process)
                    continue
                if self.mode == "enforce":
                    try:
                        self.signal_fn(process["pid"], verb)
                    except OSError as error:
                        log(self.project_root, "watchdog FAILED %s pid %d: %r" % (verb, process["pid"], error))
                        if verb == "pause":
                            new_state["paused"].pop(str(process["pid"]), None)
                        continue
                self.note_event(verb, process, reason)
                done[verb].append(process)
            self.state = new_state
            self.heartbeat = time.time()
        self.announce(done, actions)
        return done

    def terminate_group(self, reason, now):
        if self.mode == "enforce":
            self.terminated_at = now
            self.signal_tree(signal.SIGTERM)
            self.signal_tree(signal.SIGCONT)  # stopped members handle SIGTERM only once continued
            self.state = {"paused": {}, "triggered_since": None}
        self.note_event("terminate", None, reason)

    def signal_tree(self, signum):
        """Signal the build's root group and every process group and process in its tree.

        Build tools put jobs in groups of their own (ninja does), so the root group alone misses them.
        """
        try:
            tree = tree_of(self.list_fn(), self.root_pid)
        except Exception:
            tree = []
        groups = set(process.get("pgid") for process in tree if process.get("pgid"))
        groups.add(self.root_pid)
        mine = self.uid
        for group in groups:
            if group > 1:
                self.group_signal_fn(group, signum)
        for process in tree:
            if process["pid"] > 1 and (mine is None or process.get("uid") == mine):
                try:
                    os.kill(process["pid"], signum)
                except OSError:
                    pass

    def note_event(self, verb, process, reason):
        if verb in self.counts:
            self.counts[verb] += 1
        prefix = "" if self.mode == "enforce" else "would "
        if process is not None:
            line = "%s%s pid %d (%s): %s" % (prefix, verb, process["pid"], process["name"], reason)
        else:
            line = "%s%s build group %d: %s" % (prefix, verb, self.root_pid, reason)
        if len(self.events) < 40:
            self.events.append({"t": round(time.time(), 1), "event": line})
        log(self.project_root, "watchdog " + line)

    def announce(self, done, actions):
        reasons = {verb: reason for verb, _, reason in actions}
        enforce = self.mode == "enforce"
        prefix = "sushirice: watchdog " + ("" if enforce else "(dry-run) would ")
        lines = []
        for verb, past in (("pause", "paused"), ("resume", "resumed")):
            count = len(done[verb])
            if count:
                line = "%s%s %d compiler%s" % (prefix, past if enforce else verb, count, "" if count == 1 else "s")
                if verb == "pause":
                    line += " (%s)" % reasons.get("pause")
                lines.append(line)
        if done["terminate"]:
            lines.append("%s%s the build (%s)" % (prefix, "terminated" if enforce else "terminate",
                                                  reasons.get("terminate")))
        for line in lines:
            self.err.write(line + "\n")
        if lines:
            self.err.flush()

    # ---- release
    def continue_paused_now(self):
        """SIGCONT every paused pid at once, without the lock (safe inside a signal handler).

        A paused process can't exit while stopped, so its pid can't have been reused.
        """
        if self.mode != "enforce":
            return
        for pid_text in list((self.state.get("paused") or {}).keys()):
            try:
                os.kill(int(pid_text), signal.SIGCONT)
            except (OSError, ValueError):
                pass

    def release(self, why):
        """Resume every paused process in the tree and stop acting. Safe to call more than once."""
        with self.lock:
            if self.released:
                return
            self.released = why or "build finished"
            paused = list((self.state.get("paused") or {}).items())
            self.state = {"paused": {}, "triggered_since": None}
        if self.mode != "enforce":
            return
        if paused:
            self.group_signal_fn(self.root_pid, signal.SIGCONT)  # SIGCONT to the build's own group is harmless
        try:
            current = {process["pid"]: process for process in self.list_fn()}
        except Exception:
            current = {}
        now = time.time()
        for pid_text, entry in paused:
            process = current.get(int(pid_text))
            if process is None or process["name"] != entry.get("name"):
                continue
            if abs((now - process["age_s"]) - entry.get("start", 0)) > 5:
                continue
            try:
                self.signal_fn(int(pid_text), "resume")
                self.counts["resume"] += 1
            except OSError:
                pass
        if why:
            self.note_event("release", None, why)

    def summary(self):
        return {"mode": self.mode, "paused": self.counts["pause"], "resumed": self.counts["resume"],
                "terminated": self.counts["terminate"] > 0 and self.mode == "enforce",
                "released": self.released if self.released != "build finished" else None,
                "failure": self.failure, "events": self.events}


def command_slot(arguments):
    critical = False
    timeout_s = None
    dry_run = False
    command = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--":
            command = arguments[index + 1:]
            break
        if word == "--priority" and index + 1 < len(arguments):
            critical = arguments[index + 1] == "critical"
            index += 2
            continue
        if word in ("--timeout", "--wait") and index + 1 < len(arguments):
            timeout_s = float(arguments[index + 1])
            index += 2
            continue
        if word == "--dry-run":
            dry_run = True
            index += 1
            continue
        command = arguments[index:]
        break
    if not command:
        sys.stderr.write("usage: sushirice slot [--priority critical] [--timeout S] [--dry-run] -- CMD...\n")
        return 2
    project_root = find_project_root(os.getcwd())
    config = load_config(project_root)
    for variable in config["priority_vars"]:
        if os.environ.get(variable) == "critical":
            critical = True
    command_text = " ".join(shlex.quote(word) for word in command)
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "cmd": command_text, "cwd": os.getcwd(),
              "host": socket.gethostname(), "priority": "critical" if critical else "normal",
              "build_dir": detect_build_dir(command)}
    nested = any(os.environ.get(variable) == "1" for variable in config["nesting_vars"])
    if nested:  # the outer slot's watchdog already watches this tree
        started = time.time()
        try:
            code = subprocess.call(command)
        except OSError as error:
            sys.stderr.write("sushirice: cannot run %s: %s\n" % (command[0], error))
            code = 127
        if code < 0:
            code = 128 - code
        record.update({"nested": True, "slot": None, "waited_s": 0.0,
                       "duration_s": round(time.time() - started, 3), "exit": code})
        record_run(project_root, record)
        return code
    index, directory, waited = acquire_slot(config, critical, timeout_s, command_text, project_root)
    if index is None:
        record.update({"slot": None, "waited_s": round(waited, 3), "duration_s": 0.0, "exit": TIMED_OUT,
                       "timed_out": True})
        record_run(project_root, record)
        return TIMED_OUT
    log(project_root, "slot %d granted after %.1fs: %s" % (index, waited, command_text))
    environment = dict(os.environ)
    for variable in config["nesting_vars"]:
        environment[variable] = "1"
    environment["SUSHIRICE_SLOT"] = str(index)
    mode = resolve_watchdog_mode(config, dry_run)
    started = time.time()
    summary = None
    try:
        code, summary = run_child(command, environment, config, project_root, mode)
    finally:
        remove_tree(directory)
    record.update({"slot": index, "waited_s": round(waited, 3),
                   "duration_s": round(time.time() - started, 3), "exit": code})
    if summary is not None:
        record["watchdog"] = summary
        if summary["paused"] or summary["terminated"] or summary["released"]:
            sys.stderr.write("sushirice: watchdog summary: %d paused, %d resumed%s%s\n" % (
                summary["paused"], summary["resumed"], ", build terminated" if summary["terminated"] else "",
                (", released: " + summary["released"]) if summary["released"] else ""))
    record_run(project_root, record)
    return code


# ================================================================ watchdog rules

def watchdog_thresholds(config, cores):
    settings = config["watchdog"]
    max_compilers = settings.get("max_compilers") or max(2, int(cores * 1.5))
    return settings, int(max_compilers)


def memory_bad(headroom, settings):
    if level_at_least(headroom.get("pressure"), settings.get("pressure_level")):
        return True
    free_percent = headroom.get("free_percent")
    return free_percent is not None and free_percent < settings.get("min_free_memory_percent", 0)


def memory_recovered(headroom, settings):
    level = headroom.get("pressure")
    threshold = settings.get("pressure_level")
    if level is not None and threshold in PRESSURE_LEVELS and PRESSURE_LEVELS.get(level, 1) >= PRESSURE_LEVELS[threshold]:
        return False
    free_percent = headroom.get("free_percent")
    needed = settings.get("min_free_memory_percent", 0) + settings.get("resume_margin_percent", 0)
    return free_percent is None or free_percent >= needed


def watchdog_decide(processes, headroom, state, config, now, uid, protected_pids=()):
    """Pure decision step over one build's processes. Returns (actions, new_state).

    actions: list of (verb, process, reason) with verb in pause/resume/terminate.
    state: {"paused": {pid: {"name", "start", "since"}}, "triggered_since": ts or None}
    """
    settings, max_compilers = watchdog_thresholds(config, headroom.get("cores") or 1)
    excluded = set(protected_pids)
    excluded.update((os.getpid(), 0, 1))
    excluded_names = settings.get("exclude_names") or []
    compilers = compiler_processes(processes, config, uid)
    # A driver (clang++) waiting on its compiler child (clang -cc1, ld) is idle: count and pause only
    # leaves, or pausing the children while counting the drivers as running deadlocks the build.
    parents = set(process.get("ppid") for process in compilers)
    mine = [process for process in compilers
            if process["pid"] not in excluded and process["pid"] not in parents
            and not name_matches(process["name"], excluded_names)]
    by_pid = {process["pid"]: process for process in mine}
    paused = {}
    for pid_text, entry in (state.get("paused") or {}).items():
        process = by_pid.get(int(pid_text))
        if process is None:
            continue  # finished, or no longer ours / no longer a compiler
        if abs((now - process["age_s"]) - entry.get("start", 0)) > 5 or process["name"] != entry.get("name"):
            continue  # pid reused by another process
        paused[int(pid_text)] = entry
    running = [process for process in mine if process["pid"] not in paused and "T" not in process["stat"]]
    running.sort(key=lambda process: process["age_s"])  # newest first
    compilers_high = len(running) > max_compilers
    memory_high = memory_bad(headroom, settings)
    triggered = compilers_high or memory_high
    actions = []
    triggered_since = state.get("triggered_since")
    if triggered:
        triggered_since = triggered_since or now
        to_pause = []
        if compilers_high:
            to_pause = running[:len(running) - max_compilers]
        if memory_high:
            spare = max(len(running) - int(settings.get("min_running", 1)), 0)
            step = min(int(settings.get("pause_step", 2)), spare)
            for process in running[:step]:
                if process not in to_pause:
                    to_pause.append(process)
        reasons = []
        if compilers_high:
            reasons.append("%d compilers (max %d)" % (len(running), max_compilers))
        if memory_high:
            reasons.append(memory_text(headroom))
        reason = "; ".join(reasons)
        for process in to_pause:
            actions.append(("pause", process, reason))
            paused[process["pid"]] = {"name": process["name"], "start": now - process["age_s"], "since": now}
        terminate_after = settings.get("terminate_after_s")
        if terminate_after is not None and now - triggered_since >= float(terminate_after):
            for pid, entry in sorted(paused.items()):
                if now - entry["since"] >= float(terminate_after) and pid in by_pid:
                    actions.append(("terminate", by_pid[pid],
                                    "pressure sustained %ds" % int(now - triggered_since)))
            for verb, process, _ in actions:
                if verb == "terminate":
                    paused.pop(process["pid"], None)
    else:
        triggered_since = None
        if paused and memory_recovered(headroom, settings):
            room = max_compilers - len(running)
            step = min(int(settings.get("resume_step", 2)), room)
            oldest_first = sorted(paused.items(), key=lambda item: item[1]["start"])
            for pid, entry in oldest_first[:max(step, 0)]:
                actions.append(("resume", by_pid[pid], "pressure fell"))
                paused.pop(pid)
    new_state = {"paused": {str(pid): entry for pid, entry in paused.items()},
                 "triggered_since": triggered_since}
    return actions, new_state


def send_signal(pid, verb):
    if IS_WINDOWS:
        raise OSError("watchdog signals are not supported on Windows")
    if verb == "pause":
        os.kill(pid, signal.SIGSTOP)
    elif verb == "resume":
        os.kill(pid, signal.SIGCONT)


def command_guard(arguments):
    """Diagnostic only: what the watchdog rules would do to this user's compilers right now. Never signals."""
    project_root = find_project_root(os.getcwd())
    config = load_config(project_root)
    processes = list_processes()
    headroom = measure_headroom(config, processes)
    actions, _ = watchdog_decide(processes, headroom, {"paused": {}, "triggered_since": None}, config,
                                 time.time(), None if IS_WINDOWS else os.getuid())
    settings, max_compilers = watchdog_thresholds(config, headroom["cores"])
    print("sushirice guard (diagnostic, never signals): %s; watchdog max %d compilers per build" % (
        format_headroom(headroom), max_compilers))
    for verb, process, reason in actions:
        print("  a watchdog would %s pid %d (%s): %s" % (verb, process["pid"], process["name"], reason))
    if not actions:
        print("  no action")
    return 0


def install_exit_handlers():
    def stop(signum, frame):
        raise SystemExit(0)

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)


# ================================================================ report

def parse_ninja_log(text):
    """Return list of builds; each a list of entries {start, end, mtime, output, hash}.

    Entries are written in completion order, so end times rise within one build; a drop
    marks the start of a new build (the approach ninjatracing uses).
    """
    builds = []
    current = []
    last_end = -1
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        try:
            entry = {"start": int(fields[0]), "end": int(fields[1]), "mtime": int(fields[2]),
                     "output": fields[3], "hash": fields[4]}
        except ValueError:
            continue
        if entry["end"] < last_end and current:
            builds.append(current)
            current = []
        current.append(entry)
        last_end = entry["end"]
    if current:
        builds.append(current)
    return builds


def mtime_seconds(value):
    if value > 10 ** 14:
        return value / 1e9
    if value > 10 ** 11:
        return value / 1e3
    return float(value)


def is_object(output):
    return output.endswith((".o", ".obj", ".pch", ".gch"))


def source_dir_for(build_dir):
    cache = read_text(os.path.join(build_dir, "CMakeCache.txt")) or ""
    match = re.search(r"^CMAKE_HOME_DIRECTORY:INTERNAL=(.+)$", cache, re.MULTILINE)
    if match and os.path.isdir(match.group(1).strip()):
        return match.group(1).strip()
    return os.path.dirname(os.path.abspath(build_dir))


def changed_sources(source_dir, after, before):
    """Tracked source files whose mtime lies in (after, before]. None without git."""
    text = run_quiet(["git", "-C", source_dir, "ls-files", "-z"], timeout=20)
    if text is None or not text:
        return None
    changed = []
    for relative in text.split("\0"):
        if not relative or os.path.splitext(relative)[1].lower() not in SOURCE_EXTENSIONS:
            continue
        try:
            mtime = os.stat(os.path.join(source_dir, relative)).st_mtime
        except OSError:
            continue
        if after < mtime <= before:
            changed.append(relative)
    return changed


def analyze_ninja(build_dir, config):
    text = read_text(os.path.join(build_dir, ".ninja_log"))
    if text is None:
        return None
    builds = parse_ninja_log(text)
    if not builds:
        return {"builds": 0}
    last = builds[-1]
    settings = config["report"]
    objects = [entry for entry in last if is_object(entry["output"])]
    edges = sorted(last, key=lambda entry: entry["end"] - entry["start"], reverse=True)
    result = {
        "builds": len(builds),
        "edges": len(last),
        "objects_rebuilt": len(objects),
        "wall_s": round(max(entry["end"] for entry in last) / 1000.0, 1),
        "longest": [{"output": entry["output"], "s": round((entry["end"] - entry["start"]) / 1000.0, 1)}
                    for entry in edges[:int(settings.get("longest_edges", 8))]],
    }
    mtimes = [mtime_seconds(entry["mtime"]) for entry in last if entry["mtime"] > 0]
    previous_mtimes = [mtime_seconds(entry["mtime"]) for entry in builds[-2] if entry["mtime"] > 0] if len(builds) > 1 else []
    if mtimes and previous_mtimes:
        first_end_s = min(entry["end"] for entry in last) / 1000.0
        last_start = min(mtimes) - first_end_s
        previous_end = max(previous_mtimes)
        changed = changed_sources(source_dir_for(build_dir), previous_end, last_start + 1)
        if changed is not None:
            result["changed_sources"] = len(changed)
            result["changed_headers"] = len([p for p in changed if os.path.splitext(p)[1].lower() in HEADER_EXTENSIONS])
            result["changed_sample"] = changed[:5]
    return result


def parse_ccache_stats(text):
    """`ccache --print-stats` (tab separated) or `ccache -s` (3.x/4.x). Returns (hits, misses)."""
    if not text:
        return None
    values = {}
    for line in text.splitlines():
        if "\t" in line:
            key, _, value = line.partition("\t")
            if value.strip().isdigit():
                values[key.strip()] = int(value.strip())
    if values:
        hits = values.get("direct_cache_hit", 0) + values.get("preprocessed_cache_hit", 0)
        misses = values.get("cache_miss", 0)
        return hits, misses
    match = re.search(r"^\s*Hits:\s+(\d+)\s*/\s*(\d+)", text, re.MULTILINE)
    if match:
        hits, total = int(match.group(1)), int(match.group(2))
        return hits, total - hits
    hit_direct = re.search(r"cache hit \(direct\)\s+(\d+)", text)
    hit_pre = re.search(r"cache hit \(preprocessed\)\s+(\d+)", text)
    miss = re.search(r"cache miss\s+(\d+)", text)
    if miss:
        return (int(hit_direct.group(1)) if hit_direct else 0) + (int(hit_pre.group(1)) if hit_pre else 0), int(miss.group(1))
    return None


def ccache_stats(source_dir):
    env = dict(os.environ)
    if "CCACHE_DIR" not in env and source_dir and os.path.isdir(os.path.join(source_dir, ".ccache")):
        env["CCACHE_DIR"] = os.path.join(source_dir, ".ccache")
    text = run_quiet(["ccache", "--print-stats"], env=env)
    stats = parse_ccache_stats(text)
    if stats is None:
        stats = parse_ccache_stats(run_quiet(["ccache", "-s"], env=env))
    return stats, env.get("CCACHE_DIR")


def read_runs(project_root):
    runs = []
    path = os.path.join(state_dir(project_root, create=False), "runs.jsonl")
    for line in (read_text(path) or "").splitlines():
        try:
            runs.append(json.loads(line))
        except ValueError:
            continue
    return runs


def baseline_for(runs, run, config):
    """Median duration of earlier successful runs of the same command."""
    settings = config["report"]
    earlier = [r for r in runs if r != run and r.get("cmd") == run.get("cmd") and r.get("exit") == 0
               and not r.get("nested") and r.get("duration_s") and r.get("ts", "") <= run.get("ts", "")]
    earlier = earlier[-int(settings.get("baseline_runs", 10)):]
    if len(earlier) < 3:
        return None
    durations = sorted(r["duration_s"] for r in earlier)
    middle = len(durations) // 2
    if len(durations) % 2:
        return durations[middle]
    return (durations[middle - 1] + durations[middle]) / 2.0


def report_warnings(ninja, ccache_delta, run, baseline, config):
    settings = config["report"]
    warnings = []
    if ninja and ninja.get("objects_rebuilt") is not None:
        rebuilt = ninja["objects_rebuilt"]
        changed = ninja.get("changed_sources")
        if changed is not None:
            limit = max(int(settings.get("min_objects", 20)), int(settings.get("rebuild_ratio", 10)) * changed)
            if rebuilt > limit:
                if changed:
                    header_note = " (%d header%s)" % (ninja["changed_headers"], "" if ninja["changed_headers"] == 1 else "s") \
                        if ninja.get("changed_headers") else ""
                    warnings.append("rebuilt %d objects for %d changed file%s%s" % (
                        rebuilt, changed, "" if changed == 1 else "s", header_note))
                else:
                    warnings.append("rebuilt %d objects with no changed sources detected "
                                    "(flags, toolchain, generated header, or mtimes reset?)" % rebuilt)
    if ccache_delta is not None:
        hits, misses = ccache_delta
        total = hits + misses
        if total >= 10 and hits / float(total) < float(settings.get("ccache_min_hit_rate", 0.5)):
            warnings.append("ccache hit rate %d%% (%d of %d) since the last report" % (
                int(100 * hits / total), hits, total))
    if run is not None and baseline:
        if run.get("exit") == 0 and run.get("duration_s", 0) > float(settings.get("slow_factor", 1.5)) * baseline:
            warnings.append("build took %s vs baseline %s (%s)" % (
                format_duration(run["duration_s"]), format_duration(baseline), run.get("cmd", "")[:60]))
    return warnings


def format_duration(seconds):
    seconds = float(seconds)
    if seconds >= 90:
        return "%.1f min" % (seconds / 60.0)
    return "%.0f s" % seconds


def build_report(project_root, config, build_dir=None, run=None, update_ccache_state=True, include_trees=True):
    runs = read_runs(project_root)
    if run is None and runs:
        run = [r for r in runs if not r.get("timed_out")][-1] if [r for r in runs if not r.get("timed_out")] else None
    if build_dir is None and run is not None:
        build_dir = run.get("build_dir")
    report = {"build_dir": build_dir, "run": run}
    ninja = analyze_ninja(build_dir, config) if build_dir and os.path.isdir(build_dir) else None
    report["ninja"] = ninja
    ccache_delta = None
    cumulative = False
    if run_quiet(["ccache", "--version"]) is not None:
        stats, ccache_dir = ccache_stats(source_dir_for(build_dir) if build_dir else None)
        if stats is not None:
            key = ccache_dir or "default"
            state_path = os.path.join(state_dir(project_root, create=update_ccache_state), "ccache_last.json")
            previous = read_json(state_path, {})
            cumulative = False
            if key in previous:
                old_hits, old_misses = previous[key]
                if stats[0] >= old_hits and stats[1] >= old_misses:
                    ccache_delta = (stats[0] - old_hits, stats[1] - old_misses)
            else:
                ccache_delta = stats
                cumulative = True
            report["ccache"] = {"hits": stats[0], "misses": stats[1], "dir": ccache_dir,
                                "delta": ccache_delta}
            if update_ccache_state:
                previous[key] = list(stats)
                write_json(state_path, previous)
    baseline = baseline_for(runs, run, config) if run is not None else None
    report["baseline_s"] = baseline
    report["warnings"] = report_warnings(ninja, ccache_delta, run, baseline, config)
    if report.get("ccache") and cumulative:
        report["warnings"] = [w.replace("since the last report", "(all-time; deltas from the next report)")
                              for w in report["warnings"]]
    if include_trees:
        roots = list(tree_roots(config, project_root))
        if build_dir:
            roots.append(source_dir_for(build_dir))
        trees = census(roots, int((config.get("trees") or {}).get("max_depth", 3)))
        report["trees"] = {"count": len(trees["trees"]), "duplicates": trees["duplicates"], "shared": trees["shared"]}
        report["warnings"].extend(tree_warnings(trees, config))
    return report


def print_report(report):
    lines = []
    run = report.get("run")
    if run:
        lines.append("last run: %s  exit %s  %s%s" % (run.get("cmd", "")[:80], run.get("exit"),
                                                     format_duration(run.get("duration_s", 0)),
                                                     ("  (baseline %s)" % format_duration(report["baseline_s"]))
                                                     if report.get("baseline_s") else ""))
    ninja = report.get("ninja")
    if ninja:
        if ninja.get("builds"):
            changed = ninja.get("changed_sources")
            lines.append("ninja: %d edges, %d objects rebuilt, %s wall; changed sources: %s" % (
                ninja["edges"], ninja["objects_rebuilt"], format_duration(ninja["wall_s"]),
                "unknown (no git)" if changed is None else changed))
            for edge in ninja.get("longest", []):
                lines.append("  %6.1fs  %s" % (edge["s"], edge["output"]))
        else:
            lines.append("ninja: no entries in .ninja_log")
    elif report.get("build_dir"):
        lines.append("ninja: no .ninja_log in %s" % report["build_dir"])
    else:
        lines.append("ninja: no build dir (pass --build-dir, or run a build through `sushirice slot`)")
    ccache = report.get("ccache")
    if ccache:
        total = ccache["hits"] + ccache["misses"]
        lines.append("ccache: %d hits / %d calls (%s)" % (ccache["hits"], total,
                                                         ("%d%%" % (100 * ccache["hits"] // total)) if total else "-"))
    if report.get("trees") is not None:
        lines.append("trees: %d configured build tree%s, %d duplicate set%s (details: `sushirice trees`)" % (
            report["trees"]["count"], "" if report["trees"]["count"] == 1 else "s",
            len(report["trees"]["duplicates"]), "" if len(report["trees"]["duplicates"]) == 1 else "s"))
    for warning in report["warnings"]:
        lines.append("WARNING: " + warning)
    print("\n".join(lines))


def command_report(arguments):
    project_root = find_project_root(os.getcwd())
    config = load_config(project_root)
    build_dir = None
    if "--build-dir" in arguments:
        build_dir = os.path.abspath(arguments[arguments.index("--build-dir") + 1])
    report = build_report(project_root, config, build_dir, update_ccache_state="--no-state" not in arguments)
    if "--json" in arguments:
        print(json.dumps(report, indent=1))
    else:
        print_report(report)
    return 0


# ================================================================ status / monitor

def command_status(arguments):
    project_root = find_project_root(os.getcwd())
    config = load_config(project_root)
    headroom = measure_headroom(config)
    directories = slot_dirs(config)
    held = [d for d in directories if os.path.isdir(d) and not lock_is_stale(d)]
    allowed, why = headroom_allows(headroom, config, False)
    if "--json" in arguments:
        print(json.dumps({"headroom": headroom, "slots_held": len(held), "max_slots": config["max_slots"],
                          "foreign_held": len(foreign_holders(config)), "new_slot": allowed, "reason": why}))
    else:
        print("sushirice: %s; slots %d/%d; new slot: %s" % (format_headroom(headroom), len(held),
                                                           int(config["max_slots"]), why))
    return 0


def abnormal_reasons(headroom, config):
    settings, max_compilers = watchdog_thresholds(config, headroom.get("cores") or 1)
    reasons = []
    if headroom.get("compilers", 0) > max_compilers:
        reasons.append("%d compilers running (watchdog max %d per build)" % (headroom["compilers"], max_compilers))
    if memory_bad(headroom, settings):
        reasons.append(memory_text(headroom))
    load = headroom.get("load")
    if load is not None and load / headroom["cores"] > 1.5:
        reasons.append("load %.1f on %d cores" % (load, headroom["cores"]))
    return reasons


def command_monitor(arguments):
    project_root = find_project_root(os.getcwd())
    config = load_config(project_root)
    role = os.environ.get("SUSHI_ROLE")
    if role and role not in (config.get("monitor_roles") or []):
        return 0
    runs_path = os.path.join(state_dir(project_root, create=False), "runs.jsonl")
    try:
        offset = os.path.getsize(runs_path)
    except OSError:
        offset = 0
    interval = float(config.get("monitor_interval_s", 10))
    status_every = float(config.get("monitor_status_every_s", 300))
    last_status = 0.0
    was_abnormal = False
    trees_every = float((config.get("trees") or {}).get("every_s", 600))
    last_trees = None
    last_tree_warnings = set()
    install_exit_handlers()

    def emit(text):
        sys.stdout.write("sushirice: " + text + "\n")
        sys.stdout.flush()

    try:
        while True:
            try:
                now = time.time()
                headroom = measure_headroom(config)
                reasons = abnormal_reasons(headroom, config)
                if reasons:
                    if not was_abnormal or now - last_status >= status_every:
                        emit("%s (%s)" % ("; ".join(reasons), format_headroom(headroom)))
                        last_status = now
                    was_abnormal = True
                elif was_abnormal:
                    emit("back to normal (%s)" % format_headroom(headroom))
                    was_abnormal = False
                try:
                    size = os.path.getsize(runs_path)
                except OSError:
                    size = 0
                if size > offset:
                    with open(runs_path, encoding="utf-8") as handle:
                        handle.seek(offset)
                        fresh = handle.read()
                    offset = size
                    for line in fresh.splitlines():
                        try:
                            run = json.loads(line)
                        except ValueError:
                            continue
                        if run.get("timed_out"):
                            emit("slot wait timed out for %s" % run.get("cmd", "")[:60])
                            continue
                        if run.get("nested"):
                            continue
                        watchdog = run.get("watchdog") or {}
                        if watchdog.get("paused") or watchdog.get("terminated") or watchdog.get("released"):
                            emit("watchdog on %s: %d paused, %d resumed%s%s" % (
                                run.get("cmd", "")[:60], watchdog.get("paused", 0), watchdog.get("resumed", 0),
                                ", build terminated" if watchdog.get("terminated") else "",
                                (", released: " + watchdog["released"]) if watchdog.get("released") else ""))
                        report = build_report(project_root, config, run=run, include_trees=False)
                        for warning in report["warnings"]:
                            emit(warning)
                elif size < offset:
                    offset = size
                if trees_every > 0 and (last_trees is None or now - last_trees >= trees_every):
                    last_trees = now
                    result = census(tree_roots(config, project_root),
                                    int((config.get("trees") or {}).get("max_depth", 3)))
                    current = set(tree_warnings(result, config))
                    for warning in sorted(current - last_tree_warnings):
                        emit(warning + " (see `sushirice trees`)")
                    last_tree_warnings = current
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:  # keep monitoring; log the fault
                log(project_root, "monitor error: %r" % (error,))
            time.sleep(interval)
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


# ================================================================ trees

SKIP_WALK = {".git", "node_modules", "CMakeFiles", "_deps", "__pycache__", ".ccache", ".cache", "Library"}
OUTPUT_KEY = re.compile(r"(^CMAKE_INSTALL_PREFIX$|^CPACK_PACKAGE_DIRECTORY$|_PACKAGE_DIR$|_PACKAGES_DIR$|"
                        r"_INSTALL_DIR$|_OUTPUT_DIRECTORY$|_OUTPUT_DIR$|^FETCHCONTENT_BASE_DIR$|_STAGING_DIR$)")
DEFAULT_PREFIXES = {"/usr/local", "C:/Program Files", "C:/Program Files (x86)", "/usr"}
SANITIZER_FLAG = re.compile(r"-fsanitize=([\w,-]+)|/fsanitize=(\w+)")


def parse_cmake_cache(text, types=None):
    """KEY:TYPE=VALUE lines -> {key: value}; fills `types` ({key: TYPE}) when given."""
    values = {}
    for line in (text or "").splitlines():
        if not line or line.startswith(("#", "//")):
            continue
        match = re.match(r"^([^:=]+)(?::([A-Z]+))?=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(3).strip()
            if types is not None:
                types[match.group(1)] = match.group(2) or ""
    return values


def normalize_path(path):
    return os.path.normcase(os.path.realpath(path)) if path else path


def describe_cache(build_dir, values, types=None):
    flags = " ".join(value for key, value in values.items()
                     if key.startswith(("CMAKE_C_FLAGS", "CMAKE_CXX_FLAGS", "CMAKE_EXE_LINKER_FLAGS")))
    sanitizers = set()
    for match in SANITIZER_FLAG.finditer(flags):
        sanitizers.update(part for part in (match.group(1) or match.group(2)).split(",") if part)
    for key, value in values.items():
        if "SANITIZ" in key.upper() and key.upper() != "CMAKE_SANITIZERS_HELP":
            if value.upper() in ("ON", "TRUE", "1", "YES"):
                sanitizers.add(key)
            elif value and value.upper() not in ("OFF", "FALSE", "0", "NO", "") and not os.path.isabs(value):
                sanitizers.update(part for part in re.split(r"[;,]", value) if part)
    outputs = {}
    for key, value in values.items():
        if OUTPUT_KEY.search(key) and value and os.path.isabs(value) and value.rstrip("/") not in DEFAULT_PREFIXES:
            outputs[key] = value
    dir_refs = {key: value for key, value in values.items()
                if key.endswith("_DIR") and value and os.path.isabs(value) and key not in outputs}
    newest = 0.0
    for name in (".ninja_log", "build.ninja", "CMakeCache.txt", "Makefile"):
        try:
            newest = max(newest, os.stat(os.path.join(build_dir, name)).st_mtime)
        except OSError:
            pass
    # Project BOOL options (FOO_ENABLE_X=ON/OFF) tell deliberate A/B trees apart from true duplicates.
    options = sorted("%s=%s" % (key, value.upper()) for key, value in values.items()
                     if (types or {}).get(key) == "BOOL" and not key.startswith(("CMAKE_", "CPACK_", "FETCHCONTENT_")))
    return {
        "build_dir": build_dir,
        "options": options,
        "source": values.get("CMAKE_HOME_DIRECTORY", ""),
        "generator": values.get("CMAKE_GENERATOR", ""),
        "build_type": values.get("CMAKE_BUILD_TYPE", "") or "(none)",
        "sanitizers": sorted(sanitizers),
        "compiler": os.path.basename(values.get("CMAKE_CXX_COMPILER", "") or values.get("CMAKE_C_COMPILER", "")),
        "outputs": outputs,
        "dir_refs": dir_refs,
        "modified": newest,
    }


def find_build_dirs(roots, max_depth=3):
    """Directories holding CMakeCache.txt, at most max_depth below each root. Doesn't descend into one."""
    found = []
    seen = set()
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        stack = [(root, 0)]
        while stack:
            directory, depth = stack.pop()
            key = normalize_path(directory)
            if key in seen:
                continue
            seen.add(key)
            if os.path.isfile(os.path.join(directory, "CMakeCache.txt")):
                found.append(directory)
                continue
            if depth >= max_depth:
                continue
            try:
                entries = sorted(os.listdir(directory))
            except OSError:
                continue
            for name in entries:
                if name in SKIP_WALK or (name.startswith(".") and name not in (".build",)):
                    continue
                child = os.path.join(directory, name)
                if os.path.isdir(child) and not os.path.islink(child):
                    stack.append((child, depth + 1))
    return found


def read_build_tree(build_dir):
    types = {}
    values = parse_cmake_cache(read_text(os.path.join(build_dir, "CMakeCache.txt")), types)
    return describe_cache(build_dir, values, types)


def parse_worktree_list(text):
    worktrees = []
    current = None
    for line in (text or "").splitlines():
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):], "branch": None, "head": None, "bare": False}
            worktrees.append(current)
        elif current is not None and line.startswith("branch "):
            current["branch"] = line[len("branch "):].replace("refs/heads/", "", 1)
        elif current is not None and line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif current is not None and line == "detached":
            current["branch"] = "(detached)"
        elif current is not None and line == "bare":
            current["bare"] = True
    return worktrees


def git_worktrees(directory):
    """(common git dir, worktrees) for the repo holding directory, or (None, [])."""
    if not directory or not os.path.isdir(directory):
        return None, []
    common = run_quiet(["git", "-C", directory, "rev-parse", "--path-format=absolute", "--git-common-dir"], timeout=10)
    if not common or not common.strip() or common.strip().startswith("--"):
        common = run_quiet(["git", "-C", directory, "rev-parse", "--git-common-dir"], timeout=10)  # git < 2.31
        if not common or not common.strip():
            return None, []
        common = os.path.join(directory, common.strip())
    worktrees = parse_worktree_list(run_quiet(["git", "-C", directory, "worktree", "list", "--porcelain"], timeout=10))
    return normalize_path(common.strip()), [w for w in worktrees if not w["bare"]]


def tree_signature(tree):
    return (normalize_path(tree["source"]), tree["generator"], tree["build_type"].lower(),
            ",".join(tree["sanitizers"]), tree["compiler"], ";".join(tree.get("options") or []))


def census(roots, max_depth=3):
    trees = [read_build_tree(directory) for directory in find_build_dirs(roots, max_depth)]
    by_source = {}
    for tree in trees:
        by_source.setdefault(normalize_path(tree["source"]) or "(unknown)", []).append(tree)
    duplicates = []
    signatures = {}
    for tree in trees:
        signatures.setdefault(tree_signature(tree), []).append(tree["build_dir"])
    for signature, directories in signatures.items():
        if len(directories) > 1:
            duplicates.append({"source": signature[0], "config": describe_signature(signature), "dirs": directories})
    shared = []
    users = {}
    build_roots = [normalize_path(tree["build_dir"]) for tree in trees]
    for tree in trees:
        for key, value in tree["outputs"].items():
            users.setdefault(normalize_path(value), []).append((tree["build_dir"], key))
        own = normalize_path(tree["build_dir"])
        for key, value in tree["dir_refs"].items():
            target = normalize_path(value)
            for other in build_roots:
                if other != own and (target == other or target.startswith(other + os.sep)):
                    shared.append({"path": value, "users": [(tree["build_dir"], key)],
                                   "why": "points into another build tree (%s)" % other})
    for path, holders in users.items():
        if len(set(directory for directory, _ in holders)) > 1:
            shared.append({"path": path, "users": holders, "why": "output/package dir shared by %d trees" % len(holders)})
    repos = {}
    for source in by_source:
        if source == "(unknown)":
            continue
        common, worktrees = git_worktrees(source if os.path.isdir(source) else None)
        if common and common not in repos:
            repos[common] = worktrees
    for root in roots:
        common, worktrees = git_worktrees(os.path.abspath(os.path.expanduser(root)))
        if common and common not in repos:
            repos[common] = worktrees
    return {"trees": trees, "by_source": by_source, "duplicates": duplicates, "shared": shared, "repos": repos}


def describe_signature(signature):
    _, generator, build_type, sanitizers, compiler = signature[:5]
    parts = [generator or "?", build_type or "(none)"]
    parts.append(("sanitize " + sanitizers) if sanitizers else "no sanitizer")
    if compiler:
        parts.append(compiler)
    return ", ".join(parts)


def tree_warnings(result, config):
    settings = config.get("trees") or {}
    per_source = int(settings.get("max_per_source", 2))
    warnings = []
    duplicate_count = {}
    for duplicate in result["duplicates"]:
        duplicate_count[duplicate["source"]] = duplicate_count.get(duplicate["source"], 0) + len(duplicate["dirs"]) - 1
    for source, trees in sorted(result["by_source"].items()):
        duplicates = duplicate_count.get(source, 0)
        if len(trees) > per_source or duplicates:
            warnings.append("%d build trees for one source (%s); %d duplicate%s" % (
                len(trees), source, duplicates, "" if duplicates == 1 else "s"))
    for item in result["shared"]:
        warnings.append("%s: %s" % (item["path"], item["why"]))
    max_worktrees = settings.get("max_worktrees_per_repo")
    if max_worktrees:
        for common, worktrees in result["repos"].items():
            if len(worktrees) > int(max_worktrees):
                warnings.append("%d worktrees of one repo (%s)" % (len(worktrees), common))
    return warnings


def tree_roots(config, project_root, explicit=None):
    if explicit:
        return explicit
    configured = (config.get("trees") or {}).get("roots")
    if configured:
        return [os.path.expanduser(root) for root in configured]
    if project_root:
        return [project_root]
    return [os.getcwd()]


def format_time(timestamp):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp)) if timestamp else "-"


def command_trees(arguments):
    project_root = find_project_root(os.getcwd())
    config = load_config(project_root)
    roots = []
    index = 0
    while index < len(arguments):
        if arguments[index] == "--root" and index + 1 < len(arguments):
            roots.append(os.path.abspath(arguments[index + 1]))
            index += 2
            continue
        index += 1
    depth = int((config.get("trees") or {}).get("max_depth", 3))
    result = census(tree_roots(config, project_root, roots), depth)
    warnings = tree_warnings(result, config)
    if "--json" in arguments:
        print(json.dumps({"trees": result["trees"], "duplicates": result["duplicates"], "shared": result["shared"],
                          "repos": result["repos"], "warnings": warnings}, indent=1))
        return 0
    lines = []
    for common, worktrees in sorted(result["repos"].items()):
        lines.append("repo %s: %d worktree%s" % (common, len(worktrees), "" if len(worktrees) == 1 else "s"))
        for worktree in worktrees:
            lines.append("  %s  [%s]" % (worktree["path"], worktree["branch"] or "?"))
    for source, trees in sorted(result["by_source"].items()):
        lines.append("source %s: %d build tree%s" % (source, len(trees), "" if len(trees) == 1 else "s"))
        for tree in sorted(trees, key=lambda t: t["build_dir"]):
            lines.append("  %-50s %s  modified %s" % (tree["build_dir"], describe_signature(tree_signature(tree)),
                                                     format_time(tree["modified"])))
    if not result["trees"]:
        lines.append("no configured build trees under %s" % ", ".join(tree_roots(config, project_root, roots)))
    for duplicate in result["duplicates"]:
        lines.append("DUPLICATE (%s): %s" % (duplicate["config"], ", ".join(duplicate["dirs"])))
    for item in result["shared"]:
        lines.append("SHARED %s: %s (%s)" % (item["path"], item["why"],
                                             ", ".join("%s via %s" % (d, k) for d, k in item["users"])))
    for warning in warnings:
        lines.append("WARNING: " + warning)
    print("\n".join(lines))
    return 1 if "--gate" in arguments and warnings else 0


# ================================================================ hook

CONTROL_OPERATORS = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")", "\n"}
COMMAND_PREFIXES = {"env", "nice", "time", "nohup", "caffeinate", "exec", "command", "sudo", "taskpolicy",
                    "stdbuf", "timeout", "gtimeout", "xcrun"}
PYTHONS = re.compile(r"^(python|python3|python3\.\d+|py|pypy3?)(\.exe)?$", re.IGNORECASE)
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
SHELL_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "}"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(command):
    """Drop here-document bodies, which are data, not commands."""
    lines = command.split("\n")
    kept = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        delimiters = [match.group(2) for match in HEREDOC.finditer(line)]
        index += 1
        for delimiter in delimiters:
            while index < len(lines) and lines[index].strip() != delimiter:
                index += 1
            index += 1
    return "\n".join(kept)


def split_segments(command):
    """Split a shell command line into simple-command word lists."""
    command = strip_heredocs(command.replace("\\\n", " "))
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = "#"
    segments = []
    current = []
    try:
        tokens = list(lexer)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if token in CONTROL_OPERATORS or set(token) <= set(";&|()\n") and token:
            if current:
                segments.append(current)
            current = []
            continue
        if "\n" in token:
            parts = token.split("\n")
            for index, part in enumerate(parts):
                if part:
                    current.append(part)
                if index < len(parts) - 1 and current:
                    segments.append(current)
                    current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def strip_prefixes(words):
    """Drop env assignments and wrappers like nice/time/env; return the remaining words."""
    index = 0
    while index < len(words):
        word = words[index]
        if word in SHELL_KEYWORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            index += 1
            continue
        base = os.path.basename(word)
        if base in COMMAND_PREFIXES:
            index += 1
            while index < len(words) and words[index].startswith("-"):
                option = words[index]
                index += 1
                if base in ("nice", "timeout", "gtimeout", "taskpolicy", "sudo", "stdbuf") and option in ("-n", "-c", "-u", "-s", "-k", "-o", "-e", "-i") \
                        and index < len(words):
                    index += 1
            if base in ("timeout", "gtimeout") and index < len(words) and re.match(r"^\d", words[index]):
                index += 1
            continue
        break
    return words[index:]


def program_of(words):
    """(program basename, rest) where a python invocation's program is its script."""
    if not words:
        return None, []
    base = os.path.basename(words[0])
    if PYTHONS.match(base):
        index = 1
        while index < len(words) and words[index].startswith("-"):
            if words[index] in ("-m", "-c"):
                if words[index] == "-m" and index + 1 < len(words):
                    return words[index + 1], words[index + 2:]
                return base, words[index:]
            index += 1
        if index < len(words):
            return os.path.basename(words[index]), words[index + 1:]
        return base, []
    return base, words[1:]


def build_kind(program, rest, config):
    """Name of the build this program runs, or None."""
    if program is None:
        return None
    lowered = program.lower()
    if lowered in ("cmake", "cmake.exe"):
        return "cmake --build" if "--build" in rest else None
    if lowered in [name.lower() for name in config["hook_build_programs"]]:
        if lowered == "ninja" and ("-n" in rest or "-t" in rest or "--version" in rest or "-h" in rest):
            return None
        if lowered in ("make", "gmake") and set(rest) & {"-n", "--dry-run", "--just-print", "--version", "-v", "-q", "--question", "-h", "--help"}:
            return None
        if lowered == "xcodebuild" and set(rest) & {"-list", "-showBuildSettings", "-version", "-showsdks", "-help", "-usage", "-showdestinations"}:
            return None
        if lowered.startswith("msbuild") and set(r.lower() for r in rest) & {"/version", "-version", "/?", "-help"}:
            return None
        return program
    for pattern in config["hook_build_scripts"]:
        if fnmatch.fnmatch(program, pattern):
            if "--help" in rest or "-h" in rest:
                return None
            return program
    return None


def find_unwrapped_builds(command, config, depth=0):
    """Builds in a Bash command line that no slot wrapper covers."""
    found = []
    if depth > 3:
        return found
    wrappers = config["hook_wrappers"]
    for segment in split_segments(command):
        words = strip_prefixes(segment)
        program, rest = program_of(words)
        if program is None:
            continue
        if program in wrappers or os.path.splitext(program)[0] in wrappers:
            continue
        if program in SHELLS and "-c" in rest:
            position = rest.index("-c")
            if position + 1 < len(rest):
                found.extend(find_unwrapped_builds(rest[position + 1], config, depth + 1))
            continue
        if program == "ssh":
            remote = ssh_remote_command(rest)
            if remote:
                found.extend(find_unwrapped_builds(remote, config, depth + 1))
            continue
        if program == "xargs":
            index = 0
            while index < len(rest) and rest[index].startswith("-"):
                index += 2 if rest[index] in ("-n", "-P", "-I", "-L", "-s", "-d", "-E") else 1
            program, rest = program_of(strip_prefixes(rest[index:]))
        kind = build_kind(program, rest, config)
        if kind:
            found.append(kind)
    return found


def ssh_remote_command(rest):
    with_value = set("bcDEeFIiJLlmOopQRSWw")
    index = 0
    while index < len(rest):
        word = rest[index]
        if word.startswith("-") and len(word) > 1:
            if word[-1] in with_value and len(word) == 2:
                index += 2
            else:
                index += 1
            continue
        break
    remainder = rest[index + 1:]  # skip the host
    return " ".join(remainder) if remainder else None


def resolve(path, cwd):
    path = os.path.expanduser(path)
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path))


def parse_git_invocation(rest, cwd):
    """(directory git runs in, subcommand words) for `git [global options] sub ...`."""
    index = 0
    directory = cwd
    while index < len(rest) and rest[index].startswith("-"):
        option = rest[index]
        if option == "-C" and index + 1 < len(rest):
            directory = resolve(rest[index + 1], directory)
            index += 2
        elif option in ("-c", "--git-dir", "--work-tree", "--namespace") and index + 1 < len(rest):
            index += 2
        else:
            index += 1
    return directory, rest[index:]


def parse_cmake_configure(rest, cwd):
    """Describe a CMake configure: {source, build, new, build_type, sanitizers}, or None."""
    if not rest or set(rest) & {"--build", "--install", "-E", "-P", "--help", "--version", "-h", "--open",
                                "--workflow", "--list-presets"}:
        return None
    source = build = None
    defines = {}
    preset = False
    index = 0
    while index < len(rest):
        word = rest[index]
        value = None
        for flag in ("-S", "-B", "-D"):
            if word == flag and index + 1 < len(rest):
                value = rest[index + 1]
                index += 1
            elif word.startswith(flag) and len(word) > 2:
                value = word[2:]
            if value is not None:
                if flag == "-S":
                    source = resolve(value, cwd)
                elif flag == "-B":
                    build = resolve(value, cwd)
                else:
                    key, _, setting = value.partition("=")
                    defines[key.split(":")[0]] = setting
                break
        else:
            if word in ("-G", "-T", "-A", "-C", "--toolchain", "--install-prefix", "--graphviz") and index + 1 < len(rest):
                index += 1
            elif word == "--preset" or word.startswith("--preset="):
                preset = True
                if word == "--preset":
                    index += 1
            elif not word.startswith("-"):
                path = resolve(word, cwd)
                if os.path.isfile(os.path.join(path, "CMakeCache.txt")):
                    build = build or path
                else:
                    source = source or path
        index += 1
    if build is None:
        if preset:
            return None  # binary dir comes from the preset
        build = cwd
    if source is None:
        cache = parse_cmake_cache(read_text(os.path.join(build, "CMakeCache.txt")))
        source = cache.get("CMAKE_HOME_DIRECTORY") or cwd
    sanitizers = sorted(key for key, value in defines.items()
                        if "SANITIZ" in key.upper() and value.upper() in ("ON", "TRUE", "1", "YES"))
    for key, value in defines.items():
        if key.startswith(("CMAKE_C_FLAGS", "CMAKE_CXX_FLAGS")):
            for match in SANITIZER_FLAG.finditer(value):
                sanitizers.extend(part for part in (match.group(1) or match.group(2)).split(",") if part)
    return {"source": source, "build": build, "new": not os.path.isfile(os.path.join(build, "CMakeCache.txt")),
            "build_type": defines.get("CMAKE_BUILD_TYPE", ""), "sanitizers": sorted(set(sanitizers))}


def short_tree(tree):
    return "%s (%s)" % (tree["build_dir"], describe_signature(tree_signature(tree)))


def tree_findings(command, cwd, config, depth=0):
    """Reminders for churned trees: `git worktree add`, and CMake configures into a new build dir."""
    findings = []
    if depth > 3:
        return findings
    for segment in split_segments(command):
        words = strip_prefixes(segment)
        program, rest = program_of(words)
        if program is None:
            continue
        if program in ("cd", "pushd") and rest:
            if rest[0] != "-":
                cwd = resolve(rest[0], cwd)
            continue
        if program in SHELLS and "-c" in rest:
            position = rest.index("-c")
            if position + 1 < len(rest):
                findings.extend(tree_findings(rest[position + 1], cwd, config, depth + 1))
            continue
        if program in ("git", "git.exe"):
            directory, sub = parse_git_invocation(rest, cwd)
            if len(sub) >= 2 and sub[0] == "worktree" and sub[1] == "add":
                finding = worktree_finding(directory, sub[2:], cwd)
                if finding:
                    findings.append(finding)
            continue
        if program.lower() in ("cmake", "cmake.exe"):
            configure = parse_cmake_configure(rest, cwd)
            if configure and configure["new"]:
                finding = configure_finding(configure, config)
                if finding:
                    findings.append(finding)
    return findings


def worktree_finding(directory, arguments, cwd):
    common, worktrees = git_worktrees(directory if os.path.isdir(directory) else None)
    if not common or not worktrees:
        return None
    described = []
    for worktree in worktrees[:4]:
        builds = [os.path.basename(d) for d in find_build_dirs([worktree["path"]], 2)]
        described.append("%s [%s]%s" % (worktree["path"], worktree["branch"] or "?",
                                         ("; build trees: " + ", ".join(builds[:4])) if builds else ""))
    if len(worktrees) > 4:
        described.append("and %d more" % (len(worktrees) - 4))
    text = ("SushiRice: `git worktree add` adds another checkout of %s, which has %d worktree%s: %s. "
            "A new tree rebuilds from scratch; reusing an existing worktree of your own keeps builds "
            "incremental." % (
                common, len(worktrees), "" if len(worktrees) == 1 else "s", "; ".join(described)))
    return {"key": "worktree:" + common, "kind": "trees", "text": text}


def configure_finding(configure, config):
    source = normalize_path(configure["source"])
    new_build = normalize_path(configure["build"])
    depth = int((config.get("trees") or {}).get("hook_depth", 2))
    roots = [configure["source"], os.path.dirname(configure["build"])]
    existing = []
    for directory in find_build_dirs([r for r in roots if os.path.isdir(r)], depth):
        if normalize_path(directory) == new_build:
            continue
        tree = read_build_tree(directory)
        if normalize_path(tree["source"]) == source:
            existing.append(tree)
    if not existing:
        return None
    same = [tree for tree in existing
            if configure["build_type"] and tree["build_type"].lower() == configure["build_type"].lower()
            and tree["sanitizers"] == configure["sanitizers"]]
    names = "; ".join(short_tree(tree) for tree in existing[:4])
    text = ("SushiRice: this configures a new build tree %s for %s, which already has configured tree%s: %s. "
            % (configure["build"], configure["source"], "" if len(existing) == 1 else "s", names))
    if same:
        text += "%s already has the same configuration. " % same[0]["build_dir"]
    text += ("A new tree rebuilds from scratch, and trees sharing a package dir overwrite each other; reusing "
             "an existing tree keeps builds incremental.")
    return {"key": "configure:" + source, "kind": "trees", "text": text}


def command_hook():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if event.get("tool_name") not in (None, "Bash"):
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    if not command.strip():
        return 0
    cwd = event.get("cwd") or os.getcwd()
    project_root = find_project_root(cwd)
    config = load_config(project_root)
    modes = {"build": config.get("hook_mode", "remind"), "trees": config.get("hook_trees_mode", "remind")}
    findings = []
    if modes["build"] != "off" and not any(os.environ.get(v) == "1" for v in config["nesting_vars"]):
        kinds = sorted(set(find_unwrapped_builds(command, config)))
        if kinds:
            wrapper = "python3 %s slot --" % shlex.quote(SCRIPT_PATH)
            findings.append({"key": "build:" + ",".join(kinds), "kind": "build", "text": (
                "SushiRice: this command runs a build (%s) outside a build slot. On this machine, builds run "
                "through `%s <cmd>`, build_slot.py, or m4ws, so parallel builds "
                "wait for headroom instead of overloading the Mac." % (", ".join(kinds), wrapper))})
    if modes["trees"] != "off":
        findings.extend(tree_findings(command, cwd, config))
    if not findings:
        return 0
    denied = [finding for finding in findings if modes[finding["kind"]] == "deny"]
    if denied:
        log(project_root, "hook denied: %s :: %s" % (", ".join(f["key"] for f in denied), command[:200]))
        reason = " ".join(finding["text"] for finding in denied) + " Re-run it wrapped, or reuse the existing tree."
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
        return 0
    state_path = os.path.join(state_dir(project_root), "hook_reminded.json")
    state = read_json(state_path, {})
    session = event.get("session_id") or "unknown"
    now = time.time()
    state = {key: value for key, value in state.items()
             if isinstance(value, dict) and now - max(list(value.values()) or [0]) < 2 * 86400}
    reminded = state.setdefault(session, {})
    every = float(config.get("hook_remind_every_s", 1800))
    fresh = [finding for finding in findings if now - reminded.get(finding["key"], 0) >= every]
    if not fresh:
        return 0
    for finding in fresh:
        reminded[finding["key"]] = now
    write_json(state_path, state)
    log(project_root, "hook reminded: %s :: %s" % (", ".join(f["key"] for f in fresh), command[:200]))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "additionalContext": "\n".join(f["text"] for f in fresh)}}))
    return 0


# ================================================================ main

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    command, arguments = argv[0], argv[1:]
    if command == "hook":
        try:
            return command_hook()
        except Exception as error:  # a hook must never break the session
            log(None, "hook error: %r" % (error,))
            return 0
    handlers = {"slot": command_slot, "guard": command_guard, "report": command_report, "trees": command_trees,
                "status": command_status, "monitor": command_monitor}
    if command in handlers:
        return handlers[command](arguments)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
