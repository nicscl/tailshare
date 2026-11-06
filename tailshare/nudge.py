"""Waking an idle Codex session when mail arrives. Codex has nothing like Claude's background task that ends when a
message comes, so a Codex agent sees mail only with its next prompt. When a message is filed for a Codex agent on
this machine that is idle at its prompt, in a tmux pane of this user's that nobody is attached to, the mailbox types
one fixed line into that pane: the prompt hook then shows the messages. The line never holds message text (a peer's
words are never typed as the person's); at most one nudge per agent a minute."""
from __future__ import annotations

import json
import subprocess
import threading
import time

from . import bus

TEXT = "New tailshare bus mail: read it (above, or `tailshare agent inbox`) and handle it."
EVERY = 60.0  # seconds between nudges of one agent
SETTLE = 3.0  # seconds idle before a nudge, so a turn that just ended is not raced
AGENTS = ("codex", "node")

_last: dict[str, float] = {}
_lock = threading.Lock()


def _tmux(binary: str, sock: str, *args: str) -> str | None:
    try:
        cp = subprocess.run([binary, "-S", sock, *args], capture_output=True, text=True, timeout=3,
                            stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout if cp.returncode == 0 else None


def ready(a: dict, now: float) -> tuple[str, dict] | None:
    """(tmux binary, pane) to nudge agent row `a`, or None: a Codex agent, idle for a few seconds, in a live pane
    on this user's own tmux server that runs Codex, not in copy mode, its tmux server attached to no client (a device's view is a session of its own there): someone
    attached may have a draft at the prompt, and typing into it would send their half-written words."""
    from . import keys

    if a.get("harness") != "codex":
        return None
    try:
        st = json.loads(a.get("state") or "{}")
        t = bus.clean_tmux(json.loads(a.get("tmux") or "{}"))
    except ValueError:
        return None
    if not isinstance(st, dict) or st.get("v") != "idle" or now - float(st.get("since") or now) < SETTLE or not t:
        return None
    binary = keys.tmux_binary(t["bin"])
    if not binary or not keys.own_socket(t["socket"]):
        return None
    out = _tmux(binary, t["socket"], "display", "-p", "-t", t["pane"],
                "#{pane_id}\t#{pane_dead}\t#{pane_in_mode}\t#{pane_current_command}")
    parts = (out or "").rstrip("\n").split("\t")
    if len(parts) != 4 or parts[0] != t["pane"] or parts[1] == "1" or parts[2] == "1" or parts[3] not in AGENTS:
        return None
    clients = _tmux(binary, t["socket"], "list-clients", "-F", "#{client_name}")  # a device's view is its own session
    if clients is None or clients.strip():
        return None
    return binary, t


def nudge(b, aids: list[str], now: float | None = None) -> list[str]:
    """Nudge those of `aids` that are ready; returns the ones nudged."""
    now = time.time() if now is None else now
    done = []
    for aid in aids:
        with _lock:
            if now - _last.get(aid, 0.0) < EVERY:
                continue
        a = b.store.one("SELECT id, harness, state, tmux FROM agents WHERE id=?", aid)
        r = ready(dict(a), now) if a else None
        if not r:
            continue
        binary, t = r
        with _lock:
            if now - _last.get(aid, 0.0) < EVERY:
                continue
            _last[aid] = now
        if _tmux(binary, t["socket"], "send-keys", "-t", t["pane"], "-l", TEXT) is not None \
                and _tmux(binary, t["socket"], "send-keys", "-t", t["pane"], "Enter") is not None:
            bus.log(f"nudge: woke codex {aid[:8]} for new mail")
            done.append(aid)
    return done


def later(b, aids: list[str]) -> None:
    """Off the mailbox's lock and the sender's request: the checks run tmux."""
    if aids:
        threading.Thread(target=nudge, args=(b, aids), daemon=True, name="nudge").start()
