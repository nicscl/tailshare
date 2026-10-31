"""`tailshare hive pair`: a QR code that makes this machine the Tailshare Hive app's first trusted machine.

iOS apps cannot run `tailscale status`, so the app starts from one machine the person points it at by
scanning its screen. From there it reads /hello (full host keys, checked against the fingerprints here)
and /peers (the rest of the tailnet).

The link is not signed: the person scans their own terminal, which is what makes it trustworthy. It
expires within 10 minutes, and its single-use token makes the terminal the confirmation: when a phone
pairs, this command prints which one it was (from the tailnet, not from the phone), and a photographed
code cannot pair a second phone.
"""

from __future__ import annotations

import base64
import io
import json
import secrets
import shutil
import subprocess
import sys
import time

from . import bus, keys

FORMAT = 1
SCHEME = "tailsharehive://pair/"
TTL = 600  # seconds a printed code stays valid
BORDER = 2  # QR quiet zone, in modules


def host_keys(net: bus.Net) -> list[str]:
    """The host keys that answer SSH on port 22 here: tailscaled's with Tailscale SSH, else /etc/ssh's."""
    return list(net.ssh_host_keys) if net.tailscale_ssh else keys.host_keys()


def payload(net: bus.Net, token: str, exp: int) -> dict:
    return {"v": FORMAT, "ip": net.ip, "port": bus.BUS_PORT, "host": net.host, "owner": net.user,
            "hk": [keys.fingerprint(" ".join(k.split()[:2])) for k in host_keys(net)], "t": token, "exp": exp}


def pair_link(p: dict) -> str:
    raw = json.dumps(p, separators=(",", ":")).encode()
    return SCHEME + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def qr_text(data: str) -> str:
    """Half-block QR, light modules on a dark terminal."""
    import qrcode

    qr = qrcode.QRCode(border=BORDER, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(data)
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=True)
    return buf.getvalue().rstrip("\n")


def _copy(text: str) -> bool:
    exe = shutil.which("pbcopy") or shutil.which("wl-copy") or shutil.which("xclip")
    if not exe:
        return False
    argv = [exe] + (["-selection", "clipboard"] if exe.endswith("xclip") else [])
    try:
        return subprocess.run(argv, input=text.encode(), timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def cmd_pair(args) -> None:
    from . import agent

    if not 60 <= args.ttl <= TTL:
        sys.exit(f"✗ --ttl must be between 60 and {TTL} seconds")
    net = bus.Net()
    net.refresh()
    if not net.ip:
        sys.exit("✗ tailscale is not running here, or this machine is not on a tailnet")
    token, exp = secrets.token_urlsafe(16), int(time.time() + args.ttl)
    try:
        agent.call("/hive/token", {"t": token, "exp": exp}, timeout=3)
    except (agent.Offline, bus.BusError, OSError) as e:
        sys.exit(f"✗ the tailshare mailbox is not answering here ({e}), so the app could not reach this machine. "
                 "Start it with `tailshare install`.")
    link = pair_link(payload(net, token, exp))
    qr = qr_text(link)
    width = max(len(line) for line in qr.splitlines())
    fits = sys.stdout.isatty() and shutil.get_terminal_size((0, 0)).columns >= width + 4
    if args.link or not fits:
        print(link)
    else:
        print(f"Pair Tailshare Hive with {net.host} ({net.user} · {net.ip})\n")
        print(qr)
        print(f"\n{link}\n")
        print(f"Expires in {args.ttl // 60} minutes. Ctrl-C cancels.")
    if args.copy:
        print("link copied" if _copy(link) else "⚠ no clipboard tool found", file=sys.stderr)
    sys.stdout.flush()
    try:
        while True:
            st = agent.call(f"/hive/status?t={token}", timeout=5)
            if st["state"] == "paired":
                break
            if st["state"] == "expired":
                print("✗ expired: run `tailshare hive pair` again", file=sys.stderr)
                sys.exit(1)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        sys.exit(130)
    print(f"✓ Paired with {st['device']} ({st['user'] or 'unknown user'} · {st['ip']})")
    if st["user"] != net.user:
        print(f"⚠ that phone belongs to {st['user'] or 'someone not on this tailnet'}, not {net.user}: "
              "it treats this machine as shared with it")
    if st.get("request_id"):
        try:
            keys.approve_interactive(st["request_id"][:8], tries=3, skip_ok=True)
        except (ValueError, bus.BusError) as e:
            print(f"✗ {e}", file=sys.stderr)
        except KeyboardInterrupt:
            print("\nskipped: nothing installed", file=sys.stderr)


def add_parser(sub) -> None:
    hp = sub.add_parser("hive", help="the Tailshare Hive iOS app")
    s = hp.add_subparsers(dest="hive_cmd", required=True)
    p = s.add_parser("pair", help="show a QR code that adds this machine to the app, and wait for it")
    p.add_argument("--ttl", type=int, default=TTL, help=f"seconds the code stays valid (60 to {TTL})")
    p.add_argument("--link", action="store_true", help="print only the link, no QR code")
    p.add_argument("--copy", action="store_true", help="also put the link on the clipboard")
    p.set_defaults(fn=cmd_pair)
