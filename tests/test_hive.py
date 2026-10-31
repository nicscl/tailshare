"""`tailshare hive pair`: the pairing link.

Run with: uv run python -m unittest discover tests
"""

from __future__ import annotations

import base64
import hashlib
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlparse

from tailshare import bus, hive

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDv5x1JYYiDZJcnnbjmhGVZ5GUc8HQs1i6HsYb8K0e2k"


class Pair(unittest.TestCase):
    def link(self, **net):
        n = bus.Net(host="air", ip="100.64.0.1", user="nick@example.com", **net)
        with mock.patch.object(hive.keys, "host_keys", return_value=[ED + " root@air"]):
            return hive.pair_link(n, "0.3.1+abc", now=1000, ttl=600)

    def test_fields(self):
        u = urlparse(self.link())
        self.assertEqual(f"{u.scheme}://{u.netloc}", hive.SCHEME)
        q = {k: v[0] for k, v in parse_qs(u.query, strict_parsing=True).items()}
        self.assertEqual({k: q[k] for k in ("v", "host", "ip", "port", "owner", "ver", "exp")},
                         {"v": "1", "host": "air", "ip": "100.64.0.1", "port": str(bus.BUS_PORT),
                          "owner": "nick@example.com", "ver": "0.3.1+abc", "exp": "1600"})
        want = base64.urlsafe_b64encode(hashlib.sha256(base64.b64decode(ED.split()[1])).digest()).decode().rstrip("=")
        self.assertEqual(q["k"], want)  # same as `ssh-keygen -l`, in base64url

    def test_tailscale_ssh_uses_tailscaled_keys(self):
        q = parse_qs(urlparse(self.link(tailscale_ssh=True, ssh_host_keys=[])).query)
        self.assertNotIn("k", q)  # no keys known: empty, not /etc/ssh's

    def test_fits_a_terminal_qr(self):
        self.assertLess(max(len(l) for l in hive.qr_text(self.link()).splitlines()), 80)


if __name__ == "__main__":
    unittest.main()
