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
import sqlite3
import stat
import subprocess
import sys
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
PROMPT_MAX = 8 * 1024
INSTRUCTIONS_MAX = 4 * 1024
NAME_MAX = 64
MAX_DIRS = 8
# "": the user's default tmux server, the one /hello's panes are on. Tests use a private one, set in-process or by
# `ssh-gate --tmux-socket` on the key's command= line: never from the environment, which an ssh client may send
TMUX_SOCKET = ""
SESSION = "tailshare-start"
INSTRUCTIONS = "<instructions>"  # stands for the instructions in a plan's argv

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
    if kind == "claude":  # auto counts as skip until Nicholas decides otherwise
        return opts.get("permission_mode") in ("bypassPermissions", "auto")
    return opts.get("sandbox") == "danger-full-access"


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
            "max_running": mr if isinstance(mr, int) and not isinstance(mr, bool) and 0 < mr <= 100 else 12}


def capabilities(path: Path | None = None) -> dict | None:
    """The /hello `start` block for the owner's devices: what this host takes, never its folders."""
    try:
        cfg = load_config(path)
    except Refused:
        return None
    if not cfg or not cfg["roots"] or not cfg["kinds"]:
        return None
    return {"kinds": sorted(cfg["kinds"]), "options": {k: v["options"] for k, v in sorted(cfg["kinds"].items())},
            "skip": cfg["skip_permissions"]}


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
        a = [exe, f"--cd={cwd}"]
        if instructions:
            a.append("--config=developer_instructions="
                     + (instructions if instructions == INSTRUCTIONS else toml_string(instructions)))
        for k, flag in (("model", "--model"), ("approval", "--ask-for-approval"), ("sandbox", "--sandbox")):
            if k in opts:
                a.append(f"{flag}={opts[k]}")
        if opts.get("skip_permissions"):
            a.append("--dangerously-bypass-approvals-and-sandbox")
    if prompt:
        a += ["--", prompt]
    return a


def build(item: object, cfg: dict, grant: str, host: str) -> dict:
    """The plan for one start, or Refused. Deterministic for the same item, config and grant."""
    if not isinstance(item, dict):
        raise Refused("bad_request")
    if item.get("squad") is not None:
        raise Refused("bad_request", "squad", "squads are not supported on this host yet")
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
    session = str(item.get("_session") or "") if kind == "claude" and not opts.get("resume") else ""
    if kind == "claude":
        session = opts.get("resume") or session
    exe = binary(kind, cfg)
    env = {"TAILSHARE_AGENT_NAME": name, "TAILSHARE_STARTED_BY": device or "a device",
           "PATH": os.pathsep.join(dict.fromkeys([os.path.dirname(exe), os.path.expanduser("~/.local/bin"),
                                                  "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]))}
    return {"host": host, "kind": kind, "cwd": cwd, "name": name, "options": opts, "skip": skip,
            "argv": argv_for(kind, exe, cwd, name, opts, instructions and INSTRUCTIONS, prompt, session), "env": env,
            "prompt": prompt,
            "tmux": {"session": SESSION, "window": slug(name)}, "session": session,
            "instructions": instructions,
            "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest() if instructions else ""}


# --------------------------------------------------------------------------- tmux


def _tmux() -> list[str]:
    from .keys import tmux_binary

    t = tmux_binary("")
    if not t:
        raise Refused("no_tmux", reason="no tmux binary that only this user or root can change")
    return [t, *(["-S", TMUX_SOCKET] if TMUX_SOCKET else [])]


def _windows(tmux: list[str]) -> list[tuple[str, str]]:
    """(session, window name) of every window tailshare started, on the server it starts them on."""
    r = subprocess.run([*tmux, "list-windows", "-a", "-F", "#{session_name}\t#{window_name}"],
                       capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    rows = [tuple(line.split("\t", 1)) for line in r.stdout.splitlines() if "\t" in line] if r.returncode == 0 else []
    return [(s, w) for s, w in rows if s == SESSION or s.startswith("squad-")]


def launch(plan: dict, max_running: int) -> dict:
    tmux = _tmux()
    wins = _windows(tmux)
    t = plan["tmux"]
    if (t["session"], t["window"]) in wins:
        raise Refused("running", "name", "a session with this name that tailshare started is still running")
    if len(wins) >= max_running:
        raise Refused("limit", reason=f"{len(wins)} started sessions are running here (at most {max_running})")
    env = [x for k, v in sorted(plan["env"].items()) for x in ("-e", f"{k}={v}")]
    fmt = ["-P", "-F", "#{pane_id}\t#{socket_path}"]
    argv = argv_for(plan["kind"], plan["argv"][0], plan["cwd"], plan["name"], plan["options"], plan["instructions"],
                    plan["prompt"], plan["session"])
    common = ["-n", t["window"], "-c", plan["cwd"], *env, *fmt, "--", *argv]
    has = subprocess.run([*tmux, "has-session", "-t", "=" + t["session"]], capture_output=True, timeout=10,
                         stdin=subprocess.DEVNULL).returncode == 0
    cmd = ["new-window", "-d", "-t", f"={t['session']}:"] if has else ["new-session", "-d", "-s", t["session"]]
    r = subprocess.run([*tmux, *cmd, *common], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    if r.returncode != 0 and not has:  # another start made the session in between
        cmd = ["new-window", "-d", "-t", f"={t['session']}:"]
        r = subprocess.run([*tmux, *cmd, *common], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
    pane, _, sock = r.stdout.strip().partition("\t")
    if r.returncode != 0 or not re.fullmatch(r"%[0-9]+", pane):
        raise Refused("failed", reason=(r.stderr.strip() or "tmux did not start the window")[:200])
    return {"session": plan["session"], "tmux": {"socket": sock, "pane": pane, "bin": tmux[0]}}


# --------------------------------------------------------------------------- plan store


SCHEMA = """
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, key TEXT NOT NULL, item TEXT NOT NULL, plan TEXT NOT NULL,
  created REAL NOT NULL, expires REAL NOT NULL, used REAL, result TEXT);
CREATE TABLE IF NOT EXISTS confirms(key TEXT NOT NULL, at REAL NOT NULL);
"""


def _db(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    os.chmod(path, 0o600)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    now = time.time()
    con.execute("DELETE FROM plans WHERE (used IS NULL AND expires<?) OR (used IS NOT NULL AND used<?)",
                (now, now - KEEP))
    con.execute("DELETE FROM confirms WHERE at<?", (now - 60,))
    return con


def _canon(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def dry_run(con: sqlite3.Connection, starts: object, cfg: dict, grant: str, key: str, host: str) -> list[dict]:
    if not isinstance(starts, list) or not 0 < len(starts) <= MAX_ITEMS:
        raise Refused("bad_request", "starts", f"1 to {MAX_ITEMS} starts")
    out = []
    for item in starts:
        try:
            if isinstance(item, dict):
                item = {k: v for k, v in item.items() if not k.startswith("_")}
                if item.get("kind") == "claude":
                    item["_session"] = str(uuid.uuid4())
            plan = build(item, cfg, grant, host)
        except Refused as e:
            out.append(e.out)
            continue
        pid, now = secrets.token_hex(16), time.time()
        con.execute("INSERT INTO plans(id, key, item, plan, created, expires) VALUES(?,?,?,?,?,?)",
                    (pid, key, json.dumps(item), _canon(plan), now, now + PLAN_TTL))
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
        result = launch(plan, cfg["max_running"])
    except Refused as e:
        con.execute("UPDATE plans SET result=? WHERE id=?", (json.dumps(e.out), pid))
        raise
    con.execute("UPDATE plans SET result=? WHERE id=?", (json.dumps(result), pid))
    return result


def handle(doc: object, grant: str, key: str, host: str, cfg_path: Path | None = None,
           db: Path | None = None) -> dict:
    """One `tailshare-start` call: {"v":1, "results":[…]} or {"v":1, "error":…}."""
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
            if doc.get("dry_run") is True and "confirm" not in doc:
                return {"v": 1, "results": dry_run(con, doc.get("starts"), cfg, grant, key, host)}
            if "confirm" in doc and "starts" not in doc:
                return {"v": 1, "results": confirm(con, doc.get("confirm"), cfg, grant, key, host)}
            raise Refused("bad_request", reason="send either dry_run with starts, or confirm")
        finally:
            con.close()
    except Refused as e:
        return {"v": 1, **e.out}


def gate(grant: str, key: str) -> int:
    """`ssh-gate --start=…`: read the request from stdin, print one JSON line, exit 0 only if everything worked."""
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        out = {"v": 1, "error": "bad_request", "reason": "request too large"}
    else:
        try:
            doc = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            doc = None
        out = handle(doc, grant, key, host_name())
    line = json.dumps(out, separators=(",", ":"))
    if len(line) > MAX_OUTPUT:
        line = json.dumps({"v": 1, "error": "too_large", "reason": "the reply is too large; send fewer starts"})
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0 if "error" not in out and not any("error" in r for r in out.get("results", [])) else 1


def host_name() -> str:
    """This machine's tailnet name, as the bus knows it; the hostname if the bus does not answer."""
    try:
        return str(bus.request(f"http://127.0.0.1:{bus.BUS_PORT}/bus/v1/hello", None, timeout=3)["host"])
    except Exception:  # noqa: BLE001
        import socket

        return bus.slug(socket.gethostname().split(".")[0])
