"""SSH keys for new clients: a device asks over the tailnet, a person approves, the key lands in authorized_keys.

A client with no tailshare install (the iOS app) generates a key and calls a machine's mailbox:

  POST /bus/v1/keys/enroll        {pubkey, device_name} -> a pending request, by tailnet node and user
  GET  /bus/v1/keys/enroll/<id>   its status, for polling
  GET  /bus/v1/keys/request/<prefix>  one of the owner's requests by an 8-32 hex id prefix, for the owner's other devices

Nothing is installed until a person runs `tailshare keys approve` in a terminal on that machine. That
installs the key there, and on the owner's other machines when the person signs the approval with a
security key they trust (the same ~/.tailshare/allowed_signers as launches, namespace tailshare-keys).
Without one, the approval still reaches those of them where a person ran `tailshare keys trust-machine
<this machine>`: this machine's own key signs it (docs/ssh-keys.md#machine-trust). Keys are written into a
managed block of ~/.ssh/authorized_keys and, by default, can only run `tailshare ssh-gate`, which attaches
to a tmux pane an agent on that machine registered.
That still lets the client type into an agent or a shell, so approving a key is approving shell-level
access as this account; the gate only keeps the key from doing anything else (no other commands,
no port or agent forwarding). An attached client can still run tmux commands (`run-shell`, new windows,
the command prompt, or anything over -CC), so a pane key is a shell key and is approved as one.
"""

from __future__ import annotations

import base64
import functools
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
START_KEY_TYPE = "ecdsa-sha2-nistp256"  # start and plan keys: what the Secure Enclave makes, and nothing else
MODES = ("pane", "shell", "start", "start-skip")
PURPOSES = {"pane": ("pane", "shell"), "start": ("start", "start-skip")}  # what each kind of request may become
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
_PREFIX_RE = re.compile(r"[0-9a-f]{8,32}")
_EXE_RE = re.compile(r"/[A-Za-z0-9._/+-]+")
SQUADS_NAMESPACE = "tailshare-squads"  # a device's signed squad write; never valid as a launch or a key approval
SIGNED_WINDOW = 60  # how far a signed write's time may be from this machine's clock
VERIFY_PER_MINUTE = 30  # signature checks per address: each one runs ssh-keygen
SIGNED_HEADERS = ("X-Tailshare-Key", "X-Tailshare-Time", "X-Tailshare-Nonce", "X-Tailshare-Signature")
MACHINE_KEY_TYPE = "ssh-ed25519"  # a machine key is a plain file: whatever reads it signs as the machine
INSTALL_RETRY_FOR = 7 * 86400  # how long an approval keeps trying a machine that was offline
_HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")  # also a valid allowed_signers principal

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
CREATE TABLE IF NOT EXISTS key_installs(
  key_id TEXT NOT NULL, host TEXT NOT NULL, created REAL NOT NULL, done REAL, final INTEGER NOT NULL DEFAULT 0,
  error TEXT, PRIMARY KEY(key_id, host));
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


def machine_key_path() -> Path:
    """This machine's own signing key, for approvals it sends to the machines that trust it."""
    return bus.STATE_DIR / "machine_key"


def trusted_machines_path() -> Path:
    """The machines whose approvals this one accepts (`tailshare keys trust-machine`): an allowed_signers file
    whose lines name only the tailshare-keys namespace, so a machine key never signs a launch here."""
    return bus.STATE_DIR / "trusted_machines"


def machine_line(host: str, pubkey: str) -> str:
    from .sessions import KEYS_NAMESPACE

    if not _HOST_RE.fullmatch(host):
        raise bus.BusError("bad machine name")
    return f'{host} namespaces="{KEYS_NAMESPACE}" {parse_pubkey(pubkey)}\n'


def read_trusted(path: Path) -> list[dict]:
    """[{host, pubkey, fingerprint}] of a trusted_machines file (none when it is missing)."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and _HOST_RE.fullmatch(parts[0]) and parts[1].startswith("namespaces="):
            try:
                pub = parse_pubkey(" ".join(parts[2:4]))
            except bus.BusError:
                continue
            out.append({"host": parts[0], "pubkey": pub, "fingerprint": fingerprint(pub)})
    return out


def write_trusted(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".trusted_machines.")  # mode 0600
    try:
        with os.fdopen(fd, "w") as f:
            f.write("".join(machine_line(r["host"], r["pubkey"]) for r in rows))
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


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


def pair_fingerprint(pubkey: str, plan: str) -> str:
    """A start key and its plan key as one: SHA256 of both key blobs, start first (both P-256, so fixed length).
    Its last characters are the code for a start request, so a plan key swapped in by someone else changes it."""
    blob = base64.b64decode(pubkey.split()[1]) + base64.b64decode(plan.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def confirm_code(r) -> str:
    """What the person types to approve a request: the end of the fingerprint the device shows, which for a start
    request covers its plan key too."""
    fp = pair_fingerprint(r["pubkey"], r["plan_pubkey"]) if r["plan_pubkey"] else r["fingerprint"]
    return fp[-CONFIRM_CHARS:]


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


@functools.lru_cache(maxsize=1)
def gate_python() -> str:
    """The interpreter tailshare runs under, for the keys' lines, or "" when its path is not plain or tailshare
    does not import under it with -I (a `pip install --user`, or a checkout on PYTHONPATH): -I leaves those out."""
    p = sys.executable
    if not (p and _EXE_RE.fullmatch(p) and os.access(p, os.X_OK)):
        return ""
    try:
        ok = subprocess.run([p, "-I", "-c", "import tailshare"], cwd="/", env={"PATH": "/usr/bin:/bin"},
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return p if ok else ""


def key_lines(k: dict, exe: str, python: str | None = None) -> list[str]:
    """A key's authorized_keys lines: one, or two for a start key (the start key, then its plan key)."""
    if k["mode"] in ("start", "start-skip") and k.get("plan_pubkey"):
        return [key_line(k, exe, python), key_line(k, exe, python, plan=True)]
    return [key_line(k, exe, python)]


def key_line(k: dict, exe: str, python: str | None = None, plan: bool = False) -> str:
    comment = "tailshare:" + k["id"] + (" plan" if plan else "") + " " + k["device"].replace(" ", "-")
    if k["mode"] == "shell":
        return f"{k['pubkey']} {comment}"
    # the interpreter itself with -I: PYTHONPATH, PYTHONSTARTUP and friends act before any of tailshare's code
    # runs, so the console script's shebang (no -I) would let an AcceptEnv'd variable load code
    py = gate_python() if python is None else python
    if k["mode"] in ("start", "start-skip"):  # no pty: it only takes `tailshare-start` (tailshare/start.py)
        grant = "skip" if k["mode"] == "start-skip" else "safe"
        # the plan key (no Face ID) shares the start key's id, so its plans are the pair's, but it only dry-runs
        flags = f"--start={grant} --key={k['id']}" + (" --plan" if plan else "")
        cmd = f"{py} -I -m tailshare ssh-gate {flags}" if py else "/usr/bin/false"
        return f'restrict,command="{cmd}" {k["plan_pubkey"] if plan else k["pubkey"]} {comment}'
    # an interpreter path that is not plain keeps the console script: attaching still works, without -I
    cmd = f"{py} -I -m tailshare ssh-gate" if py else f"{exe} ssh-gate" if exe else "/usr/bin/false"
    return f'restrict,pty,command="{cmd}" {k["pubkey"]} {comment}'


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


def request_purpose(body: dict) -> tuple[str, list[str]]:
    """What a device asks a key for: `pane` (attach, the default) or `start` with want ["start"] or ["start","skip"]."""
    purpose = body.get("purpose", "pane")
    if purpose not in PURPOSES:
        raise bus.BusError("purpose must be pane or start")
    if purpose == "pane":
        return "pane", []
    want = body.get("want", ["start"])
    if not isinstance(want, list) or "start" not in want or not set(want) <= {"start", "skip"}:
        raise bus.BusError('want must be ["start"] or ["start","skip"]')
    return "start", sorted(set(want), key=["start", "skip"].index)


def default_mode(purpose: str, want: list[str]) -> str:
    return ("start-skip" if "skip" in want else "start") if purpose == "start" else "pane"


def mode_problem(mode: str, purpose: str, want: list[str]) -> str:
    """Why a request of `purpose` cannot be installed as `mode`, or "". An attach key never gets a start grant,
    a start key never attaches, and a person can grant less than asked, never more."""
    if mode not in MODES:
        return f"mode must be one of {', '.join(MODES)}"
    if mode not in PURPOSES.get(purpose, ()):
        return f"a {purpose} key cannot be installed as {mode}"
    if mode == "start-skip" and "skip" not in want:
        return "the device did not ask to skip permissions"
    return ""


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


LAUNCHED_ATTACH = 600  # seconds after a start launched it that its pane may be attached to before its agent registers


def launched_panes(db: Path, now: float | None = None) -> list[dict]:
    """Panes `tailshare start` launched here in the last LAUNCHED_ATTACH seconds, on this user's per-agent servers
    (ts-*), as each confirm's result recorded them: so a device can attach while the agent still sits at a first-run
    prompt, before it registers. No new access: once the agent registers, every pane key may attach to it anyway.
    Read straight from start.db, read-only, with the same ownership check as bus.db; [] if there is none."""
    import sqlite3

    from .start import LABEL

    if not db.exists():
        return []
    st = db.stat()
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise OSError(f"{db} must belong to this user and not be writable by others")
    now = time.time() if now is None else now
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute("SELECT id, result FROM plans WHERE launched IS NOT NULL AND launched>? AND result IS NOT NULL",
                           (now - LAUNCHED_ATTACH,)).fetchall()
    finally:
        con.close()
    out = []
    for pid, raw in rows:
        try:
            r = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = bus.clean_tmux(r.get("tmux")) if isinstance(r, dict) and "error" not in r else {}
        if t and Path(t["socket"]).name.startswith(LABEL):
            out.append({"address": f"(started just now, plan {pid[:8]})", "tmux": t})
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


def cmd_gate(args) -> None:
    """Forced command of pane keys: attach to a registered agent pane, or refuse. Of start keys: start sessions."""
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    if getattr(args, "start", ""):
        from . import start

        if cmd.strip() != "tailshare-start":
            sys.exit("tailshare ssh-gate: this key may only run `tailshare-start`, with the request on stdin")
        # never from the environment: only the authorized_keys line itself may move these (tests do)
        start.settle(args.state, args.bus_port, args.tmux_socket)
        sys.exit(start.gate(args.start, getattr(args, "key", ""), plan_only=args.plan))
    try:
        panes = registered_panes(bus.STATE_DIR / "bus.db")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"tailshare ssh-gate: cannot read the registered panes ({e})")
    try:
        panes += launched_panes(bus.STATE_DIR / "start.db")
    except Exception as e:  # noqa: BLE001  (registered panes still work; just-started ones wait for their agent)
        log(f"ssh-gate: cannot read the started panes ({e})")
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
                 ttl: int = APPROVAL_TTL, squads: bool = False, machine: bool = False) -> str:
    """The exact text a person signs to install a key on `hosts` (`squads`: its plan key may sign squad edits).
    `machine`: signed by the origin's machine key, not a person's security key (`tailshare keys trust-machine`)."""
    from .sessions import canonical

    now = time.time()
    return canonical({
        "v": 1, "action": "ssh-key", "signer": signer, "origin": origin, "request": r["id"], "key_id": key_id,
        "pubkey": r["pubkey"], "fingerprint": r["fingerprint"], "device": r["device"], "node": r["node"],
        "user": r["user"], "mode": mode, "hosts": hosts, "nonce": secrets.token_hex(16),
        **({"plan_pubkey": r["plan_pubkey"], "plan_fingerprint": fingerprint(r["plan_pubkey"])}
           if mode in ("start", "start-skip") else {}),
        **({"squads": True} if squads else {}),
        **({"by": "machine"} if machine else {}),
        "issued": round(now, 3), "expires": round(now + ttl, 3),
    })


class Keys:
    def __init__(self, b: bus.Bus, signers: Path | None = None, keys_file: Path | None = None,
                 token_file: Path | None = None, machine_key: Path | None = None, trusted: Path | None = None) -> None:
        from .sessions import SIGNERS

        self.bus, self.store = b, b.store
        self.signers = signers or SIGNERS
        self.machine_key = machine_key or machine_key_path()
        self.trusted = trusted or trusted_machines_path()
        self._key_lock = threading.Lock()
        self.keys_file = keys_file  # None: ~/.ssh/authorized_keys
        self.token = load_token(token_file or token_path(), create=True)
        self._verified: dict[str, list[float]] = {}  # address -> times of its recent signature checks
        with self.store.lock:
            self.store.db.executescript(SCHEMA)
            cols = {r[1] for r in self.store.db.execute("PRAGMA table_info(key_requests)")}
            for col, decl in (("purpose", "TEXT NOT NULL DEFAULT 'pane'"), ("want", "TEXT NOT NULL DEFAULT '[]'"),
                              ("plan_pubkey", "TEXT NOT NULL DEFAULT ''")):
                if col not in cols:
                    self.store.db.execute(f"ALTER TABLE key_requests ADD COLUMN {col} {decl}")
            have = {r[1] for r in self.store.db.execute("PRAGMA table_info(ssh_keys)")}
            if "plan_pubkey" not in have:
                self.store.db.execute("ALTER TABLE ssh_keys ADD COLUMN plan_pubkey TEXT NOT NULL DEFAULT ''")
            if "squads" not in have:  # 1: the plan key may sign squad edits (docs/squads.md), granted at approval
                self.store.db.execute("ALTER TABLE ssh_keys ADD COLUMN squads INTEGER NOT NULL DEFAULT 0")

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="keys-loop").start()

    def _loop(self) -> None:
        while True:
            time.sleep(60)
            try:
                self.retry_revocations()
            except Exception as e:  # noqa: BLE001
                log(f"revocations: {e!r}")
            try:
                self.retry_installs()
            except Exception as e:  # noqa: BLE001
                log(f"installs: {e!r}")

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

    # -- machine trust (docs/ssh-keys.md#machine-trust) ----------------------------
    def machine_pub(self) -> str:
        """This machine's public machine key, made on first use (mode 0600, by ssh-keygen)."""
        from .sessions import ssh_keygen

        with self._key_lock:
            pub = Path(str(self.machine_key) + ".pub")
            if not self.machine_key.exists():
                self.machine_key.parent.mkdir(parents=True, exist_ok=True)
                for f in (self.machine_key, pub):
                    if os.path.lexists(f):
                        os.unlink(f)
                cp = subprocess.run([ssh_keygen(), "-q", "-t", "ed25519", "-N", "", "-C",
                                     f"tailshare machine key of {self.host}", "-f", str(self.machine_key)],
                                    capture_output=True, stdin=subprocess.DEVNULL)
                if cp.returncode != 0:
                    raise bus.BusError(f"ssh-keygen could not make {self.machine_key}", 500)
                log(f"made the machine key {self.machine_key}")
            return parse_pubkey(pub.read_text())

    def _machine_sign(self, payload: str) -> str:
        from .sessions import KEYS_NAMESPACE, sign

        self.machine_pub()
        return sign(payload, self.machine_key, KEYS_NAMESPACE)

    def machine_info(self, ip: str) -> dict:
        """GET /keys/machine, for the owner's other machines: the key to trust, and which machines this one trusts
        (so `trust-machine` can say when an edge goes both ways)."""
        if self._owner_problem():
            raise bus.BusError(self._owner_problem(), 403)
        self.bus.require_same_user(ip)
        pub = self.machine_pub()
        return {"host": self.host, "pubkey": pub, "fingerprint": fingerprint(pub),
                "trusts": [t["host"] for t in read_trusted(self.trusted)]}

    def trusted_view(self) -> dict:
        pub = self.machine_pub()
        return {"host": self.host, "fingerprint": fingerprint(pub), "trusted": read_trusted(self.trusted)}

    def peer_machine(self, body: dict) -> dict:
        """Fetch machine `host`'s key for `tailshare keys trust-machine` (the person then checks its fingerprint)."""
        host = str(body.get("host") or "")
        if not _HOST_RE.fullmatch(host) or host == self.host:
            raise bus.BusError("name another of your machines")
        if self._owner_problem():
            raise bus.BusError(self._owner_problem(), 409)
        p = self._own_peer(host)
        if not p:
            raise bus.BusError(f"{host} is offline, or not a machine of {self.bus.net.user}", 404)
        d = bus.request(f"http://{p['ip']}:{p['port']}/bus/v1/keys/machine", timeout=10)
        pub = parse_pubkey(d.get("pubkey"))
        if d.get("host") != host or not pub.startswith(MACHINE_KEY_TYPE + " "):
            raise bus.BusError(f"{host} answered with something that is not its machine key", 502)
        trusts = [h for h in d.get("trusts") or [] if isinstance(h, str)]
        return {"host": host, "pubkey": pub, "fingerprint": fingerprint(pub), "trusts_us": self.host in trusts}

    def trust(self, body: dict) -> dict:
        """Add the edge host -> this machine: approvals signed by host's machine key install keys here."""
        host, confirm = str(body.get("host") or ""), str(body.get("confirm") or "")
        pub = parse_pubkey(body.get("pubkey"))
        if not _HOST_RE.fullmatch(host) or host == self.host:
            raise bus.BusError("name another of your machines")
        if not pub.startswith(MACHINE_KEY_TYPE + " "):
            raise bus.BusError(f"a machine key is {MACHINE_KEY_TYPE}")
        if confirm != fingerprint(pub)[-CONFIRM_CHARS:]:
            raise bus.BusError(f"confirm must be the last {CONFIRM_CHARS} characters of {host}'s machine key fingerprint", 403)
        with self._key_lock:
            rows = [t for t in read_trusted(self.trusted) if t["host"] != host and t["pubkey"] != pub]
            write_trusted(self.trusted, [*rows, {"host": host, "pubkey": pub}])
        log(f"now trusts {host}'s machine key {fingerprint(pub)}")
        return {"host": self.host, "trusts": host, "fingerprint": fingerprint(pub)}

    def untrust(self, body: dict) -> dict:
        host = str(body.get("host") or "")
        with self._key_lock:
            rows = read_trusted(self.trusted)
            keep = [t for t in rows if t["host"] != host]
            if len(keep) != len(rows):
                write_trusted(self.trusted, keep)
        if len(keep) != len(rows):
            log(f"no longer trusts {host}'s machine key")
        return {"host": self.host, "removed": len(keep) != len(rows)}

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
                "control_zoom": 1,  # ... and zooms a split window on the pane while it sizes
                "control_still": 1}  # ... and a client that does not size never switches the session's window


    # -- requests from devices --------------------------------------------------
    def _view(self, r) -> dict:
        grant = {}
        if r["purpose"] == "start" and r["status"] == "approved":  # a hint for the app; the 403 no_grant decides
            k = self.store.one("SELECT squads FROM ssh_keys WHERE id=?", r["key_id"])
            grant = {"squads": bool(k and k["squads"])}
        return {"request_id": r["id"], "fingerprint": r["fingerprint"], "host": self.host, "login": bus.LOGIN, **grant,
                **({"pair_fingerprint": pair_fingerprint(r["pubkey"], r["plan_pubkey"])} if r["plan_pubkey"] else {}),
                "owner": self.bus.net.user, "status": "pending" if r["status"] == "approving" else r["status"],
                "expires": r["expires"], "purpose": r["purpose"], "want": json.loads(r["want"]),
                "key_id": r["key_id"], "installed": json.loads(r["installed"]),
                "not_installed": {q["host"]: q["error"] or "not installed" for q in self.store.q(
                    "SELECT host, error FROM key_installs WHERE key_id=? AND done IS NULL ORDER BY host", r["key_id"])}
                if r["key_id"] else {}}

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
        purpose, want = request_purpose(body)
        plan = ""
        if purpose == "start":  # a start key comes with its plan key: dry runs without Face ID, confirms with it
            plan = parse_pubkey(body.get("plan_pubkey"))
            if plan == pubkey:
                raise bus.BusError("plan_pubkey must be a different key from pubkey")
            if not all(k.startswith(START_KEY_TYPE + " ") for k in (pubkey, plan)):
                raise bus.BusError(f"start and plan keys must be {START_KEY_TYPE}")
        now = time.time()
        with self.store.lock:
            self._expire()
            same = self.store.one("SELECT * FROM key_requests WHERE ip=? AND pubkey=? AND plan_pubkey=?"
                                  " AND status IN ('pending','approving','approved') ORDER BY created DESC", ip, pubkey, plan)
            if same:
                return self._view(same)
            for k in filter(None, (pubkey, plan)):
                if self.store.one("SELECT 1 FROM ssh_keys WHERE pubkey=? OR plan_pubkey=?", k, k):
                    raise bus.BusError("this key is already installed here", 409)
            pending = self.store.q("SELECT 1 FROM key_requests WHERE ip=? AND status='pending'", ip)
            today = self.store.q("SELECT 1 FROM key_requests WHERE ip=? AND created>?", ip, now - 86400)
            if len(pending) >= MAX_PENDING or len(today) >= MAX_PER_DAY:
                raise bus.BusError("too many key requests from this device; try again later", 429)
            if len(self.store.q("SELECT 1 FROM key_requests WHERE status='pending'")) >= 20:
                raise bus.BusError("too many pending key requests on this machine", 429)
            rid = secrets.token_hex(16)
            self.store.x("INSERT INTO key_requests(id, ip, node, user, device, pubkey, fingerprint, created, expires, status,"
                         " purpose, want, plan_pubkey) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)", rid, ip, node, user or "",
                         device, pubkey, fingerprint(pubkey), now, now + PENDING_TTL, purpose, json.dumps(want), plan)
            r = self.store.one("SELECT * FROM key_requests WHERE id=?", rid)
        log(f"request {rid[:8]} from {node} ({user}), device {device!r}, {r['fingerprint']}, {purpose} {want}")
        self._notify(device, node, user or "")
        return self._view(r)

    def status(self, ip: str, rid: str) -> dict:
        self._expire()
        r = self.store.one("SELECT * FROM key_requests WHERE id=?", rid) if _ID_RE.fullmatch(rid) else None
        if r is None or r["ip"] != ip:
            raise bus.BusError("no such request", 404)
        return self._view(r)

    def lookup(self, ip: str, prefix: str) -> dict:
        """One request by an id prefix, for the owner's other devices: a squad member's key_enroll card names a
        prefix, and the phone showing it checks the request is real here. It shows the request, never approves it."""
        if self._owner_problem():
            raise bus.BusError(self._owner_problem(), 403)
        self.bus.require_same_user(ip)
        if not _PREFIX_RE.fullmatch(prefix):
            raise bus.BusError("bad request id prefix")
        self._expire()
        rows = self.store.q("SELECT * FROM key_requests WHERE id >= ? AND id < ? LIMIT 2", prefix, prefix + "g")  # ids are hex
        if len(rows) != 1 or rows[0]["user"] != self.bus.net.user:  # a card's prefix is an agent's: only the owner's own
            raise bus.BusError("no single request of yours has that prefix", 404)
        r = rows[0]
        return {"request_id": r["id"], "host": self.host, "device_name": r["device"], "node": r["node"],
                "user": r["user"], "ip": r["ip"], "fingerprint": r["fingerprint"], "purpose": r["purpose"],
                "status": "pending" if r["status"] == "approving" else r["status"], "created": r["created"],
                "expires": r["expires"]}

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
                    {**{k: r[k] for k in ("id", "ip", "node", "user", "device", "pubkey", "fingerprint", "created", "expires",
                                          "purpose", "plan_pubkey")}, "want": json.loads(r["want"]),
                     "plan_fingerprint": fingerprint(r["plan_pubkey"]) if r["plan_pubkey"] else "",
                     "pair_fingerprint": pair_fingerprint(r["pubkey"], r["plan_pubkey"]) if r["plan_pubkey"] else ""}
                    for r in rows]}

    def ls(self) -> dict:
        rows = self.store.q("SELECT * FROM ssh_keys ORDER BY installed")
        unfinished = self.store.q("SELECT key_id, host, created, error FROM key_revocations WHERE done IS NULL")
        installing = self.store.q("SELECT key_id, host, created, final, error FROM key_installs WHERE done IS NULL")
        return {"host": self.host, "login": bus.LOGIN, "keys": [dict(r) for r in rows],
                "revoking": [dict(r) for r in unfinished], "installing": [dict(r) for r in installing]}

    # -- approving and installing ---------------------------------------------------
    def _rewrite(self) -> None:
        exe = gate_exe()
        rows = self.store.q("SELECT * FROM ssh_keys ORDER BY installed")
        write_authorized_keys([x for r in rows for x in key_lines(dict(r), exe)], self.keys_file)

    def install_here(self, key_id: str, r: dict, mode: str, by: str, squads: bool = False) -> dict:
        problem = install_problem()
        if problem:
            raise bus.BusError(problem, 409)
        with self.store.lock:
            if self.store.one("SELECT 1 FROM revoked_keys WHERE id=?", key_id):
                raise bus.BusError(f"key {key_id} was revoked; enroll again", 409)
            plan = (r.get("plan_pubkey") or "") if mode in ("start", "start-skip") else ""
            if mode in ("start", "start-skip") and not plan:
                raise bus.BusError("a start key needs its plan key", 409)
            if squads and not plan:
                raise bus.BusError("only a start key's plan key may sign squad edits", 409)
            if mode in ("start", "start-skip") and not all(x.startswith(START_KEY_TYPE + " ") for x in (r["pubkey"], plan)):
                raise bus.BusError(f"start keys must be {START_KEY_TYPE}", 409)
            for k in filter(None, (r["pubkey"], plan)):  # sshd takes the first line that matches a key: one line each
                if self.store.one("SELECT 1 FROM ssh_keys WHERE id!=? AND (pubkey=? OR plan_pubkey=?)", key_id, k, k):
                    raise bus.BusError("this key is already installed here under another id", 409)
            self.store.x("INSERT OR REPLACE INTO ssh_keys(id, pubkey, fingerprint, device, node, user, mode, approved_by,"
                         " installed, plan_pubkey, squads) VALUES(?,?,?,?,?,?,?,?,?,?,?)", key_id, r["pubkey"],
                         r["fingerprint"], r["device"], r["node"], r["user"], mode, by, time.time(), plan, int(squads))
            try:
                self._rewrite()
            except BaseException:
                self.store.x("DELETE FROM ssh_keys WHERE id=?", key_id)
                raise
        log(f"installed {key_id} ({r['device']}, {r['fingerprint']}) mode={mode}{', squads' if squads else ''}, {by}")
        return {"host": self.host, "login": bus.LOGIN, "mode": mode, "key_id": key_id, "squads": bool(squads)}

    def check_approval(self, payload: str, signature: str, sender: str | None = None) -> dict:
        """The approval, if it may install here. `sender`: the machine that sent it (None: a person on this one).
        A person's approval is signed with a security key in allowed_signers. A machine's (`"by": "machine"`)
        is signed with its machine key, accepted only from that machine itself, and only when trusted_machines
        here names it: one direct edge, never relayed by another machine."""
        from .sessions import KEYS_NAMESPACE, verify

        if len(payload) > 16 * 1024 or len(signature) > 8 * 1024:
            raise bus.BusError("approval too large", 413)
        try:
            head = json.loads(payload)
        except json.JSONDecodeError:
            head = None
        if isinstance(head, dict) and head.get("by") == "machine":
            signer = str(head.get("signer") or "")
            if sender is None or signer != sender or head.get("origin") != signer:
                raise bus.BusError("approval refused: a machine's approval is only accepted from that machine", 403)
            if not any(t["host"] == signer for t in read_trusted(self.trusted)):
                raise bus.BusError(f"approval refused: {self.host} does not trust {signer} (a person on {self.host} "
                                   f"can run `tailshare keys trust-machine {signer}`)", 403)
            d = verify(payload, signature, self.trusted, KEYS_NAMESPACE)
        else:
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
        if d.get("mode") in ("start", "start-skip"):
            try:
                plan = parse_pubkey(d.get("plan_pubkey"))
                if plan != d.get("plan_pubkey") or fingerprint(plan) != d.get("plan_fingerprint") or plan == d.get("pubkey"):
                    problems.append("plan key and fingerprint do not match")
                if not all(str(x).startswith(START_KEY_TYPE + " ") for x in (plan, d.get("pubkey"))):
                    problems.append(f"start keys must be {START_KEY_TYPE}")
            except bus.BusError:
                problems.append("bad plan key")
        if d.get("squads", False) is not False and (d.get("squads") is not True or d.get("mode") not in ("start", "start-skip")):
            problems.append("squads is only a start key's grant, and only true")
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
        rid = str(body.get("id") or "")
        self._expire()
        r = self.store.one("SELECT * FROM key_requests WHERE id=? AND status='pending'", rid)
        if r is None:
            raise bus.BusError("no such pending request (it may have expired, or is being approved)", 404)
        mode = str(body.get("mode") or default_mode(r["purpose"], json.loads(r["want"])))
        problem = mode_problem(mode, r["purpose"], json.loads(r["want"]))
        if problem:
            raise bus.BusError(problem)
        if r["purpose"] == "start" and not r["plan_pubkey"]:  # enroll never stores one; never fall back to the start key's code
            raise bus.BusError("this start request has no plan key; ask the device to enroll again", 409)
        tail = confirm_code(r)
        if str(body.get("confirm") or "") != tail:
            raise bus.BusError(f"confirm must be the last {CONFIRM_CHARS} characters of the "
                               f"{'pair ' if r['plan_pubkey'] else ''}fingerprint", 403)
        # the same code, or the same start key with another plan key: one of them is not the device
        others = self.store.q("SELECT fingerprint, pubkey, plan_pubkey FROM key_requests WHERE status='pending' AND id!=?", rid)
        if any(x["pubkey"] == r["pubkey"] and x["plan_pubkey"] != r["plan_pubkey"] for x in others):
            raise bus.BusError("another pending request has the same start key with a different plan key: someone may be "
                               "imitating this device. Deny the requests you do not recognize, then ask the device to "
                               "enroll again", 409)
        if any(confirm_code(x) == tail for x in others):
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
        squads = body.get("squads", False) is True
        payload, signature = str(body.get("payload") or ""), str(body.get("signature") or "")
        if payload:
            d = self.check_approval(payload, signature)
            if (d.get("request") != rid or d["pubkey"] != r["pubkey"] or d["mode"] != mode
                    or d.get("squads", False) is not squads
                    or d.get("plan_pubkey", "") != (r["plan_pubkey"] if mode in ("start", "start-skip") else "")):
                raise bus.BusError("approval refused: signed for another request", 403)
            key_id, hosts, by = d["key_id"], [h for h in d["hosts"] if isinstance(h, str)], f"signed by {d['signer']}"
        else:
            key_id, by = "k" + secrets.token_hex(6), "approved here"
            # the machines that trust this one take its machine key's word; the others answer that they do not
            hosts = [self.host] if body.get("here") is True else self.targets()
        installed = [self.install_here(key_id, r, mode, by, squads)]
        others = [h for h in hosts if h != self.host]
        if others and not payload:
            payload = approval_doc(self.host, self.host, r, key_id, mode, [self.host, *others], squads=squads, machine=True)
            signature = self._machine_sign(payload)
        errors = {}
        for h in others:
            got, error = self._deliver(key_id, h, payload, signature)
            if got:
                installed.append(got)
            else:
                errors[h] = error
        self.store.x("UPDATE key_requests SET status='approved', key_id=?, installed=? WHERE id=?",
                     key_id, json.dumps(installed), rid)
        return {"key_id": key_id, "installed": installed, "errors": errors}

    def _deliver(self, key_id: str, h: str, payload: str, signature: str) -> tuple[dict | None, str]:
        """Send a signed approval to machine h. Not installed: queued in key_installs, to retry (offline, no
        answer) or not (h refused it, e.g. it does not trust this machine), and (None, why)."""
        p = self.bus.net.peers.get(h)
        error, final = "", False
        if p and p.get("user") != self.bus.net.user or self._owner_problem():
            error, final = "not a machine of this owner", True
        elif not self._own_peer(h):
            error = "offline: will retry"
        else:
            try:
                got = bus.request(f"http://{p['ip']}:{p['port']}/bus/v1/keys/install",
                                  {"v": bus.PROTOCOL, "payload": payload, "signature": signature}, timeout=20)
                if got.get("host") != h:
                    raise bus.BusError(f"answered as {got.get('host')!r}", 502)
                self.store.x("INSERT OR REPLACE INTO key_installs(key_id, host, created, done, final, error)"
                             " VALUES(?,?,?,?,0,NULL)", key_id, h, time.time(), time.time())
                return got, ""
            except bus.BusError as e:
                error, final = str(e)[:200], 400 <= e.code < 500  # h said no: asking again changes nothing
            except Exception as e:  # noqa: BLE001
                error = f"{str(e)[:150]}: will retry"
        with self.store.lock:
            was = self.store.one("SELECT created FROM key_installs WHERE key_id=? AND host=?", key_id, h)
            self.store.x("INSERT OR REPLACE INTO key_installs(key_id, host, created, done, final, error)"
                         " VALUES(?,?,?,NULL,?,?)", key_id, h, was["created"] if was else time.time(), int(final), error)
        return None, error

    def retry_installs(self) -> dict:
        """Approvals for machines that were offline: signed again with the machine key (the first signature,
        a person's or not, has expired by then), so only machines that trust this one take them."""
        now, out = time.time(), {}
        for q in self.store.q("SELECT * FROM key_installs WHERE done IS NULL AND final=0"):
            key_id, h = q["key_id"], q["host"]
            k = self.store.one("SELECT * FROM ssh_keys WHERE id=?", key_id)
            r = self.store.one("SELECT * FROM key_requests WHERE key_id=? AND status='approved'", key_id)
            if k is None or r is None:  # removed here meanwhile: the approval is gone with it
                self.store.x("DELETE FROM key_installs WHERE key_id=? AND host=?", key_id, h)
                continue
            if now - q["created"] > INSTALL_RETRY_FOR:
                self.store.x("UPDATE key_installs SET final=1, error=? WHERE key_id=? AND host=?",
                             f"gave up after {INSTALL_RETRY_FOR // 86400} days offline: approve it there", key_id, h)
                continue
            if not self._own_peer(h):
                continue
            payload = approval_doc(self.host, self.host, dict(r), key_id, k["mode"], [h], squads=bool(k["squads"]),
                                   machine=True)
            got, error = self._deliver(key_id, h, payload, self._machine_sign(payload))
            if got:
                with self.store.lock:
                    row = self.store.one("SELECT installed FROM key_requests WHERE id=?", r["id"])
                    self.store.x("UPDATE key_requests SET installed=? WHERE id=?",
                                 json.dumps([*json.loads(row["installed"]), got]), r["id"])
                log(f"installed {key_id} on {h}, which was offline at approval")
            out[h] = "installed" if got else error
        return out

    def install_from_peer(self, ip: str, body: dict) -> dict:
        if self._owner_problem():
            raise bus.BusError(self._owner_problem(), 403)
        self.bus.require_same_user(ip)
        d = self.check_approval(str(body.get("payload") or ""), str(body.get("signature") or ""),
                                sender=self.bus.net.host_for_ip(ip) or "")
        r = {k: d.get(k, "") for k in ("pubkey", "fingerprint", "device", "node", "user", "plan_pubkey")}
        by = f"trusted machine {d['signer']}" if d.get("by") == "machine" else f"signed by {d['signer']} (from {d.get('origin')})"
        return self.install_here(d["key_id"], r, d["mode"], by, d.get("squads") is True)

    def deny(self, body: dict) -> dict:
        n = self.store.x("UPDATE key_requests SET status='denied' WHERE id=? AND status='pending'", str(body.get("id")))
        if not n:
            raise bus.BusError("no such pending request", 404)
        return {"denied": True}

    # -- signed writes from devices ------------------------------------------------------
    def check_signed(self, method: str, path: str, raw: bytes, headers: dict, owner: str, ip: str = "") -> dict:
        """A device's signed write (docs/squads.md): signed with the plan key of one of `owner`'s start keys
        installed here with the squads grant, for this machine, this method, path and body, recently and once. The
        pane key (a file in iSH, say) and the start key (Face ID on every save) never sign one. Returns the key's
        row; refuses with 403."""
        from .sessions import ssh_keygen

        def no(reason: str, why: str, **more) -> bus.BusError:
            return bus.BusError("signature", 403, {"reason": reason, "detail": why, **more})

        key_id, ts, nonce, sig = (str(headers.get(h) or "") for h in SIGNED_HEADERS)
        if not (key_id or ts or nonce or sig):
            raise no("unsigned", "squad writes from a device must be signed with one of its keys")
        if not (_KEY_ID_RE.fullmatch(key_id) and re.fullmatch(r"[0-9]{1,12}", ts) and re.fullmatch(r"[0-9a-f]{32}", nonce)
                and re.fullmatch(r"[A-Za-z0-9+/]{20,4096}={0,2}", sig)):
            raise no("bad_signature", "malformed signature headers")
        now = time.time()
        if abs(now - int(ts)) > SIGNED_WINDOW:
            raise no("expired", f"the request's time is more than {SIGNED_WINDOW} s from this machine's clock",
                     now=int(now))  # so a device with a wrong clock can correct once
        if self.store.one("SELECT 1 FROM revoked_keys WHERE id=?", key_id):
            raise no("revoked", f"key {key_id} was removed from {self.host}")
        row = self.store.one("SELECT * FROM ssh_keys WHERE id=?", key_id)
        if row is None or not owner or row["user"] != owner:
            raise no("unknown_key", f"no key {key_id} of {owner or 'the owner'} is installed on {self.host}")
        if not row["squads"] or not row["plan_pubkey"]:
            raise no("no_grant", f"key {key_id} was not granted squad edits on {self.host}: grant it there with "
                     f"`tailshare keys grant-squads {key_id}`")
        with self.store.lock:
            recent = [t for t in self._verified.get(ip, []) if t > now - 60]
            if len(recent) >= VERIFY_PER_MINUTE:
                raise bus.BusError("signature", 429, {"reason": "rate_limited", "detail": "too many signed writes from "
                                   "this device in the last minute; try again shortly"})
            self._verified[ip] = [*recent, now]
            for a in [a for a, ts in self._verified.items() if not ts or ts[-1] <= now - 60]:
                del self._verified[a]
        message = "\n".join(["tailshare-squad-write/1", self.host, method, path, ts, nonce,
                              hashlib.sha256(raw).hexdigest()])
        armored = "-----BEGIN SSH SIGNATURE-----\n" + "\n".join(sig[i : i + 70] for i in range(0, len(sig), 70)) \
            + "\n-----END SSH SIGNATURE-----\n"
        with tempfile.TemporaryDirectory() as tmp:
            signers, sigfile = Path(tmp) / "signers", Path(tmp) / "sig"
            signers.write_text(f'{key_id} namespaces="{SQUADS_NAMESPACE}" {row["plan_pubkey"]}\n')
            sigfile.write_text(armored)
            cp = subprocess.run([ssh_keygen(), "-Y", "verify", "-f", str(signers), "-I", key_id, "-n", SQUADS_NAMESPACE,
                                 "-s", str(sigfile)], input=message.encode(), capture_output=True, timeout=30)
        if cp.returncode != 0:
            raise no("bad_signature", "the signature does not match this request on this machine with that key")
        with self.store.lock:
            self.store.x("DELETE FROM key_nonces WHERE expires<?", now)
            if self.store.one("SELECT 1 FROM key_nonces WHERE nonce=?", "sq:" + nonce):
                raise no("replayed", "this nonce was already used: sign the write again")
            self.store.x("INSERT INTO key_nonces(nonce, expires) VALUES(?,?)", "sq:" + nonce, int(ts) + 2 * SIGNED_WINDOW)
        return dict(row)

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

    def grant_squads(self, body: dict) -> dict:
        """`tailshare keys grant-squads`: give an installed start key's plan key squad edits here (or take them back
        with on=false), for a key approved without them. Granting takes the same code as approving: the end of the
        pair fingerprint the device shows. This machine only; each machine keeps its own grant."""
        key_id, on = str(body.get("key_id") or ""), body.get("on", True)
        if not _KEY_ID_RE.fullmatch(key_id) or not isinstance(on, bool):
            raise bus.BusError("bad key id")
        k = self.store.one("SELECT * FROM ssh_keys WHERE id=?", key_id)
        if k is None:
            raise bus.BusError(f"no key {key_id} is installed on {self.host}", 404)
        if on:
            if k["mode"] not in ("start", "start-skip") or not k["plan_pubkey"]:
                raise bus.BusError("squad edits are only a start key's grant: its plan key signs them", 409)
            if str(body.get("confirm") or "") != confirm_code(k):
                raise bus.BusError(f"confirm must be the last {CONFIRM_CHARS} characters of the pair fingerprint", 403)
        self.store.x("UPDATE ssh_keys SET squads=? WHERE id=?", int(on), key_id)
        log(f"{'granted' if on else 'took back'} squad edits for {key_id} ({k['device']}, {k['fingerprint']})")
        return {"host": self.host, "key_id": key_id, "squads": on}

    def rm(self, body: dict) -> dict:
        """Called by `tailshare keys rm`: remove here, then on the owner's other machines."""
        key_id = str(body.get("key_id") or "")
        if not _KEY_ID_RE.fullmatch(key_id):
            raise bus.BusError("bad key id")
        removed = self.remove_here(key_id)
        self.store.x("DELETE FROM key_installs WHERE key_id=? AND done IS NULL", key_id)  # stop installing it
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
                      ("POST", "/keys/deny"): lambda: self.deny(body), ("POST", "/keys/rm"): lambda: self.rm(body),
                      ("POST", "/keys/grant-squads"): lambda: self.grant_squads(body),
                      ("GET", "/keys/trusted"): lambda: self.trusted_view(),
                      ("POST", "/keys/peer-machine"): lambda: self.peer_machine(body),
                      ("POST", "/keys/trust"): lambda: self.trust(body),
                      ("POST", "/keys/untrust"): lambda: self.untrust(body)}
        else:
            if method == "GET" and path.startswith("/keys/request/"):
                return self.lookup(ip, path.removeprefix("/keys/request/"))
            if method == "GET" and path.startswith("/keys/enroll/"):
                return self.status(ip, path.removeprefix("/keys/enroll/"))
            if method == "POST" and path.startswith("/keys/enroll/") and path.endswith("/cancel"):
                return self.cancel(ip, path.removeprefix("/keys/enroll/").removesuffix("/cancel"))
            routes = {("POST", "/keys/enroll"): lambda: self.enroll(ip, body),
                      ("POST", "/keys/install"): lambda: self.install_from_peer(ip, body),
                      ("POST", "/keys/revoke"): lambda: self.revoke(ip, body),
                      ("GET", "/keys/machine"): lambda: self.machine_info(ip)}
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
        kind = "" if p.get("purpose", "pane") == "pane" else "   start key" + (", wants skip" if "skip" in p.get("want", []) else "")
        print(f"{p['id'][:8]}  {p['device']!r} on {p['node']} ({who}){other}{kind}")
        print(f"          {_requested(p)}")
        print(f"          {p['fingerprint']}  expires in {max(0, int(p['expires'] - time.time())) // 60} min")
    print("\napprove: tailshare keys approve <id>     deny: tailshare keys deny <id>")


def _requested(p: dict) -> str:
    """When and from where a request came, so the person can match it to the device in their hand."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(p["created"])) if p.get("created") else "at an unknown time"
    return f"{when} from {p.get('ip') or 'an unknown address'}, by {p.get('user') or 'an unknown tailnet user'}"


@_run
def cmd_approve(args):
    approve_interactive(args.id, args.shell, args.here, args.key, args.signer, start=args.start,
                        squads=True if args.squads else None)


def approve_interactive(rid: str, shell: bool = False, here: bool = False, key_arg: str = "", signer_arg: str = "",
                        tries: int = 1, skip_ok: bool = False, start: str = "", squads: bool | None = None) -> bool:
    """Show a pending request and install it if the person types the fingerprint's end. Returns whether a key
    was installed (False only when skip_ok and the person pressed Enter)."""
    from . import sessions, sessions_cli

    _human_check()
    d = _call("/keys/pending")
    if d.get("problem"):
        raise ValueError(d["problem"])
    p = _find(d["pending"], rid)
    purpose, want = p.get("purpose", "pane"), p.get("want", [])
    if purpose == "start":
        if shell:
            raise ValueError("this device asked for a start key; --shell is for attach keys")
        mode = {"safe": "start", "skip": "start-skip"}.get(start) or default_mode(purpose, want)
    elif start:
        raise ValueError("this device asked for an attach key; it cannot get a start grant (the device enrolls a "
                         "separate start key for that)")
    else:
        mode = "shell" if shell else "pane"
    problem = mode_problem(mode, purpose, want)
    if problem:
        raise ValueError(problem)
    if squads and mode not in ("start", "start-skip"):
        raise ValueError("--squads is for a start key: its plan key signs the squad edits")
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
    hosts = d["targets"] if key or not here else [d["host"]]
    mine = p["user"] == d["owner"]
    print("You are approving an SSH key:")
    print(f"  device       {p['device']!r} (the name the device gave itself)")
    print(f"  tailnet      {p['node']}, owned by {p['user'] or 'unknown'}" + ("" if mine else "   ⚠ NOT one of your devices"))
    print(f"  requested    {_requested(p)}")
    print(f"  fingerprint  {p['fingerprint']}")
    if p.get("plan_fingerprint"):
        print(f"  plan key     {p['plan_fingerprint']} (dry runs only, without Face ID; installed with it)")
        print(f"  pair         {p['pair_fingerprint']} (both keys: the device shows this one)")
    if mode == "pane":
        print(f"  access       attach to tmux panes of agents, as {d['login']}. Typing into a pane is as good as a shell.")
    elif mode in ("start", "start-skip"):
        print(f"  access       START agent sessions as {d['login']}, in the folders and kinds ~/.tailshare/start.json "
              "allows (starting stays off where it has none)")
        if mode == "start-skip":
            print("               and may SKIP PERMISSIONS: the agent then runs any code without asking")
        elif "skip" in want:
            print("               (the device also asked to skip permissions; not granted: add --start=skip for that)")
    else:
        print(f"  access       a full shell as {d['login']}")
    if key or here:
        print(f"  machines     {', '.join(hosts)}" + ("" if key else "   (--here: this machine only)"))
    else:
        print(f"  machines     {d['host']}, and those of {', '.join(hosts[1:]) or 'your other machines'} that trust "
              f"{d['host']} (`tailshare keys trust-machine {d['host']}` there)")
    want = confirm_code(p)
    which = "pair fingerprint" if p.get("plan_pubkey") else "fingerprint"
    skip = "skip" if skip_ok else "cancel"
    for attempt in range(tries):
        got = input(f"Type the last {CONFIRM_CHARS} characters of the {which} shown on the device (Enter to {skip}): ").strip()
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
        raise ValueError(f"does not match the {which}: nothing installed")
    if squads is None and mode in ("start", "start-skip"):
        squads = input("Let its plan key (no Face ID) also edit your squads from the app? [y/N]: ").strip().lower() in ("y", "yes")
    body = {"id": p["id"], "mode": mode, "confirm": got, "squads": bool(squads), "here": bool(here)}
    if key:
        signer = sessions_cli._principal(signer_arg)
        payload = approval_doc(signer, d["host"], p, "k" + secrets.token_hex(6), mode, hosts, squads=bool(squads))
        body.update(payload=payload, signature=sessions.sign(payload, key, sessions.KEYS_NAMESPACE))
    r = _call("/keys/approve", body, timeout=90)
    for i in r["installed"]:
        print(f"✓ installed on {i['host']} as {i['login']} ({i['mode']}{', squad edits' if i.get('squads') else ''})")
    for h, e in r["errors"].items():
        print(f"✗ not on {h}: {e}")
    print(f"key id {r['key_id']}; remove it with `tailshare keys rm {r['key_id']}`")
    return True


@_run
def cmd_machine_key(args):
    d = _call("/keys/trusted")
    print(f"{d['host']}'s machine key: {d['fingerprint']}")
    print(f"To let another of your machines take {d['host']}'s approvals, run there: tailshare keys trust-machine {d['host']}")


@_run
def cmd_trust_machine(args):
    """The edge X -> this machine: a person here checks X's key and types the end of its fingerprint."""
    _human_check()
    m = _call("/keys/peer-machine", {"host": args.host})
    here = _call("/keys/trusted")["host"]
    print(f"You are letting {m['host']} install SSH keys on {here}:")
    print(f"  machine key  {m['fingerprint']} (`tailshare keys machine-key` on {m['host']} shows it)")
    print(f"  means        anything that can read {m['host']}'s machine key (~/.tailshare/machine_key there), an agent")
    print(f"               running on {m['host']} included, can get a shell on {here}, with no person approving")
    print(f"  not          {here}'s approvals going to {m['host']}: that is `tailshare keys trust-machine {here}` "
          f"on {m['host']}")
    if m.get("trusts_us"):
        print(f"⚠ {m['host']} already trusts {here}: with this, each one can install keys on the other, so either one "
              "taken over gives the other away too.")
        if input("Trust both ways? [y/N]: ").strip().lower() not in ("y", "yes"):
            raise ValueError("cancelled: nothing changed")
    got = input(f"Type the last {CONFIRM_CHARS} characters of the fingerprint {m['host']} shows (Enter to cancel): ").strip()
    if not got:
        raise ValueError("cancelled: nothing changed")
    if got != m["fingerprint"][-CONFIRM_CHARS:]:
        raise ValueError("does not match the fingerprint: nothing changed")
    _call("/keys/trust", {"host": m["host"], "pubkey": m["pubkey"], "confirm": got})
    print(f"✓ {here} now takes key approvals from {m['host']} (undo: tailshare keys untrust-machine {m['host']})")


@_run
def cmd_untrust_machine(args):
    r = _call("/keys/untrust", {"host": args.host})
    print(f"✓ {r['host']} no longer takes approvals from {args.host}" if r["removed"] else f"{r['host']} did not trust {args.host}")


@_run
def cmd_trusted(args):
    d = _call("/keys/trusted")
    if args.json:
        print(json.dumps(d, indent=1))
        return
    print(f"{d['host']}'s machine key: {d['fingerprint']}")
    if not d["trusted"]:
        print(f"{d['host']} takes key approvals from no other machine")
    for t in d["trusted"]:
        print(f"  {t['host']:<20} {t['fingerprint']}   may install keys on {d['host']}")


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
        print(f"{k['id']}  {k['mode'] + ('+squads' if k.get('squads') else ''):<17}  {k['device']!r} on {k['node']} ({k['user']})  {k['fingerprint']}  {when}, {k['approved_by']}")
    for r in d.get("installing", []):
        print(f"⚠ {r['key_id']} is not installed on {r['host']}" + ("" if r["final"] else ", retrying") +
              (f" ({r['error']})" if r["error"] else ""))
    for r in d.get("revoking", []):
        print(f"⚠ {r['key_id']} is still installed on {r['host']}: removal pending since "
              f"{time.strftime('%Y-%m-%d', time.localtime(r['created']))}" + (f" ({r['error']})" if r["error"] else ""))


@_run
@_run
def cmd_grant_squads(args):
    if args.off:
        r = _call("/keys/grant-squads", {"key_id": args.key_id, "on": False})
        print(f"✓ {r['key_id']} may no longer edit squads on {r['host']}")
        return
    _human_check()
    d = _call("/keys/ls")
    k = next((k for k in d["keys"] if k["id"] == args.key_id), None)
    if k is None:
        raise ValueError(f"no key {args.key_id} is installed on {d['host']} (`tailshare keys ls` lists them)")
    if k["mode"] not in ("start", "start-skip") or not k.get("plan_pubkey"):
        raise ValueError("squad edits are only a start key's grant: its plan key signs them")
    if k.get("squads"):
        print(f"{k['id']} may already edit squads on {d['host']}")
        return
    print(f"You are letting a start key's plan key edit your squads on {d['host']}:")
    print(f"  device       {k['device']!r} on {k['node']} ({k['user']})")
    print(f"  pair         {pair_fingerprint(k['pubkey'], k['plan_pubkey'])} (the device shows this one)")
    print("  means        the plan key, which signs without Face ID, may add, change and delete squads and their members")
    print(f"  only         {d['host']}: run this on each machine the squads should be edited on")
    got = input(f"Type the last {CONFIRM_CHARS} characters of the pair fingerprint shown on the device (Enter to cancel): ").strip()
    if not got:
        raise ValueError("cancelled: nothing changed")
    r = _call("/keys/grant-squads", {"key_id": k["id"], "confirm": got})
    print(f"✓ {r['key_id']} may edit squads on {r['host']} (undo: tailshare keys grant-squads --off {r['key_id']})")


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
    p.add_argument("--start", choices=("safe", "skip"), default="",
                   help="for a start key: grant starting sessions (safe), or also skipping permissions (skip)")
    p.add_argument("--squads", action="store_true",
                   help="for a start key: its plan key may also edit your squads from the app (asked otherwise)")
    p.add_argument("--here", action="store_true", help="only this machine, even with a signing key or trusting machines")
    p.add_argument("--key", default="", help="signing key (default: TAILSHARE_SIGN_KEY or ~/.ssh/id_*_sk)")
    p.add_argument("--as", dest="signer", default="", help="signer name (default: your tailnet user)")
    p.set_defaults(fn=cmd_approve)
    p = s.add_parser("deny", help="refuse a key request")
    p.add_argument("id")
    p.set_defaults(fn=cmd_deny)
    p = s.add_parser("ls", help="keys installed here by tailshare")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ls)
    p = s.add_parser("grant-squads", help="let an installed start key's plan key edit your squads here")
    p.add_argument("key_id")
    p.add_argument("--off", action="store_true", help="take the grant back")
    p.set_defaults(fn=cmd_grant_squads)
    p = s.add_parser("rm", help="remove a key here and on your other machines")
    p.add_argument("key_id")
    p.set_defaults(fn=cmd_rm)
    p = s.add_parser("trust-machine", help="let machine HOST's approvals install keys here (one direction, no relaying)")
    p.add_argument("host")
    p.set_defaults(fn=cmd_trust_machine)
    p = s.add_parser("untrust-machine", help="stop taking approvals from machine HOST")
    p.add_argument("host")
    p.set_defaults(fn=cmd_untrust_machine)
    p = s.add_parser("trusted", help="the machines whose approvals install keys here")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_trusted)
    p = s.add_parser("machine-key", help="this machine's key fingerprint, to check from `trust-machine`")
    p.set_defaults(fn=cmd_machine_key)
    p = sub.add_parser("ssh-gate", help="forced command of tailshare's SSH keys (run by sshd, not by hand)")
    p.add_argument("--start", choices=("safe", "skip"), default="", help="set by a start key's line")
    p.add_argument("--key", default="", help="set by a start key's line")
    p.add_argument("--plan", action="store_true", help="set by a plan key's line: dry runs only")
    p.add_argument("--tmux-socket", default="", help="tests only: a private tmux server, set on the key's line")
    p.add_argument("--state", default="", help="tests only: a state dir instead of ~/.tailshare, set on the key's line")
    p.add_argument("--bus-port", type=int, default=0, help="tests only: the local bus port, set on the key's line")
    p.set_defaults(fn=cmd_gate)
