#!/usr/bin/env python3
"""SushiComms: typed handoff and question tickets between workers (seats).

The orchestrator (the coordinating session) assigns roles. Each role has responsibilities and
channels (which ticket types it may file, to which roles). Tickets are typed and validated and
kept by this program in one JSON store; hooks block direct reads and edits of the store.

Subcommands
  ticket file --type T --to ROLE [fields...]   file a ticket (permission-checked)
  ticket claim ID                              claim an open ticket addressed to your role
  ticket update ID --status S [--detail D]     change status (permission-checked)
  ticket route ID --to ROLE [--to-seat S]      orchestrator only: forward a ticket
  ticket close ID [--answer A] [--detail D]    close (answer a question)
  ticket list [filters] | ticket show ID
  handoff --branch B [...]                     coder: file a handoff plus a notify, one step
  registry init|validate [FILE]|show           role registry (init: orchestrator only)
  quorum                                       print every batch-complete rule that is met now
  monitor [--once] [--interval S]              batch monitor (orchestrator seat only)
  export-queue [--all] [--out FILE]            render handoffs as INTEGRATION_QUEUE.md entries
  import-queue FILE [--apply]                  parse queue entries into tickets (dry run default)
  hook-bash | hook-edit | hook-agent           PreToolUse hooks (read hook JSON on stdin)
  paths                                        print the resolved project, registry and store

Identity comes from SUSHI_ROLE and SUSHI_SEAT (or, inside hooks, from the subagent's
agent_type mapped through the registry). Standard library only; Python 3.9+.
"""

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import time

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.abspath(__file__)
EXAMPLE_REGISTRY = os.path.join(PLUGIN_ROOT, "examples", "roles.json")
SCHEMA_VERSION = 1

TICKET_TYPES = ("handoff", "return", "question", "done", "block-reserve", "notify")
STATUSES = ("open", "claimed", "batched", "fixed", "returned", "folded", "superseded", "closed")
IMPORT_ONLY_STATUSES = ("other",)
OPEN_STATUSES = ("open", "claimed")
HANDOFF_COUNTED = ("open", "claimed", "batched", "fixed", "folded")
HISTORY_ACTIONS = ("filed", "claimed", "status", "routed", "closed", "superseded", "imported")
DRIFT_MODES = ("reminder", "deny")
QUORUM_KINDS = ("all-reserved", "n-of-m", "count")

SEAT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ID_RE = re.compile(r"^T(\d{4,})-([A-Za-z0-9_.-]{1,32})$")
SHA_RE = re.compile(r"^[0-9a-fA-F]{6,40}$")
GITREF_RE = re.compile(r"^[A-Za-z0-9._/@+~^-]{1,200}$")
TOKEN_RE = re.compile(r"^\S{1,80}$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Every ticket has exactly these keys (unused ones are null). Kind, max length.
FIELD_SPEC = {
    "id": ("id", 0),
    "schema": ("schema", 0),
    "type": ("type", 0),
    "status": ("status", 0),
    "status_detail": ("line", 200),
    "from_seat": ("seat", 0),
    "from_role": ("role", 0),
    "to_role": ("role", 0),
    "to_seat": ("seat", 0),
    "claimed_by": ("seat", 0),
    "block": ("token", 0),
    "batch": ("token", 0),
    "branch": ("gitref", 0),
    "head": ("sha", 0),
    "base": ("token", 0),
    "intent": ("line", 400),
    "touched": ("line", 1500),
    "tests": ("line", 1500),
    "risks": ("line", 2000),
    "reason": ("line", 400),
    "failing": ("line", 400),
    "question": ("line", 400),
    "options": ("lines", 200),
    "recommendation": ("int", 0),
    "answer": ("line", 400),
    "summary": ("line", 400),
    "refs": ("refs", 0),
    "created": ("time", 0),
    "updated": ("time", 0),
    "history": ("history", 0),
    "source": ("source", 0),
}
FIELDS = tuple(FIELD_SPEC)
COMMON_REQUIRED = ("id", "schema", "type", "status", "from_role", "to_role", "created", "updated", "history")
COMMON_ALLOWED = ("status_detail", "from_seat", "to_seat", "claimed_by", "source")
TYPE_RULES = {
    "handoff": {"required": ("branch", "head", "intent", "touched", "tests", "risks"),
                "allowed": ("base", "block", "batch", "refs")},
    "return": {"required": ("refs", "branch", "head", "reason"),
               "allowed": ("failing", "base", "block", "batch")},
    "question": {"required": ("question",),
                 "allowed": ("options", "recommendation", "answer", "refs", "branch", "block", "batch")},
    "done": {"required": ("summary",),
             "allowed": ("branch", "head", "base", "block", "batch", "refs")},
    "block-reserve": {"required": ("block", "batch", "intent", "to_seat"),
                      "allowed": ("base", "branch", "refs")},
    "notify": {"required": ("refs",),
               "allowed": ("summary", "branch", "head", "block", "batch")},
}
MAX_OPTIONS = 6

DEFAULT_CONFIG = {
    "state_file": None,           # default <project>/.sushi/state/sushicomms/state.json
    "monitor": True,
    "monitor_interval_s": 5,
}

# Drift-rule placeholders (plain text substitution, so regex braces stay intact).
CMD_PREFIX = (
    r"(?:^|[;&|({]|\$\(|\bthen\s|\bdo\s|\belse\s)\s*"
    r"(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*"
    r"|sudo|time|command|exec|env|nohup|caffeinate"
    r"|nice(?:\s+-n\s*-?\d+|\s+-\d+)?"
    r"|m4ws\s+\S+"
    r"|m4run\s+\S+(?:\s+(?!--(?:\s|$))\S+)*\s+--"
    r"|ssh\s+(?:-\S+\s+)*\S+"
    r"|python3?(?:\.\d+)?(?:\s+-[A-Za-z]+)*|sh|bash|zsh|xcrun"
    r")\s+)*"
    r"['\"]?(?:\S*/)?"
)
CMD_END = r"(?=$|[\s;&|)'\"`])"


# ---------------------------------------------------------------- small utilities

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CommsError(Exception):
    """A user-facing refusal or error: printed as one line, exit status 1."""


def find_project_root(start_path):
    directory = os.path.abspath(start_path or os.getcwd())
    while not os.path.isdir(directory):
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent
    while True:
        if os.path.isdir(os.path.join(directory, ".sushi")) or os.path.exists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def resolve_project(explicit=None):
    for candidate in (explicit, os.environ.get("SUSHICOMMS_PROJECT")):
        if candidate:
            root = find_project_root(candidate)
            return root or os.path.abspath(candidate)
    return find_project_root(os.getcwd())


def load_config(project_root):
    config = dict(DEFAULT_CONFIG)
    if project_root:
        path = os.path.join(project_root, ".sushi", "sushicomms.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                config.update(json.load(handle))
    return config


def registry_path(project_root):
    override = os.environ.get("SUSHICOMMS_REGISTRY")
    if override:
        return os.path.abspath(override)
    return os.path.join(project_root or os.getcwd(), ".sushi", "sushicomms", "roles.json")


def store_path(project_root, config=None):
    """The one program-owned ticket store (JSON). Workers never read or edit it directly."""
    override = os.environ.get("SUSHICOMMS_STATE")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    config = config if config is not None else load_config(project_root)
    configured = config.get("state_file")
    base = project_root or os.getcwd()
    if configured:
        configured = os.path.expanduser(configured)
        return os.path.abspath(configured if os.path.isabs(configured) else os.path.join(base, configured))
    return os.path.join(base, ".sushi", "state", "sushicomms", "state.json")


def state_dir(project_root):
    directory = os.path.join(project_root or os.path.expanduser("~"), ".sushi", "state", "sushicomms")
    os.makedirs(directory, exist_ok=True)
    return directory


def log(project_root, message):
    try:
        directory = os.path.join(project_root or os.path.expanduser("~"), ".sushi", "logs")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "sushicomms.log"), "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))
    except OSError:
        pass


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(directory, ".%s.%d.%s.tmp" % (os.path.basename(path), os.getpid(), secrets.token_hex(4)))
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def cli_command():
    return 'python3 "%s"' % SCRIPT_PATH


# ---------------------------------------------------------------- registry

ROLE_KEYS = {"responsibilities", "channels", "may_set_status", "agent_types"}
DRIFT_KEYS = {"id", "roles", "tool", "pattern", "unless", "mode", "message", "enabled"}
QUORUM_KEYS = {"name", "kind", "description", "min", "min_fraction", "type", "to_role", "statuses", "enabled", "below_all"}
REGISTRY_KEYS = {"schema", "description", "settings", "roles", "drift_rules", "quorum_rules"}


def load_registry(project_root, path=None):
    path = path or registry_path(project_root)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def require_registry(project_root):
    path = registry_path(project_root)
    registry = None
    try:
        registry = load_registry(project_root, path)
    except ValueError as error:
        raise CommsError("registry %s is not valid JSON: %s" % (path, error))
    if registry is None:
        raise CommsError("no role registry at %s; the orchestrator runs `registry init`" % path)
    errors = validate_registry(registry)
    if errors:
        raise CommsError("registry %s is invalid: %s" % (path, "; ".join(errors[:5])))
    return registry


def wrapper_settings(registry):
    settings = (registry or {}).get("settings") or {}
    wrapper = settings.get("build_wrapper") or {}
    return {
        "name": wrapper.get("name", "the build wrapper"),
        "command": wrapper.get("command", ""),
        "match": wrapper.get("match", r"(?!x)x"),
    }


def expand_pattern(pattern, registry):
    return (pattern.replace("{cmd}", CMD_PREFIX)
                   .replace("{end}", CMD_END)
                   .replace("{wrapper}", "(?:%s)" % wrapper_settings(registry)["match"]))


def expand_message(message, registry):
    wrapper = wrapper_settings(registry)
    return (message.replace("{wrapper_name}", wrapper["name"])
                   .replace("{wrapper_command}", wrapper["command"])
                   .replace("{cli}", cli_command()))


def validate_registry(registry):
    errors = []
    if not isinstance(registry, dict):
        return ["registry must be a JSON object"]
    for key in registry:
        if key not in REGISTRY_KEYS:
            errors.append("unknown top-level key %r" % key)
    if registry.get("schema") != SCHEMA_VERSION:
        errors.append("schema must be %d" % SCHEMA_VERSION)
    settings = registry.get("settings", {})
    if not isinstance(settings, dict):
        errors.append("settings must be an object")
    else:
        spawn = settings.get("agent_spawn")
        if spawn is not None:
            if not isinstance(spawn, dict) or set(spawn) - {"reminder", "allowed_subagent_types", "message"}:
                errors.append("settings.agent_spawn takes reminder, allowed_subagent_types, message")
            elif not isinstance(spawn.get("reminder", True), bool) or \
                    not all(isinstance(item, str) for item in spawn.get("allowed_subagent_types", [])) or \
                    not isinstance(spawn.get("message", ""), str):
                errors.append("settings.agent_spawn has a value of the wrong type")
        wrapper = settings.get("build_wrapper")
        if wrapper is not None:
            if not isinstance(wrapper, dict) or not all(isinstance(wrapper.get(k), str) for k in ("name", "command", "match")):
                errors.append("settings.build_wrapper needs string name, command and match")
            else:
                try:
                    re.compile(wrapper["match"])
                except re.error as error:
                    errors.append("settings.build_wrapper.match: %s" % error)
    roles = registry.get("roles")
    if not isinstance(roles, dict) or not roles:
        errors.append("roles must be a non-empty object")
        roles = {}
    if roles and "orchestrator" not in roles:
        errors.append("an orchestrator role is required")
    seen_agent_types = {}
    for name, role in roles.items():
        where = "roles.%s" % name
        if not ROLE_RE.match(name):
            errors.append("%s: bad role name" % where)
        if not isinstance(role, dict):
            errors.append("%s must be an object" % where)
            continue
        for key in role:
            if key not in ROLE_KEYS:
                errors.append("%s: unknown key %r" % (where, key))
        if not isinstance(role.get("responsibilities"), str) or not role.get("responsibilities").strip():
            errors.append("%s.responsibilities must be non-empty text" % where)
        channels = role.get("channels", {})
        if not isinstance(channels, dict):
            errors.append("%s.channels must map ticket type to target roles" % where)
            channels = {}
        for ticket_type, targets in channels.items():
            if ticket_type not in TICKET_TYPES:
                errors.append("%s.channels: unknown ticket type %r" % (where, ticket_type))
            if not isinstance(targets, list) or not targets:
                errors.append("%s.channels.%s must be a non-empty list of roles" % (where, ticket_type))
                continue
            for target in targets:
                if target not in roles:
                    errors.append("%s.channels.%s: unknown target role %r" % (where, ticket_type, target))
        statuses = role.get("may_set_status", [])
        if not isinstance(statuses, list) or any(status not in STATUSES for status in statuses):
            errors.append("%s.may_set_status must list statuses from %s" % (where, ", ".join(STATUSES)))
        agent_types = role.get("agent_types", [])
        if not isinstance(agent_types, list) or not all(isinstance(item, str) and item for item in agent_types):
            errors.append("%s.agent_types must be a list of agent names" % where)
        else:
            for item in agent_types:
                if item in seen_agent_types:
                    errors.append("agent type %r mapped to both %s and %s" % (item, seen_agent_types[item], name))
                seen_agent_types[item] = name
    drift_ids = set()
    drift_rules = registry.get("drift_rules", [])
    if not isinstance(drift_rules, list):
        errors.append("drift_rules must be a list")
        drift_rules = []
    for index, rule in enumerate(drift_rules):
        where = "drift_rules[%d]" % index
        if not isinstance(rule, dict):
            errors.append("%s must be an object" % where)
            continue
        for key in rule:
            if key not in DRIFT_KEYS:
                errors.append("%s: unknown key %r" % (where, key))
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", rule_id):
            errors.append("%s.id must be kebab-case" % where)
        elif rule_id in drift_ids:
            errors.append("%s.id %r is duplicated" % (where, rule_id))
        drift_ids.add(rule_id)
        rule_roles = rule.get("roles")
        if not isinstance(rule_roles, list) or not rule_roles or any(role not in roles for role in rule_roles):
            errors.append("%s.roles must list known roles" % where)
        if rule.get("tool", "Bash") != "Bash":
            errors.append("%s.tool: only Bash is supported" % where)
        if rule.get("mode") not in DRIFT_MODES:
            errors.append("%s.mode must be reminder or deny" % where)
        message = rule.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 800:
            errors.append("%s.message must be text of at most 800 characters" % where)
        for key in ("pattern", "unless"):
            value = rule.get(key)
            if value is None and key == "unless":
                continue
            if not isinstance(value, str) or not value:
                errors.append("%s.%s must be a regex string" % (where, key))
                continue
            try:
                re.compile(expand_pattern(value, registry), re.MULTILINE)
            except re.error as error:
                errors.append("%s.%s: %s" % (where, key, error))
        if not isinstance(rule.get("enabled", True), bool):
            errors.append("%s.enabled must be true or false" % where)
    quorum_names = set()
    quorum_rules = registry.get("quorum_rules", [])
    if not isinstance(quorum_rules, list):
        errors.append("quorum_rules must be a list")
        quorum_rules = []
    for index, rule in enumerate(quorum_rules):
        where = "quorum_rules[%d]" % index
        if not isinstance(rule, dict):
            errors.append("%s must be an object" % where)
            continue
        for key in rule:
            if key not in QUORUM_KEYS:
                errors.append("%s: unknown key %r" % (where, key))
        name = rule.get("name")
        if not isinstance(name, str) or not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", name):
            errors.append("%s.name must be kebab-case" % where)
        elif name in quorum_names:
            errors.append("%s.name %r is duplicated" % (where, name))
        quorum_names.add(name)
        kind = rule.get("kind")
        if kind not in QUORUM_KINDS:
            errors.append("%s.kind must be one of %s" % (where, ", ".join(QUORUM_KINDS)))
        if kind == "n-of-m":
            has_min = isinstance(rule.get("min"), int) and rule.get("min") >= 1
            fraction = rule.get("min_fraction")
            has_fraction = isinstance(fraction, (int, float)) and 0 < fraction <= 1
            if has_min == has_fraction:
                errors.append("%s needs exactly one of min (>= 1) or min_fraction (0..1]" % where)
        if kind == "count":
            if not (isinstance(rule.get("min"), int) and rule.get("min") >= 1):
                errors.append("%s.min must be an integer >= 1" % where)
            if rule.get("type", "handoff") not in TICKET_TYPES:
                errors.append("%s.type is not a ticket type" % where)
            if rule.get("to_role") is not None and rule.get("to_role") not in roles:
                errors.append("%s.to_role is not a known role" % where)
            statuses = rule.get("statuses", ["open"])
            if not isinstance(statuses, list) or any(status not in STATUSES for status in statuses):
                errors.append("%s.statuses must list statuses" % where)
        if not isinstance(rule.get("enabled", True), bool):
            errors.append("%s.enabled must be true or false" % where)
    return errors


def role_for_agent_type(registry, agent_type):
    if not registry or not agent_type:
        return None
    short = agent_type.split(":")[-1]
    for name, role in (registry.get("roles") or {}).items():
        for item in role.get("agent_types", []) or []:
            if item == agent_type or item == short:
                return name
    return None


def hook_identity(event, registry):
    """Return (role, source) for the agent a hook fired in.

    A subagent or teammate (agent_id present) is identified only by its agent_type mapped
    through the registry, because it inherits the parent session's SUSHI_ROLE.
    """
    agent_type = event.get("agent_type")
    if agent_type:
        mapped = role_for_agent_type(registry, agent_type)
        if mapped:
            return mapped, "agent_type"
        if event.get("agent_id"):
            return None, "unmapped-subagent"
    role = os.environ.get("SUSHI_ROLE") or None
    return role, ("env" if role else "none")


# ---------------------------------------------------------------- ticket schema

def _is_line(value):
    return isinstance(value, str) and "\n" not in value and "\r" not in value


def validate_ticket(ticket):
    """Return a list of schema errors (empty when valid)."""
    errors = []
    if not isinstance(ticket, dict):
        return ["ticket must be an object"]
    keys = set(ticket)
    missing = [field for field in FIELDS if field not in keys]
    extra = sorted(keys - set(FIELDS))
    if missing:
        errors.append("missing keys: %s" % ", ".join(missing))
    if extra:
        errors.append("unknown keys: %s" % ", ".join(extra))
    if missing:
        return errors
    source = ticket.get("source")
    imported = isinstance(source, dict) and source.get("kind") == "queue-import"
    ticket_type = ticket.get("type")
    if ticket_type not in TICKET_TYPES:
        errors.append("type must be one of %s" % ", ".join(TICKET_TYPES))
        return errors
    rules = TYPE_RULES[ticket_type]
    required = set(COMMON_REQUIRED) | set(rules["required"])
    allowed = required | set(COMMON_ALLOWED) | set(rules["allowed"])
    for field in FIELDS:
        value = ticket[field]
        if field not in allowed:
            if value is not None:
                errors.append("%s is not used by %s tickets (must be null)" % (field, ticket_type))
            continue
        if value is None:
            if field in required:
                errors.append("%s is required for %s tickets" % (field, ticket_type))
            continue
        kind, limit = FIELD_SPEC[field]
        problem = None
        if kind == "id":
            problem = None if isinstance(value, str) and ID_RE.match(value) else "must look like T0001-w4"
        elif kind == "schema":
            problem = None if value == SCHEMA_VERSION else "must be %d" % SCHEMA_VERSION
        elif kind == "type":
            problem = None
        elif kind == "status":
            valid = STATUSES + (IMPORT_ONLY_STATUSES if imported else ())
            problem = None if value in valid else "must be one of %s" % ", ".join(valid)
        elif kind == "seat":
            problem = None if isinstance(value, str) and SEAT_RE.match(value) else "must be a seat name like w4"
        elif kind == "role":
            problem = None if isinstance(value, str) and ROLE_RE.match(value) else "must be a role name"
        elif kind == "token":
            if imported:
                problem = None if _is_line(value) and value else "must be one line"
            else:
                problem = None if isinstance(value, str) and TOKEN_RE.match(value) else "must be one word (no spaces)"
        elif kind == "gitref":
            if imported:
                problem = None if _is_line(value) and value.strip() else "must be one line"
            else:
                problem = None if isinstance(value, str) and GITREF_RE.match(value) else "must be a git branch name"
        elif kind == "sha":
            problem = None if isinstance(value, str) and SHA_RE.match(value) else "must be a commit sha (6-40 hex)"
        elif kind == "line":
            if not _is_line(value):
                problem = "must be one line of text"
            elif not imported and len(value) > limit:
                problem = "must be at most %d characters" % limit
            elif field in required and not imported and not value.strip():
                problem = "must not be empty"
        elif kind == "lines":
            if not isinstance(value, list) or not value or len(value) > MAX_OPTIONS:
                problem = "must be a list of 1-%d one-line items" % MAX_OPTIONS
            elif any(not _is_line(item) or not item.strip() or len(item) > limit for item in value):
                problem = "items must be one line of at most %d characters" % limit
        elif kind == "int":
            problem = None if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else "must be a positive integer"
        elif kind == "refs":
            if not isinstance(value, list) or not all(isinstance(item, str) and ID_RE.match(item) for item in value):
                problem = "must be a list of ticket ids"
            elif field in required and not value:
                problem = "must name at least one ticket"
        elif kind == "time":
            problem = None if isinstance(value, str) and TIME_RE.match(value) else "must be an ISO UTC time"
        elif kind == "history":
            problem = validate_history(value)
        elif kind == "source":
            if not imported:
                problem = "only queue-import sources are recognised"
            elif not (isinstance(source.get("header"), str) and isinstance(source.get("body"), list)
                      and all(isinstance(line, str) for line in source["body"])):
                problem = "queue-import source needs header text and body lines"
        if problem:
            errors.append("%s %s" % (field, problem))
    if ticket.get("recommendation") is not None:
        options = ticket.get("options") or []
        if ticket["recommendation"] > len(options):
            errors.append("recommendation must index into options (1-based)")
    return errors


def validate_history(history):
    if not isinstance(history, list) or not history:
        return "must be a non-empty list"
    for entry in history:
        if not isinstance(entry, dict) or set(entry) != {"at", "seat", "role", "action", "status", "detail"}:
            return "entries need exactly at, seat, role, action, status, detail"
        if not (isinstance(entry["at"], str) and TIME_RE.match(entry["at"])):
            return "entry time must be ISO UTC"
        if entry["action"] not in HISTORY_ACTIONS:
            return "entry action must be one of %s" % ", ".join(HISTORY_ACTIONS)
        if entry["detail"] is not None and (not _is_line(entry["detail"]) or len(entry["detail"]) > 200):
            return "entry detail must be one line of at most 200 characters"
    return None


def blank_ticket():
    return {field: None for field in FIELDS}


def history_entry(seat, role, action, status, detail=None):
    return {"at": now_iso(), "seat": seat, "role": role, "action": action, "status": status, "detail": detail}


# ---------------------------------------------------------------- ticket store

class LockTimeout(CommsError):
    pass


@contextlib.contextmanager
def dir_lock(path, timeout=20.0, stale_after=60.0):
    """Mutual exclusion with an atomic mkdir. A lock older than stale_after is broken."""
    deadline = time.time() + timeout
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            try:
                if time.time() - os.stat(path).st_mtime > stale_after:
                    os.rmdir(path)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise LockTimeout("could not take the ticket lock %s within %.0fs" % (path, timeout))
            time.sleep(0.005 + random.random() * 0.02)
    try:
        yield
    finally:
        try:
            os.rmdir(path)
        except OSError:
            pass


class TicketStore:
    """All tickets live in one JSON document owned by this program.

    Layout: {"schema": 1, "next_number": N, "tickets": {id: ticket}}. Every change happens in
    a transaction: take the lock (an atomic mkdir next to the file), load, change, write a
    temporary file and os.replace it, release.
    """

    def __init__(self, path):
        self.path = path
        self.directory = os.path.dirname(path)

    def lock(self):
        os.makedirs(self.directory, exist_ok=True)
        return dir_lock(self.path + ".lock")

    def load(self):
        if not os.path.exists(self.path):
            return {"schema": SCHEMA_VERSION, "next_number": 1, "tickets": {}}
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as error:
            raise CommsError("the ticket store %s is unreadable (%s); it was left untouched" % (self.path, error))
        if not isinstance(data, dict) or not isinstance(data.get("tickets"), dict):
            raise CommsError("the ticket store %s has an unknown layout; it was left untouched" % self.path)
        return data

    @contextlib.contextmanager
    def transaction(self):
        with self.lock():
            data = self.load()
            yield data
            write_json_atomic(self.path, data)

    def all(self):
        tickets = list(self.load()["tickets"].values())
        return sorted(tickets, key=lambda ticket: id_sort_key(ticket.get("id")))

    def get(self, ticket_id, data=None):
        data = data if data is not None else self.load()
        ticket = data["tickets"].get(ticket_id)
        if ticket is None:
            raise CommsError("no ticket %s" % ticket_id)
        return ticket

    @staticmethod
    def add(data, ticket, suffix):
        """Assign the next id inside a transaction and store the validated ticket."""
        suffix = re.sub(r"[^A-Za-z0-9_.-]", "-", suffix or "x")[:32]
        highest = 0
        for ticket_id in data["tickets"]:
            match = ID_RE.match(ticket_id)
            if match:
                highest = max(highest, int(match.group(1)))
        number = max(int(data.get("next_number") or 1), highest + 1)
        ticket["id"] = "T%04d-%s" % (number, suffix)
        errors = validate_ticket(ticket)
        if errors:
            raise CommsError("ticket rejected: %s" % "; ".join(errors))
        data["tickets"][ticket["id"]] = ticket
        data["next_number"] = number + 1
        return ticket


def id_sort_key(ticket_id):
    match = ID_RE.match(ticket_id or "")
    if not match:
        return (sys.maxsize, ticket_id or "")
    return (int(match.group(1)), match.group(2))


# ---------------------------------------------------------------- permissions

def current_identity(require=True):
    role = os.environ.get("SUSHI_ROLE") or None
    seat = os.environ.get("SUSHI_SEAT") or None
    if require and not role:
        raise CommsError("set SUSHI_ROLE (and SUSHI_SEAT) for this seat before filing or changing tickets")
    if role and not ROLE_RE.match(role):
        raise CommsError("SUSHI_ROLE %r is not a valid role name" % role)
    if seat and not SEAT_RE.match(seat):
        raise CommsError("SUSHI_SEAT %r is not a valid seat name" % seat)
    return role, seat


def check_file_permission(registry, role, ticket_type, to_role):
    roles = registry.get("roles") or {}
    if role not in roles:
        return "role %r is not in the registry" % role
    if to_role not in roles:
        return "target role %r is not in the registry" % to_role
    channels = roles[role].get("channels") or {}
    if ticket_type not in channels:
        allowed = ", ".join(sorted(channels)) or "none"
        return "role %s may not file %s tickets (its channels: %s)" % (role, ticket_type, allowed)
    if to_role not in channels[ticket_type]:
        return "role %s may file %s tickets only to %s, not %s" % (role, ticket_type, ", ".join(channels[ticket_type]), to_role)
    return None


def check_status_permission(registry, role, seat, ticket, new_status):
    if new_status not in STATUSES:
        return "unknown status %r" % new_status
    if role == "orchestrator":
        return None
    roles = registry.get("roles") or {}
    if role not in roles:
        return "role %r is not in the registry" % role
    if role == ticket.get("to_role") and (ticket.get("to_seat") in (None, seat)):
        if new_status in (roles[role].get("may_set_status") or []):
            return None
        return "role %s may not set status %s" % (role, new_status)
    if seat and seat == ticket.get("from_seat") and role == ticket.get("from_role") and new_status in ("superseded", "closed"):
        return None  # a filer may withdraw its own ticket
    return "role %s/%s may not change ticket %s (addressed to %s)" % (role, seat or "-", ticket.get("id"), ticket.get("to_role"))


# ---------------------------------------------------------------- ticket commands

TEXT_ARGS = ("block", "batch", "branch", "head", "base", "intent", "touched", "tests", "risks",
             "reason", "failing", "question", "answer", "summary", "to_seat")


def build_ticket(role, seat, ticket_type, to_role, values):
    ticket = blank_ticket()
    ticket.update({
        "schema": SCHEMA_VERSION, "type": ticket_type, "status": "open",
        "from_role": role, "from_seat": seat, "to_role": to_role,
        "created": now_iso(),
    })
    ticket["updated"] = ticket["created"]
    for key in TEXT_ARGS:
        value = values.get(key)
        if value is not None:
            ticket[key] = value
    if values.get("options"):
        ticket["options"] = list(values["options"])
    if values.get("recommendation") is not None:
        ticket["recommendation"] = values["recommendation"]
    refs = values.get("refs")
    if refs:
        ticket["refs"] = list(refs)
    ticket["history"] = [history_entry(seat, role, "filed", "open")]
    return ticket


def file_ticket(project_root, registry, store, role, seat, ticket_type, to_role, values):
    problem = check_file_permission(registry, role, ticket_type, to_role)
    if problem:
        raise CommsError("refused: " + problem)
    ticket = build_ticket(role, seat, ticket_type, to_role, values)
    with store.transaction() as data:
        for ref in ticket.get("refs") or []:
            if ref not in data["tickets"]:
                raise CommsError("refused: referenced ticket %s does not exist" % ref)
        created = store.add(data, ticket, seat or role)
    log(project_root, "filed %s %s %s/%s -> %s" % (created["id"], ticket_type, role, seat, to_role))
    return created


def mutate_ticket(store, ticket_id, change):
    with store.transaction() as data:
        ticket = store.get(ticket_id, data)
        change(ticket)
        ticket["updated"] = now_iso()
        errors = validate_ticket(ticket)
        if errors:
            raise CommsError("ticket rejected: %s" % "; ".join(errors))
    return ticket


def command_ticket(args, project_root):
    store = TicketStore(store_path(project_root))
    action = args.action
    if action == "list":
        return ticket_list(store, args)
    if action == "show":
        ticket = store.get(args.id)
        if args.json:
            print(json.dumps(ticket, indent=2))
        else:
            for field in FIELDS:
                if ticket.get(field) not in (None, [], "") and field not in ("history", "source"):
                    print("%-15s %s" % (field + ":", ticket[field] if not isinstance(ticket[field], list) else ", ".join(map(str, ticket[field]))))
            for entry in ticket.get("history") or []:
                print("history:        %s %s/%s %s -> %s%s" % (entry["at"], entry["role"], entry["seat"] or "-",
                                                             entry["action"], entry["status"],
                                                             (" (%s)" % entry["detail"]) if entry["detail"] else ""))
            errors = validate_ticket(ticket)
            if errors:
                print("INVALID: " + "; ".join(errors))
        return 0
    registry = require_registry(project_root)
    role, seat = current_identity()
    if action == "file":
        values = {key: getattr(args, key, None) for key in TEXT_ARGS}
        values["options"] = args.option
        values["recommendation"] = args.recommend
        values["refs"] = args.ref
        ticket = file_ticket(project_root, registry, store, role, seat, args.type, args.to, values)
        print(json.dumps(ticket, indent=2) if args.json else "filed %s %s -> %s" % (ticket["id"], ticket["type"], ticket["to_role"]))
        return 0
    if action == "claim":
        def claim(ticket):
            if ticket["status"] != "open":
                raise CommsError("refused: %s is %s, not open" % (ticket["id"], ticket["status"]))
            problem = check_status_permission(registry, role, seat, ticket, "claimed")
            if problem:
                raise CommsError("refused: " + problem)
            ticket["status"] = "claimed"
            ticket["claimed_by"] = seat
            ticket["history"].append(history_entry(seat, role, "claimed", "claimed"))
        ticket = mutate_ticket(store, args.id, claim)
        print("claimed %s" % ticket["id"])
        return 0
    if action == "update":
        def update(ticket):
            problem = check_status_permission(registry, role, seat, ticket, args.status)
            if problem:
                raise CommsError("refused: " + problem)
            ticket["status"] = args.status
            if args.detail is not None:
                ticket["status_detail"] = args.detail or None
            ticket["history"].append(history_entry(seat, role, "status", args.status, args.detail))
        ticket = mutate_ticket(store, args.id, update)
        print("%s is now %s" % (ticket["id"], ticket["status"]))
        return 0
    if action == "close":
        def close(ticket):
            problem = check_status_permission(registry, role, seat, ticket, "closed")
            if problem:
                raise CommsError("refused: " + problem)
            if args.answer is not None:
                if ticket["type"] != "question":
                    raise CommsError("refused: only question tickets take an answer")
                if role != "orchestrator":
                    raise CommsError("refused: only the orchestrator answers questions")
                ticket["answer"] = args.answer
            ticket["status"] = "closed"
            if args.detail is not None:
                ticket["status_detail"] = args.detail or None
            ticket["history"].append(history_entry(seat, role, "closed", "closed", args.detail))
        ticket = mutate_ticket(store, args.id, close)
        print("closed %s" % ticket["id"])
        return 0
    if action == "route":
        def route(ticket):
            if role != "orchestrator":
                raise CommsError("refused: only the orchestrator routes tickets")
            if args.to not in (registry.get("roles") or {}):
                raise CommsError("refused: unknown role %r" % args.to)
            ticket["to_role"] = args.to
            ticket["to_seat"] = args.to_seat
            if ticket["status"] == "claimed":
                ticket["status"] = "open"
                ticket["claimed_by"] = None
            ticket["history"].append(history_entry(seat, role, "routed", ticket["status"],
                                                   "to %s%s" % (args.to, ("/" + args.to_seat) if args.to_seat else "")))
        ticket = mutate_ticket(store, args.id, route)
        print("routed %s to %s%s" % (ticket["id"], ticket["to_role"], ("/" + ticket["to_seat"]) if ticket["to_seat"] else ""))
        return 0
    raise CommsError("unknown ticket action %r" % action)


def ticket_list(store, args):
    tickets = [dict(t, _invalid="; ".join(validate_ticket(t))) if validate_ticket(t) else t for t in store.all()]
    role, seat = current_identity(require=False)
    rows = []
    for ticket in tickets:
        if "_invalid" in ticket:
            rows.append(ticket)
            continue
        if args.type and ticket.get("type") != args.type:
            continue
        if args.status and ticket.get("status") != args.status:
            continue
        if args.open and ticket.get("status") not in OPEN_STATUSES:
            continue
        if args.to and ticket.get("to_role") != args.to:
            continue
        if args.batch and ticket.get("batch") != args.batch:
            continue
        if args.mine:
            mine = (ticket.get("to_role") == role and ticket.get("to_seat") in (None, seat)) or \
                   (seat is not None and ticket.get("from_seat") == seat)
            if not mine:
                continue
        rows.append(ticket)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for ticket in rows:
        if "_invalid" in ticket:
            print("%-12s INVALID (%s)" % (ticket["id"], ticket["_invalid"]))
            continue
        where = []
        if ticket.get("batch"):
            where.append("batch=%s" % ticket["batch"])
        if ticket.get("block"):
            where.append("block=%s" % ticket["block"])
        what = ""
        if ticket.get("branch"):
            what = "%s @ %s" % (ticket["branch"], ticket.get("head") or "?")
        text = ticket.get("intent") or ticket.get("question") or ticket.get("reason") or ticket.get("summary") or ""
        if len(text) > 70:
            text = text[:67] + "..."
        print("%-12s %-13s %-10s %s/%s -> %s%s  %s  %s  %s" % (
            ticket["id"], ticket["type"], ticket["status"], ticket.get("from_role"), ticket.get("from_seat") or "-",
            ticket.get("to_role"), ("/" + ticket["to_seat"]) if ticket.get("to_seat") else "",
            " ".join(where), what, text))
    return 0


def git_head(branch):
    try:
        result = subprocess.run(["git", "rev-parse", "--short=9", branch + "^{commit}"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head if SHA_RE.match(head) else None


def command_handoff(args, project_root):
    registry = require_registry(project_root)
    role, seat = current_identity()
    store = TicketStore(store_path(project_root))
    head = args.head or git_head(args.branch)
    if not head:
        raise CommsError("could not read the head of %s with git; pass --head" % args.branch)
    batch = args.batch
    reserve = None
    if args.block:
        for ticket in store.all():
            if ticket.get("type") == "block-reserve" and ticket.get("block") == args.block \
                    and ticket.get("status") not in ("superseded", "closed") \
                    and (batch is None or ticket.get("batch") == batch) \
                    and ticket.get("to_seat") in (None, seat):
                reserve = ticket
        if reserve is not None and batch is None:
            batch = reserve["batch"]
    missing = [name for name in ("intent", "touched", "tests", "risks") if not getattr(args, name)]
    if missing:
        raise CommsError("a handoff needs --%s" % ", --".join(missing))
    values = {"branch": args.branch, "head": head, "base": args.base, "block": args.block, "batch": batch,
              "intent": args.intent, "touched": args.touched, "tests": args.tests, "risks": args.risks,
              "refs": [reserve["id"]] if reserve else None}
    superseded = []
    if not args.no_supersede:
        for ticket in store.all():
            if ticket.get("type") == "handoff" and ticket.get("branch") == args.branch \
                    and ticket.get("from_seat") == seat and seat is not None and ticket.get("status") == "open":
                superseded.append(ticket["id"])
    handoff = file_ticket(project_root, registry, store, role, seat, "handoff", "integrator", values)
    for old_id in superseded:
        def supersede(ticket, new_id=handoff["id"]):
            ticket["status"] = "superseded"
            ticket["status_detail"] = "by %s" % new_id
            ticket["history"].append(history_entry(seat, role, "superseded", "superseded", "by %s" % new_id))
        mutate_ticket(store, old_id, supersede)
    summary = "handoff %s @ %s%s" % (args.branch, head, (" block %s of batch %s" % (args.block, batch)) if args.block and batch else "")
    notify = file_ticket(project_root, registry, store, role, seat, "notify", "orchestrator",
                         {"refs": [handoff["id"]], "summary": summary[:400], "branch": args.branch, "head": head,
                          "block": args.block, "batch": batch})
    print("filed %s handoff -> integrator and %s notify -> orchestrator%s" % (
        handoff["id"], notify["id"], ("; superseded " + ", ".join(superseded)) if superseded else ""))
    return 0


# ---------------------------------------------------------------- registry commands

def command_registry(args, project_root):
    path = args.file or registry_path(project_root)
    if args.action == "init":
        role, _seat = current_identity()
        if role != "orchestrator":
            raise CommsError("refused: only the orchestrator creates the registry")
        if os.path.exists(path) and not args.force:
            raise CommsError("%s exists; pass --force to replace it with the example" % path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copyfile(EXAMPLE_REGISTRY, path)
        print("wrote %s (the example registry with the five roles); edit it, then run `registry validate`" % path)
        return 0
    if args.action == "validate":
        try:
            registry = load_registry(project_root, path)
        except ValueError as error:
            print("INVALID %s: not JSON (%s)" % (path, error))
            return 1
        if registry is None:
            print("no registry at %s" % path)
            return 1
        errors = validate_registry(registry)
        for error in errors:
            print("ERROR %s" % error)
        print("%s: %d role(s), %d drift rule(s), %d quorum rule(s), %d error(s)" % (
            path, len(registry.get("roles") or {}), len(registry.get("drift_rules") or []),
            len(registry.get("quorum_rules") or []), len(errors)))
        return 1 if errors else 0
    if args.action == "show":
        registry = require_registry(project_root)
        for name, role in registry["roles"].items():
            print("%s: %s" % (name, role["responsibilities"]))
            for ticket_type, targets in sorted((role.get("channels") or {}).items()):
                print("    may file %s -> %s" % (ticket_type, ", ".join(targets)))
            if role.get("may_set_status"):
                print("    may set status: %s" % ", ".join(role["may_set_status"]))
            if role.get("agent_types"):
                print("    agent types: %s" % ", ".join(role["agent_types"]))
        for rule in registry.get("drift_rules") or []:
            print("drift %s (%s, %s)%s" % (rule["id"], ", ".join(rule["roles"]), rule["mode"],
                                          "" if rule.get("enabled", True) else " disabled"))
        for rule in registry.get("quorum_rules") or []:
            print("quorum %s (%s)%s" % (rule["name"], rule["kind"], "" if rule.get("enabled", True) else " disabled"))
        return 0
    raise CommsError("unknown registry action %r" % args.action)


# ---------------------------------------------------------------- quorum and monitor

def describe_handoff(ticket):
    parts = []
    if ticket.get("block"):
        parts.append("block %s" % ticket["block"])
    parts.append("%s %s @ %s" % (ticket.get("from_seat") or ticket.get("from_role"), ticket.get("branch"), ticket.get("head")))
    return "%s (%s)" % (ticket["id"], ", ".join(parts))


def join_limited(items, limit=8):
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + ", +%d more" % (len(items) - limit)


def evaluate_quorum(tickets, registry):
    """Return [(fingerprint, line)] for every quorum rule that is met now."""
    results = []
    rules = [rule for rule in (registry.get("quorum_rules") or []) if rule.get("enabled", True)]
    if not rules:
        return results
    reserves_by_batch = {}
    for ticket in tickets:
        if ticket.get("type") == "block-reserve" and ticket.get("status") not in ("superseded", "closed") and ticket.get("batch"):
            reserves_by_batch.setdefault(ticket["batch"], {})[ticket.get("block")] = ticket
    handoff_by_slot = {}
    for ticket in sorted(tickets, key=lambda item: id_sort_key(item.get("id"))):
        if ticket.get("type") == "handoff" and ticket.get("status") in HANDOFF_COUNTED and ticket.get("batch") and ticket.get("block"):
            handoff_by_slot[(ticket["batch"], ticket["block"])] = ticket  # newest wins
    for rule in rules:
        kind = rule["kind"]
        if kind in ("all-reserved", "n-of-m"):
            for batch in sorted(reserves_by_batch):
                blocks = reserves_by_batch[batch]
                total = len(blocks)
                arrived = [handoff_by_slot[(batch, block)] for block in sorted(blocks, key=str) if (batch, block) in handoff_by_slot]
                count = len(arrived)
                if kind == "all-reserved":
                    met = total > 0 and count == total
                else:
                    threshold = rule["min"] if rule.get("min") is not None else max(1, int(-(-rule["min_fraction"] * total // 1)))
                    met = count >= threshold and (count < total or not rule.get("below_all", True))
                if not met:
                    continue
                ids = sorted(ticket["id"] for ticket in arrived)
                fingerprint = hashlib.sha1(("%s|%s|%s" % (rule["name"], batch, ",".join(ids))).encode()).hexdigest()[:16]
                line = "Batch %s: %d of %d handoffs in (rule %s): %s." % (
                    batch, count, total, rule["name"], join_limited([describe_handoff(ticket) for ticket in arrived]))
                results.append((fingerprint, line))
        elif kind == "count":
            ticket_type = rule.get("type", "handoff")
            statuses = rule.get("statuses", ["open"])
            matching = [ticket for ticket in tickets if ticket.get("type") == ticket_type and ticket.get("status") in statuses
                        and (rule.get("to_role") is None or ticket.get("to_role") == rule["to_role"])]
            if len(matching) >= rule["min"]:
                ids = sorted((ticket["id"] for ticket in matching), key=id_sort_key)
                fingerprint = hashlib.sha1(("%s||%s" % (rule["name"], ",".join(ids))).encode()).hexdigest()[:16]
                items = [describe_handoff(ticket) if ticket_type == "handoff" else ticket["id"] for ticket in matching]
                line = "Rule %s: %d %s tickets%s are %s: %s." % (
                    rule["name"], len(matching), ticket_type,
                    (" to " + rule["to_role"]) if rule.get("to_role") else "", "/".join(statuses), join_limited(items))
                results.append((fingerprint, line))
    return results


def quorum_state_path(project_root):
    return os.path.join(state_dir(project_root), "quorum_seen.json")


def scan_once(project_root, store, registry):
    """Print each newly met quorum state once. Returns the printed lines."""
    seen = read_json(quorum_state_path(project_root), {}) or {}
    printed = []
    for fingerprint, line in evaluate_quorum(store.all(), registry):
        if fingerprint in seen:
            continue
        seen[fingerprint] = int(time.time())
        print(line, flush=True)
        printed.append(line)
    if printed:
        write_json_atomic(quorum_state_path(project_root), seen)
    return printed


def store_signature(path, registry_file):
    parts = []
    for item in (path, registry_file):
        try:
            info = os.stat(item)
            parts.append((info.st_mtime_ns, info.st_size))
        except OSError:
            parts.append((0, 0))
    return tuple(parts)


def command_monitor(args, project_root):
    role = os.environ.get("SUSHI_ROLE")
    if role != "orchestrator":
        return 0  # the dropbox runs only in the orchestrator's session; stay silent elsewhere
    config = load_config(project_root)
    if not config.get("monitor", True):
        return 0
    interval = args.interval or float(config.get("monitor_interval_s") or 5)
    store = TicketStore(store_path(project_root, config))
    registry_file = registry_path(project_root)
    last_signature = None
    parent = os.getppid()
    while True:
        try:
            signature = store_signature(store.path, registry_file)
            if signature != last_signature:
                last_signature = signature
                registry = load_registry(project_root, registry_file)
                problems = validate_registry(registry) if registry is not None else ["no registry"]
                if problems:
                    log(project_root, "monitor: registry unusable (%s); staying silent" % "; ".join(problems[:3]))
                else:
                    scan_once(project_root, store, registry)
        except Exception as error:  # the monitor must not spam the session; log and keep going
            log(project_root, "monitor error: %r" % (error,))
        if args.once:
            return 0
        if os.getppid() != parent:
            return 0  # orphaned: the session ended
        time.sleep(interval)


def command_quorum(args, project_root):
    registry = require_registry(project_root)
    store = TicketStore(store_path(project_root))
    results = evaluate_quorum(store.all(), registry)
    for _fingerprint, line in results:
        print(line)
    if not results:
        print("no quorum rule is met")
    return 0


# ---------------------------------------------------------------- INTEGRATION_QUEUE.md interop

QUEUE_HEADER_RE = re.compile(
    r"^### (?P<branch>.+?) @ (?P<head>[0-9a-fA-F]{6,40})\s+\((?P<note>.*)\)\s+status:\s*(?P<status>.*?)\s*$")
QUEUE_FIELD_RE = re.compile(r"^- (intent|touched|tests|risks):\s?(.*)$")


def parse_status_text(text):
    match = re.match(r"^(\w+)\s*(.*)$", text or "")
    if not match:
        return "other", text or None, None
    word, rest = match.group(1).lower(), match.group(2)
    if word == "queued":
        claimed = re.match(r"^\(claimed by ([A-Za-z0-9_.-]{1,32})\)$", rest)
        if claimed:
            return "claimed", None, claimed.group(1)
        return "open", rest or None, None
    if word in STATUSES and word not in ("open", "claimed"):
        return word, rest or None, None
    return "other", text, None


def render_status_text(ticket):
    status, detail = ticket.get("status"), ticket.get("status_detail")
    if status == "open":
        return "queued" + ((" " + detail) if detail else "")
    if status == "claimed":
        return "queued (claimed by %s)" % (ticket.get("claimed_by") or "?")
    if status == "other":
        return detail or ""
    return status + ((" " + detail) if detail else "")


def parse_queue(text):
    """Return (entries, skipped). Each entry: line, header, body (raw lines), parsed header fields."""
    lines = text.split("\n")
    has_queue_heading = any(line.strip() == "## Queue" for line in lines)
    in_queue = not has_queue_heading
    in_fence = False
    entries, skipped = [], []
    current = None

    def finish():
        if current is not None:
            body = current["body"]
            while body and not body[-1].strip():
                body.pop()
            entries.append(current)

    for number, line in enumerate(lines, start=1):
        if line.startswith("```"):
            in_fence = not in_fence
            if current is not None:
                current["body"].append(line)
            continue
        if in_fence:
            if current is not None:
                current["body"].append(line)
            continue
        if line.startswith("## ") or line.startswith("### "):
            finish()
            current = None
            if line.strip() == "## Queue":
                in_queue = True
                continue
            if not in_queue:
                continue
            match = QUEUE_HEADER_RE.match(line)
            if match and line.startswith("### "):
                current = {"line": number, "header": line, "body": [], "match": match.groupdict()}
            else:
                skipped.append((number, line[:120]))
            continue
        if current is not None:
            current["body"].append(line)
    finish()
    return entries, skipped


def ticket_from_queue_entry(entry, source_name):
    fields = entry["match"]
    note = fields["note"]
    ticket = blank_ticket()
    status, detail, claimed_by = parse_status_text(fields["status"])
    seat_match = re.match(r"\s*(w\d+)\b", note)
    base = None
    base_match = re.search(r"\bbase\s+(\S+?)(?:[,;)]|\s|$)(?:(\S+?)(?:[,;)]|\s|$))?", note)
    if base_match:
        first, second = base_match.group(1), base_match.group(2)
        if not SHA_RE.match(first) and second and SHA_RE.match(second):
            base = second
        else:
            base = first
    block_match = re.search(r"\bblock (\S+?) of batch ([^\s,;()]+)", note)
    values = {"intent": "", "touched": "", "tests": "", "risks": ""}
    found = set()
    current_key = None
    for line in entry["body"]:
        match = QUEUE_FIELD_RE.match(line)
        if match and match.group(1) not in found:
            current_key = match.group(1)
            found.add(current_key)
            values[current_key] = match.group(2).strip()
        elif line.startswith("- "):
            current_key = None
        elif current_key and line.startswith((" ", "\t")) and line.strip():
            values[current_key] = (values[current_key] + " " + line.strip()).strip()
    now = now_iso()
    ticket.update({
        "schema": SCHEMA_VERSION, "type": "handoff", "status": status, "status_detail": detail,
        "claimed_by": claimed_by, "from_seat": seat_match.group(1) if seat_match else None,
        "from_role": "coder", "to_role": "integrator", "branch": fields["branch"], "head": fields["head"],
        "base": base, "block": block_match.group(1) if block_match else None,
        "batch": block_match.group(2) if block_match else None,
        "intent": values["intent"], "touched": values["touched"], "tests": values["tests"], "risks": values["risks"],
        "created": now, "updated": now,
        "source": {"kind": "queue-import", "file": source_name, "line": entry["line"], "header": entry["header"],
                   "note": note, "status_text": fields["status"], "body": list(entry["body"])},
    })
    importer_role = os.environ.get("SUSHI_ROLE") or "orchestrator"
    ticket["history"] = [history_entry(os.environ.get("SUSHI_SEAT") or None, importer_role, "imported", status,
                                       ("%s line %d" % (source_name, entry["line"]))[:200])]
    return ticket


def build_note(ticket):
    note = ticket.get("from_seat") or ticket.get("from_role") or "?"
    if ticket.get("base"):
        note += ", base %s" % ticket["base"]
    if ticket.get("block") and ticket.get("batch"):
        note += "; block %s of batch %s" % (ticket["block"], ticket["batch"])
    return note


def render_queue_entry(ticket):
    source = ticket.get("source") or {}
    imported = source.get("kind") == "queue-import"
    status_text = render_status_text(ticket)
    if imported and source.get("status_text") == status_text:
        header = source["header"]
    else:
        note = source.get("note") if imported else build_note(ticket)
        header = "### %s @ %s  (%s)  status: %s" % (ticket["branch"], ticket["head"], note, status_text)
    if imported:
        body = list(source.get("body") or [])
    else:
        body = ["- intent: %s" % ticket["intent"], "- touched: %s" % ticket["touched"],
                "- tests: %s" % ticket["tests"], "- risks: %s" % ticket["risks"]]
    return "\n".join([header] + body)


def command_export_queue(args, project_root):
    store = TicketStore(store_path(project_root))
    blocks = []
    for ticket in store.all():
        if ticket.get("type") != "handoff":
            continue
        if not args.all and ticket.get("status") not in OPEN_STATUSES:
            continue
        blocks.append(render_queue_entry(ticket))
    text = "\n\n".join(blocks) + ("\n" if blocks else "")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print("wrote %d entr%s to %s" % (len(blocks), "y" if len(blocks) == 1 else "ies", args.out))
    else:
        sys.stdout.write(text)
    return 0


def command_import_queue(args, project_root):
    with open(args.file, encoding="utf-8") as handle:
        text = handle.read()
    entries, skipped = parse_queue(text)
    name = os.path.basename(args.file)
    tickets = [ticket_from_queue_entry(entry, name) for entry in entries]
    store = TicketStore(store_path(project_root))
    existing = {(ticket.get("branch"), ticket.get("head")) for ticket in store.all() if ticket.get("type") == "handoff"}
    fresh, duplicate = [], 0
    for ticket in tickets:
        key = (ticket["branch"], ticket["head"])
        if key in existing:
            duplicate += 1
            continue
        existing.add(key)
        fresh.append(ticket)
    by_status = {}
    for ticket in tickets:
        by_status[ticket["status"]] = by_status.get(ticket["status"], 0) + 1
    print("%s: %d entr%s parsed (%s), %d heading(s) skipped as non-standard, %d already imported" % (
        args.file, len(tickets), "y" if len(tickets) == 1 else "ies",
        ", ".join("%s %d" % item for item in sorted(by_status.items())), len(skipped), duplicate))
    for number, header in skipped:
        print("  skipped line %d: %s" % (number, header))
    invalid = [(ticket, validate_ticket(dict(ticket, id="T0001-import"))) for ticket in fresh]
    invalid = [(ticket, errors) for ticket, errors in invalid if errors]
    for ticket, errors in invalid:
        print("  invalid line %d: %s" % (ticket["source"]["line"], "; ".join(errors)))
    if not args.apply:
        print("dry run: would create %d ticket(s) in %s; pass --apply to write them" % (len(fresh) - len(invalid), store.path))
        return 1 if invalid else 0
    role, seat = current_identity()
    if role != "orchestrator":
        raise CommsError("refused: only the orchestrator imports the queue")
    created = 0
    with store.transaction() as data:
        for ticket in fresh:
            if any(ticket is bad for bad, _errors in invalid):
                continue
            store.add(data, ticket, seat or role)
            created += 1
    print("created %d ticket(s) in %s" % (created, store.path))
    return 1 if invalid else 0


# ---------------------------------------------------------------- hooks

TICKET_WRITE_RE = re.compile(
    r"\bsushicomms(?:\.py)?\b[\"']?\s+(?:--project\s+\S+\s+)?"
    r"(?:ticket\s+(?:file|claim|update|route|close)|handoff|registry\s+init|import-queue\b.*--apply)")
SUSHICOMMS_RE = re.compile(r"sushicomms\.py\b|(?:^|[\s;&|(])sushicomms\s")
REGISTRY_WRITE_HINT = re.compile(r"(?<![0-9&>])>(?!&)(?!\s*/dev/null)|\btee\b|\bsed\s+-i|\bperl\s+-[a-z]*i|\bmv\b|\bcp\b|\brm\b|\btruncate\b|\bpython3?\b|\bdd\b|\bln\b")


def emit(output):
    print(json.dumps(output))


def deny(reason):
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                 "permissionDecisionReason": reason}})


def hook_project_roots(event, extra_path=None):
    roots = []
    for candidate in (extra_path, os.environ.get("CLAUDE_PROJECT_DIR"), event.get("cwd")):
        if candidate:
            root = find_project_root(candidate)
            if root and root not in roots:
                roots.append(root)
    return roots


def drift_seen_path(project_root):
    return os.path.join(state_dir(project_root), "drift_seen.json")


def remind_once(project_root, session, key):
    """True the first time `key` is seen in this session (state kept per project)."""
    path = drift_seen_path(project_root)
    seen = read_json(path, {}) or {}
    cutoff = time.time() - 14 * 86400
    seen = {name: value for name, value in seen.items() if isinstance(value, dict) and value.get("_t", 0) >= cutoff}
    session_seen = seen.setdefault(session or "no-session", {"_t": int(time.time())})
    fresh = key not in session_seen
    session_seen[key] = int(time.time())
    session_seen["_t"] = int(time.time())
    write_json_atomic(path, seen)
    return fresh


def store_references(roots, cwd=None):
    """Spellings of the store's location that a shell command could use."""
    home = os.path.expanduser("~")
    needles = {"/.sushi/state/sushicomms", ".sushi/state/sushicomms"}
    for root in roots:
        try:
            path = store_path(root)
        except (OSError, ValueError):
            continue
        for spelled in {path, os.path.realpath(path)}:
            needles.add(spelled)
            if spelled.startswith(home + os.sep):
                needles.add("~" + spelled[len(home):])
            for base in (root, cwd):
                if base and spelled.startswith(os.path.realpath(base) + os.sep):
                    needles.add(os.path.relpath(spelled, os.path.realpath(base)))
                elif base and spelled.startswith(base + os.sep):
                    needles.add(os.path.relpath(spelled, base))
        name = os.path.basename(path)
        if name != "state.json":
            needles.add(name)
    return {needle for needle in needles if needle and needle not in (".", os.sep)}


STORE_DENIAL = ("SushiComms: direct reads and edits of the ticket store are blocked. Read tickets with "
                "%s ticket list [--open|--mine] or ticket show ID; change them with ticket file|claim|update|close.")


def command_hook_bash():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    if not command:
        return 0
    roots = hook_project_roots(event)
    project_root = roots[0] if roots else None
    registry = None
    try:
        registry = load_registry(project_root) if project_root else None
        if registry is not None and validate_registry(registry):
            log(project_root, "hook-bash: registry invalid; drift rules skipped")
            registry = None
    except ValueError:
        log(project_root, "hook-bash: registry is not JSON; drift rules skipped")
        registry = None
    role, source = hook_identity(event, registry)

    # 0. The ticket store is read and written only by the program, for every role.
    if not SUSHICOMMS_RE.search(command):
        for needle in store_references(roots, event.get("cwd")):
            if needle in command:
                deny(STORE_DENIAL % cli_command())
                log(project_root, "denied shell access to the store (role=%s): %s" % (role, command[:200]))
                return 0

    # 1. Registry protection through the shell (best effort; the Edit/Write hook is the main guard).
    if role != "orchestrator" and ".sushi/sushicomms" in command and REGISTRY_WRITE_HINT.search(command) \
            and not SUSHICOMMS_RE.search(command):
        deny("SushiComms: only the coordinator (orchestrator role) edits the role registry (this seat: %s). "
             "To ask for a change, file a question ticket to the orchestrator." % (role or "no role"))
        return 0

    # 2. Identity: a seat files tickets only as its own role.
    if TICKET_WRITE_RE.search(command):
        explicit = re.findall(r"\bSUSHI_ROLE=([A-Za-z0-9_-]+)", command)
        effective = explicit[-1] if explicit else os.environ.get("SUSHI_ROLE")
        if source == "unmapped-subagent":
            deny("SushiComms: this subagent's agent type (%s) has no role in the registry, so it cannot file or "
                 "change tickets. Report back to the session that started you instead." % event.get("agent_type"))
            return 0
        if role and effective != role:
            deny("SushiComms: this seat's role is %s, so run SushiComms as that role: prefix the command with "
                 "SUSHI_ROLE=%s (and SUSHI_SEAT=<your seat>)." % (role, role))
            return 0

    # 3. Drift rules from the registry.
    if registry is None or not role:
        return 0
    denials, reminders, fired = [], [], []
    for rule in registry.get("drift_rules") or []:
        if not rule.get("enabled", True) or role not in rule.get("roles", []):
            continue
        if not re.search(expand_pattern(rule["pattern"], registry), command, re.MULTILINE):
            continue
        if rule.get("unless") and re.search(expand_pattern(rule["unless"], registry), command, re.MULTILINE):
            continue
        message = expand_message(rule["message"], registry)
        if rule["mode"] == "deny":
            denials.append(message)
        else:
            fired.append((rule["id"], message))
    if denials:
        deny(" ".join(denials))
        log(project_root, "drift deny role=%s: %s" % (role, command[:200]))
        return 0
    for rule_id, message in fired:
        if remind_once(project_root, event.get("session_id"), rule_id):
            reminders.append(message)
    if reminders:
        emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "\n".join(reminders)}})
    return 0


def protected_kind(path, roots):
    real = os.path.realpath(path)
    for root in roots:
        sushicomms_dir = os.path.realpath(os.path.join(root, ".sushi", "sushicomms"))
        config_file = os.path.realpath(os.path.join(root, ".sushi", "sushicomms.json"))
        registry_file = os.path.realpath(registry_path(root))
        if real == config_file or real == registry_file or real.startswith(sushicomms_dir + os.sep):
            return "registry", root
        state_directory = os.path.realpath(os.path.join(root, ".sushi", "state", "sushicomms"))
        try:
            store = os.path.realpath(store_path(root))
        except (OSError, ValueError):
            store = None
        if real == state_directory or real.startswith(state_directory + os.sep) or \
                (store and (real == store or real.startswith(store + "."))):
            return "store", root
    return None, None


READ_TOOLS = ("Read", "Grep", "Glob")


def command_hook_edit():
    """Guard for Edit/Write/MultiEdit/NotebookEdit and Read/Grep/Glob."""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_input = event.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path") or ""
    if not path:
        return 0
    if not os.path.isabs(path):
        path = os.path.join(event.get("cwd") or os.getcwd(), path)
    roots = hook_project_roots(event, extra_path=path)
    kind, root = protected_kind(path, roots)
    if kind is None:
        return 0
    registry = None
    try:
        registry = load_registry(root)
    except ValueError:
        registry = None
    role, _source = hook_identity(event, registry)
    if kind == "registry":
        if role == "orchestrator" or event.get("tool_name") in READ_TOOLS:
            return 0
        deny("SushiComms: only the coordinator (orchestrator role) edits the role registry (this seat: %s). "
             "To ask for a change, file a question ticket to the orchestrator." % (role or "no SUSHI_ROLE"))
        log(root, "denied registry edit by role=%s: %s" % (role, path))
        return 0
    deny(STORE_DENIAL % cli_command())
    log(root, "denied direct store access by role=%s via %s: %s" % (role, event.get("tool_name"), path))
    return 0


def command_hook_agent():
    """Note (additionalContext, never a denial) when a seat other than the orchestrator starts an agent."""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if event.get("tool_name") not in ("Agent", "Task"):
        return 0
    roots = hook_project_roots(event)
    project_root = roots[0] if roots else None
    try:
        registry = load_registry(project_root) if project_root else None
    except ValueError:
        registry = None
    if registry is None or validate_registry(registry):
        return 0  # no SushiComms roles in this project
    spawn = (registry.get("settings") or {}).get("agent_spawn") or {}
    if not spawn.get("reminder", True):
        return 0
    role, _source = hook_identity(event, registry)
    if role == "orchestrator":
        return 0
    subagent_type = (event.get("tool_input") or {}).get("subagent_type") or ""
    if subagent_type in (spawn.get("allowed_subagent_types") or []) or \
            subagent_type.split(":")[-1] in (spawn.get("allowed_subagent_types") or []):
        return 0
    if not remind_once(project_root, event.get("session_id"), "agent-spawn"):
        return 0
    message = spawn.get("message") or (
        "Only the coordinator (orchestrator role) starts agents; this seat is %s. "
        "Ask for outside work with a question ticket: "
        "{cli} ticket file --type question --to orchestrator --question \"...\" --option ... --recommend 1")
    message = expand_message(message.replace("%s", role or "no SUSHI_ROLE"), registry)
    emit({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": message}})
    return 0


# ---------------------------------------------------------------- main

def build_parser():
    parser = argparse.ArgumentParser(prog="sushicomms", description="Structured role tickets for Claude Code seats.")
    parser.add_argument("--project", help="project root (default: nearest ancestor with .sushi/ or .git)")
    commands = parser.add_subparsers(dest="command")

    ticket = commands.add_parser("ticket", help="file, claim, update, close, list or show tickets")
    actions = ticket.add_subparsers(dest="action")
    file_parser = actions.add_parser("file")
    file_parser.add_argument("--type", required=True, choices=TICKET_TYPES)
    file_parser.add_argument("--to", required=True, help="target role")
    file_parser.add_argument("--to-seat", dest="to_seat")
    for name in ("block", "batch", "branch", "head", "base", "intent", "touched", "tests", "risks",
                 "reason", "failing", "question", "summary"):
        file_parser.add_argument("--" + name)
    file_parser.add_argument("--option", action="append", help="a numbered option (question tickets; repeat)")
    file_parser.add_argument("--recommend", type=int, help="1-based index of the recommended option")
    file_parser.add_argument("--ref", action="append", help="a related ticket id (repeat)")
    file_parser.add_argument("--json", action="store_true")
    file_parser.set_defaults(answer=None)
    claim = actions.add_parser("claim")
    claim.add_argument("id")
    update = actions.add_parser("update")
    update.add_argument("id")
    update.add_argument("--status", required=True, choices=STATUSES)
    update.add_argument("--detail", help="one line, e.g. integration/batch3 (7e8862c2)")
    route = actions.add_parser("route")
    route.add_argument("id")
    route.add_argument("--to", required=True)
    route.add_argument("--to-seat", dest="to_seat")
    close = actions.add_parser("close")
    close.add_argument("id")
    close.add_argument("--answer")
    close.add_argument("--detail")
    listing = actions.add_parser("list")
    listing.add_argument("--type", choices=TICKET_TYPES)
    listing.add_argument("--status", choices=STATUSES + IMPORT_ONLY_STATUSES)
    listing.add_argument("--to")
    listing.add_argument("--batch")
    listing.add_argument("--open", action="store_true")
    listing.add_argument("--mine", action="store_true")
    listing.add_argument("--json", action="store_true")
    show = actions.add_parser("show")
    show.add_argument("id")
    show.add_argument("--json", action="store_true")

    handoff = commands.add_parser("handoff", help="coder: file a handoff to the integrator plus a notify to the orchestrator")
    handoff.add_argument("--branch", required=True)
    for name in ("head", "base", "block", "batch", "intent", "touched", "tests", "risks"):
        handoff.add_argument("--" + name)
    handoff.add_argument("--no-supersede", action="store_true",
                         help="keep this seat's earlier open handoff for the same branch open")

    registry = commands.add_parser("registry", help="init, validate or show the role registry")
    registry.add_argument("action", choices=("init", "validate", "show"))
    registry.add_argument("file", nargs="?")
    registry.add_argument("--force", action="store_true")

    commands.add_parser("quorum", help="print every quorum rule that is met now")
    monitor = commands.add_parser("monitor", help="batch monitor (orchestrator seat only)")
    monitor.add_argument("--project", default=argparse.SUPPRESS, help="project root (may be empty: then the cwd)")
    monitor.add_argument("--once", action="store_true")
    monitor.add_argument("--interval", type=float)

    export = commands.add_parser("export-queue", help="render handoff tickets as INTEGRATION_QUEUE.md entries")
    export.add_argument("--all", action="store_true", help="include handoffs that are no longer open")
    export.add_argument("--out")
    importer = commands.add_parser("import-queue", help="parse INTEGRATION_QUEUE.md entries into tickets")
    importer.add_argument("file")
    importer.add_argument("--apply", action="store_true")

    commands.add_parser("hook-bash")
    commands.add_parser("hook-edit")
    commands.add_parser("hook-agent")
    commands.add_parser("paths")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    hooks = {"hook-bash": command_hook_bash, "hook-edit": command_hook_edit, "hook-agent": command_hook_agent}
    if argv and argv[0] in hooks:
        try:
            return hooks[argv[0]]()
        except Exception as error:  # a hook must never break the session
            log(None, "%s error: %r" % (argv[0], error))
            return 0
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        print(__doc__)
        return 2
    if args.command == "monitor":
        explicit = args.project if args.project and args.project.strip() else None
        try:
            return command_monitor(args, resolve_project(explicit))
        except Exception as error:  # never let the monitor print a traceback into the session
            log(None, "monitor fatal: %r" % (error,))
            return 0
    project_root = resolve_project(args.project)
    try:
        if args.command == "ticket":
            if not args.action:
                parser.parse_args(["ticket", "--help"])
            return command_ticket(args, project_root)
        if args.command == "handoff":
            return command_handoff(args, project_root)
        if args.command == "registry":
            return command_registry(args, project_root)
        if args.command == "quorum":
            return command_quorum(args, project_root)
        if args.command == "export-queue":
            return command_export_queue(args, project_root)
        if args.command == "import-queue":
            return command_import_queue(args, project_root)
        if args.command == "paths":
            config = load_config(project_root)
            print("project:  %s" % project_root)
            print("registry: %s" % registry_path(project_root))
            print("store:    %s" % store_path(project_root, config))
            print("state:    %s" % os.path.join(project_root or "~", ".sushi", "state", "sushicomms"))
            return 0
    except CommsError as error:
        print("sushicomms: %s" % error, file=sys.stderr)
        return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
