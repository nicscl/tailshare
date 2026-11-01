"""Agent message bus: a mailbox per machine, store-and-forward between tailnet peers.

Every machine's share server also runs a mailbox for the agents on that machine:

  127.0.0.1:BUS_PORT      local API for agents   (register, state, send, inbox, ack, agents)
  <tailnet ip>:BUS_PORT   peer API for mailboxes (hello, deliver)

There is no hub. Addresses are ``host/harness/folder[/session]``, so the host part says
which mailbox to deliver to. Messages for another machine wait in an outbox and are
retried until that machine takes them, so either side can sleep, reboot or go offline.
The host part of a sender's address is checked against the tailnet IP it connects from,
so it cannot be forged.
"""

from __future__ import annotations

import getpass
import json
import math
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, core, power

BUS_PORT = int(os.environ.get("TAILSHARE_BUS_PORT", "8789"))
STATE_DIR = Path(os.environ.get("TAILSHARE_STATE", "~/.tailshare")).expanduser()
PROTOCOL = 1
SHARE_PANES = os.environ.get("TAILSHARE_SHARE_PANES", "1") != "0"  # tmux panes in /hello, for attaching
HEADER = "X-Tailshare"  # required on every request: a web page cannot add it cross-site
TTL = 7 * 86400  # undelivered and unread messages expire after a week
LIVE = 90  # an agent that polled this recently counts as online
KNOWN = 86400  # agents seen within a day still receive pattern sends
MAX_BODY = 64 * 1024
RETRY_MAX = 300

# Never send tailnet traffic through an HTTP proxy from the environment (Codex sets one).
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BusError(Exception):
    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.code = code


def log(msg: str) -> None:
    sys.stderr.write(f"{datetime.now():%H:%M:%S} bus: {msg}\n")


def request(url: str, payload: dict | None = None, timeout: float = 5.0, headers: dict | None = None) -> dict:
    """GET (payload None) or POST JSON to a bus endpoint. Raises BusError on HTTP errors."""
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="GET" if data is None else "POST",
        headers={HEADER: "1", "Content-Type": "application/json", **(headers or {})},
    )
    try:
        with _opener.open(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or str(e)
        except Exception:  # noqa: BLE001
            msg = str(e)
        raise BusError(msg, e.code) from None


# --------------------------------------------------------------------------- addresses


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", (s or "").lower()).strip("-.")
    return s[:40] or "x"


def make_address(host: str, harness: str, folder: str, session: str = "") -> str:
    parts = [slug(host), slug(harness), slug(folder)]
    if session:
        parts.append(slug(session))
    return "/".join(parts)


def _login() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return ""


LOGIN = _login()  # the account this mailbox and its agents run as: what to SSH in as
_LOGIN_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,63}")
_PANE_RE = re.compile(r"%[0-9]{1,6}")
_PATH_RE = re.compile(r"/[^\x00-\x1f\x7f]{0,255}")
_TARGET_RE = re.compile(r"[^\x00-\x1f\x7f]{0,128}")


def clean_login(v: object) -> str:
    return v if isinstance(v, str) and _LOGIN_RE.fullmatch(v) else ""


STATES = ("working", "waiting_input", "idle", "ended")
REASONS = ("permission", "question", "plan", "elicitation")  # why a waiting_input waits; never free text
_ASKS = {"AskUserQuestion": "question", "ExitPlanMode": "plan"}  # tools that wait on the person
_NOTIFIED = {"permission_prompt": ("waiting_input", "permission"), "elicitation_dialog": ("waiting_input", "elicitation"),
             "idle_prompt": ("idle", "")}


def hook_state(d: dict) -> tuple[str, str, str]:
    """(agent id, state, reason) from a Claude Code hook's input or a Codex notify payload, or empty
    strings if the event says nothing about the state."""
    if d.get("type") == "agent-turn-complete":  # Codex notify: the only event it has
        return str(d.get("thread-id") or ""), "idle", ""
    aid, event = str(d.get("session_id") or ""), d.get("hook_event_name")
    state, reason = "", ""
    if event == "PreToolUse" and d.get("tool_name") in _ASKS:
        state, reason = "waiting_input", _ASKS[d["tool_name"]]
    elif event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        state = "working"
    elif event == "Notification":
        state, reason = _NOTIFIED.get(d.get("notification_type"), ("", ""))
    elif event == "Stop":
        state = "idle"
    elif event == "SessionStart" and d.get("source") != "compact":  # after a compaction it carries on working
        state = "idle"
    elif event == "SessionEnd":
        state = "ended"
    return (aid, state, reason) if aid and state else ("", "", "")


def clean_state(s: object) -> dict | None:
    """A peer's {v, reason?, since, at}, or None if it is missing or malformed."""
    if not isinstance(s, dict) or s.get("v") not in STATES:
        return None
    since, at = s.get("since"), s.get("at")
    ok = all(type(t) in (int, float) and math.isfinite(t) and 0 < t <= time.time() + 120 for t in (since, at))
    if not ok or since > at:
        return None
    out = {"v": s["v"], "since": since, "at": at}
    if s["v"] == "waiting_input" and s.get("reason") in REASONS:
        out["reason"] = s["reason"]
    return out


def clean_tmux(t: object) -> dict:
    """A tmux pane {socket, pane, target, bin}, or {} if it is missing or malformed.
    Peers send these too, and they end up in shell commands, so only well-formed values pass."""
    if not isinstance(t, dict):
        return {}
    sock, pane = t.get("socket"), t.get("pane")
    if not (isinstance(sock, str) and _PATH_RE.fullmatch(sock) and isinstance(pane, str) and _PANE_RE.fullmatch(pane)):
        return {}
    out = {"socket": sock, "pane": pane, "target": "", "bin": ""}
    if isinstance(t.get("target"), str) and _TARGET_RE.fullmatch(t["target"]):
        out["target"] = t["target"]
    if isinstance(t.get("bin"), str) and _PATH_RE.fullmatch(t["bin"]):
        out["bin"] = t["bin"]
    return out


def is_pattern(target: str) -> bool:
    """Patterns and @user targets reach the agents that exist now; exact addresses wait for theirs."""
    return target.startswith("@") or any(c in target for c in "*?[")


def split_target(target: str) -> tuple[str | None, str]:
    """``@user/rest`` → (user, rest or "*"); anything else → (None, target)."""
    if target.startswith("@"):
        user, _, rest = target[1:].partition("/")
        return user, rest or "*"
    return None, target


def handle(login: str) -> str:
    """A tailnet login (nick@example.com) as a short user name (nick)."""
    return slug(login.split("@")[0]) if login else ""


def matches(address: str, target: str) -> bool:
    """True if `target` names `address`: exactly, as a glob, or as a prefix (host, host/harness, …/folder).

    A collision suffix (``:6d2b``) is optional when matching.
    """
    base = address.split(":")[0]
    return any(fnmatchcase(a, t) for a in (address, base) for t in (target, target + "/*"))


def target_matches(address: str, user: str, target: str) -> bool:
    """`matches`, plus ``@user`` / ``@user/<address pattern>`` targets."""
    want, rest = split_target(target)
    if want is not None and not fnmatchcase(user or "", want):
        return False
    return matches(address, rest)


# --------------------------------------------------------------------------- tailnet view


def clean_host_keys(keys: object) -> list[str]:
    """`type base64` host key lines from tailscale status, dropping anything else."""
    out = []
    for k in keys if isinstance(keys, list) else []:
        parts = k.split() if isinstance(k, str) else []
        if len(parts) >= 2 and re.fullmatch(r"[a-z0-9@.-]{1,64}", parts[0]) and re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1]):
            out.append(f"{parts[0]} {parts[1]}")
    return out


@dataclass
class Net:
    """This machine's tailnet name and IP, and the other machines, from `tailscale status`."""

    host: str = ""
    ip: str = ""
    user: str = ""  # tailnet login that owns this machine
    peers: dict[str, dict] = field(default_factory=dict)  # host -> {"ip", "port", "online", "user"}
    updated: float = 0.0
    tailscale_ssh: bool = False  # Tailscale SSH answers port 22 here (it ignores authorized_keys)
    ssh_host_keys: list[str] = field(default_factory=list)  # Tailscale SSH's host keys here, per tailscale status
    node_id: str = ""
    nodes: list[dict] = field(default_factory=list)  # every peer node, even ones sharing a name: {"id", "host", ...}

    def host_for_ip(self, ip: str) -> str | None:
        if ip == self.ip:
            return self.host
        return next((h for h, p in self.peers.items() if p["ip"] == ip), None)

    def user_for_ip(self, ip: str) -> str | None:
        """Tailnet login that owns the device at `ip` (None if it is not on the tailnet)."""
        if ip == self.ip:
            return self.user
        return next((p.get("user", "") for p in self.peers.values() if p["ip"] == ip), None)

    def refresh(self) -> None:
        self.updated = time.time()
        if not core.TAILSCALE:
            self.host = self.host or slug(socket.gethostname().split(".")[0])
            return
        try:
            d = json.loads(core._run_ts("status", "--json", timeout=8).stdout)
        except Exception as e:  # noqa: BLE001
            log(f"tailscale status failed: {e}")
            return

        def name(n: dict) -> str:
            # iOS devices report HostName "localhost"; the MagicDNS label is the real name.
            return slug((n.get("DNSName") or "").split(".")[0] or n.get("HostName") or "")

        logins = {str(k): v.get("LoginName", "") for k, v in (d.get("User") or {}).items()}
        me = d.get("Self") or {}
        self.host = name(me)
        self.ip = next(iter(me.get("TailscaleIPs") or []), "")
        self.user = logins.get(str(me.get("UserID")), "")
        self.tailscale_ssh = bool(me.get("sshHostKeys"))
        self.ssh_host_keys = clean_host_keys(me.get("sshHostKeys"))
        self.node_id = str(me.get("ID") or "")
        peers, nodes = {}, []
        for p in (d.get("Peer") or {}).values():
            ips = p.get("TailscaleIPs") or []
            if ips:
                peers[name(p)] = {"ip": ips[0], "port": BUS_PORT, "online": bool(p.get("Online")),
                                  "user": logins.get(str(p.get("UserID")), ""),
                                  "ssh_host_keys": clean_host_keys(p.get("sshHostKeys")), "id": str(p.get("ID") or "")}
                nodes.append({"host": name(p), **peers[name(p)]})
        self.peers, self.nodes = peers, nodes


# --------------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, address TEXT NOT NULL, harness TEXT, cwd TEXT, session TEXT,
  pid INTEGER, first_seen REAL, last_seen REAL, user TEXT NOT NULL DEFAULT '', tmux TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, sender TEXT NOT NULL, target TEXT NOT NULL, body TEXT NOT NULL,
  files TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, received REAL NOT NULL,
  sender_user TEXT NOT NULL DEFAULT '');
-- one row per recipient; agent is NULL while an exact address has no agent yet
CREATE TABLE IF NOT EXISTS inbox(msg TEXT NOT NULL, agent TEXT, read REAL, UNIQUE(msg, agent));
CREATE INDEX IF NOT EXISTS inbox_agent ON inbox(agent, read);
CREATE TABLE IF NOT EXISTS outbox(
  msg TEXT NOT NULL, host TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL, done REAL, error TEXT,
  PRIMARY KEY(msg, host));
CREATE TABLE IF NOT EXISTS peers(
  host TEXT PRIMARY KEY, ip TEXT, version TEXT, last_ok REAL, agents TEXT NOT NULL DEFAULT '[]');
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)  # notified when an inbox row arrives
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            for table, col in (("agents", "user"), ("messages", "sender_user"), ("agents", "tmux"), ("peers", "node"), ("peers", "power"), ("agents", "state")):  # added after 1st release
                if col not in {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql: str, *args) -> sqlite3.Row | None:
        rows = self.q(sql, *args)
        return rows[0] if rows else None

    def x(self, sql: str, *args) -> int:
        with self.lock:
            return self.db.execute(sql, args).rowcount


def _message(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "sender": r["sender"], "target": r["target"], "body": r["body"],
        "files": json.loads(r["files"]), "created": r["created"], "sender_user": r["sender_user"],
    }


def _check_message(m: object) -> dict:
    if not isinstance(m, dict):
        raise BusError("msg must be an object")
    ok = (
        isinstance(m.get("id"), str) and 0 < len(m["id"]) <= 64
        and isinstance(m.get("sender"), str) and 0 < len(m["sender"]) <= 200
        and isinstance(m.get("target"), str) and 0 < len(m["target"]) <= 200
        and isinstance(m.get("body"), str) and len(m["body"]) <= MAX_BODY
        and isinstance(m.get("files", []), list) and len(m.get("files", [])) <= 20
        and all(isinstance(f, str) and len(f) <= 500 for f in m.get("files", []))
        and isinstance(m.get("created"), (int, float))
        and isinstance(m.get("sender_user", ""), str) and len(m.get("sender_user", "")) <= 64
    )
    if not ok:
        raise BusError("malformed message")
    return m


# --------------------------------------------------------------------------- the mailbox


class Bus:
    def __init__(self, store: Store, net: Net, port: int = BUS_PORT) -> None:
        self.store, self.net, self.port = store, net, port
        self._alive: dict[tuple[str, str], tuple[float, bool]] = {}
        self._pair_tokens: dict[str, dict] = {}  # one-time tokens of `tailshare hive pair` codes, in memory only
        self.kick = threading.Event()  # wakes the delivery loop
        self._peer_server: ThreadingHTTPServer | None = None
        self._peer_ip = ""
        self._pruned = 0.0
        self.updater = None  # set by start_bus when self-update is available
        self.sessions = None  # set by start_bus: sessions.Sessions (index, search, ask, launch)
        self.keys = None  # set by start_bus: keys.Keys (SSH keys devices ask for)
        self.battery = power.Battery()

    # -- agents ---------------------------------------------------------------
    def register(self, a: dict) -> dict:
        aid = str(a.get("id") or "").strip()[:100]
        if not aid:
            raise BusError("agent id required")
        harness = slug(a.get("harness") or "cli")
        cwd = str(a.get("cwd") or "")
        session = str(a.get("session") or "").strip()
        user = self._user_for(a)
        tmux = clean_tmux(a.get("tmux"))
        base = make_address(self.net.host, harness, Path(cwd).name or "root", session)
        now = time.time()
        with self.store.lock:
            row = self.store.one("SELECT address FROM agents WHERE id=?", aid)
            if row and row["address"].split(":")[0] == base:
                address = row["address"]  # keep a collision suffix stable
            else:
                live = self.store.q("SELECT address FROM agents WHERE id!=? AND last_seen>?", aid, now - LIVE)
                clash = any(r["address"].split(":")[0] == base for r in live)
                address = f"{base}:{slug(aid)[:4]}" if clash else base
            self.store.x(
                "INSERT INTO agents(id, address, harness, cwd, session, pid, first_seen, last_seen, user, tmux)"
                " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET address=excluded.address,"
                " harness=excluded.harness, cwd=excluded.cwd, session=excluded.session,"
                " pid=excluded.pid, last_seen=excluded.last_seen, user=excluded.user, tmux=excluded.tmux",
                aid, address, harness, cwd, session, int(a.get("pid") or 0), now, now, user,
                json.dumps(tmux) if tmux else "",
            )
            self._claim(aid, address, user)
        return self.agent(aid)

    def _user_for(self, a: dict) -> str:
        """The human behind a session: set explicitly, else the tailnet user of the device that
        SSH'd in, else the owner of this machine."""
        if a.get("user"):
            return slug(str(a["user"]))
        ip = str(a.get("ssh_ip") or "")
        if ip:
            login = self.net.user_for_ip(ip)
            if login is None and time.time() - self.net.updated > 5:
                self.net.refresh()
                login = self.net.user_for_ip(ip)
            if login:
                return handle(login)
        return handle(self.net.user)

    def _claim(self, aid: str, address: str, user: str = "") -> None:
        """Give an agent the messages that were waiting for its address."""
        pending = self.store.q(
            "SELECT inbox.rowid AS rid, m.target FROM inbox JOIN messages m ON m.id=inbox.msg WHERE inbox.agent IS NULL"
        )
        hit = [r["rid"] for r in pending if target_matches(address, user, r["target"])]
        for rid in hit:
            self.store.x("UPDATE OR IGNORE inbox SET agent=? WHERE rowid=?", aid, rid)
        if hit:
            self.store.changed.notify_all()

    def agent(self, aid: str) -> dict:
        r = self.store.one("SELECT * FROM agents WHERE id=?", aid)
        if r is None:
            raise BusError("unknown agent; register first", 404)
        return self._agent(r)

    def _agent(self, r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "address": r["address"], "user": r["user"], "harness": r["harness"], "session": r["session"],
            "cwd": r["cwd"], "pid": r["pid"], "last_seen": r["last_seen"],
            "online": r["last_seen"] > time.time() - LIVE, "host": self.net.host,
            "tmux": json.loads(r["tmux"]) if r["tmux"] else {}, "login": LOGIN,
            "state": json.loads(r["state"]) if r["state"] else None,
        }

    def report_state(self, d: dict) -> dict:
        """What an agent's harness says it is doing, from its hooks. Only agents that registered count."""
        aid, state, reason = hook_state(d)
        if not state:
            return {"ok": False}
        now = time.time()
        with self.store.lock:
            r = self.store.one("SELECT state FROM agents WHERE id=?", aid)
            if r is None:
                return {"ok": False}
            old = json.loads(r["state"]) if r["state"] else {}
            new = {"v": state, "since": now, "at": now}
            if reason:
                new["reason"] = reason
            if old.get("v") == state and old.get("reason") == new.get("reason"):
                new["since"] = old.get("since", now)  # the same state again: it began earlier
            self.store.x("UPDATE agents SET state=? WHERE id=?", json.dumps(new), aid)
            if state != "ended":  # a report shows the session is alive; a closed one is not online
                self.store.x("UPDATE agents SET last_seen=? WHERE id=?", now, aid)
        return {"ok": True, "state": new}

    def by_pid(self, pid: int) -> dict | None:
        r = self.store.one("SELECT * FROM agents WHERE pid=? ORDER BY last_seen DESC", pid)
        return self._agent(r) if r else None

    def touch(self, aid: str) -> None:
        self.store.x("UPDATE agents SET last_seen=? WHERE id=?", time.time(), aid)

    def local_agents(self) -> list[dict]:
        rows = self.store.q("SELECT * FROM agents WHERE last_seen>? ORDER BY last_seen DESC", time.time() - KNOWN)
        return [self._agent(r) for r in rows]

    def directory(self) -> dict:
        """Every agent this mailbox knows of: its own, and what peers said at their last hello."""
        out = self.local_agents()
        for p in self.store.q("SELECT * FROM peers WHERE last_ok IS NOT NULL ORDER BY host"):
            up = bool(self.net.peers.get(p["host"], {}).get("online"))
            for a in json.loads(p["agents"]):
                a.update(host=p["host"], online=bool(a.get("online")) and up, host_online=up,
                         tmux=clean_tmux(a.get("tmux")), login=clean_login(a.get("login")),
                         state=clean_state(a.get("state")))
                out.append(a)
        return {"host": self.net.host, "agents": out}

    # -- messages -------------------------------------------------------------
    def send(self, aid: str, target: str, body: str, files: list[str] | None = None) -> dict:
        me = self.agent(aid)
        self.touch(aid)
        target = target.strip().lower()
        if not target or len(target) > 200:
            raise BusError("address required: host/harness/folder[/session] or a pattern like */*/tailshare")
        if not body.strip():
            raise BusError("empty message")
        if len(body) > MAX_BODY:
            raise BusError(f"message too long (max {MAX_BODY // 1024} KB)")
        if time.time() - self.net.updated > 15:
            self.net.refresh()
        host_pat = split_target(target)[1].split("/")[0]
        if is_pattern(host_pat):
            known = {r["host"] for r in self.store.q("SELECT host FROM peers WHERE last_ok IS NOT NULL")}
            hosts = sorted(h for h in known | {self.net.host} if fnmatchcase(h, host_pat))
        elif host_pat == self.net.host or host_pat in self.net.peers:
            hosts = [host_pat]
        else:
            raise BusError(f"no machine named {host_pat!r} on the tailnet")
        m = {
            "id": uuid.uuid4().hex, "sender": me["address"], "sender_user": me["user"], "target": target,
            "body": body, "files": list(files or []), "created": time.time(),
        }
        routes = []
        for h in hosts:
            if h == self.net.host:
                self.receive(m)
                routes.append({"host": h, "status": "delivered"})
            else:
                self.store.x(
                    "INSERT OR IGNORE INTO outbox(msg, host, payload, created, next_try) VALUES(?,?,?,?,?)",
                    m["id"], h, json.dumps(m), m["created"], m["created"],
                )
                routes.append({"host": h, "status": self._attempt(m["id"], h)})
        return {"id": m["id"], "sender": me["address"], "routes": routes}

    def receive(self, m: dict) -> bool:
        """File a message into this mailbox. Returns False for a duplicate."""
        now = time.time()
        with self.store.lock:
            n = self.store.x(
                "INSERT OR IGNORE INTO messages(id, sender, target, body, files, created, received, sender_user)"
                " VALUES(?,?,?,?,?,?,?,?)",
                m["id"], m["sender"], m["target"], m["body"], json.dumps(m.get("files", [])), m["created"], now,
                m.get("sender_user", ""),
            )
            if n == 0:
                return False
            agents = self.store.q("SELECT id, address, user FROM agents WHERE last_seen>?", now - KNOWN)
            hits = [a["id"] for a in agents
                    if a["address"] != m["sender"] and target_matches(a["address"], a["user"], m["target"])]
            if hits:
                for aid in hits:
                    self.store.x("INSERT OR IGNORE INTO inbox(msg, agent) VALUES(?,?)", m["id"], aid)
            elif not is_pattern(m["target"]):
                self.store.x("INSERT INTO inbox(msg, agent) VALUES(?, NULL)", m["id"])  # waits for the address
            self.store.changed.notify_all()
        return True

    def inbox(self, aid: str, wait: float = 0.0, peek: bool = False) -> list[dict]:
        """Unread messages for an agent, oldest first. Long-polls up to `wait` seconds."""
        self.agent(aid)
        deadline = time.time() + max(0.0, min(wait, 55.0))
        sql = (
            "SELECT m.* FROM inbox JOIN messages m ON m.id=inbox.msg"
            " WHERE inbox.agent=? AND inbox.read IS NULL ORDER BY m.created"
        )
        with self.store.changed:
            while True:
                self.touch(aid)
                rows = self.store.q(sql, aid)
                left = deadline - time.time()
                if rows or left <= 0:
                    break
                self.store.changed.wait(min(left, 20))
            if rows and not peek:
                self.ack(aid, [r["id"] for r in rows])
        return [_message(r) for r in rows]

    def ack(self, aid: str, ids: list[str]) -> int:
        now = time.time()
        return sum(
            self.store.x("UPDATE inbox SET read=? WHERE agent=? AND msg=? AND read IS NULL", now, aid, i)
            for i in ids
        )

    def outbox(self) -> list[dict]:
        rows = self.store.q("SELECT * FROM outbox WHERE done IS NULL ORDER BY created")
        return [
            {"id": r["msg"], "host": r["host"], "target": json.loads(r["payload"])["target"],
             "created": r["created"], "attempts": r["attempts"], "error": r["error"]}
            for r in rows
        ]

    # -- peers ----------------------------------------------------------------
    def hello(self, ip: str = "") -> dict:
        """What any tailnet peer may know. With SHARE_PANES, also where each agent's tmux pane is,
        so a person can attach to it; SSH to that machine is still the gate. `you` is what the tailnet
        says about the caller, so a client can tell its own machines from ones shared with it."""
        keys = ("address", "user", "harness", "session", "online", "last_seen") + (("tmux", "login", "state") if SHARE_PANES else ())
        agents = [{k: a[k] for k in keys if a.get(k) is not None} for a in self.local_agents()]
        for a in agents:
            if a.get("tmux"):
                a["tmux"] = {**a["tmux"], "alive": self._pane_alive(a["tmux"])}
        ssh = self.keys.ssh_info() if self.keys else {"enroll": False}
        out = {"v": PROTOCOL, "version": self.version(), "host": self.net.host, "agents": agents, "ssh": ssh}
        if ip:
            if self.net.host_for_ip(ip) is None and time.time() - self.net.updated > 30:
                self.net.refresh()
            out["you"] = {"user": self.net.user_for_ip(ip) or "", "node": self.net.host_for_ip(ip) or ""}
            u = out["you"]["user"]  # tagged machines have no owner: other tagged devices aren't own
            battery = self.battery.for_caller(own=bool(u) and u != "tagged-devices" and u == self.net.user)
            if battery:
                out["power"] = battery
        return out

    def _pane_alive(self, t: dict) -> bool:
        """Whether the registered pane still exists (the gate's check), cached a few seconds: /hello is open
        to the tailnet and should not run tmux on every call."""
        from . import keys

        k = (t.get("socket", ""), t.get("pane", ""))
        hit = self._alive.get(k)
        if hit and hit[0] > time.time() - 5:
            return hit[1]
        binary = keys.tmux_binary(t.get("bin") or "")
        alive = bool(binary) and keys.own_socket(k[0]) and keys.pane_alive(binary, *k)
        self._alive[k] = (time.time(), alive)
        return alive

    # -- Hive app pairing -----------------------------------------------------
    PAIR_TTL = 600  # the longest a `tailshare hive pair` code may live

    def add_pair_token(self, t: str, code: str, exp: float) -> dict:
        """From `tailshare hive pair` (local API). The token grants nothing: it lets the app tell a fresh code
        from a used one, and lets the waiting terminal say which device used it."""
        now = time.time()
        if not re.fullmatch(r"[A-Za-z0-9_-]{22,64}", t) or not re.fullmatch(r"[A-Z0-9]{6}", code):
            raise BusError("bad token or code")
        if not now < exp <= now + self.PAIR_TTL + 5:
            raise BusError(f"expiry must be within {self.PAIR_TTL} seconds")
        with self.store.lock:
            for k in [k for k, v in self._pair_tokens.items() if v["exp"] <= now - 3600]:
                del self._pair_tokens[k]
            if sum(v["exp"] > now for v in self._pair_tokens.values()) >= 20:
                raise BusError("too many pairing codes open", 429)
            self._pair_tokens[t] = {"exp": exp, "code": code, "used_by": None}
        return {"ok": True}

    def pair_status(self, t: str) -> dict:
        """For the waiting `tailshare hive pair`: waiting, used (by whom, per the tailnet) or expired."""
        with self.store.lock:
            v = self._pair_tokens.get(t)
            used = dict(v["used_by"]) if v and v["used_by"] else None
        if v is None:
            raise BusError("no such pairing code", 404)
        if used:
            return {"state": "used", **used}
        return {"state": "expired" if v["exp"] < time.time() else "waiting"}

    def use_pair_token(self, ip: str, body: dict) -> dict:
        """POST /hive/pair {t, device_name} from the phone: 200 once, then 410; 404 if unknown. A key for the
        phone is a separate, normal POST /keys/enroll."""
        from .keys import clean_device

        t, now = str(body.get("t") or ""), time.time()
        if t in self._pair_tokens and self.net.host_for_ip(ip) is None and now - self.net.updated > 30:
            self.net.refresh()  # outside the lock: it runs `tailscale status`
        with self.store.lock:
            v = self._pair_tokens.get(t)
            if v is None:
                raise BusError("no pairing is running with that code", 404)
            if v["used_by"] or v["exp"] < now:
                raise BusError("pairing code already used or expired", 410)
            you = {"user": self.net.user_for_ip(ip) or "", "node": self.net.host_for_ip(ip) or ""}
            v["used_by"] = {"ip": ip, **you, "device": clean_device(body.get("device_name"))}
        return {"paired": True, "you": you, "owner": self.net.user, "host": self.net.host, "code": v["code"]}

    def peer_list(self, ip: str) -> list[dict]:
        """Every node in this machine's `tailscale status`, for clients that cannot run it (phones), with
        `tailshare` saying which ones have answered this machine as tailshare peers. Only the owner's devices
        may ask: it names their machines. Rows are about IPs and node ids. id, ip, owner and online are the
        coordination server's view in tailscale status, never what a node tells tailshare. ssh_host_keys is the
        node's own Hostinfo claim (tied to its node key, not checked by Tailscale), present only on nodes
        running Tailscale SSH. `tailshare` needs the same IP and node id that answered /hello, so a node
        that takes over a reused IP does not inherit it."""
        self.require_same_user(ip)
        own = self.net.ssh_host_keys if self.net.tailscale_ssh else (self.keys.ssh_info()["host_keys"] if self.keys else [])
        out = [{"id": self.net.node_id, "host": self.net.host, "ip": self.net.ip, "port": self.port,
                "owner": self.net.user, "online": True, "ssh_host_keys": own, "tailshare": True, "self": True}]
        answered = {(r["ip"], r["node"]) for r in self.store.q("SELECT ip, node FROM peers WHERE last_ok IS NOT NULL")}
        nodes = self.net.nodes or [{"id": "", "host": h, **p} for h, p in self.net.peers.items()]
        for n in sorted(nodes, key=lambda n: (n["host"], n["ip"])):
            ts = (n["ip"], n.get("id", "")) in answered
            out.append({"id": n.get("id", ""), "host": n["host"], "ip": n["ip"],
                        "port": n.get("port", BUS_PORT) if ts else None, "owner": n.get("user", ""),
                        "online": bool(n.get("online")), "ssh_host_keys": n.get("ssh_host_keys", []),
                        "tailshare": ts, "self": False})
        return out

    def version(self) -> str:
        from .update import describe

        return describe()

    def machines(self) -> list[dict]:
        """This machine and every peer that has answered, with tailshare versions."""
        out = [{"host": self.net.host, "ip": self.net.ip, "online": True, "version": self.version(),
                "last_ok": time.time(), "agents": len(self.local_agents()), "self": True,
                "update": self.updater.status if self.updater else None,
                "power": power.clean(self.battery.for_caller(own=True))}]
        for p in self.store.q("SELECT * FROM peers WHERE last_ok IS NOT NULL ORDER BY host"):
            out.append({"host": p["host"], "ip": p["ip"], "version": p["version"], "last_ok": p["last_ok"],
                        "online": bool(self.net.peers.get(p["host"], {}).get("online")),
                        "agents": len(json.loads(p["agents"])), "self": False,
                        "power": power.clean(json.loads(p["power"] or "null"))})
        return out

    def update_trigger(self, ip: str) -> dict:
        """A peer asks this machine to check for an update. Only machines of the same tailnet user may."""
        from .update import ENABLED

        if self.updater is None or not ENABLED:
            raise BusError("auto-update is off on this machine", 409)
        self.require_same_user(ip)
        return self.updater.trigger()

    def require_same_user(self, ip: str) -> None:
        """Only machines owned by this machine's tailnet user may update it or read its sessions."""
        if self.net.user_for_ip(ip) is None:
            self.net.refresh()
        who = self.net.user_for_ip(ip)
        if not who or who != self.net.user:
            raise BusError(f"{ip} belongs to {who or 'nobody on this tailnet'}, not {self.net.user}", 403)

    def update(self, everywhere: bool) -> dict:
        """`tailshare update [--all]`: tell peers to check, then update this machine (may restart)."""
        peers = {}
        if everywhere:
            for m in self.machines()[1:]:
                p = self.net.peers.get(m["host"])
                if not p or not p["online"]:
                    peers[m["host"]] = "offline"
                    continue
                try:
                    r = request(f"http://{p['ip']}:{p['port']}/bus/v1/update", {"v": PROTOCOL}, timeout=5)
                    peers[m["host"]] = f"{r.get('result')} (now {r.get('version')})"
                except Exception as e:  # noqa: BLE001
                    peers[m["host"]] = f"failed: {e}"
        mine = self.updater.check("requested") if self.updater else {"result": "self-update unavailable"}
        return {"self": mine, "peers": peers}

    def accept(self, ip: str, body: dict) -> dict:
        """A peer mailbox hands over a message. The sender's host must be the connecting machine."""
        if body.get("v") != PROTOCOL:
            raise BusError(f"protocol {body.get('v')} not supported (this mailbox speaks {PROTOCOL})")
        m = _check_message(body.get("msg"))
        who = self.net.host_for_ip(ip)
        if who is None:
            self.net.refresh()
            who = self.net.host_for_ip(ip)
        claimed = m["sender"].split("/")[0]
        if who != claimed:
            raise BusError(f"{ip} is {who or 'not on the tailnet'}, not {claimed}", 403)
        fresh = self.receive(m)
        return {"ok": True, "duplicate": not fresh}

    def _attempt(self, msg_id: str, host: str) -> str:
        """Try to hand one outbox row to its machine now. Returns a short status."""
        row = self.store.one("SELECT * FROM outbox WHERE msg=? AND host=?", msg_id, host)
        if row is None or row["done"]:
            return "delivered"
        peer = self.net.peers.get(host)
        if not peer or not peer["online"]:
            self.store.x("UPDATE outbox SET error='machine offline' WHERE msg=? AND host=?", msg_id, host)
            return "queued (machine offline)"
        url = f"http://{peer['ip']}:{peer['port']}/bus/v1/deliver"
        try:
            request(url, {"v": PROTOCOL, "msg": json.loads(row["payload"])}, timeout=4)
        except BusError as e:
            if 400 <= e.code < 500:  # the peer refused it; retrying will not help
                self.store.x("UPDATE outbox SET done=?, error=? WHERE msg=? AND host=?", time.time(), f"rejected: {e}", msg_id, host)
                log(f"{host} rejected {msg_id[:8]}: {e}")
                return f"rejected: {e}"
            err = str(e)
        except Exception as e:  # noqa: BLE001
            err = getattr(e, "reason", None) or e
            err = str(err)
        else:
            now = time.time()
            self.store.x("UPDATE outbox SET done=?, error=NULL WHERE msg=? AND host=?", now, msg_id, host)
            self.store.x("UPDATE peers SET last_ok=? WHERE host=?", now, host)
            return "delivered"
        attempts = row["attempts"] + 1
        self.store.x(
            "UPDATE outbox SET attempts=?, next_try=?, error=? WHERE msg=? AND host=?",
            attempts, time.time() + min(RETRY_MAX, 5 * 2 ** attempts), err[:200], msg_id, host,
        )
        return "queued (no tailshare mailbox answering)" if "refused" in err.lower() else "queued"

    def discover(self, hosts: list[str]) -> None:
        """Ask peers for their agents. Machines that do not run tailshare simply do not answer."""

        def ask(h: str) -> None:
            p = self.net.peers[h]
            try:
                d = request(f"http://{p['ip']}:{p['port']}/bus/v1/hello", timeout=2)
            except Exception:  # noqa: BLE001
                return
            self.store.x(
                "INSERT INTO peers(host, ip, version, last_ok, agents, node, power) VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(host) DO UPDATE SET ip=excluded.ip, version=excluded.version,"
                " last_ok=excluded.last_ok, agents=excluded.agents, node=excluded.node, power=excluded.power",
                h, p["ip"], str(d.get("version", "")), time.time(), json.dumps(d.get("agents", [])), p.get("id", ""),
                json.dumps(power.clean(d.get("power"))),
            )

        if hosts:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(ask, hosts))

    def deliver_pending(self) -> None:
        now = time.time()
        self.store.x(
            "UPDATE outbox SET done=?, error='expired' WHERE done IS NULL AND created<?", now, now - TTL
        )
        online = {h for h, p in self.net.peers.items() if p["online"]}
        for r in self.store.q("SELECT msg, host FROM outbox WHERE done IS NULL AND next_try<=?", now):
            if r["host"] in online:
                self._attempt(r["msg"], r["host"])

    def prune(self) -> None:
        cutoff = time.time() - TTL
        with self.store.lock:
            self.store.x("DELETE FROM inbox WHERE msg IN (SELECT id FROM messages WHERE created<?)", cutoff)
            self.store.x("DELETE FROM outbox WHERE done IS NOT NULL AND done<?", cutoff)
            self.store.x(
                "DELETE FROM messages WHERE created<? AND id NOT IN (SELECT msg FROM outbox WHERE done IS NULL)",
                cutoff,
            )
            self.store.x("DELETE FROM agents WHERE last_seen<?", time.time() - 30 * 86400)

    # -- running --------------------------------------------------------------
    def _loop(self) -> None:
        was_online: set[str] = set()
        last_hello = 0.0
        while True:
            self.kick.wait(5)
            self.kick.clear()
            try:
                now = time.time()
                if now - self.net.updated > 15:
                    self.net.refresh()
                    self._bind_peer()
                online = {h for h, p in self.net.peers.items() if p["online"]}
                back = online - was_online
                was_online = online
                if back:  # a machine came back: retry what waits for it right away
                    for h in back:
                        self.store.x("UPDATE outbox SET next_try=? WHERE host=? AND done IS NULL", now, h)
                if back or now - last_hello > 60:
                    self.discover(sorted(online))
                    last_hello = now
                self.deliver_pending()
                if now - self._pruned > 600:
                    self.prune()
                    self._pruned = now
            except Exception as e:  # noqa: BLE001
                log(f"delivery loop: {e!r}")

    def listen(self, addr: str, local: bool) -> ThreadingHTTPServer:
        srv = ThreadingHTTPServer((addr, self.port), _Handler)
        srv.daemon_threads = True
        srv.bus = self  # type: ignore[attr-defined]
        srv.local = local  # type: ignore[attr-defined]
        threading.Thread(target=srv.serve_forever, daemon=True, name=f"bus-{addr}").start()
        log(f"{'local' if local else 'peer'} API on http://{addr}:{self.port}")
        return srv

    def _bind_peer(self) -> None:
        """Listen on the tailnet IP (and follow it if it changes)."""
        ip = self.net.ip
        if not ip or ip == self._peer_ip:
            return
        if self._peer_server:
            self._peer_server.shutdown()
            self._peer_server.server_close()
            self._peer_server, self._peer_ip = None, ""
        try:
            self._peer_server = self.listen(ip, local=False)
            self._peer_ip = ip
        except OSError as e:
            log(f"cannot listen on {ip}:{self.port}: {e}")

    def start(self) -> None:
        self.net.refresh()
        self.listen("127.0.0.1", local=True)
        self._bind_peer()
        threading.Thread(target=self._loop, daemon=True, name="bus-loop").start()


class _Handler(BaseHTTPRequestHandler):
    server_version = f"tailshare-bus/{__version__}"

    def log_message(self, fmt, *args):  # requests are not logged; failures are
        pass

    @property
    def bus(self) -> Bus:
        return self.server.bus  # type: ignore[attr-defined]

    def _reply(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4 * MAX_BODY:
            raise BusError("request too large", 413)
        d = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(d, dict):
            raise BusError("expected a JSON object")
        return d

    def _sessions(self, path: str, body: dict, local: bool, ip: str) -> dict:
        s = self.bus.sessions
        if s is None:
            raise BusError("sessions are not available on this machine", 409)
        if local:
            routes = {"/sessions/search": s.search, "/sessions/resolve": lambda b: s.resolve(str(b.get("ref") or "")),
                      "/sessions/ask": s.ask, "/sessions/launch": s.launch}
        else:
            self.bus.require_same_user(ip)  # transcripts are private to their owner
            routes = {"/sessions/search": lambda b: {"results": s.search_local(b)}, "/sessions/ask": s.ask_local,
                      "/sessions/launch": lambda b: s.launch_local(str(b.get("payload") or ""), str(b.get("signature") or ""))}
        fn = routes.get(path)
        if fn is None:
            raise BusError("not found", 404)
        return fn(body)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        local = self.server.local  # type: ignore[attr-defined]
        if self.headers.get(HEADER) != "1":
            return self._reply(403, {"error": f"missing {HEADER} header"})
        ip = self.client_address[0]
        if local and ip != "127.0.0.1":
            return self._reply(403, {"error": "local API"})
        url = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            body = self._body() if method == "POST" else {}
            b = self.bus
            path = url.path.removeprefix("/bus/v1")
            if path.startswith("/sessions/") and method == "POST":
                return self._reply(200, self._sessions(path, body, local, ip))
            if path.startswith("/keys/"):
                if b.keys is None:
                    raise BusError("SSH keys are not available on this machine", 409)
                return self._reply(200, b.keys.route(method, path, body, local, ip, self.headers.get("X-Tailshare-Keys", "")))
            if not local:
                if (method, path) == ("GET", "/hello"):
                    return self._reply(200, b.hello(ip))
                if (method, path) == ("GET", "/peers"):
                    return self._reply(200, {"peers": b.peer_list(ip)})
                if (method, path) == ("POST", "/hive/pair"):
                    return self._reply(200, b.use_pair_token(ip, body))
                if (method, path) == ("POST", "/deliver"):
                    return self._reply(200, b.accept(ip, body))
                if (method, path) == ("POST", "/update"):
                    return self._reply(200, b.update_trigger(ip))
            elif (method, path) == ("POST", "/register"):
                return self._reply(200, b.register(body))
            elif (method, path) == ("POST", "/state"):
                return self._reply(200, b.report_state(body))
            elif (method, path) == ("POST", "/send"):
                return self._reply(200, b.send(str(body.get("from", "")), str(body.get("to", "")),
                                               str(body.get("body", "")), list(body.get("files") or [])))
            elif (method, path) == ("GET", "/inbox"):
                msgs = b.inbox(qs.get("agent", ""), float(qs.get("wait", 0)), qs.get("peek") == "1")
                return self._reply(200, {"messages": msgs})
            elif (method, path) == ("POST", "/ack"):
                return self._reply(200, {"acked": b.ack(str(body.get("agent", "")), list(body.get("ids") or []))})
            elif (method, path) == ("GET", "/agents"):
                if "pid" in qs:
                    return self._reply(200, {"agent": b.by_pid(int(qs["pid"]))})
                return self._reply(200, b.directory())
            elif (method, path) == ("GET", "/outbox"):
                return self._reply(200, {"outbox": b.outbox()})
            elif (method, path) == ("POST", "/hive/token"):
                return self._reply(200, b.add_pair_token(str(body.get("token") or ""), str(body.get("code") or ""),
                                                         float(body.get("exp") or 0)))
            elif (method, path) == ("GET", "/hive/status"):
                return self._reply(200, b.pair_status(qs.get("t", "")))
            elif (method, path) == ("GET", "/machines"):
                return self._reply(200, {"machines": b.machines()})
            elif (method, path) == ("POST", "/update"):
                return self._reply(200, b.update(bool(body.get("all"))))
            self._reply(404, {"error": "not found"})
        except BusError as e:
            self._reply(e.code, {"error": str(e)})
        except (ValueError, json.JSONDecodeError) as e:
            self._reply(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            log(f"{method} {url.path}: {e!r}")
            self._reply(500, {"error": repr(e)})


def start_bus() -> Bus | None:
    """Start this machine's mailbox inside the share server. Never takes the file server down."""
    try:
        b = Bus(Store(STATE_DIR / "bus.db"), Net())
        b.start()
    except Exception as e:  # noqa: BLE001
        log(f"not started: {e!r}")
        return None
    try:
        from . import sessions

        b.sessions = sessions.start(b)
    except Exception as e:  # noqa: BLE001
        log(f"sessions not started: {e!r}")
    try:
        from . import keys

        b.keys = keys.start(b)
    except Exception as e:  # noqa: BLE001
        log(f"keys not started: {e!r}")
    try:
        from . import agent

        if agent.upgrade_claude_hooks():  # after an update, sessions that joined the bus get the new hooks
            log("added newer tailshare hooks to Claude Code settings")
    except Exception as e:  # noqa: BLE001
        log(f"Claude Code hooks not upgraded: {e!r}")
    try:
        from .update import ENABLED, Updater

        b.updater = Updater()
        if ENABLED:
            b.updater.start()
    except Exception as e:  # noqa: BLE001
        log(f"self-update not started: {e!r}")
    return b
