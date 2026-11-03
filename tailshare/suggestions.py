"""Squad change suggestions: an agent proposes, the owner applies (docs/suggestions.md).

A suggestion is stored on the machine whose agent made it, as ops plus a diff against the squad version it was
made at (`Squads.sum`). Agents write suggestions; only an owner write (the keys token locally, a signed write from a
device) applies or dismisses one. Applying writes the ops as one squad write, all or nothing; the squad then
replicates as usual."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
import time

from . import bus, reports, squads

WHY_MAX = 600
OPS_MAX = 8
PENDING_PER_SQUAD = 20
PENDING_PER_AGENT = 3
PER_HOUR = 10
EXPIRES = 7 * 86400
KEEP = 86400  # resolved ones are listed for a day
FORGET = 30 * 86400  # and deleted after this
ADD_FIELDS = ("name", "role", "icon", "host", "kind", "folder", "instructions", "reports_to")
SET_FIELDS = ("name", "role", "icon", "instructions", "reports_to")
SHOWN = ADD_FIELDS  # a member's fields as a diff shows them
_B62 = string.digits + string.ascii_letters

SCHEMA = """
CREATE TABLE IF NOT EXISTS squad_suggestions(id TEXT PRIMARY KEY, owner TEXT NOT NULL, squad TEXT NOT NULL,
  base TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL, why TEXT NOT NULL, by_ TEXT NOT NULL,
  agent TEXT NOT NULL, ops TEXT NOT NULL, diff TEXT NOT NULL, key TEXT NOT NULL, resolved_at REAL);
"""

Bad = squads.Bad


def new_id() -> str:
    return "sg_" + "".join(secrets.choice(_B62) for _ in range(22))


def _sid(v: object) -> str:
    if not isinstance(v, str) or len(v) != 25 or not v.startswith("sg_") or any(c not in _B62 for c in v[3:]):
        raise bus.BusError("no such suggestion", 404)
    return v


def _shown(doc: dict) -> dict:
    return {k: doc.get(k) for k in SHOWN}


class Suggestions:
    def __init__(self, sq: squads.Squads) -> None:
        self.squads, self.bus, self.store = sq, sq.bus, sq.store
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    # -- checking ops --------------------------------------------------------------------
    def _owner_host(self, host: str) -> bool:
        """One of the squad owner's own machines: this one, or a peer of the same tailnet user that runs tailshare."""
        if host == self.bus.net.host:
            return True
        p = self.bus.net.peers.get(host) or {}
        return p.get("user") == self.squads.owner and bool(self.store.one("SELECT 1 FROM peers WHERE host=?", host))

    def plan(self, sq: dict, ops: object, me: dict | None) -> tuple[list, dict, list, set]:
        """Run `ops` on a copy of `sq`. Returns (clean ops, the members after them by id, the diff, the ids whose
        lead was written). New members are keyed "new:<index>". Raises Bad as the write would. `me` is the
        suggester ({harness, cwd, member}) or None when the owner applies."""
        if not isinstance(ops, list) or not 1 <= len(ops) <= OPS_MAX:
            raise Bad("ops", f"1 to {OPS_MAX} ops")
        members = {m["id"]: {k: v for k, v in m.items() if k not in ("id", "rev", "agent")}
                   for m in sq["members"] if not m.get("deleted")}
        names = {mid: m["name"] for mid, m in members.items()}
        order = max([m.get("order", 0) for m in members.values()] or [0])
        clean, diff, leads = [], [], set()

        def lead_ref(v: object, i: int, field: str) -> str | None:
            if v is None:
                return None
            if isinstance(v, str) and v.startswith("new:"):
                k = v[4:]
                if not k.isdigit() or int(k) >= i or clean[int(k)]["op"] != "add_member":
                    raise Bad(field, "new:<index> of an add_member earlier in this suggestion")
                return v
            if not isinstance(v, str) or v not in members:
                raise Bad(field, "not a member of this squad")
            return v

        for i, op in enumerate(ops):
            f = f"ops.{i}"
            if not isinstance(op, dict):
                raise Bad(f)
            kind = op.get("op")
            if kind == "add_member":
                m = op.get("member")
                self_ = op.get("self", False)
                if set(op) - {"op", "member", "self"} or not isinstance(m, dict) or not isinstance(self_, bool):
                    raise Bad(f, "{op, member, self}")
                if set(m) - set(ADD_FIELDS):
                    raise Bad(f"{f}.member", "only " + ", ".join(ADD_FIELDS) + ": the owner sets the rest")
                m = dict(m)
                if self_:
                    if me is None:
                        raise Bad(f"{f}.self")
                    if me.get("member"):
                        raise Bad(f"{f}.self", "you are a member of this squad already")
                    if any(c["op"] == "add_member" and c["self"] for c in clean):
                        raise Bad(f"{f}.self", "one self-join per suggestion")
                    mine = {"host": self.bus.net.host, "kind": me["harness"],
                            "folder": os.path.realpath(me["cwd"]) if me.get("cwd") else ""}
                    for k, v in mine.items():
                        m.setdefault(k, v)
                        given = os.path.realpath(m[k]) if k == "folder" and isinstance(m[k], str) and m[k] else m[k]
                        if given != v:
                            raise Bad(f"{f}.member.{k}", f"a self-join is for this session: {v or 'no folder'}")
                lead = lead_ref(m.pop("reports_to", None), i, f"{f}.member.reports_to")
                order += 1
                for k in ("name", "role"):
                    m[k] = reports.text(m.get(k), f"{f}.member.{k}", 200)
                m["instructions"] = reports.text(m.get("instructions"), f"{f}.member.instructions",
                                                 squads.INSTRUCTIONS_MAX, lines=True)
                try:
                    doc = squads.clean_member({**m, "order": order, "start": {}})
                except Bad as e:
                    raise Bad(f"{f}.member.{e.extra.get('field', '')}", e.extra.get("reason", "")) from None
                if not self_ and not self._owner_host(doc["host"]):
                    raise Bad(f"{f}.member.host", "one of the owner's own machines that runs tailshare")
                doc["reports_to"] = lead
                if lead is not None:
                    leads.add(f"new:{i}")
                members[f"new:{i}"] = doc
                clean.append({"op": "add_member", "self": self_, "member": _shown(doc)})
                diff.append({"op": "add_member", "self": self_, "member": _shown(doc)})
            elif kind == "remove_member":
                mid = op.get("member")
                if set(op) - {"op", "member"} or not isinstance(mid, str) or mid not in members or mid.startswith("new:"):
                    raise Bad(f"{f}.member", "a member of this squad")
                under = sorted(k for k, m in members.items() if m.get("reports_to") == mid)
                if under:
                    raise Bad(f"{f}.member", f"{len(under)} member(s) report to it: set their reports_to first")
                before = members.pop(mid)
                clean.append({"op": "remove_member", "member": mid})
                diff.append({"op": "remove_member", "member": mid, "name": names.get(mid, ""), "before": _shown(before)})
            elif kind == "set":
                mid, field = op.get("member"), op.get("field")
                if set(op) - {"op", "member", "field", "value"} or "value" not in op:
                    raise Bad(f, "{op, member, field, value}")
                if not isinstance(mid, str) or mid not in members or mid.startswith("new:"):
                    raise Bad(f"{f}.member", "a member of this squad")
                if field not in SET_FIELDS:
                    raise Bad(f"{f}.field", "one of " + ", ".join(SET_FIELDS) + ": the owner sets the rest")
                old = members[mid]
                v = op["value"]
                if field == "reports_to":
                    v = lead_ref(v, i, f"{f}.value")
                    leads.add(mid)
                    new = {**old, "reports_to": v}
                else:
                    if field in ("name", "role", "instructions"):
                        v = reports.text(v, f"{f}.value", squads.INSTRUCTIONS_MAX if field == "instructions" else 200,
                                         lines=field == "instructions")
                    try:
                        new = {**squads.clean_member({**old, field: v, "reports_to": None}), "reports_to": old.get("reports_to")}
                    except Bad as e:
                        raise Bad(f"{f}.value", e.extra.get("reason", "")) from None
                    if field == "instructions":
                        new["instructions_from"] = ""
                    v = new[field]
                members[mid] = new
                clean.append({"op": "set", "member": mid, "field": field, "value": v})
                diff.append({"op": "set", "member": mid, "name": names.get(mid, ""), "field": field,
                             "before": old.get(field), "after": v})
            else:
                raise Bad(f"{f}.op", "add_member, remove_member or set")
        live = [{"id": k, **v} for k, v in members.items()]
        seen_names = {}
        for m in live:
            name = " ".join(m["name"].split()).lower()
            if name in seen_names:
                raise Bad(f"ops.members.{m['id']}.name", f"same name as {seen_names[name]} (case-insensitive)")
            seen_names[name] = m["id"]
        try:
            squads.check_members(live, leads)
        except Bad as e:
            raise Bad(f"ops.{e.extra.get('field', '')}", e.extra.get("reason", "")) from None
        return clean, members, diff, leads

    # -- writing -------------------------------------------------------------------------
    def suggest(self, sid: str, body: dict) -> dict:
        """POST /squads/<id>/suggestions from a live local agent."""
        aid = str(body.get("agent") or "")
        why = reports.text(body.get("why"), "why", WHY_MAX, lines=True, required=True)
        now = time.time()
        with self.store.lock:
            self._expire(now)
            a = self.store.one("SELECT address, harness, cwd, squad FROM agents WHERE id=? AND last_seen>?",
                               aid, now - bus.LIVE)
            if a is None:
                raise bus.BusError("this session is not running on the bus", 404)
            sq = self.squads.squad(sid)
            if sq is None:
                raise bus.BusError("no such squad", 404)
            version = self.squads.sum(sid)
            if body.get("base") != version:
                raise bus.BusError("stale", 409, {"version": version})
            tag = json.loads(a["squad"]) if a["squad"] else {}
            member = tag.get("member") if tag.get("id") == sid else None
            ops, _, diff, _ = self.plan(sq, body.get("ops"), {"harness": a["harness"], "cwd": a["cwd"], "member": member})
            key = hashlib.sha256(json.dumps([sid, version, ops], sort_keys=True).encode()).hexdigest()
            same = self.store.one("SELECT id FROM squad_suggestions WHERE owner=? AND squad=? AND key=? AND status='pending'",
                                  self.squads.owner, sid, key)
            if same:
                return {"suggestion": self.view(self._row(same["id"]))}
            pend = lambda col, v: self.store.one(  # noqa: E731
                f"SELECT COUNT(*) AS n FROM squad_suggestions WHERE {col}=? AND status='pending'", v)["n"]
            if pend("squad", sid) >= PENDING_PER_SQUAD:
                raise bus.BusError("too_many", 429, {"reason": f"this squad has {PENDING_PER_SQUAD} suggestions waiting"})
            if pend("agent", aid) >= PENDING_PER_AGENT:
                raise bus.BusError("too_many", 429, {"reason": f"you have {PENDING_PER_AGENT} suggestions waiting"})
            if self.store.one("SELECT COUNT(*) AS n FROM squad_suggestions WHERE agent=? AND created>?",
                              aid, now - 3600)["n"] >= PER_HOUR:
                raise bus.BusError("too_many", 429, {"reason": f"at most {PER_HOUR} suggestions an hour"})
            sgid = new_id()
            by = {"agent": a["address"], "host": self.bus.net.host, "member": member}
            if member:
                author = next((m for m in sq["members"] if m["id"] == member), None)
                if author:
                    by["name"] = reports.text(author["name"], "by.name", 200)
            self.store.x("INSERT INTO squad_suggestions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)", sgid, self.squads.owner,
                         sid, version, now, "pending", why, json.dumps(by), aid, json.dumps(ops), json.dumps(diff), key)
            return {"suggestion": self.view(self._row(sgid))}

    def apply(self, sid: str, sgid: str, body: dict) -> dict:
        """An owner write: the ops as one squad write, if nothing changed since the suggestion and what was shown."""
        with self.store.lock:
            r = self._mine(sid, sgid)
            if r["status"] != "pending":
                raise bus.BusError("not_pending", 409, {"suggestion": self.view(r)})
            version = self.squads.sum(sid)
            if not (r["base"] == body.get("base") == version):
                raise bus.BusError("stale", 409, {"version": version, "suggestion": self.view(r)})
            sq = self.squads.squad(sid)
            by = json.loads(r["by_"])
            a = self.store.one("SELECT harness, cwd FROM agents WHERE id=?", r["agent"])
            me = {"harness": a["harness"], "cwd": a["cwd"], "member": by["member"]} if a else None
            _, members, _, leads = self.plan(sq, json.loads(r["ops"]), me)
            before = {m["id"] for m in sq["members"] if not m.get("deleted")}
            ids = {k: ("m_" + secrets.token_urlsafe(9) if k.startswith("new:") else k) for k in members}
            now = int(time.time())
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                for mid in before - set(members):
                    self.squads._write_member(sid, mid, None, self.squads.tick())
                old = {m["id"]: m for m in sq["members"]}
                for k, doc in members.items():
                    doc = {**doc, "reports_to": ids.get(doc.get("reports_to"), doc.get("reports_to"))}
                    if k in old and all(doc.get(f) == old[k].get(f) for f in doc if f != "updated"):
                        continue  # untouched: keep its rev
                    self.squads._write_member(sid, ids[k], {**doc, "updated": now}, self.squads.tick())
                self.squads._check_caps(sid, {ids[k]: 1 for k in leads if k in ids})
                self.store.x("UPDATE squad_suggestions SET status='applied', resolved_at=? WHERE id=?", time.time(), sgid)
            except BaseException:
                self.store.db.execute("ROLLBACK")
                raise
            self.store.db.execute("COMMIT")
            return {"suggestion": self.view(self._row(sgid)), "squad": self.squads.squad(sid)}

    def dismiss(self, sid: str, sgid: str) -> dict:
        with self.store.lock:
            r = self._mine(sid, sgid)
            if r["status"] != "pending":
                raise bus.BusError("not_pending", 409, {"suggestion": self.view(r)})
            self.store.x("UPDATE squad_suggestions SET status='dismissed', resolved_at=? WHERE id=?", time.time(), sgid)
            return {"suggestion": self.view(self._row(sgid))}

    # -- reading -------------------------------------------------------------------------
    def _row(self, sgid: str):
        return self.store.one("SELECT * FROM squad_suggestions WHERE id=?", sgid)

    def _mine(self, sid: str, sgid: str):
        self._expire(time.time())
        r = self.store.one("SELECT * FROM squad_suggestions WHERE id=? AND owner=? AND squad=?",
                           _sid(sgid), self.squads.owner, sid)
        if r is None:
            raise bus.BusError("no such suggestion", 404)
        return r

    def _expire(self, now: float) -> None:
        self.store.x("UPDATE squad_suggestions SET status='expired', resolved_at=created+? WHERE status='pending' AND created<?",
                     EXPIRES, now - EXPIRES)

    def view(self, r) -> dict:
        status = r["status"]
        if status == "pending" and r["base"] != self.squads.sum(r["squad"]):
            status = "stale"
        return {"id": r["id"], "squad": r["squad"], "host": self.bus.net.host, "base": r["base"], "created": r["created"],
                "status": status, "why": r["why"], "by": json.loads(r["by_"]), "ops": json.loads(r["ops"]),
                "diff": json.loads(r["diff"]),
                "resolved": None if r["resolved_at"] is None else {"at": r["resolved_at"], "how": r["status"]}}

    def here(self, sid: str) -> dict:
        if self.squads.squad(sid) is None:
            raise bus.BusError("no such squad", 404)
        now = time.time()
        with self.store.lock:
            self._expire(now)
        rows = self.store.q("SELECT * FROM squad_suggestions WHERE owner=? AND squad=? AND (status='pending' OR "
                            "resolved_at>?) ORDER BY created", self.squads.owner, sid, now - KEEP)
        return {"squad": sid, "host": self.bus.net.host, "version": self.squads.sum(sid),
                "suggestions": [self.view(r) for r in rows]}

    def everywhere(self, sid: str) -> dict:
        """This machine's suggestions and those of the owner's other machines that run tailshare."""
        d = self.here(sid)
        missing = []
        for h, p in sorted(self.bus.net.peers.items()):
            if h == self.bus.net.host or p.get("user") != self.squads.owner or not p.get("ip") or not p.get("port") \
                    or not self.store.one("SELECT 1 FROM peers WHERE host=?", h):
                continue
            try:
                got = bus.request(f"http://{p['ip']}:{p['port']}/bus/v1/squads/{sid}/suggestions", timeout=5)
                d["suggestions"] += [s for s in got.get("suggestions") or [] if isinstance(s, dict) and s.get("host") == h]
            except Exception:  # noqa: BLE001
                missing.append(h)
        d["unreachable"] = missing
        return d

    def prune(self) -> None:
        self.store.x("DELETE FROM squad_suggestions WHERE status!='pending' AND resolved_at<?", time.time() - FORGET)

    # -- HTTP: called by Squads.route ------------------------------------------------------
    def read(self, parts: list, local: bool, qs: dict) -> dict:
        sid = squads._id(parts[1], "id")
        if len(parts) == 3:
            return self.everywhere(sid) if local and qs.get("all") == "1" else self.here(sid)
        return {"suggestion": self.view(self._mine(sid, parts[3]))}
