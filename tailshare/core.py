"""Shared-folder operations, Tailscale wrapper, macOS clipboard helpers."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SHARE_DIR = Path(os.environ.get("TAILSHARE_DIR", "~/Public/ts-share")).expanduser()
PORT = int(os.environ.get("TAILSHARE_PORT", "8787"))
HEALTH_PATH = "/__health"
HEALTH_BODY = "tailshare ok"

_TS_CANDIDATES = (
    shutil.which("tailscale"),
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
)
TAILSCALE = next((p for p in _TS_CANDIDATES if p and Path(p).exists()), None)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".svg", ".tiff", ".tif", ".avif"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg"}
SHEET_EXT = {".xlsx", ".xls", ".csv", ".numbers", ".tsv"}
DOC_EXT = {".pdf", ".doc", ".docx", ".pages", ".txt", ".md", ".rtf", ".key", ".ppt", ".pptx"}
ARCHIVE_EXT = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".dmg"}
CODE_EXT = {".py", ".js", ".ts", ".json", ".html", ".css", ".sh", ".yaml", ".yml", ".toml", ".sql"}


# --------------------------------------------------------------------------- files


@dataclass(frozen=True)
class SharedFile:
    name: str
    size: int
    mtime: float

    @property
    def path(self) -> Path:
        return SHARE_DIR / self.name

    @property
    def ext(self) -> str:
        return Path(self.name).suffix.lower()

    @property
    def kind(self) -> str:
        e = self.ext
        if e in IMAGE_EXT:
            return "image"
        if e in VIDEO_EXT:
            return "video"
        if e in AUDIO_EXT:
            return "audio"
        if e in SHEET_EXT:
            return "sheet"
        if e in DOC_EXT:
            return "doc"
        if e in ARCHIVE_EXT:
            return "archive"
        if e in CODE_EXT:
            return "code"
        return "file"


KIND_ICON = {
    "image": "◧",
    "video": "▶",
    "audio": "♪",
    "sheet": "▦",
    "doc": "≡",
    "archive": "◫",
    "code": "‹›",
    "file": "·",
}


def ensure_share_dir() -> Path:
    SHARE_DIR.mkdir(parents=True, exist_ok=True)
    return SHARE_DIR


def list_shared() -> list[SharedFile]:
    ensure_share_dir()
    out: list[SharedFile] = []
    for p in SHARE_DIR.iterdir():
        if p.name.startswith(".") or p.is_symlink() or not p.is_file():
            continue
        st = p.stat()
        out.append(SharedFile(p.name, st.st_size, st.st_mtime))
    out.sort(key=lambda f: f.mtime, reverse=True)
    return out


def unique_dest(name: str) -> Path:
    """Return a destination path in SHARE_DIR that does not collide."""
    ensure_share_dir()
    dest = SHARE_DIR / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while True:
        dest = SHARE_DIR / f"{stem}-{n}{suffix}"
        if not dest.exists():
            return dest
        n += 1


_publish_lock = threading.Lock()


def _publish(stage: Path, name: str) -> SharedFile:
    """Atomically move a fully written staging file into the share under a unique name."""
    with _publish_lock:
        dest = unique_dest(name)
        now = time.time()
        os.utime(stage, (now, now))  # "newest first" reflects share time, not source mtime
        os.replace(stage, dest)
    st = dest.stat()
    return SharedFile(dest.name, st.st_size, st.st_mtime)


def add_path(src: Path) -> SharedFile:
    """Copy a file (or zip a directory) into the share. Returns the new entry.

    Data is staged as a hidden ``.part`` file (hidden names are never listed or
    served) and renamed into place only once complete.
    """
    src = src.expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(src)
    ensure_share_dir()
    share = SHARE_DIR.resolve()
    if src.is_dir():
        if src == share or src in share.parents:
            raise ValueError(f"{src.name} contains the share folder itself")
        with tempfile.TemporaryDirectory(prefix="tailshare-") as tmp:
            made = shutil.make_archive(str(Path(tmp) / src.name), "zip", root_dir=src.parent, base_dir=src.name)
            stage = share / f".{src.name}.zip.part"
            shutil.move(made, stage)
            return _publish(stage, src.name + ".zip")
    if src.is_symlink() or not src.is_file():
        raise ValueError(f"{src.name} is not a regular file")
    stage = share / f".{src.name}.part"
    try:
        shutil.copy2(src, stage)
        return _publish(stage, src.name)
    except BaseException:
        stage.unlink(missing_ok=True)
        raise


def remove_shared(name: str) -> None:
    p = SHARE_DIR / name
    if p.is_file():
        p.unlink()


def parse_dropped_paths(text: str) -> tuple[list[Path], list[str]]:
    """Turn terminal drag-and-drop / pasted text into existing paths.

    Handles iTerm2 escaping (``My\\ File.png``), quoted paths, ``file://`` URLs,
    ``~`` and several paths in one drop. Returns (found, missing_tokens).
    """
    text = text.strip()
    if not text:
        return [], []

    def normalize(tok: str) -> Path | None:
        tok = tok.strip()
        if tok.startswith("file://"):
            u = urllib.parse.urlparse(tok)
            if u.netloc not in ("", "localhost"):
                return None
            tok = urllib.parse.unquote(u.path)
        return Path(tok).expanduser()

    # 1. The whole line as one path (hand-typed, unescaped spaces). Checked first so
    #    "/tmp/My File.txt" is never split into "/tmp/My" + "File.txt".
    whole = normalize(text.strip("'\""))
    if whole is not None and whole.exists():
        return [whole], []

    # 2. Shell-style tokens: iTerm2 escapes spaces, quotes are honoured, several
    #    files arrive space-separated; Finder pastes give one path per line.
    candidates: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        lp = normalize(line.strip("'\""))
        if lp is not None and lp.exists():
            candidates.append(line.strip("'\""))
            continue
        try:
            candidates += shlex.split(line)
        except ValueError:
            candidates.append(line)

    found: list[Path] = []
    missing: list[str] = []
    seen: set[Path] = set()
    for tok in candidates:
        if not tok:
            continue
        p = normalize(tok)
        if p is not None and p.exists():
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                found.append(p)
        else:
            missing.append(tok)
    return found, missing


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024:
            return f"{n:.0f} {unit}" if n >= 10 else f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def relative_time(ts: float) -> str:
    delta = time.time() - ts
    if delta < 45:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        h = int(delta // 3600)
        return f"{h} h ago"
    d = int(delta // 86400)
    if d < 7:
        return f"{d} d ago"
    return datetime.fromtimestamp(ts).strftime("%b %d")


# --------------------------------------------------------------------------- server


def server_status(timeout: float = 0.6) -> str:
    """'ours' | 'other' | 'down' for whatever listens on PORT."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout):
            pass
    except OSError:
        return "down"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{HEALTH_PATH}", timeout=timeout) as r:
            return "ours" if r.read().decode().startswith(HEALTH_BODY) else "other"
    except Exception:
        return "other"


def pids_on_port() -> list[int]:
    try:
        out = subprocess.run(
            ["lsof", "-nP", "-t", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    return [int(x) for x in out.split() if x.strip().isdigit()]


# --------------------------------------------------------------------------- tailscale


@dataclass(frozen=True)
class Peer:
    hostname: str
    dns: str
    os: str
    online: bool
    taildrop: bool


@dataclass
class TailscaleInfo:
    ok: bool
    self_name: str = ""
    dns_name: str = ""
    ip: str = ""
    peers: list[Peer] | None = None
    error: str = ""

    @property
    def base_url(self) -> str:
        return f"https://{self.dns_name}/" if self.dns_name else ""


def _run_ts(*args: str, timeout: float = 15) -> subprocess.CompletedProcess:
    if not TAILSCALE:
        raise RuntimeError("tailscale CLI not found")
    return subprocess.run([TAILSCALE, *args], capture_output=True, text=True, timeout=timeout)


def tailscale_info() -> TailscaleInfo:
    if not TAILSCALE:
        return TailscaleInfo(False, error="tailscale CLI not found")
    try:
        cp = _run_ts("status", "--json", timeout=8)
    except Exception as e:  # noqa: BLE001
        return TailscaleInfo(False, error=str(e))
    if cp.returncode != 0:
        return TailscaleInfo(False, error=(cp.stderr or cp.stdout).strip()[:120])
    try:
        d = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return TailscaleInfo(False, error="bad status json")
    if d.get("BackendState") != "Running":
        return TailscaleInfo(False, error=f"tailscale {d.get('BackendState', 'stopped').lower()}")
    me = d.get("Self", {})
    peers = []
    for p in (d.get("Peer") or {}).values():
        peers.append(
            Peer(
                hostname=p.get("HostName", ""),
                dns=(p.get("DNSName") or "").rstrip("."),
                os=p.get("OS", ""),
                online=bool(p.get("Online")),
                taildrop=p.get("TaildropTarget") in (1, "available", True),
            )
        )
    peers.sort(key=lambda p: (not p.online, p.hostname.lower()))
    ips = me.get("TailscaleIPs") or [""]
    return TailscaleInfo(
        True,
        self_name=me.get("HostName", ""),
        dns_name=(me.get("DNSName") or "").rstrip("."),
        ip=ips[0],
        peers=peers,
    )


def url_for(base_url: str, name: str) -> str:
    return base_url + urllib.parse.quote(name)


def serve_configured() -> bool | None:
    """True if `tailscale serve` maps / to our port. None if unknown."""
    try:
        cp = _run_ts("serve", "status", "--json", timeout=8)
    except Exception:
        return None
    if cp.returncode != 0:
        return None
    try:
        cfg = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return None
    for host in (cfg.get("Web") or {}).values():
        for path, h in (host.get("Handlers") or {}).items():
            if path == "/" and str(h.get("Proxy", "")).endswith(f":{PORT}"):
                return True
    return False


def configure_serve() -> tuple[bool, str]:
    try:
        cp = _run_ts("serve", "--bg", "--yes", f"http://127.0.0.1:{PORT}", timeout=20)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if cp.returncode != 0:
        return False, (cp.stderr or cp.stdout).strip()
    return True, "tailscale serve → / → 127.0.0.1:%d" % PORT


def taildrop_send(path: Path, peer: Peer) -> tuple[bool, str]:
    target = (peer.dns or peer.hostname) + ":"
    try:
        cp = _run_ts("file", "cp", str(path), target, timeout=600)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if cp.returncode != 0:
        return False, (cp.stderr or cp.stdout).strip()
    return True, f"sent to {peer.hostname}"


# --------------------------------------------------------------------------- clipboard (macOS)


def _osascript(*lines: str, timeout: float = 20) -> subprocess.CompletedProcess:
    args: list[str] = []
    for ln in lines:
        args += ["-e", ln]
    return subprocess.run(["osascript", *args], capture_output=True, text=True, timeout=timeout)


def clipboard_kind() -> str:
    """'file' | 'image' | 'text' | 'empty'."""
    cp = _osascript("clipboard info")
    info = cp.stdout
    if "furl" in info:
        return "file"
    if "PNGf" in info or "TIFF" in info or "JPEG" in info:
        return "image"
    if "string" in info or "utf8" in info:
        return "text"
    return "empty"


def clipboard_text() -> str:
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def clipboard_files() -> list[Path]:
    """Files copied in Finder (⌘C) are on the clipboard as file URLs."""
    script = (
        'set out to ""\n'
        "try\n"
        "  set items to (the clipboard as list)\n"
        "  repeat with i in items\n"
        "    try\n"
        '      set out to out & (POSIX path of (i as «class furl»)) & linefeed\n'
        "    end try\n"
        "  end repeat\n"
        "end try\n"
        "if out is \"\" then\n"
        "  try\n"
        '    set out to (POSIX path of (the clipboard as «class furl»)) & linefeed\n'
        "  end try\n"
        "end if\n"
        "return out"
    )
    cp = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    paths = [Path(l) for l in cp.stdout.splitlines() if l.strip()]
    return [p for p in paths if p.exists()]


def clipboard_image_to(dest: Path) -> bool:
    """Write the clipboard image as PNG to dest. Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    q = str(dest).replace('"', '\\"')
    cp = _osascript(
        f'set f to (open for access POSIX file "{q}" with write permission)',
        "set eof of f to 0",
        "try",
        "write (the clipboard as «class PNGf») to f",
        "end try",
        "close access f",
    )
    ok = cp.returncode == 0 and dest.exists() and dest.stat().st_size > 0
    if not ok and dest.exists():
        dest.unlink()
    return ok


def paste_clipboard() -> list[SharedFile]:
    """Share whatever is on the clipboard: Finder files, an image, or a pasted path."""
    kind = clipboard_kind()
    if kind == "file":
        return [add_path(p) for p in clipboard_files()]
    if kind == "image":
        name = datetime.now().strftime("clipboard-%Y%m%d-%H%M%S.png")
        stage = ensure_share_dir() / f".{name}.part"
        if not clipboard_image_to(stage):
            raise RuntimeError("could not read image from clipboard")
        return [_publish(stage, name)]
    if kind == "text":
        found, _ = parse_dropped_paths(clipboard_text())
        return [add_path(p) for p in found]
    return []


def copy_to_clipboard(text: str) -> bool:
    return subprocess.run(["pbcopy"], input=text, text=True, check=False).returncode == 0


def reveal_in_finder(path: Path) -> None:
    subprocess.Popen(["open", "-R", str(path)])


def open_url(url: str) -> None:
    subprocess.Popen(["open", url])
