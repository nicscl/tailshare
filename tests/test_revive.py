"""The revive watchdog (tailshare/revive.py): a crashed session tailshare started is reopened with a resume, with
backoff and caps, and the owner is told past a cap. All on temp state; start.launch is mocked, never a real agent."""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from tailshare import revive, start
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
                                  "skip": False}, {"session": S1})
        con.close()
        self.tick()
        self.assertEqual(self.launch.call_args.args[0]["options"],
                         {"effort": "high", "permission_mode": "plan", "resume": S1})

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
