"""Starting sessions from a start key (tailshare/start.py): config, plans, confirm, the gate, key grants.

Everything runs in a temp state dir and on a private tmux socket; the agent is a fake script that records its argv.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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
case "$TMUX" in "$TAILSHARE_TMUX_SOCKET",*) ;; *) echo "not on the test's tmux socket" >&2; exit 1 ;; esac
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
        patches = [mock.patch.object(bus, "STATE_DIR", self.state),
                   mock.patch.object(start, "TMUX_SOCKET", str(self.tmp / "sock")),
                   # and in every gate this test runs as a subprocess: never the user's own tmux server
                   mock.patch.dict(os.environ, {"TAILSHARE_TMUX_SOCKET": str(self.tmp / "sock")})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._kill_tmux)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config()

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
                       self.item(options={"permission_mode": "auto"}),
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
        self.assertEqual(a[-2:], ["--", "--help"])
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
        env = {**os.environ, "SSH_ORIGINAL_COMMAND": cmd, "TAILSHARE_STATE": str(self.state),
               "TAILSHARE_BUS_PORT": str(free_port())}  # never the real bus
        return subprocess.run([sys.executable, "-m", "tailshare", "ssh-gate", f"--start={grant}", f"--key={KEY}"],
                              input=stdin, capture_output=True, env=env, cwd=str(Path(__file__).parents[1]), timeout=30)

    def test_only_tailshare_start(self):
        for cmd in ("", "tmux attach -t %1", "tailshare-start; sh", "sh"):
            r = self.run_gate(cmd)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertEqual(r.stdout, b"")

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
        for name in ("host", "client"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(d / name)], check=True)
        exe = f"env TAILSHARE_STATE={self.state} TAILSHARE_TMUX_SOCKET={self.tmp / 'sock'} TAILSHARE_BUS_PORT={free_port()}" \
              f" PYTHONPATH={Path(__file__).parents[1]}" \
              f" {sys.executable} -m tailshare"
        line = keys.key_line({"id": KEY, "mode": "start", "pubkey": " ".join((d / "client.pub").read_text().split()[:2]),
                              "device": "test"}, exe)
        (d / "authorized_keys").write_text(line + "\n")
        (d / "authorized_keys").chmod(0o600)
        self.port = free_port()
        (d / "sshd_config").write_text(
            f"Port {self.port}\nListenAddress 127.0.0.1\nHostKey {d / 'host'}\nAuthorizedKeysFile {d / 'authorized_keys'}\n"
            f"PidFile {d / 'pid'}\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nUsePAM no\n"
            "PermitUserRC no\nStrictModes no\nPermitUserEnvironment no\n")
        self.sshd = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", str(d / "sshd_config")],
                                     stdout=subprocess.DEVNULL, stderr=open(d / "log", "wb"))
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

    def send(self, cmd, doc):
        r = subprocess.run([*self.ssh, cmd], input=json.dumps(doc).encode(), capture_output=True, timeout=60)
        return r.returncode, r.stdout, r.stderr

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
        self.assertEqual(keys.key_line({**k, "mode": "start"}, "/x/tailshare"),
                         f'restrict,command="/x/tailshare ssh-gate --start=safe --key={KEY}" ecdsa-sha2-nistp256 AAAA '
                         f"tailshare:{KEY} Nick-iPhone")
        self.assertIn("--start=skip", keys.key_line({**k, "mode": "start-skip"}, "/x/tailshare"))
        self.assertNotIn("pty", keys.key_line({**k, "mode": "start-skip"}, "/x/tailshare"))
        self.assertNotIn("--start", keys.key_line({**k, "mode": "pane"}, "/x/tailshare"))

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


class Enroll(unittest.TestCase):
    """A start key goes through the same enroll and approve as an attach key, with its purpose kept."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        net = StaticNet("air", ipmap={"100.1.1.1": "iphone"}, usermap={"100.1.1.1": "nick@example.com"})
        self.b = make_bus("air", net)
        p = [mock.patch.object(keys, "install_problem", return_value=""),
             mock.patch.object(keys, "gate_exe", return_value="/x/tailshare")]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        self.k = keys.Keys(self.b, keys_file=self.tmp / "authorized_keys", token_file=self.tmp / "token")
        self.pub = subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-N", "", "-f", str(self.tmp / "id")],
                                  capture_output=True) and (self.tmp / "id.pub").read_text().strip()

    def test_start_key_installs_with_its_grant(self):
        r = self.k.enroll("100.1.1.1", {"pubkey": self.pub, "device_name": "iPhone", "purpose": "start",
                                        "want": ["start", "skip"]})
        self.assertEqual((r["purpose"], r["want"]), ("start", ["start", "skip"]))
        p = self.k.pending()["pending"][0]
        self.assertEqual((p["purpose"], p["want"]), ("start", ["start", "skip"]))
        tail = p["fingerprint"][-keys.CONFIRM_CHARS:]
        with self.assertRaises(bus.BusError):
            self.k.approve({"id": r["request_id"], "mode": "pane", "confirm": tail})
        out = self.k.approve({"id": r["request_id"], "mode": "start", "confirm": tail})  # grant less than asked
        self.assertEqual(out["installed"][0]["mode"], "start")
        line = [x for x in (self.tmp / "authorized_keys").read_text().splitlines() if "ecdsa" in x][0]
        self.assertTrue(line.startswith('restrict,command="/x/tailshare ssh-gate --start=safe --key=k'), line)

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
