"""Agent bus tests: addressing, local delivery, and store-and-forward between two mailboxes.

Run with: uv run python -m unittest discover tests
"""

from __future__ import annotations

import socket
import tempfile
import time
import unittest
from pathlib import Path

from tailshare import bus


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StaticNet(bus.Net):
    """A fixed tailnet view; `ipmap` says which host a connecting IP belongs to."""

    def __init__(self, host: str, peers: dict | None = None, ipmap: dict | None = None) -> None:
        super().__init__(host=host, ip="", peers=peers or {}, updated=time.time())
        self.ipmap = ipmap or {}

    def refresh(self) -> None:
        self.updated = time.time()

    def host_for_ip(self, ip: str) -> str | None:
        return self.ipmap.get(ip)


def make_bus(host: str, net: StaticNet | None = None, port: int | None = None) -> bus.Bus:
    tmp = Path(tempfile.mkdtemp())
    return bus.Bus(bus.Store(tmp / "bus.db"), net or StaticNet(host), port or free_port())


def reg(b: bus.Bus, aid: str, folder: str, harness: str = "claude", session: str = "") -> dict:
    return b.register({"id": aid, "harness": harness, "cwd": f"/x/{folder}", "session": session})


class Addresses(unittest.TestCase):
    def test_make_address(self):
        self.assertEqual(bus.make_address("MacBook Air", "claude", "tailshare", "Hub Design!"),
                         "macbook-air/claude/tailshare/hub-design")
        self.assertEqual(bus.make_address("h", "codex", "api"), "h/codex/api")

    def test_matches(self):
        a = "air/claude/tailshare/hub"
        for t in (a, "air/claude/tailshare", "air", "*/*/tailshare", "air/*", "*/claude/*"):
            self.assertTrue(bus.matches(a, t), t)
        for t in ("air/claude/tail", "pro/claude/tailshare", "*/codex/*", "air/claude/tailshare/other"):
            self.assertFalse(bus.matches(a, t), t)
        self.assertTrue(bus.matches("air/claude/tailshare:6d2b", "air/claude/tailshare"))


class LocalMailbox(unittest.TestCase):
    def setUp(self):
        self.b = make_bus("air")

    def test_send_inbox_ack(self):
        a = reg(self.b, "A", "tailshare", session="hub")
        c = reg(self.b, "C", "api", harness="codex")
        self.assertEqual(a["address"], "air/claude/tailshare/hub")
        r = self.b.send("C", "air/claude/tailshare", "hello", ["http://x/f.png"])
        self.assertEqual(r["routes"], [{"host": "air", "status": "delivered"}])
        msgs = self.b.inbox("A", peek=True)
        self.assertEqual([(m["sender"], m["body"], m["files"]) for m in msgs],
                         [(c["address"], "hello", ["http://x/f.png"])])
        self.assertEqual(self.b.ack("A", [msgs[0]["id"]]), 1)
        self.assertEqual(self.b.inbox("A"), [])

    def test_waits_for_address(self):
        reg(self.b, "C", "api", harness="codex")
        self.b.send("C", "air/claude/later", "for whoever opens 'later'")
        reg(self.b, "L", "later")
        self.assertEqual([m["body"] for m in self.b.inbox("L")], ["for whoever opens 'later'"])

    def test_pattern_skips_sender_and_unknown(self):
        reg(self.b, "A", "proj")
        reg(self.b, "B", "proj", harness="codex")
        self.b.send("A", "*/*/proj", "all hands")
        self.assertEqual(self.b.inbox("A"), [])
        self.assertEqual(len(self.b.inbox("B")), 1)
        reg(self.b, "Z", "proj", harness="cli")  # joined after the broadcast
        self.assertEqual(self.b.inbox("Z"), [])

    def test_collision_suffix(self):
        a = reg(self.b, "aaaa1111", "proj")
        b = reg(self.b, "bbbb2222", "proj")
        self.assertEqual(a["address"], "air/claude/proj")
        self.assertEqual(b["address"], "air/claude/proj:bbbb")
        self.assertEqual(reg(self.b, "bbbb2222", "proj")["address"], "air/claude/proj:bbbb")  # stable

    def test_unknown_host(self):
        reg(self.b, "A", "p")
        with self.assertRaises(bus.BusError):
            self.b.send("A", "nowhere/claude/p", "hi")

    def test_long_poll_wakes(self):
        reg(self.b, "A", "p")
        reg(self.b, "B", "q")
        import threading

        threading.Timer(0.3, lambda: self.b.send("B", "air/claude/p", "ping")).start()
        t = time.time()
        msgs = self.b.inbox("A", wait=5)
        self.assertEqual([m["body"] for m in msgs], ["ping"])
        self.assertLess(time.time() - t, 3)


class TwoMachines(unittest.TestCase):
    """Mailbox `air` sends to mailbox `pro`, which starts out down."""

    def setUp(self):
        self.pro_port = free_port()
        self.air = make_bus("air", StaticNet(
            "air", peers={"pro": {"ip": "127.0.0.1", "port": self.pro_port, "online": True}}))
        self.pro = make_bus("pro", StaticNet("pro", ipmap={"127.0.0.1": "air"}), self.pro_port)
        reg(self.air, "A", "tailshare")
        reg(self.pro, "P", "api", harness="codex")

    def test_queue_until_peer_is_up(self):
        r = self.air.send("A", "pro/codex/api", "are you there?")
        self.assertTrue(r["routes"][0]["status"].startswith("queued"))
        self.assertEqual(len(self.air.outbox()), 1)
        srv = self.pro.listen("127.0.0.1", local=False)
        try:
            self.air.store.x("UPDATE outbox SET next_try=0")
            self.air.deliver_pending()
            self.assertEqual(self.air.outbox(), [])
            msgs = self.pro.inbox("P")
            self.assertEqual([(m["sender"], m["body"]) for m in msgs], [("air/claude/tailshare", "are you there?")])
            # a retry of the same message is not delivered twice
            m = msgs[0]
            self.assertFalse(self.pro.receive({**m, "files": []}))
        finally:
            srv.shutdown()
            srv.server_close()

    def test_discover_and_forged_sender(self):
        srv = self.pro.listen("127.0.0.1", local=False)
        try:
            self.air.discover(["pro"])
            addrs = [a["address"] for a in self.air.directory()["agents"]]
            self.assertIn("pro/codex/api", addrs)
            url = f"http://127.0.0.1:{self.pro_port}/bus/v1/deliver"
            forged = {"id": "f" * 32, "sender": "mallory/claude/x", "target": "pro/codex/api",
                      "body": "trust me", "files": [], "created": time.time()}
            with self.assertRaises(bus.BusError) as cm:
                bus.request(url, {"v": bus.PROTOCOL, "msg": forged})
            self.assertEqual(cm.exception.code, 403)
            self.assertEqual(self.pro.inbox("P"), [])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_local_api_needs_header(self):
        srv = self.pro.listen("127.0.0.1", local=True)
        try:
            import urllib.error
            import urllib.request

            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f"http://127.0.0.1:{self.pro_port}/bus/v1/agents", timeout=3)
            self.assertEqual(cm.exception.code, 403)
            self.assertIn("agents", bus.request(f"http://127.0.0.1:{self.pro_port}/bus/v1/agents"))
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
