"""Agent state in /hello: what the harness's hooks say a session is doing."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tailshare import agent, bus
from tests.test_bus import PANE, StaticNet, free_port, make_bus, reg


def hook(event: str, sid: str = "S", **kw) -> dict:
    return {"session_id": sid, "hook_event_name": event, "cwd": "/x/api", **kw}


class Mapping(unittest.TestCase):
    def test_events(self):
        cases = [
            (hook("UserPromptSubmit"), ("S", "working", "")),
            (hook("PreToolUse", tool_name="Bash"), ("S", "working", "")),
            (hook("PostToolUse", tool_name="Bash"), ("S", "working", "")),
            (hook("PreToolUse", tool_name="AskUserQuestion"), ("S", "waiting_input", "question")),
            (hook("PreToolUse", tool_name="ExitPlanMode"), ("S", "waiting_input", "plan")),
            (hook("Notification", notification_type="permission_prompt"), ("S", "waiting_input", "permission")),
            (hook("Notification", notification_type="elicitation_dialog"), ("S", "waiting_input", "elicitation")),
            (hook("Notification", notification_type="idle_prompt"), ("S", "idle", "")),
            (hook("Stop"), ("S", "idle", "")),
            (hook("SessionStart", source="startup"), ("S", "idle", "")),
            (hook("SessionStart", source="resume"), ("S", "idle", "")),
            (hook("SessionEnd", reason="exit"), ("S", "ended", "")),
            ({"type": "agent-turn-complete", "thread-id": "T", "last-assistant-message": "done"}, ("T", "idle", "")),
        ]
        for d, want in cases:
            self.assertEqual(bus.hook_state(d), want, d)

    def test_malformed_fields_say_nothing(self):
        self.assertEqual(bus.hook_state(hook("PreToolUse", tool_name=["AskUserQuestion"])), ("S", "working", ""))
        for d in (hook("Notification", notification_type={"a": 1}), {**hook("Stop"), "session_id": ["S"]},
                  {**hook("Stop"), "hook_event_name": ["Stop"]}):
            self.assertEqual(bus.hook_state(d), ("", "", ""), d)

    def test_says_nothing(self):
        for d in (hook("SessionStart", source="compact"), hook("Notification", notification_type="auth_success"),
                  hook("Notification"), hook("SubagentStop"), hook("PreCompact"), hook("Stop", sid=""), {},
                  {"type": "something-else", "thread-id": "T"}):
            self.assertEqual(bus.hook_state(d), ("", "", ""), d)

    def test_clean_state(self):
        now = time.time()
        good = {"v": "waiting_input", "reason": "permission", "since": now - 5, "at": now}
        self.assertEqual(bus.clean_state(good), good)
        self.assertEqual(bus.clean_state({**good, "v": "idle"}), {"v": "idle", "since": now - 5, "at": now})
        self.assertEqual(bus.clean_state({**good, "reason": "please run rm -rf"}),
                         {"v": "waiting_input", "since": now - 5, "at": now})
        for bad in (None, "working", {}, {**good, "v": "sleeping"}, {**good, "at": float("nan")},
                    {**good, "at": now + 3600}, {**good, "since": now + 1}, {**good, "at": "1"},
                    {**good, "at": True}, {**good, "since": True}, {**good, "since": 0}):
            self.assertIsNone(bus.clean_state(bad), bad)


class Reports(unittest.TestCase):
    def setUp(self):
        self.b = make_bus("air")
        reg(self.b, "S", "api", tmux=PANE)

    def state(self):
        return self.b.agent("S")["state"]

    def test_unknown_agent_is_ignored(self):
        self.assertEqual(self.b.report_state(hook("UserPromptSubmit", sid="nobody")), {"ok": False})
        self.assertIsNone(self.b.store.one("SELECT * FROM agents WHERE id='nobody'"))
        self.assertEqual(self.b.report_state(hook("PreCompact")), {"ok": False})
        self.assertIsNone(self.state())

    def test_since_and_at(self):
        self.b.report_state(hook("UserPromptSubmit"))
        first = self.state()
        self.assertEqual(first["v"], "working")
        self.assertNotIn("reason", first)
        time.sleep(0.01)
        self.b.report_state(hook("PreToolUse", tool_name="Read"))
        again = self.state()
        self.assertEqual(again["since"], first["since"])  # still the same piece of work
        self.assertGreater(again["at"], first["at"])
        self.b.report_state(hook("Notification", notification_type="permission_prompt"))
        wait = self.state()
        self.assertEqual((wait["v"], wait["reason"]), ("waiting_input", "permission"))
        self.assertGreater(wait["since"], first["since"])
        self.b.report_state(hook("PreToolUse", tool_name="AskUserQuestion"))
        self.assertEqual(self.state()["reason"], "question")  # another reason is a new wait
        self.assertGreater(self.state()["since"], wait["since"])
        self.b.report_state(hook("PostToolUse", tool_name="AskUserQuestion"))
        self.b.report_state(hook("Stop"))
        self.assertEqual(self.state()["v"], "idle")

    def test_report_counts_as_seen(self):
        self.b.store.x("UPDATE agents SET last_seen=0")
        self.b.report_state(hook("Stop"))
        self.assertTrue(self.b.agent("S")["online"])
        self.b.store.x("UPDATE agents SET last_seen=0")
        self.b.report_state(hook("SessionEnd"))
        self.assertFalse(self.b.agent("S")["online"])

    def test_registering_keeps_the_state(self):
        self.b.report_state(hook("UserPromptSubmit"))
        reg(self.b, "S", "api", tmux=PANE)
        self.assertEqual(self.state()["v"], "working")


class Hello(unittest.TestCase):
    def setUp(self):
        self.b = make_bus("pro")
        reg(self.b, "S", "api", tmux=PANE)
        reg(self.b, "Q", "web")

    def agents(self):
        return {a["address"]: a for a in self.b.hello()["agents"]}

    def test_state_shared_like_panes(self):
        self.b.report_state(hook("Notification", notification_type="permission_prompt"))
        a = self.agents()
        self.assertEqual(set(a["pro/claude/api"]["state"]), {"v", "reason", "since", "at"})
        self.assertNotIn("state", a["pro/claude/web"])  # unknown: absent, never guessed
        old = bus.SHARE_PANES
        bus.SHARE_PANES = False
        try:
            self.assertNotIn("state", self.agents()["pro/claude/api"])
        finally:
            bus.SHARE_PANES = old

    def test_peer_state_is_cleaned(self):
        pro_port = free_port()
        air = make_bus("air", StaticNet("air", peers={"pro": {"ip": "127.0.0.1", "port": pro_port, "online": True}}))
        pro = make_bus("pro", StaticNet("pro"), pro_port)
        reg(pro, "S", "api")
        reg(pro, "B", "bad")
        pro.report_state(hook("Stop"))
        pro.store.x("UPDATE agents SET state=? WHERE id='B'", json.dumps({"v": "working", "since": "x", "at": 1}))
        srv = pro.listen("127.0.0.1", local=False)
        try:
            air.discover(["pro"])
        finally:
            srv.shutdown()
            srv.server_close()
        got = {a["address"]: a for a in air.directory()["agents"]}
        self.assertEqual(got["pro/claude/api"]["state"]["v"], "idle")
        self.assertIsNone(got["pro/claude/bad"]["state"])


class CurlHook(unittest.TestCase):
    """The installed command, run by a shell as Claude Code would, against a throwaway local bus."""

    def setUp(self):
        self.port = free_port()
        self.b = make_bus("air", port=self.port)
        reg(self.b, "S", "api")
        self.srv = self.b.listen("127.0.0.1", local=True)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def run_hook(self, d: dict, env: dict | None = None, port: int | None = None) -> subprocess.CompletedProcess:
        with mock.patch.object(bus, "BUS_PORT", port or self.port):
            cmd = agent.state_command()
        return subprocess.run(["sh", "-c", cmd], input=json.dumps(d), capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", **(env or {})}, timeout=10)

    def test_reports_and_stays_quiet(self):
        t = time.monotonic()
        cp = self.run_hook(hook("PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""))
        self.assertEqual(self.b.agent("S")["state"]["v"], "working")

    def test_large_and_malformed_bodies(self):
        big = hook("PreToolUse", tool_name="Write", tool_input={"content": "x" * (2 * 1024 * 1024)})
        self.assertEqual(self.run_hook(big).returncode, 0)
        self.assertEqual(self.b.agent("S")["state"]["v"], "working")  # a large Write still counts
        url = f"http://127.0.0.1:{self.port}/bus/v1/state"
        self.assertEqual(bus.request(url, hook("Notification", notification_type=[1])), {"ok": False})
        with self.assertRaises((bus.BusError, OSError)):  # other routes keep the usual limit (413, often a broken pipe)
            bus.request(f"http://127.0.0.1:{self.port}/bus/v1/register", {"id": "Z", "pad": "x" * (2 * 1024 * 1024)})
        self.assertIsNone(self.b.store.one("SELECT * FROM agents WHERE id='Z'"))

    def test_bus_down_or_disabled_never_blocks(self):
        cp = self.run_hook(hook("Stop"), port=free_port())  # nothing listening
        self.assertEqual((cp.returncode, cp.stdout, cp.stderr), (0, "", ""))
        cp = self.run_hook(hook("Stop"), env={"TAILSHARE_HOOKS": "0"})
        self.assertEqual(cp.returncode, 0)
        self.assertIsNone(self.b.agent("S")["state"])

    def test_codex_notify(self):
        reg(self.b, "T", "api", harness="codex")
        with mock.patch.object(agent, "LOCAL", f"http://127.0.0.1:{self.port}/bus/v1"):
            with self.assertRaises(SystemExit) as cm:
                agent.cmd_codex_notify(SimpleNamespace(payload=json.dumps({"type": "agent-turn-complete", "thread-id": "T"})))
            self.assertEqual(cm.exception.code, 0)
            with self.assertRaises(SystemExit):
                agent.cmd_codex_notify(SimpleNamespace(payload="not json"))
        self.assertEqual(self.b.agent("T")["state"]["v"], "idle")


class Install(unittest.TestCase):
    """install-claude, the upgrade at service start, and uninstall, on a temp CLAUDE_HOME only."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        p = mock.patch.object(agent, "CLAUDE_HOME", self.home)
        p.start()
        self.addCleanup(p.stop)
        w = mock.patch.object(agent.shutil, "which", lambda name: "/bin/tailshare" if name == "tailshare" else None)
        w.start()
        self.addCleanup(w.stop)
        self.path = self.home / "settings.json"
        self.mine = {"type": "command", "command": "say done"}

    def settings(self) -> dict:
        return json.loads(self.path.read_text())

    def install(self) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            agent.cmd_install_claude(SimpleNamespace())
        return out.getvalue()

    def commands(self, event: str) -> list[str]:
        return [h["command"] for g in self.settings()["hooks"].get(event, []) for h in g["hooks"]]

    def test_install_is_idempotent_and_keeps_the_users_hooks(self):
        self.path.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"matcher": "", "hooks": [self.mine]}]}}))
        self.assertIn("hooks added", self.install())
        first = self.settings()
        self.assertIn("already installed", self.install())
        self.assertEqual(self.settings(), first)
        self.assertEqual(first["model"], "x")
        stop = self.commands("Stop")
        self.assertEqual(stop[0], "say done")
        self.assertEqual(len(stop), 2)
        for event in agent.STATE_EVENTS:
            self.assertEqual(sum(agent.STATE_MARK in c for c in self.commands(event)), 1, event)
        self.assertEqual(sum("agent hook" in c for c in self.commands("SessionStart")), 1)

    def test_upgrade_adds_only_what_is_missing(self):
        old = {"hooks": {
            "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "/bin/tailshare agent hook session-start"}]}],
            "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": "/bin/tailshare agent hook prompt"}]}],
            "Stop": [{"matcher": "", "hooks": [self.mine]}]}}
        self.path.write_text(json.dumps(old))
        self.assertTrue(agent.upgrade_claude_hooks())
        self.assertEqual(json.loads(self.path.with_suffix(".json.bak-tailshare-upgrade").read_text()), old)
        self.assertEqual(self.commands("Stop")[0], "say done")
        self.assertEqual(len(self.commands("SessionStart")), 2)
        self.assertFalse(agent.upgrade_claude_hooks())  # nothing left to add

    def test_keeps_a_symlink_and_the_mode(self):
        dotfiles = Path(tempfile.mkdtemp())
        real = dotfiles / "claude-settings.json"
        real.write_text(json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [self.mine]}]}}))
        real.chmod(0o640)
        self.path.symlink_to(real)
        self.install()
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(self.path.resolve(), real.resolve())
        self.assertEqual(real.stat().st_mode & 0o777, 0o640)
        self.assertTrue(any(agent.STATE_MARK in c for c in self.commands("Stop")))
        self.assertTrue((dotfiles / "claude-settings.json.bak-tailshare").exists())  # next to the real file
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["settings.json"])  # no tmp left behind

    def test_upgrade_leaves_settings_that_never_joined(self):
        self.assertFalse(agent.upgrade_claude_hooks())
        self.assertFalse(self.path.exists())
        plain = {"hooks": {"Stop": [{"matcher": "", "hooks": [self.mine]}]}}
        self.path.write_text(json.dumps(plain))
        self.assertFalse(agent.upgrade_claude_hooks())
        self.assertEqual(self.settings(), plain)

    def test_uninstall_removes_only_ours(self):
        shared = {"matcher": "Bash", "hooks": [self.mine]}
        self.path.write_text(json.dumps({"hooks": {"PreToolUse": [shared]}}))
        self.install()
        s = self.settings()
        s["hooks"]["PreToolUse"][0]["hooks"].append({"type": "command", "command": agent.state_command()})
        self.path.write_text(json.dumps(s))
        with redirect_stdout(io.StringIO()):
            agent.cmd_uninstall_claude(SimpleNamespace())
        self.assertEqual(self.settings()["hooks"], {"PreToolUse": [shared]})


if __name__ == "__main__":
    unittest.main()
