"""Sessions: find old Claude Code / Codex sessions on any machine, ask them questions, reopen them.

Every mailbox indexes the transcripts on its own machine (``~/.claude/projects`` and
``~/.codex/sessions``) into ``~/.tailshare/sessions.db``; a search asks every machine of the
same tailnet user and merges the answers. There is still no hub.

Two ways to reach a session on another machine:

- **ask**: resume a throwaway fork of it with no tools and ask it a question. Nothing it
  says is saved, and it cannot run anything. Any machine of the same tailnet user may ask.
- **launch**: resume it in the background with real permissions. Only with a request signed
  by a key listed in that machine's ``~/.tailshare/allowed_signers``, which a person adds by
  hand. The signature covers the exact machine, session, folder, permission mode and prompt,
  and expires within minutes; each one works once. See docs/sessions.md.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from . import bus

CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
SIGNERS = bus.STATE_DIR / "allowed_signers"
NAMESPACE = "tailshare-launch"
TEXT_CAP = 256 * 1024  # searchable text kept per session
SCAN_EVERY = 60
ASK_TIMEOUT = 300
MAX_QUESTION = 8 * 1024
LAUNCH_TTL = 300  # how long a signed launch request stays valid
LAUNCH_TTL_MAX = 900
MODES = ("manual", "plan", "dontAsk", "acceptEdits", "auto")  # never bypassPermissions
BIN_DIRS = ("~/.local/bin", "~/.claude/local", "/opt/homebrew/bin", "/usr/local/bin", "~/.npm-global/bin", "/usr/bin")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, harness TEXT NOT NULL, path TEXT NOT NULL, cwd TEXT NOT NULL DEFAULT '',
  folder TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', ai_title TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '', first_prompt TEXT NOT NULL DEFAULT '', last_prompt TEXT NOT NULL DEFAULT '',
  prompts INTEGER NOT NULL DEFAULT 0, started REAL NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, id TEXT);
CREATE TABLE IF NOT EXISTS nonces(nonce TEXT PRIMARY KEY, expires REAL NOT NULL);
"""


def log(msg: str) -> None:
    sys.stderr.write(f"{datetime.now():%H:%M:%S} sessions: {msg}\n")


def _ts(s: object) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _blocks(content: object, kind: str) -> str:
    """The text of a message's content: a string, or a list of blocks of type `kind`."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == kind)
    return ""


def _is_prompt(text: str) -> bool:
    """A real prompt, not a command wrapper, reminder or caveat injected by the harness."""
    t = text.strip()
    return bool(t) and not t.startswith(("<", "Caveat:", "[Request interrupted"))


def _summary(prompts: list[str], replies: list[str]) -> dict:
    text = "\n".join(prompts)[: TEXT_CAP // 2] + "\n" + "\n".join(r[:2000] for r in replies)[: TEXT_CAP // 2]
    first = prompts[0] if prompts else ""
    return {"first_prompt": first[:300], "last_prompt": (prompts[-1] if prompts else "")[:300],
            "prompts": len(prompts), "text": text}


# --------------------------------------------------------------------------- transcripts


def parse_claude(path: Path) -> dict | None:
    """One Claude Code transcript (``projects/<dir>/<session id>.jsonl``). None if it has no prompts."""
    s = {"id": path.stem, "harness": "claude", "path": str(path), "cwd": "", "title": "", "ai_title": "",
         "branch": "", "started": 0.0, "updated": 0.0}
    prompts: list[str] = []
    replies: list[str] = []
    with path.open("rb") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(d, dict):
                continue
            t = d.get("type")
            if t == "custom-title":
                s["title"] = str(d.get("customTitle") or "")
                continue
            if t == "ai-title":
                s["ai_title"] = str(d.get("aiTitle") or "")
                continue
            if t not in ("user", "assistant") or d.get("isSidechain"):
                continue
            s["cwd"] = s["cwd"] or str(d.get("cwd") or "")
            s["branch"] = str(d.get("gitBranch") or s["branch"])
            when = _ts(d.get("timestamp"))
            if when:
                s["started"] = s["started"] or when
                s["updated"] = when
            content = (d.get("message") or {}).get("content")
            if t == "user":
                if d.get("isMeta") or d.get("isCompactSummary"):
                    continue
                if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    continue
                text = _blocks(content, "text")
                if _is_prompt(text):
                    prompts.append(text.strip())
            else:
                text = _blocks(content, "text")
                if text.strip():
                    replies.append(text.strip())
    if not prompts:
        return None
    return {**s, **_summary(prompts, replies)}


def codex_titles() -> dict[str, str]:
    titles: dict[str, str] = {}
    try:
        with (CODEX_HOME / "session_index.jsonl").open() as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                    titles[str(d["id"])] = str(d.get("thread_name") or "")
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return titles


def parse_codex(path: Path, titles: dict[str, str]) -> dict | None:
    """One Codex rollout (``sessions/YYYY/MM/DD/rollout-*.jsonl``). None if it has no prompts."""
    s = {"id": "", "harness": "codex", "path": str(path), "cwd": "", "title": "", "ai_title": "",
         "branch": "", "started": 0.0, "updated": 0.0}
    items: tuple[list[str], list[str]] = ([], [])  # from response_item messages
    events: tuple[list[str], list[str]] = ([], [])  # from event_msg (older rollouts have only these)
    with path.open("rb") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(d, dict):
                continue
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            when = _ts(d.get("timestamp"))
            if when:
                s["started"] = s["started"] or when
                s["updated"] = when
            t, pt = d.get("type"), p.get("type")
            if t == "session_meta":
                s["id"] = str(p.get("id") or p.get("session_id") or "")
                s["cwd"] = str(p.get("cwd") or "")
                s["branch"] = str((p.get("git") or {}).get("branch") or "")
                s["started"] = _ts(p.get("timestamp")) or s["started"]
            elif t == "turn_context" and p.get("cwd") and not s["cwd"]:
                s["cwd"] = str(p["cwd"])
            elif t == "response_item" and pt == "message" and p.get("role") in ("user", "assistant"):
                user = p["role"] == "user"
                text = _blocks(p.get("content"), "input_text" if user else "output_text").strip()
                if text and (not user or _is_prompt(text)):
                    items[0 if user else 1].append(text)
            elif t == "event_msg" and pt in ("user_message", "agent_message"):
                text = str(p.get("message") or "").strip()
                if text and (pt == "agent_message" or _is_prompt(text)):
                    events[0 if pt == "user_message" else 1].append(text)
    prompts, replies = items if items[0] else events
    if not s["id"] or not prompts:
        return None
    s["title"] = titles.get(s["id"], "")
    return {**s, **_summary(prompts, replies)}


def transcript_files() -> list[tuple[str, Path]]:
    out = [("claude", p) for p in (CLAUDE_HOME / "projects").glob("*/*.jsonl")]
    out += [("codex", p) for p in (CODEX_HOME / "sessions").glob("*/*/*/rollout-*.jsonl")]
    return out


# --------------------------------------------------------------------------- the index


class Index:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)
            try:
                self.db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5("
                    "id UNINDEXED, title, folder, text, tokenize='unicode61 remove_diacritics 2')"
                )
                self.fts = True
            except sqlite3.OperationalError:  # a Python built without FTS5: plain substring search
                self.db.execute("CREATE TABLE IF NOT EXISTS sessions_text(id TEXT PRIMARY KEY, title, folder, text)")
                self.fts = False
        self.text_table = "sessions_fts" if self.fts else "sessions_text"

    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, *args) -> int:
        with self.lock:
            return self.db.execute(sql, args).rowcount

    # -- indexing -------------------------------------------------------------
    def scan(self) -> int:
        """Re-read transcripts that changed since the last scan. Returns how many were read."""
        seen = {r["path"]: r for r in self.q("SELECT * FROM files WHERE path NOT LIKE 'meta:%'")}
        titles: dict[str, str] | None = None
        titles_mtime = _mtime(CODEX_HOME / "session_index.jsonl")
        renamed = titles_mtime != self._meta("codex_titles_mtime")
        n = 0
        present = set()
        for harness, path in transcript_files():
            key = str(path)
            present.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            old = seen.get(key)
            if old and old["mtime"] == st.st_mtime and old["size"] == st.st_size and not (renamed and harness == "codex"):
                continue
            try:
                if harness == "claude":
                    s = parse_claude(path)
                else:
                    titles = titles if titles is not None else codex_titles()
                    s = parse_codex(path, titles)
            except OSError:
                continue
            self._store(key, st, s)
            n += 1
        for gone in set(seen) - present:
            self._forget(gone)
        self._set_meta("codex_titles_mtime", titles_mtime)
        return n

    def _store(self, key: str, st: os.stat_result, s: dict | None) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute("INSERT OR REPLACE INTO files(path, mtime, size, id) VALUES(?,?,?,?)",
                                (key, st.st_mtime, st.st_size, s["id"] if s else None))
                if s:
                    cur = self.db.execute("SELECT path, updated FROM sessions WHERE id=?", (s["id"],)).fetchone()
                    if cur and cur["path"] != key and cur["updated"] > s["updated"]:
                        self.db.execute("COMMIT")
                        return  # another file holds a newer copy of this session
                    s["folder"] = Path(s["cwd"]).name
                    cols = ("id", "harness", "path", "cwd", "folder", "title", "ai_title", "branch",
                            "first_prompt", "last_prompt", "prompts", "started", "updated")
                    self.db.execute(f"INSERT OR REPLACE INTO sessions({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                    tuple(s[c] for c in cols))
                    self.db.execute(f"DELETE FROM {self.text_table} WHERE id=?", (s["id"],))
                    self.db.execute(f"INSERT INTO {self.text_table}(id, title, folder, text) VALUES(?,?,?,?)",
                                    (s["id"], f"{s['title']} {s['ai_title']}", s["folder"], s["text"]))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def _forget(self, key: str) -> None:
        with self.lock:
            row = self.db.execute("SELECT id FROM files WHERE path=?", (key,)).fetchone()
            self.db.execute("DELETE FROM files WHERE path=?", (key,))
            if row and row["id"]:
                if self.db.execute("SELECT 1 FROM sessions WHERE id=? AND path=?", (row["id"], key)).fetchone():
                    self.db.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
                    self.db.execute(f"DELETE FROM {self.text_table} WHERE id=?", (row["id"],))

    def _meta(self, key: str) -> float | None:
        r = self.q("SELECT mtime FROM files WHERE path=?", f"meta:{key}")
        return r[0]["mtime"] if r else None

    def _set_meta(self, key: str, value: float | None) -> None:
        self.x("INSERT OR REPLACE INTO files(path, mtime, size, id) VALUES(?,?,0,NULL)", f"meta:{key}", value)

    # -- reading --------------------------------------------------------------
    def search(self, q: str = "", limit: int = 20, harness: str = "", folder: str = "", id_prefix: str = "") -> list[dict]:
        where, args = [], []
        if harness:
            where.append("s.harness=?")
            args.append(harness)
        if folder:
            where.append("s.folder LIKE ?")
            args.append(f"%{folder}%")
        if id_prefix:
            where.append("s.id LIKE ?")
            args.append(id_prefix.replace("%", "").replace("_", "") + "%")
        words = re.findall(r"\w+", q.lower())
        limit = max(1, min(int(limit), 100))
        if words and self.fts:
            match = " ".join(f'"{w}"*' for w in words)
            sql = (
                "SELECT s.*, bm25(sessions_fts, 0, 10.0, 4.0, 1.0) AS score,"
                " snippet(sessions_fts, 3, '[', ']', '…', 12) AS snippet"
                " FROM sessions_fts JOIN sessions s ON s.id = sessions_fts.id"
                f" WHERE sessions_fts MATCH ? {''.join(' AND ' + w for w in where)}"
                " ORDER BY score LIMIT ?"
            )
            rows = self.q(sql, match, *args, limit)
        elif words:
            for w in words:
                where.append("(t.title LIKE ? OR t.folder LIKE ? OR t.text LIKE ?)")
                args += [f"%{w}%"] * 3
            rows = self.q(
                "SELECT s.*, 0.0 AS score, '' AS snippet FROM sessions s JOIN sessions_text t ON t.id = s.id"
                f" WHERE {' AND '.join(where)} ORDER BY s.updated DESC LIMIT ?", *args, limit)
        else:
            rows = self.q(f"SELECT s.*, 0.0 AS score, '' AS snippet FROM sessions s"
                          f"{' WHERE ' + ' AND '.join(where) if where else ''} ORDER BY s.updated DESC LIMIT ?",
                          *args, limit)
        return [_row(r) for r in rows]

    def get(self, sid: str) -> dict | None:
        rows = self.q("SELECT *, 0.0 AS score, '' AS snippet FROM sessions WHERE id=?", sid)
        return _row(rows[0]) if rows else None

    def use_nonce(self, nonce: str, expires: float) -> bool:
        """Record a launch nonce. False if it was used before (a replayed request)."""
        with self.lock:
            self.db.execute("DELETE FROM nonces WHERE expires<?", (time.time() - 60,))
            return self.db.execute("INSERT OR IGNORE INTO nonces(nonce, expires) VALUES(?,?)", (nonce, expires)).rowcount == 1


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _row(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in r.keys() if k != "path"}
    d["name"] = d["title"] or d["ai_title"]
    return d


# --------------------------------------------------------------------------- running harnesses


def find_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    return next((str(p) for d in BIN_DIRS if (p := Path(d).expanduser() / name).exists()), None)


def _env(tool: str) -> dict:
    """Environment for a harness we start: its own folder on PATH (launchd's PATH is minimal; codex
    needs node next to it), and no tailshare hooks: a throwaway fork must not join the bus."""
    dirs = [str(Path(tool).parent)] + [str(Path(d).expanduser()) for d in BIN_DIRS]
    path = os.pathsep.join(dict.fromkeys(dirs + os.environ.get("PATH", "").split(os.pathsep)))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE_", "CODEX_THREAD"))}
    return {**env, "PATH": path, "TERM": env.get("TERM") or "dumb"}


def ask_command(s: dict, prompt: str, out_file: str = "") -> tuple[list[str], str | None]:
    """The command that resumes a throwaway, tool-less copy of session `s`; (argv, stdin)."""
    if s["harness"] == "claude":
        tool = find_tool("claude")
        if not tool:
            raise bus.BusError("claude is not installed on this machine", 409)
        # The prompt goes on stdin: --tools takes a list and would swallow a positional prompt.
        return [tool, "-p", "--resume", s["id"], "--fork-session", "--no-session-persistence",
                "--strict-mcp-config", "--output-format", "json", "--tools", ""], prompt
    tool = find_tool("codex")
    if not tool:
        raise bus.BusError("codex is not installed on this machine", 409)
    return [tool, "exec", "resume", s["id"], "--ephemeral", "--skip-git-repo-check",
            "-c", 'sandbox_mode="read-only"', "-c", 'approval_policy="never"', "-o", out_file, prompt], None


def ask_prompt(question: str, asker: str) -> str:
    return (
        f"Another AI agent on the user's tailnet ({asker or 'unknown'}) is asking about this session through "
        "tailshare. Answer from what you know of this conversation, briefly and concretely. You have no tools "
        "for this reply, and nothing you say here is saved to the session.\n\n"
        f"Question: {question}"
    )


def launch_command(s: dict, mode: str, prompt: str) -> list[str]:
    tool = find_tool("claude")
    if not tool:
        raise bus.BusError("claude is not installed on this machine", 409)
    cmd = [tool, "--bg", "--resume", s["id"]]
    if mode != "manual":
        cmd += ["--permission-mode", mode]
    if prompt:
        cmd.append(prompt if not prompt.startswith("-") else " " + prompt)
    return cmd


# --------------------------------------------------------------------------- signed launch requests


def ssh_keygen() -> str:
    return shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"


def canonical(d: dict) -> str:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def launch_request(signer: str, host: str, s: dict, mode: str, prompt: str, ttl: int = LAUNCH_TTL) -> str:
    """The exact text a person signs to launch session `s` on `host`."""
    now = time.time()
    return canonical({
        "v": 1, "action": "launch", "signer": signer, "host": host, "harness": s["harness"], "session": s["id"],
        "cwd": s["cwd"], "title": s.get("name", ""), "mode": mode, "prompt": prompt,
        "nonce": secrets.token_hex(16), "issued": round(now, 3), "expires": round(now + ttl, 3),
    })


def sign(payload: str, key: Path) -> str:
    """Sign with ssh-keygen. A security key asks for a touch (on the terminal's stderr)."""
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "launch.json"
        data.write_text(payload)
        cp = subprocess.run([ssh_keygen(), "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(data)],
                            stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
        if cp.returncode != 0:
            raise bus.BusError(f"ssh-keygen could not sign with {key}")
        return (Path(tmp) / "launch.json.sig").read_text()


def verify(payload: str, signature: str, signers: Path = SIGNERS) -> dict:
    """Check the signature against this machine's allowed_signers; return the request."""
    if not signers.exists():
        raise bus.BusError("launching is off on this machine: it has no ~/.tailshare/allowed_signers", 409)
    st = signers.stat()
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise bus.BusError(f"{signers} must belong to this user and not be writable by others", 409)
    try:
        d = json.loads(payload)
    except json.JSONDecodeError:
        raise bus.BusError("malformed launch request") from None
    if not isinstance(d, dict) or not isinstance(d.get("signer"), str) or not re.fullmatch(r"[\w.@+-]{1,64}", d["signer"]):
        raise bus.BusError("malformed launch request")
    with tempfile.TemporaryDirectory() as tmp:
        sig = Path(tmp) / "sig"
        sig.write_text(signature)
        cp = subprocess.run([ssh_keygen(), "-Y", "verify", "-f", str(signers), "-I", d["signer"], "-n", NAMESPACE,
                             "-s", str(sig)], input=payload.encode(), capture_output=True)
    if cp.returncode != 0:
        raise bus.BusError(f"signature not valid for {d['signer']}: {cp.stderr.decode().strip()[-200:]}", 403)
    return d


def trust_line(principal: str, pubkey: str) -> str:
    parts = pubkey.split()
    if len(parts) < 2 or not re.fullmatch(r"[\w.@+-]{1,64}", principal):
        raise ValueError("expected an OpenSSH public key line and a simple principal name")
    return f'{principal} namespaces="{NAMESPACE}" {" ".join(parts[:3])}\n'


def hardware_backed(pubkey: str) -> bool:
    return pubkey.split()[0].startswith("sk-") if pubkey.split() else False


# --------------------------------------------------------------------------- the service


class Sessions:
    """The sessions half of a mailbox: keeps the index fresh and answers search, ask and launch."""

    def __init__(self, b: bus.Bus, index: Index, signers: Path = SIGNERS) -> None:
        self.bus, self.index, self.signers = b, index, signers
        self.asking = threading.BoundedSemaphore(2)  # each ask runs a model; don't pile them up

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="sessions-index").start()

    def _loop(self) -> None:
        while True:
            try:
                t = time.time()
                n = self.index.scan()
                if n:
                    log(f"indexed {n} transcript(s) in {time.time() - t:.1f}s")
            except Exception as e:  # noqa: BLE001
                log(f"index scan failed: {e!r}")
            time.sleep(SCAN_EVERY)

    def _live(self, rows: list[dict]) -> list[dict]:
        live = {a["id"] for a in self.bus.local_agents() if a["online"]}
        for r in rows:
            r["host"] = self.bus.net.host
            r["live"] = r["id"] in live
        return rows

    # -- search ---------------------------------------------------------------
    def search_local(self, body: dict) -> list[dict]:
        q = str(body.get("q") or "")[:200]
        return self._live(self.index.search(
            q, int(body.get("limit") or 20), str(body.get("harness") or ""),
            str(body.get("folder") or "")[:100], str(body.get("id") or "")[:64]))

    def search(self, body: dict) -> dict:
        """Search this machine and every peer that answers (filtered by `host` glob)."""
        from fnmatch import fnmatchcase

        want = str(body.get("host") or "*")
        results, hosts = [], {}
        if fnmatchcase(self.bus.net.host, want):
            results += self.search_local(body)
            hosts[self.bus.net.host] = "ok"
        peers = [h for h in self._peers() if fnmatchcase(h, want)]

        def ask(h: str) -> tuple[str, list[dict] | str]:
            p = self.bus.net.peers[h]
            try:
                r = bus.request(f"http://{p['ip']}:{p['port']}/bus/v1/sessions/search", {"v": bus.PROTOCOL, **body}, timeout=8)
                return h, list(r.get("results") or [])
            except bus.BusError as e:
                return h, "no sessions support (old tailshare)" if e.code == 404 else str(e)
            except Exception as e:  # noqa: BLE001
                return h, f"unreachable: {getattr(e, 'reason', e)}"

        if peers:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for h, r in pool.map(ask, peers):
                    if isinstance(r, list):
                        results += [{**x, "host": h} for x in r]
                        hosts[h] = "ok"
                    else:
                        hosts[h] = r
        if body.get("q"):
            results.sort(key=lambda r: r.get("score") or 0.0)
        else:
            results.sort(key=lambda r: -(r.get("updated") or 0.0))
        return {"results": results[: max(1, min(int(body.get("limit") or 20), 100))], "hosts": hosts}

    def _peers(self) -> list[str]:
        known = {r["host"] for r in self.bus.store.q("SELECT host FROM peers WHERE last_ok IS NOT NULL")}
        return sorted(h for h in known if self.bus.net.peers.get(h, {}).get("online"))

    def resolve(self, ref: str) -> dict:
        """``host:id-prefix`` or ``id-prefix`` → the one session it names (with its host)."""
        host, _, prefix = ref.rpartition(":")
        prefix = prefix.strip().lower()
        if len(prefix) < 4:
            raise bus.BusError("give at least 4 characters of the session id")
        found = self.search({"id": prefix, "host": host or "*", "limit": 10})["results"]
        if not found:
            raise bus.BusError(f"no session {ref!r} found" + ("" if host else " on any machine that answered"), 404)
        if len(found) > 1:
            raise bus.BusError("ambiguous: " + ", ".join(f"{r['host']}:{r['id'][:12]}" for r in found))
        return found[0]

    # -- ask ------------------------------------------------------------------
    def ask(self, body: dict) -> dict:
        """Local API: ask a session anywhere. Forwards to the session's machine."""
        s = self.resolve(str(body.get("ref") or ""))
        payload = {"v": bus.PROTOCOL, "id": s["id"], "question": str(body.get("question") or ""),
                   "from": str(body.get("from") or "")}
        if s["host"] == self.bus.net.host:
            return self.ask_local(payload)
        p = self.bus.net.peers.get(s["host"])
        if not p or not p["online"]:
            raise bus.BusError(f"{s['host']} is offline", 503)
        return bus.request(f"http://{p['ip']}:{p['port']}/bus/v1/sessions/ask", payload, timeout=ASK_TIMEOUT + 30)

    def ask_local(self, body: dict) -> dict:
        question = str(body.get("question") or "").strip()
        if not question:
            raise bus.BusError("empty question")
        if len(question) > MAX_QUESTION:
            raise bus.BusError(f"question too long (max {MAX_QUESTION // 1024} KB)")
        s = self.index.get(str(body.get("id") or ""))
        if s is None:
            raise bus.BusError("no such session on this machine", 404)
        if not Path(s["cwd"] or "/nonexistent").is_dir():
            raise bus.BusError(f"the session's folder {s['cwd']} no longer exists", 409)
        if not self.asking.acquire(blocking=False):
            raise bus.BusError("this machine is already answering two questions; try again shortly", 429)
        try:
            log(f"ask {s['harness']} {s['id'][:8]} from {body.get('from') or '?'}")
            return {"host": self.bus.net.host, "id": s["id"], "name": s["name"], **self._run_ask(s, question, str(body.get("from") or ""))}
        finally:
            self.asking.release()

    def _run_ask(self, s: dict, question: str, asker: str) -> dict:
        prompt = ask_prompt(question, asker)
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "answer.txt")
            argv, stdin = ask_command(s, prompt, out)
            env = {**_env(argv[0]), "TAILSHARE_HOOKS": "0"}
            try:
                cp = subprocess.run(argv, input=stdin, capture_output=True, text=True, cwd=s["cwd"], env=env,
                                    timeout=ASK_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise bus.BusError(f"no answer within {ASK_TIMEOUT}s", 504) from None
            if s["harness"] == "claude":
                try:
                    r = json.loads(cp.stdout)
                except json.JSONDecodeError:
                    raise bus.BusError(f"claude failed: {(cp.stderr or cp.stdout).strip()[-300:]}", 502) from None
                if r.get("is_error"):
                    raise bus.BusError(f"claude failed: {str(r.get('result'))[-300:]}", 502)
                return {"answer": str(r.get("result") or ""), "cost_usd": r.get("total_cost_usd")}
            answer = Path(out).read_text().strip() if Path(out).exists() else ""
            if cp.returncode != 0 and not answer:
                raise bus.BusError(f"codex failed: {(cp.stderr or cp.stdout).strip()[-300:]}", 502)
            return {"answer": answer}

    # -- launch ---------------------------------------------------------------
    def running(self, sid: str) -> bool:
        """Is the session open somewhere on this machine? (on the bus, or in `claude agents`)"""
        if any(a["id"] == sid and a["online"] for a in self.bus.local_agents()):
            return True
        tool = find_tool("claude")
        if not tool:
            return False
        try:
            cp = subprocess.run([tool, "agents", "--json"], capture_output=True, text=True, timeout=15,
                                env=_env(tool), stdin=subprocess.DEVNULL)
            return any(isinstance(a, dict) and a.get("sessionId") == sid for a in json.loads(cp.stdout or "[]"))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            return False

    def launch(self, body: dict) -> dict:
        """Local API: deliver a signed launch request to the machine it names."""
        payload, signature = str(body.get("payload") or ""), str(body.get("signature") or "")
        try:
            host = json.loads(payload)["host"]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise bus.BusError("malformed launch request") from None
        if host == self.bus.net.host:
            return self.launch_local(payload, signature)
        p = self.bus.net.peers.get(host)
        if not p or not p["online"]:
            raise bus.BusError(f"{host} is offline", 503)
        return bus.request(f"http://{p['ip']}:{p['port']}/bus/v1/sessions/launch",
                           {"v": bus.PROTOCOL, "payload": payload, "signature": signature}, timeout=90)

    def launch_local(self, payload: str, signature: str) -> dict:
        if len(payload) > 16 * 1024 or len(signature) > 8 * 1024:
            raise bus.BusError("launch request too large", 413)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            raise bus.BusError("tailshare runs as root here; it never launches agents as root", 409)
        d = verify(payload, signature, self.signers)
        now = time.time()
        problems = []
        if d.get("v") != 1 or d.get("action") != "launch":
            problems.append("not a launch request")
        if d.get("host") != self.bus.net.host:
            problems.append(f"signed for {d.get('host')}, not {self.bus.net.host}")
        issued, expires = float(d.get("issued") or 0), float(d.get("expires") or 0)
        if expires < now:
            problems.append("expired")
        if issued > now + 120 or expires - issued > LAUNCH_TTL_MAX:
            problems.append("bad validity window")
        if d.get("mode") not in MODES:
            problems.append(f"permission mode {d.get('mode')!r} is not allowed")
        prompt = d.get("prompt") if isinstance(d.get("prompt"), str) else None
        if prompt is None or len(prompt) > MAX_QUESTION:
            problems.append("bad prompt")
        s = self.index.get(str(d.get("session") or ""))
        if s is None:
            problems.append("no such session on this machine")
        elif s["harness"] != "claude" or d.get("harness") != "claude":
            problems.append("only Claude Code sessions can be launched so far")
        elif s["cwd"] != d.get("cwd"):
            problems.append("the session's folder does not match the signed one")
        elif self.running(s["id"]):
            problems.append("that session is running right now (message it instead; launching would start a copy)")
        if problems:
            raise bus.BusError("launch refused: " + "; ".join(problems), 403)
        if not re.fullmatch(r"[0-9a-f]{32}", str(d.get("nonce"))) or not self.index.use_nonce(d["nonce"], expires):
            raise bus.BusError("launch refused: this request was already used", 403)
        assert s is not None and prompt is not None
        log(f"launch {s['id'][:8]} mode={d['mode']} signed by {d['signer']}")
        argv = launch_command(s, d["mode"], prompt)
        try:
            cp = subprocess.run(argv, capture_output=True, text=True, cwd=s["cwd"], env=_env(argv[0]), timeout=60,
                                stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise bus.BusError("claude --bg did not return within 60s", 504) from None
        if cp.returncode != 0:
            raise bus.BusError(f"claude --bg failed: {(cp.stderr or cp.stdout).strip()[-300:]}", 502)
        return {"host": self.bus.net.host, "id": s["id"], "name": s["name"], "mode": d["mode"],
                "output": cp.stdout.strip()[-500:]}


def start(b: bus.Bus) -> Sessions:
    s = Sessions(b, Index(bus.STATE_DIR / "sessions.db"))
    s.start()
    return s
