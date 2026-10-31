"""`tailshare hive pair`: a QR code that makes this machine the Tailshare Hive app's first trusted machine.

iOS apps cannot run `tailscale status`, so the app starts from one machine the person points it at by
scanning its screen. From there it reads /hello (full host keys, checked against the fingerprints here)
and /peers (the rest of the tailnet).

The link is not signed: the person scans their own terminal, which is what makes it trustworthy, and checks
that the code the app shows matches the one printed here. It expires within 10 minutes, and its one-time
token (POST /bus/v1/hive/pair) lets the app refuse a code that was already used.
"""

from __future__ import annotations

import io
import secrets
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


ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(6))


def pair_link(net: bus.Net, version: str, code: str, token: str = "", now: float | None = None,
              ttl: int = TTL) -> str:
    hks = [keys.fingerprint(" ".join(k.split()[:2])) for k in host_keys(net)]  # `ssh-keygen -l` form
    q = [("v", FORMAT), ("ip", net.ip), ("port", bus.BUS_PORT), ("host", net.host), ("owner", net.user),
         *[("hk", h) for h in hks], ("exp", int((now or time.time()) + ttl)), ("code", code), ("ver", version)]
    if token:
        q.append(("token", token))
    return f"{SCHEME}?{urlencode(q, safe='@:')}"


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
    if not 60 <= args.ttl <= TTL:
        sys.exit(f"✗ --ttl must be between 60 and {TTL} seconds")
    code, token = new_code(), secrets.token_urlsafe(16)
    try:
        agent.call("/hive/token", {"token": token, "code": code, "exp": time.time() + args.ttl}, timeout=3)
    except (agent.Offline, bus.BusError, OSError) as e:
        sys.exit(f"✗ the tailshare mailbox is not answering here ({e}), so the app could not reach this machine. "
                 "Start it with `tailshare install`.")
    link = pair_link(net, describe(), code, token, ttl=args.ttl)
    if args.link:
        print(link)
        return
    print(qr_text(link))
    print(f"\nScan with Tailshare Hive to add {net.host} ({net.ip}, {net.user}).")
    print(f"The app will show the code {code}: it must match.")
    print(f"Valid for {args.ttl // 60} minutes. Only scan codes from your own screen.")
    print(f"\n{link}")


def add_parser(sub) -> None:
    hp = sub.add_parser("hive", help="the Tailshare Hive iOS app")
    s = hp.add_subparsers(dest="hive_cmd", required=True)
    p = s.add_parser("pair", help="show a QR code that adds this machine to the app")
    p.add_argument("--ttl", type=int, default=TTL, help=f"seconds the code stays valid (60 to {TTL})")
    p.add_argument("--link", action="store_true", help="print only the link")
    p.set_defaults(fn=cmd_pair)
