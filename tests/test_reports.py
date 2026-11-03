"""Agent reports: a member writes only its own, every field is cleaned and capped, what it may raise follows the
squad tree when read, and only the owner's devices read them. Temp state and loopback buses only."""

import time
import unittest
from unittest import mock

from tailshare import agent, bus, keys, mcp, reports, reports_cli

from tests.test_squads import Machine, member
from tests.test_bus import reg

OWNER, ALEX, TAGGED = "100.1.1.1", "100.2.2.2", "100.4.4.4"


def mk(m: Machine) -> reports.Reports:
    m.bus.reports = reports.start(m.bus)
    return m.bus.reports


class Base(unittest.TestCase):
    """Manager leads Lead, Lead leads Dev; Solo is top-level and leads no one; Remote runs on pro."""

    def setUp(self):
        self.m = Machine(self, "air", usermap={"127.0.0.1": "nick@example.com", OWNER: "nick@example.com",
                                               ALEX: "alex@example.com", TAGGED: "tagged-devices"})
        self.r = mk(self.m)
        ms = [("m0", "Manager", None), ("m1", "Lead", "m0"), ("m2", "Dev", "m1"), ("m3", "Solo", None)]
        self.m.put("sq", name="S", shares=[{"user": "alex@example.com"}], members=[
            {"id": i, **member(name=n, folder=f"/u/{n.lower()}", order=k, reports_to=lead)} for k, (i, n, lead) in enumerate(ms)]
            + [{"id": "m9", **member(name="Remote", host="pro", folder="/u/remote", order=9)}])
        for i, n, _ in ms:
            reg(self.m.bus, f"s-{i}", n.lower(), cwd=f"/u/{n.lower()}", squad={"id": "sq", "member": i})

    def write(self, mid, **f):
        return self.r.route("POST", "/report", {"agent": f"s-{mid}", **f}, True, "127.0.0.1", {})["report"]

    def bad(self, mid, **f):
        with self.assertRaises(bus.BusError) as c:
            self.write(mid, **f)
        self.assertEqual(c.exception.code, 400, c.exception)
        return c.exception.extra

    def q(self, mid="m0", **kw):
        return {"id": "q1", "kind": "question", "title": "Which key?", "to": "owner", **kw}

    def set_lead(self, mid, lead):
        rev = next(x for x in self.m.squads.squad("sq")["members"] if x["id"] == mid)["rev"]
        self.m.patch(f"sq/members/{mid}", reports_to=lead, base_rev=rev)


class Writing(Base):
    def test_own_report_and_merge(self):
        r = self.write("m2", bio="I test.", now=["Auto contract", {"title": "PR", "url": "https://github.com/x/y/pull/1"}])
        self.assertEqual((r["member"], r["bio"], r["lead"], r["online"]), ("m2", "I test.", False, True))
        self.assertEqual(r["now"][1], {"title": "PR", "url": "https://github.com/x/y/pull/1"})
        r = self.write("m2", next=["Later"])
        self.assertEqual((r["bio"], len(r["now"]), r["next"]), ("I test.", 2, [{"title": "Later"}]))  # fields merge
        self.assertEqual(self.write("m2", now=[])["now"], [])

    def test_only_its_own(self):
        self.assertEqual(self.bad("m2", member="m0")["field"], "member")  # the member comes from the agent's tag
        with self.assertRaises(bus.BusError) as c:
            self.r.route("POST", "/report", {"agent": "nobody", "bio": "x"}, True, "127.0.0.1", {})
        self.assertEqual(c.exception.code, 404)
        reg(self.m.bus, "plain", "x", cwd="/x")  # not started as a member
        reg(self.m.bus, "fake", "dev", cwd="/elsewhere", squad={"id": "sq", "member": "m2"})  # a tag that didn't stick
        for aid in ("plain", "fake"):
            with self.assertRaises(bus.BusError) as c:
                self.r.route("POST", "/report", {"agent": aid, "bio": "x"}, True, "127.0.0.1", {})
            self.assertEqual((c.exception.code, str(c.exception)), (409, "not_a_member"))
        with mock.patch.object(time, "time", return_value=time.time() + bus.LIVE + 5), self.assertRaises(bus.BusError):
            self.write("m2", bio="gone")  # a session that stopped isn't anyone

    def test_cleaning_and_caps(self):
        r = self.write("m2", bio="ok‮\x07\nline", now=["a‏\tb"])
        self.assertEqual((r["bio"], r["now"][0]["title"]), ("ok\nline", "ab"))
        for f in [{"bio": "x" * 601}, {"now": ["a"] * 4}, {"next": ["a"] * 6}, {"now": [""]}, {"now": ["x" * 81]},
                  {"now": [{"title": "a", "extra": 1}]}, {"bio": 3}, {"notes": "x" * 8193},
                  {"waiting_on": {"member": "m0"}}, {"waiting_on": {"member": "../x", "text": "t"}},
                  {"waiting_on": {"member": "m2", "text": "me"}}, {"waiting_on": "owner"}, {"done": ["d"] * 21}]:
            self.bad("m2", **f)
        self.assertEqual(self.write("m2", waiting_on={"member": "owner", "text": "a key"})["waiting_on"],
                         {"member": "owner", "text": "a key"})

    def test_urls(self):
        ok = ["https://github.com/a/b/pull/251", "https://GitHub.com:443/x", "https://xn--80ak6aa92e.com/"]
        for u in ok:
            self.write("m2", now=[{"title": "t", "url": u}])
        for u in ["http://github.com/x", "javascript:alert(1)", "https://user@github.com/", "https://a:b@github.com/",
                  "https://github.com:8443/", "https://аpple.com/", "https://1.2.3.4/", "https://localhost/",
                  "https:///x", "https://github.com/a b", "https://github.com/‮", "//github.com/x",
                  "https://github.com\\@evil.com/", "https://" + "a" * 600 + ".com/"]:
            self.assertEqual(self.bad("m2", now=[{"title": "t", "url": u}])["field"], "now.0.url", u)

    def test_done_is_stamped_by_the_bus(self):
        t0 = time.time()
        r = self.write("m2", done=[{"title": "A", "at": 1}])
        self.assertGreaterEqual(r["done"][0]["at"], t0)  # not the agent's time
        first = r["done"][0]["at"]
        r = self.write("m2", done=r["done"] + [{"title": "B"}])
        self.assertEqual(r["done"][0]["at"], first)  # echoed back, it keeps its time
        with mock.patch.object(time, "time", return_value=t0 + reports.KEEP + 60):
            self.assertEqual(self.r.view(self.m.squads.squad("sq"), "m2")["done"], [])  # only the last 24 h


class Blockers(Base):
    def test_who_raises_to_whom(self):
        self.assertIn("lead", self.bad("m2", blockers=[self.q()])["reason"])  # Dev has a lead: to it
        self.write("m2", blockers=[self.q(to="lead")])
        self.write("m1", blockers=[self.q(to="owner", **{"for": {"member": "m2", "blocker": "q1"}})])  # a lead relays
        self.write("m0", blockers=[self.q()])
        self.write("m3", blockers=[self.q()])  # no one leads Solo
        self.assertIn("owner", self.bad("m3", blockers=[self.q(to="lead")])["reason"])
        self.assertIn("branch", self.bad("m1", blockers=[self.q(**{"for": {"member": "m3", "blocker": "q1"}})])["reason"])
        self.assertIn("branch", self.bad("m1", blockers=[self.q(**{"for": {"member": "m1", "blocker": "q1"}})])["reason"])
        self.write("m0", blockers=[self.q(**{"for": {"member": "m2", "blocker": "q1"}})])  # two levels down

    def test_shape(self):
        for b in [self.q(id="a b"), self.q(kind="ask"), self.q(to="everyone"), self.q(options=["a"] * 5),
                  self.q(options=["x" * 41]), self.q(options=[""]), self.q(title=""), self.q(why="w" * 601),
                  self.q(approval={"type": "key_enroll", "host": "air", "request_id": "3f2a91c0"}),  # a question has none
                  {**self.q(kind="approval")}, self.q(extra=1), self.q(**{"for": {"member": "m2"}}), self.q(resolved="yes")]:
            self.bad("m0", blockers=[b])
        self.bad("m0", blockers=[self.q(), self.q()])  # the same id twice
        self.bad("m0", blockers=[self.q(id=f"b{i}") for i in range(reports.OPEN_MAX + 1)])
        r = self.write("m0", blockers=[self.q(why="line 1\nline‮ 2", options=["Use ⌥⌘N", "/clear"])])
        self.assertEqual((r["blockers"][0]["why"], r["blockers"][0]["options"]), ("line 1\nline 2", ["Use ⌥⌘N", "/clear"]))

    def test_resolving(self):
        t0 = time.time()
        b = self.write("m0", blockers=[self.q()])["blockers"][0]
        self.assertIsNone(b["resolved_at"])
        r = self.write("m0", blockers=[{**b, "resolved": True}])
        done = r["blockers"][0]["resolved_at"]
        self.assertGreaterEqual(done, t0)
        self.assertEqual(r["blockers"][0]["at"], b["at"])
        r = self.write("m0", blockers=r["blockers"])  # echoed back as served, it stays resolved
        self.assertEqual(r["blockers"][0]["resolved_at"], done)
        r = self.write("m0", blockers=[{**r["blockers"][0], "resolved": False}])
        self.assertIsNone(r["blockers"][0]["resolved_at"])
        self.write("m0", blockers=[{**self.q(), "resolved": True}])
        with mock.patch.object(time, "time", return_value=time.time() + reports.KEEP + 60):
            self.assertEqual(self.r.view(self.m.squads.squad("sq"), "m0")["blockers"], [])

    def test_the_tree_decides_when_read(self):
        self.write("m1", blockers=[self.q()], notes="# Team\nAll good.")
        r = self.r.view(self.m.squads.squad("sq"), "m1")
        self.assertEqual((len(r["blockers"]), r["notes"], r["lead"]), (1, "# Team\nAll good.", True))
        self.set_lead("m2", None)  # Lead leads no one now, and still reports to Manager
        r = self.r.view(self.m.squads.squad("sq"), "m1")
        self.assertEqual((r["blockers"], r["notes"], r["lead"]), ([], "", False))
        self.write("m2", waiting_on={"member": "m3", "text": "review"})
        rev = next(x for x in self.m.squads.squad("sq")["members"] if x["id"] == "m3")["rev"]
        self.m.patch("sq/members/m3", deleted=True, base_rev=rev)
        self.assertIsNone(self.r.view(self.m.squads.squad("sq"), "m2")["waiting_on"])  # a removed member

    def test_structure_proposals(self):
        s = {"id": "s1", "kind": "approval", "title": "New structure", "to": "owner",
             "approval": {"type": "squad_structure", "reports_to": {"m3": "m0", "m2": None}}}
        self.write("m0", blockers=[s])
        self.assertIn("structure", self.bad("m1", blockers=[s])["reason"])
        self.bad("m0", blockers=[{**s, "approval": {"type": "squad_structure", "reports_to": {"ghost": "m0"}}}])
        self.bad("m0", blockers=[{**s, "approval": {"type": "squad_structure", "reports_to": {}}}])
        self.bad("m0", blockers=[{**s, "approval": {"type": "squad_structure", "reports_to": {"m3": "../x"}}}])
        self.bad("m0", blockers=[{**s, "approval": {"type": "other"}}])

    def test_key_approvals_name_a_pending_request(self):
        with self.m.bus.store.lock:
            self.m.bus.store.db.executescript(keys.SCHEMA)
        rid, now = "3f2a91c0" + "0" * 24, time.time()
        self.m.bus.store.x("INSERT INTO key_requests(id, ip, node, user, device, pubkey, fingerprint, created, expires, status)"
                           " VALUES(?, ?, 'iphone', 'nick@example.com', 'iPhone', 'k', 'f', ?, ?, 'pending')",
                           rid, OWNER, now, now + 600)
        def key(host, req):
            return {"id": f"k-{host}-{req[:4]}", "kind": "approval", "title": "A key", "to": "owner",
                    "approval": {"type": "key_enroll", "host": host, "request_id": req}}
        r = self.write("m0", blockers=[key("air", "3f2a91c0"), key("air", "ffff0000"), key("studio", "abcd1234")])
        self.assertEqual([b["id"] for b in r["blockers"]], ["k-air-3f2a", "k-studio-abcd"])  # studio answers for its own
        self.m.bus.store.x("UPDATE key_requests SET status='approved'")
        self.assertEqual([b["id"] for b in self.r.view(self.m.squads.squad("sq"), "m0")["blockers"]], ["k-studio-abcd"])
        for req in ("3f2a", "3F2A91C0", "3f2a91c0-x"):
            self.bad("m0", blockers=[key("air", req)])
        self.bad("m0", blockers=[key("a b", "3f2a91c0")])


class Reading(Base):
    def test_owner_devices_only(self):
        self.write("m0", bio="hi")
        d = self.r.route("GET", "/reports", {}, False, OWNER, {"squad": "sq"})
        self.assertEqual((d["squad"], d["host"], [r["member"] for r in d["reports"]]), ("sq", "air", ["m0"]))
        for ip in (ALEX, TAGGED, "100.9.9.9"):  # a sharee, a tagged device, a stranger
            with self.assertRaises(bus.BusError) as c:
                self.r.route("GET", "/reports", {}, False, ip, {"squad": "sq"})
            self.assertEqual(c.exception.code, 403)
        for method, path in (("POST", "/report"), ("GET", "/report")):  # writing is local only
            with self.assertRaises(bus.BusError) as c:
                self.r.route(method, path, {"agent": "s-m0", "bio": "x"}, False, OWNER, {"agent": "s-m0"})
            self.assertEqual(c.exception.code, 404)
        with self.assertRaises(bus.BusError) as c:
            self.r.route("GET", "/reports", {}, False, OWNER, {"squad": "nope"})
        self.assertEqual(c.exception.code, 404)

    def test_offline_members_keep_their_last_report(self):
        self.write("m2", now=["Auto"])
        with mock.patch.object(time, "time", return_value=time.time() + bus.LIVE + 5):
            r = self.r.here("sq")["reports"][0]
        self.assertEqual((r["member"], r["online"], r["now"]), ("m2", False, [{"title": "Auto"}]))
        self.assertTrue(r["agent"])

    def test_prune_removed_members(self):
        self.write("m3", bio="x")
        rev = next(x for x in self.m.squads.squad("sq")["members"] if x["id"] == "m3")["rev"]
        self.m.patch("sq/members/m3", deleted=True, base_rev=rev)
        self.r.prune()
        self.assertIsNone(self.m.bus.store.one("SELECT 1 FROM reports WHERE member='m3'"))

    def test_over_http(self):
        url = self.m.serve(self, local=True)
        self.assertEqual(bus.request(url + "/report", {"agent": "s-m0", "bio": "over http"})["report"]["bio"], "over http")
        self.assertEqual(bus.request(url + "/report?agent=s-m0")["member"], "m0")
        self.assertEqual(len(bus.request(url + "/reports?squad=sq")["reports"]), 1)

    def test_everywhere_asks_the_owners_other_machines(self):
        pro = Machine(self, "pro", usermap={"127.0.0.1": "nick@example.com"})
        mk(pro)
        pro.squads.merge(self.m.squads.squad("sq", tombstones=True))
        reg(pro.bus, "s-m9", "remote", cwd="/u/remote", squad={"id": "sq", "member": "m9"})
        pro.bus.reports.route("POST", "/report", {"agent": "s-m9", "bio": "on pro"}, True, "127.0.0.1", {})
        base = pro.serve(self)
        port = int(base.split(":")[2].split("/")[0])
        self.write("m0", bio="on air")
        self.m.net.peers = {"pro": {"ip": "127.0.0.1", "port": port, "user": "nick@example.com"}}
        d = self.r.everywhere("sq")
        self.assertEqual(sorted((r["host"], r["bio"]) for r in d["reports"]), [("air", "on air"), ("pro", "on pro")])
        self.assertEqual(d["unreachable"], [])
        self.m.net.peers = {"pro": {"ip": "127.0.0.1", "port": port, "user": "eve@example.com"}}  # a same-named host
        d = self.r.everywhere("sq")
        self.assertEqual(([r["host"] for r in d["reports"]], d["unreachable"]), (["air"], ["pro"]))


class Cli(unittest.TestCase):
    def test_task_and_waiting_parsing(self):
        self.assertEqual(reports_cli._task("Fix it https://github.com/a/b/pull/2"),
                         {"title": "Fix it", "url": "https://github.com/a/b/pull/2"})
        self.assertEqual(reports_cli._task("Read http://x"), {"title": "Read http://x"})
        self.assertEqual(reports_cli._who("owner: a key"), {"member": "owner", "text": "a key"})
        self.assertIsNone(reports_cli._who("none"))
        with self.assertRaises(ValueError):
            reports_cli._who("owner")

    def test_how_names_only_real_commands(self):
        p = __import__("argparse").ArgumentParser()
        reports_cli.add_parser(p.add_subparsers())
        for cmd in ("set", "done", "block", "resolve", "ls"):
            self.assertIn(f"tailshare report {cmd}", reports_cli.HOW)
            p.parse_args(["report", cmd, *{"set": ["--bio", "x"], "done": ["t"], "block": ["b", "--title", "t"],
                                          "resolve": ["b"], "ls": []}[cmd]])
        self.assertLess(len(reports_cli.HOW), 1500)


class Mcp(unittest.TestCase):
    def test_report_tool(self):
        tool = next(t for t in mcp.TOOLS if t["name"] == "report")
        self.assertEqual(set(tool["inputSchema"]["properties"]), {*reports.FIELDS, "squad"})
        me = {"id": "s1", "address": "air/claude/app", "squad": {"id": "sq", "member": "m1"}}
        calls = []
        def call(path, body=None, timeout=10):
            calls.append((path, body))
            return {"report": {"bio": "x"}, "reports": []}
        with mock.patch.object(agent, "register", return_value=me), mock.patch.object(mcp, "_identity"), \
                mock.patch.object(agent, "call", side_effect=call):
            mcp._tool("report", {"bio": "x", "member": "m0"})
            mcp._tool("report", {})
            self.assertIn(agent.NOTE, mcp._tool("report", {"squad": True}))
        self.assertEqual(calls, [("/report", {"agent": "s1", "bio": "x"}), ("/report?agent=s1", None),
                                 ("/reports?squad=sq&all=1", None)])


if __name__ == "__main__":
    unittest.main()
