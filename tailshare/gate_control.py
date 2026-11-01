"""`ssh-gate` for `tmux -C attach`: tmux control mode, filtered both ways, so the app sees one pane.

The app sends `<tmux> -S <socket> -C attach [-r] [-f ignore-size] -t %N` over `ssh -T`. Instead of exec'ing
tmux, the gate runs it as a child and passes lines through:

- to tmux, only these commands on %N: send-keys -H, capture-pane, display with one fixed format (formats
  can run `#()`, so no other is allowed), and refresh-client -C <w>x<h> / select-window to set the size
  of %N's window (never resize-window, which pins it). Under -r only capture-pane and display;
- to the app, only command replies and the notifications about %N and its window. Other panes' output
  never leaves this machine.

When the pane exits (or is left dead by remain-on-exit) the gate writes `%tailshare-gone %N` and exits.
Once the app has set a size, it must send a line at least every LEASE seconds, or the gate writes
`%tailshare-idle %N` and exits, so a phone that dropped off the network gives the window its size back.

A client that may size (no -r, no -f ignore-size) is attached to a session of its own that holds only %N's
window (linked, not moved), so its size can only reach that window: a client's size goes to every window
of its session that no other client holds. `select-window -t %N` is run in that session, so taking the
size back never changes which window the Mac shows. tmux removes that session once no client holds it
(destroy-unattached, set right at attach), so a gate killed after attaching leaves nothing; a gate killed
before that leaves a session the next gate removes (sweep). If the session cannot be made or armed, the gate
writes `%tailshare-nosize %N` and exits, and the app attaches again with -f ignore-size. If %N leaves that
window, the gate writes `%tailshare-moved %N` and exits.

A sizing client on a pane in a split window zooms the window on %N (resize-pane -Z, run by the gate), so the
phone's size goes to the pane and not to a layout around it; the Mac shows the pane zoomed meanwhile. It
zooms once attached, so the app gets the %layout-change (flags with Z, a visible layout of %N alone). The
window option @tailshare-zoom ("%N %prev", prev the pane that was active) says gates zoomed it: the last gate
on %N to leave unzooms it if it is still zoomed on %N, and gives prev back. A window zoomed by someone
else, or on another pane (a second phone on another pane of the same window), is left as it is: that phone
gets the split pane, scaled to fit.

This narrows what the app can do; it is not what makes a pane key safe. Plain attach still gives a full
tmux client (see docs/ssh-keys.md: approving a key is approving shell access).
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import sys
import threading
import time
from typing import BinaryIO, Callable

FMT = "#{pane_width} #{pane_height} #{cursor_x} #{cursor_y} #{alternate_on} #{cursor_flag} #{pane_dead}"
MAX_LINE = 65536  # bytes, either way
MAX_HISTORY = 10000  # lines capture-pane may reach back
POLL = 1.0  # seconds between checks that the pane is still there
LEASE = 30.0  # seconds without a line from the app, once it has set a size, before the gate lets go
SIZE = re.compile(r"refresh-client -C ([1-9][0-9]{1,3})x([1-9][0-9]{0,2})")
COLS, ROWS = (20, 1000), (5, 500)
PANE_NOTICES = (b"%output", b"%extended-output", b"%pane-mode-changed", b"%pause", b"%continue")


def size_of(line: str) -> tuple[int, int] | None:
    """(cols, rows) of a `refresh-client -C <w>x<h>` within bounds."""
    m = SIZE.fullmatch(line)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return (w, h) if COLS[0] <= w <= COLS[1] and ROWS[0] <= h <= ROWS[1] else None


def allowed_in(line: str, pane: str, readonly: bool, sizing: bool = True) -> bool:
    """A command line from the app that may reach tmux. The size is the client's, never a window's
    (`-C @W:WxH`): under window-size latest, the Mac takes a client size back by typing, a window size it
    does not."""
    p = re.escape(pane)
    if re.fullmatch(rf"send-keys -H -t {p}( [0-9a-f]{{2}})+", line):
        return not readonly
    if size_of(line) or line == f"select-window -t {pane}":
        return sizing and not readonly
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
            if w[0] in (b"%end", b"%error") and w[1:4] == self.block:
                self.block = None
            return True
        if w[0] == b"%begin" and len(w) >= 4:
            self.block = w[1:4]  # time, number and flags, all of which a pane's text could only guess
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


def fd_lines(fd: int, stop: threading.Event):
    """lines() for a raw fd, giving up within POLL once `stop` is set, so the thread reading it can be
    joined instead of being left blocked in a read at exit."""
    buf, skipping = b"", False
    while not stop.is_set():
        if not select.select([fd], [], [], POLL)[0]:
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            if buf and not skipping:
                yield buf
            return
        buf += chunk
        while (i := buf.find(b"\n")) >= 0:
            line, buf = buf[:i], buf[i + 1 :]
            yield None if skipping or len(line) > MAX_LINE else line
            skipping = False
        if len(buf) > MAX_LINE:
            buf, skipping = b"", True


def _tmux(binary: str, sock: str, *args: str) -> str | None:
    """stdout of one tmux command, or None if it failed."""
    try:
        cp = subprocess.run([binary, "-S", sock, *args], capture_output=True, text=True, timeout=3,
                            stdin=subprocess.DEVNULL, env=child_env())
    except (OSError, subprocess.SubprocessError):
        return None
    return cp.stdout.strip() if cp.returncode == 0 else None


def _display(binary: str, sock: str, pane: str, fmt: str) -> str:
    return _tmux(binary, sock, "display", "-p", "-t", pane, fmt) or ""


def gate_running(pid: int) -> bool:
    """Whether `pid` is another live `tailshare ssh-gate` (not one whose pid was reused)."""
    if pid == os.getpid():
        return False  # this gate has made no session yet: one with its name is a dead gate's
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                             timeout=3, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # can't tell: keep it
    return "ssh-gate" in cmd.split()


def sweep(binary: str, sock: str, log: Callable[[str], None] = lambda m: None) -> None:
    """Remove sessions left by gates killed between making theirs and arming destroy-unattached: a gate's
    name, no client, one window and that one linked (so the window lives on elsewhere), its gate gone."""
    listed = _tmux(binary, sock, "list-sessions", "-F",
                   "#{session_name} #{session_attached} #{session_windows} #{window_linked}") or ""
    for row in listed.splitlines():
        m = re.fullmatch(r"(tailshare-[0-9]{1,6}-([0-9]{1,10})) 0 1 1", row)
        if m and not gate_running(int(m.group(2))):
            _tmux(binary, sock, "kill-session", "-t", f"={m.group(1)}")
            log(f"control: removed {m.group(1)}, left by a gate that is gone")


def own_session(binary: str, sock: str, pane: str, window: str) -> str:
    """A new session holding only `window` (%N's), for a client that sizes; "" if it could not be made."""
    if not re.fullmatch(r"@[0-9]{1,6}", window):
        return ""
    name = f"tailshare-{pane[1:]}-{os.getpid()}"
    # a session needs a window of its own to start with; it only holds the place until %N's is linked
    first = _tmux(binary, sock, "new-session", "-d", "-P", "-F", "#{window_id}", "-s", name, "sleep 60")
    if not first:
        return ""
    if (_tmux(binary, sock, "link-window", "-s", window, "-t", f"={name}:") is None
            or _tmux(binary, sock, "kill-window", "-t", first) is None):
        _tmux(binary, sock, "kill-session", "-t", f"={name}")
        return ""
    return name


def other_gates(binary: str, sock: str, pane: str, window: str, session: str) -> bool:
    """Whether another live gate on `pane` still holds `window` in a session of its own."""
    listed = _tmux(binary, sock, "list-windows", "-a", "-F", "#{session_name} #{window_id}") or ""
    for row in listed.splitlines():
        m = re.fullmatch(rf"(tailshare-{pane[1:]}-([0-9]{{1,10}})) (@[0-9]+)", row)
        if m and m.group(1) != session and m.group(3) == window and gate_running(int(m.group(2))):
            return True
    return False


def zoom(binary: str, sock: str, pane: str, window: str, log: Callable[[str], None] = lambda m: None) -> None:
    """Zoom %N's window on %N if it is split and not zoomed yet (see the docstring at the top)."""
    st = _display(binary, sock, window, "#{window_panes} #{window_zoomed_flag} #{pane_id}").split()
    if len(st) != 3 or st[0] == "1" or st[1] != "0":
        if len(st) == 3 and st[1] == "1" and _display(binary, sock, pane, "#{pane_active}") != "1":
            log(f"control {pane}: its window is zoomed on {st[2]}, left as it is")
        return
    # checked and done in the server at once: two gates zooming together must not toggle it back off
    if _tmux(binary, sock, "if-shell", "-F", "-t", pane, "#{||:#{window_zoomed_flag},#{==:#{window_panes},1}}", "",
             f"set-option -w -t {window} @tailshare-zoom '{pane} {st[2]}' ; resize-pane -Z -t {pane}") is None:
        log(f"control {pane}: could not zoom {window}")


def unzoom(binary: str, sock: str, pane: str, window: str, session: str) -> None:
    """Undo zoom() once no other gate holds the window: only if gates zoomed it, and it is still on %N."""
    mark = _display(binary, sock, window, "#{@tailshare-zoom}").split()
    if len(mark) != 2 or mark[0] != pane or not re.fullmatch(r"%[0-9]+", mark[1]) \
            or other_gates(binary, sock, pane, window, session):
        return
    back = f" ; select-pane -t {mark[1]}" if mark[1] != pane and _display(binary, sock, mark[1], "#{window_id}") == window else ""
    _tmux(binary, sock, "set-option", "-w", "-u", "-t", window, "@tailshare-zoom")
    # unzoomed meanwhile, or zoomed on another pane, by someone else: leave it. Checked in the server, so
    # two gates leaving together don't zoom it again
    _tmux(binary, sock, "if-shell", "-F", "-t", pane, "#{&&:#{window_zoomed_flag},#{pane_active}}",
          f"resize-pane -Z -t {pane}{back}")


def child_env() -> dict:
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def control_argv(req: dict, binary: str, session: str = "") -> list[str]:
    argv = [binary, "-C", "-S", req["socket"], "attach"] + (["-r"] if req["readonly"] else [])
    argv += ["-f", ",".join(req["flags"])] if req["flags"] else []
    if not session:
        return argv + ["-t", req["pane"]]
    # in the same command, once attached (set before, it would remove the session at once): from here on a
    # gate killed by any signal leaves nothing behind
    return argv + ["-t", f"={session}", ";", "set-option", "-t", f"={session}:", "destroy-unattached", "on"]


def run(req: dict, binary: str, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None,
        log: Callable[[str], None] = lambda m: None) -> int:
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    sock, pane, ro = req["socket"], req["pane"], req["readonly"]
    window = _display(binary, sock, pane, "#{window_id}")
    out = Outbound(pane, window, lambda: _display(binary, sock, pane, "#{window_id}"))
    sizing = not ro and "ignore-size" not in req["flags"]
    if sizing:
        sweep(binary, sock, log)
    session = own_session(binary, sock, pane, window) if sizing else ""
    if sizing and not session:  # never attach a sizing client where its size could reach other windows
        log(f"control {pane}: no session of its own")
        stdout.write(f"%tailshare-nosize {pane}\n".encode())
        stdout.flush()
        return 0
    child = subprocess.Popen(control_argv(req, binary, session), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=child_env())
    write = threading.Lock()
    done = threading.Event()
    gone = threading.Event()
    lease = {"sized": False, "last": time.monotonic()}

    def send(line: bytes) -> None:
        with write:
            try:
                stdout.write(line + b"\n")
                stdout.flush()
            except (OSError, ValueError):
                done.set()

    def alive() -> bool:
        return _display(binary, sock, pane, "#{pane_id} #{pane_dead}") == f"{pane} 0"

    def end(why: str = "gone") -> None:
        if not gone.is_set():
            gone.set()
            send(f"%tailshare-{why} {pane}".encode())
            done.set()
            child.terminate()

    def inbound() -> None:
        for raw in fd_lines(stdin.fileno(), done):
            lease["last"] = time.monotonic()
            try:
                line = raw.decode("ascii") if raw is not None else None
            except UnicodeDecodeError:
                line = None
            if line is not None and allowed_in(line, pane, ro, bool(session)):
                lease["sized"] = lease["sized"] or bool(size_of(line))
                if line.startswith("select-window"):  # in its own session: -t %N would pick the Mac's
                    raw = f"select-window -t ={session}:{window}".encode()
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
                end()
            elif session and _display(binary, sock, pane, "#{window_id}") != window:
                end("moved")  # its window no longer shows %N
            elif lease["sized"] and time.monotonic() - lease["last"] > LEASE:
                end("idle")

    threads = [threading.Thread(target=inbound, daemon=True), threading.Thread(target=watch, daemon=True)]
    for t in threads:
        t.start()
    replies = 0 if session else 2  # attach's goes to the app; the set-option's is the gate's own
    try:
        for line in lines(child.stdout):
            if gone.is_set():
                break  # %tailshare-gone is the last line
            if replies < 2 and line:
                w0 = line.split(b" ")[0]
                if w0 in (b"%end", b"%error"):
                    replies += 1
                    if replies == 2:
                        if w0 == b"%error":
                            log(f"control {pane}: could not set destroy-unattached on {session}")
                            end("nosize")
                        else:  # attached: the app sees the zoom as %layout-change with Z
                            zoom(binary, sock, pane, window, log)
                        continue
                elif replies == 1 and w0 == b"%begin":
                    continue
            if line is not None and out.allowed(line):
                send(line)
        child.wait()
        child.stdout.close()
        done.set()
        if not gone.is_set() and not alive():
            end()
        elif not gone.is_set() and session and _display(binary, sock, pane, "#{window_id}") != window:
            end("moved")  # its window left with it, and so did the session that held only that
        for t in threads:  # both stop within POLL of done
            t.join(POLL + 5)
    finally:
        # %N's window stays where it was. A Mac's bare `tmux attach` may have landed here (the newest
        # session): then leave it to destroy-unattached instead of throwing the Mac out
        if session and _tmux(binary, sock, "display", "-p", "-t", f"={session}:", "#{session_attached}") == "0":
            _tmux(binary, sock, "kill-session", "-t", f"={session}")
        if session:
            unzoom(binary, sock, pane, window, session)
    return 0 if gone.is_set() else child.returncode
