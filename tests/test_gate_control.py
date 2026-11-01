"""ssh-gate `-C attach`: the filtered tmux control-mode proxy, against a real tmux on a private socket.

Run with: uv run python -m unittest discover tests
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
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

    def test_long_lines_are_skipped(self):
        fh = io.BytesIO(b"a\n" + b"x" * (gate_control.MAX_LINE + 10) + b"\nb\n")
        self.assertEqual(list(gate_control.lines(fh)), [b"a", None, b"b"])

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

    def test_sizes_the_window_it_shows(self):
        self.start(flags=())
        self.say("refresh-client -C 60x20")
        self.assertTrue(self.wait_for(b"%end", count=2))
        self.assertTrue(self.wait_until(lambda: self.size(self.a) == "60x20"), self.size(self.a))
        self.t("break-pane", "-d", "-s", self.a)  # %A moves to a window of its own, not the current one
        self.say(f"select-window -t {self.a}")  # brings it back to this client
        self.assertTrue(self.wait_until(lambda: self.size(self.a) == "60x20"), self.size(self.a))
        self.assertEqual(self.dropped, [])
        self.stop()

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
