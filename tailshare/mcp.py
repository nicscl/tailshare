"""Minimal MCP server (stdio, JSON-RPC 2.0) exposing the agent bus as tools.

Hand-written to keep tailshare free of extra dependencies; it implements only what a
tools-only server needs: initialize, tools/list, tools/call and ping.
"""

from __future__ import annotations

import json
import os
import sys

from . import __version__, agent, bus, reports, sessions, sessions_cli, squads_cli

TOOLS = [
    {
        "name": "squad",
        "description": ("The squad this session belongs to (its members, their roles and agent addresses), or with "
                        "`squad` (a name or id) another of the user's squads. action:suggest proposes ops with why; "
                        "only the owner can apply or dismiss them. Omitting action reads the squad."),
        "inputSchema": {"type": "object", "properties": {
            "squad": {"type": "string"}, "action": {"type": "string", "enum": ["show", "suggest"]},
            "why": {"type": "string", "maxLength": 600},
            "ops": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
                "type": "object", "description": "add_member: member object, self bool; remove_member: member id; "
                "set: member id, field (name, role, icon, instructions, reports_to), value. "
                "Only add_member may set host/kind/folder. No start or shares.",
                "properties": {"op": {"type": "string", "enum": ["add_member", "remove_member", "set"]},
                               "member": {"oneOf": [{"type": "string"}, {"type": "object"}]},
                               "self": {"type": "boolean"}, "field": {"type": "string", "enum": [
                                   "name", "role", "icon", "instructions", "reports_to"]}, "value": {}},
                "required": ["op", "member"], "additionalProperties": False}}}},
    },
    {
        "name": "report",
        "description": ("Your own report on your squad's page, which its owner reads instead of your session. With no "
                        "fields it returns your report. The fields you pass replace those fields: bio, now (≤3 tasks), "
                        "next (≤5), done (today's), waiting_on, blockers (the whole list; keep an id to keep a "
                        "blocker, resolved:true once you have the answer), notes (leads only, Markdown). A task is "
                        "{title, url?}. A blocker is {id, kind: question|approval, title, why, options[≤4], to: "
                        "lead|owner, for?: {member, blocker}, approval?}. With squad:true, every squadmate's report "
                        "instead (how a lead sees its branch's blockers)."),
        "inputSchema": {"type": "object", "properties": {
            "bio": {"type": "string"},
            "now": {"type": "array", "items": {"type": "object"}},
            "next": {"type": "array", "items": {"type": "object"}},
            "done": {"type": "array", "items": {"type": "object"}},
            "waiting_on": {"type": ["object", "null"]},
            "blockers": {"type": "array", "items": {"type": "object"}},
            "notes": {"type": "string"},
            "squad": {"type": "boolean"}}},
    },
    {
        "name": "whoami",
        "description": "Your address on the tailshare agent bus (host/harness/folder[/session]).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "agents",
        "description": "List AI agents on this machine and the rest of the tailnet, with whether they are online.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "send",
        "description": (
            "Send a message to another agent on the tailnet. `to` is an address (host/harness/folder[/session]), "
            "a prefix such as host/harness/folder (every session in that folder), a pattern like */*/myproject, "
            "or @user / @user/<pattern> for the sessions of one person. "
            "If the machine is offline the message is queued and delivered when it comes back."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "message": {"type": "string"},
                "files": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Local file paths to share over tailshare and attach as links.",
                },
            },
            "required": ["to", "message"],
        },
    },
    {
        "name": "inbox",
        "description": "Read (and mark read) new messages from other agents. Optionally wait up to 50 s for one.",
        "inputSchema": {"type": "object", "properties": {"wait": {"type": "number", "minimum": 0, "maximum": 50}}},
    },
    {
        "name": "find_sessions",
        "description": (
            "Search past and current Claude Code and Codex sessions on every machine of the user's tailnet, by "
            "words from their prompts, replies, name or folder. Empty query: the most recent. Each hit has a ref "
            "(host:id) for ask_session, and `tailshare sessions open <ref>` resumes it in a terminal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "host": {"type": "string", "description": "only machines matching this glob"},
                "harness": {"type": "string", "enum": ["", "claude", "codex"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
    {
        "name": "ask_session",
        "description": (
            "Ask another session (from find_sessions) a question about its work, e.g. why it chose something. "
            "A throwaway copy of it answers with no tools; the original session is not changed. Takes up to a few minutes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session": {"type": "string", "description": "ref: host:id or an id prefix"},
                           "question": {"type": "string"}},
            "required": ["session", "question"],
        },
    },
]

INSTRUCTIONS = (
    "Tools for messaging other AI agents (Claude Code, Codex) on the user's tailnet. " + agent.NOTE
)


def _identity() -> agent.Identity:
    """Our session. Claude Code may not pass the session id to MCP servers, so fall back to
    the session the SessionStart hook registered for our parent process."""
    if os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or os.environ.get("TAILSHARE_AGENT_ID"):
        return agent.detect()
    found = agent.call(f"/agents?pid={os.getppid()}").get("agent")
    if found and found.get("harness") == "claude":
        return agent.detect({"session_id": found["id"], "cwd": found["cwd"]})
    return agent.detect()


def _tool(name: str, args: dict) -> str:
    me = agent.register(_identity())
    if name == "whoami":
        return me["address"]
    if name == "agents":
        rows = agent.call("/agents")["agents"]
        lines = [
            f"{a['address']}  user {a.get('user') or '-'}  {'(you)' if a['address'] == me['address'] else 'online' if a.get('online') else 'offline, seen ' + agent.ago(a.get('last_seen', 0))}"
            for a in rows
        ]
        return "\n".join(lines) or "no agents yet"
    if name == "send":
        files = agent.share_files(args.get("files") or []) if args.get("files") else []
        r = agent.call("/send", {"from": me["id"], "to": args["to"], "body": args["message"], "files": files}, timeout=30)
        return "\n".join(f"{x['status']}: {x['host']}" for x in r["routes"]) or "no matching machines"
    if name == "inbox":
        wait = min(float(args.get("wait") or 0), 50)
        msgs = agent.call(f"/inbox?agent={me['id']}&wait={wait}", timeout=wait + 10)["messages"]
        if not msgs:
            return "no new messages"
        return "\n\n".join(agent.format_message(m) for m in msgs) + f"\n\n({agent.NOTE})"
    if name == "find_sessions":
        d = sessions_cli.search(str(args.get("query") or ""), str(args.get("host") or ""),
                                str(args.get("harness") or ""), limit=int(args.get("limit") or 15))
        return sessions_cli.format_results(d)
    if name == "ask_session":
        r = agent.call("/sessions/ask", {"ref": args["session"], "question": args["question"], "from": me["address"]},
                       timeout=sessions.ASK_TIMEOUT + 60)
        return f"{r['host']}:{r['id'][:8]} {r.get('name') or ''} answered:\n\n{r['answer']}\n\n({agent.NOTE})"
    if name == "report":
        if args.get("squad"):
            tag = me.get("squad") or {}
            if not tag:
                return "This session is not part of a squad, so it has no report."
            d = agent.call(f"/reports?squad={tag['id']}&all=1", timeout=30)
            return json.dumps(d, indent=1) + f"\n\n({agent.NOTE})"
        fields = {k: args[k] for k in reports.FIELDS if k in args}
        if not fields:
            return json.dumps(agent.call(f"/report?agent={me['id']}"), indent=1)
        return json.dumps(agent.call("/report", {"agent": me["id"], **fields})["report"], indent=1)
    if name == "squad":
        ref = str(args.get("squad") or "")
        action = args.get("action", "show")
        if action == "suggest":
            return json.dumps(squads_cli.suggest(ref, args.get("ops"), args.get("why"), me), indent=2)
        if action != "show":
            raise ValueError("squad action must be show or suggest")
        if ref:
            return json.dumps(squads_cli._find(ref), indent=2)
        tag = me.get("squad") or agent.squad_tag()
        if not tag:
            return "This session is not part of a squad. Squads: " + json.dumps(
                [{"id": s["id"], "name": s["name"]} for s in agent.call("/squads")["squads"]])
        return json.dumps({"you": tag["member"], "squad": agent.call(f"/squads/{tag['id']}")["squad"]}, indent=2)
    raise ValueError(f"unknown tool {name}")


def _handle(req: dict) -> dict | None:
    method, rid, params = req.get("method"), req.get("id"), req.get("params") or {}
    if rid is None:  # a notification
        return None
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tailshare", "version": __version__},
            "instructions": INSTRUCTIONS,
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            text, err = _tool(params.get("name", ""), params.get("arguments") or {}), False
        except (agent.Offline, bus.BusError, ValueError, KeyError, OSError) as e:
            text, err = f"error: {e}", True
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = _handle(req)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
