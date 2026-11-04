"""Start agent sessions from a device's start key: a dry run that shows the plan, then a confirm that runs it.

A device (the Hive app) enrolls a separate *start key*. Once a person approves it with `tailshare keys approve`,
its authorized_keys line is `restrict,command="tailshare ssh-gate --start=safe|skip --key=k…"`, and its only
accepted command is `tailshare-start`, with one JSON document on stdin:

  {"v":1, "dry_run":true, "starts":[{kind, cwd, name, options, instructions?, prompt?, device_name?}, …]}
  {"v":1, "confirm":["<plan_id>", …]}

The reply is one JSON line on stdout with `results` in the same order, each a plan, a start, or {error, field}.
Nothing runs on a dry run. A plan is kept for PLAN_TTL, belongs to the key that asked for it, and runs at most
once: it is rebuilt at confirm and runs only if it is still exactly what the device showed.

Starting is off unless ~/.tailshare/start.json exists (owned by this user, not writable by others). tailshare
never writes that file and never starts sessions as root. See docs/start.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path

from . import bus

PLAN_TTL = 300  # a plan waits this long for its confirm
KEEP = 600  # a used plan's result is kept this long, for a retried confirm
MAX_INPUT = 256 * 1024
MAX_OUTPUT = 512 * 1024  # 32 plans, each with its instructions once
MAX_ITEMS = 32  # a whole squad on one host
CONFIRMS_PER_MIN = 6  # per key; counts confirm calls, not sessions
# the plan key dry-runs without Face ID, so a copy of it could fill start.db: cap the open plans per key pair
MAX_OPEN_PLANS = 4 * MAX_ITEMS
PROMPT_MAX = 8 * 1024
INSTRUCTIONS_MAX = 4 * 1024
NAME_MAX = 64
MAX_DIRS = 8
# Each started agent runs on its own tmux server, <TMUX_DIR>/ts-<name> (what `tmux -L ts-<name>` names), never
# the user's default one: a crash or `kill-server` there takes one agent down, not all. "": tmux's own per-user
# folder, /tmp/tmux-<uid>. TMUX_SOCKET pins every start to one server instead (tests: in-process or by
# `ssh-gate --tmux-socket` on the key's command= line). Neither comes from the environment, which an ssh client
# may send.
TMUX_SOCKET = ""
TMUX_DIR = ""
LABEL = "ts-"
# where starts went before each agent had its own server: "" is the default server (tests: a private one)
LEGACY_SOCKET = ""
SESSION = "tailshare-start"
INSTRUCTIONS = "<instructions>"  # stands for the instructions in a plan's argv
PROMPT = "<prompt>"  # and the prompt

_WORD = re.compile(r"[A-Za-z0-9._-]{1,64}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PLAN_ID = re.compile(r"[0-9a-f]{32}")
_KEY_ID = re.compile(r"k[0-9a-f]{12}")

# What each kind takes. An option is only accepted if it is here *and* in the host's start.json.
KINDS = {
    "claude": {
        "permission_mode": ("enum", ("default", "acceptEdits", "plan", "dontAsk", "auto", "bypassPermissions")),
        "skip_permissions": ("bool",),
        "model": ("word",),
        "effort": ("word",),
        "resume": ("uuid",),
        "add_dirs": ("dirs",),
    },
    "codex": {
        "approval": ("enum", ("untrusted", "on-request", "never")),
        "sandbox": ("enum", ("read-only", "workspace-write", "danger-full-access")),
        "skip_permissions": ("bool",),
        "model": ("word",),
        "effort": ("word",),
        "resume": ("uuid",),
        "add_dirs": ("dirs",),
    },
}


class Refused(Exception):
    def __init__(self, error: str, field: str = "", reason: str = "", **extra) -> None:
        super().__init__(reason or error)
        self.out = {"error": error, **({"field": field} if field else {}), **({"reason": reason} if reason else {}), **extra}


def config_path() -> Path:
    return bus.STATE_DIR / "start.json"


def db_path() -> Path:
    return bus.STATE_DIR / "start.db"


def skips(kind: str, opts: dict) -> bool:
    """Whether these options run the agent without asking: skip itself, or a mode that amounts to it."""
    if opts.get("skip_permissions") is True:
        return True
    if kind == "claude":
        return opts.get("permission_mode") == "bypassPermissions"
    return opts.get("sandbox") == "danger-full-access"


def autos(kind: str, opts: dict) -> bool:
    """Whether these options run Claude in auto mode: its own mode, not skipping (Nicholas, 2026-11-02)."""
    return kind == "claude" and opts.get("permission_mode") == "auto"


# --------------------------------------------------------------------------- the host's config


def load_config(path: Path | None = None) -> dict | None:
    """The host's start.json, checked; None when starting is off here. Raises Refused for a file to fix."""
    path = path or config_path()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return None
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        raise Refused("off", reason=f"cannot read start.json ({e.strerror})") from None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise Refused("off", reason="start.json must be a plain file of this user, not writable by group or others")
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        raise Refused("off", reason="start.json is not valid JSON") from None
    if not isinstance(d, dict) or d.get("enabled") is not True:
        return None
    roots = []
    for r in d.get("roots") or []:
        if isinstance(r, str) and r:
            p = os.path.realpath(os.path.expanduser(r))
            if os.path.isabs(p) and os.path.isdir(p) and p != "/":
                roots.append(p)
    kinds = {}
    for k, v in (d.get("kinds") or {}).items() if isinstance(d.get("kinds"), dict) else ():
        if k in KINDS and isinstance(v, dict):
            opts = [o for o in v.get("options") or [] if isinstance(o, str) and o in KINDS[k]]
            b = v.get("bin")
            kinds[k] = {"options": opts, "bin": b if isinstance(b, str) and b.startswith("/") else ""}
    mr = d.get("max_running")
    return {"roots": roots, "kinds": kinds, "skip_permissions": d.get("skip_permissions") is True,
            "auto": d.get("auto") is True,
            "max_running": mr if isinstance(mr, int) and not isinstance(mr, bool) and 0 < mr <= 100 else 12}


def capabilities(path: Path | None = None) -> dict | None:
    """The /hello `start` block for the owner's devices: what this host takes, never its folders."""
    try:
        cfg = load_config(path)
    except Refused:
        return None
    if not cfg or not cfg["roots"] or not cfg["kinds"]:
        return None
    out = {"kinds": sorted(cfg["kinds"]), "options": {k: v["options"] for k, v in sorted(cfg["kinds"].items())},
           "skip": cfg["skip_permissions"]}
    if "permission_mode" in cfg["kinds"].get("claude", {}).get("options", []):
        # the values a device may pick: auto only where this host allows it, bypassPermissions only with skip
        out["permission_modes"] = {"claude": [m for m in KINDS["claude"]["permission_mode"][1]
                                              if (m != "auto" or cfg["auto"])
                                              and (m != "bypassPermissions" or cfg["skip_permissions"])]}
    if "model" in cfg["kinds"].get("codex", {}).get("options", []):
        out["models"] = {"codex": codex_models()}
    return out


def codex_models(limit: int = 30) -> list[str]:
    """The models this host's Codex offers in its picker (its models_cache.json), so a device need not guess."""
    home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
    try:
        doc = json.loads((home / "models_cache.json").read_text())
        rows = doc.get("models") or []
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for m in rows:
        slug = m.get("slug") if isinstance(m, dict) else None
        if (m.get("visibility") == "list" if isinstance(m, dict) else False) and isinstance(slug, str) \
                and len(slug) <= 40 and _WORD.fullmatch(slug) and not slug.startswith("-") and slug not in out:
            out.append(slug)
    return out[:limit]


def folders(path: Path | None = None, depth: int = 2, limit: int = 200) -> list[str]:
    """Folders under the roots a device may pick from: two levels deep, no hidden ones."""
    try:
        cfg = load_config(path)
    except Refused:
        return []
    out: list[str] = []
    for root in (cfg or {}).get("roots", []):
        level = [root]
        for _ in range(depth):
            nxt = []
            for d in level:
                try:
                    names = sorted(os.listdir(d))
                except OSError:
                    continue
                for n in names:
                    p = os.path.join(d, n)
                    if not n.startswith(".") and os.path.isdir(p) and not os.path.islink(p):
                        nxt.append(p)
                        out.append(p)
                        if len(out) >= limit:
                            return out
            level = nxt
    return out


# --------------------------------------------------------------------------- plans


def _in_roots(value: object, roots: list[str], field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\0" in value:
        raise Refused("bad_request", field)
    p = os.path.expanduser(value)
    if not os.path.isabs(p):
        raise Refused("bad_request", field, "must be an absolute path")
    real = os.path.realpath(p)
    if not os.path.isdir(real):
        raise Refused("not_allowed", field, "no such folder")
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise Refused("not_allowed", field, "outside the folders this host allows")
    return real


def _text(value: object, field: str, cap: int, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise Refused("bad_request", field)
        return ""
    if not isinstance(value, str) or len(value.encode()) > cap or "\0" in value:
        raise Refused("bad_request", field)
    return value


def clean_name(value: object) -> str:
    """A display-safe name: no control or format characters, at most NAME_MAX."""
    s = _text(value, "name", 4 * NAME_MAX, required=True)
    s = "".join(c for c in s if unicodedata.category(c)[0] != "C").strip()
    if not s or len(s) > NAME_MAX:
        raise Refused("bad_request", "name")
    return s


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "agent"


def _options(kind: str, raw: object, allowed: list[str], roots: list[str]) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise Refused("bad_request", "options")
    out = {}
    for k, v in raw.items():
        f = f"options.{k}"
        if k not in KINDS[kind]:
            raise Refused("bad_request", f, "unknown option")
        if k not in allowed:
            raise Refused("not_allowed", f)
        t = KINDS[kind][k]
        if t[0] == "enum" and v in t[1]:
            out[k] = v
        elif t[0] == "bool" and isinstance(v, bool):
            out[k] = v
        elif t[0] == "word" and isinstance(v, str) and _WORD.fullmatch(v):
            out[k] = v
        elif t[0] == "uuid" and isinstance(v, str) and _UUID.fullmatch(v):
            out[k] = v
        elif t[0] == "dirs" and isinstance(v, list) and len(v) <= MAX_DIRS:
            out[k] = [_in_roots(d, roots, f) for d in v]
        else:
            raise Refused("bad_request", f)
    return out


def binary(kind: str, cfg: dict) -> str:
    """The agent's command: start.json's `bin`, or the first on a fixed search path. Only one this user or root
    can change, as for tmux in the gate."""
    from .keys import trusted_file

    home = os.path.expanduser("~")
    cands = [cfg["kinds"][kind].get("bin") or ""] + [
        os.path.join(d, kind) for d in (f"{home}/.local/bin", f"{home}/.claude/local", "/opt/homebrew/bin",
                                        "/usr/local/bin", "/usr/bin")]
    for c in cands:
        if c and trusted_file(c):
            return c
    raise Refused("not_installed", "kind", f"{kind} is not installed here")


def toml_string(s: str) -> str:
    """A TOML basic string: Codex reads `-c key=value` values as TOML."""
    out = []
    for c in s:
        if c in '"\\':
            out.append("\\" + c)
        elif ord(c) < 0x20 or ord(c) == 0x7F or unicodedata.category(c) in ("Cs", "Cc"):
            out.append(f"\\u{ord(c):04X}")
        else:
            out.append(c)
    return '"' + "".join(out) + '"'


def argv_for(kind: str, exe: str, cwd: str, name: str, opts: dict, instructions: str, prompt: str,
             session: str) -> list[str]:
    """The command, values as --flag=value so none can be read as a flag; the prompt alone after `--`.
    With instructions=INSTRUCTIONS, the plan's copy: the text itself is in the plan once, beside it."""
    if kind == "claude":
        a = [exe, f"--name={name}"]
        a += [f"--resume={opts['resume']}"] if opts.get("resume") else [f"--session-id={session}"]
        if instructions:
            a.append(f"--append-system-prompt={instructions}")
        for k, flag in (("permission_mode", "--permission-mode"), ("model", "--model"), ("effort", "--effort")):
            if k in opts:
                a.append(f"{flag}={opts[k]}")
        a += [f"--add-dir={d}" for d in opts.get("add_dirs", [])]
        if opts.get("skip_permissions"):
            a.append("--dangerously-skip-permissions")
    else:
        # `codex resume <thread id>` takes the same flags; the id goes after `--`, before the prompt
        a = [exe, *(["resume"] if opts.get("resume") else []), f"--cd={cwd}"]
        if instructions:
            a.append("--config=developer_instructions="
                     + (instructions if instructions == INSTRUCTIONS else toml_string(instructions)))
        if "effort" in opts:
            a.append("--config=model_reasoning_effort=" + toml_string(opts["effort"]))
        for k, flag in (("model", "--model"), ("approval", "--ask-for-approval"), ("sandbox", "--sandbox")):
            if k in opts:
                a.append(f"{flag}={opts[k]}")
        a += [f"--add-dir={d}" for d in opts.get("add_dirs", [])]
        if opts.get("skip_permissions"):
            a.append("--dangerously-bypass-approvals-and-sandbox")
        if opts.get("resume"):
            return a + ["--", opts["resume"], *([prompt] if prompt else [])]
    if prompt:
        a += ["--", prompt]
    return a


def squad_member(tag: object, host: str) -> tuple[dict, dict]:
    """(squad header, member) for a start's `squad: {id, member}`, from this machine's own copy of its owner's
    squads (docs/squads.md), read-only. The member must be one that runs on this machine."""
    if not isinstance(tag, dict) or not bus.clean_squad_tag(tag):
        raise Refused("bad_request", "squad", "{id, member}")
    path = bus.STATE_DIR / "bus.db"
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        raise Refused("bad_request", "squad", "no squads on this host") from None
    try:
        con.row_factory = sqlite3.Row
        if con.execute("SELECT COUNT(DISTINCT owner) FROM squads").fetchone()[0] > 1:  # the login changed here
            raise Refused("bad_request", "squad", "squads of more than one owner on this host")
        heads = con.execute("SELECT * FROM squads WHERE id=? AND deleted=0", (tag["id"],)).fetchall()
        if len(heads) != 1:
            raise Refused("bad_request", "squad", "no such squad on this host")
        h = heads[0]
        m = con.execute("SELECT * FROM squad_members WHERE owner=? AND squad=? AND id=? AND deleted=0",
                        (h["owner"], h["id"], tag["member"])).fetchone()
        if m is None:
            raise Refused("bad_request", "squad.member", "no such member in the squad")
        live = con.execute("SELECT 1 FROM agents WHERE squad=? AND last_seen>?",
                           (json.dumps({"id": tag["id"], "member": tag["member"]}, sort_keys=True),
                            time.time() - bus.LIVE)).fetchone()
    except sqlite3.Error:
        raise Refused("bad_request", "squad", "no squads on this host") from None
    finally:
        con.close()
    head, member = {"id": h["id"], **json.loads(h["doc"])}, {"id": m["id"], **json.loads(m["doc"])}
    if member["host"] != host:
        raise Refused("bad_request", "squad.member", f"this member runs on {member['host']}, not {host}")
    if live:
        raise Refused("running", "squad.member", "this member's agent is already running")
    return head, member


def _from_squad(item: dict, host: str) -> tuple[dict, dict]:
    """The item a squad member stands for: kind, folder, name, instructions, options and prompt from the squad;
    a prompt in the request takes the squad's place, and device_name is the request's."""
    head, m = squad_member(item.get("squad"), host)
    for k in ("kind", "cwd", "name", "instructions", "options"):
        if item.get(k) not in (None, "", {}):
            raise Refused("bad_request", k, "a squad member's comes from the squad")
    st = m.get("start") or {}
    out = {"kind": m["kind"], "cwd": m["folder"], "name": m["name"], "instructions": m.get("instructions") or "",
           "options": st.get("options") or {}, "prompt": item.get("prompt") or st.get("prompt") or "",
           "device_name": item.get("device_name"), "_session": item.get("_session")}
    return out, {"id": head["id"], "name": head["name"], "member": m["id"]}


def _reopen(sid: str, cwd: str, name: str, host: str, kind: str = "claude") -> dict | None:
    """A start that resumes session `sid`: only one this host lists as dead, in its own folder under its own name.
    Returns the squad it was a member of (it reopens as that member), or None."""
    from . import dead

    r, live = dead.find(sid)
    if live:
        raise Refused("running", "options.resume", "this session is running")
    if r is None or r["kind"] != kind:
        raise Refused("not_dead", "options.resume", "not a session this host lists as dead")
    if os.path.realpath(r["cwd"]) != cwd:
        raise Refused("cwd", "cwd", "a session reopens in its own folder")
    if r["name"] != name:
        raise Refused("bad_request", "name", "a session reopens under its own name")
    if not r["squad"]:
        return None
    head, m = squad_member(r["squad"], host)
    return {"id": head["id"], "name": head["name"], "member": m["id"]}


def build(item: object, cfg: dict, grant: str, host: str) -> dict:
    """The plan for one start, or Refused. Deterministic for the same item, config, grant and squad."""
    if not isinstance(item, dict):
        raise Refused("bad_request")
    squad = None
    if item.get("squad") is not None:
        item, squad = _from_squad(item, host)
    kind = item.get("kind")
    if kind not in KINDS:
        raise Refused("bad_request", "kind")
    if kind not in cfg["kinds"]:
        raise Refused("not_allowed", "kind")
    cwd = _in_roots(item.get("cwd"), cfg["roots"], "cwd")
    name = clean_name(item.get("name"))
    opts = _options(kind, item.get("options"), cfg["kinds"][kind]["options"], cfg["roots"])
    instructions = _text(item.get("instructions"), "instructions", INSTRUCTIONS_MAX)
    prompt = _text(item.get("prompt"), "prompt", PROMPT_MAX)
    device = "".join(c for c in _text(item.get("device_name"), "device_name", 256)
                     if unicodedata.category(c)[0] != "C")[:64]
    skip = skips(kind, opts)
    if skip and not cfg["skip_permissions"]:
        raise Refused("not_allowed", "options.skip_permissions", "this host does not allow skipping permissions")
    if skip and grant != "skip":
        raise Refused("no_skip_grant", "options.skip_permissions", "this key may not skip permissions")
    auto = autos(kind, opts)
    if auto and not cfg["auto"]:
        raise Refused("not_allowed", "options.permission_mode", "this host does not allow auto mode")
    if opts.get("resume") and not squad:  # a squad member's resume comes from the squad, signed by its owner
        squad = _reopen(opts["resume"], cwd, name, host, kind)
    # A member keeps its report current for the squad page; after the reopen, so a resumed member gets it too. The
    # owner's instructions stay within their cap.
    if squad:
        from .reports_cli import HOW

        instructions = f"{instructions}\n\n{HOW}" if instructions else HOW
    session = str(item.get("_session") or "") if kind == "claude" and not opts.get("resume") else ""
    session = opts.get("resume") or session  # a new Codex thread's id is not known until it starts
    exe = binary(kind, cfg)
    env = {"TAILSHARE_AGENT_NAME": name, "TAILSHARE_STARTED_BY": device or "a device",
           **({"TAILSHARE_SQUAD": squad["id"], "TAILSHARE_MEMBER": squad["member"]} if squad else {}),
           "PATH": os.pathsep.join(dict.fromkeys([os.path.dirname(exe), os.path.expanduser("~/.local/bin"),
                                                  "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))}
    return {"host": host, "kind": kind, "cwd": cwd, "name": name, "options": opts, "skip": skip, "auto": auto,
            "argv": argv_for(kind, exe, cwd, name, opts, instructions and INSTRUCTIONS, prompt and PROMPT, session), "env": env,
            "prompt": prompt,
            "tmux": {"session": squad_session(squad["name"]) if squad else SESSION, "window": slug(name)},
            "session": session, **({"squad": squad} if squad else {}),
            "instructions": instructions,
            "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest() if instructions else ""}


# --------------------------------------------------------------------------- tmux


def _tmux_bin() -> str:
    from .keys import tmux_binary

    t = tmux_binary("")
    if not t:
        raise Refused("no_tmux", reason="no tmux binary that only this user or root can change")
    return t


def tmux_dir() -> Path:
    """The folder of the agents' own servers: this user's, and nobody else may write to it."""
    d = Path(TMUX_DIR or f"/tmp/tmux-{os.getuid()}")
    try:
        d.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(d)
    except OSError as e:
        raise Refused("no_tmux", reason=f"cannot use {d}: {e.strerror}") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise Refused("no_tmux", reason=f"{d} must be a folder only this user can use (0700)")
    return d


def socket_for(name: str) -> str:
    """The server a started agent called `name` runs on."""
    return TMUX_SOCKET or str(tmux_dir() / (LABEL + slug(name)))


def _servers() -> list[str]:
    """Every server started agents may be on: the pinned one, or the default (older starts) and each ts-* one."""
    if TMUX_SOCKET:
        return [TMUX_SOCKET]
    own = []
    for p in sorted(tmux_dir().iterdir()):
        try:
            st = os.lstat(p)
        except OSError:  # gone in between
            continue
        if p.name.startswith(LABEL) and stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid():
            own.append(str(p))
    return [LEGACY_SOCKET, *own]


def _windows(t: str) -> list[tuple[str, str, str]]:
    """(socket, session, window name) of every window tailshare started, on any server it starts them on."""
    out = []
    for sock in _servers():
        try:
            r = subprocess.run([t, *(["-S", sock] if sock else []), "list-windows", "-a", "-F",
                                "#{session_name}\t#{window_name}"],
                               capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            continue
        rows = [line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line] if r.returncode == 0 else []
        out += [(sock, s, w) for s, w in rows if s == SESSION or s.startswith("squad-")]
    return out


def _sessions(t: str, sock: str) -> list[str]:
    try:
        r = subprocess.run([t, "-S", sock, "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True,
                           timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    return r.stdout.split() if r.returncode == 0 else []


def server_env() -> dict:
    """What a new agent server starts with, and so every agent on it: who this user is, and nothing an ssh client
    or the gate's own process sent (SSH_CONNECTION would make the bus take the agent for the device's user)."""
    import pwd

    pw = pwd.getpwuid(os.getuid())
    return {"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name, "SHELL": pw.pw_shell or "/bin/sh",
            "PATH": SAFE_PATH, "LANG": "en_US.UTF-8", "TERM": "xterm-256color"}


def launch(plan: dict, max_running: int) -> dict:
    t = _tmux_bin()
    sock = socket_for(plan["name"])
    wins = _windows(t)
    w = plan["tmux"]
    if any((s, n) == (w["session"], w["window"]) for _, s, n in wins) or (not TMUX_SOCKET and _sessions(t, sock)):
        raise Refused("running", "name", "a session with this name is still running here")
    if len(wins) >= max_running:
        raise Refused("limit", reason=f"{len(wins)} started sessions are running here (at most {max_running})",
                      max_running=max_running)
    env = [x for k, v in sorted(plan["env"].items()) for x in ("-e", f"{k}={v}")]
    fmt = ["-P", "-F", "#{pane_id}\t#{socket_path}"]
    argv = argv_for(plan["kind"], plan["argv"][0], plan["cwd"], plan["name"], plan["options"], plan["instructions"],
                    plan["prompt"], plan["session"])
    common = ["-n", w["window"], "-c", plan["cwd"], *env, *fmt, "--", *argv]
    tmux = [t, "-S", sock]
    has = bool(TMUX_SOCKET) and subprocess.run([*tmux, "has-session", "-t", "=" + w["session"]], capture_output=True,
                                               timeout=10, stdin=subprocess.DEVNULL).returncode == 0
    cmd = ["new-window", "-d", "-t", f"={w['session']}:"] if has else ["new-session", "-d", "-s", w["session"]]
    r = subprocess.run([*tmux, *cmd, *common], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                       env=server_env())
    if r.returncode != 0 and not has and TMUX_SOCKET:  # another start made the session in between
        cmd = ["new-window", "-d", "-t", f"={w['session']}:"]
        r = subprocess.run([*tmux, *cmd, *common], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                       env=server_env())
    pane, _, got = r.stdout.strip().partition("\t")
    if r.returncode != 0 and not TMUX_SOCKET and "duplicate session" in r.stderr:  # the same name, started just now
        raise Refused("running", "name", "a session with this name is still running here")
    if r.returncode != 0 or not re.fullmatch(r"%[0-9]+", pane):
        raise Refused("failed", reason=(r.stderr.strip() or "tmux did not start the window")[:200])
    return {"session": plan["session"], "tmux": {"socket": got, "pane": pane, "bin": t}}


# --------------------------------------------------------------------------- plan store


SCHEMA = """
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, key TEXT NOT NULL, item TEXT NOT NULL, plan TEXT NOT NULL,
  created REAL NOT NULL, expires REAL NOT NULL, used REAL, result TEXT, approved REAL, launched REAL);
CREATE TABLE IF NOT EXISTS confirms(key TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS member_starts(squad TEXT NOT NULL, member TEXT NOT NULL, instructions_sha256 TEXT NOT NULL,
  at REAL NOT NULL, PRIMARY KEY(squad, member));
"""


def _db(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    old = os.umask(0o077)  # the database and its -wal/-journal files are owner-only from the start
    try:
        con = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        have = {r["name"] for r in con.execute("PRAGMA table_info(plans)")}
        for col in ("approved", "launched"):  # a start.db from before the bus launched plans
            if col not in have:
                con.execute(f"ALTER TABLE plans ADD COLUMN {col} REAL")
    finally:
        os.umask(old)
    now = time.time()
    con.execute("DELETE FROM plans WHERE (used IS NULL AND expires<?) OR (used IS NOT NULL AND used<?)",
                (now, now - KEEP))
    con.execute("DELETE FROM confirms WHERE at<?", (now - 60,))
    return con


def squad_session(name: str) -> str:
    return "squad-" + slug(name).replace(".", "-")  # tmux reads "." in a target as a pane


def _canon(d: dict) -> str:
    return json.dumps({k: v for k, v in d.items() if k != "changed"}, sort_keys=True, separators=(",", ":"))


def _changed(con: sqlite3.Connection, plan: dict) -> str:
    """For a squad member: whether its instructions differ from the last time it was started here."""
    sq = plan.get("squad")
    if not sq:
        return ""
    r = con.execute("SELECT instructions_sha256, at FROM member_starts WHERE squad=? AND member=?",
                    (sq["id"], sq["member"])).fetchone()
    if r is None or r["instructions_sha256"] == plan["instructions_sha256"]:
        return ""
    return f"instructions changed since this member last started ({time.strftime('%Y-%m-%d %H:%M', time.localtime(r['at']))})"


def running() -> int | None:
    """How many started sessions run here now, or None if tmux cannot say."""
    try:
        return len(_windows(_tmux_bin()))
    except Refused:
        return None


def dry_run(con: sqlite3.Connection, starts: object, cfg: dict, grant: str, key: str, host: str,
            fit: int | None = None) -> list[dict]:
    if not isinstance(starts, list) or not 0 < len(starts) <= MAX_ITEMS:
        raise Refused("bad_request", "starts", f"1 to {MAX_ITEMS} starts")
    con.execute("BEGIN IMMEDIATE")
    try:
        n = con.execute("SELECT COUNT(*) FROM plans WHERE key=? AND used IS NULL", (key,)).fetchone()[0]
        return _dry_run(con, starts, cfg, grant, key, host, MAX_OPEN_PLANS - n, fit)
    finally:
        con.execute("COMMIT")


def _dry_run(con: sqlite3.Connection, starts: list, cfg: dict, grant: str, key: str, host: str, room: int,
             fit: int | None = None) -> list[dict]:
    """A plan or a refusal per item. With `fit`, how many more may run here: the items past it get "limit"."""
    if len(starts) > room:
        oldest = con.execute("SELECT MIN(expires) FROM plans WHERE key=? AND used IS NULL", (key,)).fetchone()[0]
        raise Refused("rate", retry_after=max(1, int((oldest or time.time()) + 1 - time.time())))
    out = []
    for item in starts:
        try:
            if isinstance(item, dict):
                # every item gets one: a squad member's kind is only known once build() reads the squad,
                # and build() keeps it only for a Claude start that does not resume
                item = {**{k: v for k, v in item.items() if not k.startswith("_")}, "_session": str(uuid.uuid4())}
            plan = build(item, cfg, grant, host)
        except Refused as e:
            out.append(e.out)
            continue
        if fit is not None:
            if fit <= 0:
                out.append(Refused("limit", reason=f"at most {cfg['max_running']} started sessions run here",
                                   max_running=cfg["max_running"]).out)
                continue
            fit -= 1
        pid, now = secrets.token_hex(16), time.time()
        con.execute("INSERT INTO plans(id, key, item, plan, created, expires) VALUES(?,?,?,?,?,?)",
                    (pid, key, json.dumps(item), _canon(plan), now, now + PLAN_TTL))
        changed = _changed(con, plan)
        if changed:
            plan = {**plan, "changed": changed}
        out.append({"plan_id": pid, "expires": round(now + PLAN_TTL), "plan": plan})
    return out


def confirm(con: sqlite3.Connection, ids: object, cfg: dict, grant: str, key: str, host: str) -> list[dict]:
    if not isinstance(ids, list) or not 0 < len(ids) <= MAX_ITEMS or not all(isinstance(i, str) for i in ids):
        raise Refused("bad_request", "confirm", f"1 to {MAX_ITEMS} plan ids")
    now = time.time()
    con.execute("BEGIN IMMEDIATE")
    try:
        recent = con.execute("SELECT at FROM confirms WHERE key=? AND at>? ORDER BY at", (key, now - 60)).fetchall()
        if len(recent) >= CONFIRMS_PER_MIN:
            raise Refused("rate", retry_after=max(1, int(recent[0]["at"] + 61 - now)))
        con.execute("INSERT INTO confirms(key, at) VALUES(?,?)", (key, now))
    finally:
        con.execute("COMMIT")
    out = []
    for pid in ids:
        try:
            out.append({"plan_id": pid, **_confirm_one(con, pid, cfg, grant, key, host)})
        except Refused as e:
            out.append({"plan_id": pid, **e.out})
    return out


def _confirm_one(con: sqlite3.Connection, pid: str, cfg: dict, grant: str, key: str, host: str) -> dict:
    row = con.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone() if _PLAN_ID.fullmatch(pid) else None
    if row is None or row["key"] != key:
        raise Refused("unknown_plan")
    if row["used"] is not None:
        return {**json.loads(row["result"] or '{"error":"failed"}'), "already": True}
    if row["expires"] < time.time():
        raise Refused("expired")
    if not con.execute("UPDATE plans SET used=? WHERE id=? AND used IS NULL", (time.time(), pid)).rowcount:
        raise Refused("unknown_plan")  # a concurrent confirm took it; its result is there on the next try
    try:
        try:
            plan = build(json.loads(row["item"]), cfg, grant, host)
        except Refused as e:
            raise Refused("changed", e.out.get("field", ""), "the host's config or this key's grant changed: "
                          + str(e.out.get("reason") or e.out["error"])) from None
        if _canon(plan) != row["plan"]:
            raise Refused("changed", reason="the plan is no longer what was shown; dry-run again")
        con.execute("UPDATE plans SET approved=? WHERE id=?", (time.time(), pid))
        result = via_bus(pid)
    except Refused as e:
        con.execute("UPDATE plans SET result=? WHERE id=?", (json.dumps(e.out), pid))
        raise
    con.execute("UPDATE plans SET result=? WHERE id=?", (json.dumps(result), pid))
    if plan.get("squad"):
        con.execute("INSERT OR REPLACE INTO member_starts(squad, member, instructions_sha256, at) VALUES(?,?,?,?)",
                    (plan["squad"]["id"], plan["squad"]["member"], plan["instructions_sha256"], time.time()))
    return result


def close(con: sqlite3.Connection, ids: object, key: str) -> list[dict]:
    """`{"v":1, "close":["<session id>", …]}`: end sessions `tailshare start` started here. Counts against the
    same per-key rate as confirms. The bus does the closing (close_started), as it does the launching."""
    if not isinstance(ids, list) or not 0 < len(ids) <= MAX_ITEMS or not all(isinstance(i, str) for i in ids):
        raise Refused("bad_request", "close", f"1 to {MAX_ITEMS} session ids")
    now = time.time()
    con.execute("BEGIN IMMEDIATE")
    try:
        recent = con.execute("SELECT at FROM confirms WHERE key=? AND at>? ORDER BY at", (key, now - 60)).fetchall()
        if len(recent) >= CONFIRMS_PER_MIN:
            raise Refused("rate", retry_after=max(1, int(recent[0]["at"] + 61 - now)))
        con.execute("INSERT INTO confirms(key, at) VALUES(?,?)", (key, now))
    finally:
        con.execute("COMMIT")
    out = []
    for sid in ids:
        try:
            if not _UUID.fullmatch(sid.lower()):
                raise Refused("bad_request", "close", "a session id")
            out.append({"session_id": sid.lower(), **_ask_bus({"v": 1, "close": sid.lower()})})
        except Refused as e:
            out.append({"session_id": sid.lower(), **e.out})
    return out


def handle(doc: object, grant: str, key: str, host: str, cfg_path: Path | None = None,
           db: Path | None = None, plan_only: bool = False) -> dict:
    """One `tailshare-start` call: {"v":1, "results":[…]} or {"v":1, "error":…}. `plan_only`: the call came in on
    the pair's plan key, which needs no Face ID, so it may dry-run but never confirm."""
    try:
        if grant not in ("safe", "skip") or not _KEY_ID.fullmatch(key):
            raise Refused("no_grant", reason="this key may not start sessions")
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise Refused("root", reason="tailshare never starts sessions as root")
        cfg = load_config(cfg_path)
        if not cfg or not cfg["roots"] or not cfg["kinds"]:
            raise Refused("off", reason="starting sessions is off on this host")
        if not isinstance(doc, dict) or doc.get("v") != 1:
            raise Refused("bad_request", "v")
        con = _db(db)
        try:
            if doc.get("dry_run") is True and "confirm" not in doc and "close" not in doc:
                n = running()
                fit = None if n is None else max(0, cfg["max_running"] - n)
                return {"v": 1, "results": dry_run(con, doc.get("starts"), cfg, grant, key, host, fit),
                        **({"running": n} if n is not None else {}), "max_running": cfg["max_running"]}
            if "close" in doc and not {"starts", "confirm", "dry_run"} & set(doc):
                if plan_only:
                    raise Refused("no_grant", reason="the plan key only makes plans: close with the start key")
                return {"v": 1, "results": close(con, doc.get("close"), key)}
            if "confirm" in doc and "starts" not in doc and "close" not in doc:
                if plan_only:
                    raise Refused("no_grant", reason="the plan key only makes plans: confirm with the start key")
                return {"v": 1, "results": confirm(con, doc.get("confirm"), cfg, grant, key, host)}
            raise Refused("bad_request", reason="send either dry_run with starts, confirm, or close")
        finally:
            con.close()
    except Refused as e:
        return {"v": 1, **e.out}


# --------------------------------------------------------------------------- launching, in the bus
#
# The gate runs under sshd, in an audit session of its own: an agent server started there could not reach the
# login keychain (Claude's sign-in). So the gate only approves a plan, and asks the bus, which runs in the user's
# GUI session, to launch it: one line, {"v":1,"launch":"<plan id>"}, over a unix socket in a 0700 folder of the
# state dir, from this user only. The bus takes nothing else from the request. It loads the plan from start.db
# itself and launches it only if the gate approved it moments ago and it was never launched, so whatever
# reaches the socket can launch no more than a plan a device signed and confirmed, once.

APPROVED_FOR = 30  # seconds the bus takes to launch a plan the gate approved
LAUNCH_WAIT = 30


def launch_socket() -> Path:
    return bus.STATE_DIR / "launch" / "sock"


def _peer_uid(conn: socket.socket) -> int | None:
    try:
        if sys.platform == "darwin":  # SOL_LOCAL, LOCAL_PEERCRED: struct xucred {u_int version; uid_t uid; ...}
            raw = conn.getsockopt(0, 0x001, 76)
            return int.from_bytes(raw[4:8], sys.byteorder)
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)  # struct ucred {pid; uid; gid}
        return int.from_bytes(raw[4:8], sys.byteorder)
    except OSError:
        return None


def launch_approved(pid: object) -> dict:
    """In the bus: launch the plan `pid` if the gate approved it just now and nobody launched it yet."""
    try:
        if not isinstance(pid, str) or not _PLAN_ID.fullmatch(pid):
            raise Refused("unknown_plan")
        con = _db()
        try:
            now = time.time()
            con.execute("BEGIN IMMEDIATE")
            try:
                row = con.execute("SELECT * FROM plans WHERE id=?", (pid,)).fetchone()
                if (row is None or row["used"] is None or row["approved"] is None or row["launched"] is not None
                        or row["result"] is not None or row["approved"] < now - APPROVED_FOR):
                    raise Refused("unknown_plan")  # forged, replayed, or never confirmed
                con.execute("UPDATE plans SET launched=? WHERE id=?", (now, pid))
            finally:
                con.execute("COMMIT")
        finally:
            con.close()
        cfg = load_config()
        if not cfg or not cfg["roots"] or not cfg["kinds"]:
            raise Refused("off", reason="starting sessions is off on this host")
        return launch(json.loads(row["plan"]), cfg["max_running"])
    except Refused as e:
        return e.out


CLOSE_WAIT = 5  # seconds a closed pane has to go away


def close_started(sid: object) -> dict:
    """In the bus: end the live session `sid` if `tailshare start` started it here, on this user's own per-agent
    server, and mark it ended, so it is listed as dead (dead.rows) and may be reopened at once. Never a pane
    tailshare did not start: not the user's own sessions, not ts-agents' (which brings its agents back)."""
    from . import dead, keys

    try:
        if not isinstance(sid, str) or not _UUID.fullmatch(sid):
            raise Refused("bad_request", "close", "a session id")
        cfg = load_config()
        if not cfg or not cfg["roots"] or not cfg["kinds"]:
            raise Refused("off", reason="starting sessions is off on this host")
        db = bus.STATE_DIR / "bus.db"
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                con.row_factory = sqlite3.Row
                rows = con.execute("SELECT id, harness, tmux, state, last_seen FROM agents WHERE lower(id)=?",
                                   (sid,)).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            raise Refused("no_bus", reason="cannot read the bus's agents") from None
        row = next((r for r in rows if dead.session_id(r["harness"] or "", r["id"]) == sid), None)
        if row is None or not dead.is_live(row):
            raise Refused("not_running", "close", "no session with this id runs here")
        t = bus.clean_tmux(dead._json(row["tmux"]))
        session = (t.get("target") or "").rsplit(":", 1)[0] if t else ""
        if not t or not Path(t["socket"]).name.startswith(LABEL) or not session.startswith(dead.STARTED):
            raise Refused("not_started", "close", "tailshare closes only sessions it started")
        binary = keys.tmux_binary(t["bin"])
        if not binary:
            raise Refused("no_tmux", reason="no tmux binary that only this user or root can change")
        if not (keys.own_socket(t["socket"]) and keys.pane_alive(binary, t["socket"], t["pane"])):
            raise Refused("not_running", "close", "its pane is gone already")
        r = subprocess.run([binary, "-S", t["socket"], "kill-pane", "-t", t["pane"]], capture_output=True, text=True,
                           timeout=10, stdin=subprocess.DEVNULL)
        end = time.time() + CLOSE_WAIT
        while keys.pane_alive(binary, t["socket"], t["pane"]):
            if time.time() > end:
                raise Refused("failed", reason=(r.stderr.strip() or "the pane is still there")[:200])
            time.sleep(0.1)
        now = time.time()
        rw = sqlite3.connect(str(db), timeout=10)
        try:
            rw.execute("UPDATE agents SET state=? WHERE id=?",
                       (json.dumps({"v": "ended", "since": now, "at": now}), row["id"]))
            rw.commit()
        finally:
            rw.close()
        return {"closed": True, "tmux": {"socket": t["socket"], "pane": t["pane"]}}
    except Refused as e:
        return e.out


def _serve_one(conn: socket.socket) -> None:
    with conn:
        conn.settimeout(10)
        try:
            if _peer_uid(conn) != os.getuid():
                return
            raw = b""
            while b"\n" not in raw and len(raw) <= 256:
                chunk = conn.recv(256)
                if not chunk:
                    break
                raw += chunk
            try:
                req = json.loads(raw.split(b"\n", 1)[0])
            except ValueError:
                req = None
            if isinstance(req, dict) and req.get("v") == 1 and set(req) == {"v", "launch"}:
                out = launch_approved(req["launch"])
            elif isinstance(req, dict) and req.get("v") == 1 and set(req) == {"v", "close"}:
                out = close_started(req["close"])
            else:
                out = {"error": "bad_request"}
            conn.sendall(json.dumps(out).encode() + b"\n")
        except OSError:
            pass


def serve_launches() -> socket.socket:
    """In the bus: listen for launches. Returns the listening socket (close it to stop)."""
    path = launch_socket()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path.parent)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise OSError(f"{path.parent} must be a folder only this user can use (0700)")
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        srv.bind(str(path))
    finally:
        os.umask(old)
    srv.listen(16)

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return  # closed
            threading.Thread(target=_serve_one, args=(conn,), daemon=True, name="start-launch").start()

    threading.Thread(target=loop, daemon=True, name="start-launches").start()
    return srv


def via_bus(pid: str) -> dict:
    """In the gate: have the bus launch the approved plan `pid`. Never launches it here."""
    return _ask_bus({"v": 1, "launch": pid})


def _ask_bus(req: dict) -> dict:
    path = launch_socket()
    try:
        st = os.lstat(path.parent)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise Refused("no_bus", reason="the bus's launch folder is not this user's alone")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(LAUNCH_WAIT)
            c.connect(str(path))
            c.sendall(json.dumps(req).encode() + b"\n")
            raw = b""
            while b"\n" not in raw and len(raw) < 65536:
                chunk = c.recv(65536)
                if not chunk:
                    break
                raw += chunk
        out = json.loads(raw.split(b"\n", 1)[0])
    except (OSError, ValueError):
        raise Refused("no_bus", reason="the bus is not running here; start it with `tailshare serve`") from None
    if not isinstance(out, dict):
        raise Refused("no_bus", reason="the bus gave no answer")
    if "error" in out:
        e = Refused("x")
        e.out = out
        raise e
    return out


SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


def settle(state: str = "", bus_port: int = 0, tmux_socket: str = "") -> None:
    """Before the start gate does anything: nothing an ssh client may have put in the environment (AcceptEnv,
    PermitUserEnvironment) decides where state lives, which bus or tmux server is used, or where binaries come
    from. HOME comes from the password database, the rest from the key's command= line or the defaults."""
    import pwd

    home = pwd.getpwuid(os.getuid()).pw_dir
    for k in [k for k in os.environ if k.startswith(("TAILSHARE_", "TMUX", "PYTHON"))]:
        del os.environ[k]
    os.environ.update(HOME=home, PATH=SAFE_PATH)
    global TMUX_SOCKET
    bus.STATE_DIR = Path(state) if state else Path(home) / ".tailshare"
    bus.BUS_PORT = bus_port or 8789
    TMUX_SOCKET = tmux_socket


def gate(grant: str, key: str, plan_only: bool = False) -> int:
    """`ssh-gate --start=…`: read the request from stdin, print one JSON line, exit 0 only if everything worked."""
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        out = {"v": 1, "error": "bad_request", "reason": "request too large"}
    else:
        try:
            doc = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            doc = None
        try:
            host = host_name()
        except Refused as e:
            out = {"v": 1, **e.out}
        else:
            out = handle(doc, grant, key, host, plan_only=plan_only)
    line = json.dumps(out, separators=(",", ":"))
    if len(line) > MAX_OUTPUT:
        line = json.dumps({"v": 1, "error": "too_large", "reason": "the reply is too large; send fewer starts"})
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0 if "error" not in out and not any("error" in r for r in out.get("results", [])) else 1


def host_name() -> str:
    """This machine's tailnet name, as the bus knows it (its local /machines: /hello is served on the tailnet only).
    No guess when the bus does not say: devices check a plan's host against the machine they reached, so a wrong
    name would only be refused there, after the person had looked at it."""
    try:
        rows = bus.request(f"http://127.0.0.1:{bus.BUS_PORT}/bus/v1/machines", None, timeout=3)["machines"]
        host = next(str(m["host"]) for m in rows if m.get("self") is True)
    except Exception:  # noqa: BLE001
        raise Refused("no_bus", reason="the bus is not running here, so this machine's name is unknown; start it "
                      "with `tailshare serve`") from None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", host):
        raise Refused("no_bus", reason="the bus gave no usable machine name")
    return host
