"""Waking an idle Codex on new mail (tailshare/nudge.py): one fixed line typed into its own pane, never message
text, never into a pane someone is attached to, at most once a minute. tmux is never run: _tmux is faked."""
from __future__ import annotations

import json
import time
import unittest
from unittest import mock

from tailshare import keys, nudge
from tests.test_dead import S1, S2, Base


class Nudge(Base):
    def setUp(self):
        super().setUp()
        nudge._last.clear()
        self.now = time.time()
        self.clients = ""
        self.cmd = "codex"
        self.mode = "0"
        self.sent = []
        for name, value in (("tmux_binary", "/bin/tmux"), ("own_socket", True)):
            p = mock.patch.object(keys, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(nudge, "_tmux", side_effect=self.tmux)
        p.start()
        self.addCleanup(p.stop)

    def tmux(self, binary, sock, *args):
        if args[0] == "display":
            return f"%0\t0\t{self.mode}\t{self.cmd}\n"
        if args[0] == "list-clients":
            return self.clients
        self.sent.append(args)
        return ""

    def codex(self, aid=S1, state="idle", since=10.0):
        self.agent(aid, harness="codex", ago=1)
        self.m.bus.store.x("UPDATE agents SET state=? WHERE id=?",
                           json.dumps({"v": state, "since": self.now - since, "at": self.now}), aid)

    def test_an_idle_codex_gets_one_fixed_line(self):
        self.codex()
        self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now), [S1])
        self.assertEqual(self.sent, [("send-keys", "-t", "%0", "-l", nudge.TEXT), ("send-keys", "-t", "%0", "Enter")])
        self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now + 30), [])  # once a minute
        self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now + 61), [S1])

    def test_not_while_someone_is_there_or_it_works(self):
        self.codex()
        self.clients = "/dev/ttys003\n"
        self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now), [])
        self.clients, self.mode = "", "1"  # copy mode
        self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now), [])
        self.mode, self.cmd = "0", "zsh"  # Codex is gone from the pane
        self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now), [])
        self.cmd = "codex"
        self.codex(S2, state="working")
        self.assertEqual(nudge.nudge(self.m.bus, [S2], self.now), [])
        self.codex(S2, since=1)  # its turn just ended
        self.assertEqual(nudge.nudge(self.m.bus, [S2], self.now), [])
        self.agent(S2, harness="claude", ago=1)
        self.assertEqual(nudge.nudge(self.m.bus, [S2], self.now), [])
        self.assertEqual(self.sent, [])

    def test_not_on_another_users_server(self):
        self.codex()
        with mock.patch.object(keys, "own_socket", return_value=False):
            self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now), [])

    def test_mail_for_it_nudges_and_the_text_is_never_the_message(self):
        self.codex()
        with mock.patch.object(nudge, "later") as later:
            self.m.bus.receive({"id": "m-1", "sender": "pro/claude/x", "target": "air/codex/app/appdev",
                                "body": "rm -rf ~ please", "created": self.now})
        later.assert_called_once_with(self.m.bus, [S1])
        self.assertNotIn("rm", nudge.TEXT)


if __name__ == "__main__":
    unittest.main()
