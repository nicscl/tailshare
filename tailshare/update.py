"""Self-update: follow a branch of the repo tailshare was installed from, then restart into it.

Only the source recorded in this install's own metadata is used. A trigger from another
machine can ask for a check now; it cannot choose what gets installed.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

from . import __version__

ENABLED = os.environ.get("TAILSHARE_AUTOUPDATE", "1") != "0"
EVERY = float(os.environ.get("TAILSHARE_UPDATE_HOURS", "1")) * 3600
REF = os.environ.get("TAILSHARE_UPDATE_REF", "main")
STATE = Path(os.environ.get("TAILSHARE_STATE", "~/.tailshare")).expanduser() / "update.json"
TRIGGER_GAP = 30  # seconds between accepted network triggers
UV_CANDIDATES = ("~/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv", "~/.cargo/bin/uv")
CHECK_IMPORT = "import tailshare.server, tailshare.bus, tailshare.agent, tailshare.update, tailshare.cli"


def log(msg: str) -> None:
    sys.stderr.write(f"{datetime.now():%H:%M:%S} update: {msg}\n")


def install_info() -> dict:
    """Where this copy came from, from the install's direct_url.json (PEP 610)."""
    try:
        raw = metadata.distribution("tailshare").read_text("direct_url.json") or "{}"
    except metadata.PackageNotFoundError:
        raw = "{}"
    d = json.loads(raw)
    vcs = d.get("vcs_info") or {}
    return {
        "url": d.get("url", ""),
        "git": vcs.get("vcs") == "git",
        "commit": vcs.get("commit_id", ""),
        "editable": bool((d.get("dir_info") or {}).get("editable")),
    }


def describe() -> str:
    """Version plus commit, e.g. 0.1.0+e08c0ee, or 0.1.0+dev for an editable checkout."""
    i = install_info()
    if i["commit"]:
        return f"{__version__}+{i['commit'][:7]}"
    return f"{__version__}+{'dev' if i['editable'] else 'local'}"


def latest_commit(url: str, ref: str = REF) -> str:
    u = urlparse(url)
    if u.hostname == "github.com":  # no git needed (a bare Mac's /usr/bin/git pops an installer)
        repo = u.path.strip("/").removesuffix(".git")
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/commits/{ref}",
            headers={"Accept": "application/vnd.github.sha", "User-Agent": "tailshare-update"},
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            sha = r.read().decode().strip()
    else:
        git = shutil.which("git") or "/usr/bin/git"
        out = subprocess.run([git, "ls-remote", url, ref], capture_output=True, text=True, timeout=60).stdout.split()
        sha = out[0] if out else ""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError(f"no commit found for {ref}")
    return sha


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    return next((str(p) for c in UV_CANDIDATES if (p := Path(c).expanduser()).exists()), None)


def supervised() -> bool:
    """Is something going to start us again if we exit? (launchd agent or systemd unit)"""
    return bool(os.environ.get("INVOCATION_ID")) or os.environ.get("XPC_SERVICE_NAME", "").startswith("com.nicholas.tailshare")


def _state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(d, indent=1))


def _uv_install(uv: str, url: str, commit: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [uv, "tool", "install", "--force", "--quiet", f"git+{url}@{commit}"],
        capture_output=True, text=True, timeout=900,
    )


class Updater:
    def __init__(self, restart=lambda: os._exit(0)) -> None:
        self.kick = threading.Event()
        self.lock = threading.Lock()
        self.restart = restart
        self.status = {"result": "not checked yet", "checked": 0.0, "version": describe()}
        self._last_trigger = 0.0

    def _done(self, result: str, **extra) -> dict:
        self.status = {"result": result, "checked": time.time(), "version": describe(), **extra}
        log(result)
        return self.status

    def check(self, reason: str = "periodic") -> dict:
        """Update to the newest commit of REF if there is one. Restarts the process on success."""
        if not self.lock.acquire(blocking=False):
            return {"result": "an update is already running", "version": describe()}
        try:
            info = install_info()
            if info["editable"]:
                return self._done("skipped: editable checkout (update it with git)")
            if not info["git"] or not info["url"]:
                return self._done("skipped: not installed from git")
            latest = latest_commit(info["url"])
            if latest == info["commit"]:
                return self._done(f"up to date ({latest[:7]})")
            state = _state()
            if latest in state.get("bad", []):
                return self._done(f"skipped {latest[:7]}: it failed to load before")
            uv = find_uv()
            if not uv:
                return self._done("cannot update: uv not found")
            old = info["commit"]
            log(f"updating {old[:7]} → {latest[:7]} ({reason})")
            cp = _uv_install(uv, info["url"], latest)
            if cp.returncode != 0:
                return self._done(f"install failed: {(cp.stderr or cp.stdout).strip()[-300:]}")
            test = subprocess.run([sys.executable, "-c", CHECK_IMPORT], capture_output=True, text=True, timeout=120)
            if test.returncode != 0:
                state.setdefault("bad", []).append(latest)
                _save_state(state)
                back = _uv_install(uv, info["url"], old) if old else None
                rolled = back is not None and back.returncode == 0
                return self._done(f"{latest[:7]} failed to load; {'rolled back to ' + old[:7] if rolled else 'ROLLBACK FAILED'}: "
                                  f"{test.stderr.strip()[-200:]}")
            state["updated"] = {"from": old, "to": latest, "at": time.time(), "reason": reason}
            _save_state(state)
            if supervised():
                threading.Timer(1.0, self.restart).start()  # let the reply go out first
                return self._done(f"updated {old[:7]} → {latest[:7]}; restarting")
            return self._done(f"updated {old[:7]} → {latest[:7]}; restart tailshare to use it")
        except Exception as e:  # noqa: BLE001
            return self._done(f"check failed: {e}")
        finally:
            self.lock.release()

    def trigger(self) -> dict:
        """Ask for a check soon (from another machine). Rate limited; never blocks."""
        now = time.time()
        if now - self._last_trigger < TRIGGER_GAP:
            return {"result": "check already requested", "version": describe()}
        self._last_trigger = now
        self.kick.set()
        return {"result": "checking for updates", "version": describe()}

    def run(self) -> None:
        delay = 120 + random.uniform(0, 120)  # not right at boot, and not all machines at once
        while True:
            triggered = self.kick.wait(delay)
            self.kick.clear()
            self.check("requested by a peer" if triggered else "periodic")
            delay = EVERY * random.uniform(0.9, 1.1)

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name="updater").start()
