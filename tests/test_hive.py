"""`tailshare hive pair`: the pairing link.

Run with: uv run python -m unittest discover tests
"""

from __future__ import annotations

import base64
import hashlib
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from tailshare import bus, hive

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDv5x1JYYiDZJcnnbjmhGVZ5GUc8HQs1i6HsYb8K0e2k"


class Pair(unittest.TestCase):
    def link(self, **net):
        n = bus.Net(host="air", ip="100.64.0.1", user="nick@example.com", **net)
        with mock.patch.object(hive.keys, "host_keys", return_value=[ED + " root@air"]):
            return hive.pair_link(n, "0.3.1+abc", "ABC234", "t" * 22, now=1000, ttl=600)

    def test_fields(self):
        u = urlparse(self.link())
        self.assertEqual(f"{u.scheme}://{u.netloc}", hive.SCHEME)
        q = parse_qs(u.query, strict_parsing=True)
        self.assertEqual({k: q[k][0] for k in ("v", "host", "ip", "port", "owner", "ver", "exp", "code", "token")},
                         {"v": "1", "host": "air", "ip": "100.64.0.1", "port": str(bus.BUS_PORT),
                          "owner": "nick@example.com", "ver": "0.3.1+abc", "exp": "1600", "code": "ABC234",
                          "token": "t" * 22})
        want = base64.b64encode(hashlib.sha256(base64.b64decode(ED.split()[1])).digest()).decode().rstrip("=")
        self.assertEqual(q["hk"], ["SHA256:" + want])  # as `ssh-keygen -l` prints it

    def test_tailscale_ssh_uses_tailscaled_keys(self):
        q = parse_qs(urlparse(self.link(tailscale_ssh=True, ssh_host_keys=[])).query)
        self.assertNotIn("hk", q)  # no keys known: none, not /etc/ssh's

    def test_one_time_token(self):
        from tests.test_bus import make_bus

        b = make_bus("air")
        b.add_pair_token("t" * 22, "ABC234", time.time() + 60)
        self.assertEqual(b.use_pair_token("t" * 22), {"host": "air", "code": "ABC234"})
        for token, code in (("t" * 22, 410), ("u" * 22, 404)):
            with self.assertRaises(bus.BusError) as cm:
                b.use_pair_token(token)
            self.assertEqual(cm.exception.code, code)
        b.add_pair_token("v" * 22, "ABC234", time.time() + 60)
        b._pair_tokens["v" * 22]["exp"] = time.time() - 1
        with self.assertRaises(bus.BusError) as cm:
            b.use_pair_token("v" * 22)
        self.assertEqual(cm.exception.code, 410)
        for bad in (("short", "ABC234", 60), ("t" * 22, "abc", 60), ("t" * 22, "ABC234", 7200)):
            with self.assertRaises(bus.BusError):
                b.add_pair_token(bad[0], bad[1], time.time() + bad[2])

    def test_codes(self):
        c = hive.new_code()
        self.assertRegex(c, r"^[A-Z2-9]{6}$")
        self.assertFalse(set(c) & set("01OIL"))

    def test_fits_a_terminal_qr(self):
        self.assertLess(max(len(l) for l in hive.qr_text(self.link()).splitlines()), 80)


if __name__ == "__main__":
    unittest.main()
