"""ssh-gate `-C attach`: the filtered tmux control-mode proxy, against a real tmux on a private socket.

Run with: uv run python -m unittest discover tests
"""

from __future__ import annotations

import io
import fcntl
import os
import pty
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tailshare import gate_control, keys

FMT = f"'{gate_control.FMT}'"


class Filters(unittest.TestCase):
    def test_in(self):
        ok = ["send-keys -H -t %3 68 69 0d", "capture-pane -p -e -t %3", "capture-pane -p -e -t %3 -S -10000",
              f"display -p -t %3 {FMT}", "refresh-client -C 80x24", "refresh-client -C 20x5",
              "refresh-client -C 1000x500", "select-window -t %3"]
        bad = ["send-keys -H -t %4 68", "send-keys -t %3 hi", "send-keys -H -t %3 68 ; run-shell id",
               "send-keys -H -t %3 6G", "send-keys -H -t %3", "send-keys -H -t %3 68 ;", "send-keys -H -t %3 6",
               "send-keys -H -t %3 6;", "send-keys -H -t %3 68 6; run-shell id", "capture-pane -p -e -t %3 -S -10001",
               "capture-pane -p -e -t %3 -S 0", "display -p -t %3 '#(id)'", f"display -p -t %3 {FMT} ; kill-server",
               "run-shell id", "", "send-keys -H -t %3 68\r", "refresh-client -C 19x24", "refresh-client -C 1001x24",
               "refresh-client -C 80x4", "refresh-client -C 80x501", "refresh-client -C 080x24",
               "refresh-client -C 80x024", "refresh-client -C @2:80x24", "refresh-client -C 80x24 ; kill-server",
               "refresh-client -C 80,24", "refresh-client 80x24", "refresh-client -C 80x24 -t /dev/ttys1",
               "select-window -t %4", "select-window -t :1", "select-window -t %3 ; kill-server", "resize-window -x 80"]
        for line in ok:
            self.assertTrue(gate_control.allowed_in(line, "%3", False), line)
        for line in bad:
            self.assertFalse(gate_control.allowed_in(line, "%3", False), line)
        self.assertFalse(gate_control.allowed_in("send-keys -H -t %3 68", "%3", True))  # -r
        self.assertTrue(gate_control.allowed_in("capture-pane -p -e -t %3", "%3", True))
        for line in ("refresh-client -C 80x24", "select-window -t %3"):
            self.assertFalse(gate_control.allowed_in(line, "%3", True), line)
            self.assertFalse(gate_control.allowed_in(line, "%3", False, sizing=False), line)
        self.assertTrue(gate_control.allowed_in("send-keys -H -t %3 68", "%3", False, sizing=False))

    def test_out(self):
        moved = []
        o = gate_control.Outbound("%3", "@1", lambda: moved.append(1) or "@1")
        keep = [b"%begin 1 2 1", b"%output %4 inside a reply is fine", b"%end 1 2 1", b"%output %3 hi",
                b"%extended-output %3 5 : hi", b"%layout-change @1 abcd,80x24,0,0,3 abcd,80x24,0,0,3 *",
                b"%pane-mode-changed %3", b"%pause %3", b"%continue %3", b"%exit"]
        drop = [b"%output %4 secret", b"%extended-output %4 5 : secret", b"%window-renamed @2 secrets",
                b"%sessions-changed", b"%session-changed $1 work", b"%layout-change @2 x", b"%pause %4",
                b"%unlinked-window-add @9", b"%client-session-changed /dev/ttys1 $2 other"]
        for line in keep:
            self.assertTrue(o.allowed(line), line)
        for line in drop:
            self.assertFalse(o.allowed(line), line)
        self.assertEqual(len(moved), 1)  # a foreign %layout-change re-checks which window %3 is in
        o.allowed(b"%begin 5 6 1")
        self.assertTrue(o.allowed(b"%end 5 6 0"))  # pane text in a reply, not its end
        self.assertTrue(o.allowed(b"%session-changed $1 still inside the reply"))
        self.assertTrue(o.allowed(b"%end 5 6 1"))
        self.assertFalse(o.allowed(b"%session-changed $1 after it"))

    def test_long_lines_are_skipped(self):
        fh = io.BytesIO(b"a\n" + b"x" * (gate_control.MAX_LINE + 10) + b"\nb\n")
        self.assertEqual(list(gate_control.lines(fh)), [b"a", None, b"b"])

    def test_fd_lines(self):
        r, w = os.pipe()
        data = b"a\n" + b"x" * (gate_control.MAX_LINE + 10) + b"\nb\nc"  # more than a pipe holds
        threading.Thread(target=lambda: (os.write(w, data), os.close(w)), daemon=True).start()
        self.assertEqual(list(gate_control.fd_lines(r, threading.Event())), [b"a", None, b"b", b"c"])
        os.close(r)

    def test_fd_lines_stops_without_input(self):
        r, w = os.pipe()
        stop = threading.Event()
        stop.set()
        with mock.patch.object(gate_control, "POLL", 0.1):
            self.assertEqual(list(gate_control.fd_lines(r, stop)), [])
        os.close(r), os.close(w)

    def test_parse(self):
        r = keys.parse_attach("/opt/homebrew/bin/tmux -S /tmp/s -C attach -r -f ignore-size -t %3")
        self.assertEqual((r["cc"], r["readonly"], r["flags"], r["pane"]), ("-C", True, ["ignore-size"], "%3"))


class Proxy(unittest.TestCase):
    """gate_control.run between pipes and a real tmux: %A is the attached pane, %B another window's."""

    def setUp(self):
        self.tmux = keys.tmux_binary("")
        if not self.tmux:
            self.skipTest("no tmux")
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.sock = str(Path(tempfile.mkdtemp(dir="/tmp")) / "s")
        self.marker = Path(tempfile.mkdtemp()) / "ran"
        self.t("new-session", "-d", "-x", "80", "-y", "24", "cat")
        self.a = self.t("display", "-p", "#{pane_id}")
        self.t("new-window", "-d", "while :; do echo SECRET; sleep 0.1; done")
        self.b = self.t("display", "-p", "-t", ":1", "#{pane_id}")
        self.dropped: list[str] = []

    def tearDown(self):
        subprocess.run([self.tmux, "-S", self.sock, "kill-server"], capture_output=True, env=self.env)

    def t(self, *args) -> str:
        return subprocess.run([self.tmux, "-S", self.sock, *args], capture_output=True, text=True, check=True,
                              env=self.env).stdout.strip()

    def start(self, readonly=False, flags=("ignore-size",)):
        req = {"socket": self.sock, "pane": self.a, "readonly": readonly, "flags": list(flags), "cc": "-C",
               "tail": []}
        rin, self.win = os.pipe()
        self.rout, wout = os.pipe()
        self.result = {}
        self.out = b""

        def go():
            with os.fdopen(rin, "rb") as i, os.fdopen(wout, "wb") as o:
                self.result["rc"] = gate_control.run(req, self.tmux, i, o, self.dropped.append)

        self.thread = threading.Thread(target=go, daemon=True)
        self.thread.start()
        threading.Thread(target=self._read, daemon=True).start()
        self.wait_for(b"%end")  # the attach itself

    def _read(self):
        with os.fdopen(self.rout, "rb") as fh:
            for chunk in iter(lambda: fh.read1(4096), b""):
                self.out += chunk

    def say(self, line: str):
        os.write(self.win, line.encode() + b"\n")

    def wait_for(self, what: bytes, timeout=5.0, count=1) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.out.count(what) >= count:
                return True
            time.sleep(0.05)
        return False

    def stop(self) -> int:
        os.close(self.win)
        self.thread.join(5)
        return self.result.get("rc")

    def test_types_and_reads_only_its_pane(self):
        self.start()
        self.say(f"send-keys -H -t {self.a} 68 65 6c 6c 6f 0d")  # "hello\r", echoed by the pane's cat
        self.assertTrue(self.wait_for(f"%output {self.a} hello".encode()))
        self.say(f"capture-pane -p -e -t {self.a} -S -5")
        self.say(f"display -p -t {self.a} {FMT}")
        self.assertTrue(self.wait_for(b"80 24 "))
        time.sleep(0.5)  # %B prints SECRET every 0.1 s
        self.assertEqual(self.stop(), 0)
        self.assertNotIn(b"SECRET", self.out)
        self.assertNotIn(f"%output {self.b}".encode(), self.out)
        self.assertIn(b"%exit", self.out)

    def test_drops_other_commands(self):
        self.start()
        for line in (f"run-shell 'touch {self.marker}'", f"send-keys -H -t {self.b} 68 0d",
                     f"send-keys -H -t {self.a} 68 ; run-shell 'touch {self.marker}'",
                     f"display -p -t {self.a} '#(touch {self.marker})'", "new-window", "kill-server"):
            self.say(line)
        self.say(f"display -p -t {self.a} {FMT}")  # a reply after the dropped lines: they went nowhere
        self.assertTrue(self.wait_for(b"80 24 "))
        self.assertEqual(self.out.count(b"%begin"), 2)  # the attach and the display
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.t("list-windows", "-F", "#{window_id}").count("@"), 2)
        self.assertEqual(len(self.dropped), 6)
        self.stop()

    def test_read_only_drops_send_keys(self):
        self.start(readonly=True)
        self.say(f"send-keys -H -t {self.a} 7a 7a 7a 0d")
        self.say(f"capture-pane -p -e -t {self.a}")
        self.assertTrue(self.wait_for(b"%end", count=2))  # the attach and the capture
        self.stop()
        self.assertNotIn(b"zzz", self.out)
        self.assertNotIn("zzz", self.t("capture-pane", "-p", "-t", self.a))
        self.assertEqual(len(self.dropped), 1)

    def size(self, target) -> str:
        return self.t("display", "-p", "-t", target, "#{window_width}x#{window_height}")

    def test_sizes_only_the_panes_window(self):
        """The phone is the only client, and the session has three windows: only %A's takes its size."""
        self.t("new-window", "-d", "cat")
        self.start(flags=())
        self.say("refresh-client -C 60x20")
        self.say(f"select-window -t {self.a}")
        self.assertTrue(self.wait_for(b"%end", count=3))
        self.assertTrue(self.wait_until(lambda: self.size(self.a) == "60x20"), self.size(self.a))
        self.assertEqual(self.t("list-windows", "-t", "=0", "-F", "#{window_width}x#{window_height}").split(),
                         ["60x20", "80x24", "80x24"])
        self.assertEqual(self.dropped, [])
        self.assertEqual(len(self.sessions()), 2)  # its own, holding %A's window
        self.assertEqual(self.stop(), 0)
        self.assertEqual(self.sessions(), ["0"])
        self.assertEqual(self.t("list-windows", "-t", "=0", "-F", "#{window_width}x#{window_height}").split(),
                         ["60x20", "80x24", "80x24"])  # no client is left to size %A's window
        self.assertTrue(self.t("display", "-p", "-t", self.a, "#{pane_id}"))

    def test_takes_the_size_back_without_moving_the_mac(self):
        pid, fd = pty.fork()
        if pid == 0:  # the Mac's terminal, showing the other window
            os.execve(self.tmux, [self.tmux, "-S", self.sock, "attach", "-t", "=0:1"], {**self.env, "TERM": "xterm"})
        self.addCleanup(os.waitpid, pid, 0)
        self.addCleanup(os.close, fd)
        threading.Thread(target=self.drain, args=(fd,), daemon=True).start()
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 29, 100, 0, 0))
        mac = lambda: self.t("list-clients", "-t", "=0", "-F", "#{window_index}")
        self.assertTrue(self.wait_until(lambda: mac() == "1"))
        self.start(flags=())
        self.say("refresh-client -C 60x20")
        self.assertTrue(self.wait_until(lambda: self.size(self.a) == "60x20"), self.size(self.a))
        self.t("select-window", "-t", "=0:0")  # the Mac comes to %A's window and takes the size
        os.write(fd, b"x")
        self.assertTrue(self.wait_until(lambda: self.size(self.a) == "100x28"), self.size(self.a))
        self.t("select-window", "-t", "=0:1")  # and goes back to the other one
        self.say(f"select-window -t {self.a}")
        self.assertTrue(self.wait_until(lambda: self.size(self.a) == "60x20"), self.size(self.a))
        self.assertEqual(mac(), "1")
        self.assertEqual(self.dropped, [])
        self.stop()

    @staticmethod
    def drain(fd):
        try:
            while os.read(fd, 65536):
                pass
        except OSError:
            pass

    def test_says_when_the_pane_moves(self):
        self.start(flags=())
        self.t("join-pane", "-d", "-s", self.a, "-t", self.b)  # into a window its session doesn't hold
        self.assertTrue(self.wait_for(f"%tailshare-moved {self.a}".encode()))
        self.thread.join(5)
        self.assertEqual(self.sessions(), ["0"])

    def test_ignore_size_never_sizes(self):
        self.start()
        self.say("refresh-client -C 60x20")
        self.say(f"display -p -t {self.a} {FMT}")
        self.assertTrue(self.wait_for(b"80 24 "))
        self.assertEqual(len(self.dropped), 1)
        self.assertEqual(self.sessions(), ["0"])
        self.stop()

    def sessions(self) -> list[str]:
        return self.t("list-sessions", "-F", "#{session_name}").split()

    def test_read_only_never_sizes(self):
        self.start(readonly=True, flags=())
        self.say("refresh-client -C 60x20")
        self.say(f"select-window -t {self.a}")
        self.say(f"display -p -t {self.a} {FMT}")
        self.assertTrue(self.wait_for(b"80 24 "))
        self.assertEqual(len(self.dropped), 2)
        self.stop()

    def test_lease_lets_go_of_a_silent_client(self):
        with mock.patch.object(gate_control, "LEASE", 1.5):
            self.start(flags=())
            time.sleep(2.5)  # no size set yet: no lease
            self.assertNotIn(b"%tailshare-idle", self.out)
            self.say("refresh-client -C 60x20")
            for _ in range(3):  # a line every second keeps it
                time.sleep(1)
                self.say(f"display -p -t {self.a} {FMT}")
            self.assertNotIn(b"%tailshare-idle", self.out)
            self.assertTrue(self.wait_for(f"%tailshare-idle {self.a}".encode()))
            self.thread.join(5)
        self.assertEqual(self.result.get("rc"), 0)
        self.assertNotIn("1", self.t("list-clients", "-F", "#{client_control_mode}"))  # the client left
        self.assertEqual(self.sessions(), ["0"])

    def test_exits_cleanly_when_idle(self):
        """The process that ran it exits 0 with nothing on stderr, while its stdin is still open."""
        script = ("import sys; from tailshare import gate_control as g; g.LEASE, g.POLL = 1.0, 0.2; "
                  f"req = {{'socket': {self.sock!r}, 'pane': {self.a!r}, 'readonly': False, 'flags': [], "
                  "'cc': '-C', 'tail': []}; sys.exit(g.run(req, sys.argv[1]))")
        p = subprocess.Popen([sys.executable, "-c", script, self.tmux], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env)
        p.stdin.write(b"refresh-client -C 60x20\n")
        p.stdin.flush()
        with p.stdout, p.stderr:
            out, err = p.stdout.read(), p.stderr.read()  # until it exits; its stdin stays open
        self.assertEqual(p.wait(5), 0)
        self.assertEqual(err, b"")
        self.assertTrue(out.rstrip(b"\n").endswith(f"%tailshare-idle {self.a}".encode()))
        p.stdin.close()

    def gate(self):
        script = ("import sys; from tailshare import gate_control as g; "
                  f"req = {{'socket': {self.sock!r}, 'pane': {self.a!r}, 'readonly': False, 'flags': [], "
                  "'cc': '-C', 'tail': []}; sys.exit(g.run(req, sys.argv[1]))")
        p = subprocess.Popen([sys.executable, "-c", script, self.tmux], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env)
        self.addCleanup(lambda: (p.stdin.close(), p.stdout.close(), p.kill(), p.wait()))
        return p

    def test_a_killed_gate_leaves_no_session(self):
        p = self.gate()
        p.stdin.write(b"refresh-client -C 60x20\n")
        p.stdin.flush()
        self.assertTrue(self.wait_until(lambda: self.size(self.a) == "60x20"), self.size(self.a))
        p.kill()  # no finally: only tmux can clean up
        p.wait()
        self.assertTrue(self.wait_until(lambda: self.sessions() == ["0"]), self.sessions())

    def left(self, name: str):
        """What a gate killed before arming destroy-unattached leaves: %A's window linked, no client."""
        self.t("new-session", "-d", "-s", name, "sleep 60")
        self.t("link-window", "-s", self.a, "-t", f"={name}:")
        self.t("kill-window", "-t", f"={name}:0")

    def test_a_new_gate_removes_what_a_dead_one_left(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        reused = subprocess.Popen(["sleep", "30"])  # its pid was a gate's once
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "ssh-gate"])
        for q in (reused, live):
            self.addCleanup(q.wait)
            self.addCleanup(q.kill)
        for name in (f"tailshare-9-{dead.pid}", f"tailshare-8-{reused.pid}", f"tailshare-7-{live.pid}",
                     "tailshare-notes", "tailshare-1-x", "tailshare-1-2-3"):
            self.left(name)
        self.t("new-session", "-d", "-s", "tailshare-6-1", "cat")  # its own window: not a gate's
        self.start(flags=())
        self.say("refresh-client -C 60x20")
        self.assertTrue(self.wait_until(lambda: self.size(self.a) == "60x20"), self.size(self.a))
        names = self.sessions()
        self.assertNotIn(f"tailshare-9-{dead.pid}", names)
        self.assertNotIn(f"tailshare-8-{reused.pid}", names)
        for kept in (f"tailshare-7-{live.pid}", "tailshare-notes", "tailshare-1-x", "tailshare-1-2-3",
                     "tailshare-6-1"):
            self.assertIn(kept, names)
        self.assertIn(self.a, self.t("list-panes", "-a", "-F", "#{pane_id}").split())
        self.stop()

    def test_no_session_of_its_own_says_nosize(self):
        self.t("new-session", "-d", "-s", f"tailshare-{self.a[1:]}-{os.getpid()}", "cat")  # its name is taken
        before = self.sessions()
        self.start(flags=())
        self.thread.join(5)
        self.assertEqual(self.result.get("rc"), 0)
        self.assertEqual(self.out, f"%tailshare-nosize {self.a}\n".encode())
        self.assertEqual(self.sessions(), before)
        os.close(self.win)

    def test_leaves_a_mac_that_attached_to_its_session(self):
        self.start(flags=())
        self.say("refresh-client -C 60x20")
        self.assertTrue(self.wait_until(lambda: len(self.sessions()) == 2), self.sessions())
        mine = [n for n in self.sessions() if n.startswith("tailshare-")][0]
        fd, tty = os.openpty()  # not pty.fork(): this process has threads now
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 29, 100, 0, 0))
        # as a bare `tmux attach` may, when the phone's is the most recently active session
        mac = subprocess.Popen([self.tmux, "-S", self.sock, "attach", "-t", f"={mine}"], stdin=tty, stdout=tty, stderr=tty,
                               env={**self.env, "TERM": "xterm"}, start_new_session=True)
        os.close(tty)
        self.addCleanup(os.close, fd)
        self.addCleanup(lambda: (mac.kill(), mac.wait()))
        threading.Thread(target=self.drain, args=(fd,), daemon=True).start()
        attached = lambda: self.t("display", "-p", "-t", f"={mine}:", "#{session_attached}")
        self.assertTrue(self.wait_until(lambda: attached() == "2"), attached())
        self.assertEqual(self.stop(), 0)
        self.assertEqual(attached(), "1")  # the Mac is still there
        self.t("detach-client", "-s", f"={mine}")
        mac.wait(5)
        self.assertTrue(self.wait_until(lambda: self.sessions() == ["0"]), self.sessions())

    def wait_until(self, ok, timeout=5.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if ok():
                return True
            time.sleep(0.05)
        return False

    def test_says_when_the_pane_is_gone(self):
        self.start()
        self.t("kill-pane", "-t", self.a)
        self.assertTrue(self.wait_for(f"%tailshare-gone {self.a}".encode()))
        self.thread.join(5)
        self.assertEqual(self.result.get("rc"), 0)
        self.assertTrue(self.out.rstrip(b"\n").endswith(f"%tailshare-gone {self.a}".encode()))


if __name__ == "__main__":
    unittest.main()
