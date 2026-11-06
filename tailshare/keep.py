"""Keep squad members running (docs/start.md#keep-running): each host keeps its own members up.

The desired state is per member, on the member's host (start.db member_run): `keep` (the default) or `stopped`.
Only tailshare stops a member: Stop in the app, `tailshare agent stop`, or the app's Close of a member's session. A
/exit, a clean quit or a crash are deaths, and the member is started again.

The reconciler runs in the bus: when it starts, after a sleep, when a machine comes back, and every CHECK seconds.
For each member of the owner's squads that runs on this host and should run, it looks for its agent:

- running: a live agent with the member's tag, or one that went quiet but whose process still runs (a long tool
  call polls nothing); or a session started by hand that matches the member (host, kind, folder, name), which the
  bus tags as the member when it registers (squads.match_tag). Never a second copy next to one of those;
- starting: a window `tailshare start` made for it is there, and the agent has not registered yet;
- else down: it is started again, resuming its last session, or fresh in its folder under its name when the resume
  is refused or the resumed session did not stay up. Backoff DELAYS between tries, capped at 10 minutes, forever;
  after FLAG_AFTER tries in a row that did not stay up STABLE seconds, one "Needs you" blocker in its report with
  the last output, resolved once it stays up.

Never more than its start key allows now (as revive.py, tailshare#61): a member last started by a start key
restarts with that key's grant only while the key is still installed, and one started with skip only while its key
still allows skip; otherwise it is not restarted and the owner is told. A member never started by a key (by hand)
restarts with the safe grant.

The state each member is in is what /hello `keep` shows the owner's devices."""
from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import bus, dead

CHECK = 30
DELAYS = (0, 30, 60, 120, 240, 480, 600)  # before try n+1, after n tries that did not stay up
STABLE = 600  # up this long: the tries count again from 0
FLAG_AFTER = 5
STARTUP = 120  # a window started this recently counts as starting, not stuck
FLAG_ID = "keep"
RUNS = ("keep", "stopped")
OUT_BYTES = 32 * 1024
OUT_LINES = 40
DEVICE = "keep running"

_lock = threading.Lock()  # the reconciler and run changes, one at a time (both run in the bus)

_ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[@-Z\\-_])")


def delay(tries: int) -> int:
    return DELAYS[min(max(tries, 0), len(DELAYS) - 1)]


def tag_json(sid: str, mid: str) -> str:
    return json.dumps({"id": sid, "member": mid}, sort_keys=True)


# --------------------------------------------------------------------------- what runs here


def members_here(b) -> list[tuple[dict, dict]]:
    """(squad, member) for every live member of the owner's squads that runs on this host."""
    if b.squads is None:
        return []
    out = []
    for sid in b.squads.ids():
        sq = b.squads.squad(sid)
        for m in (sq or {}).get("members", []):
            if not m.get("deleted") and m["host"] == b.net.host:
                out.append((sq, m))
    return out


def managed(b) -> set[tuple[str, str]]:
    """The (squad, member) pairs this reconciler keeps: the revive watchdog leaves them to it."""
    return {(sq["id"], m["id"]) for sq, m in members_here(b)}


def matches(a, m: dict) -> bool:
    """Whether agents row `a` is a session of member `m` by what it is: kind, folder and name (as a slug)."""
    want = bus.slug(m.get("name") or "")
    return (bool(want) and (a["harness"] or "") == m["kind"] and bool(a["cwd"])
            and os.path.realpath(a["cwd"]) == os.path.realpath(m["folder"]) and bus.slug(a["session"] or "") == want)


def _rows(b, sql: str, *args) -> list:
    return b.store.q(sql, *args)


def holder(b, sq: dict, m: dict, now: float, windows=None) -> tuple[str, dict | None]:
    """("running" | "elsewhere" | "starting" | "down", the agents row it is or was, newest first)."""
    cut = now - dead.MAX_AGE
    tagged = _rows(b, "SELECT * FROM agents WHERE squad=? AND last_seen>? ORDER BY last_seen DESC",
                   tag_json(sq["id"], m["id"]), cut)
    for a in tagged[:4]:
        if dead.holds(a, now):
            return "running", a
    if tagged and not dead.ended(tagged[0]) and elsewhere(tagged[0], sq, m):
        return "elsewhere", tagged[0]  # ts-agents' pane is still there: it brings its agent back itself
    for a in _rows(b, "SELECT * FROM agents WHERE squad='' AND harness=? AND last_seen>? ORDER BY last_seen DESC",
                   m["kind"], now - bus.LIVE):
        if matches(a, m) and dead.holds(a, now):
            return "running", a  # started by hand: it becomes the member when it registers again
    if windows is not None and windows(sq, m):
        return "starting", tagged[0] if tagged else None
    return "down", tagged[0] if tagged else None


def elsewhere(a, sq: dict, m: dict) -> bool:
    """Whether the member's last session ran in a pane something else keeps (ts-agents: `tmux -L ts-<name>`, not
    one `tailshare start` made), and that pane is still there: never a second copy next to its restart."""
    from . import keys

    t = bus.clean_tmux(dead._json(a["tmux"]))
    if not t or not dead._managed_elsewhere(t, {"id": sq["id"], "member": m["id"]}):
        return False
    binary = keys.tmux_binary(t["bin"])
    return bool(binary) and keys.own_socket(t["socket"]) and keys.pane_alive(binary, t["socket"], t["pane"])


class _Windows:
    """The windows `tailshare start` has open, read once per look."""

    def __init__(self) -> None:
        self._seen: set | None = None
        self._servers: set | None = None

    def __call__(self, sq: dict, m: dict) -> bool:
        from . import start

        if self._seen is None:
            try:
                t = start._tmux_bin()
                self._seen = {(s, w) for _, s, w in start._windows(t)}
                self._servers = {os.path.basename(p) for p in start._servers() if p and start._sessions(t, p)}
            except (start.Refused, OSError):
                self._seen, self._servers = set(), set()
        return ((start.squad_session(sq["name"]), start.slug(m["name"])) in self._seen
                or (not start.TMUX_SOCKET and start.LABEL + start.slug(m["name"]) in (self._servers or set())))


# --------------------------------------------------------------------------- start.db


def _keep(con: sqlite3.Connection, sid: str, mid: str) -> dict:
    r = con.execute("SELECT * FROM member_keep WHERE squad=? AND member=?", (sid, mid)).fetchone()
    d = {"tries": 0, "next_at": 0.0, "last_error": "", "last_try": 0.0, "fresh": 0, "up_since": None, "flagged": 0,
         "state": "{}", "grant_": "", "key": ""}
    return {**d, **dict(r)} if r else d


def _save(con: sqlite3.Connection, sid: str, mid: str, k: dict, state: dict) -> None:
    con.execute("INSERT OR REPLACE INTO member_keep(squad, member, tries, next_at, last_error, last_try, fresh, "
                "up_since, flagged, state, grant_, key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, mid, k["tries"], k["next_at"], k["last_error"][:300], k["last_try"], k["fresh"], k["up_since"],
                 k["flagged"], json.dumps(state, sort_keys=True), k["grant_"], k["key"]))


def run_of(con: sqlite3.Connection, sid: str, mid: str) -> dict:
    r = con.execute("SELECT run, by, at FROM member_run WHERE squad=? AND member=?", (sid, mid)).fetchone()
    return dict(r) if r else {"run": "keep", "by": "", "at": 0.0}


def set_run(con: sqlite3.Connection, sid: str, mid: str, run: str, by: str, now: float | None = None) -> None:
    """The member's desired state, and a fresh count of tries when it should run again."""
    if run not in RUNS:
        raise ValueError(run)
    now = time.time() if now is None else now
    con.execute("INSERT OR REPLACE INTO member_run(squad, member, run, by, at) VALUES(?,?,?,?,?)",
                (sid, mid, run, by[:80], now))
    if run == "keep":
        con.execute("UPDATE member_keep SET tries=0, next_at=0, fresh=0 WHERE squad=? AND member=?", (sid, mid))


def _grant(b, con: sqlite3.Connection, k: dict, sessions: list[str]) -> tuple[str | None, str, str]:
    """(grant, key, why not): the member's last start's (a device's, or this reconciler's, which kept the one before
    it), else the newest of its sessions a start key launched here; never more than that key allows now."""
    from . import revive

    row = {"grant_": k["grant_"], "key": k["key"]} if k["grant_"] else None
    for s in sessions if row is None else []:
        row = con.execute("SELECT grant_, key FROM launched WHERE session=?", (s,)).fetchone()
        if row:
            break
    if not row or not row["key"]:
        return "safe", "", ""
    now = revive._key_grant(b, row["key"])
    if now is None:
        return None, row["key"], "gone"
    if row["grant_"] == "skip" and now != "skip":
        return None, row["key"], "narrowed"
    return ("skip" if row["grant_"] == "skip" else "safe"), row["key"], ""


def _resumable(sid: str, sq: dict, m: dict) -> bool:
    """A session the member may resume: listed as dead here, its own (tag, or by hand in its folder and name)."""
    r, live = dead.find(sid)
    if r is None or live or r["kind"] != m["kind"]:
        return False
    if os.path.realpath(r["cwd"]) != os.path.realpath(m["folder"]):
        return False
    tag = r.get("squad") or {}
    return tag == {"id": sq["id"], "member": m["id"]} or (not tag and bus.slug(r["name"]) == bus.slug(m["name"]))


# --------------------------------------------------------------------------- last output


def out_path(sid: str, mid: str) -> Path:
    return bus.STATE_DIR / "keep" / f"{sid}.{mid}.out"


def _capture(res: dict, sid: str, mid: str) -> None:
    """Keep the last OUT_BYTES the new pane prints, written when it closes (tail -c on a pipe waits for its end)."""
    t = res.get("tmux") or {}
    p = out_path(sid, mid)
    try:
        p.parent.mkdir(mode=0o700, exist_ok=True)
        p.unlink(missing_ok=True)
        subprocess.run([t["bin"], "-S", t["socket"], "pipe-pane", "-t", t["pane"],
                        f"exec tail -c {OUT_BYTES} > {shlex.quote(str(p))}"],
                       capture_output=True, timeout=5, stdin=subprocess.DEVNULL, check=False)
    except (OSError, KeyError, subprocess.SubprocessError):
        pass


def last_output(sid: str, mid: str, lines: int = OUT_LINES) -> str:
    """The last lines the member's last pane printed, without terminal escapes; '' if none were kept."""
    try:
        raw = out_path(sid, mid).read_bytes()[-OUT_BYTES:]
    except OSError:
        return ""
    text = _ANSI.sub("", raw.decode("utf-8", "replace")).replace("\r\n", "\n").replace("\r", "\n")
    keep = []
    for line in text.split("\n"):
        line = "".join(c for c in line if c == "\t" or c.isprintable()).rstrip()
        if line.strip():
            keep.append(line)
    return "\n".join(keep[-lines:])


# --------------------------------------------------------------------------- the owner's board


def _flag(b, sq: dict, m: dict, title: str, why: str) -> None:
    if b.reports is None:
        return
    try:
        if not b.reports.flag(sq["id"], m["id"], {"id": FLAG_ID, "kind": "question", "to": "owner",
                                                   "title": title[:80], "why": why[-600:]}):
            bus.log(f"keep: {m['name']} not flagged: too many open blockers")
    except Exception as e:  # noqa: BLE001
        bus.log(f"keep: cannot flag {m['name']}: {e!r}")


def _unflag(b, sq: dict, m: dict) -> None:
    if b.reports is None or not hasattr(b.reports, "unflag"):
        return
    try:
        b.reports.unflag(sq["id"], m["id"], FLAG_ID)
    except Exception as e:  # noqa: BLE001
        bus.log(f"keep: cannot clear {m['name']}'s flag: {e!r}")


# --------------------------------------------------------------------------- the reconciler


def _migrate(b, con: sqlite3.Connection, here: list, now: float, windows) -> None:
    """The first look on a host: members that are not running then were not kept running before this version, so
    they stay stopped (by "upgrade") instead of all starting at once; the owner starts them from the app."""
    if con.execute("SELECT 1 FROM keep_meta WHERE k='migrated'").fetchone():
        return
    for sq, m in here:
        if con.execute("SELECT 1 FROM member_run WHERE squad=? AND member=?", (sq["id"], m["id"])).fetchone():
            continue
        state, _ = holder(b, sq, m, now, windows)
        if state == "down":
            set_run(con, sq["id"], m["id"], "stopped", "upgrade", now)
    con.execute("INSERT OR REPLACE INTO keep_meta(k, v) VALUES('migrated', ?)", (str(now),))


def tick(b, now: float | None = None) -> list[dict]:
    """One look at every member here; each one's state, as /hello shows it."""
    from . import start

    now = time.time() if now is None else now
    with _lock:
        here = members_here(b)
        try:
            cfg = start.load_config()
        except start.Refused:
            cfg = None
        con = start._db()
        try:
            windows = _Windows()
            _migrate(b, con, here, now, windows)
            out = []
            for sq, m in here:
                try:
                    out.append(_one(b, con, cfg, sq, m, now, windows))
                except Exception as e:  # noqa: BLE001
                    bus.log(f"keep {m['name']}: {e!r}")
            mine = {(sq["id"], m["id"]) for sq, m in here}
            for r in con.execute("SELECT squad, member FROM member_keep").fetchall():
                if (r["squad"], r["member"]) not in mine:  # removed, or moved to another host
                    con.execute("DELETE FROM member_keep WHERE squad=? AND member=?", (r["squad"], r["member"]))
            return out
        finally:
            con.close()


def _one(b, con: sqlite3.Connection, cfg: dict | None, sq: dict, m: dict, now: float, windows) -> dict:
    from . import start

    sid, mid = sq["id"], m["id"]
    head = {"squad": sid, "member": mid, "name": m["name"]}
    run = run_of(con, sid, mid)
    k = _keep(con, sid, mid)
    state, a = holder(b, sq, m, now, windows)

    def done(st: dict) -> dict:
        st = {**head, "run": run["run"], **st}
        _save(con, sid, mid, k, st)
        return st

    if run["run"] == "stopped":
        k.update(tries=0, next_at=0.0, up_since=None)
        return done({"state": "stopped", "by": run["by"], "at": run["at"], **({"still_running": True}
                                                                            if state == "running" else {})})
    if state == "elsewhere":
        k.update(tries=0, next_at=0.0, up_since=None)
        return done({"state": "running", "managed_by": "ts-agents", **({"address": a["address"]} if a else {})})
    if state == "running":
        k["up_since"] = k["up_since"] or now
        if now - k["up_since"] >= STABLE and (k["tries"] or k["flagged"] or k["fresh"]):
            k.update(tries=0, next_at=0.0, fresh=0, last_error="")
            if k["flagged"]:
                _unflag(b, sq, m)
                k["flagged"] = 0
        return done({"state": "running", "since": k["up_since"], **({"address": a["address"]} if a else {})})
    if state == "starting" and now - k["last_try"] < STARTUP:
        return done({"state": "starting", "since": k["last_try"]})
    k["up_since"] = None
    if not cfg or not cfg["roots"] or not cfg["kinds"]:
        return done({"state": "blocked", "why": "off", "last_error": "starting sessions is off on this host"})
    if k["tries"] >= FLAG_AFTER and not k["flagged"]:
        out = last_output(sid, mid)
        _flag(b, sq, m, f"{m['name']} keeps stopping: {k['tries']} restarts did not stay up",
              (f"Last error: {k['last_error']}\n" if k["last_error"] else "") +
              (f"Last output:\n{out}" if out else "No output was kept.") +
              "\nTailshare keeps trying every 10 minutes.")
        k["flagged"] = 1
    if k["next_at"] > now:
        return done({"state": "restarting", "attempt": k["tries"] + 1, "next_at": k["next_at"],
                     "last_error": k["last_error"], **({"needs_you": True} if k["flagged"] else {})})
    if state == "starting":  # its window is there but it never registered: leave it, it may wait on a prompt
        k["next_at"] = now + delay(k["tries"] + 1)
        return done({"state": "starting", "since": k["last_try"], "stuck": True})
    sessions = [s for s in (dead.session_id(r["harness"] or "", r["id"]) for r in _rows(
        b, "SELECT id, harness FROM agents WHERE squad=? ORDER BY last_seen DESC LIMIT 8", tag_json(sid, mid))) if s]
    grant, key, gone = _grant(b, con, k, sessions)
    if grant is None:
        why = (f"its start key ({key}) was removed from this host" if gone == "gone"
               else f"it was started with skip, and its start key ({key}) no longer allows it")
        if not k["flagged"]:
            _flag(b, sq, m, f"{m['name']} stopped: not restarted, its start key "
                  + ("is gone" if gone == "gone" else "no longer allows skip"), why + ". Start it from the app.")
            k["flagged"] = 1
        k["last_error"] = why
        return done({"state": "blocked", "why": "key", "last_error": why})
    resume = sessions[0] if sessions and not k["fresh"] and _resumable(sessions[0], sq, m) else ""
    item = {"squad": {"id": sid, "member": mid}, "device_name": DEVICE,
            **({"_resume": resume} if resume else {"_session": str(uuid.uuid4())})}
    tries = k["tries"] + 1
    try:
        plan = start.build(item, cfg, grant, b.net.host)
        res = start.launch(plan, cfg["max_running"])
    except start.Refused as e:
        err = e.out.get("error", "failed")
        if err == "running":  # a copy is there already: never a second one
            k["last_try"] = k["last_try"] or now
            return done({"state": "starting", "since": k["last_try"]})
        k.update(tries=tries, next_at=now + delay(tries), last_try=now,
                 last_error=f"{err}: {e.out.get('reason', '')}".rstrip(": "), fresh=1 if resume else k["fresh"])
        bus.log(f"keep {m['name']}: {'resume' if resume else 'start'} refused {k['last_error']} (try {tries})")
        return done({"state": "restarting", "attempt": tries + 1, "next_at": k["next_at"],
                     "last_error": k["last_error"]})
    start.record_launch(con, plan, res, key)
    k.update(grant_=grant, key=key)
    con.execute("INSERT OR REPLACE INTO member_starts(squad, member, instructions_sha256, at) VALUES(?,?,?,?)",
                (sid, mid, plan["instructions_sha256"], now))
    _capture(res, sid, mid)
    # a resume that does not stay up is not tried again: the next start is a fresh one
    k.update(tries=tries, next_at=now + delay(tries), last_try=now, last_error="", fresh=1 if resume else k["fresh"])
    bus.log(f"keep {m['name']}: {'resumed ' + resume[:8] if resume else 'started fresh'} in {res['tmux']['pane']} "
            f"(try {tries})")
    return done({"state": "starting", "since": now, "attempt": tries, "resumed": bool(resume)})


# --------------------------------------------------------------------------- changing it


def change(b, sid: str, mid: str, run: str, by: str) -> dict:
    """Stop, keep, or restart a member of this host (the chain or the owner was checked before)."""
    from . import start

    if run not in ("keep", "stopped", "restart"):
        raise bus.BusError("bad_request", 400, {"field": "run", "reason": "keep, stopped or restart"})
    sq = b.squads.squad(sid) if b.squads else None
    m = next((x for x in (sq or {}).get("members", []) if x["id"] == mid), None)
    if m is None:
        raise bus.BusError("no such member", 404)
    if m["host"] != b.net.host:
        raise bus.BusError("this member runs on another machine", 409, {"host": m["host"]})
    now = time.time()
    with _lock:
        con = start._db()
        try:
            set_run(con, sid, mid, "stopped" if run == "stopped" else "keep", by, now)
            if run != "stopped":
                k = _keep(con, sid, mid)
                if k["flagged"]:
                    _unflag(b, sq, m)
                    k["flagged"] = 0
                k.update(tries=0, next_at=0.0, fresh=0, last_error="")
                _save(con, sid, mid, k, json.loads(k["state"] or "{}"))
        finally:
            con.close()
        state, a = holder(b, sq, m, now)
    out = {"squad": sid, "member": mid, "name": m["name"], "run": "stopped" if run == "stopped" else "keep", "by": by}
    if run in ("stopped", "restart") and state == "running" and a is not None:
        s = dead.session_id(a["harness"] or "", a["id"])
        closed = start.close_started(s, stop=False) if s else {"error": "not_started"}
        out["closed"] = bool(closed.get("closed"))
        if not out["closed"]:
            out["reason"] = ("it was not started by tailshare: end it in its own terminal (/exit)"
                             + ("; tailshare will not start it again" if run == "stopped" else
                                ", and tailshare starts it again") if closed.get("error") == "not_started"
                             else str(closed.get("reason") or closed.get("error") or "not closed"))
    b.keep_due = True
    b.kick.set()
    return out


def stopped_by_close(sid: str, mid: str) -> None:
    """The app's Close of a member's session is its Stop (start.close_started)."""
    from . import start

    con = start._db()
    try:
        set_run(con, sid, mid, "stopped", "a device (Close)")
    finally:
        con.close()


def kept_by_start(con: sqlite3.Connection, sid: str, mid: str, device: str, grant: str, key: str) -> None:
    """A device starting a member means: keep it running (start._confirm_one), restarted with that start's key."""
    set_run(con, sid, mid, "keep", f"a device ({device})" if device else "a device")
    con.execute("INSERT INTO member_keep(squad, member, grant_, key, last_try) VALUES(?,?,?,?,?) "
                "ON CONFLICT(squad, member) DO UPDATE SET grant_=excluded.grant_, key=excluded.key, tries=0, "
                "next_at=0, fresh=0, last_error='', last_try=excluded.last_try", (sid, mid, grant, key, time.time()))


# --------------------------------------------------------------------------- who may


def may_change(sq: dict, caller: str, target: str) -> bool:
    """A member's manager chain: the members above it in its squad's tree (never itself)."""
    from . import squads

    return target != caller and target in squads.branch(sq.get("members", []), caller)


def find_member(b, ref: str, squad: str = "", within: dict | None = None) -> tuple[dict, dict]:
    """(squad, member) by member id or name (case-insensitive), in `within`, or the owner's squads (one squad
    named or id'd by `squad`, else exactly one member by that name)."""
    ref = str(ref or "").strip()
    if not ref:
        raise bus.BusError("bad_request", 400, {"field": "member"})
    sqs = [within] if within else [s for s in (b.squads.squad(i) for i in b.squads.ids()) if s]
    if squad and not within:
        sqs = [s for s in sqs if s["id"] == squad or s["name"].lower() == squad.lower()]
    hits = [(s, m) for s in sqs for m in s.get("members", []) if not m.get("deleted")
            and (m["id"] == ref or m["name"].lower() == ref.lower())]
    if len(hits) != 1:
        raise bus.BusError("no such member" if not hits else "more than one member by that name: name the squad",
                           404 if not hits else 409, {"field": "member"})
    return hits[0]


def route_local(b, body: dict, token: str) -> dict:
    """POST /bus/v1/member/run (local API): {agent, member, squad?, run}. A squad member changes the members under
    it; a session that is no member needs the owner's ~/.tailshare/keys.token, as squad edits do."""
    import secrets

    run = str(body.get("run") or "")
    aid = str(body.get("agent") or "")[:100]
    row = b.store.one("SELECT * FROM agents WHERE id=?", aid) if aid else None
    tag = json.loads(row["squad"]) if row and row["squad"] and dead.is_live(row) else {}
    if tag:
        csq = b.squads.squad(tag["id"])
        caller = next((x for x in (csq or {}).get("members", []) if x["id"] == tag["member"]), None)
        if caller is None:
            raise bus.BusError("not_a_member", 409)
        sq, m = find_member(b, body.get("member"), within=csq)
        if not may_change(sq, caller["id"], m["id"]):
            raise bus.BusError("not_allowed", 403, {"reason": f"only {m['name']}'s leads (or the owner) may change "
                                                              "whether it runs"})
        by = caller["name"]
        if m["host"] != b.net.host:
            return forward(b, m["host"], {"v": 1, "caller": {"squad": sq["id"], "member": caller["id"]},
                                          "squad": sq["id"], "member": m["id"], "run": run})
    else:
        if b.keys is None or len(b.keys.token) < 64 or not secrets.compare_digest(token.encode(),
                                                                                   b.keys.token.encode()):
            raise bus.BusError("not_allowed", 403, {"reason": "this session is no squad member: only the owner may, "
                                                              "with ~/.tailshare/keys.token"})
        sq, m = find_member(b, body.get("member"), str(body.get("squad") or ""))
        by = "owner"
        if m["host"] != b.net.host:
            raise bus.BusError("this member runs on another machine: run it there, or use the app", 409,
                               {"host": m["host"]})
    return change(b, sq["id"], m["id"], run, by)


def forward(b, host: str, doc: dict) -> dict:
    p = b.net.peers.get(host)
    if not p or not p.get("online"):
        raise bus.BusError(f"{host} is offline", 503, {"host": host})
    return bus.request(f"http://{p['ip']}:{p.get('port') or b.port}/bus/v1/member/run", doc, timeout=20)


def route_peer(b, ip: str, body: dict) -> dict:
    """POST /bus/v1/member/run from another of the owner's machines, for a lead that runs there: that machine must
    be where the lead runs (by this host's copy of the squad), and the lead above the member."""
    b.require_same_user(ip)
    if body.get("v") != 1 or not isinstance(body.get("caller"), dict):
        raise bus.BusError("bad_request", 400)
    who = b.net.host_for_ip(ip)
    sq = b.squads.squad(str(body.get("squad") or "")) if b.squads else None
    if sq is None or body["caller"].get("squad") != sq["id"]:
        raise bus.BusError("no such squad", 404)
    caller = next((x for x in sq["members"] if x["id"] == body["caller"].get("member")), None)
    if caller is None or caller["host"] != who:
        raise bus.BusError("not_allowed", 403, {"reason": "the lead does not run on the machine that asked"})
    m = next((x for x in sq["members"] if x["id"] == body.get("member")), None)
    if m is None or not may_change(sq, caller["id"], m["id"]):
        raise bus.BusError("not_allowed", 403, {"reason": "only a member's leads may change whether it runs"})
    return change(b, sq["id"], m["id"], str(body.get("run") or ""), caller["name"])


# --------------------------------------------------------------------------- /hello


def hello() -> list[dict]:
    """/hello `keep` (the owner's devices): each member's last state here, as the reconciler saw it."""
    from . import start

    try:
        con = sqlite3.connect(f"file:{start.db_path()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        con.row_factory = sqlite3.Row
        out = []
        for r in con.execute("SELECT state FROM member_keep ORDER BY squad, member"):
            try:
                st = json.loads(r["state"] or "{}")
            except ValueError:
                continue
            if st:
                out.append(st)
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def offline(b) -> list[dict]:
    """The owner's members on machines the tailnet shows offline: nothing there can keep them running."""
    out = []
    for sid in b.squads.ids() if b.squads else []:
        for m in (b.squads.squad(sid) or {}).get("members", []):
            p = b.net.peers.get(m["host"])
            if m.get("deleted") or m["host"] == b.net.host or not p or p.get("online"):
                continue
            out.append({"squad": sid, "member": m["id"], "name": m["name"], "state": "host_offline",
                        "host": m["host"], **({"since": p["last_seen"]} if p.get("last_seen") else {})})
    return out

