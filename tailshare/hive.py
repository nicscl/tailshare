"""`tailshare hive pair`: a QR code that makes this machine the Tailshare Hive app's first trusted machine.

iOS apps cannot run `tailscale status`, so the app starts from one machine the person points it at by
scanning its screen. From there it reads /hello (full host keys, checked against the fingerprints here)
and /peers (the rest of the tailnet).

The link is not signed. What protects the phone is the 6-character code printed here: a machine shared into
the tailnet can mint a link that names this machine (and answer its own /hive/pair), but not the code on
this screen, which the app makes the person check or type. What protects this machine is the single-use
token: the terminal prints which device used it (from the tailnet, not from the device), and a
photographed code cannot be used twice. Codes expire within 10 minutes; the mailbox enforces that too.
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
TTL = bus.Bus.PAIR_TTL  # seconds a printed code stays valid
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L
BORDER = 2  # QR quiet zone, in modules
KEY_WAIT = 60  # seconds to wait after pairing for the app's SSH key request


def host_keys(net: bus.Net) -> list[str]:
    """The host keys that answer SSH on port 22 here: tailscaled's with Tailscale SSH, else /etc/ssh's."""
    return list(net.ssh_host_keys) if net.tailscale_ssh else keys.host_keys()


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(6))


def payload(net: bus.Net, token: str, code: str, exp: int) -> dict:
    return {"v": FORMAT, "ip": net.ip, "port": bus.BUS_PORT, "host": net.host, "owner": net.user,
            "hk": [keys.fingerprint(" ".join(k.split()[:2])) for k in host_keys(net)], "t": token, "c": code,
            "exp": exp}


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
    token, code, exp = secrets.token_urlsafe(16), new_code(), int(time.time() + args.ttl)
    try:
        agent.call("/hive/token", {"token": token, "code": code, "exp": exp}, timeout=3)
    except (agent.Offline, bus.BusError, OSError) as e:
        sys.exit(f"✗ the tailshare mailbox is not answering here ({e}), so the app could not reach this machine. "
                 "Start it with `tailshare install`.")
    link = pair_link(payload(net, token, code, exp))
    qr = qr_text(link)
    width = max(len(line) for line in qr.splitlines())
    fits = sys.stdout.isatty() and shutil.get_terminal_size((0, 0)).columns >= width + 4
    bold = ("\033[1m", "\033[0m") if sys.stdout.isatty() else ("", "")
    if args.link or not fits:
        print(link)
        print(f"Pairing code  {code[:3]} {code[3:]}", file=sys.stderr)
    else:
        print(f"Pair Tailshare Hive with {net.host} ({net.user} · {net.ip})\n")
        print(qr)
        print(f"\nPairing code  {bold[0]}{code[:3]} {code[3:]}{bold[1]}")
        print("Check that the app shows the same code, or type it there if you opened the link.\n")
        print(link)
        print(f"\nExpires in {args.ttl // 60} minutes. Ctrl-C cancels.")
    if args.copy:
        print("link copied" if _copy(link) else "⚠ no clipboard tool found", file=sys.stderr)
    sys.stdout.flush()
    try:
        while True:
            st = agent.call(f"/hive/status?t={token}", timeout=5)
            if st["state"] == "used":
                break
            if st["state"] == "expired":
                print("✗ expired: run `tailshare hive pair` again", file=sys.stderr)
                sys.exit(1)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        sys.exit(130)
    print(f"✓ Code used by {st['device']} ({st['user'] or 'unknown user'} · {st['ip']})")
    if st["user"] != net.user:
        print(f"⚠ that phone belongs to {st['user'] or 'someone not on this tailnet'}, not {net.user}: "
              "it treats this machine as shared with it")
    _offer_key(st)


def _offer_key(st: dict) -> None:
    """The app usually asks for an SSH key right after pairing (POST /keys/enroll). For a person at this
    terminal, wait a little for that request from the device that used the code (same tailnet IP) and offer
    the usual approve prompt, with the same typed check."""
    try:
        keys._human_check()
    except ValueError:
        return  # not a person at a terminal: `tailshare keys approve` later
    print(f"Waiting up to {KEY_WAIT} s for the app to ask for an SSH key (Ctrl-C skips)…")
    try:
        end = time.time() + KEY_WAIT
        while time.time() < end:
            mine = [p for p in keys._call("/keys/pending")["pending"] if p["ip"] == st["ip"]]
            if mine:
                keys.approve_interactive(mine[-1]["id"][:8], tries=3, skip_ok=True)
                return
            time.sleep(1)
        print("no key request came; approve one later with `tailshare keys pending`")
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
