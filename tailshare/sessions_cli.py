"""`tailshare sessions`: find, ask, open and launch sessions on any machine of the tailnet."""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path

from . import agent, bus, sessions

SK_KEYS = ("~/.ssh/id_ed25519_sk", "~/.ssh/id_ecdsa_sk")


def _run(fn):
    def wrapped(args):
        try:
            fn(args)
        except (agent.Offline, bus.BusError, ValueError) as e:
            print(f"✗ {e}", file=sys.stderr)
            sys.exit(1)
    return wrapped


def ref(r: dict) -> str:
    return f"{r['host']}:{r['id'][:8]}"


def describe(r: dict) -> str:
    """One search hit: `host:id  harness  folder  "name"  when  live`."""
    name = r.get("name") or (r.get("first_prompt") or "").replace("\n", " ")[:60]
    live = "  ● live" if r.get("live") else ""
    return f"{ref(r):<22} {r['harness']:<6} {r.get('folder') or '-':<18} {name!r:<50} {agent.ago(r.get('updated') or 0)}{live}"


def format_results(d: dict, show_hosts: bool = True) -> str:
    lines = []
    for r in d["results"]:
        lines.append(describe(r))
        if r.get("snippet"):
            lines.append("    " + " ".join(r["snippet"].split())[:160])
    if not d["results"]:
        lines.append("no sessions found")
    bad = {h: s for h, s in d.get("hosts", {}).items() if s != "ok"}
    if show_hosts and bad:
        lines.append("not searched: " + ", ".join(f"{h} ({s})" for h, s in sorted(bad.items())))
    return "\n".join(lines)


def search(query: str, host: str = "", harness: str = "", folder: str = "", limit: int = 20) -> dict:
    return agent.call("/sessions/search", {"q": query, "host": host or "*", "harness": harness, "folder": folder,
                                           "limit": limit}, timeout=30)


def resolve(r: str) -> dict:
    return agent.call("/sessions/resolve", {"ref": r}, timeout=30)


@_run
def cmd_find(args):
    d = search(" ".join(args.query or []), args.host, args.harness, args.folder, args.limit)
    print(json.dumps(d, indent=1) if args.json else format_results(d))


@_run
def cmd_show(args):
    s = resolve(args.ref)
    if args.json:
        print(json.dumps(s, indent=1))
        return
    when = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"
    print(f"{ref(s)}  ({s['id']})")
    for k, v in (("harness", s["harness"]), ("name", s.get("name") or "-"), ("folder", s["cwd"]),
                 ("branch", s.get("branch") or "-"), ("started", when(s["started"])),
                 ("updated", f"{when(s['updated'])} ({agent.ago(s['updated'])})"), ("prompts", s["prompts"]),
                 ("live", "yes" if s.get("live") else "no")):
        print(f"  {k:<8} {v}")
    print(f"  first    {s['first_prompt'][:300]!r}")
    print(f"  last     {s['last_prompt'][:300]!r}")


def resume_argv(s: dict) -> list[str]:
    if s["harness"] == "claude":
        return ["claude", "--resume", s["id"]]
    return ["codex", "resume", s["id"]]


@_run
def cmd_open(args):
    """Resume a session in this terminal: here if it is on this machine, over SSH otherwise."""
    s = resolve(args.ref)
    argv = resume_argv(s)
    here = agent.call("/machines")["machines"][0]["host"]
    if s["host"] == here and not args.ssh:
        tool = sessions.find_tool(argv[0]) or argv[0]
        os.chdir(s["cwd"])
        print(f"→ {s['cwd']}: {' '.join(argv)}", file=sys.stderr)
        os.execvp(tool, [tool, *argv[1:]])
    target = args.ssh or s["host"]
    remote = f"cd {shlex.quote(s['cwd'])} && exec \"$SHELL\" -lic {shlex.quote(shlex.join(argv))}"
    print(f"→ ssh {target}: {remote}", file=sys.stderr)
    os.execvp("ssh", ["ssh", "-t", target, remote])


@_run
def cmd_ask(args):
    me = agent.register(agent.detect())
    question = " ".join(args.question) if args.question and args.question != ["-"] else sys.stdin.read()
    r = agent.call("/sessions/ask", {"ref": args.ref, "question": question, "from": me["address"]},
                   timeout=sessions.ASK_TIMEOUT + 60)
    if args.json:
        print(json.dumps(r, indent=1))
        return
    print(f"── {r['host']}:{r['id'][:8]} {r.get('name') or ''}".rstrip(), file=sys.stderr)
    print(r["answer"])


def _principal(given: str) -> str:
    if given:
        return given
    net = bus.Net()
    net.refresh()
    if not net.user:
        raise ValueError("cannot tell your tailnet user; pass --as NAME")
    return bus.handle(net.user)


def _key(given: str) -> Path:
    if given:
        return Path(given).expanduser()
    env = os.environ.get("TAILSHARE_SIGN_KEY")
    if env:
        return Path(env).expanduser()
    for k in SK_KEYS:
        if Path(k).expanduser().exists():
            return Path(k).expanduser()
    raise ValueError("no security key found. Make one with `ssh-keygen -t ed25519-sk` (needs a FIDO key), "
                     "or point TAILSHARE_SIGN_KEY / --key at your signing key")


@_run
def cmd_launch(args):
    """Ask a machine to resume a session in the background with real permissions, signed by you."""
    if args.mode not in sessions.MODES:
        raise ValueError(f"--mode must be one of {', '.join(sessions.MODES)}")
    s = resolve(args.ref)
    if s["harness"] != "claude":
        raise ValueError("only Claude Code sessions can be launched so far")
    key = _key(args.key)
    payload = sessions.launch_request(_principal(args.signer), s["host"], s, args.mode, args.prompt or "")
    d = json.loads(payload)
    print("You are approving:", file=sys.stderr)
    print(f"  resume   {s['harness']} session {s['id']} ({s.get('name') or 'unnamed'})", file=sys.stderr)
    print(f"  on       {d['host']}, in {d['cwd']}", file=sys.stderr)
    print(f"  mode     {d['mode']}", file=sys.stderr)
    print(f"  prompt   {d['prompt'][:500]!r}" if d["prompt"] else "  prompt   (none: it waits for you to attach)", file=sys.stderr)
    print(f"  signer   {d['signer']} with {key}, valid {sessions.LAUNCH_TTL // 60} min, once", file=sys.stderr)
    if not args.yes:
        if not sys.stdin.isatty():
            raise ValueError("not a terminal; review the request and pass --yes to sign it")
        if input("Sign it? [y/N] ").strip().lower() not in ("y", "yes"):
            raise ValueError("not signed")
    signature = sessions.sign(payload, key)
    r = agent.call("/sessions/launch", {"payload": payload, "signature": signature}, timeout=120)
    print(f"✓ launched on {r['host']} ({r['mode']})")
    if r.get("output"):
        print(r["output"])
    print(f"attach: ssh -t {r['host']} claude attach {r['id'][:8]}   (or `claude agents` there)")


@_run
def cmd_trust(args):
    """Allow a public key to sign launch requests for this machine."""
    if args.list:
        print(sessions.SIGNERS.read_text() if sessions.SIGNERS.exists() else "no allowed_signers yet: launching is off")
        return
    if not args.pubkey:
        raise ValueError("give a public key file (or - for stdin)")
    pub = sys.stdin.read() if args.pubkey == "-" else Path(args.pubkey).expanduser().read_text()
    line = sessions.trust_line(_principal(args.signer), pub.strip())
    if not sessions.hardware_backed(pub):
        print("⚠ this is not a security key (sk-*) key. If it is a plain file, any agent that can read it can "
              "approve launches. Use a hardware-backed key.", file=sys.stderr)
    path = sessions.SIGNERS
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if line.split()[2:4] in [ln.split()[2:4] for ln in existing.splitlines()]:
        print("✓ already trusted")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(line)
    os.chmod(path, 0o600)
    print(f"✓ {line.split()[0]} may now sign launches on this machine ({path})")


def add_parser(sub) -> None:
    sp = sub.add_parser("sessions", help="find, ask, open and launch Claude Code / Codex sessions on any machine")
    sp.set_defaults(fn=cmd_find, query=[], host="", harness="", folder="", limit=20, json=False)
    s = sp.add_subparsers(dest="sessions_cmd")
    p = s.add_parser("find", help="search sessions on every machine (no query: most recent)")
    p.add_argument("query", nargs="*")
    p.add_argument("--host", default="", help="only machines matching this (glob)")
    p.add_argument("--harness", default="", choices=["", "claude", "codex"])
    p.add_argument("--folder", default="", help="only sessions whose folder contains this")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_find)
    p = s.add_parser("show", help="details of one session (REF is host:id or an id prefix)")
    p.add_argument("ref")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_show)
    p = s.add_parser("open", help="resume a session in this terminal (over SSH if it is elsewhere)")
    p.add_argument("ref")
    p.add_argument("--ssh", default="", help="SSH destination to use for its machine (e.g. an alias)")
    p.set_defaults(fn=cmd_open)
    p = s.add_parser("ask", help="ask a session a question (a throwaway copy with no tools answers)")
    p.add_argument("ref")
    p.add_argument("question", nargs="*", help="omit or '-' to read stdin")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ask)
    p = s.add_parser("launch", help="resume a session in the background on its machine, signed by you")
    p.add_argument("ref")
    p.add_argument("--mode", default="manual", help=f"permission mode: {', '.join(sessions.MODES)}")
    p.add_argument("--prompt", default="", help="what to tell it (none: it waits for you to attach)")
    p.add_argument("--key", default="", help="signing key (default: TAILSHARE_SIGN_KEY or ~/.ssh/id_*_sk)")
    p.add_argument("--as", dest="signer", default="", help="signer name (default: your tailnet user)")
    p.add_argument("-y", "--yes", action="store_true", help="sign without asking (the key may still ask for a touch)")
    p.set_defaults(fn=cmd_launch)
    p = s.add_parser("trust", help="let a public key sign launches on THIS machine")
    p.add_argument("pubkey", nargs="?", help="public key file, or - for stdin")
    p.add_argument("--as", dest="signer", default="", help="signer name (default: your tailnet user)")
    p.add_argument("--list", action="store_true", help="show the keys trusted here")
    p.set_defaults(fn=cmd_trust)
