"""SSH key enrollment: key parsing, the authorized_keys block, the gate, and approve/fan-out/revoke.

Run with: uv run python -m unittest discover tests
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from tailshare import bus, keys, sessions, start
from tests.test_bus import StaticNet, free_port, make_bus

OWNER = "nick@example.com"
PHONE = "100.64.0.9"


def keypair(tmp: Path, name: str = "id") -> tuple[Path, str]:
    path = tmp / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "phone comment", "-f", str(path)], check=True)
    return path, (tmp / f"{name}.pub").read_text().strip()


class Parsing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.priv, self.pub = keypair(self.tmp)

    def test_pubkey_and_fingerprint(self):
        k = keys.parse_pubkey(self.pub)
        self.assertEqual(k, " ".join(self.pub.split()[:2]))  # comment dropped
        out = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(self.tmp / "id.pub")],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(keys.fingerprint(k), out.split()[1])

    def test_pubkey_rejects(self):
        kind, b64 = self.pub.split()[:2]
        for bad in ("", "ssh-rsa " + b64, f"{kind} not-base64!!", f"ssh-ed25519 {b64[:-8]}AAAAAAA=",
                    f'command="x" {kind} {b64}', 42, "ecdsa-sha2-nistp256 " + b64):
            with self.assertRaises(bus.BusError, msg=bad):
                keys.parse_pubkey(bad)

    def test_clean_device(self):
        self.assertEqual(keys.clean_device("Nick's iPhone"), "Nick's iPhone")
        self.assertEqual(keys.clean_device('x"; rm -rf ~\n'), "x rm -rf")
        self.assertEqual(keys.clean_device(None), "unnamed device")
        self.assertEqual(len(keys.clean_device("a" * 200)), 64)

    def test_sshd_keys_files(self):
        conf = self.tmp / "sshd_config"
        conf.write_text("# AuthorizedKeysFile /nope\nPort 22\nAuthorizedKeysFile\t.ssh/authorized_keys .ssh/k2 # hi\n")
        self.assertEqual(keys.sshd_keys_files([str(conf)]), [".ssh/authorized_keys", ".ssh/k2"])
        self.assertEqual(keys.sshd_keys_files([str(self.tmp / "missing")]), [])


class Block(unittest.TestCase):
    def test_replace_block(self):
        old = "ssh-ed25519 AAAA mine\n# a note\n"
        new = keys.replace_block(old, ["L1"])
        self.assertEqual(new, f"ssh-ed25519 AAAA mine\n# a note\n{keys.BEGIN}\nL1\n{keys.END}\n")
        again = keys.replace_block(new + "after\n", ["L2", "L3"])
        self.assertEqual(again, f"ssh-ed25519 AAAA mine\n# a note\n{keys.BEGIN}\nL2\nL3\n{keys.END}\nafter\n")
        self.assertEqual(keys.replace_block(again, []), "ssh-ed25519 AAAA mine\n# a note\nafter\n")
        self.assertEqual(keys.replace_block("", []), "")
        with self.assertRaises(bus.BusError):
            keys.replace_block(f"{keys.BEGIN}\nL1\n", ["L2"])

    def test_write_keeps_mode_and_refuses_symlinks(self):
        tmp = Path(tempfile.mkdtemp())
        f = tmp / ".ssh" / "authorized_keys"
        f.parent.mkdir(mode=0o700)
        f.write_text("ssh-ed25519 AAAA mine\n")
        os.chmod(f, 0o640)
        self.assertTrue(keys.write_authorized_keys(["L1"], f))
        self.assertEqual(f.stat().st_mode & 0o777, 0o640)
        self.assertTrue(f.read_text().startswith("ssh-ed25519 AAAA mine\n"))
        self.assertFalse(keys.write_authorized_keys(["L1"], f))  # unchanged
        self.assertEqual([p.name for p in f.parent.iterdir()], ["authorized_keys"])  # no temp files left
        link = tmp / ".ssh" / "linked"
        link.symlink_to(f)
        with self.assertRaises(bus.BusError):
            keys.write_authorized_keys(["L2"], link)
        fresh = tmp / "new" / "authorized_keys"
        keys.write_authorized_keys(["L1"], fresh)
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o600)

    def test_key_line(self):
        k = {"id": "k0123456789ab", "device": "Nick's iPhone", "pubkey": "ssh-ed25519 AAAA", "mode": "pane"}
        self.assertEqual(keys.key_line(k, "/u/bin/tailshare", "/u/bin/python"),  # -I: no PYTHON* from the client
                         'restrict,pty,command="/u/bin/python -I -m tailshare ssh-gate" ssh-ed25519 AAAA tailshare:k0123456789ab Nick\'s-iPhone')
        self.assertEqual(keys.key_line(k, "/u/bin/tailshare", ""),  # an interpreter path that is not plain
                         'restrict,pty,command="/u/bin/tailshare ssh-gate" ssh-ed25519 AAAA tailshare:k0123456789ab Nick\'s-iPhone')
        self.assertEqual(keys.key_line({**k, "mode": "shell"}, "/x"), "ssh-ed25519 AAAA tailshare:k0123456789ab Nick's-iPhone")
        self.assertEqual(keys.key_line(k, "", ""),  # neither: fail closed, as start keys do
                         'restrict,pty,command="/usr/bin/false" ssh-ed25519 AAAA tailshare:k0123456789ab Nick\'s-iPhone')

    def test_gate_python_imports_tailshare_under_I(self):
        """A `pip install --user` or a checkout is off sys.path under -I: those keep the console script."""
        keys.gate_python.cache_clear()
        self.addCleanup(keys.gate_python.cache_clear)
        self.assertEqual(keys.gate_python(), sys.executable)  # the suite's interpreter has tailshare installed
        keys.gate_python.cache_clear()
        with mock.patch.object(keys.subprocess, "run", return_value=mock.Mock(returncode=1)) as run:
            self.assertEqual(keys.gate_python(), "")
        self.assertEqual(run.call_args.args[0], [sys.executable, "-I", "-c", "import tailshare"])
        self.assertEqual(run.call_args.kwargs["cwd"], "/")


class Gate(unittest.TestCase):
    S = "/private/tmp/tmux-501/default"

    def ok(self, cmd):
        r = keys.parse_attach(cmd)
        self.assertIsNotNone(r, cmd)
        return r

    def test_accepts(self):
        from tailshare.agent import attach_command

        generated = attach_command({"host": "h", "tmux": {"socket": self.S, "pane": "%7", "bin": "/opt/homebrew/bin/tmux"}}, "h")
        r = self.ok(generated)
        self.assertEqual((r["socket"], r["pane"], r["cc"]), (self.S, "%7", ""))
        r = self.ok(f"tmux -CC -S {self.S} attach -f ignore-size,active-pane -t %7")
        self.assertEqual((r["cc"], r["flags"]), ("-CC", ["ignore-size", "active-pane"]))
        r = self.ok(f"/usr/bin/tmux -S {self.S} attach-session -r -t %12 \\; select-pane -t %12")
        self.assertTrue(r["readonly"])
        self.assertEqual(keys.gate_argv(r, "/bin/tmux"),
                         ["/bin/tmux", "-S", self.S, "attach", "-r", "-t", "%12", ";", "select-pane", "-t", "%12"])

    def test_refuses(self):
        for cmd in ("", "bash", f"sh -c 'tmux -S {self.S} attach -t %7'", f"tmux -S {self.S} new-window",
                    f"tmux -S {self.S} attach -t %7 ; run-shell id", f"tmux -S {self.S} attach -t %7 ; select-pane -t %8",
                    f"tmux -S {self.S} attach -t main", f"tmux -S {self.S} attach -f read-only,run-shell -t %7",
                    f"tmux -S {self.S} attach -t %7 -t %8", f"tmux attach -t %7", f"tmux -S {self.S} attach -E -t %7",
                    f"tmux -S {self.S} attach -t %7 && id", f"tmux -S {self.S} attach -t %7 'unclosed"):
            self.assertIsNone(keys.parse_attach(cmd), cmd)


class Flow(unittest.TestCase):
    """air takes the phone's request; pro is the owner's other machine; eve is someone else's."""

    def setUp(self):
        self._problem = keys.install_problem
        keys.install_problem = lambda: ""
        self._exe = keys.gate_exe
        keys.gate_exe = lambda: "/u/bin/tailshare"
        self._notify = keys.Keys._notify
        keys.Keys._notify = lambda *a: None  # no desktop notifications from tests
        self._sk = keys.REQUIRE_SK
        keys.REQUIRE_SK = False  # ssh-keygen cannot make security keys without the hardware
        self.tmp = Path(tempfile.mkdtemp())
        self.pro_port = free_port()
        self.air = make_bus("air", StaticNet(
            "air", peers={"pro": {"ip": "127.0.0.1", "port": self.pro_port, "online": True, "user": OWNER}},
            ipmap={PHONE: "iphone", "100.64.0.66": "eve-laptop"},
            usermap={PHONE: OWNER, "100.64.0.66": "eve@example.com"}, user=OWNER))
        self.pro = make_bus("pro", StaticNet("pro", ipmap={"127.0.0.1": "air"}, usermap={"127.0.0.1": OWNER}, user=OWNER),
                            self.pro_port)
        self.signers = self.tmp / "allowed_signers"
        self.signing_key, sign_pub = keypair(self.tmp, "signer")
        self.signers.write_text(sessions.trust_line("nick", sign_pub))
        os.chmod(self.signers, 0o600)
        self.air_keys = self.machine_keys(self.air, "air")
        self.pro_keys = self.machine_keys(self.pro, "pro")
        self.air.keys, self.pro.keys = self.air_keys, self.pro_keys
        self.air.store.x("INSERT INTO peers(host, ip, version, last_ok) VALUES('pro','127.0.0.1','t',?)", time.time())
        _, self.phone_pub = keypair(self.tmp, "phone")
        self.srv = self.pro.listen("127.0.0.1", local=False)

    def tearDown(self):
        keys.install_problem, keys.gate_exe, keys.Keys._notify = self._problem, self._exe, self._notify
        keys.REQUIRE_SK = self._sk
        self.srv.shutdown()
        self.srv.server_close()

    def machine_keys(self, b, name):
        d = self.tmp / name
        return keys.Keys(b, self.signers, d / "authorized_keys", self.tmp / f"{name}.token",
                         machine_key=d / "machine_key", trusted=d / "trusted_machines")

    def enroll(self, ip=PHONE, pub=None, device="Nick's iPhone"):
        return self.air_keys.enroll(ip, {"pubkey": pub or self.phone_pub, "device_name": device})

    def test_lookup_by_prefix_from_another_of_the_owners_devices(self):
        """A squad card names a request by prefix; the owner's iPad checks a request the phone made."""
        rid = self.enroll()["request_id"]
        self.air.net.usermap["100.64.0.7"] = OWNER
        r = self.air_keys.route("GET", f"/keys/request/{rid[:8]}", {}, False, "100.64.0.7")
        self.assertEqual((r["request_id"], r["host"], r["device_name"], r["ip"], r["status"]),
                         (rid, "air", "Nick's iPhone", PHONE, "pending"))
        self.assertNotIn("pubkey", r)
        eve = self.enroll(ip="100.64.0.66", device="Eve")
        with self.assertRaises(bus.BusError) as c:  # another tailnet user's request: the owner's devices don't see it
            self.air_keys.route("GET", f"/keys/request/{eve['request_id'][:8]}", {}, False, "100.64.0.7")
        self.assertEqual(c.exception.code, 404)
        for prefix in (rid[:7], rid[:8].upper(), rid + "0", "../" + rid[:8], "0" * 8):
            with self.assertRaises(bus.BusError) as c:
                self.air_keys.route("GET", f"/keys/request/{prefix}", {}, False, "100.64.0.7")
            self.assertIn(c.exception.code, (400, 404), prefix)
        with self.assertRaises(bus.BusError) as c:
            self.air_keys.route("GET", f"/keys/request/{rid[:8]}", {}, False, "100.64.0.66")  # another tailnet user
        self.assertEqual(c.exception.code, 403)

    def test_enroll_and_status(self):
        r = self.enroll()
        self.assertEqual((r["status"], r["host"], r["owner"]), ("pending", "air", OWNER))
        self.assertEqual(r["fingerprint"], keys.fingerprint(keys.parse_pubkey(self.phone_pub)))
        self.assertEqual(self.enroll()["request_id"], r["request_id"])  # same key again: same request
        self.assertEqual(self.air_keys.status(PHONE, r["request_id"])["status"], "pending")
        with self.assertRaises(bus.BusError) as cm:
            self.air_keys.status("100.64.0.66", r["request_id"])  # someone else polling it
        self.assertEqual(cm.exception.code, 404)
        p = self.air_keys.pending()["pending"][0]
        self.assertEqual((p["node"], p["user"], p["device"]), ("iphone", OWNER, "Nick's iPhone"))
        with self.assertRaises(bus.BusError) as cm:
            self.air_keys.enroll("100.99.99.99", {"pubkey": self.phone_pub})
        self.assertEqual(cm.exception.code, 403)

    def test_rate_limit(self):
        for i in range(keys.MAX_PENDING):
            self.enroll(pub=keypair(self.tmp, f"k{i}")[1])
        with self.assertRaises(bus.BusError) as cm:
            self.enroll(pub=keypair(self.tmp, "one-more")[1])
        self.assertEqual(cm.exception.code, 429)

    def test_approve_here_only(self):
        r = self.enroll()
        out = self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "mode": "pane"})
        self.assertEqual([i["host"] for i in out["installed"]], ["air"])
        text = (self.tmp / "air" / "authorized_keys").read_text()
        self.assertIn(f'restrict,pty,command="{sys.executable} -I -m tailshare ssh-gate" {keys.parse_pubkey(self.phone_pub)} '
                      f"tailshare:{out['key_id']} Nick's-iPhone", text)
        self.assertFalse((self.tmp / "pro" / "authorized_keys").exists())
        st = self.air_keys.status(PHONE, r["request_id"])
        self.assertEqual((st["status"], st["key_id"]), ("approved", out["key_id"]))
        with self.assertRaises(bus.BusError):
            self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"]})  # not pending any more

    def signed(self, req, mode="pane", hosts=None):
        p = self.air_keys.pending()["pending"][0]
        self.assertEqual(p["id"], req["request_id"])
        payload = keys.approval_doc("nick", "air", p, "k" + "a" * 12, mode, hosts or ["air", "pro"])
        return payload, sessions.sign(payload, self.signing_key, sessions.KEYS_NAMESPACE)

    def test_signed_fan_out_replay_and_revoke(self):
        r = self.enroll()
        payload, sig = self.signed(r)
        out = self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "mode": "pane", "payload": payload, "signature": sig})
        self.assertEqual(sorted(i["host"] for i in out["installed"]), ["air", "pro"], out)
        self.assertIn("tailshare:kaaaaaaaaaaaa", (self.tmp / "pro" / "authorized_keys").read_text())
        with self.assertRaises(bus.BusError) as cm:  # the same signed approval again
            bus.request(f"http://127.0.0.1:{self.pro_port}/bus/v1/keys/install", {"payload": payload, "signature": sig})
        self.assertEqual(cm.exception.code, 403)
        rm = self.air_keys.rm({"key_id": "kaaaaaaaaaaaa"})
        self.assertEqual((rm["removed_here"], rm["elsewhere"]), (True, {"pro": "removed"}))
        self.assertNotIn("tailshare:", (self.tmp / "pro" / "authorized_keys").read_text())
        self.assertEqual(self.pro_keys.ls()["keys"], [])
        with self.assertRaises(bus.BusError):  # a revoked key id never comes back
            self.pro_keys.install_here("kaaaaaaaaaaaa", {"pubkey": "x", "fingerprint": "y", "device": "d",
                                                         "node": "n", "user": "u"}, "pane", "test")

    def test_start_keys_must_be_p256_at_install_and_verify(self):
        """An ed25519 pair cannot become a start key here, nor through a signed approval for another machine."""
        r = self.enroll()
        p = self.air_keys.pending()["pending"][0]
        plan = keys.parse_pubkey(keypair(self.tmp, "plan")[1])
        rec = {**p, "plan_pubkey": plan, "plan_fingerprint": keys.fingerprint(plan)}
        with self.assertRaises(bus.BusError) as cm:
            self.air_keys.install_here("k" + "b" * 12, rec, "start", "test")
        self.assertEqual(cm.exception.code, 409)
        payload = keys.approval_doc("nick", "air", rec, "k" + "a" * 12, "start", ["air", "pro"])
        sig = sessions.sign(payload, self.signing_key, sessions.KEYS_NAMESPACE)
        with self.assertRaises(bus.BusError) as cm:
            self.pro_keys.check_approval(payload, sig)
        self.assertEqual(cm.exception.code, 403)
        self.assertIn("start keys must be ecdsa-sha2-nistp256", str(cm.exception))
        self.assertTrue(r["request_id"])

    def p256(self, name):
        path = self.tmp / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ecdsa", "-b", "256", "-N", "", "-C", "", "-f", str(path)], check=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp)}, stdin=subprocess.DEVNULL)
        return keys.parse_pubkey((self.tmp / f"{name}.pub").read_text())

    def test_squad_grant_is_explicit_for_the_plan_key_only_and_fans_out_signed(self):
        """Only a start key's plan key, and only when the person grants it, may sign squad edits (docs/squads.md)."""
        start_pub, plan_pub = self.p256("start"), self.p256("plan")
        r = self.air_keys.enroll(PHONE, {"pubkey": start_pub, "plan_pubkey": plan_pub, "purpose": "start",
                                         "device_name": "iPad"})
        p = self.air_keys.pending()["pending"][0]
        payload = keys.approval_doc("nick", "air", p, "k" + "a" * 12, "start", ["air", "pro"])
        sig = sessions.sign(payload, self.signing_key, sessions.KEYS_NAMESPACE)
        body = {"confirm": r["pair_fingerprint"][-6:], "id": r["request_id"], "mode": "start", "payload": payload,
                "signature": sig, "squads": True}
        with self.assertRaises(bus.BusError) as cm:  # the grant must be in what was signed, not only asked for
            self.air_keys.approve(body)
        self.assertIn("signed for another request", str(cm.exception))
        payload = keys.approval_doc("nick", "air", p, "k" + "a" * 12, "start", ["air", "pro"], squads=True)
        sig = sessions.sign(payload, self.signing_key, sessions.KEYS_NAMESPACE)
        out = self.air_keys.approve({**body, "payload": payload, "signature": sig})
        self.assertEqual(sorted((i["host"], i["squads"]) for i in out["installed"]), [("air", True), ("pro", True)], out)
        self.assertEqual([k["squads"] for k in self.pro_keys.ls()["keys"]], [1])
        self.assertIs(self.air_keys.status(PHONE, r["request_id"])["squads"], True)  # the phone's poll says so
        # without it: nothing
        r2 = self.enroll()
        out = self.air_keys.approve({"confirm": r2["fingerprint"][-6:], "id": r2["request_id"], "mode": "pane"})
        self.assertFalse(out["installed"][0]["squads"])
        self.assertNotIn("squads", self.air_keys.status(PHONE, r2["request_id"]))  # an attach key: no such grant
        self.assertEqual({k["id"]: k["squads"] for k in self.air_keys.ls()["keys"]},
                         {"kaaaaaaaaaaaa": 1, out["key_id"]: 0})
        other = self.p256("other")
        with self.assertRaises(bus.BusError) as cm:  # a pane key never gets it
            self.air_keys.install_here("k" + "c" * 12, {**p, "pubkey": other, "fingerprint": keys.fingerprint(other),
                                                        "plan_pubkey": ""}, "pane", "test", squads=True)
        self.assertIn("only a start key's plan key", str(cm.exception))
        doc = json.loads(keys.approval_doc("nick", "air", p, "k" + "d" * 12, "pane", ["pro"]))
        doc.update(squads=True, nonce="e" * 32)
        payload = sessions.canonical(doc)
        with self.assertRaises(bus.BusError) as cm:  # nor through a signed approval
            self.pro_keys.check_approval(payload, sessions.sign(payload, self.signing_key, sessions.KEYS_NAMESPACE))
        self.assertIn("squads is only a start key's grant", str(cm.exception))

    def test_the_poll_tells_each_start_key_its_own_grant(self):
        """Two start keys side by side, one granted squad edits: each poll says its own, and a revoked one says none."""
        rs = [self.air_keys.enroll(PHONE, {"pubkey": self.p256(f"start{i}"), "plan_pubkey": self.p256(f"plan{i}"),
                                           "purpose": "start", "device_name": "iPad"}) for i in (0, 1)]
        for r, squads in zip(rs, (True, False)):
            p = next(x for x in self.air_keys.pending()["pending"] if x["id"] == r["request_id"])
            key_id = "k" + ("a" if squads else "b") * 12
            payload = keys.approval_doc("nick", "air", p, key_id, "start", ["air"], squads=squads)
            self.air_keys.approve({"confirm": r["pair_fingerprint"][-6:], "id": r["request_id"], "mode": "start",
                                   "payload": payload, "squads": squads,
                                   "signature": sessions.sign(payload, self.signing_key, sessions.KEYS_NAMESPACE)})
        self.assertEqual([self.air_keys.status(PHONE, r["request_id"])["squads"] for r in rs], [True, False])
        self.air_keys.revoke(PHONE, {"key_id": "kaaaaaaaaaaaa"})
        gone = self.air_keys.status(PHONE, rs[0]["request_id"])
        self.assertEqual(gone["status"], "revoked")
        self.assertNotIn("squads", gone)
        self.assertIs(self.air_keys.status(PHONE, rs[1]["request_id"])["squads"], False)

    def test_grant_squads_later_takes_the_pair_code_and_stays_on_this_machine(self):
        """A start key approved without squad edits gets them afterwards with the code the device shows, here only."""
        r = self.air_keys.enroll(PHONE, {"pubkey": self.p256("start"), "plan_pubkey": self.p256("plan"),
                                         "purpose": "start", "device_name": "iPad"})
        p = self.air_keys.pending()["pending"][0]
        kid = "k" + "a" * 12
        payload = keys.approval_doc("nick", "air", p, kid, "start", ["air", "pro"])
        self.air_keys.approve({"confirm": r["pair_fingerprint"][-6:], "id": r["request_id"], "mode": "start",
                               "payload": payload, "signature": sessions.sign(payload, self.signing_key,
                                                                              sessions.KEYS_NAMESPACE)})

        def grant(body, local=True, token=None):
            return self.air_keys.route("POST", "/keys/grant-squads", body, local, "127.0.0.1" if local else PHONE,
                                       self.air_keys.token if token is None else token)

        def code(body, **kw):
            with self.assertRaises(bus.BusError) as cm:
                grant(body, **kw)
            return cm.exception.code

        good = {"key_id": kid, "confirm": r["pair_fingerprint"][-6:]}
        self.assertEqual(code(good, token="x" * 64), 403)  # only the login's own CLI
        self.assertEqual(code(good, local=False), 404)  # no such route for devices
        self.assertEqual(code({"key_id": kid, "confirm": r["fingerprint"][-6:]}), 403)  # the start key's alone: no
        self.assertEqual(code({"key_id": kid}), 403)
        self.assertEqual(code({"key_id": "k" + "f" * 12, "confirm": "x"}), 404)
        self.assertEqual(code({"key_id": "nope"}), 400)
        self.assertEqual(code({"key_id": kid, "on": "yes"}), 400)
        self.assertIs(self.air_keys.status(PHONE, r["request_id"])["squads"], False)
        self.assertEqual(grant(good), {"host": "air", "key_id": kid, "squads": True})
        self.assertIs(self.air_keys.status(PHONE, r["request_id"])["squads"], True)
        self.assertEqual([k["squads"] for k in self.pro_keys.ls()["keys"]], [0])  # pro keeps its own
        grant({"key_id": kid, "on": False})  # taking it back needs no code
        self.assertIs(self.air_keys.status(PHONE, r["request_id"])["squads"], False)
        # a pane key never gets it
        r2 = self.enroll()
        k2 = self.air_keys.approve({"confirm": r2["fingerprint"][-6:], "id": r2["request_id"], "mode": "pane"})["key_id"]
        self.assertEqual(code({"key_id": k2, "confirm": r2["fingerprint"][-6:]}), 409)

    def test_grant_squads_cli_refuses_an_agent_shell(self):
        import argparse
        import io
        from contextlib import redirect_stderr

        args = argparse.Namespace(key_id="k" + "a" * 12, off=False)
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}), mock.patch.object(keys, "_call") as call, \
                redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            keys.cmd_grant_squads(args)
        call.assert_not_called()
        self.assertIn("AI agent", err.getvalue())

    def test_device_revokes_its_own_key(self):
        r = self.enroll()
        payload, sig = self.signed(r)
        self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "mode": "pane", "payload": payload, "signature": sig})
        with self.assertRaises(bus.BusError) as cm:  # not the device that enrolled it, nor the owner's machine
            self.air_keys.revoke("100.64.0.66", {"key_id": "kaaaaaaaaaaaa"})
        self.assertEqual(cm.exception.code, 403)
        out = self.air_keys.revoke(PHONE, {"key_id": "kaaaaaaaaaaaa"})
        self.assertEqual((out["removed_here"], out["elsewhere"]), (True, {"pro": "removed"}))
        self.assertNotIn("tailshare:", (self.tmp / "air" / "authorized_keys").read_text())
        self.assertNotIn("tailshare:", (self.tmp / "pro" / "authorized_keys").read_text())

    def test_tailscale_ssh_host_keys(self):
        ed = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDv5"
        self.assertEqual(bus.clean_host_keys([ed + " comment", "junk", 7, 'x"; y z']), [ed])
        self.air.net.tailscale_ssh, self.air.net.ssh_host_keys = True, [ed]
        self.air.net.peers["pro"]["ssh_host_keys"] = [ed]
        self.assertEqual(self.air_keys.ssh_info()["host_keys"], [ed])  # tailscaled's keys, not /etc/ssh
        self.assertEqual([p["ssh_host_keys"] for p in self.air.peer_list(PHONE)], [[ed], [ed]])

    def test_hello_says_who_is_asking_and_owner_gets_peers(self):
        self.assertEqual(self.air.hello(PHONE)["you"], {"user": OWNER, "node": "iphone"})
        self.assertEqual(self.air.hello("100.64.0.66")["you"], {"user": "eve@example.com", "node": "eve-laptop"})
        self.assertNotIn("you", self.air.hello())
        self.air.store.x("UPDATE peers SET node='n2' WHERE host='pro'")
        self.air.net.nodes = [  # two nodes may share a name; a node may not run tailshare
            {"id": "n2", "host": "pro", "ip": "127.0.0.1", "port": self.pro_port, "online": True, "user": OWNER},
            {"id": "n3", "host": "pro", "ip": "100.64.0.77", "port": 8789, "online": False, "user": "eve@example.com"},
            {"id": "n4", "host": "tv", "ip": "100.64.0.78", "port": 8789, "online": True, "user": OWNER}]
        peers = self.air.peer_list(PHONE)
        self.assertEqual([(p["host"], p["ip"], p["owner"], p["tailshare"]) for p in peers],
                         [("air", "", OWNER, True), ("pro", "100.64.0.77", "eve@example.com", False),
                          ("pro", "127.0.0.1", OWNER, True), ("tv", "100.64.0.78", OWNER, False)])
        self.assertEqual([p["port"] for p in peers[1:]], [None, self.pro_port, None])
        self.air.net.nodes[0]["id"] = "n9"  # another node took over pro's IP
        self.assertFalse(self.air.peer_list(PHONE)[2]["tailshare"])
        self.air.net.nodes = []
        self.air.store.x("UPDATE peers SET node='' WHERE host='pro'")
        peers = self.air.peer_list(PHONE)
        self.assertEqual([(p["host"], p["port"], p["self"]) for p in peers], [("air", self.air.port, True), ("pro", self.pro_port, False)])
        with self.assertRaises(bus.BusError) as cm:  # names the owner's machines: not for others
            self.air.peer_list("100.64.0.66")
        self.assertEqual(cm.exception.code, 403)

    def test_local_api_needs_the_token(self):
        local = self.air.listen("127.0.0.1", local=True)
        url = f"http://127.0.0.1:{local.server_address[1]}/bus/v1"
        try:
            r = self.enroll()
            body = {"id": r["request_id"], "mode": "shell", "confirm": r["fingerprint"][-6:]}
            for token in (None, "0" * 64):  # another user or process on this machine
                with self.assertRaises(bus.BusError) as cm:
                    bus.request(url + "/keys/approve", body, headers={keys.TOKEN_HEADER: token} if token else None)
                self.assertEqual(cm.exception.code, 403)
            self.assertEqual(self.air_keys.pending()["pending"][0]["id"], r["request_id"])  # nothing approved
            token = (self.tmp / "air.token").read_text()
            self.assertEqual(oct((self.tmp / "air.token").stat().st_mode & 0o777), "0o600")
            out = bus.request(url + "/keys/approve", body, headers={keys.TOKEN_HEADER: token})
            self.assertEqual(out["installed"][0]["mode"], "shell")
        finally:
            local.shutdown()
            local.server_close()

    def test_approve_checks_the_typed_characters(self):
        r = self.enroll()
        for confirm in (None, "", "abcdef", r["fingerprint"][-5:]):
            with self.assertRaises(bus.BusError) as cm:
                self.air_keys.approve({"id": r["request_id"], "confirm": confirm})
            self.assertEqual(cm.exception.code, 403)
        # a look-alike key pending at the same time blocks both
        other = self.enroll(pub=keypair(self.tmp, "twin")[1], device="Nick's iPhone")
        self.air_keys.store.x("UPDATE key_requests SET fingerprint=? WHERE id=?",
                              "SHA256:x" + r["fingerprint"][-6:], other["request_id"])
        with self.assertRaises(bus.BusError) as cm:
            self.air_keys.approve({"id": r["request_id"], "confirm": r["fingerprint"][-6:]})
        self.assertEqual(cm.exception.code, 409)

    def test_revoked_request_can_enroll_again(self):
        r = self.enroll()
        out = self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"]})
        self.air_keys.rm({"key_id": out["key_id"]})
        self.assertEqual(self.air_keys.status(PHONE, r["request_id"])["status"], "revoked")
        again = self.enroll()
        self.assertNotEqual(again["request_id"], r["request_id"])
        self.assertEqual(again["status"], "pending")

    def test_rm_reaches_machines_it_was_installed_on(self):
        r = self.enroll()
        payload, sig = self.signed(r)
        out = self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "mode": "pane",
                                     "payload": payload, "signature": sig})
        self.air.store.x("DELETE FROM peers WHERE host='pro'")  # pro no longer among the current targets
        self.assertEqual(self.air_keys.targets(), ["air"])
        self.assertEqual(self.air_keys.rm({"key_id": out["key_id"]})["elsewhere"], {"pro": "removed"})
        self.assertNotIn("tailshare:", (self.tmp / "pro" / "authorized_keys").read_text())

    def test_signed_approvals_need_a_security_key(self):
        r = self.enroll()
        payload, sig = self.signed(r)
        self.assertEqual(keys.signature_key_type(sig), "ssh-ed25519")
        keys.REQUIRE_SK = True
        with self.assertRaises(bus.BusError) as cm:
            self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "mode": "pane",
                                   "payload": payload, "signature": sig})
        self.assertEqual(cm.exception.code, 403)
        self.assertIn("security key", str(cm.exception))

    def test_device_cancels_its_pending_request(self):
        r = self.enroll()
        with self.assertRaises(bus.BusError) as cm:  # someone else's device
            self.air_keys.cancel("100.64.0.66", r["request_id"])
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(self.air_keys.cancel(PHONE, r["request_id"])["status"], "cancelled")
        self.assertEqual(self.air_keys.pending()["pending"], [])
        with self.assertRaises(bus.BusError):  # nothing to approve any more
            self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"]})
        with self.assertRaises(bus.BusError) as cm:
            self.air_keys.cancel(PHONE, r["request_id"])
        self.assertEqual(cm.exception.code, 409)
        self.assertEqual(self.enroll()["status"], "pending")  # can ask again
        url = f"http://127.0.0.1:{self.pro_port}/bus/v1/keys/enroll"
        r2 = bus.request(url, {"pubkey": self.phone_pub, "device_name": "ipad"})
        self.assertEqual(bus.request(f"{url}/{r2['request_id']}/cancel", {})["status"], "cancelled")

    def test_status_never_says_approving(self):
        r = self.enroll()
        self.air_keys.store.x("UPDATE key_requests SET status='approving' WHERE id=?", r["request_id"])
        self.assertEqual(self.air_keys.status(PHONE, r["request_id"])["status"], "pending")

    def test_launch_signature_is_not_a_key_approval(self):
        r = self.enroll()
        payload, _ = self.signed(r)
        wrong_ns = sessions.sign(payload, self.signing_key)  # tailshare-launch namespace
        with self.assertRaises(bus.BusError) as cm:
            self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "payload": payload, "signature": wrong_ns})
        self.assertEqual(cm.exception.code, 403)
        self.assertFalse((self.tmp / "air" / "authorized_keys").exists())

    def test_signature_must_match_request(self):
        r = self.enroll()
        payload, sig = self.signed(r, mode="shell")
        with self.assertRaises(bus.BusError):
            self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "mode": "pane", "payload": payload, "signature": sig})

    def test_peer_routes_need_same_user(self):
        eve = make_bus("eve", StaticNet("eve", usermap={"127.0.0.1": "eve@example.com"}, user=OWNER))
        eve.keys = self.machine_keys(eve, "eve")
        with self.assertRaises(bus.BusError) as cm:
            eve.keys.install_from_peer("127.0.0.1", {"payload": "{}", "signature": ""})
        self.assertEqual(cm.exception.code, 403)
        with self.assertRaises(bus.BusError) as cm:
            eve.keys.revoke_from_peer("127.0.0.1", {"key_id": "kaaaaaaaaaaaa"})
        self.assertEqual(cm.exception.code, 403)

    def test_http_routes(self):
        url = f"http://127.0.0.1:{self.pro_port}/bus/v1"
        # the peer listener answers enroll; 127.0.0.1 is "air" to pro, a device of the owner
        r = bus.request(f"{url}/keys/enroll", {"pubkey": self.phone_pub, "device_name": "ipad"})
        self.assertEqual(r["status"], "pending")
        self.assertEqual(bus.request(f"{url}/keys/enroll/{r['request_id']}")["status"], "pending")
        for path, body in (("/keys/pending", None), ("/keys/ls", None), ("/keys/approve", {"id": r["request_id"]}),
                           ("/keys/deny", {"id": r["request_id"]}), ("/keys/rm", {"key_id": "kaaaaaaaaaaaa"})):
            with self.assertRaises(bus.BusError) as cm:  # local-only routes are not on the peer API
                bus.request(url + path, body)
            self.assertEqual(cm.exception.code, 404, path)
        hello = bus.request(f"{url}/hello")
        self.assertEqual((hello["ssh"]["owner"], hello["ssh"]["port"], hello["ssh"]["enroll"], hello["ssh"]["control"],
                          hello["ssh"]["control_size"], hello["ssh"]["control_zoom"]), (OWNER, 22, True, 1, 1, 1))

    def test_panes_survive_idle(self):
        pane = {"socket": "/tmp/s", "pane": "%3", "target": "", "bin": ""}
        self.air.register({"id": "A", "harness": "claude", "cwd": "/x/api", "tmux": pane})
        self.air.store.x("UPDATE agents SET last_seen=?", time.time() - 5 * 86400)  # idle for days
        db = Path(self.air.store.db.execute("PRAGMA database_list").fetchone()[2])
        self.assertEqual(keys.registered_panes(db), [{"address": "air/claude/api", "tmux": pane}])
        os.chmod(db, 0o666)
        with self.assertRaises(OSError):  # a database others can write is not trusted
            keys.registered_panes(db)

    def test_double_approve(self):
        r = self.enroll()
        self.air_keys.store.x("UPDATE key_requests SET status='approving' WHERE id=?", r["request_id"])  # another approve running
        with self.assertRaises(bus.BusError) as cm:
            self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"]})
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(self.air_keys.ls()["keys"], [])

    def test_failed_approve_leaves_request_pending(self):
        r = self.enroll()
        payload, sig = self.signed(r)
        with self.assertRaises(bus.BusError):
            self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "payload": payload.replace("Nick's iPhone", "Nick's iPhonf"), "signature": sig})
        self.assertEqual(self.air_keys.status(PHONE, r["request_id"])["status"], "pending")

    def test_only_owner_machines_get_approvals_and_revocations(self):
        self.air.net.peers["pro"]["user"] = "eve@example.com"  # the name now belongs to someone else's machine
        r = self.enroll()
        payload, sig = self.signed(r)
        out = self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "mode": "pane", "payload": payload, "signature": sig})
        self.assertEqual([i["host"] for i in out["installed"]], ["air"])
        self.assertIn("pro", out["errors"])
        self.assertFalse((self.tmp / "pro" / "authorized_keys").exists())
        self.air_keys.store.x("INSERT INTO key_revocations(key_id, host, created) VALUES('kaaaaaaaaaaaa','pro',?)", time.time())
        self.assertIn("offline", self.air_keys.retry_revocations()["pro"])
        self.assertEqual(self.air_keys.ls()["revoking"][0]["host"], "pro")  # still shown as not done

    def test_tagged_machine_takes_no_part(self):
        self.pro.net.user = "tagged-devices"
        with self.assertRaises(bus.BusError) as cm:
            self.pro_keys.revoke_from_peer("127.0.0.1", {"key_id": "kaaaaaaaaaaaa"})
        self.assertEqual(cm.exception.code, 403)
        self.assertEqual(self.pro_keys.targets(), ["pro"])


    # -- machine trust: directed edges, one hop, retries (docs/ssh-keys.md#machine-trust) --------------------
    def trust(self, on, of):
        """A person on `on` runs `tailshare keys trust-machine` for `of` (Keys objects)."""
        pub = of.machine_pub()
        return on.trust({"host": of.host, "pubkey": pub, "confirm": keys.fingerprint(pub)[-6:]})

    def approve_here(self, r, mode="pane", **extra):
        code = (r.get("pair_fingerprint") or r["fingerprint"])[-6:]
        return self.air_keys.approve({"confirm": code, "id": r["request_id"], "mode": mode, **extra})

    def prismo(self):
        """A third machine of the owner, which sees air at 100.64.0.1 and pro at 100.64.0.2."""
        b = make_bus("prismo", StaticNet("prismo", ipmap={"100.64.0.1": "air", "100.64.0.2": "pro"},
                                         usermap={"100.64.0.1": OWNER, "100.64.0.2": OWNER}, user=OWNER))
        b.keys = self.machine_keys(b, "prismo")
        return b.keys

    def test_a_trusted_machine_installs_with_its_own_key_and_no_mode_cap(self):
        self.trust(self.pro_keys, self.air_keys)
        r = self.enroll()
        out = self.approve_here(r, "shell")
        self.assertEqual(sorted(i["host"] for i in out["installed"]), ["air", "pro"], out)
        self.assertEqual(out["errors"], {})
        k = self.pro_keys.ls()["keys"][0]
        self.assertEqual((k["id"], k["mode"], k["approved_by"]), (out["key_id"], "shell", "trusted machine air"))
        st = self.air_keys.status(PHONE, r["request_id"])
        self.assertEqual(([i["host"] for i in st["installed"]], st["not_installed"]), (["air", "pro"], {}))
        # a start key that may skip permissions, with the squads grant, goes the same way
        start_pub, plan_pub = self.p256("start"), self.p256("plan")
        r = self.air_keys.enroll(PHONE, {"pubkey": start_pub, "plan_pubkey": plan_pub, "purpose": "start",
                                         "want": ["start", "skip"], "device_name": "iPad"})
        out = self.approve_here(r, "start-skip", squads=True)
        self.assertEqual(sorted((i["host"], i["mode"], i["squads"]) for i in out["installed"]),
                         [("air", "start-skip", True), ("pro", "start-skip", True)], out)
        self.assertEqual({k["plan_pubkey"] for k in self.pro_keys.ls()["keys"] if k["mode"] == "start-skip"}, {plan_pub})

    def test_trust_goes_one_way(self):
        self.trust(self.air_keys, self.pro_keys)  # pro -> air: air takes pro's approvals, not the other way round
        r = self.enroll()
        out = self.approve_here(r)
        self.assertEqual([i["host"] for i in out["installed"]], ["air"])
        self.assertIn("pro does not trust air", out["errors"]["pro"])
        self.assertFalse((self.tmp / "pro" / "authorized_keys").exists())
        st = self.air_keys.status(PHONE, r["request_id"])
        self.assertIn("does not trust air", st["not_installed"]["pro"])  # the app can say: approve on pro too
        self.assertEqual(self.air_keys.retry_installs(), {})  # a refusal is not asked again
        self.assertEqual(self.air_keys.ls()["installing"][0]["final"], 1)

    def test_here_keeps_it_on_this_machine_even_when_trusted(self):
        self.trust(self.pro_keys, self.air_keys)
        out = self.approve_here(self.enroll(), here=True)
        self.assertEqual(([i["host"] for i in out["installed"]], out["errors"]), (["air"], {}))
        self.assertFalse((self.tmp / "pro" / "authorized_keys").exists())

    def machine_doc(self, signer_keys, hosts, origin=None, key_id="k" + "d" * 12, sign_with=None):
        pub = keys.parse_pubkey(keypair(self.tmp, f"dev-{key_id}")[1])
        p = {"id": "f" * 32, "pubkey": pub, "fingerprint": keys.fingerprint(pub), "device": "iPad", "node": "ipad",
             "user": OWNER}
        payload = keys.approval_doc(signer_keys.host, origin or signer_keys.host, p, key_id, "pane", hosts, machine=True)
        if sign_with is None:
            return payload, signer_keys._machine_sign(payload)
        return payload, sessions.sign(payload, sign_with, sessions.KEYS_NAMESPACE)

    def test_only_direct_edges_count_never_a_relay(self):
        """air -> pro and pro -> prismo: air's approval reaches pro, not prismo, sent by air or passed on by pro."""
        prismo = self.prismo()
        self.trust(self.pro_keys, self.air_keys)
        self.trust(prismo, self.pro_keys)
        payload, sig = self.machine_doc(self.air_keys, ["air", "pro", "prismo"])
        for sender in ("100.64.0.1", "100.64.0.2"):  # from air itself, and relayed by pro
            with self.assertRaises(bus.BusError) as cm:
                prismo.install_from_peer(sender, {"payload": payload, "signature": sig})
            self.assertEqual(cm.exception.code, 403)
        self.assertEqual(prismo.ls()["keys"], [])
        payload, sig = self.machine_doc(self.pro_keys, ["prismo"], key_id="k" + "e" * 12)  # pro's own: a direct edge
        self.assertEqual(prismo.install_from_peer("100.64.0.2", {"payload": payload, "signature": sig})["host"], "prismo")
        self.assertEqual(prismo.ls()["keys"][0]["approved_by"], "trusted machine pro")

    def test_a_machine_approval_must_come_from_that_machine_and_its_key(self):
        prismo = self.prismo()
        self.trust(prismo, self.air_keys)
        cases = {
            "sent by pro": (self.machine_doc(self.air_keys, ["prismo"], key_id="k" + "1" * 12), "100.64.0.2"),
            "origin is not the signer": (self.machine_doc(self.air_keys, ["prismo"], origin="pro", key_id="k" + "2" * 12),
                                         "100.64.0.1"),
            "another key": (self.machine_doc(self.air_keys, ["prismo"], key_id="k" + "3" * 12,
                                             sign_with=self.signing_key), "100.64.0.1"),
            "not for prismo": (self.machine_doc(self.air_keys, ["pro"], key_id="k" + "4" * 12), "100.64.0.1"),
        }
        for name, ((payload, sig), sender) in cases.items():
            with self.subTest(name), self.assertRaises(bus.BusError) as cm:
                prismo.install_from_peer(sender, {"payload": payload, "signature": sig})
            self.assertEqual(cm.exception.code, 403, name)
        self.assertEqual(prismo.ls()["keys"], [])
        # a person's approval here never takes a machine-signed document either
        r = self.enroll(pub=keypair(self.tmp, "local")[1])
        p = next(x for x in self.air_keys.pending()["pending"] if x["id"] == r["request_id"])
        payload = keys.approval_doc("pro", "pro", p, "k" + "5" * 12, "pane", ["air"], machine=True)
        self.trust(self.air_keys, self.pro_keys)
        with self.assertRaises(bus.BusError) as cm:
            self.approve_here(r, payload=payload, signature=self.pro_keys._machine_sign(payload))
        self.assertIn("only accepted from that machine", str(cm.exception))
        # replay: the same machine approval works once
        payload, sig = self.machine_doc(self.air_keys, ["prismo"], key_id="k" + "6" * 12)
        prismo.install_from_peer("100.64.0.1", {"payload": payload, "signature": sig})
        with self.assertRaises(bus.BusError) as cm:
            prismo.install_from_peer("100.64.0.1", {"payload": payload, "signature": sig})
        self.assertIn("already used", str(cm.exception))

    def test_trusted_machines_file_is_for_key_approvals_only(self):
        self.trust(self.pro_keys, self.air_keys)
        text = (self.tmp / "pro" / "trusted_machines").read_text()
        self.assertEqual(text, f'air namespaces="tailshare-keys" {self.air_keys.machine_pub()}\n')
        self.assertEqual(os.stat(self.tmp / "pro" / "trusted_machines").st_mode & 0o777, 0o600)
        self.assertNotIn(self.air_keys.machine_pub().split()[1], self.signers.read_text())  # launches never trust it
        payload = sessions.canonical({"v": 1, "signer": "air", "action": "launch"})
        with self.assertRaises(bus.BusError):
            sessions.verify(payload, self.air_keys._machine_sign(payload), self.tmp / "pro" / "trusted_machines")

    def test_trust_needs_the_fingerprint_and_a_machine_key(self):
        pub, p256 = self.air_keys.machine_pub(), self.p256("p")
        bad = {"wrong code": {"host": "air", "pubkey": pub, "confirm": "xxxxxx"},
               "itself": {"host": "pro", "pubkey": pub, "confirm": keys.fingerprint(pub)[-6:]},
               "not a machine key": {"host": "air", "pubkey": p256, "confirm": keys.fingerprint(p256)[-6:]},
               "bad name": {"host": "air x", "pubkey": pub, "confirm": keys.fingerprint(pub)[-6:]}}
        for name, body in bad.items():
            with self.subTest(name), self.assertRaises(bus.BusError):
                self.pro_keys.trust(body)
        self.assertEqual(self.pro_keys.trusted_view()["trusted"], [])
        self.trust(self.pro_keys, self.air_keys)
        self.trust(self.pro_keys, self.air_keys)  # again: still one line
        self.assertEqual([t["host"] for t in self.pro_keys.trusted_view()["trusted"]], ["air"])
        self.assertEqual(self.pro_keys.untrust({"host": "air"})["removed"], True)
        self.assertEqual(self.pro_keys.untrust({"host": "air"})["removed"], False)
        out = self.approve_here(self.enroll())
        self.assertIn("pro", out["errors"])  # untrusted again: nothing installs there

    def test_trust_machine_fetches_the_key_and_sees_a_mutual_edge(self):
        m = self.air_keys.peer_machine({"host": "pro"})  # over HTTP, from pro's GET /keys/machine
        self.assertEqual((m["pubkey"], m["trusts_us"]), (self.pro_keys.machine_pub(), False))
        self.trust(self.pro_keys, self.air_keys)
        self.assertIs(self.air_keys.peer_machine({"host": "pro"})["trusts_us"], True)
        for host in ("air", "nobody"):
            with self.subTest(host), self.assertRaises(bus.BusError):
                self.air_keys.peer_machine({"host": host})
        with self.assertRaises(bus.BusError) as cm:  # only the owner's machines may ask
            self.pro_keys.machine_info("100.64.0.66")
        self.assertEqual(cm.exception.code, 403)

    def test_offline_machines_get_it_later_signed_again(self):
        self.trust(self.pro_keys, self.air_keys)
        self.air.net.peers["pro"]["online"] = False
        r = self.enroll()
        out = self.approve_here(r)
        self.assertEqual(out["errors"], {"pro": "offline: will retry"})
        self.assertEqual(self.air_keys.status(PHONE, r["request_id"])["not_installed"], {"pro": "offline: will retry"})
        self.assertEqual(self.air_keys.retry_installs(), {})  # still offline: nothing sent
        self.air.net.peers["pro"]["online"] = True
        self.assertEqual(self.air_keys.retry_installs(), {"pro": "installed"})
        self.assertEqual(self.pro_keys.ls()["keys"][0]["id"], out["key_id"])
        st = self.air_keys.status(PHONE, r["request_id"])
        self.assertEqual(([i["host"] for i in st["installed"]], st["not_installed"]), (["air", "pro"], {}))
        self.assertEqual(self.air_keys.retry_installs(), {})
        self.assertEqual(self.air_keys.ls()["installing"], [])

    def test_a_person_signed_approval_is_retried_with_the_machine_key(self):
        self.trust(self.pro_keys, self.air_keys)
        self.air.net.peers["pro"]["online"] = False
        r = self.enroll()
        payload, sig = self.signed(r)
        out = self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "mode": "pane",
                                     "payload": payload, "signature": sig})
        self.assertEqual(out["errors"], {"pro": "offline: will retry"})
        self.air.net.peers["pro"]["online"] = True
        self.assertEqual(self.air_keys.retry_installs(), {"pro": "installed"})
        self.assertEqual(self.pro_keys.ls()["keys"][0]["approved_by"], "trusted machine air")

    def test_retries_stop_when_removed_or_too_old(self):
        self.trust(self.pro_keys, self.air_keys)
        self.air.net.peers["pro"]["online"] = False
        a = self.approve_here(self.enroll())
        b = self.approve_here(self.enroll(pub=keypair(self.tmp, "second")[1]))
        self.air_keys.rm({"key_id": a["key_id"]})
        self.assertEqual([i["key_id"] for i in self.air_keys.ls()["installing"]], [b["key_id"]])  # gone at once
        self.air.store.x("UPDATE key_installs SET created=? WHERE key_id=?", time.time() - keys.INSTALL_RETRY_FOR - 1,
                         b["key_id"])
        self.air.net.peers["pro"]["online"] = True
        self.assertEqual(self.air_keys.retry_installs(), {})
        self.assertFalse((self.tmp / "pro" / "authorized_keys").exists())
        left = self.air_keys.ls()["installing"]
        self.assertEqual([(i["key_id"], i["final"]) for i in left], [(b["key_id"], 1)])
        self.assertIn("gave up", left[0]["error"])


class Token(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "keys.token"

    def test_made_once_and_kept(self):
        t = keys.load_token(self.path, create=True)
        self.assertRegex(t, r"^[0-9a-f]{64}$")
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(keys.load_token(self.path, create=True), t)
        self.assertEqual(keys.load_token(self.path), t)
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["keys.token"])  # no temp files left

    def test_empty_or_short_file_never_opens_the_api(self):
        for junk in ("", "abc\n", "x" * 64):
            self.path.write_text(junk)
            os.chmod(self.path, 0o600)
            with self.assertRaises(ValueError):  # the CLI refuses
                keys.load_token(self.path)
            t = keys.load_token(self.path, create=True)  # the mailbox replaces it
            self.assertRegex(t, r"^[0-9a-f]{64}$")
            self.path.unlink()

    def test_empty_token_refuses_requests_without_header(self):
        b = make_bus("air")
        k = keys.Keys(b, Path("/nonexistent"), Path(tempfile.mkdtemp()) / "ak", self.path)
        k.token = ""  # as if the file had been empty
        with self.assertRaises(bus.BusError) as cm:
            k.route("POST", "/keys/approve", {"id": "x", "mode": "shell"}, True, "127.0.0.1", "")
        self.assertEqual(cm.exception.code, 403)

    def test_group_readable_file_is_refused(self):
        keys.load_token(self.path, create=True)
        os.chmod(self.path, 0o640)
        with self.assertRaises(OSError):
            keys.load_token(self.path)


class GateChecks(unittest.TestCase):
    def test_trusted_file_and_socket(self):
        import socket as so

        tmp = Path(tempfile.mkdtemp())
        exe = tmp / "tmux"
        exe.write_text("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        self.assertTrue(keys.trusted_file(str(exe)))
        os.chmod(exe, 0o777)
        self.assertFalse(keys.trusted_file(str(exe)))
        self.assertEqual(keys.tmux_binary(str(exe)), keys.tmux_binary(""))  # falls back to a trusted tmux
        sock_path = Path(tempfile.mkdtemp(dir="/tmp")) / "s"
        s = so.socket(so.AF_UNIX)
        s.bind(str(sock_path))
        try:
            self.assertTrue(keys.own_socket(str(sock_path)))
            self.assertFalse(keys.own_socket(str(exe)))
            self.assertFalse(keys.own_socket(str(tmp / "missing")))
        finally:
            s.close()


@unittest.skipUnless(keys.tmux_binary(""), "needs tmux")
class GateEndToEnd(unittest.TestCase):
    """cmd_gate against a real tmux server and a bus database, stopping at the exec."""

    def setUp(self):
        self.tmux = keys.tmux_binary("")
        self.state = Path(tempfile.mkdtemp())
        self.sock = str(Path(tempfile.mkdtemp(dir="/tmp")) / "s")  # short: sun_path is ~104 bytes
        subprocess.run([self.tmux, "-S", self.sock, "new-session", "-d", "sleep 60"], check=True)
        self.pane = subprocess.run([self.tmux, "-S", self.sock, "display", "-p", "#{pane_id}"],
                                   capture_output=True, text=True, check=True).stdout.strip()
        b = bus.Bus(bus.Store(self.state / "bus.db"), StaticNet("air"), free_port())
        b.register({"id": "A", "harness": "claude", "cwd": "/x/api",
                    "tmux": {"socket": self.sock, "pane": self.pane, "target": "", "bin": self.tmux}})
        self.old = bus.STATE_DIR, os.execv, os.environ.get("SSH_ORIGINAL_COMMAND")
        bus.STATE_DIR = self.state
        self.execs = []
        os.execv = lambda path, argv: self.execs.append(argv)

    def tearDown(self):
        bus.STATE_DIR, os.execv = self.old[0], self.old[1]
        if self.old[2] is None:
            os.environ.pop("SSH_ORIGINAL_COMMAND", None)
        else:
            os.environ["SSH_ORIGINAL_COMMAND"] = self.old[2]
        subprocess.run([self.tmux, "-S", self.sock, "kill-server"], capture_output=True)

    def gate(self, cmd):
        os.environ["SSH_ORIGINAL_COMMAND"] = cmd
        keys.cmd_gate(None)

    def test_hello_says_whether_the_pane_is_alive(self):
        b = bus.Bus(bus.Store(self.state / "bus.db"), StaticNet("air"), free_port())
        self.assertTrue(b.hello()["agents"][0]["tmux"]["alive"])
        subprocess.run([self.tmux, "-S", self.sock, "kill-server"], check=True)
        b._alive.clear()
        self.assertFalse(b.hello()["agents"][0]["tmux"]["alive"])

    def test_attaches_to_the_registered_pane(self):
        self.gate(f"tmux -S {self.sock} attach -r -t {self.pane}")
        self.assertEqual(self.execs, [[self.tmux, "-S", self.sock, "attach", "-r", "-t", self.pane]])

    def test_a_pane_is_its_socket_and_id(self):
        """Every server numbers panes from %0: a live, own %N on another agent's server is not this agent's."""
        other = str(Path(tempfile.mkdtemp(dir="/tmp")) / "s")
        self.addCleanup(subprocess.run, [self.tmux, "-S", other, "kill-server"], capture_output=True)
        subprocess.run([self.tmux, "-S", other, "new-session", "-d", "sleep 60"], check=True)
        twin = subprocess.run([self.tmux, "-S", other, "display", "-p", "#{pane_id}"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(twin, self.pane)
        self.assertTrue(keys.own_socket(other))
        for cmd in (f"tmux -S {other} attach -t {twin}", f"tmux -S {other} -C attach -r -t {twin}"):
            with self.assertRaises(SystemExit, msg=cmd):
                self.gate(cmd)
        self.assertEqual(self.execs, [])
        self.gate(f"tmux -S {self.sock} attach -t {self.pane}")
        self.assertEqual(len(self.execs), 1)

    def test_control_mode_goes_through_the_filter(self):
        with mock.patch("tailshare.gate_control.run", return_value=0) as run:
            with self.assertRaises(SystemExit) as cm:
                self.gate(f"{self.tmux} -S {self.sock} -C attach -r -f ignore-size -t {self.pane}")
        self.assertEqual(cm.exception.code, 0)
        req = run.call_args.args[0]
        self.assertEqual((req["cc"], req["readonly"], req["pane"]), ("-C", True, self.pane))
        for cmd in (f"tmux -CC -S {self.sock} attach -t {self.pane}",  # the same protocol, unfiltered
                    f"tmux -C -S {self.sock} attach -t {self.pane} ; select-pane -t {self.pane}"):
            with self.assertRaises(SystemExit, msg=cmd) as cm:
                self.gate(cmd)
            self.assertIsInstance(cm.exception.code, str)
        self.assertEqual(self.execs, [])

    def test_refuses(self):
        for cmd in ("", "id", f"tmux -S {self.sock} attach -t %999", f"tmux -S {self.sock} attach -t {self.pane} ; run-shell id",
                    f"tmux -S /tmp/elsewhere attach -t {self.pane}"):
            with self.assertRaises(SystemExit, msg=cmd):
                self.gate(cmd)
        subprocess.run([self.tmux, "-S", self.sock, "kill-server"], check=True)
        with self.assertRaises(SystemExit):  # registered, but the pane is gone
            self.gate(f"tmux -S {self.sock} attach -t {self.pane}")
        self.assertEqual(self.execs, [])



class GateJustStarted(unittest.TestCase):
    """A pane `tailshare start` launched may be attached to before its agent registers: only on a ts-* socket of this
    user, only the exact pane the confirm's result recorded, only while it lives, and only for LAUNCHED_ATTACH."""

    def setUp(self):
        self.tmux = keys.tmux_binary("")
        self.state = Path(tempfile.mkdtemp())
        self.env = {"HOME": str(self.state), "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin", "TERM": "xterm"}
        self.dir = Path(tempfile.mkdtemp(dir="/tmp"))  # short: sun_path is ~104 bytes
        self.sock, self.plain = str(self.dir / "ts-new"), str(self.dir / "plain")
        self.panes = {}
        for sock in (self.sock, self.plain):
            subprocess.run([self.tmux, "-S", sock, "new-session", "-d", "-s", "tailshare-start", "sleep 60"],
                           check=True, env=self.env)
            self.addCleanup(subprocess.run, [self.tmux, "-S", sock, "kill-server"], capture_output=True, env=self.env)
            self.panes[sock] = self.tmux_out(sock, "display", "-p", "#{pane_id}")
        self.pane = self.panes[self.sock]
        self.other = self.tmux_out(self.sock, "split-window", "-d", "-P", "-F", "#{pane_id}", "sleep 60")  # same server
        bus.Store(self.state / "bus.db")  # a bus database with no agents at all
        self.old = bus.STATE_DIR, os.execv, os.environ.get("SSH_ORIGINAL_COMMAND")
        bus.STATE_DIR = self.state
        self.execs = []
        os.execv = lambda path, argv: self.execs.append(argv)
        self.con = start._db(self.state / "start.db")
        self.addCleanup(self.con.close)

    def tearDown(self):
        bus.STATE_DIR, os.execv = self.old[0], self.old[1]
        if self.old[2] is None:
            os.environ.pop("SSH_ORIGINAL_COMMAND", None)
        else:
            os.environ["SSH_ORIGINAL_COMMAND"] = self.old[2]

    def tmux_out(self, sock, *args):
        return subprocess.run([self.tmux, "-S", sock, *args], capture_output=True, text=True, check=True,
                              env=self.env).stdout.strip()

    def plan(self, sock=None, pane=None, ago=5, launched=True, result=None):
        pid = secrets.token_hex(16)
        now = time.time()
        res = result if result is not None else {"session": "s", "tmux": {"socket": sock or self.sock,
                                                                           "pane": pane or self.pane, "bin": self.tmux}}
        self.con.execute("INSERT INTO plans(id, key, item, plan, created, expires, used, result, approved, launched)"
                         " VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, "k" + "a" * 12, "{}", "{}", now - ago - 1, now + 60,
                                                         now - ago, json.dumps(res), now - ago,
                                                         now - ago if launched else None))
        self.con.commit()

    def gate(self, sock=None, pane=None):
        os.environ["SSH_ORIGINAL_COMMAND"] = f"tmux -S {sock or self.sock} attach -t {pane or self.pane}"
        keys.cmd_gate(None)

    def refused(self, **kw):
        with self.assertRaises(SystemExit):
            self.gate(**kw)
        self.assertEqual(self.execs, [])

    def test_attaches_before_the_agent_registers(self):
        self.plan()
        self.gate()
        self.assertEqual(self.execs, [[self.tmux, "-S", self.sock, "attach", "-t", self.pane]])

    def test_nothing_launched_nothing_attached(self):
        self.refused()

    def test_an_old_plan(self):
        self.plan(ago=keys.LAUNCHED_ATTACH + 5)
        self.refused()

    def test_a_socket_that_is_not_ts(self):
        self.plan(sock=self.plain, pane=self.panes[self.plain])
        self.refused(sock=self.plain, pane=self.panes[self.plain])

    def test_a_dead_pane(self):
        self.plan()
        subprocess.run([self.tmux, "-S", self.sock, "kill-server"], check=True, env=self.env)
        self.refused()

    def test_another_pane_than_the_result(self):
        self.plan()
        self.refused(pane=self.other)  # alive, own, ts-*, same server: still not the one launched
        self.refused(sock=self.plain)  # the same %N on another server

    def test_a_plan_never_launched(self):
        self.plan(launched=False)
        self.refused()

    def test_a_failed_start(self):
        self.plan(result={"error": "failed", "tmux": {"socket": self.sock, "pane": self.pane}})
        self.refused()

    def test_a_start_db_others_may_write(self):
        self.plan()
        os.chmod(self.state / "start.db", 0o666)
        self.refused()
        with self.assertRaises(OSError):
            keys.launched_panes(self.state / "start.db")

    def test_control_mode_still_filtered(self):
        self.plan()
        with mock.patch("tailshare.gate_control.run", return_value=0) as run, self.assertRaises(SystemExit):
            os.environ["SSH_ORIGINAL_COMMAND"] = f"tmux -S {self.sock} -C attach -r -t {self.pane}"
            keys.cmd_gate(None)
        self.assertEqual(run.call_args.args[0]["pane"], self.pane)
        os.environ["SSH_ORIGINAL_COMMAND"] = f"tmux -CC -S {self.sock} attach -t {self.pane}"
        with self.assertRaises(SystemExit):
            keys.cmd_gate(None)
        self.assertEqual(self.execs, [])


class Requested(unittest.TestCase):
    """`keys pending` and `keys approve` say when, from which address and by whom a request came."""

    def test_requested_line(self):
        t = time.mktime((2026, 11, 3, 17, 5, 0, 0, 0, -1))
        self.assertEqual(keys._requested({"created": t, "ip": "100.64.0.9", "user": "nick@example.com"}),
                         "2026-11-03 17:05 from 100.64.0.9, by nick@example.com")
        self.assertEqual(keys._requested({}), "at an unknown time from an unknown address, by an unknown tailnet user")


if __name__ == "__main__":
    unittest.main()
