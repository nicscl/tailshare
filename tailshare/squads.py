"""Squads: named groups of agents with roles, kept on every machine of their owner (docs/squads.md).

A squad is a header register and one register per member, each with a rev `[hlc, host]`: the higher rev
wins, so edits to different members never clash and the same edit merges to the same result everywhere.
Deletes are tombstones, kept TOMBSTONE_DAYS. A machine keeps only squads owned by its own tailnet user,
and takes copies only from machines of that user; a sharee reads a filtered copy and never stores it."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import secrets
import threading
import time
import unicodedata

from . import bus, svg

ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
OWNER_RE = re.compile(r"[A-Za-z0-9._@+][A-Za-z0-9._@+-]{0,127}")
HOST_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
COLORS = ("honey", "teal", "indigo", "pink", "green", "slate")  # the Designer's palette ids
KINDS = ("claude", "codex")
NAME_MAX = 40
ROLE_MAX = 40
INSTRUCTIONS_MAX = 4 * 1024
PROMPT_MAX = 8 * 1024
FOLDER_MAX = 1024
MAX_SQUAD = 64 * 1024  # the whole squad, as JSON
MAX_MEMBERS = 32
LEVELS = 3  # a squad's tree: a top member, its reports, and theirs
MAX_SQUADS = 50  # per owner
MAX_SHARES = 32
FUTURE = 120_000  # ms: a rev further ahead of this machine's clock is refused
TOMBSTONE_DAYS = 30
HEADER_FIELDS = ("name", "color", "icon", "shares")
MEMBER_FIELDS = ("order", "name", "role", "icon", "host", "kind", "folder", "instructions", "instructions_from", "start",
                 "reports_to")

SCHEMA = """
CREATE TABLE IF NOT EXISTS squads(owner TEXT NOT NULL, id TEXT NOT NULL, doc TEXT NOT NULL, hlc INTEGER NOT NULL,
  rhost TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL, PRIMARY KEY(owner, id));
CREATE TABLE IF NOT EXISTS squad_members(owner TEXT NOT NULL, squad TEXT NOT NULL, id TEXT NOT NULL,
  doc TEXT NOT NULL, hlc INTEGER NOT NULL, rhost TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL,
  PRIMARY KEY(owner, squad, id));
"""


class Bad(bus.BusError):
    """400 {"error": "bad_request", "field": …}."""

    def __init__(self, field: str, reason: str = "") -> None:
        super().__init__("bad_request", 400, {"field": field, **({"reason": reason} if reason else {})})


def _id(v: object, field: str) -> str:
    if not isinstance(v, str) or not ID_RE.fullmatch(v):
        raise Bad(field, "must match [A-Za-z0-9._-]{1,64}")
    return v


_SURROGATE = re.compile("[\ud800-\udfff]")


def _text(v: object, field: str, cap: int, required: bool = False, oneline: bool = True) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str) or "\0" in v or _SURROGATE.search(v):  # a lone surrogate is no UTF-8 at all
        raise Bad(field)
    if oneline:  # display text: no control or format characters
        v = "".join(c for c in v if unicodedata.category(c)[0] != "C").strip()
    if len(v.encode()) > cap * (4 if oneline else 1) or len(v) > cap or (required and not v):
        raise Bad(field, f"1 to {cap} characters" if required else f"at most {cap} characters")
    return v


def _icon(v: object, field: str) -> str:
    try:
        return svg.clean(v)
    except svg.Bad as e:
        raise Bad(field, str(e)) from None


def _options(v: object, field: str) -> dict:
    """Shape only: the start gate checks each option against what the host allows when it starts."""
    if v is None:
        return {}
    if not isinstance(v, dict) or len(v) > 16:
        raise Bad(field)
    out = {}
    for k, x in v.items():
        if not isinstance(k, str) or not re.fullmatch(r"[a-z_]{1,32}", k):
            raise Bad(field)
        ok = (isinstance(x, bool) or (isinstance(x, str) and len(x) <= 256 and "\0" not in x)
              or (isinstance(x, list) and len(x) <= 8 and all(isinstance(i, str) and len(i) <= FOLDER_MAX for i in x)))
        if not ok:
            raise Bad(f"{field}.{k}")
        out[k] = x
    return out


def _from(v: object, field: str) -> str:
    """Whose shared squad a text was copied from (the app's "from alex" mark): a label the writer sets, not a proof."""
    if v in (None, ""):
        return ""
    if not isinstance(v, str) or not OWNER_RE.fullmatch(v):
        raise Bad(field, "a tailnet login")
    return v


def clean_header(d: dict) -> dict:
    shares = d.get("shares") or []
    if not isinstance(shares, list) or len(shares) > MAX_SHARES:
        raise Bad("shares")
    clean_shares, seen = [], set()
    for s in shares:
        if not isinstance(s, dict) or not isinstance(s.get("user"), str) or not OWNER_RE.fullmatch(s["user"]):
            raise Bad("shares")
        can = s.get("can", ["see"])
        if can != ["see"]:  # v1: seeing only; attaching needs a pane key that host's owner approved
            raise Bad("shares.can", 'only ["see"]')
        if s["user"] not in seen:
            seen.add(s["user"])
            clean_shares.append({"user": s["user"], "can": ["see"], "show_paths": s.get("show_paths") is True})
    color = d.get("color") or ""
    if color and color not in COLORS:
        raise Bad("color", "one of " + ", ".join(COLORS))
    return {"name": _text(d.get("name"), "name", NAME_MAX, required=True), "color": color,
            "icon": _icon(d.get("icon"), "icon"), "shares": clean_shares}


def clean_member(d: dict) -> dict:
    order = d.get("order", 0)
    if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order <= 9999:
        raise Bad("order", "0 to 9999")
    host = d.get("host")
    if not isinstance(host, str) or not HOST_RE.fullmatch(host):
        raise Bad("host")
    if d.get("kind") not in KINDS:
        raise Bad("kind", "claude or codex")
    folder = d.get("folder")
    if (not isinstance(folder, str) or not folder.startswith("/") or len(folder) > FOLDER_MAX
            or any(unicodedata.category(c)[0] == "C" for c in folder)):
        raise Bad("folder", "an absolute path")
    st = d.get("start") or {}
    if not isinstance(st, dict) or set(st) - {"options", "prompt", "prompt_from"}:
        raise Bad("start")
    lead = d.get("reports_to")
    if lead is not None:
        _id(lead, "reports_to")
    return {"reports_to": lead, "order": order, "name": _text(d.get("name"), "name", NAME_MAX, required=True),
            "role": _text(d.get("role"), "role", ROLE_MAX), "icon": _icon(d.get("icon"), "icon"), "host": host,
            "kind": d["kind"], "folder": posixpath.normpath(folder),
            "instructions": _text(d.get("instructions"), "instructions", INSTRUCTIONS_MAX, oneline=False),
            "instructions_from": _from(d.get("instructions_from"), "instructions_from"),
            "start": {"options": _options(st.get("options"), "start.options"),
                      "prompt": _text(st.get("prompt"), "start.prompt", PROMPT_MAX, oneline=False),
                      "prompt_from": _from(st.get("prompt_from"), "start.prompt_from")}}


def leads(members: list) -> dict[str, str | None]:
    """Each live member's lead as the tree reads it. Edits from two machines can merge into a loop, a tree deeper
    than LEVELS or a lead that was removed: a member whose chain up does any of that reads as top-level."""
    raw = {m["id"]: m.get("reports_to") for m in members if not m.get("deleted")}
    out: dict[str, str | None] = {}
    for mid in raw:
        chain, cur = [mid], raw[mid]
        while cur in raw and cur not in chain:
            chain.append(cur)
            cur = raw[cur]
        out[mid] = chain[1] if 1 < len(chain) <= LEVELS and cur not in chain else None
    return out


def branch(members: list, mid: str) -> set[str]:
    """`mid` and everyone under it, as the tree reads."""
    up = leads(members)
    out = {mid}
    for _ in range(LEVELS):
        out |= {m for m, lead in up.items() if lead in out}
    return out


def check_lead(members: list, mid: str) -> None:
    """What the owner writes for `mid` must be a tree: its lead a live member of the squad, no loop, and at most
    LEVELS levels from the top to the deepest member under it."""
    raw = {m["id"]: m.get("reports_to") for m in members if not m.get("deleted")}
    lead = raw.get(mid)
    if lead is None:
        below = 0
    elif lead == mid:
        raise Bad("reports_to", "a member can't report to itself")
    elif lead not in raw:
        raise Bad("reports_to", "not a member of this squad")
    chain, cur = [mid], lead
    while cur in raw and cur not in chain:
        chain.append(cur)
        cur = raw[cur]
    if cur == mid:
        raise Bad("reports_to", "that makes a loop")
    depth, level = 0, {mid}  # how far down its reports go, as written: the new lead may push them past LEVELS
    while depth <= LEVELS:
        level = {m for m, x in raw.items() if x in level and m != mid}
        if not level:
            break
        depth += 1
    if len(chain) + depth > LEVELS:
        raise Bad("reports_to", f"more than {LEVELS} levels")


def check_members(live: list, written) -> None:
    """The squad's live members after a write: a tree for each member whose lead it set, at most MAX_MEMBERS,
    and no two that would get the same address."""
    for mid in written:  # only what this write set: an old merged-in knot doesn't block other edits
        check_lead(live, mid)
    if len(live) > MAX_MEMBERS:
        raise Bad("members", f"at most {MAX_MEMBERS}")
    seen: dict[tuple, str] = {}
    for m in live:
        k = member_key(m)
        if k in seen:
            raise Bad(f"members.{m['id']}", f"same host, kind, folder name and name as {seen[k]}: "
                                            "they would get the same address")
        seen[k] = m["id"]


def member_key(m: dict) -> tuple:
    """Two members that would get the same agent address."""
    return m["host"], m["kind"], bus.slug(posixpath.basename(m["folder"]) or "root"), bus.slug(m["name"])


def _rev(v: object) -> tuple[int, str] | None:
    if (isinstance(v, list) and len(v) == 2 and isinstance(v[0], int) and not isinstance(v[0], bool) and v[0] > 0
            and isinstance(v[1], str) and HOST_RE.fullmatch(v[1])):
        return v[0], v[1]
    return None


def _doc_text(doc: dict | None) -> str:
    """A register's doc as stored."""
    return json.dumps(doc or {})


def _digest(text: str, deleted: object) -> str:
    """A register's content, whatever key order it was stored in."""
    try:
        canon = json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
    except ValueError:
        canon = text
    return hashlib.sha256(f"{int(bool(deleted))}:{canon}".encode()).hexdigest()[:8]


class Conflict(bus.BusError):
    def __init__(self, what: str, current: dict | None) -> None:
        super().__init__("conflict", 409, {what: current})


class Squads:
    def __init__(self, b: bus.Bus) -> None:
        self.bus, self.store = b, b.store
        with self.store.lock:
            self.store.db.executescript(SCHEMA)
            row = self.store.one("SELECT MAX(h) AS h FROM (SELECT MAX(hlc) AS h FROM squads UNION ALL "
                                 "SELECT MAX(hlc) FROM squad_members)")
        self._hlc = int(row["h"] or 0) if row else 0
        self._hlc_lock = threading.Lock()
        self._pulled: dict[tuple[str, str], str] = {}  # (host, squad) -> the sum we last pulled for
        from .suggestions import Suggestions

        self.suggestions = Suggestions(self)
        from .board import Board

        self.board = Board(self)

    # -- clock ---------------------------------------------------------------------------
    def tick(self) -> list:
        with self._hlc_lock:
            self._hlc = max(int(time.time() * 1000), self._hlc + 1)
            return [self._hlc, self.bus.net.host]

    def observe(self, hlc: int) -> None:
        with self._hlc_lock:
            self._hlc = max(self._hlc, hlc)

    @property
    def owner(self) -> str:
        u = self.bus.net.user
        if not u or u == "tagged-devices" or not OWNER_RE.fullmatch(u):
            raise bus.BusError("this machine has no single tailnet owner, so it keeps no squads", 409)
        return u

    # -- reading -------------------------------------------------------------------------
    def _header_row(self, sid: str):
        return self.store.one("SELECT * FROM squads WHERE owner=? AND id=?", self.owner, sid)

    def _member_rows(self, sid: str):
        return self.store.q("SELECT * FROM squad_members WHERE owner=? AND squad=?", self.owner, sid)

    def _member(self, r) -> dict:
        if r["deleted"]:
            return {"id": r["id"], "deleted": True, "rev": [r["hlc"], r["rhost"]]}
        return {"id": r["id"], **json.loads(r["doc"]), "rev": [r["hlc"], r["rhost"]]}

    def squad(self, sid: str, tombstones: bool = False) -> dict | None:
        """The whole squad as its owner sees it; None if there is none (or it was deleted, without tombstones)."""
        h = self._header_row(sid)
        if h is None or (h["deleted"] and not tombstones):
            return None
        ms = [self._member(r) for r in self._member_rows(sid) if tombstones or not r["deleted"]]
        ms.sort(key=lambda m: (m.get("order", 0), m["id"]))
        head = {"deleted": True} if h["deleted"] else json.loads(h["doc"])
        out = {"id": sid, "owner": h["owner"], **head, "rev": [h["hlc"], h["rhost"]], "members": ms}
        for m in ms:
            if not m.get("deleted"):
                a = self.agent_for(sid, m)
                if a:
                    m["agent"] = a
        return out

    def agent_for(self, sid: str, m: dict) -> str:
        """The member's agent, by the squad tag in its own host's /hello: a hint, never stored."""
        tag = {"id": sid, "member": m["id"]}
        if m["host"] == self.bus.net.host:
            rows = [self.bus._agent(r) for r in self.store.q("SELECT * FROM agents WHERE squad=?", json.dumps(tag, sort_keys=True))]
        else:  # only a machine of the squad's owner speaks for its members: a same-named host may be anyone's
            peer = self.bus.net.peers.get(m["host"]) or {}
            p = self.store.one("SELECT agents FROM peers WHERE host=?", m["host"]) if peer.get("user") == self.owner else None
            rows = [a for a in (json.loads(p["agents"]) if p else []) if isinstance(a, dict) and a.get("squad") == tag]
        rows = [a for a in rows if a.get("online")]
        rows.sort(key=lambda a: (a.get("last_seen") or 0, str(a.get("address"))))
        return str(rows[-1]["address"]) if rows else ""

    def _machine(self, ip: str) -> bool:
        """Whether `ip` is this machine or one that runs tailshare (it answered /hello), rather than a device."""
        net = self.bus.net
        host = net.host_for_ip(ip)
        if ip == net.ip or host == net.host:
            return True
        if host and self.store.one("SELECT 1 FROM peers WHERE host=?", host):
            return True
        return bool(self.store.one("SELECT 1 FROM peers WHERE ip=?", ip))

    def check_tag(self, tag: dict, aid: str, harness: str, cwd: str) -> dict:
        """The squad tag an agent registers with, if it is that member: on this machine, of its kind, in its
        folder, and no other live agent is it already (a `claude -p` inside a member's shell inherits the env).
        Else {}: a tag decides who squadmates and the app talk to, and whether the member may be started."""
        if not tag:
            return {}
        try:
            d = self.squad(tag["id"])
        except bus.BusError:
            return {}
        m = next((x for x in (d or {}).get("members", []) if x["id"] == tag["member"]), None)
        if m is None or m["host"] != self.bus.net.host or m["kind"] != harness \
                or not cwd or os.path.realpath(cwd) != os.path.realpath(m["folder"]):
            return {}
        other = self.store.one("SELECT 1 FROM agents WHERE squad=? AND id!=? AND last_seen>?",
                               json.dumps(tag, sort_keys=True), aid, time.time() - bus.LIVE)
        return {} if other else tag

    def match_tag(self, aid: str, harness: str, cwd: str, session: str) -> dict:
        """The member an untagged agent is, started by hand: the one member of the owner's squads on this machine of
        its kind, in its folder, under its name (as a slug). Never when another agent still runs as that member, even
        one quiet for a while (a long tool call polls nothing): its process or its pane is still there."""
        from . import dead

        want = bus.slug(session or "")
        if not want or not cwd:
            return {}
        real = os.path.realpath(cwd)
        hits = []
        for sid in self.ids():
            d = self.squad(sid) or {}
            for m in d.get("members", []):
                if not m.get("deleted") and m["host"] == self.bus.net.host and m["kind"] == harness \
                        and bus.slug(m.get("name") or "") == want and os.path.realpath(m["folder"]) == real:
                    hits.append({"id": sid, "member": m["id"]})
        if len(hits) != 1:
            return {}
        held = self.store.q("SELECT * FROM agents WHERE squad=? AND id!=? AND last_seen>?",
                            json.dumps(hits[0], sort_keys=True), aid, time.time() - dead.MAX_AGE)
        if any(dead.holds(a) for a in held):
            return {}
        return self.check_tag(hits[0], aid, harness, cwd)

    def ids(self, deleted: bool = False) -> list[str]:
        try:
            owner = self.owner
        except bus.BusError:
            return []
        return [r["id"] for r in self.store.q("SELECT id FROM squads WHERE owner=?" + ("" if deleted else " AND deleted=0")
                                               + " ORDER BY id", owner)]

    def max_rev(self, sid: str) -> list:
        revs = [(r["hlc"], r["rhost"]) for r in self.store.q(
            "SELECT hlc, rhost FROM squads WHERE owner=? AND id=? UNION ALL "
            "SELECT hlc, rhost FROM squad_members WHERE owner=? AND squad=?", self.owner, sid, self.owner, sid)]
        return list(max(revs)) if revs else []

    def sum(self, sid: str) -> str:
        """Changes with any register's rev, even one that merged in under the max: what peers compare."""
        # each register's content too: a copy stored under the writer's rev by an older version (which dropped a
        # field it did not know) differs only there, and must still make the two machines compare
        revs = sorted(f"{r['id']}:{r['hlc']}:{r['rhost']}:{_digest(r['doc'], r['deleted'])}" for r in self.store.q(
            "SELECT '' AS id, hlc, rhost, doc, deleted FROM squads WHERE owner=? AND id=? UNION ALL "
            "SELECT id, hlc, rhost, doc, deleted FROM squad_members WHERE owner=? AND squad=?", self.owner, sid, self.owner, sid))
        return hashlib.sha256("\n".join(revs).encode()).hexdigest()[:16]

    def shared_with(self, sid: str, user: str) -> dict | None:
        h = self._header_row(sid)
        if h is None or h["deleted"]:
            return None
        return next((s for s in json.loads(h["doc"])["shares"] if s["user"] == user), None)

    def for_caller(self, sid: str, user: str, own: bool, tombstones: bool = False) -> dict | None:
        """The squad as `user` may see it: the owner all of it, a sharee a copy without shares, and folders as
        basenames unless the share shows paths."""
        if own:
            return self.squad(sid, tombstones)
        share = self.shared_with(sid, user) if user else None
        if share is None:
            return None
        d = self.squad(sid)
        if d is None:
            return None
        from .start import autos, skips

        d.pop("shares", None)
        for m in d["members"]:
            st = m.get("start") or {}
            # no options: they hold folders (add_dirs), a resume id, models; only whether it runs unasked
            m["start"] = {"prompt": st.get("prompt") or "", "prompt_from": st.get("prompt_from") or "",
                          "skip": skips(m["kind"], st.get("options") or {}),
                          "auto": autos(m["kind"], st.get("options") or {})}
            if not share["show_paths"]:
                m["folder"] = posixpath.basename(m["folder"]) or "/"
        return d

    def hello(self, user: str, own: bool) -> dict:
        """/hello's `squads` (id -> max rev) and `squad_sums`, for the owner (tombstones too, so deletes spread)
        and for sharees (only what is shared with them)."""
        try:
            owner = self.owner
        except bus.BusError:
            return {}
        if own:
            ids = self.ids(deleted=True)
        elif user and user != owner:
            ids = [i for i in self.ids() if self.shared_with(i, user)]
        else:
            ids = []
        if not ids:
            return {}
        out = {"squads": {i: self.max_rev(i) for i in ids}, "squad_sums": {i: self.sum(i) for i in ids}}
        if own:  # boards go to the owner's machines only; apart from squad_sums, which suggestions use as the version
            out["board_sums"] = {i: self.board.sum(i) for i in ids}
            if self.bus.reports is not None:  # the owner's devices only, like /reports itself
                out["report_sums"] = {i: self.bus.reports.sum(i) for i in ids}
        return out

    # -- writing -------------------------------------------------------------------------
    def _check_caps(self, sid: str, written: dict | None = None) -> None:
        d = self.squad(sid)
        if d is None:
            return
        check_members(d["members"], written or {})
        if len(json.dumps(d)) > MAX_SQUAD:
            raise Bad("members", f"the squad is over {MAX_SQUAD // 1024} KB")

    def _write_header(self, sid: str, doc: dict | None, rev: list) -> None:
        self.store.x("INSERT OR REPLACE INTO squads(owner, id, doc, hlc, rhost, deleted, at) VALUES(?,?,?,?,?,?,?)",
                     self.owner, sid, json.dumps(doc or {}), rev[0], rev[1], int(doc is None), time.time())

    def _write_member(self, sid: str, mid: str, doc: dict | None, rev: list) -> None:
        self.store.x("INSERT OR REPLACE INTO squad_members(owner, squad, id, doc, hlc, rhost, deleted, at)"
                     " VALUES(?,?,?,?,?,?,?,?)", self.owner, sid, mid, json.dumps(doc or {}), rev[0], rev[1],
                     int(doc is None), time.time())

    def create(self, sid: str, body: dict) -> dict:
        """PUT /squads/<id>: a new squad, header and members, with base_rev null."""
        _id(sid, "id")
        if body.get("base_rev", None) is not None:
            raise Bad("base_rev", "null: PUT only creates a squad")
        head = clean_header({**body, "updated": None})
        members = body.get("members") or []
        if not isinstance(members, list) or len(members) > MAX_MEMBERS:
            raise Bad("members", f"at most {MAX_MEMBERS}")
        clean = []
        for i, m in enumerate(members):
            if not isinstance(m, dict):
                raise Bad(f"members.{i}")
            mid = _id(m.get("id") or "m_" + secrets.token_urlsafe(9), f"members.{i}.id")
            clean.append((mid, clean_member(m)))
        if len({mid for mid, _ in clean}) != len(clean):
            raise Bad("members", "two members with the same id")
        with self.store.lock:
            h = self._header_row(sid)
            if h is not None and not h["deleted"]:
                raise Conflict("squad", self._public_header(sid))
            if len(self.ids()) >= MAX_SQUADS:
                raise Bad("id", f"at most {MAX_SQUADS} squads")
            now = int(time.time())
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                for r in self._member_rows(sid):  # a squad made again starts with no members
                    if not r["deleted"]:
                        self._write_member(sid, r["id"], None, self.tick())
                self._write_header(sid, {**head, "updated": now}, self.tick())
                for mid, m in clean:
                    self._write_member(sid, mid, {**m, "updated": now}, self.tick())
                self._check_caps(sid, {mid: m for mid, m in clean if m["reports_to"] is not None})
            except BaseException:
                self.store.db.execute("ROLLBACK")
                raise
            self.store.db.execute("COMMIT")
        return {"squad": self.squad(sid)}

    def _public_header(self, sid: str) -> dict | None:
        d = self.squad(sid, tombstones=True)
        if d is not None:
            d.pop("members", None)
        return d

    def _base(self, body: dict, current) -> None:
        """Compare-and-set: the write is refused unless the caller saw the current rev."""
        if "base_rev" not in body:
            raise Bad("base_rev", "required: the rev you last read, or null for something new")
        base = body["base_rev"]
        have = [current["hlc"], current["rhost"]] if current is not None else None
        new = current is None or current["deleted"]
        if (base is None and not new) or (base is not None and (_rev(base) is None or list(base) != have)):
            raise Bad("base_rev") if base is not None and _rev(base) is None else _StaleBase()

    def patch_header(self, sid: str, body: dict) -> dict:
        _id(sid, "id")
        with self.store.lock:
            h = self._header_row(sid)
            if h is None or h["deleted"]:
                raise bus.BusError("no such squad", 404)
            try:
                self._base(body, h)
            except _StaleBase:
                raise Conflict("squad", self._public_header(sid)) from None
            if body.get("deleted") is True:
                self.store.db.execute("BEGIN IMMEDIATE")
                try:
                    for r in self._member_rows(sid):
                        if not r["deleted"]:
                            self._write_member(sid, r["id"], None, self.tick())
                    self._write_header(sid, None, self.tick())
                finally:
                    self.store.db.execute("COMMIT")
                return {"squad": self._public_header(sid)}
            doc = {**json.loads(h["doc"]), **{k: body[k] for k in HEADER_FIELDS if k in body}}
            self._write_header(sid, {**clean_header(doc), "updated": int(time.time())}, self.tick())
            return {"squad": self._public_header(sid)}

    def patch_member(self, sid: str, mid: str, body: dict) -> dict:
        _id(sid, "id")
        _id(mid, "member")
        with self.store.lock:
            h = self._header_row(sid)
            if h is None or h["deleted"]:
                raise bus.BusError("no such squad", 404)
            r = self.store.one("SELECT * FROM squad_members WHERE owner=? AND squad=? AND id=?", self.owner, sid, mid)
            try:
                self._base(body, r)
            except _StaleBase:
                raise Conflict("member", self._member(r) if r is not None else None) from None
            if body.get("deleted") is True:
                if r is None or r["deleted"]:
                    raise bus.BusError("no such member", 404)
                self._write_member(sid, mid, None, self.tick())
            else:
                old = json.loads(r["doc"]) if r is not None and not r["deleted"] else {}
                if "instructions" in body and "instructions_from" not in body:
                    old.pop("instructions_from", None)  # the owner's own edit clears the mark; sending it again keeps it
                doc = clean_member({**old, **{k: body[k] for k in MEMBER_FIELDS if k in body}})
                self.store.db.execute("BEGIN IMMEDIATE")
                try:
                    self._write_member(sid, mid, {**doc, "updated": int(time.time())}, self.tick())
                    self._check_caps(sid, {mid: body} if "reports_to" in body else {})
                except BaseException:
                    self.store.db.execute("ROLLBACK")
                    raise
                self.store.db.execute("COMMIT")
            r = self.store.one("SELECT * FROM squad_members WHERE owner=? AND squad=? AND id=?", self.owner, sid, mid)
            m = self._member(r)
            if not m.get("deleted"):
                a = self.agent_for(sid, m)
                if a:
                    m["agent"] = a
            return {"member": m}

    # -- replicating ---------------------------------------------------------------------
    def merge(self, d: object, source: str = "") -> int:
        """Take a copy from another machine of the owner: each register that is newer and valid. Returns how
        many registers changed. Invalid registers are dropped one by one; caps are the writer's to keep.
        `source` is the machine the copy came from: for a register it wrote itself, its copy also replaces ours at
        the same rev when they differ (ours was stored by a version that did not keep every field)."""
        if not isinstance(d, dict) or d.get("owner") != self.owner or not isinstance(d.get("id"), str) \
                or not ID_RE.fullmatch(d["id"]):
            return 0
        sid, limit, n = d["id"], int(time.time() * 1000) + FUTURE, 0

        def newer(rev, row) -> bool:
            return rev is not None and rev[0] <= limit and (row is None or rev > (row["hlc"], row["rhost"]))

        def healed(rev, row, doc) -> bool:
            """The writer's own copy at our rev, saying something else: ours was stored by a version that dropped
            a field it did not know, and the rev alone would never let it be repaired."""
            return bool(source) and rev is not None and row is not None and rev == (row["hlc"], row["rhost"]) \
                and rev[1] == source and _digest(_doc_text(doc), doc is None) != _digest(row["doc"], row["deleted"])

        with self.store.lock:
            rev, row = _rev(d.get("rev")), self._header_row(sid)
            if newer(rev, row) or (source and row is not None and rev == (row["hlc"], row["rhost"])):
                try:
                    doc = None if d.get("deleted") is True else {**clean_header(d), "updated": int(d.get("updated") or 0)}
                    if newer(rev, row) or healed(rev, row, doc):
                        self._write_header(sid, doc, list(rev))
                        self.observe(rev[0])
                        n += 1
                except bus.BusError:
                    pass
            if self._header_row(sid) is None:  # members only arrive with their squad
                return n
            for m in d.get("members") or []:
                if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not ID_RE.fullmatch(m["id"]):
                    continue
                rev = _rev(m.get("rev"))
                row = self.store.one("SELECT * FROM squad_members WHERE owner=? AND squad=? AND id=?", self.owner, sid, m["id"])
                if not newer(rev, row) and not (source and row is not None and rev == (row["hlc"], row["rhost"])):
                    continue
                try:
                    doc = None if m.get("deleted") is True else {**clean_member(m), "updated": int(m.get("updated") or 0)}
                except bus.BusError:
                    continue
                if not newer(rev, row) and not healed(rev, row, doc):
                    continue
                self._write_member(sid, m["id"], doc, list(rev))
                self.observe(rev[0])
                n += 1
        return n

    def sync_from(self, host: str, peer: dict, hello: dict) -> None:
        """After asking a peer's /hello in discover: pull each squad whose registers differ from ours. Only from
        machines of this machine's own tailnet user, the squads' owner."""
        try:
            owner = self.owner
        except bus.BusError:
            return
        if peer.get("user") != owner:
            return
        sums = hello.get("squad_sums")
        if not isinstance(sums, dict):
            return
        for sid, s in list(sums.items())[:MAX_SQUADS * 2]:
            if not isinstance(sid, str) or not ID_RE.fullmatch(sid) or not isinstance(s, str):
                continue
            if self.store.one("SELECT 1 FROM squads WHERE owner=? AND id=?", owner, sid) and self.sum(sid) == s:
                continue
            if self._pulled.get((host, sid)) == s + self.sum(sid):  # nothing newer there for us last time
                continue
            try:
                d = bus.request(f"http://{peer['ip']}:{peer['port']}/bus/v1/squads/{sid}?tombstones=1", timeout=5)
            except Exception:  # noqa: BLE001
                continue
            if self.merge(d.get("squad"), host):
                bus.log(f"squads: {sid} updated from {host}")
            self._pulled[(host, sid)] = s + self.sum(sid)
        boards = hello.get("board_sums")  # an older peer has none
        for sid, s in (list(boards.items())[:MAX_SQUADS * 2] if isinstance(boards, dict) else []):
            if not isinstance(sid, str) or not ID_RE.fullmatch(sid) or not isinstance(s, str) \
                    or self._header_row(sid) is None or self.board.sum(sid) == s \
                    or self._pulled.get((host, sid, "board")) == s + self.board.sum(sid):
                continue
            if self.board.pull(host, peer, sid):
                bus.log(f"squads: {sid}'s board updated from {host}")
            self._pulled[(host, sid, "board")] = s + self.board.sum(sid)

    def prune(self) -> None:
        cutoff = time.time() - TOMBSTONE_DAYS * 86400
        self.store.x("DELETE FROM squad_members WHERE deleted=1 AND at<?", cutoff)
        self.store.x("DELETE FROM squads WHERE deleted=1 AND at<? AND NOT EXISTS (SELECT 1 FROM squad_members m "
                     "WHERE m.owner=squads.owner AND m.squad=squads.id)", cutoff)
        self.suggestions.prune()
        self.board.prune()

    # -- HTTP ----------------------------------------------------------------------------
    def route(self, method: str, path: str, body: dict, local: bool, ip: str, qs: dict, token: str = "",
              signed: tuple[bytes, dict] = (b"", {})) -> dict:
        parts = path.strip("/").split("/")  # squads[/<id>[/members/<member>]]
        if local:
            user, own = self.owner, True
        else:
            if self.bus.net.user_for_ip(ip) is None:
                self.bus.net.refresh()
            user = self.bus.net.user_for_ip(ip) or ""
            own = bool(user) and user != "tagged-devices" and user == self.bus.net.user
        if len(parts) >= 3 and parts[2] == "board":
            def sign() -> dict:
                if not any(signed[1].values()) and self._machine(ip):
                    raise bus.BusError("board edits from a machine go through its own local API: `tailshare board`", 403)
                if self.bus.keys is None:
                    raise bus.BusError("signature", 403, {"reason": "unknown_key", "detail": "SSH keys are off on this machine"})
                return self.bus.keys.check_signed(method, "/bus/v1" + path, signed[0], signed[1], self.owner, ip)
            return self.board.route(method, parts, body, local, own, sign)
        sugg = len(parts) in (3, 4, 5) and parts[2] == "suggestions"
        if sugg and not own:  # suggestions are the owner's: a sharee sees the squad, not what agents proposed
            raise bus.BusError("only the squad's owner sees its suggestions", 403)
        if sugg and method == "GET" and len(parts) in (3, 4):
            return self.suggestions.read(parts, local, qs)
        if sugg and method == "POST" and len(parts) == 3:
            if not local:  # agents suggest through their own machine's local API
                raise bus.BusError("not found", 404)
            return self.suggestions.suggest(_id(parts[1], "id"), body)
        if method == "GET":
            if parts == ["squads"]:
                ids = self.ids() if own else [i for i in self.ids() if user and self.shared_with(i, user)]
                return {"squads": [x for x in (self.for_caller(i, user, own) for i in ids) if x]}
            if len(parts) == 2:
                with self.store.lock:
                    d = self.for_caller(_id(parts[1], "id"), user, own, tombstones=own and qs.get("tombstones") == "1")
                    if d is None:
                        raise bus.BusError("no such squad", 404)
                    return {"squad": d, **({"version": self.sum(parts[1])} if own else {})}
            raise bus.BusError("not found", 404)
        if not own:  # a sharee's writes, or anyone else's, never land
            raise bus.BusError("only the squad's owner can change it", 403)
        if not local and not any(signed[1].values()) and self._machine(ip):
            # a program on one of the owner's machines (this one included, by its tailnet IP) would skip the token
            # the local API asks for: machines write through their own local API; the peer API is for devices
            raise bus.BusError("squad edits from a machine go through its own local API: `tailshare squad edit`", 403)
        if not local:  # a device signs each write with one of its keys installed here (the iPad's comes from a machine)
            if self.bus.keys is None:
                raise bus.BusError("signature", 403, {"reason": "unknown_key", "detail": "SSH keys are off on this machine"})
            self.bus.keys.check_signed(method, "/bus/v1" + path, signed[0], signed[1], self.owner, ip)
        if local and (self.bus.keys is None or len(self.bus.keys.token) < 64
                      or not secrets.compare_digest(token.encode(), self.bus.keys.token.encode())):
            raise bus.BusError("local squad edits need ~/.tailshare/keys.token: use `tailshare squad edit`", 403)
        if method == "POST" and len(parts) == 5 and parts[2] == "members" and parts[4] == "run":
            from . import (
                keep,  # Stop / keep running / restart, from the app (signed) or the owner's token
            )

            return keep.change(self.bus, _id(parts[1], "id"), _id(parts[3], "member"), str(body.get("run") or ""),
                               "owner" if local else "a device")
        if method == "PUT" and len(parts) == 2:
            return self.create(parts[1], body)
        if method == "PATCH" and len(parts) == 2:
            return self.patch_header(parts[1], body)
        if method == "PATCH" and len(parts) == 4 and parts[2] == "members":
            return self.patch_member(parts[1], parts[3], body)
        if sugg and method == "POST" and len(parts) == 5 and parts[4] in ("apply", "dismiss"):
            sid = _id(parts[1], "id")
            return self.suggestions.apply(sid, parts[3], body) if parts[4] == "apply" else \
                self.suggestions.dismiss(sid, parts[3])
        raise bus.BusError("not found", 404)


class _StaleBase(Exception):
    pass


def start(b: bus.Bus) -> Squads:
    return Squads(b)
