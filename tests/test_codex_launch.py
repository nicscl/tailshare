"""A new Codex start records its launch once the thread registers from its pane (start.bind_launch), so Reopen,
revive and keep-running restart it with the same options and grant, never more than its key allows now."""
from __future__ import annotations

import time
import unittest
from unittest import mock

from tailshare import bus, revive, start
from tests.test_dead import S1, S2, Base

T = {"socket": "/tmp/tmux-1/ts-codexdev", "pane": "%3", "target": "tailshare-start:1.0", "bin": "/bin/tmux"}


class CodexLaunch(Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(bus, "pid_skip", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def launch(self, skip=True, key="k0123456789ab", cwd=None):
        plan = {"kind": "codex", "cwd": cwd or self.app, "options": {"model": "gpt-5", "sandbox": "workspace-write"},
                "skip": skip, "session": ""}
        con = start._db()
        try:
            start.record_launch(con, plan, {"tmux": T}, key)
        finally:
            con.close()

    def reg(self, aid=S1, pid=4242, tmux=T, cwd=None, harness="codex"):
        return self.m.bus.register({"id": aid, "harness": harness, "cwd": cwd or self.app, "session": "CodexDev",
                                    "pid": pid, "tmux": tmux})

    def row(self, sid=S1):
        con = start._db()
        try:
            return con.execute("SELECT options, grant_, key FROM launched WHERE session=?", (sid,)).fetchone()
        finally:
            con.close()

    def test_the_thread_gets_its_starts_options(self):
        self.launch()
        self.reg()
        r = self.row()
        self.assertEqual((r["grant_"], r["key"]), ("skip", "k0123456789ab"))
        item = revive._item(self.m.bus, start._db(), {"id": S1, "kind": "codex", "cwd": self.app, "name": "CodexDev"})[0]
        self.assertEqual(item["options"], {"model": "gpt-5", "sandbox": "workspace-write", "resume": S1})

    def test_once_per_start(self):
        self.launch()
        self.reg()
        self.reg(aid=S2, pid=4343)  # another thread in that pane later: nothing
        self.assertIsNone(self.row(S2))

    def test_only_its_pane_its_folder_and_soon(self):
        self.launch()
        self.reg(tmux={**T, "pane": "%4"})
        self.assertIsNone(self.row())
        self.reg(aid=S2, pid=1, cwd="/elsewhere")
        self.assertIsNone(self.row(S2))
        self.launch()
        con = start._db()
        con.execute("UPDATE launch_pending SET at=?", (time.time() - start.PENDING_BIND - 1,))
        con.close()
        self.reg(pid=99)
        self.assertIsNone(self.row())

    def test_never_claude_and_never_a_second_heartbeat(self):
        self.launch()
        self.reg(harness="claude")
        self.assertIsNone(self.row())
        with mock.patch.object(start, "bind_launch", return_value=False) as b:
            self.reg(pid=7)
            self.reg(pid=7)  # same process: no lookup again
        self.assertEqual(b.call_count, 1)

    def test_no_start_db_none_made(self):
        self.reg()
        self.assertFalse((self.tmp / "start.db").exists())


if __name__ == "__main__":
    unittest.main()
