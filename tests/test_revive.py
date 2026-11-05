"""The revive watchdog (tailshare/revive.py): a crashed session tailshare started is reopened with a resume, with
backoff and caps, and the owner is told past a cap. All on temp state; start.launch is mocked, never a real agent."""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from tailshare import keys, revive, start
from tests.test_dead import S1, S2, Base


def launched(plan, max_running):
    return {"session": plan["session"], "tmux": {"socket": "/tmp/tmux-1/ts-appdev", "pane": "%9", "bin": "/bin/tmux"}}


class Revive(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(start, "launch", side_effect=launched)
        self.launch = p.start()
        self.addCleanup(p.stop)
        self.m.bus.reports = mock.Mock()
        self.now = time.time()

    def tick(self, dt=0.0):
        return revive.tick(self.m.bus, now=self.now + dt)

    def ended(self, sid):
        self.m.bus.store.x("UPDATE agents SET state=? WHERE id=?", json.dumps({"v": "ended", "at": self.now}), sid)

    def test_reopens_a_crashed_session_with_a_resume(self):
        self.config()
        self.agent(S1, ago=120)
        (r,) = self.tick()
        self.assertEqual(r["id"], S1)
        self.assertIn("revived", r)
        plan = self.launch.call_args.args[0]
        self.assertEqual((plan["options"], plan["cwd"], plan["name"]), ({"resume": S1}, self.app, "AppDev"))
        self.assertEqual(self.tick(1), [])  # backoff

    def test_never_one_that_ended_or_is_alive_or_old(self):
        self.config()
        self.agent(S1, ago=120)
        self.ended(S1)
        self.agent(S2, ago=10)  # still polling
        self.agent("0b0e0f2a-1c2d-4e5f-8a9b-0c1d2e3f4a5c", ago=revive.FRESH + 60)
        self.assertEqual(self.tick(), [])
        self.launch.assert_not_called()

    def test_off_by_setting(self):
        self.config(revive=False)
        self.agent(S1, ago=120)
        self.assertEqual(self.tick(), [])

    def test_backoff_then_the_session_cap_then_the_owner(self):
        self.config()
        tag = {"id": "sq_a", "member": "m_b"}
        self.agent(S1, ago=120, target="squad-tail-share:0.0", squad=tag)
        with mock.patch.object(start, "_reopen", return_value=None):  # no squad store here; tested in test_dead
            self.assertIn("revived", self.tick()[0])  # weight 1
            self.assertEqual(self.tick(29), [])  # BACKOFF[0]
            self.assertIn("revived", self.tick(31)[0])  # it never came back up: weight 2, 3 of 3
            out = self.tick(31 + 121)
        self.assertEqual(out, [{"id": S1, "stopped": "session"}])
        self.assertEqual(self.launch.call_count, 2)
        (call,) = self.m.bus.reports.flag.call_args_list
        sid, mid, b = call.args
        self.assertEqual((sid, mid, b["to"], b["kind"], b["id"]), ("sq_a", "m_b", "owner", "question", "revive"))
        self.tick(31 + 122 + 600)
        self.assertEqual(self.m.bus.reports.flag.call_count, 1)  # once an hour

    def test_the_host_cap_counts_a_server_once(self):
        self.config(revive_per_host=1)
        self.agent(S1, ago=120, sock=str(self.tmp / "tmux" / "ts-a"))
        self.agent(S2, ago=130, sock=str(self.tmp / "tmux" / "ts-b"), name="FrontDev")
        out = self.tick()
        self.assertEqual(sorted(r.get("stopped", "revived") for r in out), ["host", "revived"])

    def test_reopens_with_the_options_it_was_started_with(self):
        self.config(kinds={"claude": {"bin": "/bin/echo", "options": ["resume", "effort", "permission_mode"]}})
        self.agent(S1, ago=120)
        con = start._db()
        start.record_launch(con, {"session": S1, "options": {"effort": "high", "permission_mode": "plan"},
                                  "skip": False}, {"session": S1}, "")
        con.close()
        self.tick()
        self.assertEqual(self.launch.call_args.args[0]["options"],
                         {"effort": "high", "permission_mode": "plan", "resume": S1})

    def skip_launch(self, key):
        self.config(skip_permissions=True, kinds={"claude": {"bin": "/bin/echo", "options": ["resume", "skip_permissions"]}})
        self.agent(S1, ago=120)
        self.m.bus.store.db.executescript(keys.SCHEMA)
        con = start._db()
        start.record_launch(con, {"session": S1, "options": {"skip_permissions": True}, "skip": True},
                            {"session": S1}, key)
        con.close()

    def key(self, kid, mode):
        self.m.bus.store.x("INSERT INTO ssh_keys(id, pubkey, fingerprint, device, node, user, mode, approved_by, installed)"
                           " VALUES(?,?,?,?,?,?,?,?,?)", kid, "ssh-ed25519 AAAA", "SHA256:x", "BMO", "n", "nick", mode, "me", 0)

    def test_skip_only_while_its_start_key_still_may(self):
        self.skip_launch("k1ae2c3b481a8")
        self.key("k1ae2c3b481a8", "start-skip")
        (r,) = self.tick()
        self.assertIn("revived", r)
        self.assertEqual(self.launch.call_args.args[0]["options"], {"skip_permissions": True, "resume": S1})
        con = start._db()
        self.assertEqual(con.execute("SELECT key FROM launched WHERE session=?", (S1,)).fetchone()[0], "k1ae2c3b481a8")

    def test_a_narrowed_key_stops_up_front_and_spends_no_tries(self):
        self.skip_launch("k1ae2c3b481a8")
        self.key("k1ae2c3b481a8", "start")  # approved again as a safe start key
        with mock.patch("tailshare.bus.log") as log:
            self.assertEqual(self.tick(), [{"id": S1, "stopped": "key"}])
        self.assertIn("no longer allows it", log.call_args_list[0].args[0])
        self.launch.assert_not_called()
        self.assertIsNone(start._db().execute("SELECT 1 FROM revives").fetchone())  # no try spent
        self.m.bus.store.x("UPDATE ssh_keys SET mode='start-skip'")  # allowed skip again: reopens with it
        (r,) = self.tick(700)
        self.assertIn("revived", r)

    def test_a_removed_key_is_never_reopened_and_the_owner_hears(self):
        self.skip_launch("k1ae2c3b481a8")
        self.key("k1ae2c3b481a8", "start-skip")
        self.m.bus.store.x("DELETE FROM ssh_keys")
        self.m.bus.store.x("INSERT INTO revoked_keys(id, at) VALUES(?,?)", "k1ae2c3b481a8", 0)
        with mock.patch("tailshare.bus.log") as log:
            self.assertEqual(self.tick(), [{"id": S1, "stopped": "key"}])
        self.launch.assert_not_called()
        self.assertIn("k1ae2c3b481a8", log.call_args_list[0].args[0])
        self.assertEqual(self.tick(700), [{"id": S1, "stopped": "key"}])  # still not

    def test_one_started_before_launches_were_recorded_reopens_safe(self):
        self.config(kinds={"claude": {"bin": "/bin/echo", "options": ["resume", "skip_permissions"]}})
        self.agent(S1, ago=120)
        con = start._db()
        self.assertIsNone(con.execute("SELECT 1 FROM launched").fetchone())
        con.close()
        item, grant, key, _ = revive._item(self.m.bus, start._db(), {"id": S1, "kind": "claude", "cwd": self.app,
                                                                  "name": "AppDev"})
        self.assertEqual((item["options"], grant, key), ({"resume": S1}, "safe", ""))
        self.assertIn("revived", self.tick()[0])

    def test_a_refusal_is_counted_and_logged(self):
        self.config()
        self.agent(S1, ago=120)
        self.launch.side_effect = start.Refused("limit", reason="12 running")
        with mock.patch("tailshare.bus.log") as log:
            (r,) = self.tick()
        self.assertEqual(r["refused"]["error"], "limit")
        self.assertIn("refused limit", log.call_args.args[0])
        self.assertEqual(self.tick(10), [])  # backoff applies to refusals too


class Config(Base):
    def test_defaults_and_bad_values(self):
        cfg = self.config()
        self.assertEqual((cfg["revive"], cfg["revive_per_session"], cfg["revive_per_host"]), (True, 3, 10))
        cfg = self.config(revive=False, revive_per_session=0, revive_per_host=25)
        self.assertEqual((cfg["revive"], cfg["revive_per_session"], cfg["revive_per_host"]), (False, 0, 25))
        cfg = self.config(revive="no", revive_per_session=True, revive_per_host=-1)
        self.assertEqual((cfg["revive"], cfg["revive_per_session"], cfg["revive_per_host"]), (True, 3, 10))
        self.assertIs(start.capabilities(self.tmp / "start.json")["revive"], True)


if __name__ == "__main__":
    unittest.main()
