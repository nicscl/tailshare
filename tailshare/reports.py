"""Agent reports: what each running squad member says it does, for the squad page (docs/reports.md).

A member writes only its own report: the bus knows which member an agent is from the squad tag it registered with
(squads.check_tag), and a write names the agent, not the member. Reports live on the member's machine, beside its
bus, and never sync. The peer API serves them to the owner's own devices and machines only.

Everything in a report is the agent's text: it is cleaned and capped here, and what an app may do with it is
checked again when the report is read, against the squad tree as it is then."""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from urllib.parse import urlsplit

from . import bus, squads

BIO_MAX = 600
TITLE_MAX = 80
WAIT_MAX = 120
WHY_MAX = 600
OPTION_MAX = 40
NOTES_MAX = 8 * 1024
URL_MAX = 512
NOW_MAX, NEXT_MAX, DONE_MAX = 3, 5, 20
OPTIONS_MAX = 4
OPEN_MAX = 8  # open blockers a member may have
RESOLVED_MAX = 16
KEEP = 86400  # done tasks and resolved blockers: "today", as the last 24 hours
FIELDS = ("bio", "now", "next", "done", "waiting_on", "blockers", "notes")
BLOCKER_ID = re.compile(r"[A-Za-z0-9._-]{1,32}")
REQUEST_ID = re.compile(r"[0-9a-f]{8,32}")
HOST = re.compile(r"(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports(owner TEXT NOT NULL, squad TEXT NOT NULL, member TEXT NOT NULL, doc TEXT NOT NULL,
  agent TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(owner, squad, member));
"""


class Bad(squads.Bad):
    pass


# --------------------------------------------------------------------------- cleaning


def text(v: object, field: str, cap: int, lines: bool = False, required: bool = False) -> str:
    """Display text: no control, format (bidi), private or line/paragraph-separator characters; newlines only where
    `lines`."""
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise Bad(field, "a string")
    if lines:
        v = v.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ").replace("\u2028", "\n").replace("\u2029", "\n")
    v = "".join(c for c in v if (lines and c == "\n") or unicodedata.category(c)[0] != "C" and unicodedata.category(c) not in ("Zl", "Zp")).strip()
    if len(v) > cap or len(v.encode()) > 4 * cap:
        raise Bad(field, f"at most {cap} characters")
    if required and not v:
        raise Bad(field, "required")
    return v


def url(v: object, field: str) -> str:
    """A link the app shows as its host and opens in Safari: https, a plain ASCII host name, no user or port."""
    if v in (None, ""):
        return ""
    if not isinstance(v, str) or len(v) > URL_MAX or not v.isascii() or any(c.isspace() or ord(c) < 32 or c == "\x7f" for c in v):
        raise Bad(field, "an https URL of ASCII characters")
    try:
        u = urlsplit(v)
        port = u.port
    except ValueError:
        raise Bad(field, "an https URL") from None
    host = (u.hostname or "").rstrip(".")
    if u.scheme != "https" or "@" in u.netloc or port not in (None, 443) or not HOST.fullmatch(host) \
            or u.netloc.lower() not in (host, host + ":443"):
        raise Bad(field, "an https URL with a host name, and no user, password or other port")
    return v


def task(v: object, field: str) -> dict:
    if isinstance(v, str):
        v = {"title": v}
    if not isinstance(v, dict) or set(v) - {"title", "url", "at"}:
        raise Bad(field, "{title, url?}")
    out = {"title": text(v.get("title"), f"{field}.title", TITLE_MAX, required=True)}
    link = url(v.get("url"), f"{field}.url")
    if link:
        out["url"] = link
    return out


def cut_titles(body: dict) -> list[str]:
    """Shorten over-long task titles in a write (now, next, done) in place, rather than refuse the write: to
    TITLE_MAX with an ellipsis. The fields it cut, for the reply."""
    out = []
    for f in ("now", "next", "done"):
        v = body.get(f)
        for i, x in enumerate(v if isinstance(v, list) else []):
            t = x if isinstance(x, str) else x.get("title") if isinstance(x, dict) else None
            if isinstance(t, str) and len(t.strip()) > TITLE_MAX:
                short = t.strip()[:TITLE_MAX - 1].rstrip() + "…"
                v[i] = short if isinstance(x, str) else {**x, "title": short}
                out.append(f"{f}.{i}.title")
    return out


def tasks(v: object, field: str, cap: int) -> list:
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > cap:
        raise Bad(field, f"a list of at most {cap} tasks")
    return [task(x, f"{field}.{i}") for i, x in enumerate(v)]


def member_ref(v: object, field: str) -> str:
    if not isinstance(v, str) or not squads.ID_RE.fullmatch(v):
        raise Bad(field, "a member id")
    return v


def waiting(v: object) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict) or set(v) - {"member", "text"}:
        raise Bad("waiting_on", '{member: <member id> | "owner", text}')
    who = v.get("member")
    who = "owner" if who == "owner" else member_ref(who, "waiting_on.member")
    return {"member": who, "text": text(v.get("text"), "waiting_on.text", WAIT_MAX, required=True)}


def approval(v: object, field: str) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict):
        raise Bad(field)
    if v.get("type") == "key_enroll":
        if set(v) - {"type", "host", "request_id"} or not isinstance(v.get("host"), str) \
                or not squads.HOST_RE.fullmatch(v["host"]):
            raise Bad(f"{field}.host", "a machine name")
        if not isinstance(v.get("request_id"), str) or not REQUEST_ID.fullmatch(v["request_id"]):
            raise Bad(f"{field}.request_id", "the request id `tailshare keys pending` shows")
        return {"type": "key_enroll", "host": v["host"], "request_id": v["request_id"]}
    if v.get("type") == "squad_structure":
        tree = v.get("reports_to")
        if set(v) - {"type", "reports_to"} or not isinstance(tree, dict) or not 0 < len(tree) <= squads.MAX_MEMBERS:
            raise Bad(f"{field}.reports_to", "{member: lead or null}")
        return {"type": "squad_structure", "reports_to": {
            member_ref(k, f"{field}.reports_to"): None if x is None else member_ref(x, f"{field}.reports_to.{k}")
            for k, x in tree.items()}}
    if v.get("type") == "squad_change":
        if set(v) - {"type", "host", "suggestion"} or not isinstance(v.get("host"), str) \
                or not squads.HOST_RE.fullmatch(v["host"]):
            raise Bad(f"{field}.host", "the machine the suggestion is on")
        s = v.get("suggestion")
        if not isinstance(s, str) or len(s) != 25 or not s.startswith("sg_") or not s[3:].isalnum() or not s.isascii():
            raise Bad(f"{field}.suggestion", "a suggestion id, sg_…")
        return {"type": "squad_change", "host": v["host"], "suggestion": s}
    raise Bad(f"{field}.type", "key_enroll, squad_change or squad_structure")


def blocker(v: object, field: str) -> dict:
    if not isinstance(v, dict) or set(v) - {"id", "kind", "title", "why", "options", "to", "for", "approval", "resolved",
                                            "at", "resolved_at"}:
        raise Bad(field, "{id, kind, title, why?, options?, to, for?, approval?, resolved?}")
    if not isinstance(v.get("id"), str) or not BLOCKER_ID.fullmatch(v["id"]):
        raise Bad(f"{field}.id", "[A-Za-z0-9._-]{1,32}")
    kind = v.get("kind")
    if kind not in ("question", "approval"):
        raise Bad(f"{field}.kind", "question or approval")
    to = v.get("to", "lead")
    if to not in ("lead", "owner"):
        raise Bad(f"{field}.to", "lead or owner")
    opts = v.get("options") or []
    if not isinstance(opts, list) or len(opts) > OPTIONS_MAX:
        raise Bad(f"{field}.options", f"at most {OPTIONS_MAX}")
    relay = v.get("for")
    if relay is not None:
        if not isinstance(relay, dict) or set(relay) != {"member", "blocker"} \
                or not isinstance(relay["blocker"], str) or not BLOCKER_ID.fullmatch(relay["blocker"]):
            raise Bad(f"{field}.for", "{member, blocker}")
        relay = {"member": member_ref(relay["member"], f"{field}.for.member"), "blocker": relay["blocker"]}
    appr = approval(v.get("approval"), f"{field}.approval")
    if (kind == "approval") != (appr is not None):
        raise Bad(f"{field}.approval", "an approval blocker has one, a question none")
    if v.get("resolved") not in (None, True, False):
        raise Bad(f"{field}.resolved", "true or false")
    return {"id": v["id"], "kind": kind, "title": text(v.get("title"), f"{field}.title", TITLE_MAX, required=True),
            "why": text(v.get("why"), f"{field}.why", WHY_MAX, lines=True),
            "options": [text(o, f"{field}.options.{i}", OPTION_MAX, required=True) for i, o in enumerate(opts)],
            "to": to, "for": relay, "approval": appr, "resolved": v.get("resolved") is True}


# --------------------------------------------------------------------------- the store


class Reports:
    def __init__(self, b: bus.Bus) -> None:
        self.bus, self.store = b, b.store
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    @property
    def squads(self) -> squads.Squads:
        if self.bus.squads is None:
            raise bus.BusError("squads are not available on this machine", 409)
        return self.bus.squads

    def _row(self, sid: str, mid: str):
        return self.store.one("SELECT * FROM reports WHERE owner=? AND squad=? AND member=?", self.squads.owner, sid, mid)

    def whose(self, aid: str) -> tuple[dict, dict]:
        """The squad and member a live agent is, from its registered tag. 409 not_a_member otherwise."""
        r = self.store.one("SELECT * FROM agents WHERE id=?", str(aid or "")[:100])
        if r is None or r["last_seen"] <= time.time() - bus.LIVE:
            raise bus.BusError("unknown agent; register first", 404)
        tag = json.loads(r["squad"]) if r["squad"] else {}
        sq = self.squads.squad(tag["id"]) if tag else None
        m = next((x for x in (sq or {}).get("members", []) if x["id"] == tag.get("member")), None)
        if m is None or m["host"] != self.bus.net.host:
            raise bus.BusError("not_a_member", 409, {"reason": "this session was not started as a squad member, so it "
                                                     "has no report to write"})
        return sq, {**m, "_agent": r["address"]}

    def write(self, aid: str, body: dict) -> dict:
        """POST /report (local API): the fields sent replace those fields of the agent's own report."""
        extra = set(body) - set(FIELDS) - {"agent"}
        if extra:
            raise Bad(sorted(extra)[0], "not a report field")
        sq, m = self.whose(aid)
        body = json.loads(json.dumps(body))  # cut_titles edits it
        cut = cut_titles(body)
        sid, mid, now = sq["id"], m["id"], time.time()
        with self.store.lock:
            row = self._row(sid, mid)
            old = json.loads(row["doc"]) if row else {}
            doc = {k: old.get(k) for k in FIELDS if k in old}
            if "bio" in body:
                doc["bio"] = text(body["bio"], "bio", BIO_MAX, lines=True)
            if "now" in body:
                doc["now"] = tasks(body["now"], "now", NOW_MAX)
            if "next" in body:
                doc["next"] = tasks(body["next"], "next", NEXT_MAX)
            if "done" in body:
                doc["done"] = self._stamp_done(body["done"], old.get("done") or [], now)
            if "waiting_on" in body:
                doc["waiting_on"] = waiting(body["waiting_on"])
            if "notes" in body:
                doc["notes"] = text(body["notes"], "notes", NOTES_MAX, lines=True)
            if "blockers" in body:
                doc["blockers"] = self._stamp_blockers(body["blockers"], old.get("blockers") or [], now, sq, mid)
            if (doc.get("waiting_on") or {}).get("member") == mid:
                raise Bad("waiting_on.member", "not yourself")
            if len(json.dumps(doc)) > 48 * 1024:
                raise Bad("report", "over 48 KB")
            self.store.x("INSERT OR REPLACE INTO reports(owner, squad, member, doc, agent, at) VALUES(?,?,?,?,?,?)",
                         self.squads.owner, sid, mid, json.dumps(doc), m["_agent"], now)
        return {"report": self.view(sq, mid), **({"cut": cut} if cut else {})}

    @staticmethod
    def _stamp_done(v: object, old: list, now: float) -> list:
        """done[] with the time each task first showed up as done: kept for a task already there, now for a new one."""
        new = tasks(v, "done", DONE_MAX)
        was = {(t["title"], t.get("url", "")): t["at"] for t in old if isinstance(t, dict) and "at" in t}
        return [{**t, "at": was.get((t["title"], t.get("url", "")), now)} for t in new]

    def _stamp_blockers(self, v: object, old: list, now: float, sq: dict, mid: str) -> list:
        if not isinstance(v, list) or len(v) > OPEN_MAX + RESOLVED_MAX:
            raise Bad("blockers", f"a list of at most {OPEN_MAX + RESOLVED_MAX}")
        was = {b["id"]: b for b in old if isinstance(b, dict) and "id" in b}
        out, ids = [], set()
        for i, x in enumerate(v):
            b = blocker(x, f"blockers.{i}")
            if b["id"] in ids:
                raise Bad(f"blockers.{i}.id", "two blockers with this id")
            ids.add(b["id"])
            problem = self._may_raise(sq, mid, b)
            if problem:
                raise Bad(f"blockers.{i}", problem)
            prev = was.get(b["id"]) or {}
            resolved = b.pop("resolved") or (bool(prev.get("resolved_at")) and x.get("resolved") is not False)  # echoed back
            out.append({**b, "at": prev.get("at", now),
                        "resolved_at": (prev.get("resolved_at") or now) if resolved else None})
        if sum(1 for b in out if b["resolved_at"] is None) > OPEN_MAX:
            raise Bad("blockers", f"at most {OPEN_MAX} open")
        return out

    @staticmethod
    def _may_raise(sq: dict, mid: str, b: dict) -> str:
        """Why this member may not raise `b`, by the tree as it is now; "" if it may."""
        ms = sq["members"]
        up = squads.leads(ms)
        ids = {m["id"] for m in ms if not m.get("deleted")}
        top, lead = up.get(mid) is None, mid in up.values()
        if b["to"] == "owner" and not (top or lead):
            return "only a lead, or a member no one leads, raises a blocker to the owner: raise it to your lead"
        if b["to"] == "lead" and top:
            return "no one leads you: raise it to the owner"
        if b["for"] is not None and (b["for"]["member"] == mid or b["for"]["member"] not in squads.branch(ms, mid)):
            return "for: a member of your branch, other than you"
        a = b["approval"] or {}
        if a.get("type") == "squad_structure":
            if not top:
                return "only a member no one leads proposes a structure"
            if set(a["reports_to"]) - ids or set(filter(None, a["reports_to"].values())) - ids:
                return "a structure names members that aren't in the squad"
        return ""

    # -- reading -------------------------------------------------------------------------
    def view(self, sq: dict, mid: str, now: float | None = None) -> dict | None:
        """A member's report as served: trimmed to the last 24 hours, and only what the tree lets it raise."""
        now = time.time() if now is None else now
        row = self._row(sq["id"], mid)
        if row is None:
            return None
        doc = json.loads(row["doc"])
        ms = sq["members"]
        ids = {m["id"] for m in ms if not m.get("deleted")}
        m = next((x for x in ms if x["id"] == mid), {})
        lead = mid in squads.leads(ms).values()
        agent = self.squads.agent_for(sq["id"], m) if m else ""
        wait = doc.get("waiting_on")
        if wait and wait["member"] != "owner" and wait["member"] not in ids:
            wait = None
        blockers = []
        for b in doc.get("blockers") or []:
            if b.get("resolved_at") and b["resolved_at"] < now - KEEP:
                continue
            if self._may_raise(sq, mid, b) or not self._pending(b.get("approval")):
                continue
            blockers.append(b)
        return {"member": mid, "agent": agent or row["agent"], "harness": m.get("kind") or "", "online": bool(agent),
                "at": row["at"], "lead": lead,
                "bio": doc.get("bio") or "", "now": doc.get("now") or [], "next": doc.get("next") or [],
                "done": [t for t in doc.get("done") or [] if t.get("at", 0) >= now - KEEP],
                "waiting_on": wait, "blockers": blockers, "notes": (doc.get("notes") or "") if lead else ""}

    def _pending(self, a: dict | None) -> bool:
        """A key approval that names this machine must be a request pending here; another machine answers for its
        own (the app asks it). An agent can name any request id; this keeps a card from showing for one that isn't."""
        if not a or a.get("type") != "key_enroll" or a.get("host") != self.bus.net.host:
            return True
        rows = self.store.q("SELECT id FROM key_requests WHERE status='pending' AND expires>? AND substr(id, 1, ?)=?",
                            time.time(), len(a["request_id"]), a["request_id"]) if self.bus.keys is not None else []
        return len(rows) == 1

    def here(self, sid: str) -> dict:
        """The reports of a squad's members on this machine."""
        sq = self.squads.squad(squads._id(sid, "squad"))
        if sq is None:
            raise bus.BusError("no such squad", 404)
        out = [self.view(sq, m["id"]) for m in sq["members"] if m["host"] == self.bus.net.host]
        return {"squad": sid, "host": self.bus.net.host, "at": time.time(), "reports": [r for r in out if r]}

    def sum(self, sid: str) -> str:
        """/hello's report_sums: a digest of what `here` serves for the squad, so the owner's devices refetch
        /reports as soon as a report, a blocker or the tree changes, not on their next poll. Which session is
        online is left out: that is /hello's own agents list, and it would change the sum on every restart."""
        try:
            reports = self.here(sid)["reports"]
        except bus.BusError:
            return ""
        parts = [{k: v for k, v in r.items() if k not in ("online", "agent")} for r in reports]
        return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]

    def everywhere(self, sid: str) -> dict:
        """`tailshare report ls`: this machine's reports and those of the squad's other hosts, if they answer."""
        d = self.here(sid)
        reports = [{**r, "host": d["host"]} for r in d["reports"]]
        sq = self.squads.squad(sid) or {}
        missing = []
        for h in sorted({m["host"] for m in sq.get("members", [])} - {self.bus.net.host}):
            p = self.bus.net.peers.get(h) or {}
            if p.get("user") != self.squads.owner or not p.get("ip") or not p.get("port"):
                missing.append(h)  # only a machine of the squad's owner speaks for its members
                continue
            try:
                got = bus.request(f"http://{p['ip']}:{p['port']}/bus/v1/reports?squad={sid}", timeout=5)
                reports += [{**r, "host": h} for r in got.get("reports") or [] if isinstance(r, dict)]
            except Exception:  # noqa: BLE001
                missing.append(h)
        return {"squad": sid, "reports": reports, "unreachable": missing}

    def prune(self) -> None:
        """Reports of members that are gone from their squad."""
        try:
            owner = self.squads.owner
        except bus.BusError:
            return
        for r in self.store.q("SELECT squad, member FROM reports WHERE owner=?", owner):
            sq = self.squads.squad(r["squad"])
            if sq is None or not any(m["id"] == r["member"] for m in sq["members"]):
                self.store.x("DELETE FROM reports WHERE owner=? AND squad=? AND member=?", owner, r["squad"], r["member"])

    # -- HTTP ----------------------------------------------------------------------------
    def route(self, method: str, path: str, body: dict, local: bool, ip: str, qs: dict) -> dict:
        if local:
            if (method, path) == ("POST", "/report"):
                return self.write(str(body.get("agent") or ""), body)
            if (method, path) == ("GET", "/report"):
                sq, m = self.whose(qs.get("agent", ""))
                return {"report": self.view(sq, m["id"]), "member": m["id"], "squad": sq["id"]}
            if (method, path) == ("GET", "/reports"):
                sid = qs.get("squad", "")
                return self.everywhere(sid) if qs.get("all") == "1" else self.here(sid)
            raise bus.BusError("not found", 404)
        if (method, path) != ("GET", "/reports"):
            raise bus.BusError("not found", 404)
        self.bus.require_same_user(ip)  # the owner's devices and machines; sharees see the tree, not reports
        if self.bus.net.user in ("", "tagged-devices"):
            raise bus.BusError("this machine has no single tailnet owner", 403)
        return self.here(qs.get("squad", ""))


def start(b: bus.Bus) -> Reports:
    return Reports(b)
