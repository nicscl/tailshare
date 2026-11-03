"""Dead sessions (tailshare/dead.py) and reopening them with a start that resumes (start.py, options.resume).

Everything on temp state: bus.db in a temp dir, tmux only with -S on a private socket there."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import unittest
from unittest import mock

from tailshare import bus, dead, start
from tests.test_squads import Machine, member

TMUX = shutil.which("tmux") or ""
S1, S2, S3 = ("0b0e0f2a-1c2d-4e5f-8a9b-0c1d2e3f4a5" + c for c in "abc")


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
                         {S1: "kind", S2: "off", S3: "off"})  # start is off here
        self.config()
        self.assertEqual({r["id"]: (r["resumable"], r.get("reason")) for r in dead.listing("air")["sessions"]},
                         {S1: (False, "kind"), S2: (False, "cwd"), S3: (True, None)})
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


if __name__ == "__main__":
    unittest.main()
