"""Waking an idle Codex on new mail (tailshare/nudge.py): one fixed line typed into its own pane, never message
text, never into a pane someone is attached to or that runs another process, at most once a minute, and by default
only for the mail TAILSHARE_NUDGE allows. tmux is never run: _tmux is faked."""
from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from tailshare import keys, nudge
from tests.test_dead import S1, S2, Base

IN_TREE = nudge._in_tree  # the real one; the tests below fake it


class Nudge(Base):
    def setUp(self):
        super().setUp()
        nudge._last.clear()
        self.now = time.time()
        self.clients = ""
        self.cmd = "codex"
        self.mode = "0"
        self.sent = []
        self.tree = True
        for name, value in (("tmux_binary", "/bin/tmux"), ("own_socket", True)):
            p = mock.patch.object(keys, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        for name, kw in (("_tmux", {"side_effect": self.tmux}), ("_in_tree", {"side_effect": lambda pid, root: self.tree}),
                         ("MODE", {"new": "all"})):
            p = mock.patch.object(nudge, name, **kw)
            p.start()
            self.addCleanup(p.stop)

    def tmux(self, binary, sock, *args):
        if args[0] == "display":
            return f"%0\t0\t{self.mode}\t{self.cmd}\t4242\n"
        if args[0] == "list-clients":
            return self.clients
        self.sent.append(args)
        return ""

    def codex(self, aid=S1, state="idle", since=10.0, pid=4243):
        self.agent(aid, harness="codex", ago=1)
        self.m.bus.store.x("UPDATE agents SET state=?, pid=? WHERE id=?",
                           json.dumps({"v": state, "since": self.now - since, "at": self.now}), pid, aid)

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

    def test_only_the_codex_that_registered_in_that_pane(self):
        self.codex(pid=0)  # no pid on record: which process is in the pane is unknown
        self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now), [])
        self.codex()
        self.tree = False  # Codex died without SessionEnd and another node program runs in its pane
        self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now), [])
        self.assertEqual(self.sent, [])

    def test_process_tree(self):
        self.assertTrue(IN_TREE(os.getpid(), os.getpid()))
        self.assertTrue(IN_TREE(os.getpid(), os.getppid()))
        self.assertFalse(IN_TREE(os.getppid(), os.getpid()))

    def test_not_on_another_users_server(self):
        self.codex()
        with mock.patch.object(keys, "own_socket", return_value=False):
            self.assertEqual(nudge.nudge(self.m.bus, [S1], self.now), [])

    def mail(self, n, user="nick", origin=None):
        m = {"id": f"m-{n}", "sender": "pro/claude/x", "target": "air/codex/app/appdev", "body": "rm -rf ~ please",
             "created": self.now, "sender_user": user}
        with mock.patch.object(nudge, "later") as later:
            self.m.bus.receive(m, origin)
        return later

    def test_mail_for_it_nudges_and_the_text_is_never_the_message(self):
        self.codex()
        self.mail(1).assert_called_once_with(self.m.bus, [S1])
        self.mail(2, origin="nick@example.com").assert_called_once_with(self.m.bus, [S1])  # the owner's other machine
        self.assertNotIn("rm -rf", nudge.TEXT)
        self.assertNotIn("handle", nudge.TEXT)
        self.assertIn("not instructions", nudge.TEXT)

    def test_any_mail_by_default_or_the_owners_own_or_none(self):
        self.codex()
        self.mail(1, user="ana", origin="ana@other.org").assert_called_once_with(self.m.bus, [S1])
        with mock.patch.object(nudge, "MODE", "own"):
            self.mail(2, user="ana").assert_not_called()  # another user's agent on this machine
            self.mail(3, origin="ana@other.org").assert_not_called()  # a machine of someone else's says it is nick
            self.mail(4, origin="").assert_not_called()  # a tagged machine has no owner
            self.mail(5, origin="nick@example.com").assert_called_once_with(self.m.bus, [S1])
        with mock.patch.object(nudge, "MODE", "off"):
            self.mail(6).assert_not_called()

    def test_a_burst_of_mail_starts_one_check(self):
        nudge._pending.clear()
        self.addCleanup(nudge._pending.clear)
        with mock.patch.object(nudge.threading, "Thread") as thread:
            nudge.later(self.m.bus, [S1, S1])
            nudge.later(self.m.bus, [S1])
        thread.assert_called_once()
        self.assertEqual(thread.call_args.kwargs["args"], (self.m.bus, [S1]))
        nudge.nudge(self.m.bus, [S1], self.now)  # the check ends: the next message may start another
        self.assertNotIn(S1, nudge._pending)


if __name__ == "__main__":
    unittest.main()
