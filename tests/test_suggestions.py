"""Squad change suggestions (docs/suggestions.md): agents propose, the owner applies."""

import argparse
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from tailshare import agent, bus, keys, mcp, reports, squads_cli, suggestions
from tests.test_bus import reg
from tests.test_squads import TOKEN, Machine, member

APP = "/u/projects/app"


class Base(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self, "air", usermap={"127.0.0.1": "nick@example.com", "100.9.9.9": "eve@example.com",
                                               "100.1.1.1": "nick@example.com"})
        self.m.put("sq1", name="Tailshare", shares=[{"user": "eve@example.com"}], members=[
            {"id": "lead", **member(name="Lead", folder="/u/projects/lead")},
            {"id": "m1", **member(reports_to="lead")}])
        reg(self.m.bus, "pm", "pm", cwd="/u/projects/pm")  # not a member: the bootstrap case
        reg(self.m.bus, "s1", "app", cwd=APP, squad={"id": "sq1", "member": "m1"})

    def r(self, method, path, body=None, local=True, ip="127.0.0.1", qs=None, token=""):
        return self.m.squads.route(method, "/squads/sq1" + path, body or {}, local, ip, qs or {}, token)

    def version(self):
        return self.r("GET", "")["version"]

    def suggest(self, aid="pm", ops=None, why="it helps", base=None):
        return self.r("POST", "/suggestions", {"agent": aid, "base": base or self.version(), "why": why,
                                               "ops": ops if ops is not None else [{"op": "set", "member": "m1", "field": "role",
                                                               "value": "the iOS app"}]})["suggestion"]

    def refused(self, code, *a, **kw):
        with self.assertRaises(bus.BusError) as c:
            self.suggest(*a, **kw)
        self.assertEqual(c.exception.code, code, c.exception.extra)
        return c.exception

    def apply(self, sg, base=None, token=TOKEN):
        return self.r("POST", f"/suggestions/{sg['id']}/apply", {"base": base or sg["base"]}, token=token)


class Writing(Base):
    def test_a_self_join_from_an_agent_that_isnt_a_member(self):
        sg = self.suggest(ops=[{"op": "add_member", "self": True, "member": {"name": "PM", "role": "planning",
                                                                             "reports_to": "lead"}}])
        self.assertRegex(sg["id"], r"^sg_[0-9A-Za-z]{22}$")
        self.assertEqual((sg["status"], sg["host"], sg["why"], sg["resolved"]), ("pending", "air", "it helps", None))
        self.assertEqual(sg["by"], {"agent": "air/claude/pm", "host": "air", "member": None})
        d = sg["diff"][0]
        self.assertEqual((d["op"], d["self"]), ("add_member", True))
        self.assertEqual({k: d["member"][k] for k in ("name", "host", "kind", "folder", "reports_to")},
                         {"name": "PM", "host": "air", "kind": "claude", "folder": "/u/projects/pm", "reports_to": "lead"})
        listed = self.r("GET", "/suggestions")
        self.assertEqual(([s["id"] for s in listed["suggestions"]], listed["version"]), ([sg["id"]], sg["base"]))
        self.assertEqual(self.r("GET", f"/suggestions/{sg['id']}")["suggestion"]["id"], sg["id"])

    def test_a_member_suggests_and_the_diff_shows_before_and_after(self):
        sg = self.suggest("s1", ops=[{"op": "set", "member": "m1", "field": "instructions", "value": "Ship‮ it.\nNow."}])
        self.assertEqual(sg["by"]["member"], "m1")
        self.assertEqual(sg["diff"], [{"op": "set", "member": "m1", "name": "AppDev", "field": "instructions",
                                       "before": "You are AppDev.", "after": "Ship it.\nNow."}])

    def test_refusals(self):
        add = lambda **m: [{"op": "add_member", "member": {"name": "X", "host": "air", "kind": "claude",  # noqa: E731
                                                           "folder": "/u/projects/x", **m}}]
        for ops, field in [
            ([], "ops"), ([{"op": "set", "member": "m1", "field": "role", "value": "r"}] * 9, "ops"),
            (add(start={"prompt": "go"}), "ops.0.member"),  # the owner sets start
            ([{"op": "set", "member": "m1", "field": "folder", "value": "/etc"}], "ops.0.field"),
            ([{"op": "set", "member": "m1", "field": "start", "value": {}}], "ops.0.field"),
            ([{"op": "set", "member": "nope", "field": "role", "value": "r"}], "ops.0.member"),
            ([{"op": "remove_member", "member": "nope"}], "ops.0.member"),
            ([{"op": "remove_member", "member": "lead"}], "ops.0.member"),  # m1 reports to it
            ([{"op": "shares", "value": []}], "ops.0.op"),
            (add(host="elsewhere"), "ops.0.member.host"),  # not one of the owner's machines
            (add(reports_to="new:0"), "ops.0.member.reports_to"),  # only earlier ones
            ([{"op": "set", "member": "lead", "field": "reports_to", "value": "m1"}], "ops.reports_to"),  # a loop
            (add(name="AppDev", folder=APP), "ops.members.new:0.name"),  # same address as m1
            ([{"op": "add_member", "self": True, "member": {"name": "PM", "folder": "/tmp"}}], "ops.0.member.folder"),
        ]:
            e = self.refused(400, ops=ops)
            self.assertEqual(e.extra["field"], field, ops)
        e = self.refused(400, "s1", ops=[{"op": "add_member", "self": True, "member": {"name": "Again"}}])
        self.assertIn("already", e.extra["reason"])
        self.assertEqual(self.refused(409, base="0" * 16).args[0], "stale")
        self.assertEqual(self.refused(404, "nobody").code, 404)
        self.assertEqual(self.refused(400, why="").extra["field"], "why")

    def test_an_added_member_on_another_of_the_owners_machines(self):
        self.m.net.peers = {"pro": {"ip": "100.1.1.1", "port": 1, "user": "nick@example.com"},
                            "eves": {"ip": "100.9.9.9", "port": 1, "user": "eve@example.com"}}
        for h in ("pro", "eves"):
            self.m.bus.store.x("INSERT INTO peers(host, ip) VALUES(?,?)", h, "x")
        self.suggest(ops=[{"op": "add_member", "member": {"name": "P", "host": "pro", "kind": "codex", "folder": "/p"}}])
        e = self.refused(400, ops=[{"op": "add_member", "member": {"name": "E", "host": "eves", "kind": "codex",
                                                                   "folder": "/p"}}])
        self.assertEqual(e.extra["field"], "ops.0.member.host")

    def test_limits_and_repeats(self):
        first = self.suggest()
        self.assertEqual(self.suggest()["id"], first["id"])  # the same again: that one
        for v in ("a", "b"):
            self.suggest(ops=[{"op": "set", "member": "m1", "field": "role", "value": v}])
        self.assertEqual(self.refused(429, ops=[{"op": "set", "member": "m1", "field": "role", "value": "c"}]).code, 429)
        self.m.bus.store.x("UPDATE squad_suggestions SET status='dismissed'")
        for i in range(7):
            self.m.bus.store.x("UPDATE squad_suggestions SET status='dismissed'")
            self.suggest(ops=[{"op": "set", "member": "m1", "field": "role", "value": f"r{i}"}])
        self.m.bus.store.x("UPDATE squad_suggestions SET status='dismissed'")
        self.assertIn("an hour", self.refused(429, ops=[{"op": "set", "member": "m1", "field": "role",
                                                          "value": "z"}]).extra["reason"])
        self.m.bus.store.x("UPDATE squad_suggestions SET created=0, status='pending' WHERE rowid IN "
                           "(SELECT rowid FROM squad_suggestions LIMIT 0)")
        for i in range(20):
            self.m.bus.store.x("INSERT INTO squad_suggestions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                               suggestions.new_id(), "nick@example.com", "sq1", "x", time.time(), "pending", "w",
                               "{}", f"other{i}", "[]", "[]", f"k{i}")
        reg(self.m.bus, "fresh", "f", cwd="/u/projects/f")
        self.assertIn("this squad", self.refused(429, "fresh").extra["reason"])


class Contract(Base):
    def test_names_are_unique_across_hosts_and_folders(self):
        self.refused(400, ops=[{"op": "add_member", "member": {
            "name": "APPDEV", "host": "air", "kind": "codex", "folder": "/different"}}])
        self.refused(400, ops=[{"op": "set", "member": "lead", "field": "name", "value": "appdev"}])
        self.suggest(ops=[{"op": "set", "member": "lead", "field": "name", "value": "App Dev"}])
        self.m.patch("sq1/members/lead", name="App Dev", base_rev=self.m.squads.squad("sq1")["members"][0]["rev"])
        self.refused(400, ops=[{"op": "set", "member": "m1", "field": "name", "value": "APP   DEV"}])

    def test_author_label_and_sequential_diff_survive_rename(self):
        sg = self.suggest("s1", ops=[
            {"op": "set", "member": "m1", "field": "role", "value": "first"},
            {"op": "set", "member": "m1", "field": "role", "value": "second"}])
        self.assertEqual(sg["by"]["name"], "AppDev")
        self.assertEqual(sg["diff"][1]["before"], "first")
        other = self.suggest(ops=[{"op": "set", "member": "m1", "field": "name", "value": "Renamed"}])
        self.apply(other)
        self.assertEqual(self.r("GET", f"/suggestions/{sg['id']}")["suggestion"]["by"]["name"], "AppDev")


class Applying(Base):
    def test_apply_writes_every_op_once(self):
        sg = self.suggest(ops=[
            {"op": "add_member", "self": True, "member": {"name": "PM"}},
            {"op": "add_member", "member": {"name": "Helper", "host": "air", "kind": "codex", "folder": "/u/h",
                                            "reports_to": "new:0"}},
            {"op": "set", "member": "m1", "field": "role", "value": "iOS"},
            {"op": "set", "member": "m1", "field": "reports_to", "value": "new:0"},
            {"op": "remove_member", "member": "lead"}])
        with self.assertRaises(bus.BusError) as c:  # agents have no token
            self.apply(sg, token="")
        self.assertEqual(c.exception.code, 403)
        out = self.apply(sg)
        self.assertEqual((out["suggestion"]["status"], out["suggestion"]["resolved"]["how"]), ("applied", "applied"))
        ms = {m["name"]: m for m in out["squad"]["members"]}
        self.assertEqual(set(ms), {"AppDev", "PM", "Helper"})
        self.assertEqual((ms["PM"]["folder"], ms["PM"]["reports_to"]), ("/u/projects/pm", None))
        self.assertEqual((ms["Helper"]["reports_to"], ms["AppDev"]["reports_to"]), (ms["PM"]["id"], ms["PM"]["id"]))
        self.assertEqual(ms["AppDev"]["role"], "iOS")
        self.assertTrue(ms["PM"]["id"].startswith("m_"))
        self.assertEqual(ms["PM"]["start"]["options"], {})  # the owner sets start
        with self.assertRaises(bus.BusError) as c:
            self.apply(sg, base=self.version())
        self.assertEqual(str(c.exception), "not_pending")
        a = reg(self.m.bus, "pm", "pm", cwd="/u/projects/pm", squad={"id": "sq1", "member": ms["PM"]["id"]})
        self.assertEqual(a["squad"], {"id": "sq1", "member": ms["PM"]["id"]})  # ts-agents' env on its next up binds it

    def test_a_change_makes_it_stale(self):
        sg = self.suggest()
        other = self.suggest(ops=[{"op": "set", "member": "lead", "field": "role", "value": "boss"}])
        self.apply(other)
        self.assertEqual(self.r("GET", f"/suggestions/{sg['id']}")["suggestion"]["status"], "stale")
        for base in (sg["base"], self.version()):
            with self.assertRaises(bus.BusError) as c:
                self.apply(sg, base=base)
            self.assertEqual((c.exception.code, str(c.exception)), (409, "stale"))
        fresh = self.suggest()
        with self.assertRaises(bus.BusError) as c:  # the app sends the base it showed
            self.apply(fresh, base=sg["base"])
        self.assertEqual(str(c.exception), "stale")

    def test_dismiss_and_expiry(self):
        sg = self.suggest()
        out = self.r("POST", f"/suggestions/{sg['id']}/dismiss", {}, token=TOKEN)["suggestion"]
        self.assertEqual(out["status"], "dismissed")
        with self.assertRaises(bus.BusError) as c:
            self.r("POST", f"/suggestions/{sg['id']}/dismiss", {}, token=TOKEN)
        self.assertEqual(str(c.exception), "not_pending")
        old = self.suggest(ops=[{"op": "set", "member": "m1", "field": "role", "value": "old"}])
        self.m.bus.store.x("UPDATE squad_suggestions SET created=? WHERE id=?", time.time() - 8 * 86400, old["id"])
        listed = {s["id"]: s for s in self.r("GET", "/suggestions")["suggestions"]}
        self.assertEqual(listed[sg["id"]]["status"], "dismissed")  # resolved today: still listed
        self.assertNotIn(old["id"], listed)  # expired a day ago
        self.assertEqual(self.r("GET", f"/suggestions/{old['id']}")["suggestion"]["status"], "expired")


class Who(Base):
    def test_peers(self):
        sg = self.suggest()
        with self.assertRaises(bus.BusError) as c:  # agents suggest on their own machine
            self.r("POST", "/suggestions", {"agent": "pm"}, local=False, ip="100.1.1.1")
        self.assertEqual(c.exception.code, 404)
        self.assertEqual(self.r("GET", "/suggestions", local=False, ip="100.1.1.1")["suggestions"][0]["id"], sg["id"])
        with self.assertRaises(bus.BusError) as c:  # a sharee sees the squad, not the suggestions
            self.r("GET", "/suggestions", local=False, ip="100.9.9.9")
        self.assertEqual(c.exception.code, 403)
        self.assertNotIn("version", self.r("GET", "", local=False, ip="100.9.9.9"))
        self.m.bus.store.x("INSERT INTO peers(host, ip) VALUES('pro', '100.1.1.1')")
        self.m.net.ipmap = {"100.1.1.1": "pro"}
        with self.assertRaises(bus.BusError) as c:  # a machine applies through its own local API
            self.r("POST", f"/suggestions/{sg['id']}/apply", {"base": sg["base"]}, local=False, ip="100.1.1.1")
        self.assertEqual(c.exception.code, 403)
        with self.assertRaises(bus.BusError) as c:
            self.r("GET", "/suggestions/sg_nope")
        self.assertEqual(c.exception.code, 404)


class Approval(unittest.TestCase):
    def test_squad_change(self):
        sid = suggestions.new_id()
        self.assertEqual(reports.approval({"type": "squad_change", "host": "air", "suggestion": sid}, "a"),
                         {"type": "squad_change", "host": "air", "suggestion": sid})
        for bad in ({"type": "squad_change", "host": "air", "suggestion": "sg_x"},
                    {"type": "squad_change", "host": "a b", "suggestion": sid},
                    {"type": "squad_change", "host": "air", "suggestion": sid, "ops": []}):
            with self.assertRaises(bus.BusError):
                reports.approval(bad, "a")


class Cli(Base):
    def parse(self, *args):
        p = argparse.ArgumentParser()
        squads_cli.add_parser(p.add_subparsers())
        return p.parse_args(["squad", *args])

    def call(self, path, body=None, timeout=15):
        from urllib.parse import urlsplit, parse_qsl
        url = urlsplit(path)
        return self.m.squads.route("GET" if body is None else "POST", url.path, body or {}, True,
                                  "127.0.0.1", dict(parse_qsl(url.query)))

    def setUp(self):
        super().setUp()
        self.me = {"id": "pm", "squad": {"id": "sq1", "member": "lead"}}
        for name, patch in [
            ("call", mock.patch.object(agent, "call", side_effect=self.call)),
            ("tag", mock.patch.object(agent, "squad_tag", return_value={})),
            ("register", mock.patch.object(agent, "register", return_value=self.me)),
            ("detect", mock.patch.object(agent, "detect")),
        ]:
            setattr(self, name + "_mock", patch.start())
            self.addCleanup(patch.stop)

    def run_cmd(self, *args):
        parsed = self.parse(*args)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            parsed.fn(parsed)
        return out.getvalue()

    def test_suggest_resolves_names_and_reparents_before_removing(self):
        out = self.run_cmd("suggest", "Tailshare", "--set", "AppDev.reports_to=null", "--remove", "Lead",
                           "--why", "new structure")
        sg = json.loads(out)["suggestion"]
        self.assertEqual(sg["ops"], [{"op": "set", "member": "m1", "field": "reports_to", "value": None},
                                     {"op": "remove_member", "member": "lead"}])
        self.assertEqual(sg["base"], self.version())
        self.assertIn(sg["id"], self.run_cmd("suggestions", "--json"))

    def test_files_and_join(self):
        with tempfile.TemporaryDirectory() as td:
            add, instructions = td + "/member.json", td + "/instructions.txt"
            with open(add, "w") as f:
                json.dump({"name": "Helper", "host": "air", "kind": "codex", "folder": "/helper"}, f)
            with open(instructions, "w") as f:
                f.write("Whole instructions.\nSecond line.")
            sg = json.loads(self.run_cmd("suggest", "--add", add, "--join", "PM", "--role", "planner",
                                         "--instructions", "AppDev", instructions, "--why", "help"))["suggestion"]
        self.assertEqual([o["op"] for o in sg["ops"]], ["add_member", "add_member", "set"])
        self.assertTrue(sg["ops"][1]["self"])
        self.assertEqual(sg["ops"][2]["value"], "Whole instructions.\nSecond line.")

    def test_apply_displays_whole_diff_and_uses_displayed_base(self):
        sg = self.suggest(ops=[{"op": "set", "member": "m1", "field": "instructions", "value": "All of it.\nEnd."}])
        with mock.patch.object(squads_cli, "_human_check"), mock.patch("builtins.input", return_value="n"), \
                mock.patch.object(squads_cli, "_write") as write:
            out = self.run_cmd("apply", sg["id"])
            write.assert_not_called()
            self.assertIn("You are AppDev.", out)
            self.assertIn("All of it.\\nEnd.", out)
        def changed(*args):
            self.m.patch("sq1/members/m1", role="changed elsewhere", base_rev=self.m.squads.squad("sq1")["members"][1]["rev"])
            return "y"
        with mock.patch.object(squads_cli, "_human_check"), mock.patch("builtins.input", side_effect=changed), \
                mock.patch.object(squads_cli, "_write", side_effect=lambda method, path, body:
                    self.m.squads.route(method, path, body, True, "127.0.0.1", {}, TOKEN)):
            with self.assertRaises(bus.BusError) as c:
                self.run_cmd("apply", sg["id"])
        self.assertEqual(str(c.exception), "stale")

    def test_agents_cannot_apply_or_dismiss(self):
        with mock.patch.dict(os.environ, {keys.AGENT_ENV[0]: "yes"}), \
                mock.patch.object(squads_cli, "_write") as write:
            for cmd in ("apply", "dismiss"):
                with self.assertRaises(ValueError):
                    self.run_cmd(cmd, "sg_" + "a" * 22)
            write.assert_not_called()

    def test_mcp_proposes_but_never_applies(self):
        ops = [{"op": "set", "member": "m1", "field": "role", "value": "MCP"}]
        with mock.patch.object(mcp, "_identity"):
            out = json.loads(mcp._tool("squad", {"action": "suggest", "ops": ops, "why": "help"}))
            self.assertEqual(out["suggestion"]["ops"], ops)
            self.assertEqual(out["suggestion"]["status"], "pending")
            with self.assertRaises(ValueError):
                mcp._tool("squad", {"action": "apply"})


if __name__ == "__main__":
    unittest.main()
