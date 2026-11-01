"""SSH keys for new clients: a device asks over the tailnet, a person approves, the key lands in authorized_keys.

A client with no tailshare install (the iOS app) generates a key and calls a machine's mailbox:

  POST /bus/v1/keys/enroll        {pubkey, device_name} -> a pending request, by tailnet node and user
  GET  /bus/v1/keys/enroll/<id>   its status, for polling

Nothing is installed until a person runs `tailshare keys approve` in a terminal on that machine. That
installs the key there, and when the person signs the approval with a key the other machines trust
(the same ~/.tailshare/allowed_signers as launches, namespace tailshare-keys), on the owner's other
machines too. Keys are written into a managed block of ~/.ssh/authorized_keys and, by default, can
only run `tailshare ssh-gate`, which attaches to a tmux pane an agent on that machine registered.
That still lets the client type into an agent or a shell, so approving a key is approving shell-level
access as this account; the gate only keeps the key from doing anything else (no other commands,
no port or agent forwarding). An attached client can still run tmux commands (`run-shell`, new windows,
the command prompt, or anything over -CC), so a pane key is a shell key and is approved as one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import bus

ENROLL = os.environ.get("TAILSHARE_SSH_ENROLL", "1") != "0"
KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ecdsa-sha2-nistp256")
MODES = ("pane", "shell")
REQUIRE_SK = True  # signed approvals must come from a security key (tests turn this off)
CONFIRM_CHARS = 6  # of the fingerprint the person types: 36 bits, too many to grind a look-alike key
PENDING_TTL = 900  # a request waits this long for a person
MAX_PENDING = 3  # per calling device
MAX_PER_DAY = 20
APPROVAL_TTL = 300
APPROVAL_TTL_MAX = 900
ATTACH_FLAGS = {"read-only", "ignore-size", "active-pane", "no-output"}
AGENT_ENV = ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SANDBOX", "TAILSHARE_AGENT_ID")
BEGIN = "# >>> tailshare managed keys: change them with `tailshare keys`, not by hand >>>"
END = "# <<< tailshare managed keys <<<"
_ID_RE = re.compile(r"[0-9a-f]{32}")
_KEY_ID_RE = re.compile(r"k[0-9a-f]{12}")
_EXE_RE = re.compile(r"/[A-Za-z0-9._/+-]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS key_requests(
  id TEXT PRIMARY KEY, ip TEXT NOT NULL, node TEXT NOT NULL, user TEXT NOT NULL, device TEXT NOT NULL,
  pubkey TEXT NOT NULL, fingerprint TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
  status TEXT NOT NULL, key_id TEXT NOT NULL DEFAULT '', installed TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS ssh_keys(
  id TEXT PRIMARY KEY, pubkey TEXT NOT NULL, fingerprint TEXT NOT NULL, device TEXT NOT NULL,
  node TEXT NOT NULL, user TEXT NOT NULL, mode TEXT NOT NULL, approved_by TEXT NOT NULL, installed REAL NOT NULL);
CREATE TABLE IF NOT EXISTS revoked_keys(id TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS key_nonces(nonce TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS key_revocations(
  key_id TEXT NOT NULL, host TEXT NOT NULL, created REAL NOT NULL, done REAL, error TEXT, PRIMARY KEY(key_id, host));
"""


def log(msg: str) -> None:
    bus.log(f"keys: {msg}")


# --------------------------------------------------------------------------- keys and fingerprints


def parse_pubkey(text: object) -> str:
    """`type base64` of an OpenSSH public key of an allowed type; options and comment dropped."""
    if not isinstance(text, str) or len(text) > 2048:
        raise bus.BusError("pubkey must be an OpenSSH public key line")
    parts = text.strip().split()
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        raise bus.BusError(f"key type must be one of {', '.join(KEY_TYPES)}")
    kind, b64 = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9+/]{20,1024}={0,2}", b64):
        raise bus.BusError("malformed public key")
    try:
        blob = base64.b64decode(b64, validate=True)
    except ValueError:
        raise bus.BusError("malformed public key") from None
    fields = _ssh_strings(blob)
    if not fields or fields[0] != kind.encode() or not _well_formed(kind, fields[1:]):
        raise bus.BusError("public key data does not match its type")
    return f"{kind} {b64}"


TOKEN_HEADER = "X-Tailshare-Keys"


def token_path() -> Path:
    return bus.STATE_DIR / "keys.token"


def load_token(path: Path, create: bool = False) -> str:
    """The secret that proves a local caller runs as this account: other users cannot read the file.
    Without it the local API (127.0.0.1, open to every process on the machine) would let anyone approve.
    With `create` (the mailbox), a missing or unusable file is replaced by a new token."""
    try:
        return _read_token(path)
    except (FileNotFoundError, ValueError):
        if not create:
            raise
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keys.token.")  # mode 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
        try:
            os.link(tmp, path)  # fails if another process made one meanwhile: use theirs
        except FileExistsError:
            try:
                return _read_token(path)
            except ValueError:  # still empty or short: a crash mid-write, or a `touch`
                if path.is_symlink() or path.stat().st_uid != os.geteuid():
                    raise
                os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    return _read_token(path)


def _read_token(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if st.st_uid != os.geteuid() or st.st_mode & 0o077 or not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path} must be a file that belongs to this user and is readable only by it")
        token = f.read(256).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError(f"{path} does not hold a token")
    return token


def signature_key_type(signature: str) -> str:
    """Key type of the public key inside an armored sshsig signature ('' if it does not parse)."""
    body = "".join(l for l in signature.strip().splitlines() if not l.startswith("-----"))
    try:
        blob = base64.b64decode(body, validate=True)
    except ValueError:
        return ""
    if not blob.startswith(b"SSHSIG") or len(blob) < 10:
        return ""
    parts = _ssh_strings(blob[10:], 1)
    inner = _ssh_strings(parts[0], 1) if parts else None
    return inner[0].decode("ascii", "replace") if inner else ""


def _ssh_strings(blob: bytes, n: int = 0) -> list[bytes] | None:
    """The length-prefixed strings a blob is made of (only the first n, if given), or None if it does not
    parse exactly."""
    out, i = [], 0
    while (len(out) < n) if n else (i < len(blob)):
        if i + 4 > len(blob):
            return None
        k = int.from_bytes(blob[i : i + 4], "big")
        if i + 4 + k > len(blob):
            return None
        out.append(blob[i + 4 : i + 4 + k])
        i += 4 + k
    return out





def _well_formed(kind: str, rest: list[bytes]) -> bool:
    if kind == "ssh-ed25519":
        return len(rest) == 1 and len(rest[0]) == 32
    if kind == "sk-ssh-ed25519@openssh.com":
        return len(rest) == 2 and len(rest[0]) == 32 and 0 < len(rest[1]) <= 128
    if kind == "ecdsa-sha2-nistp256":
        return len(rest) == 2 and rest[0] == b"nistp256" and len(rest[1]) == 65 and rest[1][0] == 4
    return False


def fingerprint(pubkey: str) -> str:
    """OpenSSH's SHA256 fingerprint, as `ssh-keygen -l` and the phone show it."""
    blob = base64.b64decode(pubkey.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def clean_device(name: object) -> str:
    """The device name the client reported: shown to people and written as a key comment."""
    s = re.sub(r"[^A-Za-z0-9 ._'-]", "", name if isinstance(name, str) else "")
    s = re.sub(r"\s+", " ", s).strip()[:64]
    return s or "unnamed device"


def host_keys() -> list[str]:
    """This machine's SSH host public keys, for the client's known_hosts."""
    out = []
    for f in sorted(Path("/etc/ssh").glob("ssh_host_*_key.pub")):
        try:
            parts = f.read_text().split()
        except OSError:
            continue
        if len(parts) >= 2:
            out.append(f"{parts[0]} {parts[1]}")
    return out


# --------------------------------------------------------------------------- authorized_keys


def authorized_keys() -> Path:
    return Path.home() / ".ssh" / "authorized_keys"


def sshd_keys_files(paths: list[str] | None = None) -> list[str]:
    """sshd's AuthorizedKeysFile values ([] = the default). Drop-in files come first, as sshd reads them."""
    if paths is None:
        paths = sorted(str(p) for p in Path("/etc/ssh/sshd_config.d").glob("*.conf")) + ["/etc/ssh/sshd_config"]
    for f in paths:
        try:
            lines = Path(f).read_text().splitlines()
        except OSError:
            continue
        for ln in lines:
            parts = ln.split("#", 1)[0].split()
            if parts and parts[0].lower() == "authorizedkeysfile":
                return parts[1:]
    return []


def gate_exe() -> str:
    """An absolute path to the tailshare command, for the forced command."""
    for c in (shutil.which("tailshare"), str(Path("~/.local/bin/tailshare").expanduser()),
              str(Path(sys.executable).with_name("tailshare"))):
        if c and _EXE_RE.fullmatch(c) and os.access(c, os.X_OK):
            return c
    return ""


def install_problem() -> str:
    """Why this machine cannot take SSH keys, or ""."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return "tailshare runs as root here; it never installs SSH keys for root"
    if not ENROLL:
        return "SSH key enrollment is off on this machine (TAILSHARE_SSH_ENROLL=0)"
    files = sshd_keys_files()
    if files and not {".ssh/authorized_keys", "%h/.ssh/authorized_keys", "~/.ssh/authorized_keys"} & set(files):
        return f"sshd reads keys from {' '.join(files)}, not ~/.ssh/authorized_keys"
    if not gate_exe():
        return "cannot find an absolute path to the tailshare command"
    return ""


def key_line(k: dict, exe: str) -> str:
    comment = "tailshare:" + k["id"] + " " + k["device"].replace(" ", "-")
    if k["mode"] == "shell":
        return f"{k['pubkey']} {comment}"
    return f'restrict,pty,command="{exe} ssh-gate" {k["pubkey"]} {comment}'


def replace_block(old: str, lines: list[str]) -> str:
    """`old` with the managed block replaced by `lines` (removed when empty). Nothing else changes."""
    rows = old.splitlines()
    if BEGIN in rows:
        i = rows.index(BEGIN)
        if END not in rows[i:]:
            raise bus.BusError("authorized_keys has a tailshare block without its end marker; fix it by hand", 409)
        j = rows.index(END, i)
        before, after = rows[:i], rows[j + 1 :]
    else:
        before, after = rows, []
    block = [BEGIN, *lines, END] if lines else []
    out = before + block + after
    return "\n".join(out) + "\n" if out else ""


def write_authorized_keys(lines: list[str], path: Path | None = None) -> bool:
    """Rewrite the managed block atomically, keeping the file's mode. False if nothing changed."""
    path = path or authorized_keys()
    d = path.parent
    if d.is_symlink() or path.is_symlink():
        raise bus.BusError(f"{path} or its folder is a symlink; not touching it", 409)
    d.mkdir(mode=0o700, exist_ok=True)
    if d.stat().st_uid != os.geteuid():
        raise bus.BusError(f"{d} belongs to someone else", 409)
    old, mode = "", 0o600
    if path.exists():
        st = path.stat()
        if st.st_uid != os.geteuid():
            raise bus.BusError(f"{path} belongs to someone else", 409)
        mode = stat.S_IMODE(st.st_mode)
        old = path.read_text()
    new = replace_block(old, lines)
    if new == old:
        return False
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".authorized_keys.")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(new)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True


# --------------------------------------------------------------------------- the gate


def parse_attach(cmd: str) -> dict | None:
    """The one command a pane key may run:
    tmux [-CC|-C] -S <socket> [-C] attach[-session] [-r] [-f <flags>] -t %N [; select-window -t %N ; select-pane -t %N]
    """
    try:
        a = shlex.split(cmd)
    except ValueError:
        return None
    if not a or Path(a[0]).name != "tmux":
        return None
    i, cc = 1, ""
    if i < len(a) and a[i] in ("-CC", "-C"):
        cc, i = a[i], i + 1
    if a[i : i + 1] != ["-S"] or i + 2 >= len(a):
        return None
    sock, i = a[i + 1], i + 2
    if not cc and a[i] == "-C" and i + 1 < len(a):  # the Hive app's spelling: tmux -S <socket> -C attach
        cc, i = "-C", i + 1
    if a[i] not in ("attach", "attach-session"):
        return None
    i += 1
    flags: list[str] = []
    readonly, pane = False, None
    while i < len(a) and a[i] != ";":
        if a[i] == "-r" and not readonly:
            readonly, i = True, i + 1
        elif a[i] == "-f" and i + 1 < len(a) and not flags:
            flags = a[i + 1].split(",")
            if not flags or not set(flags) <= ATTACH_FLAGS or len(set(flags)) != len(flags):
                return None
            i += 2
        elif a[i] == "-t" and i + 1 < len(a) and pane is None:
            pane, i = a[i + 1], i + 2
        else:
            return None
    if pane is None or not re.fullmatch(r"%[0-9]{1,6}", pane):
        return None
    tail = a[i:]
    if tail not in ([], [";", "select-window", "-t", pane, ";", "select-pane", "-t", pane],
                    [";", "select-pane", "-t", pane]):
        return None
    return {"cc": cc, "socket": sock, "pane": pane, "readonly": readonly, "flags": flags, "tail": tail}


def gate_argv(req: dict, binary: str) -> list[str]:
    argv = [binary] + ([req["cc"]] if req["cc"] else []) + ["-S", req["socket"], "attach"]
    if req["readonly"]:
        argv.append("-r")
    if req["flags"]:
        argv += ["-f", ",".join(req["flags"])]
    return argv + ["-t", req["pane"]] + req["tail"]


def pane_alive(binary: str, sock: str, pane: str) -> bool:
    try:
        cp = subprocess.run([binary, "-S", sock, "display", "-p", "-t", pane, "#{pane_id}"],
                            capture_output=True, text=True, timeout=3, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0 and cp.stdout.strip() == pane


def registered_panes(db: Path) -> list[dict]:
    """Every pane an agent on this machine registered (kept until the agent is pruned, 30 days after it
    was last seen). Read straight from this user's database: the gate must not trust whatever answers
    on the mailbox port, which another local user could take while the mailbox is down."""
    import sqlite3

    st = db.stat()
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise OSError(f"{db} must belong to this user and not be writable by others")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute("SELECT address, tmux FROM agents WHERE tmux != '' ORDER BY last_seen DESC").fetchall()
    finally:
        con.close()
    out = []
    for address, raw in rows:
        try:
            t = bus.clean_tmux(json.loads(raw))
        except json.JSONDecodeError:
            continue
        if t:
            out.append({"address": address, "tmux": t})
    return out


def trusted_file(path: str) -> bool:
    """A binary only this user or root can change."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_uid in (0, os.geteuid()) and not st.st_mode & 0o022 and os.access(path, os.X_OK)


def own_socket(path: str) -> bool:
    """A tmux socket that this user's own server made (not one another user put in its place)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.geteuid()


def tmux_binary(registered: str) -> str:
    for c in (registered, shutil.which("tmux") or "", "/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"):
        if c and c.startswith("/") and os.path.basename(c) == "tmux" and trusted_file(c):
            return c
    return ""


def cmd_gate(_args) -> None:
    """Forced command of pane keys: attach to a registered agent pane, or refuse."""
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        panes = registered_panes(bus.STATE_DIR / "bus.db")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"tailshare ssh-gate: cannot read the registered panes ({e})")
    if not cmd:
        print("This key can only attach to the tmux pane of an agent on this machine:", file=sys.stderr)
        for p in panes:
            print(f"  {p['address']}: tmux -S {shlex.quote(p['tmux']['socket'])} attach -t {p['tmux']['pane']}",
                  file=sys.stderr)
        sys.exit(1)
    req = parse_attach(cmd)
    if req is None:
        sys.exit("tailshare ssh-gate: this key may only run `tmux -S <socket> attach -t %N` for an agent's pane")
    t = next((p["tmux"] for p in panes
              if p["tmux"]["socket"] == req["socket"] and p["tmux"]["pane"] == req["pane"]), None)
    if t is None:
        sys.exit(f"tailshare ssh-gate: no agent here registered pane {req['pane']} on {req['socket']}")
    binary = tmux_binary(t.get("bin") or "")
    if not binary:
        sys.exit("tailshare ssh-gate: no tmux binary that only this user or root can change")
    if not own_socket(req["socket"]) or not pane_alive(binary, req["socket"], req["pane"]):
        sys.exit(f"tailshare ssh-gate: pane {req['pane']} is gone")
    if req["cc"] == "-CC":  # the same control protocol as -C, which only goes through the filter below
        sys.exit("tailshare ssh-gate: -CC is not accepted; use -C")
    if req["cc"]:
        if req["tail"]:
            sys.exit("tailshare ssh-gate: -C attach takes no other commands")
        from . import gate_control

        sys.exit(gate_control.run(req, binary, log=log))
    os.execv(binary, gate_argv(req, binary))


# --------------------------------------------------------------------------- the service


def approval_doc(signer: str, origin: str, r: dict, key_id: str, mode: str, hosts: list[str],
                 ttl: int = APPROVAL_TTL) -> str:
    """The exact text a person signs to install a key on `hosts`."""
    from .sessions import canonical

    now = time.time()
    return canonical({
        "v": 1, "action": "ssh-key", "signer": signer, "origin": origin, "request": r["id"], "key_id": key_id,
        "pubkey": r["pubkey"], "fingerprint": r["fingerprint"], "device": r["device"], "node": r["node"],
        "user": r["user"], "mode": mode, "hosts": hosts, "nonce": secrets.token_hex(16),
        "issued": round(now, 3), "expires": round(now + ttl, 3),
    })


class Keys:
    def __init__(self, b: bus.Bus, signers: Path | None = None, keys_file: Path | None = None,
                 token_file: Path | None = None) -> None:
        from .sessions import SIGNERS

        self.bus, self.store = b, b.store
        self.signers = signers or SIGNERS
        self.keys_file = keys_file  # None: ~/.ssh/authorized_keys
        self.token = load_token(token_file or token_path(), create=True)
        with self.store.lock:
            self.store.db.executescript(SCHEMA)

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="keys-loop").start()

    def _loop(self) -> None:
        while True:
            time.sleep(60)
            try:
                self.retry_revocations()
            except Exception as e:  # noqa: BLE001
                log(f"revocations: {e!r}")

    @property
    def host(self) -> str:
        return self.bus.net.host

    def _owner_problem(self) -> str:
        """Fan-out and revocation trust "machines of the same tailnet user": only when there is one."""
        u = self.bus.net.user
        return "" if u and u != "tagged-devices" else "this machine has no single tailnet owner (it is tagged)"

    def _own_peer(self, host: str) -> dict | None:
        """The peer `host`, if it is online and belongs to this machine's owner."""
        p = self.bus.net.peers.get(host)
        if not p or not p.get("online") or self._owner_problem() or p.get("user") != self.bus.net.user:
            return None
        return p

    # -- what peers may know ----------------------------------------------------
    def ssh_info(self) -> dict:
        net = self.bus.net
        ts = bool(getattr(net, "tailscale_ssh", False))
        # Tailscale SSH answers port 22 with its own host keys, not the ones in /etc/ssh
        keys = list(getattr(net, "ssh_host_keys", []) or []) if ts else host_keys()
        return {"login": bus.LOGIN, "owner": net.user, "port": 22, "host_keys": keys,
                "tailscale_ssh": ts, "enroll": not install_problem(),
                "control": 1,  # ssh-gate takes `-C attach` (tailshare/gate_control.py)
                "control_size": 1,  # ... and refresh-client -C / select-window there, with the lease
                "control_zoom": 1}  # ... and zooms a split window on the pane while it sizes


    # -- requests from devices --------------------------------------------------
    def _view(self, r) -> dict:
        return {"request_id": r["id"], "fingerprint": r["fingerprint"], "host": self.host, "login": bus.LOGIN,
                "owner": self.bus.net.user, "status": "pending" if r["status"] == "approving" else r["status"],
                "expires": r["expires"],
                "key_id": r["key_id"], "installed": json.loads(r["installed"])}

    def _expire(self) -> None:
        self.store.x("UPDATE key_requests SET status='expired' WHERE status='pending' AND expires<?", time.time())

    def enroll(self, ip: str, body: dict) -> dict:
        problem = install_problem()
        if problem:
            raise bus.BusError(problem, 409)
        net = self.bus.net
        if net.host_for_ip(ip) is None:
            net.refresh()
        node, user = net.host_for_ip(ip), net.user_for_ip(ip)
        if not node:
            raise bus.BusError(f"{ip} is not a device on this tailnet", 403)
        pubkey = parse_pubkey(body.get("pubkey"))
        device = clean_device(body.get("device_name"))
        now = time.time()
        with self.store.lock:
            self._expire()
            same = self.store.one("SELECT * FROM key_requests WHERE ip=? AND pubkey=? AND status IN ('pending','approving','approved')"
                                  " ORDER BY created DESC", ip, pubkey)
            if same:
                return self._view(same)
            if self.store.one("SELECT 1 FROM ssh_keys WHERE pubkey=?", pubkey):
                raise bus.BusError("this key is already installed here", 409)
            pending = self.store.q("SELECT 1 FROM key_requests WHERE ip=? AND status='pending'", ip)
            today = self.store.q("SELECT 1 FROM key_requests WHERE ip=? AND created>?", ip, now - 86400)
            if len(pending) >= MAX_PENDING or len(today) >= MAX_PER_DAY:
                raise bus.BusError("too many key requests from this device; try again later", 429)
            if len(self.store.q("SELECT 1 FROM key_requests WHERE status='pending'")) >= 20:
                raise bus.BusError("too many pending key requests on this machine", 429)
            rid = secrets.token_hex(16)
            self.store.x("INSERT INTO key_requests(id, ip, node, user, device, pubkey, fingerprint, created, expires, status)"
                         " VALUES(?,?,?,?,?,?,?,?,?,'pending')", rid, ip, node, user or "", device, pubkey,
                         fingerprint(pubkey), now, now + PENDING_TTL)
            r = self.store.one("SELECT * FROM key_requests WHERE id=?", rid)
        log(f"request {rid[:8]} from {node} ({user}), device {device!r}, {r['fingerprint']}")
        self._notify(device, node, user or "")
        return self._view(r)

    def status(self, ip: str, rid: str) -> dict:
        self._expire()
        r = self.store.one("SELECT * FROM key_requests WHERE id=?", rid) if _ID_RE.fullmatch(rid) else None
        if r is None or r["ip"] != ip:
            raise bus.BusError("no such request", 404)
        return self._view(r)

    def cancel(self, ip: str, rid: str) -> dict:
        """The device withdraws its own pending request (an app's "Cancel request")."""
        self._expire()
        r = self.store.one("SELECT * FROM key_requests WHERE id=?", rid) if _ID_RE.fullmatch(rid) else None
        if r is None or r["ip"] != ip:
            raise bus.BusError("no such request", 404)
        if not self.store.x("UPDATE key_requests SET status='cancelled' WHERE id=? AND status='pending'", rid):
            st = self.store.one("SELECT status FROM key_requests WHERE id=?", rid)["status"]  # as of now, not before
            raise bus.BusError("request is being approved right now" if st == "approving" else f"request is {st}, not pending", 409)
        log(f"request {rid[:8]} cancelled by the device")
        return self._view(self.store.one("SELECT * FROM key_requests WHERE id=?", rid))

    def _notify(self, device: str, node: str, user: str) -> None:
        if sys.platform != "darwin" or os.geteuid() == 0:
            return
        script = ('on run argv\ndisplay notification "Run tailshare keys pending to review it." '
                  'with title "SSH key request" subtitle (item 1 of argv)\nend run')

        def show() -> None:
            try:
                subprocess.run(["osascript", "-e", script, f"{device} on {node} ({user})"], timeout=10,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass

        threading.Thread(target=show, daemon=True).start()

    # -- what the person on this machine sees -------------------------------------
    def targets(self) -> list[str]:
        """This machine and the other machines of its owner that run tailshare."""
        net = self.bus.net
        if self._owner_problem():
            return [net.host]
        peers = [r["host"] for r in self.store.q("SELECT host FROM peers WHERE last_ok IS NOT NULL ORDER BY host")
                 if net.peers.get(r["host"], {}).get("user") == net.user and r["host"] != net.host]
        return [net.host, *peers]

    def pending(self) -> dict:
        self._expire()
        rows = self.store.q("SELECT * FROM key_requests WHERE status='pending' ORDER BY created")
        return {"host": self.host, "owner": self.bus.net.user, "login": bus.LOGIN, "problem": install_problem(),
                "targets": self.targets(), "pending": [
                    {k: r[k] for k in ("id", "ip", "node", "user", "device", "pubkey", "fingerprint", "created", "expires")}
                    for r in rows]}

    def ls(self) -> dict:
        rows = self.store.q("SELECT * FROM ssh_keys ORDER BY installed")
        unfinished = self.store.q("SELECT key_id, host, created, error FROM key_revocations WHERE done IS NULL")
        return {"host": self.host, "login": bus.LOGIN, "keys": [dict(r) for r in rows],
                "revoking": [dict(r) for r in unfinished]}

    # -- approving and installing ---------------------------------------------------
    def _rewrite(self) -> None:
        exe = gate_exe()
        rows = self.store.q("SELECT * FROM ssh_keys ORDER BY installed")
        write_authorized_keys([key_line(dict(r), exe) for r in rows], self.keys_file)

    def install_here(self, key_id: str, r: dict, mode: str, by: str) -> dict:
        problem = install_problem()
        if problem:
            raise bus.BusError(problem, 409)
        with self.store.lock:
            if self.store.one("SELECT 1 FROM revoked_keys WHERE id=?", key_id):
                raise bus.BusError(f"key {key_id} was revoked; enroll again", 409)
            self.store.x("INSERT OR REPLACE INTO ssh_keys(id, pubkey, fingerprint, device, node, user, mode, approved_by,"
                         " installed) VALUES(?,?,?,?,?,?,?,?,?)", key_id, r["pubkey"], r["fingerprint"], r["device"],
                         r["node"], r["user"], mode, by, time.time())
            try:
                self._rewrite()
            except BaseException:
                self.store.x("DELETE FROM ssh_keys WHERE id=?", key_id)
                raise
        log(f"installed {key_id} ({r['device']}, {r['fingerprint']}) mode={mode}, {by}")
        return {"host": self.host, "login": bus.LOGIN, "mode": mode, "key_id": key_id}

    def check_approval(self, payload: str, signature: str) -> dict:
        from .sessions import KEYS_NAMESPACE, verify

        if len(payload) > 16 * 1024 or len(signature) > 8 * 1024:
            raise bus.BusError("approval too large", 413)
        if REQUIRE_SK and not signature_key_type(signature).startswith("sk-"):
            raise bus.BusError("approval refused: key approvals for other machines must be signed with a security key "
                               "(ed25519-sk), so a program that can read a key file cannot sign one", 403)
        try:
            d = verify(payload, signature, self.signers, KEYS_NAMESPACE)
        except bus.BusError as e:
            if e.code != 403:
                raise
            raise bus.BusError(f"{e}. If this key was trusted before key approvals existed, run `tailshare sessions "
                               "trust` for it again: its line needs namespaces=\"...,tailshare-keys\"", 403) from None
        now = time.time()
        problems = []
        if d.get("v") != 1 or d.get("action") != "ssh-key":
            problems.append("not a key approval")
        if not isinstance(d.get("hosts"), list) or self.host not in d["hosts"]:
            problems.append(f"not signed for {self.host}")
        issued, expires = float(d.get("issued") or 0), float(d.get("expires") or 0)
        if expires < now or issued > now + 120 or expires - issued > APPROVAL_TTL_MAX:
            problems.append("expired or bad validity window")
        if d.get("mode") not in MODES:
            problems.append("bad mode")
        if not _KEY_ID_RE.fullmatch(str(d.get("key_id"))):
            problems.append("bad key id")
        try:
            if parse_pubkey(d.get("pubkey")) != d.get("pubkey") or fingerprint(d["pubkey"]) != d.get("fingerprint"):
                problems.append("key and fingerprint do not match")
        except bus.BusError:
            problems.append("bad key")
        if problems:
            raise bus.BusError("approval refused: " + "; ".join(problems), 403)
        nonce = str(d.get("nonce"))
        with self.store.lock:
            self.store.x("DELETE FROM key_nonces WHERE expires<?", now)
            if not re.fullmatch(r"[0-9a-f]{32}", nonce) or self.store.one("SELECT 1 FROM key_nonces WHERE nonce=?", nonce):
                raise bus.BusError("approval refused: already used", 403)
            self.store.x("INSERT INTO key_nonces(nonce, expires) VALUES(?,?)", nonce, expires)
        d["device"] = clean_device(d.get("device"))
        for k in ("node", "user"):
            d[k] = str(d.get(k) or "")[:128]
        return d

    def approve(self, body: dict) -> dict:
        """Called by `tailshare keys approve` on this machine (the local API)."""
        rid, mode = str(body.get("id") or ""), str(body.get("mode") or "pane")
        if mode not in MODES:
            raise bus.BusError(f"mode must be one of {', '.join(MODES)}")
        self._expire()
        r = self.store.one("SELECT * FROM key_requests WHERE id=? AND status='pending'", rid)
        if r is None:
            raise bus.BusError("no such pending request (it may have expired, or is being approved)", 404)
        tail = r["fingerprint"][-CONFIRM_CHARS:]
        if str(body.get("confirm") or "") != tail:
            raise bus.BusError(f"confirm must be the last {CONFIRM_CHARS} characters of the fingerprint", 403)
        twins = [x for x in self.store.q("SELECT fingerprint FROM key_requests WHERE status='pending' AND id!=?", rid)
                 if x["fingerprint"][-CONFIRM_CHARS:] == tail]
        if twins:
            raise bus.BusError("another pending request ends in the same characters: someone may be imitating this "
                               "device. Deny the requests you do not recognize, then ask the device to enroll again", 409)
        if not self.store.x("UPDATE key_requests SET status='approving' WHERE id=? AND status='pending'", rid):
            raise bus.BusError("no such pending request (it may have expired, or is being approved)", 404)
        try:
            return self._approve(rid, mode, body)
        except BaseException:
            self.store.x("UPDATE key_requests SET status='pending' WHERE id=? AND status='approving'", rid)
            raise

    def _approve(self, rid: str, mode: str, body: dict) -> dict:
        r = dict(self.store.one("SELECT * FROM key_requests WHERE id=?", rid))
        payload, signature = str(body.get("payload") or ""), str(body.get("signature") or "")
        if payload:
            d = self.check_approval(payload, signature)
            if d.get("request") != rid or d["pubkey"] != r["pubkey"] or d["mode"] != mode:
                raise bus.BusError("approval refused: signed for another request", 403)
            key_id, hosts, by = d["key_id"], [h for h in d["hosts"] if isinstance(h, str)], f"signed by {d['signer']}"
        else:
            key_id, hosts, by = "k" + secrets.token_hex(6), [self.host], "approved here"
        installed, errors = [self.install_here(key_id, r, mode, by)], {}
        for h in hosts:
            if h == self.host:
                continue
            p = self._own_peer(h)
            if not p:
                errors[h] = "offline, or not a machine of this owner"
                continue
            try:
                installed.append(bus.request(f"http://{p['ip']}:{p['port']}/bus/v1/keys/install",
                                             {"v": bus.PROTOCOL, "payload": payload, "signature": signature}, timeout=20))
            except Exception as e:  # noqa: BLE001
                errors[h] = str(e)[:200]
        self.store.x("UPDATE key_requests SET status='approved', key_id=?, installed=? WHERE id=?",
                     key_id, json.dumps(installed), rid)
        return {"key_id": key_id, "installed": installed, "errors": errors}

    def install_from_peer(self, ip: str, body: dict) -> dict:
        if self._owner_problem():
            raise bus.BusError(self._owner_problem(), 403)
        self.bus.require_same_user(ip)
        d = self.check_approval(str(body.get("payload") or ""), str(body.get("signature") or ""))
        r = {k: d[k] for k in ("pubkey", "fingerprint", "device", "node", "user")}
        return self.install_here(d["key_id"], r, d["mode"], f"signed by {d['signer']} (from {d.get('origin')})")

    def deny(self, body: dict) -> dict:
        n = self.store.x("UPDATE key_requests SET status='denied' WHERE id=? AND status='pending'", str(body.get("id")))
        if not n:
            raise bus.BusError("no such pending request", 404)
        return {"denied": True}

    # -- revoking ------------------------------------------------------------------------
    def remove_here(self, key_id: str) -> bool:
        with self.store.lock:
            self.store.x("INSERT OR IGNORE INTO revoked_keys(id, at) VALUES(?,?)", key_id, time.time())
            n = self.store.x("DELETE FROM ssh_keys WHERE id=?", key_id)
            self.store.x("UPDATE key_requests SET status='revoked' WHERE key_id=?", key_id)
            if n:
                self._rewrite()
        if n:
            log(f"removed {key_id}")
        return bool(n)

    def rm(self, body: dict) -> dict:
        """Called by `tailshare keys rm`: remove here, then on the owner's other machines."""
        key_id = str(body.get("key_id") or "")
        if not _KEY_ID_RE.fullmatch(key_id):
            raise bus.BusError("bad key id")
        removed = self.remove_here(key_id)
        now = time.time()
        hosts = set(self.targets()[1:])
        for r in self.store.q("SELECT installed FROM key_requests WHERE key_id=?", key_id):
            hosts.update(i.get("host") for i in json.loads(r["installed"]) if isinstance(i, dict))
        hosts = sorted(h for h in hosts if isinstance(h, str) and h and h != self.host)
        for h in hosts:
            self.store.x("INSERT OR REPLACE INTO key_revocations(key_id, host, created) VALUES(?,?,?)", key_id, h, now)
        return {"removed_here": removed, "elsewhere": self.retry_revocations(key_id)}

    def retry_revocations(self, only: str = "") -> dict:
        now = time.time()
        out = {}
        for r in self.store.q("SELECT * FROM key_revocations WHERE done IS NULL"):
            if only and r["key_id"] != only:
                continue
            p = self._own_peer(r["host"])
            if not p:
                out[r["host"]] = "offline: will keep retrying (see `tailshare keys ls`)"
                continue
            try:
                d = bus.request(f"http://{p['ip']}:{p['port']}/bus/v1/keys/revoke",
                                {"v": bus.PROTOCOL, "key_id": r["key_id"]}, timeout=10)
                if d.get("host") != r["host"]:
                    raise bus.BusError(f"answered as {d.get('host')!r}")
                self.store.x("UPDATE key_revocations SET done=? WHERE key_id=? AND host=?", now, r["key_id"], r["host"])
                out[r["host"]] = "removed" if d.get("removed") else "was not installed"
            except Exception as e:  # noqa: BLE001
                self.store.x("UPDATE key_revocations SET error=? WHERE key_id=? AND host=?", str(e)[:200], r["key_id"], r["host"])
                out[r["host"]] = f"failed, will retry: {str(e)[:100]}"
        return out

    def revoke(self, ip: str, body: dict) -> dict:
        """POST /keys/revoke. From the device that enrolled the key here: remove it here and on the
        owner's other machines (removing access needs no signature, so a lost phone's app can do it).
        From another machine: that machine's `keys rm` fanning out, handled by revoke_from_peer."""
        key_id = str(body.get("key_id") or "")
        if not _KEY_ID_RE.fullmatch(key_id):
            raise bus.BusError("bad key id")
        if self.store.one("SELECT 1 FROM key_requests WHERE key_id=? AND ip=?", key_id, ip):
            log(f"{key_id} revoked by the device that enrolled it ({ip})")
            out = self.rm(body)
            return {"host": self.host, "removed": out["removed_here"], **out}
        return self.revoke_from_peer(ip, body)

    def revoke_from_peer(self, ip: str, body: dict) -> dict:
        if self._owner_problem():
            raise bus.BusError(self._owner_problem(), 403)
        self.bus.require_same_user(ip)
        key_id = str(body.get("key_id") or "")
        if not _KEY_ID_RE.fullmatch(key_id):
            raise bus.BusError("bad key id")
        return {"host": self.host, "removed": self.remove_here(key_id)}

    # -- HTTP --------------------------------------------------------------------------
    def route(self, method: str, path: str, body: dict, local: bool, ip: str, token: str = "") -> dict:
        if local:
            if len(self.token) < 64 or not secrets.compare_digest(token.encode(), self.token.encode()):
                raise bus.BusError(f"missing or wrong {TOKEN_HEADER} token (only {bus.LOGIN} can manage keys here)", 403)
            routes = {("GET", "/keys/pending"): lambda: self.pending(), ("GET", "/keys/ls"): lambda: self.ls(),
                      ("POST", "/keys/approve"): lambda: self.approve(body),
                      ("POST", "/keys/deny"): lambda: self.deny(body), ("POST", "/keys/rm"): lambda: self.rm(body)}
        else:
            if method == "GET" and path.startswith("/keys/enroll/"):
                return self.status(ip, path.removeprefix("/keys/enroll/"))
            if method == "POST" and path.startswith("/keys/enroll/") and path.endswith("/cancel"):
                return self.cancel(ip, path.removeprefix("/keys/enroll/").removesuffix("/cancel"))
            routes = {("POST", "/keys/enroll"): lambda: self.enroll(ip, body),
                      ("POST", "/keys/install"): lambda: self.install_from_peer(ip, body),
                      ("POST", "/keys/revoke"): lambda: self.revoke(ip, body)}
        fn = routes.get((method, path))
        if fn is None:
            raise bus.BusError("not found", 404)
        return fn()


def start(b: bus.Bus) -> Keys:
    k = Keys(b)
    k.start()
    return k


# --------------------------------------------------------------------------- `tailshare keys`


def _run(fn):
    def wrapped(args):
        from . import agent

        try:
            fn(args)
        except (agent.Offline, bus.BusError, ValueError) as e:
            print(f"✗ {e}", file=sys.stderr)
            sys.exit(1)
    return wrapped


def _call(path: str, payload: dict | None = None, timeout: float = 30) -> dict:
    from . import agent

    try:
        token = load_token(token_path())
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read {token_path()} ({e}); is this the account tailshare runs as?") from None
    try:
        return bus.request(agent.LOCAL + path, payload, timeout, {TOKEN_HEADER: token})
    except bus.BusError:
        raise
    except OSError as e:
        raise agent.Offline(f"tailshare mailbox is not running on this machine ({e})") from None


def _human_check() -> None:
    agentish = [v for v in AGENT_ENV if os.environ.get(v)]
    if agentish:
        raise ValueError(f"this looks like an AI agent's shell ({', '.join(agentish)} is set). Agents must not approve "
                         "SSH keys: ask the person to run `tailshare keys approve` in their own terminal")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("approving a key needs a person at a terminal")


def _signs_key_approvals(key: Path) -> bool:
    """The signing key is a security key that this machine's allowed_signers trusts for tailshare-keys."""
    from .sessions import KEYS_NAMESPACE, SIGNERS

    try:
        pub = Path(str(key) + ".pub").read_text().split()
        lines = SIGNERS.read_text().splitlines()
    except OSError:
        return False
    if len(pub) < 2 or (REQUIRE_SK and not pub[0].startswith("sk-")):
        return False
    return any(pub[1] in l and KEYS_NAMESPACE in l for l in lines)


def _find(pending: list[dict], prefix: str) -> dict:
    hits = [p for p in pending if p["id"].startswith(prefix)] if prefix else []
    if len(hits) != 1:
        raise ValueError("no such pending request" if not hits else "ambiguous id; give more characters")
    return hits[0]


@_run
def cmd_pending(args):
    d = _call("/keys/pending")
    if d.get("problem"):
        print(f"⚠ {d['problem']}", file=sys.stderr)
    if args.json:
        print(json.dumps(d, indent=1))
        return
    if not d["pending"]:
        print("no pending key requests")
        return
    for p in d["pending"]:
        who = p["user"] or "unknown user"
        other = "" if p["user"] == d["owner"] else "   ⚠ not your device"
        print(f"{p['id'][:8]}  {p['device']!r} on {p['node']} ({who}){other}")
        print(f"          {p['fingerprint']}  expires in {max(0, int(p['expires'] - time.time())) // 60} min")
    print("\napprove: tailshare keys approve <id>     deny: tailshare keys deny <id>")


@_run
def cmd_approve(args):
    approve_interactive(args.id, args.shell, args.here, args.key, args.signer)


def approve_interactive(rid: str, shell: bool = False, here: bool = False, key_arg: str = "", signer_arg: str = "",
                        tries: int = 1, skip_ok: bool = False) -> bool:
    """Show a pending request and install it if the person types the fingerprint's end. Returns whether a key
    was installed (False only when skip_ok and the person pressed Enter)."""
    from . import sessions, sessions_cli

    _human_check()
    d = _call("/keys/pending")
    if d.get("problem"):
        raise ValueError(d["problem"])
    p = _find(d["pending"], rid)
    mode = "shell" if shell else "pane"
    key = None
    if not here:
        try:
            key = sessions_cli._key(key_arg)
        except ValueError:
            key = None
    if key and not _signs_key_approvals(key):
        print(f"⚠ {key} is not a security key, or ~/.tailshare/allowed_signers does not trust it for key approvals "
              "(re-run `tailshare sessions trust`): this machine only", file=sys.stderr)
        key = None
    hosts = d["targets"] if key else [d["host"]]
    mine = p["user"] == d["owner"]
    print("You are approving an SSH key:")
    print(f"  device       {p['device']!r} (the name the device gave itself)")
    print(f"  tailnet      {p['node']}, owned by {p['user'] or 'unknown'}" + ("" if mine else "   ⚠ NOT one of your devices"))
    print(f"  fingerprint  {p['fingerprint']}")
    if mode == "pane":
        print(f"  access       attach to tmux panes of agents, as {d['login']}. Typing into a pane is as good as a shell.")
    else:
        print(f"  access       a full shell as {d['login']}")
    print(f"  machines     {', '.join(hosts)}" + ("" if key else "   (no signing key found: this machine only)"))
    want = p["fingerprint"][-CONFIRM_CHARS:]
    skip = "skip" if skip_ok else "cancel"
    for attempt in range(tries):
        got = input(f"Type the last {CONFIRM_CHARS} characters of the fingerprint shown on the device (Enter to {skip}): ").strip()
        if not got:
            if skip_ok:
                print("skipped: nothing installed. Approve later with `tailshare keys approve " + p["id"][:8] + "`")
                return False
            raise ValueError("cancelled")
        if got == want:
            break
        if attempt + 1 < tries:
            print("✗ does not match; try again")
    else:
        raise ValueError("does not match the fingerprint: nothing installed")
    body = {"id": p["id"], "mode": mode, "confirm": got}
    if key:
        signer = sessions_cli._principal(signer_arg)
        payload = approval_doc(signer, d["host"], p, "k" + secrets.token_hex(6), mode, hosts)
        body.update(payload=payload, signature=sessions.sign(payload, key, sessions.KEYS_NAMESPACE))
    r = _call("/keys/approve", body, timeout=90)
    for i in r["installed"]:
        print(f"✓ installed on {i['host']} as {i['login']} ({i['mode']})")
    for h, e in r["errors"].items():
        print(f"✗ {h}: {e}")
    print(f"key id {r['key_id']}; remove it with `tailshare keys rm {r['key_id']}`")
    return True


@_run
def cmd_deny(args):
    p = _find(_call("/keys/pending")["pending"], args.id)
    _call("/keys/deny", {"id": p["id"]})
    print("✓ denied")


@_run
def cmd_ls(args):
    d = _call("/keys/ls")
    if args.json:
        print(json.dumps(d, indent=1))
        return
    if not d["keys"]:
        print(f"no tailshare-managed SSH keys for {d['login']} on {d['host']}")
    for k in d["keys"]:
        when = time.strftime("%Y-%m-%d", time.localtime(k["installed"]))
        print(f"{k['id']}  {k['mode']:<5}  {k['device']!r} on {k['node']} ({k['user']})  {k['fingerprint']}  {when}, {k['approved_by']}")
    for r in d.get("revoking", []):
        print(f"⚠ {r['key_id']} is still installed on {r['host']}: removal pending since "
              f"{time.strftime('%Y-%m-%d', time.localtime(r['created']))}" + (f" ({r['error']})" if r["error"] else ""))


@_run
def cmd_rm(args):
    r = _call("/keys/rm", {"key_id": args.key_id})
    print(f"{'✓ removed here' if r['removed_here'] else 'not installed here'}")
    for h, s in r["elsewhere"].items():
        print(f"  {h}: {s}")


def add_parser(sub) -> None:
    kp = sub.add_parser("keys", help="SSH keys that devices asked for: review, approve, revoke")
    s = kp.add_subparsers(dest="keys_cmd", required=True)
    p = s.add_parser("pending", help="key requests waiting for you")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_pending)
    p = s.add_parser("approve", help="install a requested key (asks you to confirm its fingerprint)")
    p.add_argument("id")
    p.add_argument("--shell", action="store_true", help="full shell access, not only attaching to agent panes")
    p.add_argument("--here", action="store_true", help="only this machine, even with a signing key")
    p.add_argument("--key", default="", help="signing key (default: TAILSHARE_SIGN_KEY or ~/.ssh/id_*_sk)")
    p.add_argument("--as", dest="signer", default="", help="signer name (default: your tailnet user)")
    p.set_defaults(fn=cmd_approve)
    p = s.add_parser("deny", help="refuse a key request")
    p.add_argument("id")
    p.set_defaults(fn=cmd_deny)
    p = s.add_parser("ls", help="keys installed here by tailshare")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ls)
    p = s.add_parser("rm", help="remove a key here and on your other machines")
    p.add_argument("key_id")
    p.set_defaults(fn=cmd_rm)
    sub.add_parser("ssh-gate", help="forced command of pane keys (run by sshd, not by hand)").set_defaults(fn=cmd_gate)
