"""Dead sessions (tailshare/dead.py) and reopening them with a start that resumes (start.py, options.resume).

Everything on temp state: bus.db in a temp dir, tmux only with -S on a private socket there."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tailshare import bus, dead, start
from tests.test_squads import Machine, member

TMUX = shutil.which("tmux") or ""
S1, S2, S3 = ("0b0e0f2a-1c2d-4e5f-8a9b-0c1d2e3f4a5" + c for c in "abc")


def started(tmp, tag):
    """start.db saying `tailshare start` started this squad member here."""
    con = start._db(tmp / "start.db")
    con.execute("INSERT INTO member_starts(squad, member, instructions_sha256, at) VALUES(?,?,?,?)",
                (tag["id"], tag["member"], "0" * 64, time.time()))
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.m = Machine(self, "air")
        self.tmp = self.m.tmp
        (self.tmp / "projects" / "app").mkdir(parents=True)
        self.root = (self.tmp / "projects").resolve()
        self.app = str(self.root / "app")
        for p in (mock.patch.object(bus, "STATE_DIR", self.tmp), mock.patch.object(start, "binary", return_value="/bin/echo")):
            p.start()
            self.addCleanup(p.stop)
        self.env = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "TERM": "xterm"}

    def agent(self, aid, harness="claude", name="AppDev", cwd=None, ago=600, sock=None, pane="%0",
              target="tailshare-start:0.0", squad=None):
        sock = sock or str(self.tmp / "tmux" / "ts-gone")
        tmux = {"socket": sock, "pane": pane, "target": target, "bin": TMUX}
        now = time.time()
        self.m.bus.store.x("INSERT OR REPLACE INTO agents(id, address, harness, cwd, session, pid, first_seen, last_seen,"
                           " user, tmux, squad) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           aid, f"air/{harness}/app/{bus.slug(name)}", harness, cwd or self.app, name, 0, now - ago,
                           now - ago, "nick", json.dumps(tmux), json.dumps(squad, sort_keys=True) if squad else "")

    def config(self, **kw):
        d = {"enabled": True, "roots": [str(self.root)], "max_running": 4,
             "kinds": {"claude": {"bin": "/bin/echo", "options": ["resume"]}}, **kw}
        p = self.tmp / "start.json"
        p.write_text(json.dumps(d))
        p.chmod(0o600)
        return start.load_config(p)

    def ids(self):
        return [r["id"] for r in dead.listing("air")["sessions"]]


class Listing(Base):
    def test_a_dead_session_and_its_fields(self):
        self.config()
        self.agent(S1)
        (r,) = dead.listing("air")["sessions"]
        self.assertEqual({k: r[k] for k in ("id", "kind", "address", "name", "folder", "cwd", "title", "squad", "resumable")},
                         {"id": S1, "kind": "claude", "address": "air/claude/app/appdev", "name": "AppDev", "folder": "app",
                          "cwd": self.app, "title": "AppDev", "squad": None, "resumable": True})
        self.assertIsInstance(r["last_seen"], float)
        self.assertNotIn("reason", r)
        self.assertEqual((r["ended"], "ended_at" in r), (False, False))  # its pane went: a crash
        self.m.bus.store.x("UPDATE agents SET state=? WHERE id=?", json.dumps({"v": "ended", "since": 5, "at": 7.25}), S1)
        (r,) = dead.listing("air")["sessions"]
        self.assertEqual((r["ended"], r["ended_at"]), (True, 7.25))  # SessionEnd or Close
        d = dead.listing("air")
        self.assertEqual((d["host"], d["max_age"], d["limit"]), ("air", 7 * 86400, 50))

    def test_what_is_not_listed(self):
        self.agent(S1, ago=10)  # still polling: alive
        self.agent(S2, ago=8 * 86400)  # too old
        self.agent("cli-0123456789ab", harness="cli")  # no session to resume
        self.agent(S3, sock=str(self.tmp / "tmux" / "ts-manager"), target="manager:0.0")  # ts-agents brings it back
        self.assertEqual(self.ids(), [])

    def test_never_a_ts_agents_session(self):
        for i, name in enumerate(("manager", "appdev", "frontdev", "tailsharedev", "tailsharepm")):
            self.agent(f"0b0e0f2a-1c2d-4e5f-8a9b-0c1d2e3f4b0{i}", name=name.title(),
                       sock=str(self.tmp / "tmux" / f"ts-{name}"), target=f"{name}:0.0")
        self.agent(S1, sock=str(self.tmp / "tmux" / "ts-x"), target="tailshare-start:0.0")  # one `start` made
        self.agent(S2, sock=str(self.tmp / "tmux" / "ts-y"), target="squad-tail-share:1.0")
        self.agent(S3, sock=str(self.tmp / "tmux" / "default"), target="workspace:3.0")  # the default server is not ts-*
        self.assertEqual(sorted(self.ids()), sorted([S1, S2, S3]))

    def test_a_viewed_then_crashed_member(self):
        """ProtosPM on prismo, 2026-11-05: viewed from Hive, its row named the gate's own session; after the tmux
        crash it was ended but never listed. A member tailshare started here is ours whatever the target says."""
        tag = {"id": "sq_2olACmvp6m4sydELdvc-Ag", "member": "m_ouu7NI73nSag"}
        started(self.tmp, tag)
        sock = str(self.tmp / "tmux" / "ts-protospm")
        self.agent(S1, name="ProtosPM", sock=sock, target="tailshare-0-63875:1.0", squad=tag)
        # the same, tagged by ts-agents (which tags members too) but never started by tailshare here: not ours
        self.agent(S2, name="Manager", sock=str(self.tmp / "tmux" / "ts-manager"), target="tailshare-0-9:1.0",
                   squad={"id": tag["id"], "member": "m_other"})
        # a member start ran once, now run by ts-agents in a session of its own: not ours
        self.agent(S3, name="ProtosPM", sock=sock, target="protospm:0.0", squad=tag)
        self.assertEqual(self.ids(), [S1])
        self.assertEqual(dead.find(S1)[1], False)

    def test_newest_first_and_capped(self):
        for i in range(dead.LIMIT + 5):
            self.agent(f"0b0e0f2a-1c2d-4e5f-8a9b-{i:012d}", ago=600 + i)
        got = self.ids()
        self.assertEqual(len(got), dead.LIMIT)
        self.assertEqual(got[0], "0b0e0f2a-1c2d-4e5f-8a9b-000000000000")

    def test_reasons(self):
        self.agent(S1, harness="codex")
        self.agent(S2, cwd="/elsewhere")
        self.agent(S3, ago=700)
        self.assertEqual({r["id"]: r.get("reason") for r in dead.listing("air")["sessions"]},
                         {S1: "off", S2: "off", S3: "off"})  # start is off here
        self.config()  # codex is not a kind here
        self.assertEqual({r["id"]: (r["resumable"], r.get("reason")) for r in dead.listing("air")["sessions"]},
                         {S1: (False, "off"), S2: (False, "cwd"), S3: (True, None)})
        self.config(kinds={"claude": {"bin": "/bin/echo", "options": ["resume"]},
                           "codex": {"bin": "/bin/echo", "options": ["resume"]}})
        self.assertEqual({r["id"]: r["resumable"] for r in dead.listing("air")["sessions"]},
                         {S1: True, S2: False, S3: True})
        self.config(kinds={"claude": {"bin": "/bin/echo", "options": []}})  # no resume here
        self.assertEqual(dead.listing("air")["sessions"][-1]["reason"], "off")

    def test_squad_tag(self):
        self.agent(S1, squad={"id": "sq1", "member": "m1"})
        self.assertEqual(dead.listing("air")["sessions"][0]["squad"], {"id": "sq1", "member": "m1"})

    @unittest.skipUnless(TMUX, "needs tmux")
    def test_pane_alive_or_taken(self):
        sock = str(self.tmp / "t.sock")
        subprocess.run([TMUX, "-S", sock, "new-session", "-d", "-s", "tailshare-start", "sleep 60"], check=True,
                       env=self.env)
        self.addCleanup(subprocess.run, [TMUX, "-S", sock, "kill-server"], capture_output=True, env=self.env)
        self.agent(S1, sock=sock)
        self.assertEqual(self.ids(), [])  # its pane is still there (Claude exited, say, then came back)
        self.agent(S2, sock=sock, ago=5)  # a newer session in that pane now
        self.assertEqual(self.ids(), [S1])


class Hello(Base):
    def test_session_id_and_the_route(self):
        self.agent(S1.upper(), ago=5, sock=str(self.tmp / "nope"))
        self.agent("cli-0123456789ab", harness="cli", ago=5)
        self.m.net.usermap = {"100.1.1.1": "nick@example.com", "100.2.2.2": "ana@other.org"}
        own = self.m.bus.hello("100.1.1.1")
        self.assertEqual(own["sessions_dead"], 1)
        self.assertEqual({a["address"]: a.get("session_id") for a in own["agents"]},
                         {"air/claude/app/appdev": S1, "air/cli/app/appdev": None})
        self.assertFalse(any("session_id" in a for a in self.m.bus.hello("100.2.2.2")["agents"]))  # not the owner's
        self.assertEqual(self.m.bus.dead_sessions("100.1.1.1")["host"], "air")
        with self.assertRaises(bus.BusError) as c:
            self.m.bus.dead_sessions("100.2.2.2")
        self.assertEqual(c.exception.code, 403)

    def test_cwd_for_the_owner_only(self):
        self.agent(S1, ago=5)
        self.m.net.usermap = {"100.1.1.1": "nick@example.com", "100.2.2.2": "ana@other.org", "100.3.3.3": "tagged-devices"}
        self.assertEqual([a.get("cwd") for a in self.m.bus.hello("100.1.1.1")["agents"]], [self.app])
        for ip in ("100.2.2.2", "100.3.3.3", ""):  # another user, a tagged device, no caller
            self.assertFalse(any("cwd" in a for a in self.m.bus.hello(ip)["agents"]))


class Reopen(Base):
    def item(self, sid=S1, **kw):
        return {"kind": "claude", "cwd": self.app, "name": "AppDev", "options": {"resume": sid}, **kw}

    def test_a_dead_session_reopens(self):
        cfg = self.config()
        self.agent(S1)
        plan = start.build(self.item(), cfg, "safe", "air")
        self.assertEqual(plan["session"], S1)
        self.assertIn(f"--resume={S1}", plan["argv"])
        self.assertIn("--name=AppDev", plan["argv"])
        self.assertEqual(plan["tmux"], {"session": "tailshare-start", "window": "appdev"})

    def test_refusals(self):
        cfg = self.config()
        self.agent(S1)
        self.agent(S2, ago=5)

        def err(item):
            with self.assertRaises(start.Refused) as c:
                start.build(item, cfg, "safe", "air")
            return c.exception.out["error"], c.exception.out.get("field")

        self.assertEqual(err(self.item(S3)), ("not_dead", "options.resume"))  # never seen here
        self.assertEqual(err(self.item(S2)), ("running", "options.resume"))
        (self.root / "other").mkdir()
        self.assertEqual(err(self.item(cwd=str(self.root / "other"))), ("cwd", "cwd"))
        self.assertEqual(err(self.item(name="Someone")), ("bad_request", "name"))
        self.agent(S3, harness="codex")
        self.assertEqual(err(self.item(S3)), ("not_dead", "options.resume"))

    def test_a_squad_member_keeps_its_tag(self):
        cfg = self.config()
        self.m.put("sq1", name="Tail Share", members=[{"id": "m1", **member(folder=self.app)}])
        self.agent(S1, squad={"id": "sq1", "member": "m1"})
        plan = start.build(self.item(), cfg, "safe", "air")
        self.assertEqual(plan["squad"], {"id": "sq1", "name": "Tail Share", "member": "m1"})
        self.assertIn("## Reporting", plan["instructions"])  # reopened, it still keeps its report
        self.assertEqual((plan["env"]["TAILSHARE_SQUAD"], plan["env"]["TAILSHARE_MEMBER"]), ("sq1", "m1"))
        self.assertEqual(plan["tmux"]["session"], "squad-tail-share")

    def test_bulk_dry_run(self):
        cfg = self.config(max_running=2)
        ids = [f"0b0e0f2a-1c2d-4e5f-8a9b-{i:012d}" for i in range(3)]
        for i, sid in enumerate(ids):
            self.agent(sid, name=f"A{i}")
        con = start._db(self.tmp / "start.db")
        self.addCleanup(con.close)
        out = start.dry_run(con, [self.item(sid, name=f"A{i}") for i, sid in enumerate(ids)], cfg, "safe",
                            "k0123456789ab", "air", fit=2)
        self.assertEqual([r.get("error") for r in out], [None, None, "limit"])
        self.assertEqual(out[2]["max_running"], 2)



@unittest.skipUnless(TMUX, "needs tmux")
class Close(Base):
    """start.close_started (in the bus) and the gate's {"v":1,"close":[…]}: only a live session tailshare started,
    on this user's own ts-* server; it is listed as dead at once and reopens."""

    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))  # short: sun_path is ~104 bytes
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.socks = []

    def server(self, name="ts-appdev", session="tailshare-start"):
        sock = str(self.dir / name)
        subprocess.run([TMUX, "-S", sock, "new-session", "-d", "-s", session, "sleep 60"], check=True, env=self.env)
        self.addCleanup(subprocess.run, [TMUX, "-S", sock, "kill-server"], capture_output=True, env=self.env)
        pane = subprocess.run([TMUX, "-S", sock, "display", "-p", "#{pane_id}"], capture_output=True, text=True,
                              check=True, env=self.env).stdout.strip()
        return sock, pane

    def item(self, sid=S1, **kw):
        return {"kind": "claude", "cwd": self.app, "name": "AppDev", "options": {"resume": sid}, **kw}

    def alive(self, sock, pane):
        from tailshare import keys

        return keys.pane_alive(TMUX, sock, pane)

    def test_closes_a_started_session_and_it_reopens_at_once(self):
        cfg = self.config()
        sock, pane = self.server()
        self.agent(S1, ago=5, sock=sock, pane=pane, target="tailshare-start:0")
        self.assertNotIn(S1, self.ids())
        out = start.close_started(S1)
        self.assertEqual(out, {"closed": True, "tmux": {"socket": sock, "pane": pane}})
        self.assertFalse(self.alive(sock, pane))
        self.assertIn(S1, self.ids())  # no wait for bus.LIVE
        self.assertEqual(dead.find(S1)[1], False)
        self.assertEqual(start.build(self.item(), cfg, "safe", "air")["session"], S1)
        self.assertEqual(start.close_started(S1)["error"], "not_running")  # once

    def test_a_viewed_member_closes(self):
        self.config()
        tag = {"id": "sq_a", "member": "m_b"}
        started(self.tmp, tag)
        sock, pane = self.server(session="squad-tail-share")
        self.agent(S1, ago=5, sock=sock, pane=pane, target="tailshare-0-123:0", squad=tag)
        self.assertTrue(start.close_started(S1)["closed"])

    def test_a_squad_member(self):
        self.config()
        sock, pane = self.server(session="squad-tail-share")
        self.agent(S1, ago=5, sock=sock, pane=pane, target="squad-tail-share:0")
        self.assertTrue(start.close_started(S1)["closed"])

    def test_refusals(self):
        self.config()
        mine, p1 = self.server(name="plain")  # the user's own server, not a per-agent one
        self.agent(S1, ago=5, sock=mine, pane=p1, target="tailshare-start:0")
        managed, p2 = self.server(name="ts-bmo", session="bmo")  # ts-agents', which brings its agents back
        self.agent(S2, ago=5, sock=managed, pane=p2, target="bmo:0")
        for sid in (S1, S2):
            self.assertEqual(start.close_started(sid)["error"], "not_started")
        self.assertTrue(self.alive(mine, p1) and self.alive(managed, p2))
        self.assertEqual(start.close_started(S3)["error"], "not_running")  # never seen here
        self.agent(S3, ago=600)  # seen, not live
        self.assertEqual(start.close_started(S3)["error"], "not_running")
        self.assertEqual(start.close_started("../x")["error"], "bad_request")
        self.assertEqual(start.close_started(S1.upper())["error"], "bad_request")  # the gate lowercases

    def test_a_pane_gone_already(self):
        self.config()
        sock, pane = self.server()
        self.agent(S1, ago=5, sock=sock, pane=pane, target="tailshare-start:0")
        subprocess.run([TMUX, "-S", sock, "kill-server"], check=True, env=self.env)
        self.assertEqual(start.close_started(S1)["error"], "not_running")

    def test_off(self):
        sock, pane = self.server()
        self.agent(S1, ago=5, sock=sock, pane=pane, target="tailshare-start:0")
        self.assertEqual(start.close_started(S1)["error"], "off")
        self.assertTrue(self.alive(sock, pane))

    def test_the_gate(self):
        self.config()
        sock, pane = self.server()
        self.agent(S1, ago=5, sock=sock, pane=pane, target="tailshare-start:0")
        cfg_path, db = self.tmp / "start.json", self.tmp / "start.db"
        key = "k0123456789ab"
        doc = {"v": 1, "close": [S1.upper()]}
        with mock.patch.object(start, "_ask_bus", side_effect=lambda req: start.close_started(req["close"])):
            out = start.handle(doc, "safe", key, "air", cfg_path, db, plan_only=True)
            self.assertEqual(out["error"], "no_grant")  # the plan key, with no Face ID
            self.assertTrue(self.alive(sock, pane))
            self.assertEqual(start.handle({"v": 1, "close": [S1], "confirm": ["x"]}, "safe", key, "air", cfg_path,
                                          db)["error"], "bad_request")
            self.assertEqual(start.handle({"v": 1, "close": []}, "safe", key, "air", cfg_path, db)["error"],
                             "bad_request")
            out = start.handle(doc, "safe", key, "air", cfg_path, db)
            self.assertEqual(out["results"], [{"session_id": S1, "closed": True,
                                               "tmux": {"socket": sock, "pane": pane}}])
            self.assertEqual(start.handle({"v": 1, "close": ["nope"]}, "safe", key, "air", cfg_path, db)["results"],
                             [{"session_id": "nope", "error": "bad_request", "field": "close", "reason": "a session id",
                              "fix": {"action": "none"}}])
            for _ in range(start.CONFIRMS_PER_MIN - 2):  # with the two above
                start.handle({"v": 1, "close": [S2]}, "safe", key, "air", cfg_path, db)
            self.assertEqual(start.handle(doc, "safe", key, "air", cfg_path, db)["error"], "rate")
            self.assertEqual(start.handle(doc, "", key, "air", cfg_path, db)["error"], "no_grant")

    def test_through_the_bus_socket(self):
        self.config()
        sock, pane = self.server()
        self.agent(S1, ago=5, sock=sock, pane=pane, target="tailshare-start:0")
        state = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, state, True)
        shutil.copy(self.tmp / "start.json", state / "start.json")
        self.m.bus.store.db.execute("VACUUM INTO ?", (str(state / "bus.db"),))
        with mock.patch.object(bus, "STATE_DIR", state):
            srv = start.serve_launches()
            self.addCleanup(srv.close)
            self.assertEqual(start._ask_bus({"v": 1, "close": S1}), {"closed": True, "tmux": {"socket": sock, "pane": pane}})
            with self.assertRaises(start.Refused) as c:
                start._ask_bus({"v": 1, "close": S1, "launch": "x"})
            self.assertEqual(c.exception.out, {"error": "bad_request"})
        self.assertFalse(self.alive(sock, pane))


if __name__ == "__main__":
    unittest.main()


class KeepsTarget(Base):
    """A device viewing a pane links its window into a gate's own session; the agent row keeps its own session."""

    def reg(self, target):
        return self.m.bus.register({"id": S1, "harness": "claude", "cwd": self.app, "session": "AppDev",
                                    "tmux": {"socket": "/tmp/tmux-1/ts-appdev", "pane": "%3", "target": target}})

    def stored(self):
        return json.loads(self.m.bus.store.one("SELECT tmux FROM agents WHERE id=?", S1)["tmux"])["target"]

    def test_a_gate_session_never_overwrites_it(self):
        self.reg("squad-tail-share:1.0")
        self.reg("tailshare-3-63875:1.0")
        self.assertEqual(self.stored(), "squad-tail-share:1.0")
        self.reg("squad-tail-share:2.0")  # a real move still counts
        self.assertEqual(self.stored(), "squad-tail-share:2.0")

    def test_a_first_sighting_from_a_gate_is_kept_as_is(self):
        self.reg("tailshare-3-63875:1.0")
        self.assertEqual(self.stored(), "tailshare-3-63875:1.0")

    @unittest.skipUnless(TMUX, "needs tmux")
    def test_the_hook_names_the_agents_own_session(self):
        from tailshare import agent, gate_control

        d = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(shutil.rmtree, d, True)
        sock = str(d / "ts-appdev")
        subprocess.run([TMUX, "-S", sock, "new-session", "-d", "-s", "squad-tail-share", "sleep 60"], check=True,
                       env=self.env)
        self.addCleanup(subprocess.run, [TMUX, "-S", sock, "kill-server"], capture_output=True, env=self.env)
        pane, window = subprocess.run([TMUX, "-S", sock, "display", "-p", "#{pane_id} #{window_id}"],
                                      capture_output=True, text=True, check=True, env=self.env).stdout.split()
        with mock.patch.dict(os.environ, {"TMUX": f"{sock},1,0", "TMUX_PANE": pane}), \
                mock.patch.object(gate_control.os, "getpid", return_value=63875):
            self.assertTrue(gate_control.own_session(TMUX, sock, pane, window))
            self.assertEqual(agent._tmux_pane()["target"], "squad-tail-share:0.0")
