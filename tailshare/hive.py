"""`tailshare hive pair`: a QR code that makes this machine the Tailshare Hive app's first trusted machine.

iOS apps cannot run `tailscale status`, so the app starts from one machine the person points it at by
scanning its screen. From there it reads /hello (full host keys, checked against the fingerprints here)
and /peers (the rest of the tailnet).

The link carries no secret and is not signed: the person scans their own terminal, which is what makes it
trustworthy. It expires, so an old screenshot cannot be replayed later.
"""

from __future__ import annotations

import io
import sys
import time
from urllib.parse import urlencode

from . import bus, keys

FORMAT = 1
SCHEME = "tailsharehive://pair"
TTL = 600  # seconds a printed code stays valid


def host_keys(net: bus.Net) -> list[str]:
    """The host keys that answer SSH on port 22 here: tailscaled's with Tailscale SSH, else /etc/ssh's."""
    return list(net.ssh_host_keys) if net.tailscale_ssh else keys.host_keys()


def pair_link(net: bus.Net, version: str, now: float | None = None, ttl: int = TTL) -> str:
    # SHA256 fingerprints as unpadded base64url: `+` would read as a space in a query string
    fps = [keys.fingerprint(" ".join(k.split()[:2])).removeprefix("SHA256:").replace("+", "-").replace("/", "_")
           for k in host_keys(net)]
    q = {"v": FORMAT, "host": net.host, "ip": net.ip, "port": bus.BUS_PORT, "owner": net.user,
         "k": ",".join(fps), "ver": version, "exp": int((now or time.time()) + ttl)}
    return f"{SCHEME}?{urlencode(q, safe='@:,')}"


def qr_text(data: str) -> str:
    import qrcode

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue().rstrip("\n")


def cmd_pair(args) -> None:
    from . import agent
    from .update import describe

    net = bus.Net()
    net.refresh()
    if not net.ip:
        sys.exit("✗ tailscale is not running here, or this machine is not on a tailnet")
    try:
        agent.call("/machines", timeout=3)
    except (agent.Offline, bus.BusError, OSError):
        print("⚠ the tailshare mailbox is not running here, so the app will not be able to reach this machine. "
              "Start it with `tailshare install`.", file=sys.stderr)
    link = pair_link(net, describe(), ttl=args.ttl)
    if args.link:
        print(link)
        return
    print(qr_text(link))
    print(f"\nScan with Tailshare Hive to add {net.host} ({net.ip}, {net.user}).")
    print(f"Valid for {args.ttl // 60} minutes. Only scan codes from your own screen.")
    print(f"\n{link}")


def add_parser(sub) -> None:
    hp = sub.add_parser("hive", help="the Tailshare Hive iOS app")
    s = hp.add_subparsers(dest="hive_cmd", required=True)
    p = s.add_parser("pair", help="show a QR code that adds this machine to the app")
    p.add_argument("--ttl", type=int, default=TTL, help=f"seconds the code stays valid (default {TTL})")
    p.add_argument("--link", action="store_true", help="print only the link")
    p.set_defaults(fn=cmd_pair)
