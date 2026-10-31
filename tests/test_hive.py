"""`tailshare hive pair`: the pairing link and the single-use token.

Run with: uv run python -m unittest discover tests
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import unittest
from unittest import mock

from tailshare import bus, hive
from tests.test_bus import StaticNet, make_bus

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDv5x1JYYiDZJcnnbjmhGVZ5GUc8HQs1i6HsYb8K0e2k"
PHONE = "100.64.0.9"


def decode(link: str) -> dict:
    b64 = link.removeprefix(hive.SCHEME)
    return json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))


class Link(unittest.TestCase):
    def payload(self, **net):
        n = bus.Net(host="air", ip="100.64.0.1", user="nick@example.com", **net)
        with mock.patch.object(hive.keys, "host_keys", return_value=[ED + " root@air"]):
            return hive.payload(n, "t" * 22, 1600)

    def test_fields(self):
        link = hive.pair_link(self.payload())
        self.assertTrue(link.startswith("tailsharehive://pair/"))
        self.assertNotIn("=", link)
        want = base64.b64encode(hashlib.sha256(base64.b64decode(ED.split()[1])).digest()).decode().rstrip("=")
        self.assertEqual(decode(link), {"v": 1, "ip": "100.64.0.1", "port": bus.BUS_PORT, "host": "air",
                                        "owner": "nick@example.com", "hk": ["SHA256:" + want], "t": "t" * 22,
                                        "exp": 1600})

    def test_tailscale_ssh_uses_tailscaled_keys(self):
        self.assertEqual(self.payload(tailscale_ssh=True, ssh_host_keys=[])["hk"], [])  # not /etc/ssh's

    def test_fits_a_terminal_qr(self):
        qr = hive.qr_text(hive.pair_link(self.payload())).splitlines()
        self.assertLess(max(len(l) for l in qr), 80)


class Token(unittest.TestCase):
    def setUp(self):
        self.b = make_bus("air", StaticNet("air", ipmap={PHONE: "iphone"}, usermap={PHONE: "nick@example.com"}))

    def test_single_use_and_status(self):
        self.b.add_pair_token("t" * 22, time.time() + 60)
        self.assertEqual(self.b.pair_status("t" * 22), {"state": "waiting"})
        r = self.b.use_pair_token(PHONE, {"t": "t" * 22, "device_name": "Nick's iPhone"})
        self.assertEqual(r, {"paired": True, "you": {"user": "nick@example.com", "node": "iphone"},
                             "owner": "nick@example.com"})
        self.assertEqual(self.b.pair_status("t" * 22), {"state": "paired", "device": "Nick's iPhone", "ip": PHONE,
                                                        "user": "nick@example.com", "node": "iphone"})
        for t, code in (("t" * 22, 410), ("u" * 22, 404)):
            with self.assertRaises(bus.BusError) as cm:
                self.b.use_pair_token(PHONE, {"t": t})
            self.assertEqual(cm.exception.code, code)

    def test_expired(self):
        self.b.add_pair_token("v" * 22, time.time() + 60)
        self.b._pair_tokens["v" * 22]["exp"] = time.time() - 1
        self.assertEqual(self.b.pair_status("v" * 22), {"state": "expired"})
        with self.assertRaises(bus.BusError) as cm:
            self.b.use_pair_token(PHONE, {"t": "v" * 22})
        self.assertEqual(cm.exception.code, 410)

    def test_pubkey_enrolls(self):
        self.b.keys = mock.Mock()
        self.b.keys.enroll.return_value = {"request_id": "r" * 32, "status": "pending"}
        self.b.add_pair_token("t" * 22, time.time() + 60)
        r = self.b.use_pair_token(PHONE, {"t": "t" * 22, "device_name": "p", "pubkey": ED})
        self.b.keys.enroll.assert_called_once_with(PHONE, {"pubkey": ED, "device_name": "p"})
        self.assertEqual(r["enroll"]["status"], "pending")
        self.assertEqual(self.b.pair_status("t" * 22)["request_id"], "r" * 32)
        self.b.add_pair_token("w" * 22, time.time() + 60)
        self.b.keys.enroll.side_effect = bus.BusError("tailshare runs as root here", 409)
        r = self.b.use_pair_token(PHONE, {"t": "w" * 22, "pubkey": ED})  # pairing still works
        self.assertEqual((r["paired"], r["enroll"]["code"]), (True, 409))

    def test_bad_input(self):
        for t, exp in (("short", 60), ("t" * 22, 7200), ("t" * 22, -1)):
            with self.assertRaises(bus.BusError):
                self.b.add_pair_token(t, time.time() + exp)


if __name__ == "__main__":
    unittest.main()
