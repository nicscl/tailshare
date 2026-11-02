"""Squads: the icon subset, validation, compare-and-set writes, what sharees see, and syncing between an owner's
machines. Everything runs on temp state and loopback buses; nothing touches ~/.tailshare or a real tmux."""

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tailshare import bus, squads, squads_cli, start, svg

from tests.test_bus import StaticNet, free_port, reg

TOKEN = "t" * 64
ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24px"><path d="M2 2L22 22z" fill="#fa0"/></svg>'


class Icons(unittest.TestCase):
    def test_canonical(self):
        self.assertEqual(svg.clean(ICON), '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
                                          '<path d="M 2 2 L 22 22 z" fill="#fa0"/></svg>')
        self.assertEqual(svg.clean(""), "")
        ok = ('<svg viewBox="0 0 10 10"><g transform="translate(1,2) rotate(45)"><circle cx="5" cy="5" r="2" '
              'stroke-linecap="round" stroke-linejoin="bevel"/><polygon points="0,0 1,1 2,0"/></g></svg>')
        self.assertIn('stroke-linecap="round"', svg.clean(ok))
        self.assertIn('d="M 0 0 a 1 1 0 1 0 2 2"', svg.clean('<svg viewBox="0 0 4 4"><path d="M0 0a1 1 0 102 2"/></svg>'))

    def test_refused(self):
        bad = [
            '<!DOCTYPE svg [<!ENTITY x "y">]><svg viewBox="0 0 1 1">&x;</svg>',
            '<svg viewBox="0 0 1 1"><!-- hi --></svg>',
            '<?xml-stylesheet href="x"?><svg viewBox="0 0 1 1"/>',
            '<svg viewBox="0 0 1 1"><script>alert(1)</script></svg>',
            '<svg viewBox="0 0 1 1" onload="x()"/>',
            '<svg viewBox="0 0 1 1"><image href="http://x/y.png"/></svg>',
            '<svg viewBox="0 0 1 1"><path d="M0 0" fill="url(#a)"/></svg>',
            '<svg viewBox="0 0 1 1"><path d="L0 0"/></svg>',
            '<svg viewBox="0 0 1 1"><circle r="1e9"/></svg>',
            '<svg viewBox="0 0 1 1"><circle r="nan"/></svg>',
            '<svg viewBox="0 0 1 1" xmlns:xlink="http://www.w3.org/1999/xlink"><g xlink:href="#a"/></svg>',
            '<svg viewBox="0 0 1 1">text</svg>',
            '<svg/>',
            '<g/>',
            '<svg viewBox="0 0 1 1"><svg viewBox="0 0 1 1"/></svg>',
            '<svg viewBox="0 0 1 1">' + "<g>" * 11 + "</g>" * 11 + "</svg>",
            '<svg viewBox="0 0 1 1"><path d="M0 0' + " L1 1" * 2001 + '"/></svg>',
            "x" * (svg.MAX_BYTES + 1),
        ]
        for b in bad:
            with self.assertRaises(svg.Bad, msg=b[:60]):
                svg.clean(b)


def member(**kw):
    return {"name": "AppDev", "role": "iOS app", "host": "air", "kind": "claude", "folder": "/u/projects/app",
            "instructions": "You are AppDev.", "start": {"options": {"model": "opus"}, "prompt": ""}, **kw}


class Machine:
    """A bus with squads on temp state, its local and peer APIs on loopback."""

    def __init__(self, test, host, user="nick@example.com", peers=None, usermap=None):
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.tmp, True)
        self.net = StaticNet(host, peers=peers or {}, user=user, usermap=usermap or {"127.0.0.1": user})
        self.bus = bus.Bus(bus.Store(self.tmp / "bus.db"), self.net, free_port())
        self.bus.keys = mock.Mock(token=TOKEN, **{"ssh_info.return_value": {"enroll": False, "host_keys": []}})
        self.squads = self.bus.squads = squads.start(self.bus)

    def serve(self, test, local=False):
        srv = self.bus.listen("127.0.0.1", local=local)
        test.addCleanup(srv.server_close)
        test.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{self.bus.port}/bus/v1"

    def put(self, sid, **body):
        return self.squads.route("PUT", f"/squads/{sid}", {"base_rev": None, **body}, True, "127.0.0.1", {}, TOKEN)

    def patch(self, path, **body):
        return self.squads.route("PATCH", f"/squads/{path}", body, True, "127.0.0.1", {}, TOKEN)


class Writes(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self, "air")

    def test_create_read_and_compare_and_set(self):
        sq = self.m.put("sq1", name="Tailshare", color="teal", icon=ICON, members=[{"id": "m1", **member()}])["squad"]
        self.assertEqual((sq["owner"], sq["name"], sq["members"][0]["name"]), ("nick@example.com", "Tailshare", "AppDev"))
        self.assertEqual(sq["members"][0]["instructions_from"], "")
        with self.assertRaises(squads.Conflict):  # PUT only creates
            self.m.put("sq1", name="Again")
        rev = sq["members"][0]["rev"]
        out = self.m.patch("sq1/members/m1", role="lead", base_rev=rev)["member"]
        self.assertEqual((out["role"], out["name"]), ("lead", "AppDev"))  # partial: the rest stays
        with self.assertRaises(squads.Conflict) as c:  # someone wrote since rev
            self.m.patch("sq1/members/m1", role="other", base_rev=rev)
        self.assertEqual(c.exception.code, 409)
        self.assertEqual(c.exception.extra["member"]["role"], "lead")
        with self.assertRaises(squads.Bad):
            self.m.patch("sq1/members/m1", role="x")  # no base_rev
        with self.assertRaises(squads.Conflict):
            self.m.patch("sq1/members/m1", base_rev=None, **member())  # null base_rev only creates
        self.m.patch("sq1/members/m2", base_rev=None, **member(name="Other"))
        self.assertEqual(len(self.m.squads.squad("sq1")["members"]), 2)
        h = self.m.squads.squad("sq1")
        self.m.patch("sq1", name="Renamed", base_rev=h["rev"])
        self.assertEqual(self.m.squads.squad("sq1")["name"], "Renamed")

    def test_delete_and_recreate(self):
        sq = self.m.put("sq1", name="A", members=[{"id": "m1", **member()}])["squad"]
        self.m.patch("sq1/members/m1", deleted=True, base_rev=sq["members"][0]["rev"])
        self.assertEqual(self.m.squads.squad("sq1")["members"], [])
        self.assertTrue(self.m.squads.squad("sq1", tombstones=True)["members"][0]["deleted"])
        h = self.m.squads.squad("sq1")
        self.m.patch("sq1", deleted=True, base_rev=h["rev"])
        self.assertIsNone(self.m.squads.squad("sq1"))
        self.assertIn("sq1", self.m.squads.hello("nick@example.com", True)["squads"])  # the delete spreads
        self.m.put("sq1", name="B")  # a deleted squad may be made again
        self.assertEqual(self.m.squads.squad("sq1")["name"], "B")

    def test_validation(self):
        bad = [{"name": ""}, {"name": "x" * 41}, {"name": "A", "color": "red"}, {"name": "A", "icon": "<svg/>"},
               {"name": "A", "shares": [{"user": "-x"}]}, {"name": "A", "shares": [{"user": "a@b", "can": ["attach"]}]},
               {"name": "A", "members": [member(kind="bash")]}, {"name": "A", "members": [member(folder="rel/path")]},
               {"name": "A", "members": [member(host="a b")]}, {"name": "A", "members": [member(instructions="x" * 4097)]},
               {"name": "A", "members": [member(start={"options": {"Bad-Key": 1}})]},
               {"name": "A", "members": [member(start={"options": {}, "prompt": "", "extra": 1})]},
               {"name": "A", "members": [member(instructions_from="-bad")]},
               {"name": "A", "members": [member(), member(role="dup")]}]  # same address
        for i, b in enumerate(bad):
            with self.assertRaises(squads.Bad, msg=str(b)[:80]):
                self.m.put(f"sq{i}", **b)
        self.assertEqual(self.m.squads.ids(deleted=True), [])  # a refused PUT leaves nothing behind
        self.assertEqual(squads.clean_member(member(name="App‮Dev\x07"))["name"], "AppDev")
        with self.assertRaises(bus.BusError):
            self.m.put("../x", name="A")

    def test_from_mark(self):
        sq = self.m.put("sq1", name="A", members=[{"id": "m1", **member(instructions_from="alex@example.com",
                                                                         start={"prompt": "p", "prompt_from": "alex@example.com"})}])["squad"]
        m = sq["members"][0]
        self.assertEqual((m["instructions_from"], m["start"]["prompt_from"]), ("alex@example.com", "alex@example.com"))
        m = self.m.patch("sq1/members/m1", role="r", base_rev=m["rev"])["member"]
        self.assertEqual(m["instructions_from"], "alex@example.com")  # an edit of something else keeps it
        m = self.m.patch("sq1/members/m1", instructions="mine", base_rev=m["rev"])["member"]
        self.assertEqual(m["instructions_from"], "")  # the owner's edit clears it

    def test_caps(self):
        with mock.patch.object(squads, "MAX_SQUADS", 2):
            self.m.put("a", name="A")
            self.m.put("b", name="B")
            with self.assertRaises(squads.Bad):
                self.m.put("c", name="C")

    def test_local_writes_need_the_token(self):
        with self.assertRaises(bus.BusError) as c:
            self.m.squads.route("PUT", "/squads/sq1", {"name": "A", "base_rev": None}, True, "127.0.0.1", {}, "wrong")
        self.assertEqual(c.exception.code, 403)

    def test_no_single_owner(self):
        self.m.net.user = "tagged-devices"
        with self.assertRaises(bus.BusError):
            self.m.put("sq1", name="A")
        self.assertEqual(self.m.squads.hello("tagged-devices", True), {})


class Views(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self, "air", usermap={"100.1.1.1": "nick@example.com", "100.2.2.2": "alex@example.com",
                                               "100.3.3.3": "eve@example.com"})
        self.m.put("sq1", name="A", shares=[{"user": "alex@example.com"}], members=[{"id": "m1", **member()}])
        self.m.put("sq2", name="Private")

    def get(self, ip, path="/squads"):
        return self.m.squads.route("GET", path, {}, False, ip, {"tombstones": "1"})

    def test_sharee_sees_a_filtered_copy(self):
        own = self.get("100.1.1.1")["squads"]
        self.assertEqual([s["id"] for s in own], ["sq1", "sq2"])
        alex = self.get("100.2.2.2")["squads"]
        self.assertEqual([s["id"] for s in alex], ["sq1"])
        self.assertNotIn("shares", alex[0])
        self.assertEqual(alex[0]["members"][0]["folder"], "app")  # a basename unless show_paths
        self.assertEqual(self.get("100.3.3.3")["squads"], [])
        with self.assertRaises(bus.BusError):
            self.get("100.3.3.3", "/squads/sq1")
        self.assertEqual(set(self.m.squads.hello("alex@example.com", False)["squads"]), {"sq1"})
        self.assertEqual(self.m.squads.hello("eve@example.com", False), {})

    def test_only_the_owner_writes(self):
        for ip in ("100.2.2.2", "100.3.3.3"):
            with self.assertRaises(bus.BusError) as c:
                self.m.squads.route("PATCH", "/squads/sq1", {"name": "X", "base_rev": None}, False, ip, {})
            self.assertEqual(c.exception.code, 403)

    def test_agent_hint(self):
        self.assertNotIn("agent", self.m.squads.squad("sq1")["members"][0])
        a = reg(self.m.bus, "s1", "app", squad={"id": "sq1", "member": "m1"})
        self.assertEqual(a["squad"], {"id": "sq1", "member": "m1"})
        self.assertEqual(self.m.squads.squad("sq1")["members"][0]["agent"], a["address"])
        self.assertEqual(reg(self.m.bus, "s2", "x", squad={"id": "../", "member": "m1"})["squad"], None)
        hello = self.m.bus.hello("100.1.1.1")
        self.assertIn("squad", next(x for x in hello["agents"] if x["address"] == a["address"]))
        self.assertTrue(all("squad" not in x for x in self.m.bus.hello("100.2.2.2")["agents"]))  # owner only


class Replication(unittest.TestCase):
    def test_lww_merge_converges(self):
        a, b = Machine(self, "air"), Machine(self, "pro")
        a.put("sq1", name="A", members=[{"id": "m1", **member()}])
        b.squads.merge(a.squads.squad("sq1", tombstones=True))
        self.assertEqual(a.squads.sum("sq1"), b.squads.sum("sq1"))
        # edits to different members on different machines both survive
        a.patch("sq1/members/m1", role="from-a", base_rev=a.squads.squad("sq1")["members"][0]["rev"])
        b.patch("sq1/members/m2", base_rev=None, **member(name="Two"))
        da, db = a.squads.squad("sq1", tombstones=True), b.squads.squad("sq1", tombstones=True)
        a.squads.merge(db)
        b.squads.merge(da)
        self.assertEqual(a.squads.squad("sq1"), b.squads.squad("sq1"))
        self.assertEqual(a.squads.sum("sq1"), b.squads.sum("sq1"))
        self.assertEqual({m["role"] for m in a.squads.squad("sq1")["members"]}, {"from-a", "iOS app"})

    def test_merge_refuses_bad_registers(self):
        a = Machine(self, "air")
        a.put("sq1", name="A")
        d = a.squads.squad("sq1", tombstones=True)
        future = int(time.time() * 1000) + 10 * 60_000
        d["members"] = [{"id": "m1", **member(), "rev": [future, "pro"]},  # too far ahead
                        {"id": "m2", **member(kind="bash"), "rev": [future // 2, "pro"]},  # invalid
                        {"id": "m3", **member(name="Ok"), "rev": [1, "pro"]}]
        self.assertEqual(a.squads.merge(d), 1)
        self.assertEqual([m["id"] for m in a.squads.squad("sq1")["members"]], ["m3"])
        self.assertEqual(a.squads.merge({**d, "owner": "eve@example.com"}), 0)  # not this owner's

    def test_sync_over_the_bus(self):
        """pro asks air's /hello like discover does and pulls what differs, including a register merged in
        under the max rev, which max_rev alone would miss."""
        air = Machine(self, "air")
        air.serve(self)
        pro = Machine(self, "pro", peers={"air": {"ip": "127.0.0.1", "port": air.bus.port, "online": True,
                                                  "user": "nick@example.com"}})
        air.put("sq1", name="A", members=[{"id": "m1", **member()}])
        pro.bus.discover(["air"])
        self.assertEqual(pro.squads.squad("sq1")["members"][0]["name"], "AppDev")
        # a register older than air's max arrives at air from a third machine
        d = air.squads.squad("sq1", tombstones=True)
        d["members"] = [{"id": "m9", **member(name="Late"), "rev": [d["rev"][0] - 5, "deck"]}]
        air.squads.merge(d)
        self.assertEqual(air.squads.max_rev("sq1"), pro.squads.max_rev("sq1"))
        pro.bus.discover(["air"])
        self.assertEqual(len(pro.squads.squad("sq1")["members"]), 2)
        self.assertEqual(air.squads.sum("sq1"), pro.squads.sum("sq1"))

    def test_no_sync_from_another_user(self):
        air = Machine(self, "air", user="eve@example.com")
        air.serve(self)
        air.put("sq1", name="Eve's")
        pro = Machine(self, "pro", peers={"air": {"ip": "127.0.0.1", "port": air.bus.port, "online": True,
                                                  "user": "eve@example.com"}})
        pro.bus.discover(["air"])
        self.assertEqual(pro.squads.ids(deleted=True), [])

    def test_http_errors_carry_the_conflict(self):
        air = Machine(self, "air")
        url = air.serve(self, local=True)
        h = {"X-Tailshare-Keys": TOKEN}
        sq = bus.request(f"{url}/squads/sq1", {"name": "A", "base_rev": None}, headers=h, method="PUT")["squad"]
        bus.request(f"{url}/squads/sq1", {"name": "B", "base_rev": sq["rev"]}, headers=h, method="PATCH")
        with self.assertRaises(bus.BusError) as c:
            bus.request(f"{url}/squads/sq1", {"name": "C", "base_rev": sq["rev"]}, headers=h, method="PATCH")
        self.assertEqual((c.exception.code, str(c.exception), c.exception.extra["squad"]["name"]), (409, "conflict", "B"))
        with self.assertRaises(bus.BusError) as c:
            bus.request(f"{url}/squads/sq2", {"name": "", "base_rev": None}, headers=h, method="PUT")
        self.assertEqual((c.exception.code, c.exception.extra["field"]), (400, "name"))
        self.assertEqual(bus.request(f"{url}/squads")["squads"][0]["name"], "B")


class Edit(unittest.TestCase):
    def test_changes_are_per_register_with_their_rev(self):
        m = Machine(self, "air")
        sq = m.put("sq1", name="A", members=[{"id": "m1", **member()}, {"id": "m2", **member(name="Two")}])["squad"]
        new = squads_cli._editable(sq)
        new["name"] = "Renamed"
        new["members"][0]["role"] = "lead"
        del new["members"][1]
        new["members"].append({k: v for k, v in member(name="Three").items()})
        todo = squads_cli.changes(sq, new)
        self.assertEqual([(x[1].rsplit("/", 1)[-1], sorted(x[2])) for x in todo], [
            ("sq1", ["base_rev", "name"]), ("m1", ["base_rev", "role"]),
            (todo[2][1].rsplit("/", 1)[-1], sorted([*member(), "order", "base_rev"])), ("m2", ["base_rev", "deleted"])])
        for method, path, body in todo:
            m.squads.route(method, path, body, True, "127.0.0.1", {}, TOKEN)
        self.assertEqual([x["name"] for x in m.squads.squad("sq1")["members"]], ["AppDev", "Three"])
        self.assertEqual(squads_cli.changes(m.squads.squad("sq1"), squads_cli._editable(m.squads.squad("sq1"))), [])

    def test_agents_cannot_edit(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), self.assertRaises(ValueError):
            squads_cli._human_check()


class Starting(unittest.TestCase):
    """A start item naming a squad member takes everything from this machine's copy of the squad."""

    def setUp(self):
        self.m = Machine(self, "air")
        (self.m.tmp / "projects" / "app").mkdir(parents=True)
        root = (self.m.tmp / "projects").resolve()
        p = mock.patch.object(bus, "STATE_DIR", self.m.tmp)
        p.start()
        self.addCleanup(p.stop)
        self.m.put("sq1", name="Tail Share", members=[
            {"id": "m1", **member(folder=str(root / "app"), instructions="Be AppDev.", start={"options": {"model": "opus"},
                                                                                              "prompt": "go"})},
            {"id": "m2", **member(name="Remote", host="pro", folder=str(root / "app"))}])
        self.cfg = {"roots": [str(root)], "skip_permissions": False, "max_running": 4,
                    "kinds": {"claude": {"bin": "/bin/echo", "options": ["model"]}}}
        b = mock.patch.object(start, "binary", return_value="/bin/echo")
        b.start()
        self.addCleanup(b.stop)

    def test_plan_from_the_squad(self):
        plan = start.build({"squad": {"id": "sq1", "member": "m1"}, "device_name": "iPhone"}, self.cfg, "safe", "air")
        self.assertEqual((plan["name"], plan["instructions"], plan["prompt"], plan["options"]),
                         ("AppDev", "Be AppDev.", "go", {"model": "opus"}))
        self.assertEqual((plan["env"]["TAILSHARE_SQUAD"], plan["env"]["TAILSHARE_MEMBER"]), ("sq1", "m1"))
        self.assertEqual(plan["tmux"], {"session": "squad-tail-share", "window": "appdev"})
        self.assertEqual(plan["squad"], {"id": "sq1", "name": "Tail Share", "member": "m1"})
        plan = start.build({"squad": {"id": "sq1", "member": "m1"}, "prompt": "other"}, self.cfg, "safe", "air")
        self.assertEqual(plan["prompt"], "other")

    def test_refusals(self):
        for item, err in [({"squad": {"id": "sq1", "member": "m2"}}, "bad_request"),  # runs on pro
                          ({"squad": {"id": "nope", "member": "m1"}}, "bad_request"),
                          ({"squad": {"id": "sq1", "member": "m1"}, "instructions": "mine"}, "bad_request"),
                          ({"squad": "sq1"}, "bad_request")]:
            with self.assertRaises(start.Refused) as c:
                start.build(item, self.cfg, "safe", "air")
            self.assertEqual(c.exception.out["error"], err, item)
        reg(self.m.bus, "s1", "app", squad={"id": "sq1", "member": "m1"})
        with self.assertRaises(start.Refused) as c:
            start.build({"squad": {"id": "sq1", "member": "m1"}}, self.cfg, "safe", "air")
        self.assertEqual(c.exception.out["error"], "running")

    def test_changed_since_the_last_start(self):
        con = start._db(self.m.tmp / "start.db")
        self.addCleanup(con.close)
        item = {"squad": {"id": "sq1", "member": "m1"}}
        plan = start.build(item, self.cfg, "safe", "air")
        self.assertEqual(start._changed(con, plan), "")
        con.execute("INSERT INTO member_starts VALUES('sq1','m1','old',?)", (time.time(),))
        self.assertIn("instructions changed", start._changed(con, plan))
        self.assertEqual(start._canon({**plan, "changed": "x"}), start._canon(plan))


if __name__ == "__main__":
    unittest.main()
