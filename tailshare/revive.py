"""The revive watchdog (docs/start.md#revive): in the bus, reopen a session `tailshare start` started here whose pane
died while the session had not ended (tmux crashed, the pane or its server was killed), with a start that resumes it.

Only what a device could reopen by hand: a row in the dead list (dead.rows: ours, gone) that is resumable under the
host's start.json now, re-checked by start.build like any start. A session that ended (SessionEnd, or Close) is never
revived. Bounded, so a crash loop ends up with the owner instead of looping:

- backoff: a session's 2nd revive in an hour waits BACKOFF[0] after its 1st, then BACKOFF[1], BACKOFF[2];
- at most `revive_per_session` an hour (a death within QUICK of its revive counts twice), and `revive_per_host`
  an hour on the host (one event per tmux server, so a whole server dying counts once);
- past a cap: one "Needs you" blocker in a squad member's report (or a log line for a single session), at most once
  an hour each.

Each revive, refusal and stop is a dated `revive` line in tailshare.log."""
from __future__ import annotations

import json
import sqlite3
import time

from . import bus, dead

CHECK = 30  # seconds between looks
BACKOFF = (30, 120, 600)
QUICK = 120  # dying this soon after a revive counts twice
HOUR = 3600
FRESH = 15 * 60  # only a pane that died this recently: never one from before the bus was up for days
FLAG_ID = "revive"


def _states(ids: list[str], db=None) -> dict[str, dict]:
    """{session id: {"ended", "socket", "last_seen"}} from bus.db, read-only."""
    db = db or bus.STATE_DIR / "bus.db"
    out: dict[str, dict] = {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return out
    try:
        con.row_factory = sqlite3.Row
        for sid in ids:
            for a in con.execute("SELECT id, harness, tmux, state, last_seen FROM agents WHERE lower(id)=?", (sid,)):
                if dead.session_id(a["harness"] or "", a["id"]) != sid:
                    continue
                st = dead._json(a["state"])
                t = bus.clean_tmux(dead._json(a["tmux"]))
                out[sid] = {"ended": isinstance(st, dict) and st.get("v") == "ended",
                            "socket": t.get("socket", ""), "last_seen": float(a["last_seen"])}
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def _key_grant(b, key: str) -> str | None:
    """What start key `key` may do here now: "skip", "safe", or None once it was removed or is no start key."""
    try:
        if b.store.one("SELECT 1 FROM revoked_keys WHERE id=?", key):
            return None
        k = b.store.one("SELECT mode FROM ssh_keys WHERE id=?", key)
    except sqlite3.Error:  # no keys tables: no key was ever installed here
        return None
    return {"start-skip": "skip", "start": "safe"}.get(k["mode"]) if k else None


def _item(b, con: sqlite3.Connection, r: dict) -> tuple[dict, str | None, str, str]:
    """The start that resumes dead row `r`, with the options it was started with here (start.db `launched`); the
    grant to build it with, never more than the start key that approved it may do now; that key; and, when the
    grant is None (not reopened), why: the key is gone, or it was started with skip and its key no longer allows
    skip (reopening it without skip would not be the same session, and would only be refused). One started before
    launches were recorded: only resume, and the safe grant."""
    row = con.execute("SELECT options, grant_, key FROM launched WHERE session=?", (r["id"],)).fetchone()
    opts = json.loads(row["options"]) if row else {}
    opts = {k: v for k, v in opts.items() if k != "resume"} if isinstance(opts, dict) else {}
    item = {"kind": r["kind"], "cwd": r["cwd"], "name": r["name"], "options": {**opts, "resume": r["id"]},
            "device_name": "revive"}
    if not row or not row["key"]:
        return item, "safe", "", ""
    now = _key_grant(b, row["key"])
    if now is None:
        return item, None, row["key"], "gone"
    if row["grant_"] == "skip" and now != "skip":
        return item, None, row["key"], "narrowed"
    return item, row["grant_"] if row["grant_"] in ("skip", "safe") else "safe", row["key"], ""


def _flag(b, con: sqlite3.Connection, key: str, r: dict | None, why: str, now: float, title: str = "") -> None:
    """Tell the owner, once an hour per `key`: a blocker in the member's report, or only the log for a single
    session (it has no board)."""
    last = con.execute("SELECT at FROM revive_flags WHERE key=?", (key,)).fetchone()
    if last and last["at"] > now - HOUR:
        return
    con.execute("INSERT OR REPLACE INTO revive_flags(key, at) VALUES(?,?)", (key, now))
    bus.log(f"revive stopped {key}: {why}")
    if not r or not r.get("squad") or b.reports is None:
        return
    title = title or f"{r['name']} keeps crashing: not reopened again"
    try:
        if not b.reports.flag(r["squad"]["id"], r["squad"]["member"],
                                 {"id": FLAG_ID, "kind": "question", "to": "owner", "title": title[:80],
                                  "why": f"{why}. Reopen it from the app once you have looked."}):
            bus.log(f"revive: {key} not flagged: {r['name']} has too many open blockers")
    except Exception as e:  # noqa: BLE001
        bus.log(f"revive: cannot flag {key}: {e!r}")


def tick(b, now: float | None = None) -> list[dict]:
    """One look; what it did, for tests and the log."""
    from . import start

    now = time.time() if now is None else now
    try:
        cfg = start.load_config()
    except start.Refused:
        return []
    if not cfg or not cfg["revive"] or not cfg["roots"] or not cfg["kinds"]:
        return []
    listed = [r for r in dead.listing(b.net.host)["sessions"] if r["resumable"] and r["last_seen"] > now - FRESH]
    if not listed:
        return []
    states = _states([r["id"] for r in listed])
    out: list[dict] = []
    con = start._db()
    try:
        cut = now - HOUR
        servers = set()  # tmux servers counted toward the host's cap in this look
        for r in listed:
            st = states.get(r["id"])
            if not st or st["ended"]:
                continue  # ended on purpose
            sid = r["id"]
            past = con.execute("SELECT at, weight FROM revives WHERE session=? AND at>? ORDER BY at", (sid, cut)).fetchall()
            if past and past[-1]["at"] + BACKOFF[min(len(past), len(BACKOFF)) - 1] > now:
                continue
            item, grant, key, gone = _item(b, con, r)
            if grant is None:
                _flag(b, con, sid, r, f"the start key that opened it ({key}) was removed from this host"
                      if gone == "gone" else f"it was started with skip, and its start key ({key}) no longer allows it",
                      now, f"{r['name']} crashed: not reopened, its start key " +
                      ("is gone" if gone == "gone" else "no longer allows skip"))
                out.append({"id": sid, "stopped": "key"})
                continue
            # dying soon after the last revive (or never coming up: last_seen before it) counts twice
            weight = 2 if past and st["last_seen"] - past[-1]["at"] < QUICK else 1
            used = sum(p["weight"] for p in past)
            if used + weight > cfg["revive_per_session"]:
                _flag(b, con, sid, r, f"{used} reopens of this session in the last hour "
                                      f"(revive_per_session {cfg['revive_per_session']})", now)
                out.append({"id": sid, "stopped": "session"})
                continue
            event = st["socket"] not in servers
            host = con.execute("SELECT COUNT(*) FROM revives WHERE host_event=1 AND at>?", (cut,)).fetchone()[0]
            if event and host >= cfg["revive_per_host"]:
                _flag(b, con, "host", r, f"{host} reopens on this host in the last hour "
                                         f"(revive_per_host {cfg['revive_per_host']})", now,
                      f"Sessions on {b.net.host} keep crashing: reopening paused ({r['name']} waits)")
                out.append({"id": sid, "stopped": "host"})
                continue
            servers.add(st["socket"])
            con.execute("INSERT INTO revives(session, socket, at, weight, host_event) VALUES(?,?,?,?,?)",
                        (sid, st["socket"], now, weight, int(event)))
            try:
                plan = start.build(item, cfg, grant, b.net.host)
                res = start.launch(plan, cfg["max_running"])
            except start.Refused as e:
                bus.log(f"revive {sid[:8]} {r['name']}: refused {e.out.get('error')} {e.out.get('reason', '')}".rstrip())
                out.append({"id": sid, "refused": e.out})
                continue
            except Exception as e:  # noqa: BLE001
                bus.log(f"revive {sid[:8]} {r['name']}: {e!r}")
                out.append({"id": sid, "refused": {"error": "failed"}})
                continue
            start.record_launch(con, plan, res, key)
            bus.log(f"revive {sid[:8]} {r['name']}: reopened in {res['tmux']['pane']} "
                    f"(#{len(past) + 1} this hour, weight {weight})")
            out.append({"id": sid, "revived": res})
    finally:
        con.close()
    return out
