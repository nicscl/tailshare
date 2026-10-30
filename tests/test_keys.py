"""SSH key enrollment: key parsing, the authorized_keys block, the gate, and approve/fan-out/revoke.

Run with: uv run python -m unittest discover tests
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tailshare import bus, keys, sessions
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
        self.assertEqual(keys.key_line(k, "/u/bin/tailshare"),
                         'restrict,pty,command="/u/bin/tailshare ssh-gate" ssh-ed25519 AAAA tailshare:k0123456789ab Nick\'s-iPhone')
        self.assertEqual(keys.key_line({**k, "mode": "shell"}, "/x"), "ssh-ed25519 AAAA tailshare:k0123456789ab Nick's-iPhone")


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
        self.air_keys = keys.Keys(self.air, self.signers, self.tmp / "air" / "authorized_keys")
        self.pro_keys = keys.Keys(self.pro, self.signers, self.tmp / "pro" / "authorized_keys")
        self.air.keys, self.pro.keys = self.air_keys, self.pro_keys
        self.air.store.x("INSERT INTO peers(host, ip, version, last_ok) VALUES('pro','127.0.0.1','t',?)", time.time())
        _, self.phone_pub = keypair(self.tmp, "phone")
        self.srv = self.pro.listen("127.0.0.1", local=False)

    def tearDown(self):
        keys.install_problem, keys.gate_exe, keys.Keys._notify = self._problem, self._exe, self._notify
        self.srv.shutdown()
        self.srv.server_close()

    def enroll(self, ip=PHONE, pub=None, device="Nick's iPhone"):
        return self.air_keys.enroll(ip, {"pubkey": pub or self.phone_pub, "device_name": device})

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
        out = self.air_keys.approve({"id": r["request_id"], "mode": "pane"})
        self.assertEqual([i["host"] for i in out["installed"]], ["air"])
        text = (self.tmp / "air" / "authorized_keys").read_text()
        self.assertIn(f'restrict,pty,command="/u/bin/tailshare ssh-gate" {keys.parse_pubkey(self.phone_pub)} '
                      f"tailshare:{out['key_id']} Nick's-iPhone", text)
        self.assertFalse((self.tmp / "pro" / "authorized_keys").exists())
        st = self.air_keys.status(PHONE, r["request_id"])
        self.assertEqual((st["status"], st["key_id"]), ("approved", out["key_id"]))
        with self.assertRaises(bus.BusError):
            self.air_keys.approve({"id": r["request_id"]})  # not pending any more

    def signed(self, req, mode="pane", hosts=None):
        p = self.air_keys.pending()["pending"][0]
        self.assertEqual(p["id"], req["request_id"])
        payload = keys.approval_doc("nick", "air", p, "k" + "a" * 12, mode, hosts or ["air", "pro"])
        return payload, sessions.sign(payload, self.signing_key, sessions.KEYS_NAMESPACE)

    def test_signed_fan_out_replay_and_revoke(self):
        r = self.enroll()
        payload, sig = self.signed(r)
        out = self.air_keys.approve({"id": r["request_id"], "mode": "pane", "payload": payload, "signature": sig})
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

    def test_launch_signature_is_not_a_key_approval(self):
        r = self.enroll()
        payload, _ = self.signed(r)
        wrong_ns = sessions.sign(payload, self.signing_key)  # tailshare-launch namespace
        with self.assertRaises(bus.BusError) as cm:
            self.air_keys.approve({"id": r["request_id"], "payload": payload, "signature": wrong_ns})
        self.assertEqual(cm.exception.code, 403)
        self.assertFalse((self.tmp / "air" / "authorized_keys").exists())

    def test_signature_must_match_request(self):
        r = self.enroll()
        payload, sig = self.signed(r, mode="shell")
        with self.assertRaises(bus.BusError):
            self.air_keys.approve({"id": r["request_id"], "mode": "pane", "payload": payload, "signature": sig})

    def test_peer_routes_need_same_user(self):
        eve = make_bus("eve", StaticNet("eve", usermap={"127.0.0.1": "eve@example.com"}, user=OWNER))
        eve.keys = keys.Keys(eve, self.signers, self.tmp / "eve" / "authorized_keys")
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
        for path in ("/keys/pending", "/keys/ls", "/keys/panes"):  # local-only routes are not on the peer API
            with self.assertRaises(bus.BusError) as cm:
                bus.request(url + path)
            self.assertEqual(cm.exception.code, 404, path)
        hello = bus.request(f"{url}/hello")
        self.assertEqual((hello["ssh"]["owner"], hello["ssh"]["port"], hello["ssh"]["enroll"]), (OWNER, 22, True))

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
            self.air_keys.approve({"id": r["request_id"]})
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(self.air_keys.ls()["keys"], [])

    def test_failed_approve_leaves_request_pending(self):
        r = self.enroll()
        payload, sig = self.signed(r)
        with self.assertRaises(bus.BusError):
            self.air_keys.approve({"id": r["request_id"], "payload": payload.replace("Nick's iPhone", "Nick's iPhonf"), "signature": sig})
        self.assertEqual(self.air_keys.status(PHONE, r["request_id"])["status"], "pending")

    def test_only_owner_machines_get_approvals_and_revocations(self):
        self.air.net.peers["pro"]["user"] = "eve@example.com"  # the name now belongs to someone else's machine
        r = self.enroll()
        payload, sig = self.signed(r)
        out = self.air_keys.approve({"id": r["request_id"], "mode": "pane", "payload": payload, "signature": sig})
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


if __name__ == "__main__":
    unittest.main()
