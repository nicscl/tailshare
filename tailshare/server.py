"""Local HTTP server for the share folder. Bound to 127.0.0.1; tailscale serve fronts it."""

from __future__ import annotations

import html
import json
import sys
import urllib.parse
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .core import HEALTH_BODY, HEALTH_PATH, PORT, SharedFile, ensure_share_dir, human_size, list_shared

INDEX_CSS = """
:root{color-scheme:dark light;--bg:#0f1115;--fg:#e6e6e6;--mut:#8b90a0;--card:#171a21;--line:#252a35;--acc:#7dd3fc}
@media(prefers-color-scheme:light){:root{--bg:#f7f7f8;--fg:#15171c;--mut:#6b7080;--card:#fff;--line:#e4e6ec;--acc:#0369a1}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:18px;font-weight:600;margin:0 0 4px;display:flex;align-items:center;gap:8px}
h1 small{color:var(--mut);font-weight:400;font-size:13px}
p.sub{color:var(--mut);margin:0 0 20px;font-size:13px}
ul{list-style:none;padding:0;margin:0;display:grid;gap:10px}
li{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;display:grid;grid-template-columns:44px minmax(0,1fr);gap:12px;align-items:center}
.act{grid-column:1/-1;display:flex;gap:8px;flex-wrap:wrap}
.act a,.act button{flex:1;min-height:44px;display:flex;align-items:center;justify-content:center;text-decoration:none;color:var(--acc);border:1px solid var(--line);border-radius:10px;font:inherit;font-size:14px;background:transparent}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
header a{color:var(--mut);font-size:13px;text-decoration:none}
li img{width:44px;height:44px;object-fit:cover;border-radius:8px;background:var(--line)}
.ic{width:44px;height:44px;border-radius:8px;background:var(--line);display:grid;place-items:center;font-size:18px;color:var(--mut)}
a.name{color:var(--fg);text-decoration:none;font-weight:500;word-break:break-all}
a.name:hover{color:var(--acc)}
.meta{color:var(--mut);font-size:12px;margin-top:2px}
button{cursor:pointer}button:active,.act a:active{opacity:.6}
.empty{color:var(--mut);text-align:center;padding:48px 0}
"""

INDEX_JS = """
async function cp(btn,u){try{await navigator.clipboard.writeText(u);btn.textContent='Copied';setTimeout(()=>btn.textContent='Copy link',1200)}catch(e){prompt('Copy link',u)}}
"""

ICONS = {"image": "🖼", "video": "🎬", "audio": "🎵", "sheet": "📊", "doc": "📄", "archive": "🗜", "code": "‹›", "file": "📎"}


def render_index(files: list[SharedFile], host: str) -> str:
    items = []
    for f in files:
        href = urllib.parse.quote(f.name)
        name = html.escape(f.name)
        when = datetime.fromtimestamp(f.mtime).strftime("%b %d, %H:%M")
        thumb = (
            f'<img src="{href}" alt="" loading="lazy">'
            if f.kind == "image" and f.ext != ".heic"
            else f'<div class="ic">{ICONS[f.kind]}</div>'
        )
        items.append(
            f"<li>{thumb}<div><a class=name href=\"{href}\">{name}</a>"
            f"<div class=meta>{human_size(f.size)} · {when}</div></div>"
            f"<div class=act><a href=\"{href}\">Open</a><a href=\"{href}\" download>Download</a>"
            f"<button onclick=\"cp(this,location.origin+'/{href}')\">Copy link</button></div></li>"
        )
    body = "<ul>" + "".join(items) + "</ul>" if items else '<div class="empty">Nothing shared yet.</div>'
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>tailshare · {html.escape(host)}</title><style>{INDEX_CSS}</style></head><body><main>"
        f"<header><h1>tailshare <small>{html.escape(host)}</small></h1><a href=\"/\">Refresh</a></header>"
        f"<p class=sub>{len(files)} file{'s' if len(files) != 1 else ''} · tailnet only</p>"
        f"{body}</main><script>{INDEX_JS}</script></body></html>"
    )


class Handler(SimpleHTTPRequestHandler):
    server_version = f"tailshare/{__version__}"

    def __init__(self, *a, directory: str, **kw):
        super().__init__(*a, directory=directory, **kw)

    # -- routing -----------------------------------------------------------
    def _route(self) -> bool:
        """Handle app routes; return True if the request was answered."""
        path = urllib.parse.urlparse(self.path).path
        if path == HEALTH_PATH:
            self._text(HEALTH_BODY)
            return True
        if path == "/__api/files":
            data = [{"name": f.name, "size": f.size, "mtime": f.mtime, "kind": f.kind} for f in list_shared()]
            self._text(json.dumps(data), "application/json")
            return True
        if path in ("/", "/index.html"):
            self._text(render_index(list_shared(), self.headers.get("Host", "")), "text/html; charset=utf-8")
            return True
        name = urllib.parse.unquote(path.lstrip("/"))
        target = Path(self.directory) / name
        if "/" in name or name.startswith(".") or target.is_symlink() or not target.is_file():
            self.send_error(404)
            return True
        self._download_name = name
        return False

    def do_GET(self):  # noqa: N802
        if not self._route():
            super().do_GET()

    def do_HEAD(self):  # noqa: N802
        if not self._route():
            super().do_HEAD()

    def list_directory(self, path):  # never expose raw listings
        self.send_error(404)
        return None

    _download_name: str | None = None

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._download_name:
            # Shared HTML/SVG must not run scripts against the index's origin.
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'")
            q = urllib.parse.quote(self._download_name)
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{q}")
        super().end_headers()

    def guess_type(self, path):
        t = super().guess_type(path)
        if t.startswith("text/") and "charset" not in t:
            t += "; charset=utf-8"
        return t

    def _text(self, body: str, ctype: str = "text/plain; charset=utf-8"):
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def log_message(self, fmt, *args):
        if HEALTH_PATH in self.path:
            return
        sys.stderr.write("%s %s %s\n" % (datetime.now().strftime("%H:%M:%S"), self.address_string(), fmt % args))


def serve(port: int = PORT, bind: str = "127.0.0.1") -> None:
    root = ensure_share_dir()
    httpd = ThreadingHTTPServer((bind, port), partial(Handler, directory=str(root)))
    httpd.daemon_threads = True
    sys.stderr.write(f"tailshare {__version__} serving {root} on http://{bind}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
