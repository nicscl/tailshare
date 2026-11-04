"""Codex sessions on the bus: which harness a process runs under, and install-codex (in a temporary CODEX_HOME)."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tailshare import agent, bus

TID = "019a0b1c-2d3e-4f5a-8b6c-7d8e9f0a1b2c"
SID = "0b0e0f2a-1c2d-4e5f-8a9b-0c1d2e3f4a5a"


class Detect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(agent, "CODEX_HOME", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        agent._codex_found = 0

    def env(self, **kw):
        base = {k: v for k, v in os.environ.items()
                if k not in ("TAILSHARE_AGENT_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "TAILSHARE_AGENT_NAME")}
        return mock.patch.dict(os.environ, {**base, **kw}, clear=True)

    def test_nearest_harness_wins(self):
        with self.env(CLAUDE_CODE_SESSION_ID=SID, CODEX_THREAD_ID=TID, TAILSHARE_AGENT_NAME="Worker"), \
                mock.patch.object(agent, "_nearest_harness", return_value=("codex", 42)):
            i = agent._detect_session({})
        self.assertEqual((i.id, i.harness, i.session, i.pid), (TID, "codex", "Worker", 42))
        with self.env(CLAUDE_CODE_SESSION_ID=SID, CODEX_THREAD_ID=TID), \
                mock.patch.object(agent, "_nearest_harness", return_value=("claude", 7)), \
                mock.patch.object(agent, "_claude_transcript", return_value=None):
            self.assertEqual(agent._detect_session({}).harness, "claude")

    def test_codex_hook_input(self):
        with self.env(), mock.patch.object(agent, "_nearest_harness", return_value=("", 0)):
            i = agent._detect_session({"session_id": TID, "cwd": "/w"}, "codex")
        self.assertEqual((i.id, i.harness, i.cwd), (TID, "codex", "/w"))

    def test_ps_walk(self):
        ps = f"  1     0 /sbin/launchd\n 10     1 /opt/homebrew/bin/codex --cd /w\n 20    10 /bin/zsh -c x\n"
        with mock.patch.object(agent.os, "getppid", return_value=20), \
                mock.patch.object(agent.subprocess, "run", return_value=mock.Mock(stdout=ps)):
            self.assertEqual(agent._nearest_harness(), ("codex", 10))

    def test_permission_request_state(self):
        self.assertEqual(bus.hook_state({"session_id": TID, "hook_event_name": "PermissionRequest"})[1:],
                         ("waiting_input", "permission"))


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(agent, "CODEX_HOME", self.tmp),
                  mock.patch.object(agent.shutil, "which", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def run_cmd(self, fn):
        with redirect_stdout(io.StringIO()) as out:
            fn(None)
        return out.getvalue()

    def test_install_keeps_others_and_uninstalls(self):
        theirs = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}]}}
        (self.tmp / "hooks.json").write_text(json.dumps(theirs))
        out = self.run_cmd(agent.cmd_install_codex)
        self.assertIn("/hooks", out)
        doc = json.loads((self.tmp / "hooks.json").read_text())
        cmds = [h["command"] for g in doc["hooks"]["SessionStart"] for h in g["hooks"]]
        self.assertTrue(any(c.endswith("agent hook session-start --harness codex") for c in cmds), cmds)
        self.assertIn("say done", json.dumps(doc["hooks"]["Stop"]))
        self.assertIn("PermissionRequest", doc["hooks"])
        self.assertIn("already", self.run_cmd(agent.cmd_install_codex))
        self.run_cmd(agent.cmd_uninstall_codex)
        self.assertEqual(json.loads((self.tmp / "hooks.json").read_text()), theirs)


    def test_bad_hooks_file_is_left_alone(self):
        (self.tmp / "hooks.json").write_text("[1, 2]")
        with self.assertRaises(SystemExit):
            self.run_cmd(agent.cmd_install_codex)
        self.run_cmd(agent.cmd_uninstall_codex)
        (self.tmp / "hooks.json").write_text("{nope")
        self.run_cmd(agent.cmd_uninstall_codex)
        self.assertEqual((self.tmp / "hooks.json").read_text(), "{nope")


if __name__ == "__main__":
    unittest.main()
