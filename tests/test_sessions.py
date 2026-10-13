"""Sessions tests: transcript parsing, the index, search across mailboxes, ask, and signed launches.

Run with: uv run python -m unittest discover tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tailshare import bus, sessions
from tests.test_bus import StaticNet, free_port, make_bus

SID = "11111111-2222-3333-4444-555555555555"
CODEX_ID = "019df553-ca5c-72f0-9930-b6ccff82d6bd"


def jl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def claude_records(cwd: str, prompt: str = "fix the stripe webhook retry loop") -> list[dict]:
    base = {"sessionId": SID, "cwd": cwd, "gitBranch": "main"}
    return [
        {"type": "user", **base, "timestamp": "2026-10-01T10:00:00Z", "message": {"role": "user", "content": prompt}},
        {"type": "user", **base, "isMeta": True, "timestamp": "2026-10-01T10:00:01Z", "message": {"content": "meta stuff"}},
        {"type": "user", **base, "timestamp": "2026-10-01T10:00:02Z",
         "message": {"content": "<command-name>/rename</command-name>"}},
        {"type": "assistant", **base, "timestamp": "2026-10-01T10:00:03Z",
         "message": {"content": [{"type": "text", "text": "The retry loop uses exponential backoff."},
                                 {"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "user", **base, "timestamp": "2026-10-01T10:00:04Z",
         "message": {"content": [{"type": "tool_result", "content": "secret tool output"}]}},
        {"type": "assistant", **base, "isSidechain": True, "timestamp": "2026-10-01T10:00:05Z",
         "message": {"content": [{"type": "text", "text": "subagent chatter"}]}},
        {"type": "user", **base, "gitBranch": "fix/retries", "timestamp": "2026-10-01T11:00:00Z",
         "message": {"content": [{"type": "text", "text": "now add a test"}]}},
        {"type": "ai-title", "aiTitle": "Stripe retries", "sessionId": SID},
        {"type": "custom-title", "customTitle": "webhooks", "sessionId": SID},
    ]


def codex_records(cwd: str) -> list[dict]:
    return [
        {"timestamp": "2026-10-02T09:00:00Z", "type": "session_meta",
         "payload": {"id": CODEX_ID, "cwd": cwd, "timestamp": "2026-10-02T09:00:00Z", "git": {"branch": "dev"}}},
        {"timestamp": "2026-10-02T09:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "system"}]}},
        {"timestamp": "2026-10-02T09:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>x"}]}},
        {"timestamp": "2026-10-02T09:00:03Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "migrate the replica database"}]}},
        {"timestamp": "2026-10-02T09:00:04Z", "type": "event_msg", "payload": {"type": "user_message", "message": "migrate the replica database"}},
        {"timestamp": "2026-10-02T09:05:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done with pg_dump."}]}},
    ]


class Fixture(unittest.TestCase):
    """Fake ~/.claude and ~/.codex with one session each."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.saved = (sessions.CLAUDE_HOME, sessions.CODEX_HOME)
        sessions.CLAUDE_HOME, sessions.CODEX_HOME = self.root / "claude", self.root / "codex"
        self.proj = self.root / "work" / "payments"
        self.proj.mkdir(parents=True)
        self.claude_file = sessions.CLAUDE_HOME / "projects" / "-work-payments" / f"{SID}.jsonl"
        jl(self.claude_file, claude_records(str(self.proj)))
        self.codex_file = sessions.CODEX_HOME / "sessions/2026/10/02" / f"rollout-2026-10-02T09-00-00-{CODEX_ID}.jsonl"
        jl(self.codex_file, codex_records(str(self.root / "work" / "infra")))
        jl(sessions.CODEX_HOME / "session_index.jsonl", [{"id": CODEX_ID, "thread_name": "Replica migration"}])
        self.index = sessions.Index(self.root / "sessions.db")

    def tearDown(self):
        sessions.CLAUDE_HOME, sessions.CODEX_HOME = self.saved


class Parsing(Fixture):
    def test_claude(self):
        s = sessions.parse_claude(self.claude_file)
        self.assertEqual((s["id"], s["cwd"], s["title"], s["ai_title"], s["branch"]),
                         (SID, str(self.proj), "webhooks", "Stripe retries", "fix/retries"))
        self.assertEqual(s["prompts"], 2)  # not the meta record, the /rename wrapper or the tool result
        self.assertEqual(s["first_prompt"], "fix the stripe webhook retry loop")
        self.assertIn("exponential backoff", s["text"])
        self.assertNotIn("secret tool output", s["text"])
        self.assertNotIn("subagent chatter", s["text"])
        self.assertEqual(s["updated"] - s["started"], 3600)

    def test_codex(self):
        s = sessions.parse_codex(self.codex_file, sessions.codex_titles())
        self.assertEqual((s["id"], s["title"], s["branch"], s["prompts"]), (CODEX_ID, "Replica migration", "dev", 1))
        self.assertIn("pg_dump", s["text"])

    def test_codex_old_format(self):
        recs = [r for r in codex_records("/w/old") if r["type"] != "response_item"]
        recs.append({"timestamp": "2026-10-02T09:06:00Z", "type": "event_msg", "payload": {"type": "agent_message", "message": "ok"}})
        jl(self.codex_file, recs)
        s = sessions.parse_codex(self.codex_file, {})
        self.assertEqual((s["prompts"], s["first_prompt"]), (1, "migrate the replica database"))

    def test_no_prompts_is_skipped(self):
        jl(self.claude_file, [{"type": "custom-title", "customTitle": "empty"}])
        self.assertIsNone(sessions.parse_claude(self.claude_file))


class Indexing(Fixture):
    def test_scan_search_and_refresh(self):
        self.assertEqual(self.index.scan(), 2)
        self.assertEqual(self.index.scan(), 0)  # nothing changed
        hits = self.index.search("stripe retry")
        self.assertEqual([h["id"] for h in hits], [SID])
        self.assertEqual(hits[0]["name"], "webhooks")
        self.assertIn("[", hits[0]["snippet"])
        self.assertEqual([h["id"] for h in self.index.search("replica")], [CODEX_ID])
        self.assertEqual([h["id"] for h in self.index.search("", harness="codex")], [CODEX_ID])
        self.assertEqual([h["id"] for h in self.index.search("", id_prefix="1111")], [SID])
        self.assertEqual([h["folder"] for h in self.index.search("", folder="pay")], ["payments"])
        # an appended transcript is re-read; a renamed Codex thread picks up its new name
        jl(self.claude_file, claude_records(str(self.proj), "teach it about idempotency keys"))
        jl(sessions.CODEX_HOME / "session_index.jsonl", [{"id": CODEX_ID, "thread_name": "Replica cutover"}])
        self.assertEqual(self.index.scan(), 2)
        self.assertEqual([h["id"] for h in self.index.search("idempotency")], [SID])
        self.assertEqual(self.index.get(CODEX_ID)["name"], "Replica cutover")
        # a deleted transcript leaves the index
        self.claude_file.unlink()
        self.index.scan()
        self.assertIsNone(self.index.get(SID))
        self.assertEqual(self.index.search("stripe"), [])


def fake_tool(tmp: Path, name: str, script: str) -> str:
    p = tmp / name
    p.write_text(f"#!{sys.executable}\n{script}")
    p.chmod(0o755)
    return str(p)


class AcrossMachines(Fixture):
    """`air` searches and asks `pro`, which holds the sessions. `eve` (another tailnet user) may not."""

    def setUp(self):
        super().setUp()
        self.index.scan()
        port = free_port()
        self.pro = make_bus("pro", StaticNet("pro", user="nick@x.com", usermap={"127.0.0.1": "nick@x.com"}), port)
        self.pro.sessions = sessions.Sessions(self.pro, self.index)
        self.srv = self.pro.listen("127.0.0.1", local=False)
        self.air = make_bus("air", StaticNet("air", peers={"pro": {"ip": "127.0.0.1", "port": port, "online": True}}))
        self.air.sessions = sessions.Sessions(self.air, sessions.Index(self.root / "air.db"))
        self.air.store.x("INSERT INTO peers(host, ip, version, last_ok) VALUES('pro','127.0.0.1','t',?)", time.time())

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def test_search_reaches_peers(self):
        d = self.air.sessions.search({"q": "stripe"})
        self.assertEqual([(r["host"], r["id"]) for r in d["results"]], [("pro", SID)])
        self.assertEqual(d["hosts"], {"air": "ok", "pro": "ok"})
        self.assertEqual(self.air.sessions.resolve("pro:019df5")["id"], CODEX_ID)
        self.assertEqual(self.air.sessions.resolve("1111")["host"], "pro")
        with self.assertRaises(bus.BusError):
            self.air.sessions.resolve("pro:dead")

    def test_other_user_is_refused(self):
        self.pro.net.usermap = {"127.0.0.1": "eve@other.org"}
        url = f"http://127.0.0.1:{self.srv.server_address[1]}/bus/v1/sessions/search"
        with self.assertRaises(bus.BusError) as cm:
            bus.request(url, {"q": "stripe"})
        self.assertEqual(cm.exception.code, 403)
        self.assertEqual(self.air.sessions.search({"q": "stripe"})["results"], [])

    def test_ask(self):
        tmp = Path(tempfile.mkdtemp())
        # stands in for `claude -p --resume … --tools ""`: echoes what it was given
        tool = fake_tool(tmp, "claude", (
            "import json, os, sys\n"
            "print(json.dumps({'result': 'cwd=' + os.getcwd() + ' args=' + ' '.join(sys.argv[1:]) +"
            " ' hooks=' + os.environ.get('TAILSHARE_HOOKS', '') + ' q=' + sys.stdin.read()[-20:], 'is_error': False}))\n"))
        saved = sessions.find_tool
        sessions.find_tool = lambda name: tool
        try:
            r = self.air.sessions.ask({"ref": "pro:11111111", "question": "why backoff?", "from": "air/claude/x"})
        finally:
            sessions.find_tool = saved
        a = r["answer"]
        self.assertEqual((r["host"], r["id"]), ("pro", SID))
        self.assertIn(f"cwd={os.path.realpath(self.proj)} ", a)
        for flag in ("--resume " + SID, "--fork-session", "--no-session-persistence", "--tools ", "hooks=0", "why backoff?"):
            self.assertIn(flag, a)


class Launch(Fixture):
    def setUp(self):
        super().setUp()
        self.index.scan()
        self.b = make_bus("pro", StaticNet("pro"))
        self.keys = Path(tempfile.mkdtemp())
        self.key = self.keys / "id"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.key)], check=True)
        self.signers = self.keys / "allowed_signers"
        self.signers.write_text(sessions.trust_line("nick", (self.keys / "id.pub").read_text()))
        self.signers.chmod(0o600)
        self.svc = sessions.Sessions(self.b, self.index, self.signers)
        self.live = False
        self.svc.running = lambda sid: self.live  # instead of asking `claude agents`
        self.ran = []
        self.saved_launch = sessions.launch_command
        sessions.launch_command = lambda s, mode, prompt: (self.ran.append((s["id"], mode, prompt)), ["/bin/echo", "bg-id"])[1]
        self.session = self.index.get(SID)

    def tearDown(self):
        sessions.launch_command = self.saved_launch
        super().tearDown()

    def request(self, **over) -> tuple[str, str]:
        d = json.loads(sessions.launch_request("nick", "pro", self.session, "acceptEdits", "run the tests"))
        d.update(over)
        payload = sessions.canonical(d)
        return payload, sessions.sign(payload, self.key)

    def refused(self, payload: str, sig: str, code: int = 403) -> str:
        with self.assertRaises(bus.BusError) as cm:
            self.svc.launch_local(payload, sig)
        self.assertEqual(cm.exception.code, code)
        return str(cm.exception)

    def test_signed_launch_runs_once(self):
        payload, sig = self.request()
        r = self.svc.launch_local(payload, sig)
        self.assertEqual((r["id"], r["mode"], r["output"]), (SID, "acceptEdits", "bg-id"))
        self.assertEqual(self.ran, [(SID, "acceptEdits", "run the tests")])
        self.assertIn("already used", self.refused(payload, sig))

    def test_tampering_and_wrong_requests(self):
        payload, sig = self.request()
        self.refused(payload.replace("acceptEdits", "auto"), sig)  # changed after signing
        self.assertIn("not pro", self.refused(*self.request(host="air")))
        self.assertIn("expired", self.refused(*self.request(issued=time.time() - 900, expires=time.time() - 1)))
        self.assertIn("not allowed", self.refused(*self.request(mode="bypassPermissions")))
        self.assertIn("folder", self.refused(*self.request(cwd="/etc")))
        self.assertIn("only Claude", self.refused(*self.request(session=CODEX_ID, harness="codex")))
        # signed by a key this machine does not trust
        other = self.keys / "other"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
        p, _ = self.request()
        self.refused(p, sessions.sign(p, other))
        self.assertEqual(self.ran, [])

    def test_running_session_is_not_launched(self):
        self.live = True
        self.assertIn("running right now", self.refused(*self.request()))
        self.assertEqual(self.ran, [])

    def test_off_without_signers_and_as_root(self):
        payload, sig = self.request()
        self.signers.chmod(0o666)
        self.refused(payload, sig, 409)
        self.signers.unlink()
        self.refused(payload, sig, 409)
        saved = os.geteuid
        os.geteuid = lambda: 0
        try:
            self.assertIn("root", self.refused(payload, sig, 409))
        finally:
            os.geteuid = saved


if __name__ == "__main__":
    unittest.main()
