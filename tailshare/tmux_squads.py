"""Squads on plain tmux: read them, show them. The squad-up script (next to this file) creates and runs them.

A squad is a tmux session `squad-<name>` with one window per agent. A window is an agent only if it carries the
`@agent` user option, which squad-up sets; restored shells (tmux-resurrect) and the person's own windows never do.
Everything here only reads tmux: `list-windows -a` once, and `capture-pane -p` for each agent's last line.
docs/tmux-squads.md has the convention.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).with_name("squad-up")
SEP = "\x1f"
FIELDS = (
    "session_name", "window_id", "window_name", "pane_id", "pane_dead", "pane_dead_status", "pane_current_command",
    "@squad", "@squad_dir", "@agent", "@harness", "@cwd", "@keep", "@state", "@stopping", "@tries", "@last_exit",
    "@resume",
)
FORMAT = SEP.join("#{" + f + "}" for f in FIELDS)
PAGE_PORT = int(os.environ.get("TAILSHARE_SQUAD_PAGE_PORT", "8792"))

# Screen text that means the agent is asking the person something (a permission prompt, a question).
ASKING = re.compile(r"do you want|would you like|\(y/n\)|\[y/n\]|press enter|(^|[\s│])(❯|›|>) *\d+\.", re.IGNORECASE)
WORKING = re.compile(r"esc to interrupt", re.IGNORECASE)
CHROME = re.compile(r"^[\s│╭╮╰╯─━┃┏┓┗┛>❯›⏵·•*]*$")  # borders, an empty prompt, spinners
PROMPT = re.compile(r"^(>|❯|›)(\s|$)")  # the input line, with its placeholder text
FOOTER = re.compile(
    r"for shortcuts|for agents|⏵⏵|shift\+tab|bypass permissions|esc to |/effort|· ~/|^ctrl[+-]", re.IGNORECASE
)


def tmux_argv() -> list[str]:
    """The tmux to read; TAILSHARE_SQUAD_TMUX may name another server (e.g. "tmux -S /path") for tests."""
    return os.environ.get("TAILSHARE_SQUAD_TMUX", "tmux").split()


def _run(argv: list[str]) -> str:
    try:
        # the default server, even from inside another server's pane (an inherited $TMUX would pick that one)
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        p = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout if p.returncode == 0 else ""


def state(w: dict) -> str:
    """What a person should read: running, stopping, waiting (between tries), failed, stopped, exited."""
    st = w.get("@state", "")
    if w.get("pane_dead") == "1":
        return st if st in ("failed", "stopped") else "exited"
    if st == "running" and w.get("@keep") == "off" and w.get("@stopping"):
        return "stopping"
    return st or "running"


def discover(tmux: list[str] | None = None) -> list[dict]:
    """Every agent window on the server, as dicts keyed by FIELDS (without the @), plus `state`."""
    out = _run([*(tmux or tmux_argv()), "list-windows", "-a", "-F", FORMAT])
    rows = []
    for line in out.splitlines():
        vals = line.split(SEP)
        if len(vals) != len(FIELDS):
            continue
        w = dict(zip(FIELDS, vals, strict=True))
        if not w["@agent"] or not w["session_name"].startswith("squad-"):
            continue
        w["state"] = state(w)
        rows.append({k.lstrip("@"): v for k, v in w.items()})
    return rows


def screen(pane: str, tmux: list[str] | None = None) -> list[str]:
    return [ln.rstrip() for ln in _run([*(tmux or tmux_argv()), "capture-pane", "-p", "-t", pane]).splitlines()]


def last_line(lines: list[str]) -> str:
    """The last line of real output: not borders, not the empty prompt, not the footer hints."""
    for ln in reversed(lines):
        s = ln.strip().strip("│").strip()
        if not s or CHROME.match(s) or PROMPT.match(s) or FOOTER.search(s):
            continue
        return s[:200]
    return ""


def attention(w: dict, lines: list[str]) -> str:
    """Why this agent needs a person, or ""."""
    st = w["state"]
    if st == "failed":
        return f"stopped restarting after {w.get('tries') or '?'} tries (exit {w.get('last_exit') or '?'})"
    if st == "exited":
        return f"exited ({w.get('pane_dead_status') or '?'}) and is not restarting"
    if st == "running":
        tail = "\n".join(ln for ln in lines if ln.strip())
        tail = "\n".join(tail.splitlines()[-15:])
        if ASKING.search(tail) and not WORKING.search(tail):
            return "is asking something"
    return ""


def snapshot(tmux: list[str] | None = None) -> list[dict]:
    """Squads with their members, each with `last` and `needs`, sorted by squad then window order."""
    squads: dict[str, dict] = {}
    for w in discover(tmux):
        lines = screen(w["pane_id"], tmux) if w["pane_dead"] != "1" else []
        w["last"] = last_line(lines)
        w["needs"] = attention(w, lines)
        sq = squads.setdefault(w["session_name"], {
            "session": w["session_name"], "name": w["squad"] or w["session_name"][len("squad-"):],
            "dir": w["squad_dir"], "members": [],
        })
        sq["members"].append(w)
    return list(squads.values())


# --------------------------------------------------------------------------- the default page

CSS = """
:root{color-scheme:dark light;--bg:#0f1115;--fg:#e6e6e6;--mut:#8b90a0;--card:#171a21;--line:#252a35;--acc:#7dd3fc;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}
@media(prefers-color-scheme:light){:root{--bg:#f7f7f8;--fg:#15171c;--mut:#6b7080;--card:#fff;--line:#e4e6ec;
--acc:#0369a1;--ok:#15803d;--warn:#b45309;--bad:#b91c1c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}p.sub{color:var(--mut);margin:0 0 16px;font-size:13px}
ul{list-style:none;padding:0;margin:0;display:grid;gap:8px}
li{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.top{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}.name{font-weight:600}
.meta{color:var(--mut);font-size:12px}.last{font:12px/1.4 ui-monospace,Menlo,monospace;color:var(--mut);
margin-top:4px;white-space:pre-wrap;word-break:break-word}
.st{font-size:12px;border-radius:6px;padding:1px 6px;border:1px solid var(--line)}
.st.running{color:var(--ok)}.st.waiting,.st.stopping{color:var(--warn)}.st.failed,.st.exited{color:var(--bad)}
.needs li{border-color:var(--warn)}.empty{color:var(--mut)}
"""


def _member(w: dict) -> str:
    e = html.escape
    meta = " · ".join(x for x in (w["harness"], f"tries {w['tries']}" if w["tries"] not in ("", "0") else "") if x)
    return (
        f'<li><div class="top"><span class="name">{e(w["agent"])}</span>'
        f'<span class="st {e(w["state"])}">{e(w["state"])}</span><span class="meta">{e(meta)}</span></div>'
        + (f'<div class="last">{e(w["last"])}</div>' if w["last"] else "")
        + "</li>"
    )


def render(squads: list[dict], title: str = "Squads") -> str:
    e = html.escape
    needs = [(sq, w) for sq in squads for w in sq["members"] if w["needs"]]
    parts = [
        "<!doctype html><html><head><meta charset=utf-8>",
        '<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=15>',
        f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>",
        f"<h1>{e(title)}</h1><p class=sub>from tmux at {time.strftime('%H:%M:%S')}</p>",
        "<h2>Needs you</h2>",
    ]
    if needs:
        parts.append('<ul class="needs">')
        parts += [f'<li><span class="name">{e(w["agent"])}</span> <span class="meta">{e(sq["name"])}</span> '
                  f'{e(w["needs"])}</li>' for sq, w in needs]
        parts.append("</ul>")
    else:
        parts.append('<p class="empty">Nothing.</p>')
    for sq in squads:
        parts.append(f'<h2>{e(sq["name"])} <span class="meta">{e(sq["session"])}</span></h2><ul>')
        parts += [_member(w) for w in sq["members"]]
        parts.append("</ul>")
    if not squads:
        parts.append('<p class="empty">No squad sessions on this tmux server.</p>')
    parts.append("</main></body></html>")
    return "".join(parts)


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_address[1]
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self.send_error(421)  # DNS rebinding: only answer requests addressed to this machine by name
            return
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
        body = render(snapshot()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def serve(bind: str = "127.0.0.1", port: int = PAGE_PORT) -> None:
    srv = ThreadingHTTPServer((bind, port), _Page)
    print(f"squad page on http://{bind}:{srv.server_address[1]}/")
    srv.serve_forever()


# --------------------------------------------------------------------------- CLI

TEMPLATE = """# One squad per folder. Lines: squad <Name> | agent <Name> <claude|codex> <folder> [keep|once] [args...]
#                            | env KEY=VALUE (for every agent)
# keep (the default) restarts an agent that exits; once runs it once. Instructions: .squad/<Name>.md (Claude).
squad {name}
agent {name}Dev claude . keep
"""


def init(folder: Path, name: str) -> list[str]:
    """Copy squad-up into <folder>/.squad and write a squad file if there is none. Never overwrites a squad file."""
    d = folder / ".squad"
    if d.is_symlink():
        raise ValueError(f"{d} is a symlink; refusing")
    d.mkdir(parents=True, exist_ok=True)
    for f in ("squad-up", "squad", ".gitignore", ".squad-up.new"):
        if (d / f).is_symlink():
            raise ValueError(f"{d / f} is a symlink; refusing")
    done = []
    tmp = d / ".squad-up.new"  # replace, never rewrite in place: running windows' sh still reads the old file
    shutil.copyfile(SCRIPT, tmp)
    tmp.chmod(0o755)
    tmp.replace(d / "squad-up")
    done.append(str(d / "squad-up"))
    if not (d / "squad").exists():
        (d / "squad").write_text(TEMPLATE.format(name=name))
        done.append(str(d / "squad"))
    ign = d / ".gitignore"
    if not ign.exists():
        ign.write_text("state/\n")
        done.append(str(ign))
    return done


def cmd(args) -> None:
    import json

    if args.tsq == "ls":
        snap = snapshot()
        if args.json:
            print(json.dumps(snap, indent=2))
            return
        for sq in snap:
            print(f"{sq['name']}  ({sq['session']}, {sq['dir']})")
            for w in sq["members"]:
                flag = f"  ! {w['needs']}" if w["needs"] else ""
                print(f"  {w['agent']:<24} {w['harness']:<7} {w['state']:<9} {w['last'][:60]}{flag}")
        if not snap:
            print("no squad sessions")
    elif args.tsq == "page":
        text = render(snapshot())
        if args.out:
            Path(args.out).write_text(text)
        else:
            print(text)
    elif args.tsq == "serve":
        serve(args.bind, args.port)
    elif args.tsq == "init":
        folder = Path(args.folder).resolve()
        try:
            made = init(folder, args.name or re.sub(r"[^A-Za-z0-9]", "", folder.name) or "Squad")
        except ValueError as e:
            raise SystemExit(f"tmux-squad init: {e}") from None
        for p in made:
            print(p)
    elif args.tsq == "script":
        print(SCRIPT)


def add_parser(sub) -> None:
    p = sub.add_parser("tmux-squad", help="squads on plain tmux (squad-up): list, page, init")
    s = p.add_subparsers(dest="tsq", required=True)
    ls = s.add_parser("ls", help="squad sessions and their agents on this tmux server")
    ls.add_argument("--json", action="store_true")
    pg = s.add_parser("page", help="the default squad page as HTML")
    pg.add_argument("-o", "--out")
    sv = s.add_parser("serve", help="serve the default squad page (local only by default)")
    sv.add_argument("--bind", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=PAGE_PORT)
    it = s.add_parser("init", help="put squad-up and a squad file into a project folder")
    it.add_argument("folder", nargs="?", default=".")
    it.add_argument("--name")
    s.add_parser("script", help="print the path of the squad-up script")
    p.set_defaults(fn=cmd)
