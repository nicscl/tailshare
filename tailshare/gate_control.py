"""`ssh-gate` for `tmux -C attach`: tmux control mode, filtered both ways, so the app sees one pane.

The app sends `<tmux> -S <socket> -C attach [-r] [-f ignore-size] -t %N` over `ssh -T`. Instead of exec'ing
tmux, the gate runs it as a child and passes lines through:

- to tmux, only three commands on %N: send-keys -H (never with -r), capture-pane, and display with one
  fixed format (formats can run `#()`, so no other is allowed);
- to the app, only command replies and the notifications about %N and its window. Other panes' output
  never leaves this machine.

When the pane exits (or is left dead by remain-on-exit) the gate writes `%tailshare-gone %N` and exits.

This narrows what the app can do; it is not what makes a pane key safe. Plain attach still gives a full
tmux client (see docs/ssh-keys.md: approving a key is approving shell access).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from typing import BinaryIO, Callable

FMT = "#{pane_width} #{pane_height} #{cursor_x} #{cursor_y} #{alternate_on} #{cursor_flag} #{pane_dead}"
MAX_LINE = 65536  # bytes, either way
MAX_HISTORY = 10000  # lines capture-pane may reach back
POLL = 1.0  # seconds between checks that the pane is still there
PANE_NOTICES = (b"%output", b"%extended-output", b"%pane-mode-changed", b"%pause", b"%continue")


def allowed_in(line: str, pane: str, readonly: bool) -> bool:
    """A command line from the app that may reach tmux."""
    p = re.escape(pane)
    if re.fullmatch(rf"send-keys -H -t {p}( [0-9a-f]{{2}})+", line):
        return not readonly
    m = re.fullmatch(rf"capture-pane -p -e -t {p}( -S -([0-9]{{1,5}}))?", line)
    if m:
        return m.group(2) is None or int(m.group(2)) <= MAX_HISTORY
    return line == f"display -p -t {pane} '{FMT}'"


class Outbound:
    """Which lines from tmux may reach the app. Replies (%begin … %end/%error) only come from the commands
    allowed_in let through, so their bodies pass whole."""

    def __init__(self, pane: str, window: str, window_of: Callable[[], str]):
        self.pane, self.window, self.window_of = pane.encode(), window.encode(), window_of
        self.block: list[bytes] | None = None

    def allowed(self, line: bytes) -> bool:
        w = line.split(b" ")
        if self.block is not None:
            if w[0] in (b"%end", b"%error") and w[1:3] == self.block:
                self.block = None
            return True
        if w[0] == b"%begin" and len(w) >= 3:
            self.block = w[1:3]
            return True
        if w[0] in PANE_NOTICES:
            return w[1:2] == [self.pane]
        if w[0] == b"%layout-change":
            if w[1:2] != [self.window]:  # the pane may have moved to another window
                self.window = self.window_of().encode() or self.window
            return w[1:2] == [self.window]
        return w[0] == b"%exit"


def lines(fh: BinaryIO):
    """Lines without their newline; None for one over MAX_LINE (skipped to its end)."""
    while True:
        raw = fh.readline(MAX_LINE + 1)
        if not raw:
            return
        if not raw.endswith(b"\n") and len(raw) > MAX_LINE:
            while raw and not raw.endswith(b"\n"):
                raw = fh.readline(MAX_LINE + 1)
            yield None
            continue
        yield raw.removesuffix(b"\n")


def _display(binary: str, sock: str, pane: str, fmt: str) -> str:
    try:
        cp = subprocess.run([binary, "-S", sock, "display", "-p", "-t", pane, fmt], capture_output=True,
                            text=True, timeout=3, stdin=subprocess.DEVNULL, env=child_env())
    except (OSError, subprocess.SubprocessError):
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def child_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def control_argv(req: dict, binary: str) -> list[str]:
    argv = [binary, "-C", "-S", req["socket"], "attach"] + (["-r"] if req["readonly"] else [])
    return argv + (["-f", ",".join(req["flags"])] if req["flags"] else []) + ["-t", req["pane"]]


def run(req: dict, binary: str, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
        log: Callable[[str], None] = lambda m: None) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    sock, pane, ro = req["socket"], req["pane"], req["readonly"]
    out = Outbound(pane, _display(binary, sock, pane, "#{window_id}"),
                   lambda: _display(binary, sock, pane, "#{window_id}"))
    child = subprocess.Popen(control_argv(req, binary), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=child_env())
    write = threading.Lock()
    done = threading.Event()
    gone = threading.Event()

    def send(line: bytes) -> None:
        with write:
            try:
                stdout.write(line + b"\n")
                stdout.flush()
            except (OSError, ValueError):
                done.set()

    def alive() -> bool:
        return _display(binary, sock, pane, "#{pane_id} #{pane_dead}") == f"{pane} 0"

    def end_gone() -> None:
        if not gone.is_set():
            gone.set()
            send(f"%tailshare-gone {pane}".encode())
            done.set()
            child.terminate()

    def inbound() -> None:
        for raw in lines(stdin):
            try:
                line = raw.decode("ascii") if raw is not None else None
            except UnicodeDecodeError:
                line = None
            if line is not None and allowed_in(line, pane, ro):
                try:
                    child.stdin.write(raw + b"\n")
                    child.stdin.flush()
                except (OSError, ValueError):
                    break
            else:
                log(f"control {pane}: dropped {(raw or b'<too long>')[:120]!r}")
        try:
            child.stdin.close()  # tmux detaches
        except OSError:
            pass

    def watch() -> None:
        while not done.wait(POLL):
            if not alive():
                end_gone()

    threading.Thread(target=inbound, daemon=True).start()
    threading.Thread(target=watch, daemon=True).start()
    for line in lines(child.stdout):
        if gone.is_set():
            break  # %tailshare-gone is the last line
        if line is not None and out.allowed(line):
            send(line)
    child.wait()
    child.stdout.close()
    done.set()
    if not gone.is_set() and not alive():
        end_gone()
    return 0 if gone.is_set() else child.returncode
