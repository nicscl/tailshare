"""Dead sessions (docs/start.md#reopen): agent sessions this machine ran lately whose pane is gone, so the owner's
devices can offer to reopen them with a start that resumes the session (options.resume).

Read straight from bus.db, read-only, both in the bus (GET /bus/v1/sessions/dead) and in the start gate (which checks
a resume against the same list). Nothing here starts anything."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from . import bus

MAX_AGE = 7 * 86400  # agents seen this recently
LIMIT = 50  # a page, by default
LIMIT_MAX = 200
NAME_MAX = 256
TITLE_MAX = NAME_MAX  # the title is the name; devices cut it for display
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
STARTED = ("tailshare-start", "squad-")  # the tmux sessions `tailshare start` puts agents in (start.py)


def session_id(harness: str, aid: str) -> str:
    """The id a harness resumes an agent's session by: Claude's session uuid, Codex's thread id; else ''."""
    return aid.lower() if harness in ("claude", "codex") and _UUID.fullmatch(aid or "") else ""


def _managed_elsewhere(t: dict) -> bool:
    """A pane on a per-agent server (`tmux -L ts-<name>`) that `tailshare start` did not make: ts-agents and the
    like run those and bring their agents back themselves, so the list never offers them."""
    from .start import LABEL

    name = Path(t.get("socket") or "").name
    session = (t.get("target") or "").rsplit(":", 1)[0]
    return name.startswith(LABEL) and not session.startswith(STARTED)


def is_live(a, now: float | None = None) -> bool:
    """An agent seen within bus.LIVE that has not ended: a closed session (SessionEnd, or start.close_started) is
    not live even while its last poll is recent."""
    if a["last_seen"] <= (time.time() if now is None else now) - bus.LIVE:
        return False
    st = _json(a["state"] if "state" in a.keys() else "")
    return not (isinstance(st, dict) and st.get("v") == "ended")


def _pane_gone(t: dict, cache: dict) -> bool:
    from . import keys

    k = (t["socket"], t["pane"])
    if k not in cache:
        binary = keys.tmux_binary(t.get("bin") or "")
        cache[k] = not (binary and keys.own_socket(k[0]) and keys.pane_alive(binary, *k))
    return cache[k]


def rows(db: Path | None = None, now: float | None = None, before: tuple[float, str] | None = None,
         limit: int | None = LIMIT) -> list[dict]:
    """The dead sessions, newest first (ties by id, descending), after `before` (a page's last (last_seen, id)) in
    that order, at most `limit` (None: all), with nothing about whether this host may reopen them."""
    db = db or bus.STATE_DIR / "bus.db"
    now = time.time() if now is None else now
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        con.row_factory = sqlite3.Row
        agents = con.execute("SELECT * FROM agents WHERE last_seen>? ORDER BY last_seen DESC", (now - MAX_AGE,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    live = [a for a in agents if is_live(a, now)]
    held = {(t["socket"], t["pane"]): a["last_seen"] for a in live if (t := bus.clean_tmux(_json(a["tmux"])))}
    out, cache = [], {}
    agents.sort(key=lambda a: (round(float(a["last_seen"]), 3), (a["id"] or "").lower()), reverse=True)
    for a in agents:
        sid, t = session_id(a["harness"] or "", a["id"]), bus.clean_tmux(_json(a["tmux"]))
        if not sid or not t or is_live(a, now) or _managed_elsewhere(t):
            continue
        if before is not None and (round(float(a["last_seen"]), 3), sid) >= before:
            continue
        # gone, or a newer live agent sits in that pane now (pane ids start again at %0 on a new server)
        if not (_pane_gone(t, cache) or held.get((t["socket"], t["pane"]), 0) > a["last_seen"]):
            continue
        name = _clean(a["session"] or "", NAME_MAX)
        squad = bus.clean_squad_tag(_json(a["squad"] if "squad" in a.keys() else ""))
        out.append({"id": sid, "kind": a["harness"], "address": a["address"], "name": name,
                    "folder": Path(a["cwd"] or "").name, "cwd": a["cwd"] or "", "title": _clean(name, TITLE_MAX),
                    "squad": squad or None, "last_seen": round(float(a["last_seen"]), 3)})
        if limit is not None and len(out) >= limit:
            break
    return out


def resumable(r: dict, cfg: dict | None) -> str:
    """Why this host would refuse to reopen `r`, or '' if a start with options.resume may."""
    from . import start

    if r["kind"] not in start.KINDS:
        return "kind"
    if not cfg or not cfg["roots"] or r["kind"] not in cfg["kinds"] or "resume" not in cfg["kinds"][r["kind"]]["options"]:
        return "off"
    try:
        start._in_roots(r["cwd"], cfg["roots"], "cwd")
        start.clean_name(r["name"])
    except start.Refused as e:
        return e.out.get("field") or "cwd"
    return ""


def page(qs: dict) -> tuple[tuple[float, str] | None, int]:
    """`?before=<last_seen>[&before_id=<id>]&limit=<n>` of GET /sessions/dead: the last row of the previous page.
    Without before_id, every session seen at that same last_seen counts as on the previous page."""
    import math

    try:
        ts = float(qs["before"]) if "before" in qs else None
        limit = int(qs.get("limit", LIMIT))
    except ValueError:
        raise bus.BusError("before is a last_seen, limit a number", 400) from None
    bid = qs.get("before_id", "\uffff").lower()
    if (ts is not None and not (math.isfinite(ts) and ts > 0)) or not 1 <= limit <= LIMIT_MAX \
            or ("before_id" in qs and (ts is None or not _UUID.fullmatch(bid))):
        raise bus.BusError(f"before is a last_seen, before_id its session id, limit 1 to {LIMIT_MAX}", 400)
    return (None if ts is None else (round(ts, 3), bid)), limit


def listing(host: str, db: Path | None = None, qs: dict | None = None) -> dict:
    """GET /bus/v1/sessions/dead[?before=&limit=]: a page, newest first; `more` when older ones are left."""
    from . import start

    before, limit = page(qs or {})
    try:
        cfg = start.load_config()
    except start.Refused:
        cfg = None
    out = []
    got = rows(db, before=before, limit=limit + 1)
    for r in got[:limit]:
        why = resumable(r, cfg)
        out.append({**r, "resumable": not why, **({"reason": why} if why else {})})
    return {"host": host, "max_age": MAX_AGE, "limit": limit, "more": len(got) > limit, "sessions": out}


def find(sid: str, db: Path | None = None) -> tuple[dict | None, bool]:
    """(the dead row for session `sid`, whether a live agent holds that session now)."""
    db = db or bus.STATE_DIR / "bus.db"
    sid = sid.lower()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            con.row_factory = sqlite3.Row
            live = any(is_live(a) for a in con.execute("SELECT last_seen, state FROM agents WHERE lower(id)=?", (sid,)))
        finally:
            con.close()
    except sqlite3.Error:
        live = False
    return next((r for r in rows(db, limit=None) if r["id"] == sid), None), live  # any page can be reopened


def _json(s: object) -> object:
    try:
        return json.loads(s) if isinstance(s, str) and s else None
    except ValueError:
        return None


def _clean(s: str, cap: int) -> str:
    import unicodedata

    return "".join(c for c in s if unicodedata.category(c)[0] != "C").strip()[:cap]
