"""`tailshare squad`: see your squads, and make or edit them in your own terminal (docs/squads.md)."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile

from . import agent, bus, keys


def _get(path: str) -> dict:
    return agent.call(path)


def _write(method: str, path: str, body: dict) -> dict:
    """A squad edit through the local API, with the keys token: only the account tailshare runs as has it."""
    try:
        token = keys.load_token(keys.token_path())
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read {keys.token_path()} ({e}); is this the account tailshare runs as?") from None
    try:
        return bus.request(agent.LOCAL + path, body, 15, {keys.TOKEN_HEADER: token}, method=method)
    except OSError as e:
        raise agent.Offline(f"tailshare mailbox is not running on this machine ({e})") from None


def _human_check() -> None:
    """Agents read squads, they do not change them: a squad says what agents run and with which instructions."""
    agentish = [v for v in keys.AGENT_ENV if os.environ.get(v)]
    if agentish:
        raise ValueError(f"this looks like an AI agent's shell ({', '.join(agentish)} is set). Agents must not edit "
                         "squads: ask the person to run `tailshare squad edit` in their own terminal")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("editing a squad needs a person at a terminal")


def _find(ref: str) -> dict:
    squads = _get("/squads")["squads"]
    hit = [s for s in squads if s["id"] == ref] or [s for s in squads if s["name"].lower() == ref.lower()]
    if len(hit) != 1:
        raise ValueError(f"no squad {ref!r}" if not hit else f"{len(hit)} squads are called {ref!r}: use its id")
    return _get(f"/squads/{hit[0]['id']}")["squad"]


def _print(sq: dict) -> None:
    print(f"{sq['name']}  ({sq['id']}{', ' + sq['color'] if sq.get('color') else ''})")
    for m in sq["members"]:
        role = f" — {m['role']}" if m.get("role") else ""
        print(f"  {m['name']}{role}: {m['kind']} on {m['host']} in {m['folder']}  {m.get('agent') or 'not running'}")
    for s in sq.get("shares") or []:
        print(f"  shared with {s['user']}{' (with folders)' if s.get('show_paths') else ''}")


def cmd_squad(args):
    tag = agent.squad_tag()
    if not tag:
        print("This session is not part of a squad. `tailshare squad ls` lists your squads.")
        return
    sq = _get(f"/squads/{tag['id']}")["squad"]
    if args.json:
        print(json.dumps(sq, indent=2))
        return
    me = next((m for m in sq["members"] if m["id"] == tag["member"]), None)
    print(f"You are {me['name'] if me else tag['member']} in squad {sq['name']}.")
    _print(sq)


def cmd_ls(args):
    squads = _get("/squads")["squads"]
    if args.json:
        print(json.dumps(squads, indent=2))
        return
    if not squads:
        print("No squads. `tailshare squad new NAME` makes one.")
    for s in squads:
        live = sum(1 for m in s["members"] if m.get("agent"))
        print(f"{s['id']}  {s['name']}  {len(s['members'])} members, {live} running")


def cmd_show(args):
    sq = _find(args.ref)
    if args.json:
        print(json.dumps(sq, indent=2))
    else:
        _print(sq)


def cmd_new(args):
    _human_check()
    sid = "sq_" + secrets.token_urlsafe(16)
    sq = _write("PUT", f"/squads/{sid}", {"name": args.name, "base_rev": None})["squad"]
    print(f"Made squad {sq['name']} ({sq['id']}). `tailshare squad edit {sq['id']}` adds members.")


EDITABLE = ("name", "color", "icon", "shares")
MEMBER_EDITABLE = ("name", "role", "icon", "host", "kind", "folder", "instructions", "instructions_from", "start")


def _editable(sq: dict) -> dict:
    return {**{k: sq.get(k) for k in EDITABLE},
            "members": [{"id": m["id"], **{k: m.get(k) for k in MEMBER_EDITABLE}} for m in sq["members"]]}


def changes(sq: dict, new: dict) -> list[tuple[str, str, dict]]:
    """(method, path, body) for each register that the edit changed, each with the rev it was read at."""
    if not isinstance(new, dict) or not isinstance(new.get("members"), list):
        raise ValueError("the squad must stay a JSON object with a members list")
    sid, old = sq["id"], {m["id"]: m for m in sq["members"]}
    out = []
    head = {k: new.get(k) for k in EDITABLE if new.get(k) != sq.get(k)}
    if head:
        out.append(("PATCH", f"/squads/{sid}", {**head, "base_rev": sq["rev"]}))
    seen = set()
    for i, m in enumerate(new["members"]):
        if not isinstance(m, dict):
            raise ValueError(f"member {i + 1} is not an object")
        mid = m.get("id") or "m_" + secrets.token_urlsafe(9)
        if mid in seen:
            raise ValueError(f"two members with id {mid}")
        seen.add(mid)
        fields = {k: m.get(k) for k in MEMBER_EDITABLE if k in m}
        fields["order"] = i
        if mid not in old:
            out.append(("PATCH", f"/squads/{sid}/members/{mid}", {**fields, "base_rev": None}))
            continue
        was = {k: old[mid].get(k) for k in (*MEMBER_EDITABLE, "order")}
        diff = {k: v for k, v in fields.items() if v != was.get(k)}
        st, was_st = diff.get("start"), was.get("start") or {}
        if isinstance(st, dict) and st.get("prompt") != was_st.get("prompt") and st.get("prompt_from") == was_st.get("prompt_from"):
            diff["start"] = {k: v for k, v in st.items() if k != "prompt_from"}  # your edit: no longer "from" anyone
        if diff:
            out.append(("PATCH", f"/squads/{sid}/members/{mid}", {**diff, "base_rev": old[mid]["rev"]}))
    for mid in old.keys() - seen:
        out.append(("PATCH", f"/squads/{sid}/members/{mid}", {"deleted": True, "base_rev": old[mid]["rev"]}))
    return out


def cmd_edit(args):
    _human_check()
    sq = _find(args.ref)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".json", prefix="squad-", delete=False) as f:
        json.dump(_editable(sq), f, indent=2)
        path = f.name
    try:
        os.chmod(path, 0o600)
        if subprocess.run([*editor.split(), path]).returncode != 0:
            raise ValueError("the editor failed; nothing changed")
        with open(path) as f:
            new = json.load(f)
    finally:
        os.unlink(path)
    todo = changes(sq, new)
    if not todo:
        print("No changes.")
        return
    for method, path, body in todo:
        try:
            _write(method, path, body)
        except bus.BusError as e:
            if e.code == 409:
                raise ValueError(f"{path.rsplit('/', 1)[-1]} changed somewhere else since you opened it; nothing "
                                 "after it was saved. Run `tailshare squad edit` again.") from None
            field = e.extra.get("field")
            raise ValueError(f"{e}{' (' + field + ')' if field else ''}{': ' + e.extra['reason'] if e.extra.get('reason') else ''}") from None
    print(f"Saved {len(todo)} change{'s' * (len(todo) != 1)} to {sq['name']}.")


def add_parser(sub) -> None:
    p = sub.add_parser("squad", help="squads: named groups of agents with roles")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_squad)
    s = p.add_subparsers(dest="squad_cmd")
    q = s.add_parser("ls", help="your squads")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_ls)
    q = s.add_parser("show", help="one squad, its members and who runs where")
    q.add_argument("ref", help="the squad's name or id")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_show)
    q = s.add_parser("new", help="make a squad (in your own terminal)")
    q.add_argument("name")
    q.set_defaults(fn=cmd_new)
    q = s.add_parser("edit", help="edit a squad as JSON in $EDITOR (in your own terminal)")
    q.add_argument("ref", help="the squad's name or id")
    q.set_defaults(fn=cmd_edit)
