"""launchd LaunchAgent so the share server survives logout/reboot."""

from __future__ import annotations

import os
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path

from .core import BASE_URL, IS_MAC, PORT, configure_serve, pids_on_port, serve_routes, server_status

LABEL = "com.nicholas.tailshare"
PLIST = Path("~/Library/LaunchAgents").expanduser() / f"{LABEL}.plist"
LOG = Path("~/Library/Logs/tailshare.log" if IS_MAC else "~/tailshare.log").expanduser()


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=20)
    except FileNotFoundError:  # not macOS (e.g. iSH on an iPad)
        return subprocess.CompletedProcess(["launchctl", *args], 1, "", "launchctl not found")


def installed() -> bool:
    return PLIST.exists()


def loaded() -> bool:
    return _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def write_plist() -> Path:
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "tailshare", "serve"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {k: v for k, v in os.environ.items() if k.startswith("TAILSHARE_")},
    }
    with PLIST.open("wb") as fh:
        plistlib.dump(data, fh)
    return PLIST


def stop_foreign_server() -> list[int]:
    """Kill whatever else holds our port (e.g. a stray `python -m http.server`)."""
    killed = []
    for pid in pids_on_port():
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            pass
    if killed:
        time.sleep(0.5)
    return killed


def _start_detached() -> tuple[bool, str]:
    """No launchd here (iSH on iOS): run the server as a detached background process."""
    if server_status() == "ours":
        return True, "server already running"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "tailshare", "serve"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    for _ in range(50):  # slow hosts need a while
        if server_status() == "ours":
            return True, "server running in the background (keeps running after the TUI quits, while iSH stays open)"
        time.sleep(0.2)
    return False, f"server did not come up; see {LOG}"


def start() -> tuple[bool, str]:
    """Install (if needed) and start the LaunchAgent. Returns (ok, message)."""
    if not IS_MAC:
        return _start_detached()
    notes = []
    if server_status() == "other":
        notes.append(f"stopped stray server on :{PORT} (pid {', '.join(map(str, stop_foreign_server()))})")
    write_plist()
    if loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    cp = _launchctl("bootstrap", _domain(), str(PLIST))
    if cp.returncode != 0:
        return False, (cp.stderr or cp.stdout).strip() or "launchctl bootstrap failed"
    _launchctl("kickstart", "-k", f"{_domain()}/{LABEL}")
    for _ in range(30):
        if server_status() == "ours":
            break
        time.sleep(0.2)
    else:
        return False, f"server did not come up; see {LOG}"
    notes.append("server running under launchd")
    if BASE_URL:  # links configured by hand, no `tailscale serve`
        return True, "; ".join(notes)
    routes = serve_routes()
    if routes is None or not all(routes.values()):
        ok, msg = configure_serve()
        notes.append(msg if ok else f"tailscale serve failed: {msg}")
    return True, "; ".join(notes)


def stop() -> tuple[bool, str]:
    if loaded():
        _launchctl("bootout", f"{_domain()}/{LABEL}")
    return True, "stopped"


def uninstall() -> tuple[bool, str]:
    stop()
    if PLIST.exists():
        PLIST.unlink()
    return True, f"removed {PLIST}"
