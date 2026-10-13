"""Agent message bus: a mailbox per machine, store-and-forward between tailnet peers.

Every machine's share server also runs a mailbox for the agents on that machine:

  127.0.0.1:BUS_PORT      local API for agents   (register, send, inbox, ack, agents)
  <tailnet ip>:BUS_PORT   peer API for mailboxes (hello, deliver)

There is no hub. Addresses are ``host/harness/folder[/session]``, so the host part says
which mailbox to deliver to. Messages for another machine wait in an outbox and are
retried until that machine takes them, so either side can sleep, reboot or go offline.
The host part of a sender's address is checked against the tailnet IP it connects from,
so it cannot be forged.
"""

from __future__ import annotations

import json
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

from . import __version__, core

BUS_PORT = int(os.environ.get("TAILSHARE_BUS_PORT", "8789"))
STATE_DIR = Path(os.environ.get("TAILSHARE_STATE", "~/.tailshare")).expanduser()
PROTOCOL = 1
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


def request(url: str, payload: dict | None = None, timeout: float = 5.0) -> dict:
    """GET (payload None) or POST JSON to a bus endpoint. Raises BusError on HTTP errors."""
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, method="GET" if data is None else "POST",
        headers={HEADER: "1", "Content-Type": "application/json"},
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


def is_pattern(target: str) -> bool:
    return any(c in target for c in "*?[")


def matches(address: str, target: str) -> bool:
    """True if `target` names `address`: exactly, as a glob, or as a prefix (host, host/harness, …/folder).

    A collision suffix (``:6d2b``) is optional when matching.
    """
    base = address.split(":")[0]
    return any(fnmatchcase(a, t) for a in (address, base) for t in (target, target + "/*"))


# --------------------------------------------------------------------------- tailnet view


@dataclass
class Net:
    """This machine's tailnet name and IP, and the other machines, from `tailscale status`."""

    host: str = ""
    ip: str = ""
    peers: dict[str, dict] = field(default_factory=dict)  # host -> {"ip", "port", "online"}
    updated: float = 0.0

    def host_for_ip(self, ip: str) -> str | None:
        if ip == self.ip:
            return self.host
        return next((h for h, p in self.peers.items() if p["ip"] == ip), None)

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

        me = d.get("Self") or {}
        self.host = name(me)
        self.ip = next(iter(me.get("TailscaleIPs") or []), "")
        peers = {}
        for p in (d.get("Peer") or {}).values():
            ips = p.get("TailscaleIPs") or []
            if ips:
                peers[name(p)] = {"ip": ips[0], "port": BUS_PORT, "online": bool(p.get("Online"))}
        self.peers = peers


# --------------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, address TEXT NOT NULL, harness TEXT, cwd TEXT, session TEXT,
  pid INTEGER, first_seen REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, sender TEXT NOT NULL, target TEXT NOT NULL, body TEXT NOT NULL,
  files TEXT NOT NULL DEFAULT '[]', created REAL NOT NULL, received REAL NOT NULL);
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
        "files": json.loads(r["files"]), "created": r["created"],
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
    )
    if not ok:
        raise BusError("malformed message")
    return m


# --------------------------------------------------------------------------- the mailbox


class Bus:
    def __init__(self, store: Store, net: Net, port: int = BUS_PORT) -> None:
        self.store, self.net, self.port = store, net, port
        self.kick = threading.Event()  # wakes the delivery loop
        self._peer_server: ThreadingHTTPServer | None = None
        self._peer_ip = ""
        self._pruned = 0.0

    # -- agents ---------------------------------------------------------------
    def register(self, a: dict) -> dict:
        aid = str(a.get("id") or "").strip()[:100]
        if not aid:
            raise BusError("agent id required")
        harness = slug(a.get("harness") or "cli")
        cwd = str(a.get("cwd") or "")
        session = str(a.get("session") or "").strip()
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
                "INSERT INTO agents(id, address, harness, cwd, session, pid, first_seen, last_seen)"
                " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET address=excluded.address,"
                " harness=excluded.harness, cwd=excluded.cwd, session=excluded.session,"
                " pid=excluded.pid, last_seen=excluded.last_seen",
                aid, address, harness, cwd, session, int(a.get("pid") or 0), now, now,
            )
            self._claim(aid, address)
        return self.agent(aid)

    def _claim(self, aid: str, address: str) -> None:
        """Give an agent the messages that were waiting for its address."""
        pending = self.store.q(
            "SELECT inbox.rowid AS rid, m.target FROM inbox JOIN messages m ON m.id=inbox.msg WHERE inbox.agent IS NULL"
        )
        hit = [r["rid"] for r in pending if matches(address, r["target"])]
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
            "id": r["id"], "address": r["address"], "harness": r["harness"], "session": r["session"],
            "cwd": r["cwd"], "pid": r["pid"], "last_seen": r["last_seen"],
            "online": r["last_seen"] > time.time() - LIVE, "host": self.net.host,
        }

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
                a.update(host=p["host"], online=bool(a.get("online")) and up, host_online=up)
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
        host_pat = target.split("/")[0]
        if is_pattern(host_pat):
            known = {r["host"] for r in self.store.q("SELECT host FROM peers WHERE last_ok IS NOT NULL")}
            hosts = sorted(h for h in known | {self.net.host} if fnmatchcase(h, host_pat))
        elif host_pat == self.net.host or host_pat in self.net.peers:
            hosts = [host_pat]
        else:
            raise BusError(f"no machine named {host_pat!r} on the tailnet")
        m = {
            "id": uuid.uuid4().hex, "sender": me["address"], "target": target, "body": body,
            "files": list(files or []), "created": time.time(),
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
                "INSERT OR IGNORE INTO messages(id, sender, target, body, files, created, received) VALUES(?,?,?,?,?,?,?)",
                m["id"], m["sender"], m["target"], m["body"], json.dumps(m.get("files", [])), m["created"], now,
            )
            if n == 0:
                return False
            agents = self.store.q("SELECT id, address FROM agents WHERE last_seen>?", now - KNOWN)
            hits = [a["id"] for a in agents if a["address"] != m["sender"] and matches(a["address"], m["target"])]
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
    def hello(self) -> dict:
        agents = [
            {k: a[k] for k in ("address", "harness", "session", "online", "last_seen")}
            for a in self.local_agents()
        ]
        return {"v": PROTOCOL, "version": __version__, "host": self.net.host, "agents": agents}

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
                "INSERT INTO peers(host, ip, version, last_ok, agents) VALUES(?,?,?,?,?)"
                " ON CONFLICT(host) DO UPDATE SET ip=excluded.ip, version=excluded.version,"
                " last_ok=excluded.last_ok, agents=excluded.agents",
                h, p["ip"], str(d.get("version", "")), time.time(), json.dumps(d.get("agents", [])),
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
            if not local:
                if (method, path) == ("GET", "/hello"):
                    return self._reply(200, b.hello())
                if (method, path) == ("POST", "/deliver"):
                    return self._reply(200, b.accept(ip, body))
            elif (method, path) == ("POST", "/register"):
                return self._reply(200, b.register(body))
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
        return b
    except Exception as e:  # noqa: BLE001
        log(f"not started: {e!r}")
        return None
