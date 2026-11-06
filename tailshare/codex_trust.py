"""Whether this machine's Codex trusts tailshare's hooks, and trusting them from the owner's device.

Codex runs a hook from ~/.codex/hooks.json only once it is trusted: `[hooks.state."<key>"] trusted_hash` in
config.toml, which Codex itself writes (its /hooks screen, or its app-server's `config/batchWrite`). A Codex whose
tailshare hooks are not trusted never joins the bus. The owner's devices see that here, read-only, in /hello's
`codex_hooks`; with the start key (Face ID) a device may trust them, through Codex's own app-server, and only
tailshare's: a hook whose command is not exactly one tailshare installs is never trusted (docs/codex.md, "Trusting
the hooks"). A Codex without the app-server calls gets the /hooks fix instead.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
TTL = 60  # seconds before the inputs are looked at again: /hello must not start Codex on every call
MAX_AGE = 600  # and Codex itself is asked again only when they changed, or this long after it last was
TIMEOUT = 20
NOT_INSTALLED = {"state": "not_installed",
                 "fix": {"action": "terminal", "reason": "run `tailshare agent install-codex` on this machine"}}
REINSTALL_FIX = {"action": "terminal", "reason": "tailshare's Codex hooks here do not run an installed tailshare: "
                 "install the tool (uv tool install) and run `tailshare agent install-codex` again"}
TUI_FIX = {"action": "codex_hooks_tui", "reason": "open /hooks in Codex on this machine and trust tailshare's hooks"}

_lock = threading.Lock()
_cache: tuple[float, dict | None] = (0.0, None)
_busy = False


class Unsupported(Exception):
    """This Codex has no app-server, or it lacks hooks/list or config/batchWrite: the fix is /hooks."""


def _codex() -> str | None:
    """Codex as starting would run it: start.json's `bin` or the fixed search path, only a file this user or root
    can change (start.binary)."""
    from . import start

    cfg = start.load_config() or {}
    try:
        return start.binary("codex", {"kinds": {"codex": (cfg.get("kinds") or {}).get("codex") or {}}})
    except start.Refused:
        return None


def _version(codex: str) -> str:
    try:
        out = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL, env=_env(codex), check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"\d+\.\d+\.\d+\S*", out)
    return m.group(0) if m else ""


def _env(codex: str) -> dict:
    from .sessions import _env as env

    return {**env(codex), "CODEX_HOME": str(CODEX_HOME)}


class AppServer:
    """`codex app-server` over stdio (JSON-RPC, one JSON object a line). No thread is ever started."""

    def __init__(self, codex: str):
        self.p = subprocess.Popen([codex, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, env=_env(codex), cwd=str(Path.home()),
                                  start_new_session=True)  # its own group: the launcher and the binary it runs
        self.n = 0
        self.reap = threading.Lock()  # a kill and the reaping wait never overlap
        self.deadline = time.monotonic() + TIMEOUT
        self.timer = threading.Timer(TIMEOUT, self._expire)
        self.timer.daemon = True
        self.timer.start()
        try:
            self.call("initialize", {"clientInfo": {"name": "tailshare", "version": _tailshare_version()}})
            self._send({"jsonrpc": "2.0", "method": "initialized"})
        except BaseException:
            self.close()
            raise

    def _send(self, msg: dict) -> None:
        assert self.p.stdin is not None
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def call(self, method: str, params: dict) -> dict:
        self.n += 1
        try:
            self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
            assert self.p.stdout is not None
            for line in self.p.stdout:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == self.n:
                    if "error" in msg:
                        err = msg["error"] if isinstance(msg["error"], dict) else {}
                        if err.get("code") == -32601:  # method not found: an older Codex
                            raise Unsupported(method)
                        raise RuntimeError(f"codex {method}: {str(err.get('message', 'failed'))[:200]}")
                    if not isinstance(msg.get("result"), dict):
                        raise RuntimeError(f"codex {method}: no result")
                    return msg["result"]
        except (OSError, ValueError) as e:  # a closed pipe: it exited, or the timer ended it
            raise RuntimeError(f"codex app-server: {e}") from None
        raise Unsupported(method) if time.monotonic() < self.deadline else RuntimeError("codex app-server timed out")

    def _kill(self) -> None:
        """End the process group we started (its id is our child's exact pid): `codex` may be a launcher whose
        binary holds the pipe too. Never once it has been reaped: its pid may belong to someone else by then."""
        with self.reap:
            self._kill_locked()

    def _kill_locked(self) -> None:
        if self.p.returncode is None and self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except OSError:
                pass

    def _expire(self) -> None:
        """A hung Codex: end it, so the read sees EOF."""
        self._kill()

    def close(self) -> None:
        self.timer.cancel()
        for f in (self.p.stdin, self.p.stdout):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        with self.reap:  # reaped only under the lock, so a kill from the timer never lands on a reused pid
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_locked()
                self.p.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _line(e: object) -> str:
    """One line of at most 200 characters, for a `reason`."""
    return " ".join(str(e).split())[:200]


def _tailshare_version() -> str:
    try:
        from importlib.metadata import version

        return version("tailshare")
    except Exception:  # noqa: BLE001
        return "0"


def own_commands() -> set[str]:
    """Every command tailshare installs as a Codex hook, run by an installed tailshare: only these are ever
    trusted. Only an absolute path to a `tailshare` executable this user or root owns (keys.trusted_file); never
    `python -m tailshare`, which imports from the folder Codex runs the hook in, so trusting it would trust any
    `./tailshare` package in whatever folder a session is opened in."""
    from . import agent
    from .keys import trusted_file

    exes = {agent.exe(), str(Path("~/.local/bin/tailshare").expanduser())}
    out = {agent.state_command()}
    for t in exes:
        if os.path.isabs(t) and os.path.basename(t) == "tailshare" and " " not in t and trusted_file(t):
            out |= {f"{t} agent hook {n} --harness codex" for n in agent.HOOKS.values()}
    return out


def _tailshare_like(h: dict) -> bool:
    """A hook in this user's hooks.json that runs tailshare in a form never trusted (e.g. `python -m tailshare`)."""
    c = h.get("command")
    return isinstance(c, str) and " agent hook " in c and "tailshare" in c


def _ours(hooks: list) -> list[dict]:
    path = os.path.realpath(CODEX_HOME / "hooks.json")
    mine = own_commands()
    return [h for h in hooks if isinstance(h, dict) and h.get("handlerType") == "command"
            and h.get("source") == "user" and os.path.realpath(str(h.get("sourcePath", ""))) == path
            and h.get("command") in mine and isinstance(h.get("key"), str) and isinstance(h.get("currentHash"), str)]


def _list(srv: AppServer) -> list[dict]:
    res = srv.call("hooks/list", {"cwds": [str(Path.home())]})
    data = res.get("data")
    if not isinstance(data, list) or not data or not all(isinstance(e, dict) for e in data):
        raise RuntimeError("codex hooks/list: no data")
    seen: dict[str, dict] = {}
    for e in data:
        for h in e.get("hooks") or []:
            if isinstance(h, dict):
                seen.setdefault(str(h.get("key")), h)
    hooks = list(seen.values())
    if any("trustStatus" not in h for h in hooks):
        raise Unsupported("trustStatus")
    return hooks


def _summary(hooks: list[dict], version: str) -> dict:
    ours = _ours(hooks)
    untrusted = [h for h in ours if h.get("trustStatus") not in ("trusted", "managed")]
    out = {"codex": version, "hooks": len(ours), "untrusted": len(untrusted)}
    path = os.path.realpath(CODEX_HOME / "hooks.json")
    other = [h for h in hooks if h not in ours and h.get("source") == "user" and _tailshare_like(h)
             and os.path.realpath(str(h.get("sourcePath", ""))) == path]
    if other:  # a form never trusted from a device: reinstall with the installed tool
        return {**out, "state": "untrusted", "untrusted": len(untrusted) + len(other), "fix": REINSTALL_FIX}
    if not ours:
        return {**out, **NOT_INSTALLED}
    if not untrusted:
        return {**out, "state": "trusted"}
    modified = any(h.get("trustStatus") == "modified" for h in untrusted)
    return {**out, "state": "modified" if modified else "untrusted", "fix": {"action": "trust_codex_hooks"}}


def status(fresh: bool = False) -> dict | None:
    """The trust of tailshare's Codex hooks here, or None when Codex is not installed:
    {"state": "trusted"|"untrusted"|"modified"|"not_installed"|"unsupported"|"error", "codex": "<version>",
     "hooks": n, "untrusted": n, "fix": {…}}. Read-only."""
    codex = _codex()
    if not codex:
        return None
    if not (CODEX_HOME / "hooks.json").is_file():  # nothing of tailshare's to trust: no need to run Codex
        return {"hooks": 0, "untrusted": 0, **NOT_INSTALLED}
    version = _version(codex)
    try:
        with AppServer(codex) as srv:
            return _summary(_list(srv), version)
    except Unsupported:
        return {"state": "unsupported", "codex": version, "fix": TUI_FIX}
    except (OSError, RuntimeError) as e:
        return {"state": "error", "codex": version, "reason": _line(e), "fix": TUI_FIX}


def _inputs() -> tuple:
    """What the status depends on: Codex's path, and the hooks file, config and binary as they are now."""
    def stamp(p):
        try:
            st = os.stat(p)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return None
    codex = _codex()
    return (codex, stamp(codex) if codex else None, stamp(CODEX_HOME / "hooks.json"), stamp(CODEX_HOME / "config.toml"))


_seen: tuple[float, tuple | None] = (0.0, None)  # when the inputs were last looked at, and what they were


def cached() -> dict | None:
    """The last status, refreshed in the background when Codex's hooks, config or binary changed (looked at once
    a TTL) or MAX_AGE after the last ask: what /hello sends."""
    global _busy, _seen
    with _lock:
        at, value = _cache
        now = time.time()
        stale = now - at > MAX_AGE
        if not stale and now - _seen[0] > TTL:
            inputs = _inputs()
            stale = _seen[1] is not None and inputs != _seen[1]
            _seen = (now, inputs)
        if at == 0.0:
            stale = True
        if stale and not _busy:
            _busy = True
            threading.Thread(target=_refresh, daemon=True).start()
    return value


def _refresh() -> None:
    global _cache, _busy, _seen
    try:
        inputs = _inputs()
        value = status()
        _seen = (time.time(), inputs)
    except Exception:  # noqa: BLE001
        value = None
    with _lock:
        _cache, _busy = (time.time(), value), False


def trust() -> dict:
    """Trust tailshare's own Codex hooks here, through Codex's app-server, and return the new status. Only hooks
    whose command is exactly one tailshare installs, at the hash Codex shows for them now."""
    global _cache, _seen
    from .start import Refused

    codex = _codex()
    if not codex:
        raise Refused("not_found", reason="Codex is not installed on this machine")
    version = _version(codex)
    try:
        with AppServer(codex) as srv:
            hooks = _list(srv)
            ours = _ours(hooks)
            if _summary(hooks, version).get("fix") == REINSTALL_FIX:
                raise Refused("not_ours", reason="tailshare's Codex hooks here do not run an installed tailshare",
                              fix=REINSTALL_FIX)
            if not ours:
                raise Refused("not_found", reason="tailshare's Codex hooks are not installed here",
                              fix=NOT_INSTALLED["fix"])
            todo = {h["key"]: {"trusted_hash": h["currentHash"]} for h in ours
                    if h.get("trustStatus") not in ("trusted", "managed")}
            if todo:
                srv.call("config/batchWrite", {"edits": [{"keyPath": "hooks.state", "mergeStrategy": "upsert",
                                                          "value": todo}], "reloadUserConfig": True})
            out = _summary(_list(srv), version)
    except Unsupported:
        raise Refused("unsupported", reason=f"this Codex ({version or 'unknown version'}) cannot be told to trust "
                      "hooks from outside", fix=TUI_FIX) from None
    except (OSError, RuntimeError) as e:
        raise Refused("failed", reason=_line(e), fix=TUI_FIX) from None
    with _lock:  # /hello shows it at once, and the config Codex just wrote is not a change to ask about again
        _cache, _seen = (time.time(), out), (time.time(), _inputs())
    return out


# --------------------------------------------------------------------------- `tailshare agent trust-codex`, in your terminal

def cmd_trust(args) -> None:
    """The same trust, from the person's own terminal (never an agent's shell)."""
    from .start import Refused, _human_check

    try:
        _human_check("trust Codex hooks")
    except ValueError as e:
        sys.exit(f"tailshare agent trust-codex: {e}")
    now = status()
    if now is None:
        sys.exit("tailshare agent trust-codex: Codex is not installed on this machine")
    print(json.dumps(now, indent=2))
    if now.get("state") not in ("untrusted", "modified"):
        return
    if input(f"Trust tailshare's {now['untrusted']} Codex hook(s) here? [y/N] ").strip().lower() not in ("y", "yes"):
        print("left as it is")
        return
    try:
        print(json.dumps(trust(), indent=2))
    except Refused as e:
        sys.exit(f"tailshare agent trust-codex: {e.out.get('reason', e.out.get('error'))}")
