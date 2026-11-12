#!/usr/bin/env python3
"""SushiWrap: seats, host-reset deadlines and portable host state.

A seat is a worker record: the worker's role, track, branch, worktree and latest resume
note, kept in a file so any session can pick the work up. A deadline says when this host resets. Survey/provision describe a build
host so it can be rebuilt elsewhere.

Subcommands
  seat list                         all seats, the claiming host and resume note age
  seat show SEAT                    the seat file
  seat init SEAT [fields]           create a seat file
  seat set SEAT [fields]            change header fields
  seat take SEAT [--by NAME]        claim it for this host, print a resume brief
  seat pointer SEAT --text T | --file F [--append-track]
                                    replace the seat's resume note
  seat release [SEAT]               clear the claim (default $SUSHI_SEAT)
     fields: --role R --track F --branch B --worktree [HOST=]PATH --owned P (repeat)
             --ticket T (repeat) --manifest REF
  deadline set (--at ISO | --in 45m | --from-boot 8h) [--scope host|all] [--note T]
  deadline show | clear
  monitor [--interval S] [--once]   background monitor: checklist 30, 15 and 5 min before the reset
  hook                              PreToolUse hook: "N min left" once per 5-min bucket
  session-start                     SessionStart hook: renew boot-based deadlines
  shutdown-check [--seat S] [--max-age MIN]
  shutdown-template [--reason TEXT] a draft shutdown entry for STATE.md (stdout only)
  survey [--host H] [--out FILE] [--json] [--anonymize]
  provision --manifest FILE         print a step plan to recreate a surveyed host

Global: --project DIR, before or after the subcommand (default: $CLAUDE_PROJECT_DIR, else the
        nearest .sushi/.git ancestor). .sushi/ is not created in a tree wasabi marks shared.
Config: $SUSHIWRAP_CONFIG, else ~/.sushi/sushiwrap.json overlaid by
<project>/.sushi/sushiwrap.json. Standard library only; Python 3.9+.
"""

# The hook path must start fast: only these four modules load at import time.
import json
import os
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = 'python3 "%s"' % os.path.abspath(__file__)

DEFAULT_CONFIG = {
    "seats_dir": ".sushi/seats",
    "tracks_root": ".",
    "deadline_file": ".sushi/state/sushiwrap/deadline.json",
    "state_file": None,
    "auto_from_boot": None,
    "thresholds_min": [30, 15, 5],
    "hook_window_min": 30,
    "hook_bucket_min": 5,
    "pointer_max_age_min": 45,
    "push_allowed": False,
    "memory_mirror": [],
    "pointer_heading": "Resume note",
    "pointer_skip": r"\b(previous|superseded|old)\b",
    "shutdown_heading": r"\b(SHUTDOWN|STOPPED|PAUSE)\b",
    "survey": {},
    "hosts": {},
}

# Seat files and track files written before the rename use this heading; it is still read.
LEGACY_POINTER_HEADING = "RESUME POINTER"

SEAT_FIELDS = ["seat", "role", "track_file", "branch", "worktree", "owned_paths", "open_tickets",
               "host_manifest", "holder", "updated_at", "pointer_updated_at"]
EXPIRED_GRACE_S = 3600
BOOT_TOLERANCE_S = 120


# ---------------------------------------------------------------- small utilities

def now_epoch():
    override = os.environ.get("SUSHIWRAP_NOW")
    return float(override) if override else time.time()


def hostname():
    if hasattr(os, "uname"):
        name = os.uname().nodename
    else:
        name = os.environ.get("COMPUTERNAME", "")
    return name.split(".")[0].lower()


def iso_utc(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def local_hm(epoch):
    return time.strftime("%H:%M", time.localtime(epoch))


def parse_iso(text):
    """Parse an ISO time to epoch seconds. A time without an offset is local time."""
    import datetime
    value = text.strip()
    if value.endswith("Z") or value.endswith("z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return time.mktime(parsed.timetuple()) + parsed.microsecond / 1e6
    return parsed.timestamp()


def parse_duration(text):
    """'45m', '8h', '1h30m', '90s', '2d', or a bare number of minutes -> seconds."""
    import re
    value = text.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value) * 60
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", value)
    if not parts or "".join(n + u for n, u in parts) != value.replace(" ", ""):
        raise ValueError("bad duration %r (use e.g. 45m, 8h, 1h30m)" % text)
    scale = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(float(number) * scale[unit] for number, unit in parts)


def human_age(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return "%d s" % seconds
    if seconds < 5400:
        return "%d min" % round(seconds / 60.0)
    if seconds < 172800:
        return "%.1f h" % (seconds / 3600.0)
    return "%d days" % round(seconds / 86400.0)


# The project root this process works for; directories under its .sushi/ are created only through
# ensure_dir, which refuses to create .sushi/ in a shared working tree (sushitree.py).
CURRENT_ROOT = None
_SUSHITREE = None


def sushitree():
    """This plugin's copy of sushitree.py, loaded by path on first use (hooks stay fast)."""
    global _SUSHITREE
    if _SUSHITREE is None:
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sushitree.py")
        spec = importlib.util.spec_from_file_location("sushiwrap_sushitree", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SUSHITREE = module
    return _SUSHITREE


def ensure_dir(directory):
    """Create directory; raises sushitree.SharedTreeError for a new .sushi/ in a shared working tree."""
    if os.path.isdir(directory):
        return
    if CURRENT_ROOT:
        sushitree().check_sushi_creation(CURRENT_ROOT, directory)
    os.makedirs(directory, exist_ok=True)


def sushi_base(project_root):
    """<project>/.sushi, or ~/.sushi when the project's .sushi/ may not be created (a shared tree)."""
    home = os.path.join(os.path.expanduser("~"), ".sushi")
    if not project_root:
        return home
    base = os.path.join(project_root, ".sushi")
    if os.path.isdir(base):
        return base
    try:
        sushitree().check_sushi_creation(project_root, base)
    except sushitree().SharedTreeError:
        return home
    return base


def atomic_write(path, text):
    directory = os.path.dirname(path)
    if directory:
        ensure_dir(directory)
    temporary = "%s.tmp.%d" % (path, os.getpid())
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, path)


def log(project_root, message):
    try:
        directory = os.path.join(sushi_base(project_root), "logs")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "sushiwrap.log"), "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))
    except OSError:
        pass


# ---------------------------------------------------------------- project and config

def find_project_root(start=None):
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if start is None and env:
        return os.path.abspath(env)
    directory = os.path.abspath(start or os.getcwd())
    probe = directory
    while True:
        if os.path.isdir(os.path.join(probe, ".sushi")) or os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return directory
        probe = parent


def _merge(base, overlay):
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load_config(project_root):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    explicit = os.environ.get("SUSHIWRAP_CONFIG")
    paths = [explicit] if explicit else [os.path.join(os.path.expanduser("~"), ".sushi", "sushiwrap.json"),
                                         os.path.join(project_root, ".sushi", "sushiwrap.json")]
    for path in paths:
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                _merge(config, json.load(handle))
    return config


def claude_memory_dir(project_root):
    import re
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(project_root))
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", slug, "memory")


def resolve_path(value, project_root):
    if value is None:
        return None
    if "{claude_memory}" in value:
        value = value.replace("{claude_memory}", claude_memory_dir(project_root))
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(project_root, value)
    return os.path.normpath(value)


def deadline_path(config, project_root):
    return resolve_path(os.environ.get("SUSHIWRAP_DEADLINE_FILE") or config["deadline_file"], project_root)


# ---------------------------------------------------------------- boot time

def parse_sysctl_boottime(text):
    import re
    match = re.search(r"sec\s*=\s*(\d+)", text or "")
    return float(match.group(1)) if match else None


def parse_wmic_boottime(text):
    """wmic os get lastbootuptime -> '20261111083015.500000+060' (offset in minutes)."""
    import calendar
    import re
    match = re.search(r"(\d{14})\.\d+([+-]\d{3})", text or "")
    if not match:
        return None
    stamp = time.strptime(match.group(1), "%Y%m%d%H%M%S")
    return float(calendar.timegm(stamp) - int(match.group(2)) * 60)


def parse_powershell_epoch(text):
    import re
    match = re.search(r"-?\d+(?:[.,]\d+)?", (text or "").strip())
    return float(match.group(0).replace(",", ".")) if match else None


def boot_time():
    """System boot time as epoch seconds, or None."""
    override = os.environ.get("SUSHIWRAP_BOOT_TIME")
    if override:
        return float(override)
    import subprocess
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=5).stdout.decode()
            return parse_sysctl_boottime(out)
        except (OSError, subprocess.SubprocessError):
            return None
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/uptime") as handle:
                return time.time() - float(handle.read().split()[0])
        except (OSError, ValueError):
            return None
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
            return time.time() - ctypes.windll.kernel32.GetTickCount64() / 1000.0
        except Exception:
            pass
        attempts = [
            (["wmic", "os", "get", "lastbootuptime"], parse_wmic_boottime),
            (["powershell", "-NoProfile", "-Command",
              "[int64]((Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime()"
              " - [datetime]'1970-01-01').TotalSeconds"], parse_powershell_epoch),
        ]
        for argv, parser in attempts:
            try:
                out = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     timeout=20).stdout.decode("utf-8", "replace")
                value = parser(out)
                if value:
                    return value
            except (OSError, subprocess.SubprocessError):
                continue
    return None


# ---------------------------------------------------------------- deadline

def read_deadline(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def make_deadline(at, source, now, duration_s=None, boot=None, scope="host", note=None):
    return {"version": 1, "at": float(at), "at_iso": iso_utc(at), "source": source,
            "duration_s": duration_s, "boot_time": boot, "host": hostname(), "scope": scope,
            "set_at": float(now), "set_at_iso": iso_utc(now), "note": note}


def deadline_status(deadline, now, host=None, current_boot=None):
    """Return (status, minutes_left). status: none, active, expired, other-host, stale."""
    if not deadline or "at" not in deadline:
        return "none", None
    minutes = (float(deadline["at"]) - now) / 60.0
    if deadline.get("scope", "host") == "host" and deadline.get("host") not in (None, host or hostname()):
        return "other-host", minutes
    if (deadline.get("source") == "from-boot" and current_boot is not None and deadline.get("boot_time")
            and abs(float(deadline["boot_time"]) - current_boot) > BOOT_TOLERANCE_S):
        return "stale", minutes
    if minutes * 60 < -EXPIRED_GRACE_S:
        return "expired", minutes
    return "active", minutes


def renew_deadline(config, project_root, now, current_boot):
    """Recompute a boot-based deadline after a reboot, or create one from auto_from_boot.

    Returns the deadline now in force (possibly unchanged)."""
    path = deadline_path(config, project_root)
    deadline = read_deadline(path)
    if current_boot is None:
        return deadline
    status, _ = deadline_status(deadline, now, current_boot=current_boot)
    duration = None
    if deadline and deadline.get("source") == "from-boot" and status in ("stale", "expired"):
        duration = deadline.get("duration_s")
    elif (deadline is None or status in ("expired",)) and config.get("auto_from_boot"):
        duration = parse_duration(config["auto_from_boot"])
    if not duration:
        return deadline
    fresh = make_deadline(current_boot + duration, "from-boot", now, duration_s=duration, boot=current_boot,
                          scope=(deadline or {}).get("scope", "host"),
                          note=(deadline or {}).get("note") or "renewed after boot")
    if fresh["at"] <= now:
        return deadline  # the boot-based deadline is already past; leave it for a human
    atomic_write(path, json.dumps(fresh, indent=1) + "\n")
    return fresh


def command_deadline(args, project_root, config):
    path = deadline_path(config, project_root)
    now = now_epoch()
    action = args[0] if args else "show"
    if action == "set":
        options = parse_options(args[1:], {"--at": 1, "--in": 1, "--from-boot": 1, "--scope": 1, "--note": 1})
        scope = options.get("--scope", "host")
        if scope not in ("host", "all"):
            raise SystemExit("--scope must be host or all")
        if "--at" in options:
            deadline = make_deadline(parse_iso(options["--at"]), "manual", now, scope=scope, note=options.get("--note"))
        elif "--in" in options:
            seconds = parse_duration(options["--in"])
            deadline = make_deadline(now + seconds, "in", now, duration_s=seconds, scope=scope, note=options.get("--note"))
        elif "--from-boot" in options:
            seconds = parse_duration(options["--from-boot"])
            boot = boot_time()
            if boot is None:
                raise SystemExit("could not read this system's boot time; use --at or --in")
            deadline = make_deadline(boot + seconds, "from-boot", now, duration_s=seconds, boot=boot,
                                     scope=scope, note=options.get("--note"))
        else:
            raise SystemExit("deadline set needs --at, --in or --from-boot")
        atomic_write(path, json.dumps(deadline, indent=1) + "\n")
        if deadline["at"] <= now:
            print("WARNING: that deadline is already in the past.")
        print("deadline set: %s (%s local), %d min from now [%s, scope %s]\nfile: %s" % (
            deadline["at_iso"], local_hm(deadline["at"]), round((deadline["at"] - now) / 60.0),
            deadline["source"], scope, path))
        return 0
    if action == "clear":
        if os.path.exists(path):
            os.remove(path)
            print("deadline cleared (%s)" % path)
        else:
            print("no deadline set (%s)" % path)
        return 0
    if action == "show":
        deadline = renew_deadline(config, project_root, now, boot_time())
        status, minutes = deadline_status(deadline, now, current_boot=boot_time())
        if status == "none":
            print("no deadline set (%s)" % path)
            return 0
        print("deadline: %s (%s local) — %s, %s [%s, set on %s, scope %s]" % (
            deadline["at_iso"], local_hm(deadline["at"]),
            ("%d min left" % round(minutes)) if minutes >= 0 else ("passed %d min ago" % round(-minutes)),
            status, deadline.get("source"), deadline.get("host"), deadline.get("scope")))
        if deadline.get("note"):
            print("note: %s" % deadline["note"])
        print("file: %s" % path)
        return 0
    raise SystemExit("deadline: unknown action %r" % action)


# ---------------------------------------------------------------- monitor

def checklist(config, seat, minutes, threshold):
    push = "push the seat branch" if config.get("push_allowed") else "don't push (not allowed here); note the branch head"
    state = config.get("state_file") or "STATE.md"
    who = (" (seat %s)" % seat) if seat else ""
    if minutes <= 0:
        head = "The host reset time has passed%s." % who
    else:
        head = "Host reset in %d min%s." % (max(1, round(minutes)), who)
    return "\n".join([
        head,
        "Shutdown checklist:",
        "1. Commit your work in progress to the seat branch, by path (`git add <paths>`, then `git commit`).",
        "2. Update the resume note: `%s seat pointer %s --text \"...\" --append-track`." % (CLI, seat or "<seat>"),
        "3. %s." % (push[0].upper() + push[1:]),
        "4. Coordinator: add a shutdown entry to %s (what is in flight, and the order to resume in)." % os.path.basename(state),
        "The `shutdown` skill walks through this; `%s shutdown-check` verifies it." % CLI,
    ])


def monitor_step(state, deadline_at, now, thresholds):
    """Pure threshold logic. state: {"at": deadline or None, "fired": threshold or None}.

    Returns (fire, threshold, new_state). fire is True when a message should print:
    each threshold fires at most once per deadline, only the most urgent one crossed,
    and 0 means the deadline itself passed."""
    if deadline_at is None:
        return False, None, {"at": None, "fired": None}
    if state.get("at") != deadline_at:
        state = {"at": deadline_at, "fired": None}
    minutes = (deadline_at - now) / 60.0
    ordered = sorted(thresholds, reverse=True)
    crossed = None
    if minutes <= 0:
        crossed = 0
    else:
        for threshold in ordered:
            if minutes <= threshold:
                crossed = threshold
    if crossed is None:
        return False, None, state
    if state.get("fired") is not None and crossed >= state["fired"]:
        return False, crossed, state
    return True, crossed, {"at": deadline_at, "fired": crossed}


def command_monitor(args, project_root):
    options = parse_options(args, {"--interval": 1, "--once": 0})
    interval = float(options.get("--interval", 60))
    state = {"at": None, "fired": None}
    seat = os.environ.get("SUSHI_SEAT")
    while True:
        try:
            config = load_config(project_root)
            now = now_epoch()
            current_boot = boot_time()
            deadline = renew_deadline(config, project_root, now, current_boot)
            status, minutes = deadline_status(deadline, now, current_boot=current_boot)
            at = float(deadline["at"]) if status == "active" else None
            if status == "active" and minutes < -5:
                at = None  # long past: stay silent rather than nag after the fact
            fire, threshold, state = monitor_step(state, at, now, config.get("thresholds_min", [30, 15, 5]))
            if fire:
                sys.stdout.write(checklist(config, seat, minutes, threshold) + "\n")
                sys.stdout.flush()
        except Exception as error:  # a monitor must keep running
            log(project_root, "monitor error: %r" % (error,))
        if "--once" in options:
            return 0
        time.sleep(interval)


# ---------------------------------------------------------------- hooks

def hook_message(minutes, deadline, seat):
    branch = ("seat %s's branch" % seat) if seat else "the seat branch"
    if minutes <= 0:
        return ("The host reset time (%s local) passed %d min ago. Commit your work in progress to %s and "
                "update the resume note." % (local_hm(deadline["at"]), round(-minutes), branch))
    return ("Host reset in %d min (%s local). Commit your work in progress to %s and update the resume note "
            "before starting anything long." % (max(1, round(minutes)), local_hm(deadline["at"]), branch))


def hook_decide(event, project_root, config, now):
    """Return the additionalContext text, or None. Dedupe: one per session/agent per bucket."""
    deadline = read_deadline(deadline_path(config, project_root))
    status, minutes = deadline_status(deadline, now)
    if status != "active" or minutes > float(config.get("hook_window_min", 30)):
        return None
    bucket_size = float(config.get("hook_bucket_min", 5))
    bucket = int(minutes // bucket_size) if minutes > 0 else -1 - int(-minutes // bucket_size)
    key = "%s-%s" % (event.get("session_id") or "nosession", event.get("agent_id") or "main")
    key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)[:120]
    base = os.path.join(sushi_base(project_root), "state", "sushiwrap", "hook")
    directory = os.path.join(base, str(int(deadline["at"])))
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
        for name in os.listdir(base):  # a new deadline: drop markers for old ones
            if name != str(int(deadline["at"])):
                old = os.path.join(base, name)
                try:
                    for marker in os.listdir(old):
                        os.remove(os.path.join(old, marker))
                    os.rmdir(old)
                except OSError:
                    pass  # another hook process got there first
    try:
        handle = os.open(os.path.join(directory, "%s.%d" % (key, bucket)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(handle)
    except FileExistsError:
        return None
    return hook_message(minutes, deadline, os.environ.get("SUSHI_SEAT"))


def command_hook():
    project_root = None
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        project_root = os.environ.get("CLAUDE_PROJECT_DIR") or find_project_root(event.get("cwd") or None)
        global CURRENT_ROOT
        CURRENT_ROOT = project_root
        config = load_config(project_root)
        text = hook_decide(event, project_root, config, now_epoch())
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}))
    except Exception as error:  # a hook must never break the session
        log(project_root, "hook error: %r" % (error,))
    return 0


def command_session_start():
    project_root = None
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
        project_root = os.environ.get("CLAUDE_PROJECT_DIR") or find_project_root(event.get("cwd") or None)
        global CURRENT_ROOT
        CURRENT_ROOT = project_root
        config = load_config(project_root)
        now = now_epoch()
        current_boot = boot_time()
        deadline = renew_deadline(config, project_root, now, current_boot)
        status, minutes = deadline_status(deadline, now, current_boot=current_boot)
        if status == "active" and minutes > 0:
            text = "SushiWrap: this host resets at %s local (%d min from now)." % (local_hm(deadline["at"]), round(minutes))
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    except Exception as error:
        log(project_root, "session-start error: %r" % (error,))
    return 0


# ---------------------------------------------------------------- seats

def seats_dir(config, project_root):
    return resolve_path(config["seats_dir"], project_root)


def seat_path(config, project_root, seat):
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]*", seat or ""):
        raise SystemExit("bad seat name %r" % seat)
    return os.path.join(seats_dir(config, project_root), seat + ".md")


def render_seat(header, pointer):
    ordered = {key: header.get(key) for key in SEAT_FIELDS}
    for key in header:
        if key not in ordered:
            ordered[key] = header[key]
    stamp = header.get("pointer_updated_at") or "never"
    lines = ["# Seat %s" % header["seat"], "",
             "Managed by SushiWrap. Edit the header with `sushiwrap seat set`, the resume note with "
             "`sushiwrap seat pointer`.", "",
             "```json", json.dumps(ordered, indent=1, ensure_ascii=False), "```", "",
             "## Resume note (%s)" % stamp, ""]
    body = (pointer or "").strip("\n")
    return "\n".join(lines) + (body + "\n" if body else "")


def parse_seat(text):
    """Return (header dict, pointer text)."""
    start = text.find("```json")
    if start < 0:
        raise ValueError("seat file has no ```json header block")
    end = text.find("\n```", start + 7)
    if end < 0:
        raise ValueError("seat file header block is not closed")
    header = json.loads(text[start + 7:end])
    rest = text[end + 4:]
    marker = rest.find("## Resume note")
    if marker < 0:
        marker = rest.find("## " + LEGACY_POINTER_HEADING)
    if marker < 0:
        return header, ""
    after = rest[marker:].split("\n", 1)
    pointer = after[1] if len(after) > 1 else ""
    return header, pointer.strip("\n")


def load_seat(config, project_root, seat):
    path = seat_path(config, project_root, seat)
    if not os.path.isfile(path):
        raise SystemExit("no seat %r (%s); create it with `seat init %s`" % (seat, path, seat))
    with open(path, encoding="utf-8") as handle:
        return parse_seat(handle.read())


def save_seat(config, project_root, header, pointer, now):
    header["updated_at"] = iso_utc(now)
    atomic_write(seat_path(config, project_root, header["seat"]), render_seat(header, pointer))


def for_host(value, host=None):
    """A per-host value: a string, or {host: value, "*": default}."""
    if isinstance(value, dict):
        value = value.get(host or hostname(), value.get("*"))
    return os.path.expanduser(value) if isinstance(value, str) else value


def apply_fields(header, options):
    mapping = {"--role": "role", "--track": "track_file", "--branch": "branch", "--manifest": "host_manifest"}
    for flag, key in mapping.items():
        if flag in options:
            header[key] = options[flag]
    if "--worktree" in options:
        value = options["--worktree"]
        host, _, path = value.partition("=")
        if path and "/" not in host and "\\" not in host and ":" not in host:
            current = header.get("worktree")
            table = dict(current) if isinstance(current, dict) else ({"*": current} if current else {})
            table[host.lower()] = path
            header["worktree"] = table
        elif isinstance(header.get("worktree"), dict):
            header["worktree"][hostname()] = value
        else:
            header["worktree"] = value
    if "--owned" in options:
        header["owned_paths"] = options["--owned"]
    if "--ticket" in options:
        header["open_tickets"] = options["--ticket"]


def track_pointer(path, heading="Resume note", skip=r"\b(previous|superseded|old)\b"):
    """Return (heading line, section text) of the latest resume note in a track file.

    The latest is the last heading (in file order) that contains `heading` (or the legacy
    "RESUME POINTER") and does not match `skip`; its section runs to the next heading of the
    same or a higher level."""
    import re
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None, None
    skip_re = re.compile(skip, re.IGNORECASE) if skip else None
    found = None
    in_fence = False
    for index, line in enumerate(lines):
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence or not line.startswith("#"):
            continue
        if (heading in line or LEGACY_POINTER_HEADING in line) and not (skip_re and skip_re.search(line)):
            found = index
    if found is None:
        return None, None
    level = len(lines[found]) - len(lines[found].lstrip("#"))
    end = len(lines)
    in_fence = False
    for index in range(found + 1, len(lines)):
        if lines[index].startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        match = re.match(r"(#+)\s", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return lines[found], "\n".join(lines[found + 1:end]).strip("\n")


def latest_section(path, pattern):
    """The last level-2 section whose heading matches pattern: (heading, body)."""
    import re
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None, None
    regex = re.compile(pattern)
    found = None
    for index, line in enumerate(lines):
        if line.startswith("## ") and regex.search(line):
            found = index
    if found is None:
        return None, None
    end = next((i for i in range(found + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[found], "\n".join(lines[found + 1:end]).strip("\n")


def git(worktree, *argv, timeout=20):
    import subprocess
    try:
        completed = subprocess.run(["git", "-C", worktree] + list(argv), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        return completed.returncode, completed.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as error:
        return None, str(error)


def worktree_summary(worktree):
    if not worktree:
        return "no worktree recorded"
    if not os.path.isdir(worktree):
        return "%s (not on this host)" % worktree
    code, branch = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return "%s (not a git worktree)" % worktree
    _, head = git(worktree, "log", "-1", "--format=%h %s")
    _, status = git(worktree, "status", "--porcelain", "--untracked-files=no")
    dirty = len([line for line in status.splitlines() if line.strip()])
    return "%s (on %s @ %s; %d uncommitted tracked change(s))" % (worktree, branch.strip(), head.strip()[:60], dirty)


def clip(text, limit):
    lines = (text or "").splitlines()
    if len(lines) <= limit:
        return text or ""
    return "\n".join(lines[:limit] + ["... (%d more lines in the file)" % (len(lines) - limit)])


def take_brief(config, project_root, header, pointer, now):
    seat = header["seat"]
    lines = ["SEAT %s (role %s), taken on %s at %s" % (seat, header.get("role") or "?", hostname(), iso_utc(now))]
    track = header.get("track_file")
    track_path = resolve_path(track, resolve_path(config.get("tracks_root", "."), project_root)) if track else None
    worktree = for_host(header.get("worktree"))
    if track_path:
        if os.path.isfile(track_path):
            lines.append("Track:     %s (modified %s ago)" % (track_path, human_age(now - os.path.getmtime(track_path))))
        else:
            lines.append("Track:     %s (MISSING on this host)" % track_path)
    lines.append("Branch:    %s" % (header.get("branch") or "?"))
    lines.append("Worktree:  %s" % worktree_summary(worktree))
    if header.get("owned_paths"):
        lines.append("Owns:      %s" % ", ".join(header["owned_paths"]))
    if header.get("open_tickets"):
        lines.append("Tickets:   %s" % ", ".join(header["open_tickets"]))
    if header.get("host_manifest"):
        lines.append("Manifest:  %s" % header["host_manifest"])
    deadline = read_deadline(deadline_path(config, project_root))
    status, minutes = deadline_status(deadline, now)
    if status == "active":
        lines.append("Deadline:  %s local, %d min left" % (local_hm(deadline["at"]), round(minutes)))
    lines.append("")
    written = header.get("pointer_updated_at")
    lines.append("== Seat resume note (written %s) ==" % (written or "never"))
    lines.append(clip(pointer, 80) if pointer else "(empty)")
    if track_path:
        heading, body = track_pointer(track_path, config.get("pointer_heading", "Resume note"),
                                      config.get("pointer_skip"))
        lines.append("")
        if heading:
            lines.append("== Track file's latest resume note: %s ==" % heading.lstrip("# ").strip())
            lines.append(clip(body, 120))
        else:
            lines.append("== Track file has no resume note heading ==")
    state_file = resolve_path(config.get("state_file"), project_root) if config.get("state_file") else None
    if state_file:
        heading, body = latest_section(state_file, config.get("shutdown_heading"))
        if heading:
            relevant = [line for line in body.splitlines()
                        if seat.lower() in line.lower() or (header.get("branch") or "\0") in line]
            lines.append("")
            lines.append("== Latest STATE section: %s ==" % heading.lstrip("# ").strip())
            lines.append("\n".join(relevant[:20]) if relevant else "(no line names this seat or its branch; read the section)")
    lines.append("")
    lines.append("Do this:")
    lines.append("1. Set SUSHI_SEAT=%s SUSHI_ROLE=%s in the environment of this session's commands if you can."
                 % (seat, header.get("role") or ""))
    if worktree:
        lines.append("2. Work in %s on branch %s (switch to it if the worktree is elsewhere)." % (worktree, header.get("branch")))
    else:
        lines.append("2. Record the worktree: `%s seat set %s --worktree PATH`." % (CLI, seat))
    lines.append("3. Resume from the newer resume note above (seat written %s; track file modified as shown)."
                 % (written or "never"))
    lines.append("4. Update the resume note at every milestone and before stopping: "
                 "`%s seat pointer %s --text \"...\" --append-track`." % (CLI, seat))
    return "\n".join(lines)


def command_seat(args, project_root, config):
    if not args:
        raise SystemExit("seat: list | show | init | set | take | pointer | release")
    action, rest = args[0], args[1:]
    now = now_epoch()
    field_spec = {"--role": 1, "--track": 1, "--branch": 1, "--worktree": 1, "--owned": "*", "--ticket": "*",
                  "--manifest": 1}
    if action == "list":
        directory = seats_dir(config, project_root)
        names = sorted(n[:-3] for n in os.listdir(directory) if n.endswith(".md")) if os.path.isdir(directory) else []
        if not names:
            print("no seats in %s" % directory)
            return 0
        for name in names:
            try:
                header, pointer = load_seat(config, project_root, name)
            except (ValueError, SystemExit) as error:
                print("%-8s unreadable: %s" % (name, error))
                continue
            holder = header.get("holder") or {}
            age = ""
            if header.get("pointer_updated_at"):
                age = "resume note %s ago" % human_age(now - parse_iso(header["pointer_updated_at"]))
            print("%-8s %-12s %-40s %-22s %s" % (name, header.get("role") or "-", header.get("branch") or "-",
                                                 ("claimed by %s" % holder.get("host")) if holder else "unclaimed",
                                                 age or "no resume note"))
        return 0
    if action in ("show", "take", "pointer", "init", "set", "release"):
        seat = rest[0] if rest and not rest[0].startswith("--") else os.environ.get("SUSHI_SEAT")
        if not seat:
            raise SystemExit("seat %s needs a seat name (or $SUSHI_SEAT)" % action)
        rest = rest[1:] if rest and rest[0] == seat else rest
    if action == "show":
        with open(seat_path(config, project_root, seat), encoding="utf-8") as handle:
            sys.stdout.write(handle.read())
        return 0
    if action == "init":
        path = seat_path(config, project_root, seat)
        if os.path.exists(path):
            raise SystemExit("seat %s exists (%s); use `seat set`" % (seat, path))
        header = {"seat": seat, "role": os.environ.get("SUSHI_ROLE"), "owned_paths": [], "open_tickets": []}
        apply_fields(header, parse_options(rest, field_spec))
        save_seat(config, project_root, header, "", now)
        print("created %s" % path)
        return 0
    header, pointer = load_seat(config, project_root, seat)
    if action == "set":
        apply_fields(header, parse_options(rest, field_spec))
        save_seat(config, project_root, header, pointer, now)
        print("updated %s" % seat_path(config, project_root, seat))
        return 0
    if action == "take":
        options = parse_options(rest, {"--by": 1})
        holder = header.get("holder") or {}
        if holder and holder.get("host") != hostname():
            print("Note: seat was claimed by %s since %s; claiming it for this host. Check that nothing still runs there.\n"
                  % (holder.get("host"), holder.get("taken_at")))
        header["holder"] = {"host": hostname(), "taken_at": iso_utc(now), "by": options.get("--by")}
        save_seat(config, project_root, header, pointer, now)
        print(take_brief(config, project_root, header, pointer, now))
        return 0
    if action == "pointer":
        options = parse_options(rest, {"--text": 1, "--file": 1, "--append-track": 0})
        if "--text" in options:
            text = options["--text"]
        elif "--file" in options:
            with open(options["--file"], encoding="utf-8") as handle:
                text = handle.read()
        else:
            raise SystemExit("seat pointer needs --text or --file")
        header["pointer_updated_at"] = iso_utc(now)
        save_seat(config, project_root, header, text, now)
        print("resume note updated for %s" % seat)
        if "--append-track" in options:
            track = header.get("track_file")
            if not track:
                raise SystemExit("seat has no track_file; set it with `seat set %s --track FILE`" % seat)
            track_path = resolve_path(track, resolve_path(config.get("tracks_root", "."), project_root))
            section = "\n## %s (%s, seat %s, via sushiwrap)\n%s\n" % (
                config.get("pointer_heading", "Resume note"), time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
                seat, text.strip("\n"))
            with open(track_path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(section)
            print("appended to %s" % track_path)
        return 0
    if action == "release":
        header["holder"] = None
        save_seat(config, project_root, header, pointer, now)
        print("released %s" % seat)
        return 0
    raise SystemExit("seat: unknown action %r" % action)


# ---------------------------------------------------------------- shutdown check

def compare_dirs(source, target):
    import hashlib

    def digest(path):
        with open(path, "rb") as handle:
            return hashlib.sha1(handle.read()).hexdigest()

    def files(root):
        result = {}
        for directory, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue
                full = os.path.join(directory, name)
                result[os.path.relpath(full, root).replace(os.sep, "/")] = full
        return result

    a, b = files(source), files(target)
    missing = sorted(set(a) - set(b))
    differ = sorted(name for name in set(a) & set(b) if digest(a[name]) != digest(b[name]))
    extra = sorted(set(b) - set(a))
    return missing, differ, extra, len(a)


def check_worktree(worktree, branch, config):
    """Yield (level, message) for one worktree."""
    if not worktree:
        yield "WARN", "no worktree recorded for this host"
        return
    if not os.path.isdir(worktree):
        yield "WARN", "worktree %s is not on this host; skipped" % worktree
        return
    code, current = git(worktree, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        yield "WARN", "%s is not a git worktree" % worktree
        return
    current = current.strip()
    if branch and current != branch:
        yield "WARN", "worktree is on %s, the seat branch is %s" % (current, branch)
    _, status = git(worktree, "status", "--porcelain=v1", "--untracked-files=no")
    dirty = [line for line in status.splitlines() if line.strip()]
    if dirty:
        yield "FAIL", ("%d uncommitted change(s) in %s; commit them by path: git -C %s add <paths>, then commit"
                       % (len(dirty), worktree, worktree))
    else:
        _, subject = git(worktree, "log", "-1", "--format=%h %s")
        yield "PASS", "worktree clean (HEAD %s)" % subject.strip()[:70]
    code, ahead = git(worktree, "rev-list", "--count", "@{u}..HEAD")
    if code == 0 and ahead.strip() not in ("", "0"):
        if config.get("push_allowed"):
            yield "WARN", "%s commit(s) not pushed" % ahead.strip()
        else:
            yield "PASS", "%s local commit(s) not pushed (pushing is not allowed here; report the head)" % ahead.strip()


def command_shutdown_check(args, project_root, config):
    options = parse_options(args, {"--seat": 1, "--max-age": 1})
    now = now_epoch()
    max_age = float(options.get("--max-age", config.get("pointer_max_age_min", 45)))
    directory = seats_dir(config, project_root)
    if "--seat" in options:
        seats = [options["--seat"]]
    else:
        seats = sorted(n[:-3] for n in os.listdir(directory) if n.endswith(".md")) if os.path.isdir(directory) else []
    results = []
    deadline = read_deadline(deadline_path(config, project_root))
    status, minutes = deadline_status(deadline, now)
    print("shutdown-check on %s%s" % (hostname(), (" (deadline: %d min left)" % round(minutes)) if status == "active" else ""))
    for seat in seats:
        try:
            header, pointer = load_seat(config, project_root, seat)
        except (SystemExit, ValueError) as error:
            results.append((seat, "FAIL", "seat unreadable: %s" % error))
            continue
        stamp = header.get("pointer_updated_at")
        if not stamp or not pointer.strip():
            results.append((seat, "FAIL", "no resume note written"))
        else:
            age = (now - parse_iso(stamp)) / 60.0
            level = "PASS" if age <= max_age else "FAIL"
            results.append((seat, level, "resume note updated %s ago (limit %d min)" % (human_age(age * 60), max_age)))
        for level, message in check_worktree(for_host(header.get("worktree")), header.get("branch"), config):
            results.append((seat, level, message))
    for pair in config.get("memory_mirror", []):
        source, target = resolve_path(pair[0], project_root), resolve_path(pair[1], project_root)
        if not os.path.isdir(source):
            results.append(("mirror", "WARN", "source %s does not exist" % source))
            continue
        if not os.path.isdir(target):
            results.append(("mirror", "FAIL", "mirror target %s does not exist" % target))
            continue
        missing, differ, extra, total = compare_dirs(source, target)
        if missing or differ:
            names = ", ".join((missing + differ)[:10])
            results.append(("mirror", "FAIL", "%s -> %s: %d missing, %d differ (%s)" % (source, target, len(missing), len(differ), names)))
        else:
            results.append(("mirror", "PASS", "%s -> %s: %d file(s) match%s" % (
                source, target, total, (", %d extra in the mirror" % len(extra)) if extra else "")))
    if not seats and not config.get("memory_mirror"):
        results.append(("-", "WARN", "no seats in %s and no memory_mirror configured" % directory))
    for who, level, message in results:
        print("[%s] %s %s" % (who, level, message))
    fails = sum(1 for r in results if r[1] == "FAIL")
    warns = sum(1 for r in results if r[1] == "WARN")
    print("Result: %d FAIL, %d WARN" % (fails, warns))
    return 1 if fails else 0


def command_shutdown_template(args, project_root, config):
    options = parse_options(args, {"--reason": 1})
    now = now_epoch()
    directory = seats_dir(config, project_root)
    seats = sorted(n[:-3] for n in os.listdir(directory) if n.endswith(".md")) if os.path.isdir(directory) else []
    print("## SHUTDOWN %s (%s)" % (time.strftime("%Y-%m-%d", time.localtime(now)), options.get("--reason", "host reset")))
    print("Resume order:")
    for number, seat in enumerate(seats, start=1):
        header, pointer = load_seat(config, project_root, seat)
        worktree = for_host(header.get("worktree")) or "?"
        head = ""
        if worktree and os.path.isdir(worktree):
            code, out = git(worktree, "rev-parse", "--short", "HEAD")
            head = (" @ " + out.strip()) if code == 0 else ""
        first = next((line.strip() for line in pointer.splitlines() if line.strip()), "(no resume note)")
        print("%d. %s (%s, %s, %s on %s%s): %s" % (number, seat, header.get("role") or "?", header.get("track_file") or "no track",
                                                   worktree, header.get("branch") or "?", head, first[:200]))
    print("- Draft from the seats' resume notes: edit it, then append it to STATE yourself.")
    return 0


# ---------------------------------------------------------------- survey / provision

def load_probe_source():
    with open(os.path.join(PLUGIN_ROOT, "scripts", "sushiwrap_probe.py"), encoding="utf-8") as handle:
        return handle.read()


def survey_config(config):
    return dict(config.get("survey") or {})


def remote_survey(host, config, timeout=120):
    import subprocess
    settings = (config.get("hosts") or {}).get(host, {})
    survey = survey_config(config)
    survey.update(settings.get("survey", {}))
    script = "PROBE_CONFIG = json.loads(%r)\n" % json.dumps(survey)
    source = load_probe_source()
    # Insert the config after the imports so `json` is defined; keep the probe's own main guard.
    marker = "\nPROBE_VERSION"
    source = source.replace(marker, "\n" + script + marker, 1)
    remote = [settings.get("python", "python3"), "-"]
    if settings.get("nice", 10) is not None:
        remote = ["nice", "-n", str(settings.get("nice", 10))] + remote
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host] + remote
    completed = subprocess.run(argv, input=source.encode("utf-8"), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=timeout)
    if completed.returncode != 0:
        raise SystemExit("remote probe on %s failed (%d): %s" % (host, completed.returncode,
                                                                 completed.stderr.decode("utf-8", "replace")[-500:]))
    return json.loads(completed.stdout.decode("utf-8"))


def local_survey(config):
    import importlib.util
    spec = importlib.util.spec_from_file_location("sushiwrap_probe", os.path.join(PLUGIN_ROOT, "scripts", "sushiwrap_probe.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_probe(survey_config(config))


def anonymize(manifest):
    """Replace the surveyed user's home, user name and host name with placeholders."""
    home = (manifest.get("user") or {}).get("home") or ""
    user = (manifest.get("user") or {}).get("name") or ""
    host = (manifest.get("host") or {}).get("hostname") or ""
    text = json.dumps(manifest)
    if home and len(home) > 3:
        text = text.replace(json.dumps(home)[1:-1], "~")
    for value, placeholder in ((user, "<user>"), (host, "<host>")):
        if value and len(value) > 2:
            text = text.replace(value, placeholder)
    import re
    text = re.sub(r"/var/folders/[^/\"]+/[^/\"]+/", "/var/folders/<id>/", text)  # per-user macOS temp id
    return json.loads(text)


def gib(value):
    return "%.1f GiB" % (value / float(1 << 30)) if value else "?"


def manifest_markdown(manifest):
    host = manifest.get("host", {})
    out = ["# Host manifest: %s" % host.get("hostname"), "",
           "Collected %s by sushiwrap probe v%s in %ss." % (manifest.get("collected_at"), manifest.get("probe_version"),
                                                             manifest.get("probe_seconds")), "",
           "## System", "",
           "| | |", "|---|---|",
           "| OS | %s %s (%s) |" % (host.get("os"), host.get("macos_version") or host.get("os_release"), host.get("machine")),
           "| Cores | %s |" % ", ".join("%s %s" % (v, k) for k, v in sorted(manifest.get("cpu", {}).items())),
           "| Memory | %s |" % gib(manifest.get("memory_bytes")),
           "| Python | %s (%s) |" % (host.get("python"), host.get("python_executable")),
           "| Boot | %s |" % (iso_utc(manifest["boot_time"]) if manifest.get("boot_time") else "?"),
           "", "## Disk", "", "| Volume | Free | Total | Free % |", "|---|---|---|---|"]
    for volume in manifest.get("volumes", []):
        out.append("| %s | %s | %s | %s |" % (volume["path"], gib(volume["free_bytes"]), gib(volume["total_bytes"]), volume["free_pct"]))
    out += ["", "## Toolchain", "", "| Tool | Version | Path |", "|---|---|---|"]
    for name, tool in sorted(manifest.get("tools", {}).items()):
        if tool.get("found"):
            out.append("| %s | %s | %s |" % (name, (tool.get("version") or tool.get("note") or "").replace("|", "/"), tool.get("path") or ""))
        else:
            out.append("| %s | not found | %s |" % (name, tool.get("path") or ""))
    ccache = manifest.get("ccache")
    out += ["", "## ccache", ""]
    if ccache:
        out.append("%s" % ccache.get("version"))
        for key, entry in sorted((ccache.get("config") or {}).items()):
            out.append("- %s = %s (%s)" % (key, entry["value"], entry["origin"]))
    else:
        out.append("not installed")
    path = manifest.get("path", {})
    out += ["", "## PATH", ""] + ["%d. %s" % (i, p) for i, p in enumerate(path.get("entries", []), start=1)]
    if path.get("login_shell_entries") is not None:
        extra = [p for p in path["login_shell_entries"] if p not in path.get("entries", [])]
        out += ["", "Login shell adds: %s" % (", ".join(extra) if extra else "nothing")]
    out += ["", "## Environment (secrets redacted)", ""]
    out += ["- %s=%s" % (k, v) for k, v in sorted(manifest.get("env", {}).items())] or ["(none selected)"]
    out += ["", "## Workspaces", ""]
    for workspace in manifest.get("workspaces", []):
        out.append("### %s%s" % (workspace["root"], "" if workspace["exists"] else " (missing)"))
        for repo in workspace.get("repos", []):
            remotes = ", ".join("%s %s" % kv for kv in sorted(repo.get("remotes", {}).items()))
            out.append("- %s: %s @ %s%s%s" % (os.path.relpath(repo["path"], workspace["root"]), repo.get("branch") or "(detached)",
                                             repo.get("head"), " (worktree)" if repo.get("is_worktree") else "",
                                             ("; " + remotes) if remotes else ""))
        out.append("")
    traps = manifest.get("traps", []) + manifest.get("known_traps", [])
    out += ["## Traps", ""] + (["- %s" % t for t in traps] if traps else ["none detected"])
    if manifest.get("errors"):
        out += ["", "## Probe errors", ""] + ["- %s" % e for e in manifest["errors"]]
    return "\n".join(out) + "\n"


def command_survey(args, project_root, config):
    options = parse_options(args, {"--host": 1, "--out": 1, "--json": 0, "--anonymize": 0})
    manifest = remote_survey(options["--host"], config) if "--host" in options else local_survey(config)
    if "--anonymize" in options:
        manifest = anonymize(manifest)
    if "--out" in options:
        out = options["--out"]
        base = out[:-5] if out.endswith(".json") else out
        atomic_write(base + ".json", json.dumps(manifest, indent=1, sort_keys=True) + "\n")
        atomic_write(base + ".md", manifest_markdown(manifest))
        print("wrote %s.json and %s.md" % (base, base))
    elif "--json" in options:
        print(json.dumps(manifest, indent=1, sort_keys=True))
    else:
        sys.stdout.write(manifest_markdown(manifest))
    return 0


SYSTEM_PATH_PREFIXES = ("/usr/bin", "/bin", "/usr/sbin", "/sbin", "/System", "/Library/Apple", "/var/run",
                        "C:\\Windows", "C:/Windows")
INSTALL_HINTS = {
    "Darwin": {"cmake": "brew install cmake", "ninja": "brew install ninja", "ccache": "brew install ccache",
               "brew_llvm_clang": "brew install llvm", "git": "xcode-select --install (or brew install git)",
               "clang": "xcode-select --install", "python3": "xcode-select --install (or brew install python)",
               "rsync": "brew install rsync"},
    "Linux": {"cmake": "apt install cmake", "ninja": "apt install ninja-build", "ccache": "apt install ccache",
              "clang": "apt install clang", "gcc": "apt install build-essential", "git": "apt install git"},
    "Windows": {"cmake": "winget install Kitware.CMake", "ninja": "winget install Ninja-build.Ninja",
                "git": "winget install Git.Git", "python3": "winget install Python.Python.3.12",
                "ccache": "winget install ccache.ccache"},
}


def provision_plan(manifest):
    host = manifest.get("host", {})
    system = host.get("os")
    steps = []
    steps.append("Prepare a %s %s host (%s) with at least %s RAM and %s cores; the surveyed host had those." % (
        system, host.get("macos_version") or host.get("os_release"), host.get("machine"),
        gib(manifest.get("memory_bytes")), (manifest.get("cpu") or {}).get("logical")))
    for volume in manifest.get("volumes", []):
        used = volume["total_bytes"] - volume["free_bytes"]
        steps.append("Disk: %s held %s used of %s; leave headroom (it had %s free)." % (
            volume["path"], gib(used), gib(volume["total_bytes"]), gib(volume["free_bytes"])))
    hints = INSTALL_HINTS.get(system, {})
    bundled = []
    for name, tool in sorted(manifest.get("tools", {}).items()):
        if not (tool.get("found") and tool.get("version")):
            continue
        if system == "Darwin" and (tool.get("path") or "/usr/bin/").startswith("/usr/bin/"):
            bundled.append("%s (%s)" % (name, tool["version"]))
            continue
        hint = hints.get(name)
        steps.append("Install %s matching `%s` (was %s)%s." % (name, tool["version"], tool.get("path"),
                                                             ("; e.g. `%s`" % hint) if hint else ""))
    if bundled:
        xcode = (manifest.get("tools", {}).get("xcode_select") or {}).get("path")
        steps.append("Install the Apple developer tools (%s) for: %s." % (
            ("Xcode at " + xcode) if xcode and "Xcode.app" in xcode else "`xcode-select --install`", "; ".join(bundled)))
    ccache = manifest.get("ccache")
    if ccache:
        for key in ("cache_dir", "max_size", "base_dir", "compiler_check", "sloppiness"):
            entry = (ccache.get("config") or {}).get(key)
            if entry and entry["origin"] != "default":
                steps.append("ccache: set %s = %s (it came from %s)." % (key, entry["value"], entry["origin"]))
    custom = [p for p in manifest.get("path", {}).get("entries", []) if p and not p.startswith(SYSTEM_PATH_PREFIXES)]
    if custom:
        steps.append("PATH: add, in this order: %s." % ", ".join(custom))
    login = manifest.get("path", {}).get("login_shell_entries")
    if login:
        missing = [p for p in login if p not in manifest["path"]["entries"]]
        if missing:
            steps.append("PATH under ssh: non-interactive shells lacked %s; set PATH in the remote command or env script." % ", ".join(missing))
    env = manifest.get("env", {})
    for name, value in sorted(env.items()):
        if name in ("HOME", "SHELL", "USERPROFILE", "TMPDIR", "TEMP", "TMP", "TERM_PROGRAM", "LANG"):
            continue
        if value == "<redacted>":
            steps.append("Env: set %s by hand (value redacted in the manifest)." % name)
        else:
            steps.append("Env: %s=%s" % (name, value))
    for workspace in manifest.get("workspaces", []):
        if not workspace.get("exists"):
            continue
        steps.append("Workspace root: create %s." % workspace["root"])
        for repo in workspace.get("repos", []):
            origin = (repo.get("remotes") or {}).get("origin")
            if repo.get("is_worktree"):
                steps.append("  Worktree %s on %s @ %s: `git worktree add` it from its main clone." % (repo["path"], repo.get("branch") or "(detached)", repo.get("head")))
            elif origin and repo.get("branch"):
                steps.append("  `git clone %s %s` then `git -C %s switch %s` (was @ %s; local-only branches need a backup ref)." % (
                    origin, repo["path"], repo["path"], repo.get("branch"), repo.get("head")))
            elif origin:
                steps.append("  `git clone %s %s` then `git -C %s checkout %s` (detached)." % (
                    origin, repo["path"], repo["path"], repo.get("head")))
            else:
                steps.append("  %s has no origin remote: copy it from a backup." % repo["path"])
    for trap in manifest.get("traps", []) + manifest.get("known_traps", []):
        steps.append("Trap: %s" % trap)
    steps.append("Verify: run `sushiwrap survey --out new-host` on the new host and compare it with this manifest.")
    return steps


def command_provision(args):
    options = parse_options(args, {"--manifest": 1})
    if "--manifest" not in options:
        raise SystemExit("provision needs --manifest FILE")
    with open(options["--manifest"], encoding="utf-8") as handle:
        manifest = json.load(handle)
    print("# Provision plan for a host like %s (not executed; review each step)" % manifest.get("host", {}).get("hostname"))
    print()
    number = 0
    for step in provision_plan(manifest):
        if step.startswith("  "):
            print("   - %s" % step.strip())
        else:
            number += 1
            print("%d. %s" % (number, step))
    return 0


# ---------------------------------------------------------------- CLI

def parse_options(args, spec):
    """spec: {flag: 0 (switch) | 1 (one value) | "*" (repeatable, list)}."""
    options = {}
    index = 0
    while index < len(args):
        flag = args[index]
        if flag not in spec:
            raise SystemExit("unknown option %r" % flag)
        kind = spec[flag]
        if kind == 0:
            options[flag] = True
            index += 1
            continue
        if index + 1 >= len(args):
            raise SystemExit("%s needs a value" % flag)
        if kind == "*":
            options.setdefault(flag, []).append(args[index + 1])
        else:
            options[flag] = args[index + 1]
        index += 2
    return options


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "hook":
        return command_hook()
    if args and args[0] == "session-start":
        return command_session_start()
    # --project is global: before or after the subcommand.
    project, args = sushitree().take_project_option(args)
    if not args:
        print(__doc__)
        return 2
    command = args.pop(0)
    project_root = os.path.abspath(os.path.expanduser(project)) if project else find_project_root()
    global CURRENT_ROOT
    CURRENT_ROOT = project_root
    try:
        return run_command(command, args, project_root)
    except sushitree().SharedTreeError as error:
        sys.stderr.write("sushiwrap: %s\n" % error)
        return 2


def run_command(command, args, project_root):
    if command == "monitor":
        return command_monitor(args, project_root)
    config = load_config(project_root)
    if command == "seat":
        return command_seat(args, project_root, config)
    if command == "deadline":
        return command_deadline(args, project_root, config)
    if command == "shutdown-check":
        return command_shutdown_check(args, project_root, config)
    if command == "shutdown-template":
        return command_shutdown_template(args, project_root, config)
    if command == "survey":
        return command_survey(args, project_root, config)
    if command == "provision":
        return command_provision(args)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
