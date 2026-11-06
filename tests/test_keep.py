"""Keep running (tailshare/keep.py): each host brings its own squad members back when they are down, never two
copies, never more than their start key allows now; only tailshare's Stop ends one. All on temp state; start.launch is
mocked, never a real agent; tmux is never asked (the started windows are a flag here)."""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from tailshare import bus, dead, keep, revive, start
from tests.test_dead import S1, S2, S3, Base, started
from tests.test_squads import TOKEN, member

TAG = {"id": "sq", "member": "m1"}
LEAD = {"id": "sq", "member": "m0"}
CALLER = {"squad": "sq", "member": "m0"}
S4 = "0b0e0f2a-1c2d-4e5f-8a9b-0c1d2e3f4a5d"


def launched(plan, max_running):
    return {"session": plan["session"], "tmux": {"socket": "/tmp/tmux-1/ts-appdev", "pane": "%9", "bin": "/bin/tmux"}}


class Keep(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(start, "launch", side_effect=launched)
        self.launch = p.start()
        self.addCleanup(p.stop)
        self.windows = False
        p = mock.patch.object(keep._Windows, "__call__", lambda w, sq, m: self.windows)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(keep, "_capture")
        p.start()
        self.addCleanup(p.stop)
        self.m.bus.reports = mock.Mock(**{"flag.return_value": True})
        self.now = time.time()
        self.cfg = self.config()
        no_start = {"options": {}, "prompt": ""}
        self.m.put("sq", name="S", members=[
            {"id": "m0", **member(name="Lead", folder=self.app, start=no_start)},
            {"id": "m1", **member(name="AppDev", folder=self.app, reports_to="m0", start=no_start)},
            {"id": "m2", **member(name="Far", host="pro", folder=self.app, reports_to="m0", start=no_start)}])

    def tick(self, dt=0.0):
        return {s["name"]: s for s in keep.tick(self.m.bus, now=self.now + dt)}

    def migrated(self):
        con = start._db()
        con.execute("INSERT INTO keep_meta(k, v) VALUES('migrated', '0')")
        keep.set_run(con, "sq", "m0", "stopped", "owner")  # only AppDev's starts are counted here
        con.close()

    def lead_live(self, aid=S3):
        self.agent(aid, name="Lead", ago=5, squad=LEAD)

    def run_row(self, mid="m1"):
        con = start._db()
        try:
            return keep.run_of(con, "sq", mid)
        finally:
            con.close()

    # ------------------------------------------------------------------ starting

    def test_the_upgrade_leaves_members_that_were_not_running_stopped(self):
        self.lead_live()
        out = self.tick()
        self.launch.assert_not_called()
        self.assertEqual(out["AppDev"]["state"], "stopped")
        self.assertEqual(out["AppDev"]["by"], "upgrade")
        self.assertEqual(out["Lead"]["state"], "running")
        self.assertEqual(self.run_row("m0")["run"], "keep")  # running then: kept running from now on
        keep.change(self.m.bus, "sq", "m1", "keep", "owner")
        out = self.tick(1)
        self.assertEqual(out["AppDev"]["state"], "starting")
        plan = self.launch.call_args.args[0]
        self.assertEqual((plan["name"], plan["cwd"], plan["squad"]["member"], plan["env"]["TAILSHARE_STARTED_BY"]),
                         ("AppDev", self.app, "m1", keep.DEVICE))
        self.assertNotIn("resume", plan["options"])
        self.assertTrue(plan["session"])  # a fresh session with its own id, so the next try can resume it

    def test_resumes_its_last_session_then_starts_fresh_when_it_does_not_stay_up(self):
        self.migrated()
        started(self.tmp, TAG)
        self.agent(S1, ago=120, target="squad-s:0.0", squad=TAG)  # it crashed
        self.tick()
        plan = self.launch.call_args.args[0]
        self.assertEqual((plan["options"], plan["session"]), ({"resume": S1}, S1))
        self.tick(31)  # it never came up: the resume is not tried again
        plan = self.launch.call_args.args[0]
        self.assertNotIn("resume", plan["options"])
        self.assertNotEqual(plan["session"], S1)

    def test_never_another_members_session(self):
        self.migrated()
        started(self.tmp, LEAD)
        self.agent(S1, ago=120, name="AppDev", target="squad-s:0.0", squad=LEAD)  # the lead's, under this name
        con = start._db()
        try:
            k = keep._keep(con, "sq", "m1")
        finally:
            con.close()
        self.assertFalse(keep._resumable(S1, self.m.squads.squad("sq"),
                                         next(x for x in self.m.squads.squad("sq")["members"] if x["id"] == "m1")))
        with self.assertRaises(start.Refused):  # nor can one be asked for in the item
            start.build({"squad": TAG, "_resume": S1}, self.cfg, "safe", "air")
        self.assertEqual(k["tries"], 0)

    def test_backoff_forever_and_one_needs_you_with_the_last_output(self):
        self.migrated()
        keep.out_path("sq", "m1").parent.mkdir(exist_ok=True)
        keep.out_path("sq", "m1").write_bytes(b"\x1b[31mError:\x1b[0m bad config\r\n\x1b]0;title\x07bye\n")
        t = 0.0
        for _ in range(60):
            if self.launch.call_count == 9:
                break
            out = self.tick(t)["AppDev"]
            if out["state"] == "restarting":
                self.assertLessEqual(out["next_at"] - self.now - t, 600)
                t = out["next_at"] - self.now
            else:
                t += 1
        self.assertEqual(self.launch.call_count, 9)
        con = start._db()
        try:
            k = keep._keep(con, "sq", "m1")
        finally:
            con.close()
        self.assertEqual(k["tries"], 9)
        self.assertLessEqual(max(keep.delay(n) for n in range(20)), 600)
        (call,) = self.m.bus.reports.flag.call_args_list  # once, after FLAG_AFTER tries in a row
        sid, mid, b = call.args
        self.assertEqual((sid, mid, b["id"], b["to"], b["kind"]), ("sq", "m1", "keep", "owner", "question"))
        self.assertIn("Error: bad config\nbye", b["why"])
        self.assertNotIn("\x1b", b["why"])
        self.assertLessEqual(len(b["title"]), 80)

    def test_stays_up_then_the_count_starts_again_and_the_flag_clears(self):
        self.migrated()
        for i in range(keep.FLAG_AFTER + 1):
            self.tick(i * 700)
        self.assertEqual(self.m.bus.reports.flag.call_count, 1)
        t = (keep.FLAG_AFTER + 1) * 700
        self.agent(S2, ago=-t, squad=TAG)  # up now
        self.assertEqual(self.tick(t)["AppDev"]["state"], "running")
        self.m.bus.store.x("UPDATE agents SET last_seen=? WHERE id=?", self.now + t + keep.STABLE, S2)
        self.tick(t + keep.STABLE + 1)
        self.m.bus.reports.unflag.assert_called_once_with("sq", "m1", "keep")
        con = start._db()
        try:
            k = keep._keep(con, "sq", "m1")
        finally:
            con.close()
        self.assertEqual((k["tries"], k["flagged"], k["fresh"]), (0, 0, 0))

    def test_a_copy_already_there_is_not_a_failure(self):
        self.migrated()
        self.launch.side_effect = start.Refused("running", "name", "still running")
        self.assertEqual(self.tick()["AppDev"]["state"], "starting")
        con = start._db()
        try:
            self.assertEqual(keep._keep(con, "sq", "m1")["tries"], 0)
        finally:
            con.close()

    def test_a_started_window_waits_for_its_agent(self):
        self.migrated()
        self.tick()
        self.windows = True
        self.assertEqual(self.tick(40)["AppDev"]["state"], "starting")
        self.assertEqual(self.launch.call_count, 1)
        out = self.tick(keep.STARTUP + 1)  # never registered: left alone, it may wait on a prompt
        self.assertEqual((out["AppDev"]["state"], out["AppDev"].get("stuck")), ("starting", True))
        self.assertEqual(self.launch.call_count, 1)

    def test_off_on_this_host(self):
        self.migrated()
        (self.tmp / "start.json").unlink()
        self.assertEqual(self.tick()["AppDev"]["why"], "off")
        self.launch.assert_not_called()

    def test_only_its_own_hosts_members(self):
        self.migrated()
        self.assertNotIn("Far", self.tick())
        self.assertEqual(keep.managed(self.m.bus), {("sq", "m0"), ("sq", "m1")})

    # ------------------------------------------------------------------ never two copies

    def test_a_quiet_member_whose_process_runs_is_running(self):
        self.migrated()
        self.agent(S1, ago=300, squad=TAG)  # quiet past LIVE: a long tool call
        with mock.patch.object(dead, "pid_runs", return_value=True):
            self.assertEqual(self.tick()["AppDev"]["state"], "running")
        self.launch.assert_not_called()

    def test_a_quiet_member_outside_tmux_is_running_a_while(self):
        """Reviewer, #66: a member started by hand in Terminal registers pid 0 (MCP) and no pane; quiet past LIVE
        it must not get a fresh second copy."""
        self.migrated()
        self.agent(S1, ago=300, squad=TAG)
        self.m.bus.store.x("UPDATE agents SET tmux='', pid=0 WHERE id=?", S1)
        self.assertEqual(self.tick()["AppDev"]["state"], "running")
        self.launch.assert_not_called()
        self.assertIsNone(self.m.bus.register({"id": S2, "harness": "claude", "cwd": self.app,
                                               "session": "AppDev"})["squad"])  # nor does a hand-started one take it
        self.m.bus.store.x("UPDATE agents SET last_seen=? WHERE id=?", self.now - dead.UNSEEN - 1, S1)
        self.assertEqual(self.tick(1)["AppDev"]["state"], "running")  # long gone: the hand-started one is it now
        self.m.bus.store.x("DELETE FROM agents WHERE id=?", S2)
        self.tick(2)
        self.launch.assert_called_once()

    def test_an_mcp_registration_keeps_the_hooks_pid(self):
        self.m.bus.register({"id": S1, "harness": "claude", "cwd": self.app, "session": "AppDev", "pid": 4242})
        self.m.bus.register({"id": S1, "harness": "claude", "cwd": self.app, "session": "AppDev", "pid": 0})
        self.assertEqual(self.m.bus.store.one("SELECT pid FROM agents WHERE id=?", S1)["pid"], 4242)

    def test_a_session_started_by_hand_that_matches_is_running(self):
        self.migrated()
        self.agent(S1, ago=5, name="appdev")  # untagged, its folder and name (as a slug)
        self.assertEqual(self.tick()["AppDev"]["state"], "running")
        self.launch.assert_not_called()

    def test_a_member_ts_agents_keeps_is_left_to_it(self):
        self.migrated()
        self.agent(S1, ago=300, squad=TAG, sock=str(self.tmp / "ts-appdev"), target="appdev:0.0")
        with mock.patch.object(keep, "elsewhere", return_value=True) as e:
            st = self.tick()["AppDev"]
        self.assertEqual((st["state"], st["managed_by"]), ("running", "ts-agents"))
        self.launch.assert_not_called()
        self.assertTrue(dead._managed_elsewhere(json.loads(self.m.bus.store.one(
            "SELECT tmux FROM agents WHERE id=?", S1)["tmux"]), TAG, set()))
        e.assert_called_once()
        self.tick(1)  # its pane is gone: tailshare brings it back
        self.launch.assert_called_once()

    def test_an_ended_session_is_not_running(self):
        self.migrated()
        self.agent(S1, ago=5, squad=TAG)
        self.m.bus.store.x("UPDATE agents SET state=? WHERE id=?", json.dumps({"v": "ended", "at": self.now}), S1)
        with mock.patch.object(dead, "pid_runs", return_value=True):
            self.tick()
        self.launch.assert_called_once()

    # ------------------------------------------------------------------ the start key (#61)

    def key_row(self, grant="skip", key="k0123456789ab"):
        started(self.tmp, TAG)
        self.agent(S1, ago=120, target="squad-s:0.0", squad=TAG)
        con = start._db()
        con.execute("INSERT INTO launched(session, options, grant_, at, key) VALUES(?,?,?,?,?)",
                    (S1, "{}", grant, self.now, key))
        con.close()

    def test_a_removed_key_means_no_restart(self):
        self.migrated()
        self.key_row()
        with mock.patch.object(revive, "_key_grant", return_value=None):
            out = self.tick()["AppDev"]
        self.assertEqual((out["state"], out["why"]), ("blocked", "key"))
        self.launch.assert_not_called()
        self.assertEqual(self.m.bus.reports.flag.call_args.args[2]["id"], "keep")

    def test_a_narrowed_key_means_no_skip_restart(self):
        self.migrated()
        self.key_row()
        with mock.patch.object(revive, "_key_grant", return_value="safe"):
            self.assertEqual(self.tick()["AppDev"]["state"], "blocked")
        self.launch.assert_not_called()

    def test_the_key_carries_over_to_its_own_restarts(self):
        self.migrated()
        self.key_row(grant="safe")
        with mock.patch.object(revive, "_key_grant", return_value="safe"):
            self.tick()
        self.assertEqual(self.launch.call_count, 1)
        con = start._db()  # a Codex restart records no session: the key is kept with the member
        con.execute("DELETE FROM launched")
        con.close()
        with mock.patch.object(revive, "_key_grant", return_value=None):
            self.assertEqual(self.tick(31)["AppDev"]["state"], "blocked")
        self.assertEqual(self.launch.call_count, 1)

    def test_a_member_started_by_hand_restarts_safe(self):
        self.migrated()
        self.tick()
        self.assertFalse(self.launch.call_args.args[0]["skip"])

    def test_a_device_start_resets_it(self):
        self.migrated()
        con = start._db()
        try:
            keep.set_run(con, "sq", "m1", "stopped", "owner")
            keep.kept_by_start(con, "sq", "m1", "iPhone", "skip", "k0123456789ab")
            self.assertEqual(keep.run_of(con, "sq", "m1")["by"], "a device (iPhone)")
            k = keep._keep(con, "sq", "m1")
        finally:
            con.close()
        self.assertEqual((k["grant_"], k["key"], k["tries"]), ("skip", "k0123456789ab", 0))

    # ------------------------------------------------------------------ stop, keep, restart

    def test_stop_then_keep(self):
        self.migrated()
        out = keep.change(self.m.bus, "sq", "m1", "stopped", "Lead")
        self.assertEqual((out["run"], out["by"]), ("stopped", "Lead"))
        self.assertTrue(self.m.bus.keep_due)
        st = self.tick()["AppDev"]
        self.assertEqual((st["state"], st["by"]), ("stopped", "Lead"))
        self.launch.assert_not_called()
        keep.change(self.m.bus, "sq", "m1", "keep", "owner")
        self.tick(1)
        self.launch.assert_called_once()

    def test_stop_closes_only_what_tailshare_started(self):
        self.migrated()
        self.agent(S1, ago=5, squad=TAG)
        with mock.patch.object(start, "close_started", return_value={"error": "not_started"}) as c:
            out = keep.change(self.m.bus, "sq", "m1", "stopped", "owner")
        c.assert_called_once_with(S1, stop=False)
        self.assertFalse(out["closed"])
        self.assertIn("not started by tailshare", out["reason"])
        st = self.tick()["AppDev"]
        self.assertEqual((st["state"], st.get("still_running")), ("stopped", True))
        with mock.patch.object(start, "close_started", return_value={"closed": True}):
            out = keep.change(self.m.bus, "sq", "m1", "restart", "owner")
        self.assertEqual((out["run"], out["closed"]), ("keep", True))

    def test_a_close_from_a_device_is_a_stop(self):
        keep.stopped_by_close("sq", "m1")
        self.assertEqual(self.run_row(), {"run": "stopped", "by": "a device (Close)", "at": mock.ANY})

    def test_another_hosts_member_is_refused_here(self):
        with self.assertRaises(bus.BusError) as c:
            keep.change(self.m.bus, "sq", "m2", "stopped", "owner")
        self.assertEqual((c.exception.code, c.exception.extra), (409, {"host": "pro"}))
        with self.assertRaises(bus.BusError):
            keep.change(self.m.bus, "sq", "m1", "pause", "owner")

    # ------------------------------------------------------------------ who may

    def test_the_lead_may_its_member_may_not(self):
        self.lead_live(S3)
        self.agent(S4, ago=5, squad=TAG)
        with mock.patch.object(start, "close_started", return_value={"closed": True}):
            out = keep.route_local(self.m.bus, {"agent": S3, "member": "appdev", "run": "restart"}, "")
        self.assertEqual((out["by"], out["run"]), ("Lead", "keep"))
        for target in ("Lead", "AppDev"):  # not its lead, not itself
            with self.assertRaises(bus.BusError) as c:
                keep.route_local(self.m.bus, {"agent": S4, "member": target, "run": "stopped"}, "")
            self.assertEqual(c.exception.code, 403)

    def test_a_session_that_is_no_member_needs_the_owners_token(self):
        self.agent(S1, ago=5, name="Other")
        with self.assertRaises(bus.BusError) as c:
            keep.route_local(self.m.bus, {"agent": S1, "member": "AppDev", "run": "stopped"}, "")
        self.assertEqual(c.exception.code, 403)
        out = keep.route_local(self.m.bus, {"agent": S1, "member": "AppDev", "run": "stopped"}, TOKEN)
        self.assertEqual(out["by"], "owner")
        with self.assertRaises(bus.BusError) as c:  # the owner runs it on that machine, or uses the app
            keep.route_local(self.m.bus, {"agent": S1, "member": "Far", "run": "stopped"}, TOKEN)
        self.assertEqual(c.exception.code, 409)

    def test_a_lead_reaches_a_member_on_another_machine(self):
        self.lead_live(S3)
        self.m.net.peers = {"pro": {"ip": "100.9.9.9", "port": 1, "online": True}}
        with mock.patch.object(bus, "request", return_value={"ok": 1}) as r:
            keep.route_local(self.m.bus, {"agent": S3, "member": "Far", "run": "stopped"}, "")
        url, doc = r.call_args.args[:2]
        self.assertEqual(url, "http://100.9.9.9:1/bus/v1/member/run")
        self.assertEqual(doc, {"v": 1, "caller": CALLER, "squad": "sq", "member": "m2", "run": "stopped"})

    def test_the_peer_route_checks_where_the_lead_runs(self):
        self.m.net.ipmap = {"100.1.1.1": "air", "100.9.9.9": "pro"}
        self.m.net.usermap = {"100.1.1.1": "nick@example.com", "100.9.9.9": "nick@example.com"}
        doc = {"v": 1, "caller": CALLER, "squad": "sq", "member": "m1", "run": "stopped"}
        with self.assertRaises(bus.BusError) as c:  # the lead runs on air, not on pro
            keep.route_peer(self.m.bus, "100.9.9.9", doc)
        self.assertEqual(c.exception.code, 403)
        with self.assertRaises(bus.BusError):  # the member may not change its lead
            keep.route_peer(self.m.bus, "100.1.1.1", {**doc, "caller": {"squad": "sq", "member": "m1"}, "member": "m0"})
        self.assertEqual(keep.route_peer(self.m.bus, "100.1.1.1", doc)["by"], "Lead")

    def test_the_apps_route(self):
        out = self.m.squads.route("POST", "/squads/sq/members/m1/run", {"run": "stopped"}, True, "127.0.0.1", {},
                                  TOKEN)
        self.assertEqual((out["run"], out["by"]), ("stopped", "owner"))
        with self.assertRaises(bus.BusError):  # the local API without the owner's token
            self.m.squads.route("POST", "/squads/sq/members/m1/run", {"run": "keep"}, True, "127.0.0.1", {}, "")

    # ------------------------------------------------------------------ what devices see

    def test_hello_and_machines_that_are_off(self):
        self.migrated()
        self.tick()
        self.assertEqual({s["name"]: s["state"] for s in keep.hello()}, {"Lead": "stopped", "AppDev": "starting"})
        self.m.net.peers = {"pro": {"ip": "100.9.9.9", "online": False, "last_seen": "2026-11-05T20:00:00Z"}}
        (off,) = keep.offline(self.m.bus)
        self.assertEqual((off["name"], off["state"], off["since"]), ("Far", "host_offline", "2026-11-05T20:00:00Z"))

    def test_the_revive_watchdog_leaves_members_to_it(self):
        self.config(revive=True)
        started(self.tmp, TAG)
        self.agent(S1, ago=120, target="squad-s:0.0", squad=TAG)
        self.assertEqual(revive.tick(self.m.bus, now=self.now), [])
        self.launch.assert_not_called()


class Match(Base):
    """A session started by hand becomes the member it matches (#62), never in place of one that still runs."""

    def setUp(self):
        super().setUp()
        self.m.put("sq", name="S", members=[{"id": "m1", **member(name="AppDev", folder=self.app)}])

    def reg(self, aid, session="AppDev", **kw):
        return self.m.bus.register({"id": aid, "harness": "claude", "cwd": self.app, "session": session, **kw})

    def test_a_hand_named_session_is_the_member(self):
        self.assertEqual(self.reg(S1)["squad"], TAG)
        self.assertIsNone(self.reg(S2, session="Other")["squad"])
        self.assertIsNone(self.m.bus.register({"id": S3, "harness": "codex", "cwd": self.app,
                                               "session": "AppDev"})["squad"])

    def test_not_while_a_quiet_member_still_runs(self):
        self.agent(S1, ago=300, squad=TAG)  # quiet past LIVE, its process still there
        self.m.bus.store.x("UPDATE agents SET pid=4242 WHERE id=?", S1)
        with mock.patch.object(dead, "pid_runs", side_effect=lambda pid: pid == 4242):
            self.assertIsNone(self.reg(S2)["squad"])
            self.assertEqual(self.reg(S1, squad=TAG, pid=4242)["squad"], TAG)  # it keeps the member
        with mock.patch.object(dead, "pid_runs", return_value=False):
            self.m.bus.store.x("UPDATE agents SET last_seen=? WHERE id=?", time.time() - 300, S1)
            self.assertEqual(self.reg(S2)["squad"], TAG)  # gone now: the hand-started one takes it

    def test_only_one_match_counts(self):
        self.m.put("sq2", name="T", members=[{"id": "m9", **member(name="AppDev", folder=self.app)}])
        self.assertIsNone(self.reg(S1)["squad"])


if __name__ == "__main__":
    unittest.main()
