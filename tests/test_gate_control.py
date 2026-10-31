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

from tailshare import gate_control, keys

FMT = f"'{gate_control.FMT}'"


class Filters(unittest.TestCase):
    def test_in(self):
        ok = ["send-keys -H -t %3 68 69 0d", "capture-pane -p -e -t %3", "capture-pane -p -e -t %3 -S -10000",
              f"display -p -t %3 {FMT}"]
        bad = ["send-keys -H -t %4 68", "send-keys -t %3 hi", "send-keys -H -t %3 68 ; run-shell id",
               "send-keys -H -t %3 6G", "send-keys -H -t %3", "send-keys -H -t %3 68 ;", "send-keys -H -t %3 6",
               "send-keys -H -t %3 6;", "send-keys -H -t %3 68 6; run-shell id", "capture-pane -p -e -t %3 -S -10001",
               "capture-pane -p -e -t %3 -S 0", "display -p -t %3 '#(id)'", f"display -p -t %3 {FMT} ; kill-server",
               "run-shell id", "", "refresh-client -C 80x24", "send-keys -H -t %3 68\r"]
        for line in ok:
            self.assertTrue(gate_control.allowed_in(line, "%3", False), line)
        for line in bad:
            self.assertFalse(gate_control.allowed_in(line, "%3", False), line)
        self.assertFalse(gate_control.allowed_in("send-keys -H -t %3 68", "%3", True))  # -r
        self.assertTrue(gate_control.allowed_in("capture-pane -p -e -t %3", "%3", True))

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

    def start(self, readonly=False):
        req = {"socket": self.sock, "pane": self.a, "readonly": readonly, "flags": ["ignore-size"], "cc": "-C",
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

    def test_says_when_the_pane_is_gone(self):
        self.start()
        self.t("kill-pane", "-t", self.a)
        self.assertTrue(self.wait_for(f"%tailshare-gone {self.a}".encode()))
        self.thread.join(5)
        self.assertEqual(self.result.get("rc"), 0)
        self.assertTrue(self.out.rstrip(b"\n").endswith(f"%tailshare-gone {self.a}".encode()))


if __name__ == "__main__":
    unittest.main()
