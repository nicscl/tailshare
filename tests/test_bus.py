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
    """A fixed tailnet view; `ipmap` says which host a connecting IP belongs to, `usermap` its owner."""

    def __init__(self, host: str, peers: dict | None = None, ipmap: dict | None = None,
                 user: str = "nick@example.com", usermap: dict | None = None) -> None:
        super().__init__(host=host, ip="", user=user, peers=peers or {}, updated=time.time())
        self.ipmap = ipmap or {}
        self.usermap = usermap or {}

    def refresh(self) -> None:
        self.updated = time.time()

    def host_for_ip(self, ip: str) -> str | None:
        return self.ipmap.get(ip)

    def user_for_ip(self, ip: str) -> str | None:
        return self.usermap.get(ip)


def make_bus(host: str, net: StaticNet | None = None, port: int | None = None) -> bus.Bus:
    tmp = Path(tempfile.mkdtemp())
    return bus.Bus(bus.Store(tmp / "bus.db"), net or StaticNet(host), port or free_port())


def reg(b: bus.Bus, aid: str, folder: str, harness: str = "claude", session: str = "", **kw) -> dict:
    return b.register({"id": aid, "harness": harness, "cwd": f"/x/{folder}", "session": session, **kw})


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


class Users(unittest.TestCase):
    def setUp(self):
        self.b = make_bus("srv", StaticNet("srv", user="owner@example.com",
                                           usermap={"100.1.1.1": "nick@example.com", "100.2.2.2": "ana@other.org"}))

    def test_user_sources(self):
        self.assertEqual(reg(self.b, "L", "p")["user"], "owner")  # sitting at the machine
        self.assertEqual(reg(self.b, "S", "q", ssh_ip="100.2.2.2")["user"], "ana")  # SSH'd in from Ana's device
        self.assertEqual(reg(self.b, "P", "r", ssh_ip="203.0.113.9")["user"], "owner")  # not a tailnet IP
        self.assertEqual(reg(self.b, "E", "s", ssh_ip="100.2.2.2", user="Nick")["user"], "nick")  # explicit wins

    def test_at_user_targets(self):
        reg(self.b, "N1", "api", ssh_ip="100.1.1.1")
        reg(self.b, "N2", "web", harness="codex", ssh_ip="100.1.1.1")
        reg(self.b, "A1", "api", ssh_ip="100.2.2.2", session="ana")
        reg(self.b, "X", "x", harness="cli")
        self.b.send("X", "@nick", "to all of nick")
        self.b.send("X", "@ana/*/*/api", "to ana's api sessions")
        self.b.send("X", "@nick/*/codex/*", "to nick's codex")
        body = lambda aid: [m["body"] for m in self.b.inbox(aid)]
        self.assertEqual(body("N1"), ["to all of nick"])
        self.assertEqual(body("N2"), ["to all of nick", "to nick's codex"])
        self.assertEqual(body("A1"), ["to ana's api sessions"])
        self.assertTrue(bus.is_pattern("@nick"))

    def test_sender_user_travels(self):
        reg(self.b, "N1", "api", ssh_ip="100.1.1.1")
        reg(self.b, "A1", "web", ssh_ip="100.2.2.2")
        self.b.send("N1", "srv/claude/web", "hi")
        self.assertEqual(self.b.inbox("A1")[0]["sender_user"], "nick")


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


PANE = {"socket": "/private/tmp/tmux-501/default", "pane": "%7", "target": "main:1.0", "bin": "/opt/homebrew/bin/tmux"}


class TmuxPanes(unittest.TestCase):
    """Agents in tmux advertise their pane in /hello, so a person can attach to it."""

    def setUp(self):
        self.pro_port = free_port()
        self.air = make_bus("air", StaticNet(
            "air", peers={"pro": {"ip": "127.0.0.1", "port": self.pro_port, "online": True}}))
        self.pro = make_bus("pro", StaticNet("pro"), self.pro_port)

    def test_clean_tmux(self):
        self.assertEqual(bus.clean_tmux(PANE), PANE)
        self.assertEqual(bus.clean_tmux({"socket": "/s", "pane": "%1"}), {"socket": "/s", "pane": "%1", "target": "", "bin": ""})
        for bad in (None, "x", {}, {**PANE, "pane": "7"}, {**PANE, "pane": "%7; rm -rf ~"},
                    {**PANE, "socket": "relative"}, {**PANE, "socket": "/a\nb"}):
            self.assertEqual(bus.clean_tmux(bad), {}, bad)
        self.assertEqual(bus.clean_tmux({**PANE, "target": "x\x1b[2J", "bin": "tmux"})["target"], "")
        self.assertEqual(bus.clean_login("Glaciar"), "Glaciar")
        self.assertEqual(bus.clean_login("a b"), "")
        self.assertEqual(bus.clean_login("-oProxyCommand=x"), "")

    def test_register_and_hello(self):
        a = reg(self.pro, "P", "api", tmux={**PANE, "extra": "dropped"})
        self.assertEqual(a["tmux"], PANE)
        self.assertEqual(a["login"], bus.LOGIN)
        hello = self.pro.hello()["agents"][0]
        self.assertIsInstance(hello["tmux"].pop("alive"), bool)
        self.assertEqual((hello["tmux"], hello["login"]), (PANE, bus.LOGIN))
        self.assertEqual(reg(self.pro, "P", "api")["tmux"], {})  # left tmux: cleared on the next registration

    def test_opt_out(self):
        reg(self.pro, "P", "api", tmux=PANE)
        old = bus.SHARE_PANES
        bus.SHARE_PANES = False
        try:
            self.assertNotIn("tmux", self.pro.hello()["agents"][0])
        finally:
            bus.SHARE_PANES = old

    def test_peer_panes_reach_directory_cleaned(self):
        reg(self.pro, "P", "api", tmux=PANE)
        srv = self.pro.listen("127.0.0.1", local=False)
        try:
            self.air.discover(["pro"])
        finally:
            srv.shutdown()
            srv.server_close()
        a = next(a for a in self.air.directory()["agents"] if a["host"] == "pro")
        self.assertEqual(a["tmux"], PANE)
        # a peer that sends junk gets it dropped
        self.air.store.x("UPDATE peers SET agents=? WHERE host='pro'",
                         '[{"address": "pro/x/y", "tmux": {"socket": "/s", "pane": "$(id)"}, "login": "a;b"}]')
        a = next(a for a in self.air.directory()["agents"] if a["host"] == "pro")
        self.assertEqual((a["tmux"], a["login"]), ({}, ""))

    def test_attach_command(self):
        from tailshare.agent import attach_command

        a = {"host": "pro", "login": "Glaciar", "tmux": {**PANE, "socket": "/tmp/my sock"}}
        cmd = attach_command(a, here="air")
        self.assertTrue(cmd.startswith("ssh -t Glaciar@pro '"), cmd)
        import shlex

        remote = shlex.split(shlex.split(cmd)[3])
        self.assertEqual(remote, ["/opt/homebrew/bin/tmux", "-S", "/tmp/my sock", "attach", "-t", "%7", ";",
                                  "select-window", "-t", "%7", ";", "select-pane", "-t", "%7"])
        self.assertFalse(attach_command(a, here="pro").startswith("ssh"))
        self.assertEqual(attach_command({"host": "pro", "tmux": {}}), "")


class UpdateTrigger(unittest.TestCase):
    def test_only_same_tailnet_user(self):
        from tailshare import update

        b = make_bus("srv", StaticNet("srv", user="nick@example.com",
                                      usermap={"100.1.1.1": "nick@example.com", "100.2.2.2": "ana@other.org"}))
        b.updater = update.Updater(restart=lambda: None)
        b.updater.trigger = lambda: {"result": "checking for updates"}
        self.assertEqual(b.update_trigger("100.1.1.1")["result"], "checking for updates")
        for ip in ("100.2.2.2", "8.8.8.8"):
            with self.assertRaises(bus.BusError) as cm:
                b.update_trigger(ip)
            self.assertEqual(cm.exception.code, 403)


class Updater(unittest.TestCase):
    """The update decision logic, with installs and network faked out."""

    def setUp(self):
        from tailshare import update

        self.u = update
        self.saved = {k: getattr(update, k) for k in ("install_info", "latest_commit", "find_uv", "_uv_install", "supervised", "STATE")}
        self.saved_run = update.subprocess.run
        update.STATE = Path(tempfile.mkdtemp()) / "update.json"
        update.find_uv = lambda: "/bin/uv"
        update.supervised = lambda: True
        self.installs = []
        update._uv_install = lambda uv, url, c: (self.installs.append(c), type("CP", (), {"returncode": 0, "stderr": "", "stdout": ""})())[1]
        self.info = {"url": "https://github.com/o/r", "git": True, "commit": "a" * 40, "editable": False}
        update.install_info = lambda: dict(self.info)
        self.restarts = []

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.u, k, v)
        self.u.subprocess.run = self.saved_run

    def updater(self, import_ok=True):
        self.u.subprocess.run = lambda *a, **k: type("CP", (), {"returncode": 0 if import_ok else 1, "stderr": "boom"})()
        return self.u.Updater(restart=lambda: self.restarts.append(1))

    def test_up_to_date(self):
        self.u.latest_commit = lambda url, ref="main": "a" * 40
        self.assertIn("up to date", self.updater().check()["result"])
        self.assertEqual(self.installs, [])

    def test_updates_and_restarts(self):
        self.u.latest_commit = lambda url, ref="main": "b" * 40
        r = self.updater().check()
        self.assertIn("restarting", r["result"])
        self.assertEqual(self.installs, ["b" * 40])
        time.sleep(1.3)
        self.assertEqual(self.restarts, [1])

    def test_rollback_and_remember_bad(self):
        self.u.latest_commit = lambda url, ref="main": "c" * 40
        up = self.updater(import_ok=False)
        self.assertIn("rolled back", up.check()["result"])
        self.assertEqual(self.installs, ["c" * 40, "a" * 40])
        self.assertIn("failed to load before", up.check()["result"])  # not retried
        self.assertEqual(len(self.installs), 2)

    def test_editable_skipped(self):
        self.info["editable"] = True
        self.assertIn("editable", self.updater().check()["result"])

    def test_trigger_rate_limited(self):
        up = self.updater()
        self.assertEqual(up.trigger()["result"], "checking for updates")
        self.assertEqual(up.trigger()["result"], "check already requested")


if __name__ == "__main__":
    unittest.main()
