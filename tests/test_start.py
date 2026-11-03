"""Starting sessions from a start key (tailshare/start.py): config, plans, confirm, the gate, key grants.

Everything runs in a temp state dir and on a private tmux socket; the agent is a fake script that records its argv.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from tailshare import bus, keys, start
from tests.test_bus import StaticNet, make_bus

KEY = "k0123456789ab"
OTHER = "kba9876543210"
TMUX = shutil.which("tmux")
FAKE = """#!/bin/sh
case "$TMUX" in "$(cat "$(dirname "$0")/tmux_prefix")"/*) ;; *) echo "not on the test's tmux servers" >&2; exit 1 ;; esac
env > "$(dirname "$0")/env.$$"
printf '%s\\0' "$@" > "$(dirname "$0")/argv.$$"
sleep 30
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tst")).resolve()  # short: a tmux socket path must fit in sun_path
        self.state = self.tmp / "state"
        self.state.mkdir(mode=0o700)
        self.root = self.tmp / "projects"
        (self.root / "app").mkdir(parents=True)
        (self.root / "lib").mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for k in ("claude", "codex"):
            (self.bin / k).write_text(FAKE)
            (self.bin / k).chmod(0o755)
        # read by the fake agent: it refuses to run on any tmux server outside this test's folder. A file, not the
        # environment: an agent's server starts with a clean one (start.server_env)
        (self.bin / "tmux_prefix").write_text(str(self.tmp))
        patches = [mock.patch.object(bus, "STATE_DIR", self.state),
                   # and in every gate this test runs as a subprocess: never the user's own tmux server
                   mock.patch.object(start, "TMUX_SOCKET", str(self.tmp / "sock")),
                   mock.patch.object(start, "TMUX_DIR", str(self.tmp / "tmux"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # cleanups run last-added first: the tmux server goes before its socket's folder does
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._kill_tmux)
        self.config()
        # the bus's side: confirms are launched there (start.via_bus)
        self.launcher = start.serve_launches()
        self.addCleanup(self.launcher.close)

    def _kill_tmux(self):
        if TMUX:
            subprocess.run([TMUX, "-S", str(self.tmp / "sock"), "kill-server"], capture_output=True)

    def config(self, **kw):
        d = {"enabled": True, "roots": [str(self.root)], "skip_permissions": True, "max_running": 4,
             "kinds": {"claude": {"bin": str(self.bin / "claude"), "options": list(start.KINDS["claude"])},
                       "codex": {"bin": str(self.bin / "codex"), "options": list(start.KINDS["codex"])}}}
        d.update(kw)
        p = self.state / "start.json"
        p.write_text(json.dumps(d))
        p.chmod(0o600)
        return d

    def item(self, **kw):
        return {"kind": "claude", "cwd": str(self.root / "app"), "name": "AppDev", **kw}

    def call(self, doc, grant="safe", key=KEY):
        return start.handle(doc, grant, key, "air")

    def dry(self, *items, grant="safe", key=KEY):
        return self.call({"v": 1, "dry_run": True, "starts": list(items)}, grant, key)["results"]

    def confirm(self, *ids, grant="safe", key=KEY):
        return self.call({"v": 1, "confirm": list(ids)}, grant, key)

    def argvs(self, wait=5.0):
        end = time.time() + wait
        while time.time() < end:
            files = sorted(self.bin.glob("argv.*"))
            if files and all(f.stat().st_size for f in files):
                return [f.read_bytes().decode().split("\0")[:-1] for f in files]
            time.sleep(0.05)
        return []


class Config(Base):
    def test_off_without_file(self):
        (self.state / "start.json").unlink()
        self.assertIsNone(start.load_config())
        self.assertEqual(self.call({"v": 1, "dry_run": True, "starts": [self.item()]})["error"], "off")
        self.assertIsNone(start.capabilities())

    def test_off_when_disabled_or_unsafe(self):
        self.config(enabled=False)
        self.assertIsNone(start.load_config())
        self.config()
        (self.state / "start.json").chmod(0o620)
        with self.assertRaises(start.Refused):
            start.load_config()
        self.assertEqual(self.call({"v": 1, "dry_run": True, "starts": [self.item()]})["error"], "off")
        self.assertIsNone(start.capabilities())

    def test_symlinked_config_is_refused(self):
        real = self.tmp / "elsewhere.json"
        (self.state / "start.json").rename(real)
        (self.state / "start.json").symlink_to(real)
        with self.assertRaises(start.Refused):
            start.load_config()

    def test_root_never_starts(self):
        with mock.patch.object(os, "geteuid", return_value=0):
            self.assertIsNone(start.load_config())
            self.assertEqual(self.call({"v": 1, "dry_run": True, "starts": [self.item()]})["error"], "root")

    def test_capabilities_and_folders(self):
        self.config(kinds={"claude": {"options": ["model", "nonsense"]}, "grok": {"options": []}})
        self.assertEqual(start.capabilities(), {"kinds": ["claude"], "options": {"claude": ["model"]}, "skip": True})
        (self.root / ".hidden").mkdir()
        (self.root / "app" / "src").mkdir()
        self.assertEqual(start.folders(), [str(self.root / "app"), str(self.root / "lib"), str(self.root / "app" / "src")])


class Plans(Base):
    def refused(self, item, grant="safe"):
        r = self.dry(item, grant=grant)[0]
        self.assertIn("error", r, r)
        return r["error"], r.get("field", "")

    def test_folders_must_be_under_roots(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.root / "escape").symlink_to(outside)
        self.assertEqual(self.refused(self.item(cwd=str(outside))), ("not_allowed", "cwd"))
        self.assertEqual(self.refused(self.item(cwd=str(self.root / "escape"))), ("not_allowed", "cwd"))
        self.assertEqual(self.refused(self.item(cwd=str(self.root / "app" / ".." / ".." / "outside"))), ("not_allowed", "cwd"))
        self.assertEqual(self.refused(self.item(cwd="projects/app")), ("bad_request", "cwd"))
        self.assertEqual(self.refused(self.item(cwd=str(self.root / "nope"))), ("not_allowed", "cwd"))
        self.assertEqual(self.refused(self.item(options={"add_dirs": [str(outside)]})), ("not_allowed", "options.add_dirs"))

    def test_options_are_typed_and_allowed_by_the_host(self):
        self.assertEqual(self.refused(self.item(options={"model": "opus --dangerously-skip-permissions"})),
                         ("bad_request", "options.model"))
        self.assertEqual(self.refused(self.item(options={"flags": "-x"})), ("bad_request", "options.flags"))
        self.assertEqual(self.refused(self.item(options={"skip_permissions": "yes"})), ("bad_request", "options.skip_permissions"))
        self.assertEqual(self.refused(self.item(options={"resume": "../../x"})), ("bad_request", "options.resume"))
        self.assertEqual(self.refused(self.item(kind="grok")), ("bad_request", "kind"))
        self.config(kinds={"claude": {"bin": str(self.bin / "claude"), "options": ["model"]}})
        self.assertEqual(self.refused(self.item(options={"effort": "high"})), ("not_allowed", "options.effort"))
        self.assertEqual(self.refused(self.item(kind="codex")), ("not_allowed", "kind"))

    def test_skip_and_its_equivalents_need_host_and_grant(self):
        equivalents = [self.item(options={"skip_permissions": True}),
                       self.item(options={"permission_mode": "bypassPermissions"}),
                       self.item(kind="codex", options={"sandbox": "danger-full-access"}),
                       self.item(kind="codex", options={"sandbox": "danger-full-access", "approval": "never"}),
                       self.item(kind="codex", options={"skip_permissions": True})]
        for it in equivalents:
            self.assertEqual(self.refused(it)[0], "no_skip_grant", it)
            r = self.dry(it, grant="skip")[0]
            self.assertTrue(r["plan"]["skip"], it)
        self.config(skip_permissions=False)
        for it in equivalents:
            self.assertEqual(self.refused(it, grant="skip")[0], "not_allowed", it)
        safe = self.dry(self.item(options={"permission_mode": "acceptEdits"}),
                        self.item(kind="codex", options={"approval": "never", "sandbox": "workspace-write"}))
        self.assertEqual([r["plan"]["skip"] for r in safe], [False, False])

    def test_auto_is_its_own_mode(self):
        """Auto mode is not skipping (Nicholas, 2026-11-02): the host's own `auto` flag, no skip grant."""
        it = self.item(options={"permission_mode": "auto"})
        self.config(auto=True, skip_permissions=False)
        p = self.dry(it)[0]["plan"]  # a start=safe key, a host that never skips
        self.assertEqual((p["auto"], p["skip"]), (True, False))
        self.assertIn("--permission-mode=auto", p["argv"])
        self.assertNotIn("--dangerously-skip-permissions", p["argv"])
        self.assertEqual(start.capabilities()["permission_modes"],
                         {"claude": ["default", "acceptEdits", "plan", "dontAsk", "auto"]})  # no skip: no bypass
        self.assertIs(start.capabilities()["skip"], False)
        self.config()  # auto not in the config: off, whatever the grant
        for grant in ("safe", "skip"):
            self.assertEqual(self.refused(it, grant=grant), ("not_allowed", "options.permission_mode"))
        self.assertEqual(start.capabilities()["permission_modes"],
                         {"claude": ["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"]})
        self.config(auto="yes")  # only true turns it on
        self.assertEqual(self.refused(it)[0], "not_allowed")
        self.config(auto=True, kinds={"claude": {"bin": str(self.bin / "claude"), "options": ["model"]}})
        self.assertEqual(self.refused(it), ("not_allowed", "options.permission_mode"))  # nor without the option
        self.assertNotIn("permission_modes", start.capabilities())
        self.config(auto=True)
        others = self.dry(self.item(options={"permission_mode": "acceptEdits"}),
                          self.item(kind="codex", options={"approval": "never"}))
        self.assertEqual([r["plan"]["auto"] for r in others], [False, False])
        both = self.dry(self.item(options={"permission_mode": "auto", "skip_permissions": True}), grant="skip")[0]["plan"]
        self.assertEqual((both["auto"], both["skip"]), (True, True))  # skipping still needs its own grant
        self.assertEqual(self.refused(self.item(options={"permission_mode": "auto", "skip_permissions": True}))[0],
                         "no_skip_grant")

    def test_claude_argv(self):
        p = self.dry(self.item(name="App\x1bDev\u202e", instructions="You are AppDev.\n-x", prompt="--help",
                               options={"model": "opus", "permission_mode": "plan", "add_dirs": [str(self.root / "lib")]},
                               device_name="Nick\x07's iPhone"))[0]["plan"]
        a = p["argv"]
        self.assertEqual(a[0], str(self.bin / "claude"))
        self.assertEqual(a[1], "--name=AppDev")
        self.assertTrue(a[2].startswith("--session-id=") and p["session"] == a[2].split("=", 1)[1])
        self.assertIn("--append-system-prompt=<instructions>", a)  # the text is in the plan once, not twice
        self.assertEqual(p["instructions"], "You are AppDev.\n-x")
        self.assertIn("--permission-mode=plan", a)
        self.assertIn("--model=opus", a)
        self.assertIn(f"--add-dir={self.root / 'lib'}", a)
        self.assertEqual(a[-2:], ["--", "<prompt>"])  # the prompt too: once, as the plan's prompt
        self.assertEqual(p["prompt"], "--help")
        self.assertTrue(all(x.startswith("--") for x in a[1:-1]), a)
        self.assertEqual(p["env"]["TAILSHARE_STARTED_BY"], "Nick's iPhone")
        self.assertEqual(p["tmux"], {"session": "tailshare-start", "window": "appdev"})
        self.assertNotIn("--dangerously-skip-permissions", a)

    def test_codex_argv_and_toml(self):
        tricky = 'say "hi"\\n\nnew line\ttab \x01 \u2028 é'
        p = self.dry(self.item(kind="codex", instructions=tricky, options={"model": "gpt-5.5", "sandbox": "read-only"}))[0]["plan"]
        self.assertEqual(p["argv"][:3], [str(self.bin / "codex"), f"--cd={self.root / 'app'}",
                                         "--config=developer_instructions=<instructions>"])
        self.assertEqual(p["session"], "")
        real = start.argv_for("codex", "codex", "/x", "n", {}, tricky, "", "")
        value = real[2].split("=", 2)[2]
        self.assertEqual(tomllib.loads("v = " + value)["v"], tricky)
        for s in ('"""', "\\", "\x7f", "\x00", "a'b"):
            self.assertEqual(tomllib.loads("v = " + start.toml_string(s))["v"], s)

    def test_names_and_sizes(self):
        self.assertEqual(self.refused(self.item(name="")), ("bad_request", "name"))
        self.assertEqual(self.refused(self.item(name="\x1b\x07")), ("bad_request", "name"))
        self.assertEqual(self.refused(self.item(name="x" * 65)), ("bad_request", "name"))
        self.assertEqual(self.refused(self.item(prompt="x" * (start.PROMPT_MAX + 1))), ("bad_request", "prompt"))
        self.assertEqual(self.refused(self.item(instructions=["x"])), ("bad_request", "instructions"))
        self.assertEqual(start.slug("Front: Dev.v2"), "front-dev-v2")
        self.assertEqual(start.slug("..."), "agent")

    def test_squads_are_not_taken_from_the_request(self):
        self.assertEqual(self.refused(self.item(squad={"id": "sq", "member": "m"})), ("bad_request", "squad"))

    def test_batch_limits(self):
        r = self.call({"v": 1, "dry_run": True, "starts": [self.item()] * (start.MAX_ITEMS + 1)})
        self.assertEqual((r["error"], r["field"]), ("bad_request", "starts"))
        self.assertEqual(len(self.dry(*[self.item(name=f"m{i}") for i in range(start.MAX_ITEMS)])), start.MAX_ITEMS)
        self.assertEqual(self.call({"v": 2, "dry_run": True, "starts": [self.item()]})["error"], "bad_request")
        self.assertEqual(self.call({"v": 1, "dry_run": True, "starts": [self.item()], "confirm": []})["error"], "bad_request")

    def test_bad_grants_and_keys(self):
        self.assertEqual(self.call({"v": 1, "dry_run": True, "starts": [self.item()]}, grant="")["error"], "no_grant")
        self.assertEqual(self.call({"v": 1, "dry_run": True, "starts": [self.item()]}, key="nope")["error"], "no_grant")

    def test_store_is_private(self):
        self.dry(self.item())
        self.assertEqual((self.state / "start.db").stat().st_mode & 0o777, 0o600)


@unittest.skipUnless(TMUX, "needs tmux")
class InTheBus(Base):
    """The gate approves; the bus launches. The socket says only which plan, and the bus checks it."""

    def ask(self, req: bytes) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(10)
            c.connect(str(start.launch_socket()))
            c.sendall(req)
            return json.loads(c.makefile("rb").readline())

    def test_launched_by_the_bus_not_the_gate(self):
        where, real = [], start.launch
        def launch(plan, max_running):
            where.append(threading.current_thread().name)
            return real(plan, max_running)
        with mock.patch.object(start, "launch", launch):
            got = self.confirm(self.dry(self.item())[0]["plan_id"])["results"][0]
        self.assertNotIn("error", got, got)
        self.assertEqual(where, ["start-launch"])  # the bus's thread, never the gate's
        self.assertEqual(len(self.argvs()), 1)

    def test_forged_and_replayed_ids_are_refused(self):
        pid = self.dry(self.item())[0]["plan_id"]
        self.assertEqual(self.ask(json.dumps({"v": 1, "launch": pid}).encode() + b"\n")["error"], "unknown_plan")
        self.assertEqual(self.ask(b'{"v":1,"launch":"' + b"0" * 32 + b'"}\n')["error"], "unknown_plan")
        self.assertEqual(self.ask(b'{"v":1,"launch":"../x"}\n')["error"], "unknown_plan")
        self.assertEqual(self.ask(b'{"v":1,"launch":"%s","argv":["sh"]}\n' % pid.encode())["error"], "bad_request")
        self.assertEqual(self.ask(b"nonsense\n")["error"], "bad_request")
        self.assertEqual(self.argvs(0.3), [])
        self.assertNotIn("error", self.confirm(pid)["results"][0])  # the real confirm still works
        self.assertEqual(self.ask(json.dumps({"v": 1, "launch": pid}).encode() + b"\n")["error"], "unknown_plan")
        time.sleep(0.3)
        self.assertEqual(len(self.argvs()), 1)  # launched once

    def mark(self, pid, **cols):
        con = start._db()
        for k, v in cols.items():
            con.execute(f"UPDATE plans SET {k}=? WHERE id=?", (v, pid))
        con.close()

    def launch_req(self, pid):
        return json.dumps({"v": 1, "launch": pid}).encode() + b"\n"

    def test_confirmed_but_not_approved_is_refused(self):
        """used is set before the gate's checks (rebuild, changed): only approved says they passed."""
        pid = self.dry(self.item())[0]["plan_id"]
        self.mark(pid, used=time.time())
        self.assertEqual(self.ask(self.launch_req(pid))["error"], "unknown_plan")
        self.assertEqual(self.argvs(0.3), [])

    def test_replayed_before_the_result_is_written(self):
        """The gate writes the result only after the bus answers: a replay in between still launches nothing."""
        pid = self.dry(self.item())[0]["plan_id"]
        self.mark(pid, used=time.time(), approved=time.time())
        self.assertNotIn("error", self.ask(self.launch_req(pid)))
        self.assertEqual(self.ask(self.launch_req(pid))["error"], "unknown_plan")
        time.sleep(0.3)
        self.assertEqual(len(self.argvs()), 1)

    def test_an_approval_goes_stale(self):
        pid = self.dry(self.item())[0]["plan_id"]
        con = start._db()
        con.execute("UPDATE plans SET used=?, approved=? WHERE id=?",
                    (time.time(), time.time() - start.APPROVED_FOR - 1, pid))
        con.close()
        self.assertEqual(self.ask(json.dumps({"v": 1, "launch": pid}).encode() + b"\n")["error"], "unknown_plan")
        self.assertEqual(self.argvs(0.3), [])

    def test_only_this_user(self):
        with mock.patch.object(start, "_peer_uid", return_value=os.getuid() + 1):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.settimeout(10)
                c.connect(str(start.launch_socket()))
                c.sendall(b'{"v":1,"launch":"' + b"0" * 32 + b'"}\n')
                self.assertEqual(c.recv(100), b"")  # hung up on, unanswered
        self.assertEqual(self.argvs(0.3), [])
        a, b = socket.socketpair()
        with a, b:
            self.assertEqual(start._peer_uid(a), os.getuid())
        self.assertEqual(os.stat(start.launch_socket().parent).st_mode & 0o777, 0o700)

    def test_no_bus_no_start(self):
        self.launcher.close()
        start.launch_socket().unlink()
        r = self.confirm(self.dry(self.item())[0]["plan_id"])["results"][0]
        self.assertEqual(r["error"], "no_bus")
        self.assertEqual(self.argvs(0.3), [])


@unittest.skipUnless(TMUX, "needs tmux")
class Isolated(Base):
    """Each started agent on its own tmux server, <TMUX_DIR>/ts-<name> (here under the test's folder)."""

    def setUp(self):
        super().setUp()
        for name, value in (("TMUX_SOCKET", ""), ("LEGACY_SOCKET", str(self.tmp / "legacy"))):
            p = mock.patch.object(start, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._kill_servers)

    def _kill_servers(self):
        """Each by its own socket: never a bare kill-server, which would reach the user's default server."""
        for sock in [*(self.tmp / "tmux").glob("ts-*"), self.tmp / "legacy"]:
            subprocess.run([TMUX, "-S", str(sock), "kill-server"], capture_output=True)

    def run_one(self, **kw):
        return self.confirm(self.dry(self.item(**kw))[0]["plan_id"])["results"][0]

    def sessions(self, sock):
        r = subprocess.run([TMUX, "-S", str(sock), "list-windows", "-a", "-F", "#{session_name}:#{window_name}"],
                           capture_output=True, text=True)
        return r.stdout.split()

    def test_each_agent_gets_its_own_server(self):
        a, b = self.run_one(), self.run_one(name="Other")
        self.assertEqual((a["tmux"]["socket"], b["tmux"]["socket"]),
                         (str(self.tmp / "tmux" / "ts-appdev"), str(self.tmp / "tmux" / "ts-other")))
        self.assertEqual(self.sessions(a["tmux"]["socket"]), ["tailshare-start:appdev"])
        self.assertEqual(self.sessions(b["tmux"]["socket"]), ["tailshare-start:other"])
        self.assertEqual((self.tmp / "tmux").stat().st_mode & 0o777, 0o700)
        end = time.time() + 5
        while len(self.argvs()) < 2 and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(len(self.argvs()), 2)
        # one server going down leaves the other agent running
        subprocess.run([TMUX, "-S", a["tmux"]["socket"], "kill-server"], check=True)
        self.assertEqual(self.sessions(b["tmux"]["socket"]), ["tailshare-start:other"])
        self.assertNotIn("error", self.run_one())  # and the name is free again

    def test_squad_members_too(self):
        self.assertEqual(start.socket_for("Lead Dev"), str(self.tmp / "tmux" / "ts-lead-dev"))

    def test_never_into_a_server_it_did_not_start(self):
        """ts-agents runs the manager on `tmux -L ts-<name>`: a start of the same name must not land inside it."""
        (self.tmp / "tmux").mkdir(mode=0o700)
        sock = self.tmp / "tmux" / "ts-appdev"
        subprocess.run([TMUX, "-S", str(sock), "new-session", "-d", "-s", "AppDev", "-n", "mgr", "sleep 30"],
                       check=True)
        r = self.run_one()
        self.assertEqual((r["error"], r["field"]), ("running", "name"))
        self.assertEqual(self.sessions(sock), ["AppDev:mgr"])
        self.assertEqual(self.argvs(0.3), [])

    def test_the_limit_counts_every_server_and_the_old_one(self):
        self.config(max_running=3)
        subprocess.run([TMUX, "-S", str(self.tmp / "legacy"), "new-session", "-d", "-s", start.SESSION, "-n", "old",
                        "sleep 30"], check=True)
        r = self.run_one(name="old")
        self.assertEqual(r["error"], "running")  # a window of that name on the old shared server
        self.assertNotIn("error", self.run_one(name="a"))
        self.assertNotIn("error", self.run_one(name="b"))
        self.assertEqual(self.run_one(name="c")["error"], "limit")

    def test_the_agent_gets_a_clean_environment(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "100.64.0.9 5 100.64.0.1 22", "LD_PRELOAD": "/x.so",
                                          "TMUX_TMPDIR": "/tmp/elsewhere"}):
            self.assertNotIn("error", self.run_one())
        self.argvs()
        env = dict(line.split("=", 1) for line in next(self.bin.glob("env.*")).read_text().splitlines() if "=" in line)
        for k in ("SSH_CONNECTION", "LD_PRELOAD", "TMUX_TMPDIR"):
            self.assertNotIn(k, env)
        self.assertEqual(env["TAILSHARE_AGENT_NAME"], "AppDev")
        self.assertTrue(env["TMUX"].startswith(str(self.tmp / "tmux" / "ts-appdev") + ","))

    def test_a_folder_others_can_use_is_refused(self):
        (self.tmp / "tmux").mkdir(mode=0o755)
        (self.tmp / "tmux").chmod(0o755)
        self.assertEqual(self.run_one()["error"], "no_tmux")
        self.assertEqual(self.argvs(0.3), [])


@unittest.skipUnless(TMUX, "needs tmux")
class Confirm(Base):
    def test_start_runs_once(self):
        plan = self.dry(self.item(instructions='Role "x"\n-y', prompt="hello"))[0]
        r = self.confirm(plan["plan_id"])
        got = r["results"][0]
        self.assertNotIn("error", got, got)
        self.assertRegex(got["tmux"]["pane"], r"^%[0-9]+$")
        self.assertEqual(got["tmux"]["socket"], str(self.tmp / "sock"))
        self.assertEqual(got["session"], plan["plan"]["session"])
        [argv] = self.argvs()
        self.assertIn('--append-system-prompt=Role "x"\n-y', argv)
        self.assertEqual(argv[-2:], ["--", "hello"])
        env = next(self.bin.glob("env.*")).read_text()
        self.assertIn("TAILSHARE_AGENT_NAME=AppDev", env)
        again = self.confirm(plan["plan_id"])["results"][0]
        self.assertTrue(again["already"])
        self.assertEqual(again["tmux"], got["tmux"])
        time.sleep(0.3)
        self.assertEqual(len(self.argvs()), 1)
        out = subprocess.run([TMUX, "-S", str(self.tmp / "sock"), "list-windows", "-a", "-F", "#{session_name}:#{window_name}"],
                             capture_output=True, text=True).stdout.split()
        self.assertEqual(out, ["tailshare-start:appdev"])

    def test_plans_belong_to_their_key(self):
        pid = self.dry(self.item())[0]["plan_id"]
        self.assertEqual(self.confirm(pid, key=OTHER)["results"][0]["error"], "unknown_plan")
        self.assertEqual(self.confirm("0" * 32)["results"][0]["error"], "unknown_plan")
        self.assertEqual(self.confirm("../x")["results"][0]["error"], "unknown_plan")
        self.assertNotIn("error", self.confirm(pid)["results"][0])

    def test_plan_key_plans_and_the_start_key_confirms(self):
        pid = start.handle({"v": 1, "dry_run": True, "starts": [self.item()]}, "safe", KEY, "air", plan_only=True)
        pid = pid["results"][0]["plan_id"]
        r = start.handle({"v": 1, "confirm": [pid]}, "safe", KEY, "air", plan_only=True)
        self.assertEqual(r["error"], "no_grant")  # the plan key never starts anything
        self.assertEqual(self.argvs(wait=0.3), [])
        self.assertNotIn("error", self.confirm(pid)["results"][0])  # the pair's start key does

    def test_open_plans_are_capped(self):
        for _ in range(start.MAX_OPEN_PLANS // start.MAX_ITEMS):
            self.assertNotIn("error", self.dry(*[self.item()] * start.MAX_ITEMS)[-1])
        r = self.call({"v": 1, "dry_run": True, "starts": [self.item()]})
        self.assertEqual(r["error"], "rate")
        self.assertGreater(r["retry_after"], 0)
        self.assertNotIn("error", self.dry(self.item(), key=OTHER)[0])  # per key

    def test_expired(self):
        pid = self.dry(self.item())[0]["plan_id"]
        with mock.patch.object(start.time, "time", return_value=time.time() + start.PLAN_TTL + 1):
            self.assertIn(self.confirm(pid)["results"][0]["error"], ("expired", "unknown_plan"))
        self.assertEqual(self.argvs(0.3), [])

    def test_changed_since_the_dry_run(self):
        a, b, c = (r["plan_id"] for r in self.dry(self.item(name="a"), self.item(name="b", options={"model": "opus"}),
                                                    self.item(name="c", options={"skip_permissions": True}), grant="skip"))
        self.config(kinds={"claude": {"bin": str(self.bin / "claude"), "options": ["skip_permissions"]}})
        self.assertEqual(self.confirm(b, grant="skip")["results"][0]["error"], "changed")
        self.assertEqual(self.confirm(c, grant="safe")["results"][0]["error"], "changed")  # grant lowered since
        (self.root / "app").rename(self.root / "moved")
        self.assertEqual(self.confirm(a, grant="skip")["results"][0]["error"], "changed")
        self.assertEqual(self.argvs(0.3), [])

    def test_running_and_limit(self):
        first = self.dry(self.item(), self.item())
        r = self.confirm(*(x["plan_id"] for x in first))["results"]
        self.assertNotIn("error", r[0])
        self.assertEqual(r[1]["error"], "running")
        names = [self.item(name=f"m{i}") for i in range(4)]
        r = self.confirm(*(x["plan_id"] for x in self.dry(*names)))["results"]
        self.assertEqual([x.get("error") for x in r], [None, None, None, "limit"])

    def test_rate_counts_confirms(self):
        for _ in range(start.CONFIRMS_PER_MIN):
            self.confirm("0" * 32)
        r = self.confirm("0" * 32)
        self.assertEqual(r["error"], "rate")
        self.assertGreater(r["retry_after"], 0)
        self.assertNotIn("error", self.confirm("0" * 32, key=OTHER))  # per key


class Gate(Base):
    def run_gate(self, cmd, stdin=b"", grant="safe"):
        env = {**os.environ, "SSH_ORIGINAL_COMMAND": cmd}
        return subprocess.run([sys.executable, "-m", "tailshare", "ssh-gate", f"--start={grant}", f"--key={KEY}",
                               f"--tmux-socket={self.tmp / 'sock'}", f"--state={self.state}",
                               f"--bus-port={free_port()}"],  # never the real bus
                              input=stdin, capture_output=True, env=env, cwd=str(Path(__file__).parents[1]), timeout=30)

    def test_only_tailshare_start(self):
        for cmd in ("", "tmux attach -t %1", "tailshare-start; sh", "sh"):
            r = self.run_gate(cmd)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertEqual(r.stdout, b"")

    def test_settle_ignores_the_environment(self):
        with mock.patch.dict(os.environ, {"TAILSHARE_STATE": "/tmp/evil", "TAILSHARE_BUS_PORT": "1", "HOME": "/tmp/evil",
                                          "PATH": "/tmp/evil", "TAILSHARE_TMUX_SOCKET": "/tmp/evil", "TMUX": "/tmp/evil,1,0"}), \
                mock.patch.object(bus, "BUS_PORT", 1):
            start.settle()
            self.assertNotIn("evil", str(bus.STATE_DIR))
            self.assertTrue(str(bus.STATE_DIR).endswith("/.tailshare"))
            self.assertEqual((bus.BUS_PORT, start.TMUX_SOCKET), (8789, ""))
            self.assertEqual(os.environ["PATH"], start.SAFE_PATH)
            self.assertNotIn("evil", os.environ["HOME"])
            self.assertNotIn("TAILSHARE_STATE", os.environ)
            self.assertNotIn("TMUX", os.environ)
            start.settle(str(self.state), 4321, "/x/sock")  # only these, from the key's line
            self.assertEqual((bus.STATE_DIR, bus.BUS_PORT, start.TMUX_SOCKET), (self.state, 4321, "/x/sock"))

    def test_interpreter_is_isolated(self):
        """What the start key's line runs ignores PYTHON* variables and the current folder: no code loads early."""
        evil = self.tmp / "evil"
        (evil / "tailshare").mkdir(parents=True)
        marker = self.tmp / "loaded"
        for f in ("sitecustomize.py", "usercustomize.py", "startup.py", "tailshare/__init__.py", "json.py"):
            (evil / f).write_text(f"open({str(marker)!r}, 'a').write({f!r})\n")
        line = keys.key_line({"id": KEY, "mode": "start", "pubkey": "x", "device": "d"}, "/x")
        argv = line.split('command="', 1)[1].split('"', 1)[0].split()
        self.assertEqual(argv[:4], [sys.executable, "-I", "-m", "tailshare"])
        env = {**os.environ, "PYTHONPATH": str(evil), "PYTHONSTARTUP": str(evil / "startup.py"), "PYTHONHOME": "",
               "PYTHONUSERBASE": str(evil), "SSH_ORIGINAL_COMMAND": "nope"}
        env.pop("PYTHONHOME")
        subprocess.run([*argv[:2], "-c", "import json, sitecustomize"], env=env, cwd=str(evil), capture_output=True)
        subprocess.run([*argv[:2], "-c", "import json"], env=env, cwd=str(evil), capture_output=True)
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")

    def test_socket_never_from_the_environment(self):
        code = "from tailshare import start; print(repr(start.TMUX_SOCKET))"
        for var in ("TAILSHARE_TMUX_SOCKET", "TMUX_SOCKET", "TMUX"):
            r = subprocess.run([sys.executable, "-c", code], env={**os.environ, var: "/tmp/evil"}, capture_output=True,
                               text=True, cwd=str(Path(__file__).parents[1]), check=True)
            self.assertEqual(r.stdout.strip(), "''", var)

    def test_one_json_line(self):
        doc = json.dumps({"v": 1, "dry_run": True, "starts": [self.item(), self.item(cwd="/")]}).encode()
        r = self.run_gate("tailshare-start", doc)
        self.assertEqual(r.returncode, 1)  # one item failed
        lines = r.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1)
        out = json.loads(lines[0])
        self.assertIn("plan_id", out["results"][0])
        self.assertEqual(out["results"][1]["error"], "not_allowed")
        r = self.run_gate("tailshare-start", b"not json")
        self.assertEqual((r.returncode, json.loads(r.stdout)["error"]), (1, "bad_request"))
        r = self.run_gate("tailshare-start", b" " * (start.MAX_INPUT + 1))
        self.assertEqual(json.loads(r.stdout)["error"], "bad_request")
        ok = json.dumps({"v": 1, "dry_run": True, "starts": [self.item()]}).encode()
        self.assertEqual(self.run_gate("tailshare-start", ok).returncode, 0)


@unittest.skipUnless(TMUX and Path("/usr/sbin/sshd").exists(), "needs tmux and sshd")
class OverSSH(Base):
    """The real path: ssh to a throwaway sshd on a private loopback port, with its own host key, authorized_keys and
    state dir. Not the machine's sshd, ~/.ssh or ~/.tailshare."""

    def setUp(self):
        super().setUp()
        d = self.tmp / "ssh"
        d.mkdir(mode=0o700)
        for name in ("host", "client", "plan"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / name)], check=True)
        # stands in for the interpreter: takes the line's `-I -m tailshare` and runs this checkout's tailshare, isolated
        py = d / "python"
        py.write_text(f"""#!/bin/sh
[ "$1 $2 $3" = "-I -m tailshare" ] || exit 9
shift 3
exec {sys.executable} -I -c 'import runpy, sys; sys.path.insert(0, "{Path(__file__).parents[1]}"); \\
runpy.run_module("tailshare", run_name="__main__", alter_sys=True)' "$@"
""")
        py.chmod(0o755)
        pub = {n: " ".join((d / f"{n}.pub").read_text().split()[:2]) for n in ("client", "plan")}
        rest = f' --tmux-socket={self.tmp / "sock"} --state={self.state} --bus-port={free_port()}'
        lines = [x.replace(f'--key={KEY}"', f'--key={KEY}{rest}"').replace(" --plan\"", f'{rest} --plan"')
                 for x in keys.key_lines({"id": KEY, "mode": "start", "pubkey": pub["client"], "plan_pubkey": pub["plan"],
                                          "device": "test"}, "/unused", str(py))]
        (d / "authorized_keys").write_text("\n".join(lines) + "\n")
        (d / "authorized_keys").chmod(0o600)
        self.port = free_port()
        (d / "sshd_config").write_text(
            f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {d / 'host'}\nAuthorizedKeysFile {d / 'authorized_keys'}\n"
            f"PidFile {d / 'pid'}\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n"
            "PermitUserRC no\nStrictModes no\nPermitUserEnvironment no\n")
        self.sshd_log = open(d / "log", "wb")
        self.sshd = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(d / "sshd_config")],
                                     stdout=subprocess.DEVNULL, stderr=self.sshd_log)
        self.addCleanup(self._stop)
        self.ssh = ["ssh", "-T", "-F", "none", "-p", str(self.port), "-i", str(d / "client"), "-o", "BatchMode=yes",
                    "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", f"UserKnownHostsFile={d / 'known'}",
                    "-o", "StrictHostKeyChecking=accept-new", f"{os.environ.get('USER')}@127.0.0.1"]
        for _ in range(100):
            if subprocess.run(["nc", "-z", "127.0.0.1", str(self.port)], capture_output=True).returncode == 0:
                break
            time.sleep(0.05)

    def _stop(self):
        self.sshd.terminate()
        self.sshd.wait(5)
        self.sshd_log.close()

    def send(self, cmd, doc, ident="client"):
        ssh = [x.replace(f"{os.sep}client", f"{os.sep}{ident}") if x.endswith(f"{os.sep}client") else x for x in self.ssh]
        r = subprocess.run([*ssh, cmd], input=json.dumps(doc).encode(), capture_output=True, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def test_plan_key_over_ssh(self):
        code, out, err = self.send("tailshare-start", {"v": 1, "dry_run": True, "starts": [self.item()]}, ident="plan")
        self.assertEqual(code, 0, err)
        pid = json.loads(out)["results"][0]["plan_id"]
        code, out, _ = self.send("tailshare-start", {"v": 1, "confirm": [pid]}, ident="plan")
        self.assertEqual((code, json.loads(out)["error"]), (1, "no_grant"))
        code, out, err = self.send("tailshare-start", {"v": 1, "confirm": [pid]})
        self.assertEqual(code, 0, (out, err))
        self.assertRegex(json.loads(out)["results"][0]["tmux"]["pane"], r"^%[0-9]+$")

    def test_dry_run_then_confirm(self):
        rc, out, err = self.send("tailshare-start", {"v": 1, "dry_run": True, "starts": [self.item(prompt="hi")]})
        self.assertEqual(rc, 0, out + err)
        pid = json.loads(out)["results"][0]["plan_id"]
        rc, out, err = self.send("tailshare-start", {"v": 1, "confirm": [pid]})
        self.assertEqual(rc, 0, out + err)
        got = json.loads(out)["results"][0]["tmux"]
        self.assertRegex(got["pane"], r"^%[0-9]+$")
        self.assertEqual(got["socket"], str(self.tmp / "sock"))
        self.assertEqual(self.argvs()[0][-2:], ["--", "hi"])

    def test_nothing_else_runs(self):
        for cmd in ("id", "tmux ls", "tailshare-start && id"):
            rc, out, _ = self.send(cmd, {})
            self.assertNotEqual(rc, 0, cmd)
            self.assertEqual(out, b"", cmd)
        r = subprocess.run([*self.ssh[:1], "-tt", *self.ssh[2:]], input=b"id\nexit\n", capture_output=True, timeout=60)
        self.assertNotIn(b"uid=", r.stdout)  # no shell and no pty, even when asked for one


def free_port() -> int:
    from tests.test_bus import free_port as fp

    return fp()


class Keys(unittest.TestCase):
    def test_lines(self):
        k = {"id": KEY, "pubkey": "ecdsa-sha2-nistp256 AAAA", "device": "Nick iPhone"}
        self.assertEqual(keys.key_line({**k, "mode": "start"}, "/x/tailshare", "/x/python"),
                         f'restrict,command="/x/python -I -m tailshare ssh-gate --start=safe --key={KEY}" '
                         f"ecdsa-sha2-nistp256 AAAA tailshare:{KEY} Nick-iPhone")
        self.assertIn(f'command="{sys.executable} -I -m tailshare ', keys.key_line({**k, "mode": "start"}, "/x/tailshare"))
        self.assertIn('command="/usr/bin/false"', keys.key_line({**k, "mode": "start"}, "/x/tailshare", ""))
        self.assertIn("--start=skip", keys.key_line({**k, "mode": "start-skip"}, "/x/tailshare"))
        self.assertNotIn("pty", keys.key_line({**k, "mode": "start-skip"}, "/x/tailshare"))
        self.assertNotIn("--start", keys.key_line({**k, "mode": "pane"}, "/x/tailshare"))
        pair = keys.key_lines({**k, "mode": "start", "plan_pubkey": "ecdsa-sha2-nistp256 BBBB"}, "/x/tailshare", "/x/python")
        self.assertEqual(pair[1], f'restrict,command="/x/python -I -m tailshare ssh-gate --start=safe --key={KEY} --plan" '
                                  f"ecdsa-sha2-nistp256 BBBB tailshare:{KEY} plan Nick-iPhone")
        self.assertEqual(len(keys.key_lines({**k, "mode": "pane", "plan_pubkey": "x"}, "/x/tailshare")), 1)

    def test_purposes(self):
        self.assertEqual(keys.request_purpose({}), ("pane", []))
        self.assertEqual(keys.request_purpose({"purpose": "start"}), ("start", ["start"]))
        self.assertEqual(keys.request_purpose({"purpose": "start", "want": ["skip", "start"]}), ("start", ["start", "skip"]))
        for bad in ({"purpose": "root"}, {"purpose": "start", "want": ["skip"]}, {"purpose": "start", "want": "start"},
                    {"purpose": "start", "want": ["start", "shell"]}):
            with self.assertRaises(bus.BusError):
                keys.request_purpose(bad)

    def test_grants(self):
        self.assertEqual(keys.mode_problem("pane", "pane", []), "")
        self.assertEqual(keys.mode_problem("shell", "pane", []), "")
        self.assertTrue(keys.mode_problem("start", "pane", []))  # an attach key never gets a start grant
        self.assertTrue(keys.mode_problem("start-skip", "pane", []))
        self.assertTrue(keys.mode_problem("pane", "start", ["start"]))  # a start key never attaches
        self.assertTrue(keys.mode_problem("shell", "start", ["start"]))
        self.assertTrue(keys.mode_problem("start-skip", "start", ["start"]))  # never more than asked
        self.assertEqual(keys.mode_problem("start", "start", ["start", "skip"]), "")  # less is fine
        self.assertEqual(keys.default_mode("start", ["start", "skip"]), "start-skip")
        self.assertEqual(keys.default_mode("pane", []), "pane")


PAIR_VECTOR = "SHA256:EqpHWj31Me8a5Z+bo7GEb85e0WI41CUMdLoe1MA/XcE"  # the app checks against the same pair


class Enroll(unittest.TestCase):
    """A start key goes through the same enroll and approve as an attach key, with its purpose kept."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        net = StaticNet("air", ipmap={"100.1.1.1": "iphone"}, usermap={"100.1.1.1": "nick@example.com"})
        self.b = make_bus("air", net)
        p = [mock.patch.object(keys, "install_problem", return_value=""),
             mock.patch.object(keys, "gate_exe", return_value="/x/tailshare"),
             mock.patch.object(keys.Keys, "_notify")]  # no real macOS notification from a test
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.k = keys.Keys(self.b, keys_file=self.tmp / "authorized_keys", token_file=self.tmp / "token")
        self.pub, self.plan = (subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-N", "", "-f", str(self.tmp / n)],
                                              capture_output=True) and (self.tmp / f"{n}.pub").read_text().strip()
                               for n in ("id", "plan"))

    def start_request(self, **kw):
        return self.k.enroll("100.1.1.1", {"pubkey": self.pub, "plan_pubkey": self.plan, "device_name": "iPhone",
                                           "purpose": "start", "want": ["start", "skip"], **kw})

    def test_start_key_installs_with_its_grant(self):
        r = self.start_request()
        self.assertNotIn("squads", r)  # pending: nothing granted yet
        self.assertEqual((r["purpose"], r["want"]), ("start", ["start", "skip"]))
        p = self.k.pending()["pending"][0]
        self.assertEqual((p["purpose"], p["want"]), ("start", ["start", "skip"]))
        self.assertEqual(p["plan_fingerprint"], keys.fingerprint(keys.parse_pubkey(self.plan)))
        self.assertEqual(p["pair_fingerprint"], r["pair_fingerprint"])
        with self.assertRaises(bus.BusError) as e:  # the start key's own code is not enough
            self.k.approve({"id": r["request_id"], "mode": "start", "confirm": p["fingerprint"][-keys.CONFIRM_CHARS:]})
        self.assertEqual(e.exception.code, 403)
        tail = p["pair_fingerprint"][-keys.CONFIRM_CHARS:]
        with self.assertRaises(bus.BusError):
            self.k.approve({"id": r["request_id"], "mode": "pane", "confirm": tail})
        out = self.k.approve({"id": r["request_id"], "mode": "start", "confirm": tail})  # grant less than asked
        self.assertEqual(out["installed"][0]["mode"], "start")
        self.assertIs(self.k.status("100.1.1.1", r["request_id"])["squads"], False)  # not granted
        lines = [x for x in (self.tmp / "authorized_keys").read_text().splitlines() if "ecdsa" in x]
        kid = out["key_id"]
        self.assertEqual(lines, [  # both keys, with the same id: the plan key's plans are the pair's
            f'restrict,command="{sys.executable} -I -m tailshare ssh-gate --start=safe --key={kid}" '
            f"{keys.parse_pubkey(self.pub)} tailshare:{kid} iPhone",
            f'restrict,command="{sys.executable} -I -m tailshare ssh-gate --start=safe --key={kid} --plan" '
            f"{keys.parse_pubkey(self.plan)} tailshare:{kid} plan iPhone"])
        self.k.rm({"key_id": kid})  # removing the start key removes its plan key
        self.assertFalse([x for x in (self.tmp / "authorized_keys").read_text().splitlines() if "ecdsa" in x])

    def test_pair_fingerprint_vector(self):
        start = ("ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBAECAwQFBgcICQoLDA0ODxAREhMU"
                 "FRYXGBkaGxwdHh8gISIjJCUmJygpKissLS4vMDEyMzQ1Njc4OTo7PD0+P0A=")  # 04 01..40: the point bytes 1 to 64
        plan = ("ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEBBQkNERUZHSElKS0xNTk9QUVJT"
                "VFVWV1hZWltcXV5fYGFiY2RlZmdoaWprbG1ub3BxcnN0dXZ3eHl6e3x9fn8=")  # 04 40..7f
        self.assertEqual(keys.pair_fingerprint(start, plan), PAIR_VECTOR)
        self.assertNotEqual(keys.pair_fingerprint(plan, start), PAIR_VECTOR)  # the order matters
        self.assertEqual(keys.confirm_code({"pubkey": start, "plan_pubkey": plan, "fingerprint": "x"}), PAIR_VECTOR[-6:])
        self.assertEqual(keys.confirm_code({"pubkey": start, "plan_pubkey": "", "fingerprint": "SHA256:abcdefgh"}), "cdefgh")

    def test_someone_elses_plan_key_does_not_get_the_code(self):
        mine = self.start_request()  # the phone, on its own address
        subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-N", "", "-f", str(self.tmp / "evil")], capture_output=True)
        evil = (self.tmp / "evil.pub").read_text().strip()
        self.k.bus.net.ipmap["100.6.6.6"] = "laptop"
        self.k.bus.net.usermap["100.6.6.6"] = "nick@example.com"
        theirs = self.k.enroll("100.6.6.6", {"pubkey": self.pub, "plan_pubkey": evil, "device_name": "iPhone",
                                             "purpose": "start", "want": ["start"]})
        self.assertNotEqual(theirs["pair_fingerprint"][-6:], mine["pair_fingerprint"][-6:])
        for code in (mine["fingerprint"][-6:], mine["pair_fingerprint"][-6:]):  # what the person sees on the phone
            with self.assertRaises(bus.BusError) as e:
                self.k.approve({"id": theirs["request_id"], "mode": "start", "confirm": code})
            self.assertEqual(e.exception.code, 403)
        for rid, code in ((theirs["request_id"], theirs["pair_fingerprint"][-6:]),
                          (mine["request_id"], mine["pair_fingerprint"][-6:])):
            with self.assertRaises(bus.BusError) as e:  # same start key, another plan key: neither, until one is denied
                self.k.approve({"id": rid, "mode": "start", "confirm": code})
            self.assertEqual(e.exception.code, 409)
        with self.assertRaises(bus.BusError) as e:
            self.k.approve({"id": mine["request_id"], "mode": "start", "confirm": mine["pair_fingerprint"][-6:]})
        self.assertIn("same start key with a different plan key", str(e.exception))
        self.k.deny({"id": theirs["request_id"]})
        out = self.k.approve({"id": mine["request_id"], "mode": "start", "confirm": mine["pair_fingerprint"][-6:]})
        self.assertIn(keys.parse_pubkey(self.plan), (self.tmp / "authorized_keys").read_text())
        self.assertNotIn(keys.parse_pubkey(evil), (self.tmp / "authorized_keys").read_text())
        self.assertTrue(out["key_id"])

    def test_a_start_request_without_its_plan_key_is_never_approved(self):
        r = self.start_request()
        self.k.store.x("UPDATE key_requests SET plan_pubkey='' WHERE id=?", r["request_id"])  # never from enroll
        for code in (r["fingerprint"][-6:], r["pair_fingerprint"][-6:]):
            with self.assertRaises(bus.BusError) as e:
                self.k.approve({"id": r["request_id"], "mode": "start", "confirm": code})
            self.assertEqual(e.exception.code, 409)
        self.assertFalse(self.k.ls()["keys"])

    def test_a_new_plan_key_is_a_new_request(self):
        first = self.start_request()
        subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-N", "", "-f", str(self.tmp / "plan2")], capture_output=True)
        again = self.start_request(plan_pubkey=(self.tmp / "plan2.pub").read_text().strip())
        self.assertNotEqual(first["request_id"], again["request_id"])  # not the old request, whose code is the old pair's
        self.assertEqual(self.start_request()["request_id"], first["request_id"])

    def test_start_keys_are_p256(self):
        ed = self.tmp / "ed"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ed)], check=True, capture_output=True)
        ed = (self.tmp / "ed.pub").read_text().strip()
        for kw in ({"pubkey": ed}, {"plan_pubkey": ed}):
            with self.assertRaises(bus.BusError):
                self.start_request(**kw)

    def test_start_key_needs_its_plan_key(self):
        for bad in ({"plan_pubkey": None}, {"plan_pubkey": "nonsense"}):
            with self.assertRaises(bus.BusError):
                self.start_request(**bad)
        with self.assertRaises(bus.BusError):
            self.start_request(plan_pubkey=self.pub)  # one key cannot be both

    def test_plan_key_already_installed(self):
        r = self.k.enroll("100.1.1.1", {"pubkey": self.plan, "device_name": "iPhone"})
        self.k.approve({"id": r["request_id"], "confirm": r["fingerprint"][-keys.CONFIRM_CHARS:]})
        with self.assertRaises(bus.BusError) as e:  # else sshd would take the attach line for the plan key
            self.start_request()
        self.assertEqual(e.exception.code, 409)

    def test_signed_approval_carries_the_plan_key(self):
        r = self.start_request()
        p = self.k.pending()["pending"][0]
        d = json.loads(keys.approval_doc("me", "air", p, "k" + "1" * 12, "start", ["air"]))
        self.assertEqual((d["plan_pubkey"], d["plan_fingerprint"]), (p["plan_pubkey"], p["plan_fingerprint"]))
        self.assertNotIn("plan_pubkey", json.loads(keys.approval_doc("me", "air", p, "k" + "1" * 12, "pane", ["air"])))
        self.assertTrue(r["request_id"])

    def test_attach_key_cannot_get_start(self):
        r = self.k.enroll("100.1.1.1", {"pubkey": self.pub, "device_name": "iPhone"})
        tail = r["fingerprint"][-keys.CONFIRM_CHARS:]
        with self.assertRaises(bus.BusError):
            self.k.approve({"id": r["request_id"], "mode": "start", "confirm": tail})
        self.assertEqual(self.k.approve({"id": r["request_id"], "confirm": tail})["installed"][0]["mode"], "pane")


class Hello(Base):
    def test_start_block_only_for_the_owner(self):
        net = StaticNet("air", ipmap={"100.1.1.1": "iphone", "100.2.2.2": "alice-phone"},
                        usermap={"100.1.1.1": "nick@example.com", "100.2.2.2": "alice@example.com"})
        b = make_bus("air", net)
        self.assertEqual(b.hello("100.1.1.1")["start"]["kinds"], ["claude", "codex"])
        self.assertNotIn("start", b.hello("100.2.2.2"))
        self.assertNotIn("start", b.hello())
        self.assertEqual(b.start_folders("100.1.1.1")["folders"][0], str(self.root / "app"))
        with self.assertRaises(bus.BusError):
            b.start_folders("100.2.2.2")
        (self.state / "start.json").unlink()
        self.assertNotIn("start", b.hello("100.1.1.1"))


if __name__ == "__main__":
    unittest.main()
