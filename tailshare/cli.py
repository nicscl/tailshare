"""Command-line entry point. No arguments → interactive TUI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, agent, core, daemon, sessions_cli


def _base_url() -> str:
    return core.tailscale_info().base_url


def cmd_serve(_):
    from .server import serve

    serve()


def cmd_install(_):
    ok, msg = daemon.start()
    print(("✓ " if ok else "✗ ") + msg)
    if ok:
        print(f"  {_base_url() or '(tailscale offline)'}")
    sys.exit(0 if ok else 1)


def cmd_uninstall(_):
    ok, msg = daemon.uninstall()
    print(("✓ " if ok else "✗ ") + msg)


def cmd_status(_):
    ts = core.tailscale_info()
    srv = core.server_status()
    print(f"share dir   {core.SHARE_DIR}  ({len(core.list_shared())} files)")
    print(f"server      {srv} on {core.BIND}:{core.PORT}  launchd: {'loaded' if daemon.loaded() else 'not loaded'}")
    if core.BASE_URL:  # no tailscale CLI / serve; the server is bound to a tailnet IP directly
        print(f"tailscale   links set by TAILSHARE_BASE_URL (bound to {core.BIND})")
        print(f"links       {core.BASE_URL}")
        return
    print(f"tailscale   {'ok · ' + ts.dns_name if ts.ok else 'offline · ' + ts.error}")
    routes = core.serve_routes() or {}
    print(f"serve       http/80 {'ok' if routes.get('ip') else 'MISSING'} · https/443 {'ok' if routes.get('dns') else 'MISSING'}  (fix: tailshare install)")
    if ts.ok:
        print(f"links       {ts.base_url}   [{core.LINK_MODE}; TAILSHARE_LINKS=dns for {ts.dns_url}]")


def cmd_ls(_):
    base = _base_url()
    for f in core.list_shared():
        print(f"{core.human_size(f.size):>8}  {core.relative_time(f.mtime):>10}  {core.url_for(base, f.name)}")


def cmd_add(args):
    base = _base_url()
    for raw in args.paths:
        found, missing = core.parse_dropped_paths(raw)
        for m in missing:
            print(f"✗ not found: {m}", file=sys.stderr)
        for p in found:
            f = core.add_path(p)
            url = core.url_for(base, f.name)
            print(url)
            if args.copy:
                core.copy_to_clipboard(url)


def cmd_paste(args):
    base = _base_url()
    shared = core.paste_clipboard()
    if not shared:
        print("✗ nothing shareable on the clipboard", file=sys.stderr)
        sys.exit(1)
    for f in shared:
        url = core.url_for(base, f.name)
        print(url)
    if args.copy:
        core.copy_to_clipboard(core.url_for(base, shared[-1].name))


def cmd_rm(args):
    failed = False
    for name in args.names:
        if core.remove_shared(name):
            print(f"removed {name}")
        else:
            print(f"✗ not shared: {name}", file=sys.stderr)
            failed = True
    sys.exit(1 if failed else 0)


def cmd_url(args):
    print(core.url_for(_base_url(), args.name))


def cmd_mcp(_):
    from .mcp import serve

    serve()


def cmd_update(args):
    try:
        r = agent.call("/update", {"all": args.all}, timeout=900)
    except (agent.Offline, OSError) as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)
    print(f"this machine   {r['self'].get('result')}")
    for host, status in sorted(r["peers"].items()):
        print(f"{host:<14} {status}")
    if args.all and r["peers"]:
        print("watch them come up with: tailshare machines")


def cmd_machines(_):
    try:
        rows = agent.call("/machines")["machines"]
    except (agent.Offline, OSError) as e:
        print(f"✗ {e}", file=sys.stderr)
        sys.exit(1)
    w = max(len(m["host"]) for m in rows)
    for m in rows:
        state = "this machine" if m["self"] else ("online" if m["online"] else "offline") + f" · answered {core.relative_time(m['last_ok'])}"
        print(f"{m['host']:<{w}}  {m['version'] or '?':<16} {m['agents']:>2} agents  {state}")
        if m.get("update"):
            print(f"{'':<{w}}  last update check: {m['update']['result']}")


def cmd_tui(_):
    from .app import TailshareApp

    TailshareApp().run()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tailshare", description="Drop files into your tailnet from the terminal.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers()
    sub.add_parser("serve", help="run the share server in the foreground").set_defaults(fn=cmd_serve)
    sub.add_parser("install", help="install + start the launchd agent and tailscale serve").set_defaults(fn=cmd_install)
    sub.add_parser("uninstall", help="remove the launchd agent").set_defaults(fn=cmd_uninstall)
    sub.add_parser("status", help="show server / tailscale state").set_defaults(fn=cmd_status)
    sub.add_parser("ls", help="list shared files with URLs").set_defaults(fn=cmd_ls)
    a = sub.add_parser("add", help="share files or folders (folders are zipped)")
    a.add_argument("paths", nargs="+")
    a.add_argument("-c", "--copy", action="store_true", help="copy the URL to the clipboard")
    a.set_defaults(fn=cmd_add)
    pa = sub.add_parser("paste", help="share the clipboard (image, Finder files or a path)")
    pa.add_argument("-c", "--copy", action="store_true", help="copy the URL to the clipboard")
    pa.set_defaults(fn=cmd_paste)
    r = sub.add_parser("rm", help="remove shared files by name")
    r.add_argument("names", nargs="+")
    r.set_defaults(fn=cmd_rm)
    u = sub.add_parser("url", help="print the URL of a shared file")
    u.add_argument("name")
    u.set_defaults(fn=cmd_url)
    agent.add_parser(sub)
    sessions_cli.add_parser(sub)
    up = sub.add_parser("update", help="update tailshare from its git source now")
    up.add_argument("--all", action="store_true", help="also tell every other machine to update")
    up.set_defaults(fn=cmd_update)
    sub.add_parser("machines", help="machines running tailshare, with versions").set_defaults(fn=cmd_machines)
    sub.add_parser("mcp", help="MCP server (stdio) for the agent bus").set_defaults(fn=cmd_mcp)
    p.set_defaults(fn=cmd_tui)
    args = p.parse_args(argv)
    args.fn(args)
