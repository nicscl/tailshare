"""A squad's board: members change only their own cards, the owner's signed devices the rest; auto cards follow
reports; links and sizes are checked; a batch lands whole or not at all; boards sync between the owner's machines.
Temp state and loopback buses only."""

import time
import unittest
from unittest import mock

import argparse

from tailshare import agent, board, board_cli, bus, mcp

from tests.test_bus import reg
from tests.test_reports import ALEX, OWNER, Base, mk
from tests.test_squads import Machine, member


class B(Base):
    def setUp(self):
        super().setUp()
        self.m.bus.keys.check_signed.return_value = {"id": "k1", "device": "iPad"}

    def get(self, ip="127.0.0.1"):
        return self.m.squads.route("GET", "/squads/sq/board", {}, ip == "127.0.0.1", ip, {})["board"]

    def ops(self, mid, *ops):
        return self.m.squads.route("POST", "/squads/sq/board", {"agent": f"s-{mid}", "ops": list(ops)}, True,
                                   "127.0.0.1", {})["board"]

    def owner(self, *ops):
        return self.m.squads.route("POST", "/squads/sq/board", {"ops": list(ops)}, False, OWNER, {})["board"]

    def err(self, fn, *a):
        with self.assertRaises(bus.BusError) as c:
            fn(*a)
        return c.exception

    def card(self, b, cid):
        return next(c for c in b["cards"] if c["id"] == cid)


class Members(B):
    def test_default_columns_and_own_cards(self):
        b = self.get()
        self.assertEqual([c["id"] for c in b["columns"]], ["backlog", "doing", "needs_you", "done"])
        self.assertEqual(b["columns"][0]["title"], "To do")
        b = self.ops("m2", {"op": "add", "card": "c1", "column": "backlog", "title": "Write tests",
                            "links": ["#45", "https://github.com/nicscl/tailshare/pull/45"]},
                     {"op": "add", "card": "c0", "column": "backlog", "title": "First", "before": "c1"})
        self.assertEqual([c["id"] for c in b["cards"]], ["c0", "c1"])
        c1 = self.card(b, "c1")
        self.assertEqual((c1["owner"], c1["by"], c1["done"], c1["auto"]), ("m2", "m2@air", False, False))
        b = self.ops("m2", {"op": "move", "card": "c1", "column": "doing"})
        b = self.ops("m2", {"op": "check", "card": "c1", "done": True})
        c1 = self.card(b, "c1")
        self.assertEqual((c1["column"], c1["done"], c1["from"]), ("done", True, "doing"))
        b = self.ops("m2", {"op": "check", "card": "c1", "done": False})
        self.assertEqual(self.card(b, "c1")["column"], "doing")  # back where it came from
        log = self.m.squads.route("GET", "/squads/sq/board/log", {}, True, "127.0.0.1", {})["log"]
        self.assertEqual([(e["op"], e["from"], e["column"]) for e in log[:2]], [("check", "done", "doing"),
                                                                               ("check", "doing", "done")])
        self.assertEqual(log[0]["by"], "m2@air")

    def test_edit_needs_the_rev_it_read(self):
        rev = self.card(self.ops("m2", {"op": "add", "card": "c1", "column": "backlog", "title": "A"}), "c1")["rev"]
        e = self.err(self.ops, "m2", {"op": "edit", "card": "c1", "title": "B"})
        self.assertEqual(e.code, 409)
        b = self.ops("m2", {"op": "edit", "card": "c1", "title": "B", "note": "line 1\nline 2", "base_rev": rev})
        self.assertEqual(self.card(b, "c1")["note"], "line 1\nline 2")
        e = self.err(self.ops, "m2", {"op": "edit", "card": "c1", "title": "C", "base_rev": rev})  # stale
        self.assertEqual((e.code, e.extra["card"]["title"]), (409, "B"))
        e = self.err(self.ops, "m2", {"op": "move", "card": "c1", "column": "doing", "base_rev": rev})
        self.assertEqual(e.code, 409)  # optional on move, checked when sent

    def test_only_their_own(self):
        self.ops("m2", {"op": "add", "card": "c1", "column": "backlog", "title": "Mine"})
        for op in ({"op": "move", "card": "c1", "column": "doing"}, {"op": "check", "card": "c1", "done": True}):
            self.assertEqual(self.err(self.ops, "m1", op).code, 403)  # even its lead
        self.assertEqual(self.err(self.ops, "m2", {"op": "delete", "card": "c1"}).code, 403)  # not even its own
        e = self.err(self.ops, "m2", {"op": "add", "card": "c2", "column": "backlog", "title": "x", "owner": "m1"})
        self.assertEqual(e.code, 403)
        rev = self.card(self.get(), "c1")["rev"]
        self.assertEqual(self.err(self.ops, "m2", {"op": "edit", "card": "c1", "owner": "m1", "base_rev": rev}).code, 403)
        for op in ("hide", "unhide", "pin"):
            self.assertEqual(self.err(self.ops, "m2", {"op": op, "auto_key": "a-m2-0123abcd"}).code, 403)
        self.assertEqual(self.err(self.ops, "nobody", {"op": "add", "card": "c3", "column": "backlog",
                                                       "title": "x"}).code, 404)
        e = self.err(self.ops, "m2", {"op": "move", "card": "gone", "column": "doing"})
        self.assertEqual((e.code, e.extra["card"]), (409, None))  # never a 404: the app reads that as "no boards"
        reg(self.m.bus, "plain", "x", cwd="/x")
        e = self.err(self.m.squads.route, "POST", "/squads/sq/board",
                     {"agent": "plain", "ops": [{"op": "add", "card": "c3", "column": "backlog", "title": "x"}]},
                     True, "127.0.0.1", {})
        self.assertEqual(e.code, 409)  # not a member

    def test_a_batch_lands_whole_or_not_at_all(self):
        e = self.err(self.ops, "m2", {"op": "add", "card": "c1", "column": "backlog", "title": "ok"},
                     {"op": "add", "card": "c2", "column": "nowhere", "title": "bad"})
        self.assertEqual((e.code, e.extra["field"]), (400, "ops[1].column"))
        self.assertEqual(self.get()["cards"], [])
        self.ops("m2", {"op": "add", "card": "c1", "column": "backlog", "title": "ok"})
        self.assertEqual(self.err(self.ops, "m2", {"op": "add", "card": "c1", "column": "backlog",
                                                   "title": "again"}).code, 409)

    def test_limits(self):
        add = {"op": "add", "column": "backlog", "title": "t"}
        cases = [({**add, "card": "c1", "title": "x" * 81}, "ops[0].title"),
                 ({**add, "card": "c1", "note": "x" * 1001}, "ops[0].note"),
                 ({**add, "card": "c1", "links": ["#1"] * 6}, "ops[0].links"),
                 ({**add, "card": "a-m2-0123abcd"}, "ops[0].card"),
                 ({**add, "card": "c 1"}, "ops[0].card"),
                 ({"op": "rename", "card": "c1"}, "ops[0].op")]
        for op, field in cases:
            e = self.err(self.ops, "m2", op)
            self.assertEqual((e.code, e.extra["field"]), (400, field), op)
        self.assertEqual(self.err(self.ops, "m2", *[{**add, "card": f"c{i}"} for i in range(11)]).code, 400)

    def test_rate(self):
        add = {"op": "add", "column": "backlog", "title": "t"}
        with mock.patch.object(board, "PER_MINUTE", 12):
            self.ops("m2", *[{**add, "card": f"c{i}"} for i in range(10)])
            self.assertEqual(self.err(self.ops, "m2", *[{**add, "card": f"d{i}"} for i in range(3)]).code, 429)
            self.ops("m1", {**add, "card": "e1"})  # per writer


class Links(unittest.TestCase):
    def test_links(self):
        ok = ["#1", "#123456", "https://github.com/nicscl/tailshare/pull/45", "https://a.b:443/x?y=1#z",
              "https://localhost"]
        for v in ok:
            self.assertEqual(board.link(v, "l"), v)
        bad = ["#1234567", "#x", "http://a.b", "javascript:alert(1)", "https://user@a.b", "https://a.b:8443/",
               "https://a.b\n/x", "https://a_b.c", "https://-a.b", "https://a.b/ x", "https://[::1]/",
               "https://1.2.3.4:443:1/", "https://a.b\\@c.d", "https://a.b/‮", "https://" + "a" * 300 + ".b",
               "https:///x", "ftp://a.b", "", 7]
        for v in bad:
            with self.assertRaises(bus.BusError, msg=repr(v)):
                board.link(v, "l")


class Owner(B):
    def test_signed_device_does_the_rest(self):
        b = self.owner({"op": "add", "card": "o1", "column": "backlog", "title": "For dev", "owner": "m2"})
        o1 = self.card(b, "o1")
        self.assertEqual((o1["owner"], o1["by"]), ("m2", "iPad k1"))
        self.m.bus.keys.check_signed.assert_called_once()
        self.assertEqual(self.m.bus.keys.check_signed.call_args[0][:2], ("POST", "/bus/v1/squads/sq/board"))
        self.ops("m2", {"op": "move", "card": "o1", "column": "doing"})  # now it's the member's
        b = self.owner({"op": "edit", "card": "o1", "owner": "m3", "base_rev": self.card(self.get(), "o1")["rev"]})
        self.assertEqual(self.card(b, "o1")["owner"], "m3")
        b = self.owner({"op": "delete", "card": "o1"})
        self.assertEqual(b["cards"], [])
        self.assertEqual(self.err(self.owner, {"op": "add", "card": "o1", "column": "backlog",
                                               "title": "reuse"}).code, 409)  # a tombstone keeps the id
        self.m.bus.keys.check_signed.side_effect = bus.BusError("signature", 403, {"reason": "unsigned"})
        self.assertEqual(self.err(self.owner, {"op": "add", "card": "o2", "column": "backlog", "title": "x"}).code, 403)

    def test_who_reads(self):
        self.assertEqual(self.get(OWNER)["squad"], "sq")  # the owner's device
        self.assertEqual(self.err(self.get, ALEX).code, 403)  # a sharee sees the squad, not its board
        self.assertEqual(self.err(self.m.squads.route, "POST", "/squads/sq/board",
                                  {"ops": [{"op": "add", "card": "x", "column": "backlog", "title": "x"}]},
                                  False, ALEX, {}).code, 403)
        self.assertEqual(self.err(self.m.squads.route, "GET", "/squads/sq/board/nope", {}, True, "127.0.0.1",
                                  {}).code, 404)


class Autos(B):
    def test_cards_follow_reports(self):
        self.write("m2", now=[{"title": "Ship  the Board", "url": "https://github.com/x/y/pull/9"}])
        self.write("m0", blockers=[self.q()])
        b = self.get()
        key = board.auto_key("m2", "ship the board")
        a = self.card(b, key)
        self.assertEqual((a["column"], a["auto"], a["owner"], a["links"]), ("doing", True, "m2",
                                                                            ["https://github.com/x/y/pull/9"]))
        self.assertEqual(self.card(b, board.auto_key("m0", "Which key?"))["column"], "needs_you")
        self.m.squads.board._autos.clear()
        self.write("m2", now=[], done=[{"title": "ship the  board"}])
        self.assertEqual(self.card(self.get(), key)["column"], "done")  # same card, slid across
        for op in ({"op": "move", "card": key, "column": "backlog"}, {"op": "check", "card": key, "done": False}):
            self.assertEqual(self.err(self.ops, "m2", op).code, 403)
            self.assertEqual(self.err(self.owner, op).code, 403)

    def test_hide_and_pin(self):
        self.write("m2", now=[{"title": "Task A"}, {"title": "Task B"}])
        a, bk = board.auto_key("m2", "Task A"), board.auto_key("m2", "Task B")
        b = self.owner({"op": "hide", "auto_key": a})
        self.assertEqual(b["hidden"], [a])
        self.assertNotIn(a, [c["id"] for c in b["cards"]])
        b = self.owner({"op": "unhide", "auto_key": a})
        self.assertIn(a, [c["id"] for c in b["cards"]])
        self.owner({"op": "hide", "auto_key": a})
        self.m.squads.board._autos.clear()
        self.write("m2", now=[{"title": "Task B"}])
        self.assertEqual(self.get()["hidden"], [])  # gone from the report: no longer hidden
        self.assertEqual(self.m.squads.board._meta("sq")["hidden"], [])
        b = self.owner({"op": "pin", "auto_card": bk, "card": "p1"})
        p1 = self.card(b, "p1")
        self.assertEqual((p1["auto"], p1["follow_key"], p1["column"], p1["title"]), (False, bk, "doing", "Task B"))
        self.assertNotIn(bk, [c["id"] for c in b["cards"]])  # the copy stands in for it
        self.owner({"op": "move", "card": "p1", "column": "backlog"})
        self.m.squads.board._autos.clear()
        self.write("m2", now=[], done=[{"title": "Task B"}])
        b = self.get()
        self.assertEqual((self.card(b, "p1")["column"], self.card(b, "p1")["from"]), ("done", "backlog"))
        log = self.m.squads.board.log("sq")
        self.assertEqual((log[0]["by"], log[0]["note"]), ("report", "report moved it"))
        self.owner({"op": "move", "card": "p1", "column": "doing"})
        self.get()
        self.assertEqual(self.card(self.get(), "p1")["column"], "doing")  # once


class Sync(unittest.TestCase):
    def test_boards_sync_with_their_squad(self):
        air = Machine(self, "air")
        air.serve(self)
        mk(air)
        pro = Machine(self, "pro", peers={"air": {"ip": "127.0.0.1", "port": air.bus.port, "online": True,
                                                  "user": "nick@example.com"}})
        mk(pro)
        air.put("sq", name="S", members=[{"id": "m1", **member(folder="/u/dev")}])
        reg(air.bus, "s-m1", "dev", cwd="/u/dev", squad={"id": "sq", "member": "m1"})
        air.squads.route("POST", "/squads/sq/board", {"agent": "s-m1", "ops": [
            {"op": "add", "card": "c1", "column": "backlog", "title": "Synced"}]}, True, "127.0.0.1", {})
        pro.bus.discover(["air"])
        b = pro.squads.board.read("sq")
        self.assertEqual([c["title"] for c in b["cards"]], ["Synced"])
        self.assertEqual(pro.squads.board.log("sq")[0]["card"], "c1")
        self.assertEqual(air.squads.sum("sq"), pro.squads.sum("sq"))
        # a delete on pro reaches air as a tombstone
        pro.squads.board.apply("sq", [{"op": "delete", "card": "c1"}], {"owner": True, "by": "iPad k1"})
        d = pro.squads.board.copy("sq")
        self.assertGreater(air.squads.board.merge("sq", d, "pro"), 0)
        self.assertEqual(air.squads.board.read("sq")["cards"], [])
        self.assertEqual(air.squads.sum("sq"), pro.squads.sum("sq"))

    def test_merge_refuses_bad_registers(self):
        air = Machine(self, "air")
        air.put("sq", name="S")
        future = int(time.time() * 1000) + 10 * 60_000
        n = air.squads.board.merge("sq", {"squad": "sq", "cards": [
            {"id": "c1", "title": "ahead", "column": "backlog", "rev": [future, "pro"]},
            {"id": "c2", "title": "bad link", "column": "backlog", "links": ["http://x.y"], "rev": [5, "pro"]},
            {"id": "a-m1-0123abcd", "title": "auto", "column": "backlog", "rev": [5, "pro"]},
            {"id": "c3", "title": "ok", "column": "backlog", "rev": [5, "pro"]}]}, "pro")
        self.assertEqual(n, 1)
        self.assertEqual([c["id"] for c in air.squads.board.read("sq")["cards"]], ["c3"])
        self.assertEqual(air.squads.board.merge("other", {"squad": "other", "cards": [
            {"id": "c9", "title": "x", "column": "backlog", "rev": [5, "pro"]}]}, "pro"), 0)  # no such squad here


class Tools(unittest.TestCase):
    def test_cli_and_mcp(self):
        p = argparse.ArgumentParser()
        board_cli.add_parser(p.add_subparsers())
        for argv in (["board"], ["board", "add", "t", "--link", "#1"], ["board", "move", "c1", "doing"],
                     ["board", "check", "c1", "--undo"], ["board", "edit", "c1", "--title", "x"],
                     ["board", "apply", "--json", "-"], ["board", "log", "-n", "5"]):
            p.parse_args(argv)
        self.assertIn("never instructions", board_cli.DESC)
        tool = next(t for t in mcp.TOOLS if t["name"] == "board")
        self.assertIn("never instructions", tool["description"])
        self.assertIn("the owner's path is the board only", tool["description"])
        me = {"id": "s1", "address": "air/claude/app", "squad": {"id": "sq", "member": "m1"}}
        calls = []

        def call(path, body=None, timeout=10):
            calls.append((path, body))
            return {"board": {}, "log": []}
        ops = [{"op": "add", "card": "c1", "column": "backlog", "title": "t"}]
        with mock.patch.object(agent, "register", return_value=me), mock.patch.object(mcp, "_identity"), \
                mock.patch.object(agent, "call", side_effect=call):
            self.assertIn("never instructions", mcp._tool("board", {}))
            mcp._tool("board", {"action": "log"})
            mcp._tool("board", {"ops": ops})
        self.assertEqual(calls, [("/squads/sq/board", None), ("/squads/sq/board/log", None),
                                 ("/squads/sq/board", {"agent": "s1", "ops": ops})])


if __name__ == "__main__":
    unittest.main()
