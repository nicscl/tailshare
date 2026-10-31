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
from unittest import mock
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
        self.air_keys = keys.Keys(self.air, self.signers, self.tmp / "air" / "authorized_keys", self.tmp / "air.token")
        self.pro_keys = keys.Keys(self.pro, self.signers, self.tmp / "pro" / "authorized_keys", self.tmp / "pro.token")
        self.air.keys, self.pro.keys = self.air_keys, self.pro_keys
        self.air.store.x("INSERT INTO peers(host, ip, version, last_ok) VALUES('pro','127.0.0.1','t',?)", time.time())
        _, self.phone_pub = keypair(self.tmp, "phone")
        self.srv = self.pro.listen("127.0.0.1", local=False)

    def tearDown(self):
        keys.install_problem, keys.gate_exe, keys.Keys._notify = self._problem, self._exe, self._notify
        keys.REQUIRE_SK = self._sk
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
        out = self.air_keys.approve({"confirm": r["fingerprint"][-6:], "id": r["request_id"], "mode": "pane"})
        self.assertEqual([i["host"] for i in out["installed"]], ["air"])
        text = (self.tmp / "air" / "authorized_keys").read_text()
        self.assertIn(f'restrict,pty,command="/u/bin/tailshare ssh-gate" {keys.parse_pubkey(self.phone_pub)} '
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
        eve.keys = keys.Keys(eve, self.signers, self.tmp / "eve" / "authorized_keys", self.tmp / "eve.token")
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
        self.assertEqual((hello["ssh"]["owner"], hello["ssh"]["port"], hello["ssh"]["enroll"], hello["ssh"]["control"]), (OWNER, 22, True, 1))

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

    def test_control_mode_goes_through_the_filter(self):
        with mock.patch("tailshare.gate_control.run", return_value=0) as run, \
                mock.patch("os._exit", side_effect=SystemExit) as exit_:
            with self.assertRaises(SystemExit):
                self.gate(f"{self.tmux} -S {self.sock} -C attach -r -f ignore-size -t {self.pane}")
        exit_.assert_called_once_with(0)
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


if __name__ == "__main__":
    unittest.main()
