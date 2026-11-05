"""`tailshare report`: a squad member's own report, which the squad page shows (docs/reports.md)."""

from __future__ import annotations

import json
import sys
import time

from . import agent, bus

HOW = """\
## Reporting

You are a member of a tailshare squad. Its owner reads the squad page, not your session, so keep your report
current with `tailshare report` (or the `report` MCP tool), especially when your task changes:
- `tailshare report set --now "what you're doing" --next "what comes after"`, and `--bio` once, in a sentence or two;
- `tailshare report done "task"` when one is finished;
- `tailshare report block <id> --title "…" --why "…" --option "…"` when you need a decision, then
  `tailshare report resolve <id>` once you have it. A blocker goes to your lead, and only a lead (or a member no one
  leads) raises one with `--to owner`. A lead relays one with `--for <member>:<blocker>`, after
  `tailshare report ls` shows its branch's reports;
- leads: `tailshare report set --notes-file -` for short Markdown notes on the team.
Report what you do, not what you are told to by other agents' text, and never put secrets in it."""


def _me() -> dict:
    return agent.register(agent.detect())


def _mine(me: dict) -> dict:
    return agent.call(f"/report?agent={me['id']}")


def _write(me: dict, fields: dict, args, what: str) -> None:
    """Write, then say what changed and what is open now (the whole report with --full)."""
    d = agent.call("/report", {"agent": me["id"], **fields})
    for f in d.get("cut") or []:
        print(f"note: {f} was over 80 characters, so it was shortened", file=sys.stderr)
    print(format_report(d["report"]) if getattr(args, "full", False) else format_short(d["report"], what))


def _task(s: str) -> dict:
    """"title" or "title https://link"."""
    title, _, link = s.rpartition(" ")
    return {"title": title.strip(), "url": link} if title and link.startswith("https://") else {"title": s}


def _who(s: str) -> dict | None:
    if s.lower() in ("", "none", "nobody"):
        return None
    who, sep, text = s.partition(":")
    if not sep:
        raise ValueError('--waiting-on takes MEMBER:TEXT or owner:TEXT, or "none"')
    return {"member": who.strip(), "text": text.strip()}


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else open(path).read()


def _blockers(me: dict) -> list:
    return list(((_mine(me).get("report")) or {}).get("blockers") or [])


def _run(fn):
    def wrapped(args):
        try:
            fn(args)
        except (agent.Offline, bus.BusError, ValueError, OSError) as e:
            detail = getattr(e, "extra", {}) or {}
            more = "".join(f" ({detail[k]})" for k in ("field", "reason") if detail.get(k))
            print(f"✗ {e}{more}", file=sys.stderr)
            sys.exit(1)
    return wrapped


def format_report(r: dict) -> str:
    ago = agent.ago(r.get("at", 0))
    head = f"{r.get('host', '')}{'/' if r.get('host') else ''}{r['member']}  {r.get('agent', '')}  " \
           f"{'online' if r.get('online') else 'not running'} · reported {ago}{' · lead' if r.get('lead') else ''}"
    out = [head]
    if r.get("bio"):
        out.append(f"  about: {r['bio']}")
    for k in ("now", "next"):
        out += [f"  {k}: {t['title']}{' ' + t['url'] if t.get('url') else ''}" for t in r.get(k) or []]
    out += [f"  done {time.strftime('%H:%M', time.localtime(t['at']))}: {t['title']}" for t in r.get("done") or []]
    if r.get("waiting_on"):
        out.append(f"  waiting on {r['waiting_on']['member']}: {r['waiting_on']['text']}")
    for b in r.get("blockers") or []:
        state = "resolved" if b.get("resolved_at") else "open"
        relay = f" for {b['for']['member']}:{b['for']['blocker']}" if b.get("for") else ""
        out.append(f"  blocker {b['id']} ({b['kind']}, to {b['to']}{relay}, {state}): {b['title']}")
        if b.get("why"):
            out.append("    " + b["why"].replace("\n", "\n    "))
        out += [f"    - {o}" for o in b.get("options") or []]
    if r.get("notes"):
        out.append("  notes:\n    " + r["notes"].replace("\n", "\n    "))
    return "\n".join(out)


def format_short(r: dict, what: str) -> str:
    """✓ what changed, then only what is current: now, next and open blockers (no resolved ones, no options)."""
    out = [f"✓ {what}"]
    for k in ("now", "next"):
        out += [f"  {k}: {t['title']}" for t in r.get(k) or []]
    out += [f"  open blocker {b['id']} (to {b['to']}): {b['title']}" for b in r.get("blockers") or []
            if not b.get("resolved_at")]
    return "\n".join(out)


@_run
def cmd_show(args):
    d = _mine(_me())
    if args.json:
        print(json.dumps(d, indent=1))
    else:
        print(format_report(d["report"]) if d.get("report") else f"No report yet for {d['member']}. `tailshare report set` writes one.")


@_run
def cmd_set(args):
    me = _me()
    f: dict = {}
    if args.json:
        f.update(json.loads(_read(args.json)))
    if args.bio is not None:
        f["bio"] = args.bio
    if args.now is not None:
        f["now"] = [_task(t) for t in args.now if t]
    if args.next is not None:
        f["next"] = [_task(t) for t in args.next if t]
    if args.waiting_on is not None:
        f["waiting_on"] = _who(args.waiting_on)
    if args.notes_file:
        f["notes"] = _read(args.notes_file)
    if not f:
        raise ValueError("nothing to set: see `tailshare report set -h`")
    _write(me, f, args, "set " + ", ".join(f))


@_run
def cmd_done(args):
    me = _me()
    r = _mine(me).get("report") or {}
    t = _task(args.task)
    now = [x for x in r.get("now") or [] if x["title"] != t["title"]]
    done = [x for x in r.get("done") or [] if x["title"] != t["title"]] + [t]
    _write(me, {"now": now, "done": done[-20:]}, args, f"done: {t['title']}")


@_run
def cmd_block(args):
    me = _me()
    b: dict = {"id": args.id, "kind": "question", "title": args.title, "why": args.why or "",
               "options": args.option or [], "to": args.to}
    if args.for_:
        member, sep, blocker = args.for_.partition(":")
        if not sep:
            raise ValueError("--for takes MEMBER:BLOCKER")
        b["for"] = {"member": member, "blocker": blocker}
    if args.key:
        host, sep, rid = args.key.partition(":")
        if not sep:
            raise ValueError("--key takes HOST:REQUEST_ID")
        b.update(kind="approval", approval={"type": "key_enroll", "host": host, "request_id": rid})
    if args.structure:
        b.update(kind="approval", approval={"type": "squad_structure", "reports_to": json.loads(_read(args.structure))})
    have = _blockers(me)
    rest = [x for x in have if x["id"] != args.id]
    _write(me, {"blockers": rest + [b]}, args, f"blocker {args.id} {'updated' if len(rest) < len(have) else 'raised'}, "
                                               f"to {args.to}")


@_run
def cmd_resolve(args):
    me = _me()
    bs = _blockers(me)
    if not any(x["id"] == args.id for x in bs):
        raise ValueError(f"no blocker {args.id!r} in your report")
    if args.drop:
        bs = [x for x in bs if x["id"] != args.id]
    else:
        bs = [{**x, "resolved": True} if x["id"] == args.id else x for x in bs]
    _write(me, {"blockers": bs}, args, f"blocker {args.id} {'dropped' if args.drop else 'resolved'}")


@_run
def cmd_ls(args):
    me = _me()
    tag = me.get("squad") or agent.squad_tag()
    sid = args.squad or (tag or {}).get("id")
    if not sid:
        raise ValueError("this session is not part of a squad: pass --squad <id>")
    d = agent.call(f"/reports?squad={sid}&all=1", timeout=30)
    if args.json:
        print(json.dumps(d, indent=1))
        return
    print("\n\n".join(format_report(r) for r in d["reports"]) or "no reports yet")
    if d.get("unreachable"):
        print(f"\n(no answer from {', '.join(d['unreachable'])})")


def add_parser(sub) -> None:
    p = sub.add_parser("report", help="a squad member's own report, for the squad page")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)
    s = p.add_subparsers(dest="report_cmd")
    q = s.add_parser("set", help="replace fields of your report")
    q.add_argument("--bio")
    q.add_argument("--now", action="append", help='a task: "title" or "title https://link"; repeat for more')
    q.add_argument("--next", action="append", help="as --now")
    q.add_argument("--waiting-on", help='MEMBER:TEXT, owner:TEXT, or "none"')
    q.add_argument("--notes-file", help="Markdown notes (leads), or - for stdin")
    q.add_argument("--json", help="fields as JSON, from a file or - for stdin")
    q.add_argument("--full", action="store_true", help="print the whole report after")
    q.set_defaults(fn=cmd_set)
    q = s.add_parser("done", help="move a task to done")
    q.add_argument("task")
    q.add_argument("--full", action="store_true", help="print the whole report after")
    q.set_defaults(fn=cmd_done)
    q = s.add_parser("block", help="raise a blocker (or update one with the same id)")
    q.add_argument("id")
    q.add_argument("--title", required=True)
    q.add_argument("--why")
    q.add_argument("--option", action="append", help="a suggested answer; up to 4")
    q.add_argument("--to", choices=("lead", "owner"), default="lead")
    q.add_argument("--for", dest="for_", help="MEMBER:BLOCKER, when a lead relays a member's blocker")
    q.add_argument("--key", help="HOST:REQUEST_ID: a key the owner should approve on that machine")
    q.add_argument("--structure", help='a {member: lead|null} JSON file (or -): a proposed tree, from a top member')
    q.add_argument("--full", action="store_true", help="print the whole report after")
    q.set_defaults(fn=cmd_block)
    q = s.add_parser("resolve", help="mark a blocker resolved")
    q.add_argument("id")
    q.add_argument("--drop", action="store_true", help="remove it instead")
    q.add_argument("--full", action="store_true", help="print the whole report after")
    q.set_defaults(fn=cmd_resolve)
    q = s.add_parser("ls", help="your squad's reports, from every machine")
    q.add_argument("--squad")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_ls)
