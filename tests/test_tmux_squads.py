"""squad-up and the tmux squad reader, on a private tmux server with fake agents (never a real agent, never the
person's own tmux server)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from tailshare import tmux_squads as ts

TMUX = shutil.which("tmux")

# A stand-in for claude/codex. Its folder's `mode` file says what it shows: `idle` (an empty prompt with a dim
# placeholder), `ask` (a permission question), `exit N`.
# Typed lines go to `typed`; argv to `argv`. With a `rollout` file in its folder it behaves like Codex: it keeps
# a session file open under $CODEX_HOME/sessions (`resume <id>` reopens that one) and logs "<pid> <id>" to rollouts.
FAKE = r"""#!/bin/bash
d=$PWD
printf '%s\n' "$*" >> "$d/argv"
printf '%s %s %s\n' "$SQUAD_NAME" "$SQUAD_AGENT" "${SQUAD_TEST:-}" >> "$d/squadenv"
if [ -f "$d/rollout" ]; then
  if [ "${1:-}" = resume ]; then id=$2; else id=$(uuidgen | tr A-Z a-z); fi
  mkdir -p "$CODEX_HOME/sessions/2026/11/12"
  f="$CODEX_HOME/sessions/2026/11/12/rollout-2026-11-12T00-00-00-$id.jsonl"
  [ -s "$f" ] || printf '{"type":"session_meta","payload":{"id":"%s","cwd":"%s"}}\n' "$id" "$d" > "$f"
  exec 9>>"$f"
  echo "$$ $id" >> "$d/rollouts"
fi
while :; do
  mode=$(cat "$d/mode" 2>/dev/null || echo idle)
  case "$mode" in
    exit*) exit "${mode#exit }" ;;
    ask) printf '\033[2J\033[H Bash command\n   rm -rf build\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n' ;;
    *) printf '\033[2J\033[H● Ran 1 shell command\n● Done: all tests pass.\n╭────────╮\n│ ❯\302\240\033[2mTry "x"\033[0m │\n╰────────╯\n  ⏵⏵ auto mode on (shift+tab to cycle)\n' ;;
  esac
  if IFS= read -r -t 1 line; then
    printf '%s|%s\n' "$mode" "$line" >> "$d/typed"
    case "$line" in /exit|/quit) exit 0 ;; esac
  fi
done
"""


@unittest.skipUnless(TMUX, "needs tmux")
class SquadUp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tsq", dir="/tmp"))
        self.sock = self.tmp / "s"
        self.proj = self.tmp / "proj"
        (self.proj / ".squad").mkdir(parents=True)
        for a in ("one", "two"):
            (self.proj / a).mkdir()
        fake = self.tmp / "fake-agent"
        fake.write_text(FAKE)
        fake.chmod(0o755)
        (self.proj / ".squad" / "squad").write_text(
            "squad Test\nenv SQUAD_TEST=1\nagent One claude one keep --model x\nagent Two codex two\n")
        self.claude_home = self.tmp / "claude"
        self.tmux = [TMUX, "-f", os.devnull, "-S", str(self.sock)]
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp), "TERM": "xterm",
            "SQUAD_TMUX": " ".join(self.tmux), "SQUAD_CLAUDE": str(fake), "SQUAD_CODEX": str(fake),
            "CLAUDE_CONFIG_DIR": str(self.claude_home), "CODEX_HOME": str(self.tmp / "codex"),
            "SQUAD_BACKOFF_SECS": "1", "SQUAD_HEALTHY_SECS": "60", "SQUAD_MAX_TRIES": "3",
        }

    def tearDown(self):
        # our own private server only, by its socket; never a bare kill-server
        subprocess.run([TMUX, "-S", str(self.sock), "kill-server"], capture_output=True, check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def up(self, *args: str) -> str:
        p = subprocess.run(["sh", str(ts.SCRIPT), "-C", str(self.proj), *args], env=self.env, capture_output=True,
                           text=True, timeout=30, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def windows(self) -> list[dict]:
        return ts.discover(self.tmux)

    def member(self, name: str) -> dict:
        return next(w for w in self.windows() if w["agent"] == name)

    def wait(self, cond, what: str, secs: float = 15):
        end = time.time() + secs
        while time.time() < end:
            if cond():
                return
            time.sleep(0.2)
        self.fail(f"timed out: {what}; windows: {self.windows()}")

    def mode(self, agent: str, m: str):
        (self.proj / agent / "mode").write_text(m)

    def test_up_creates_the_session_and_only_what_is_missing(self):
        self.assertIn("One: started in squad-test", self.up())
        self.wait(lambda: len(self.windows()) == 2 and all(w["harness"] for w in self.windows()), "two agents")
        one = self.member("One")
        self.assertEqual((one["session_name"], one["window_name"], one["harness"], one["keep"], one["state"]),
                         ("squad-test", "one", "claude", "on", "running"))
        self.assertEqual(one["cwd"], str((self.proj / "one").resolve()))
        self.assertEqual(self.member("Two")["harness"], "codex")
        ids = {w["window_id"] for w in self.windows()}
        out = self.up()
        self.assertEqual(out.count("already running"), 2, out)
        self.assertEqual({w["window_id"] for w in self.windows()}, ids)

    def test_claude_gets_its_own_session_id_then_resumes_it(self):
        self.up("up", "One")
        idf = self.proj / ".squad" / "state" / "One.id"
        self.wait(lambda: (self.proj / "one" / "argv").exists(), "first start")
        sid = idf.read_text().strip()
        self.assertRegex(sid, r"^[0-9a-f-]{36}$")
        self.assertEqual((self.proj / "one" / "argv").read_text().split()[:6],
                         ["--session-id", sid, "--name", "One", "--model", "x"])
        # which squad and agent it is, for the tools it runs (tailshare's hooks map it to the squad member)
        self.assertEqual((self.proj / "one" / "squadenv").read_text().split(), ["Test", "One", "1"])
        self.assertEqual(self.member("One")["resume"], sid)
        (self.claude_home / "projects" / "p").mkdir(parents=True)
        (self.claude_home / "projects" / "p" / f"{sid}.jsonl").write_text("{}\n")
        self.mode("one", "exit 3")  # it exits; the window's loop starts it again with --resume
        self.wait(lambda: len((self.proj / "one" / "argv").read_text().splitlines()) >= 2, "a restart")
        self.mode("one", "idle")
        self.assertEqual((self.proj / "one" / "argv").read_text().splitlines()[1].split()[:2], ["--resume", sid])

    def test_a_failing_agent_stops_after_the_retry_limit_and_nothing_else_moves(self):
        self.mode("one", "exit 7")
        self.up()
        self.wait(lambda: self.member("One")["state"] == "failed", "One gives up", secs=30)
        one, two = self.member("One"), self.member("Two")
        self.assertEqual((one["pane_dead"], one["tries"], one["last_exit"]), ("1", "3", "7"))
        self.assertEqual(len((self.proj / "one" / "argv").read_text().splitlines()), 3)
        self.assertEqual((two["state"], two["pane_dead"]), ("running", "0"))
        self.assertIn("stopped restarting", ts.attention(one, []))
        # start runs it again, in the same window
        self.mode("one", "idle")
        self.assertIn("started again", self.up("start", "One"))
        self.wait(lambda: self.member("One")["state"] == "running", "One runs again")
        self.assertEqual(self.member("One")["window_id"], one["window_id"])

    def test_stop_types_nothing_and_the_agent_runs_until_it_exits(self):
        self.up()
        self.wait(lambda: "Try" in "\n".join(ts.screen(self.member("One")["pane_id"], self.tmux)), "the prompt")
        self.assertIn("stops when it exits", self.up("stop", "One"))
        self.assertEqual(self.member("One")["state"], "stopping")
        self.assertIn("stops when it exits", self.up("status"))
        time.sleep(3)
        self.assertFalse((self.proj / "one" / "typed").exists())  # nothing typed, ever
        self.assertEqual(self.member("One")["pane_dead"], "0")
        self.mode("one", "exit 0")  # the person typed /exit
        self.wait(lambda: self.member("One")["state"] == "stopped", "One stopped")
        time.sleep(2)
        one = self.member("One")
        self.assertEqual((one["pane_dead"], one["state"]), ("1", "stopped"))
        self.assertEqual(len((self.proj / "one" / "argv").read_text().splitlines()), 1)  # no restart
        self.assertEqual(self.member("Two")["state"], "running")
        self.mode("one", "idle")
        self.assertIn("started again", self.up("start", "One"))
        self.wait(lambda: self.member("One")["state"] == "running", "One runs again")
        self.assertEqual(self.member("One")["window_id"], one["window_id"])

    def test_start_after_stop_restarts_it_again(self):
        self.up()
        self.wait(lambda: "Try" in "\n".join(ts.screen(self.member("One")["pane_id"], self.tmux)), "the prompt")
        self.up("stop", "One")
        self.assertIn("auto-restart on", self.up("start", "One"))
        self.mode("one", "exit 3")
        self.wait(lambda: len((self.proj / "one" / "argv").read_text().splitlines()) >= 2, "One restarted", secs=20)

    def test_stop_during_a_backoff_wait_ends_it_without_a_restart(self):
        self.env["SQUAD_BACKOFF_SECS"] = "30"
        self.mode("one", "exit 3")
        self.up()
        self.wait(lambda: self.member("One")["state"] == "waiting", "One waits to restart")
        self.assertIn("stopped", self.up("stop", "One"))
        self.wait(lambda: self.member("One")["pane_dead"] == "1", "the wait ended", secs=15)
        self.assertEqual(self.member("One")["state"], "stopped")
        self.assertEqual(len((self.proj / "one" / "argv").read_text().splitlines()), 1)

    def test_a_lock_left_without_a_pid_goes_stale(self):
        (self.proj / ".squad" / "state" / ".up.lock").mkdir(parents=True)
        self.assertIn("One: started", self.up("up", "One"))
        self.assertFalse((self.proj / ".squad" / "state" / ".up.lock").exists())

    def test_two_quick_ups_make_one_window_per_agent(self):
        ps = [subprocess.Popen(["sh", str(ts.SCRIPT), "-C", str(self.proj), "up"], env=self.env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        for p in ps:
            self.assertEqual(p.wait(timeout=30), 0, p.stderr.read())
        self.up("up", "One", "One")
        self.wait(lambda: sorted(w["agent"] for w in self.windows()) == ["One", "Two"], "both agents")
        names = subprocess.run([*self.tmux, "list-windows", "-a", "-F", "#{window_name}"], capture_output=True,
                               text=True, check=True).stdout.split()
        self.assertEqual(sorted(names), ["one", "two"])

    def test_codex_agents_sharing_a_folder_keep_their_own_ids(self):
        (self.proj / ".squad" / "squad").write_text("squad Test\nagent A codex two\nagent B codex two\n")
        (self.proj / "two" / "rollout").write_text("")
        self.up()
        state = self.proj / ".squad" / "state"
        self.wait(lambda: all((state / f"{n}.id").exists() and (state / f"{n}.id").read_text().strip()
                              for n in "AB"), "both ids", secs=20)
        a, b = ((state / f"{n}.id").read_text().strip() for n in "AB")
        opened = {ln.split()[1] for ln in (self.proj / "two" / "rollouts").read_text().splitlines()}
        self.assertNotEqual(a, b)
        self.assertEqual({a, b}, opened)
        self.assertEqual({self.member("A")["resume"], self.member("B")["resume"]}, {a, b})

    def test_env_that_picks_programs_is_refused_and_args_are_not_globbed(self):
        (self.proj / ".squad" / "squad").write_text("squad Test\nenv DYLD_INSERT_LIBRARIES=/x\nagent One claude one\n")
        self.up("up", "One")
        self.wait(lambda: self.windows() and self.windows()[0]["pane_dead"] == "1", "the runner refuses")
        self.assertFalse((self.proj / "one" / "argv").exists())
        (self.proj / ".squad" / "squad").write_text("squad Test\nagent One claude one --x *\n")
        (self.proj / "one" / "zz-a-file").write_text("")
        self.up("start", "One")
        self.wait(lambda: (self.proj / "one" / "argv").exists(), "a start")
        self.assertEqual((self.proj / "one" / "argv").read_text().split()[2:], ["--name", "One", "--x", "*"])

    def test_up_from_inside_another_tmux_server_still_uses_the_default_server(self):
        # TMUX_TMPDIR makes a private "default" server; TMUX points at another private server, as in a pane there
        tdir = self.tmp / "tt"
        tdir.mkdir()
        default = [TMUX, "-S", str(tdir / f"tmux-{os.getuid()}" / "default")]
        subprocess.run([*self.tmux, "new-session", "-d", "-s", "elsewhere", "sleep 60"], check=True, env=self.env)
        pid = subprocess.run([*self.tmux, "display-message", "-p", "-t", "=elsewhere", "#{pid}"], capture_output=True,
                             text=True, check=True).stdout.strip()
        env = {k: v for k, v in self.env.items() if k != "SQUAD_TMUX"}
        env.update(TMUX_TMPDIR=str(tdir), TMUX=f"{self.sock},{pid},0")
        try:
            p = subprocess.run(["sh", str(ts.SCRIPT), "-C", str(self.proj), "up", "One"], env=env, capture_output=True,
                               text=True, timeout=30, check=False)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.wait(lambda: [w["agent"] for w in ts.discover(default)] == ["One"], "One on the default server")
            others = subprocess.run([*self.tmux, "list-sessions", "-F", "#{session_name}"], capture_output=True,
                                    text=True, check=True).stdout.split()
            self.assertEqual(others, ["elsewhere"])
            st = subprocess.run(["sh", str(ts.SCRIPT), "-C", str(self.proj), "status"], env=env, capture_output=True,
                                text=True, timeout=30, check=False).stdout
            self.assertIn("One", st)
        finally:
            subprocess.run([*default, "kill-server"], capture_output=True, check=False)  # our private one only

    def test_a_restored_look_alike_is_renamed_never_killed(self):
        subprocess.run([*self.tmux, "new-session", "-d", "-s", "squad-test", "-n", "one", "sleep 60"], check=True,
                       env=self.env)
        out = self.up()
        self.assertIn("renamed a look-alike window to one~restored", out)
        names = subprocess.run([*self.tmux, "list-windows", "-t", "=squad-test", "-F",
                                "#{window_name}|#{@agent}|#{pane_current_command}"], capture_output=True, text=True, check=True).stdout
        self.assertIn("one~restored||sleep", names)
        self.wait(lambda: {w["agent"] for w in self.windows()} == {"One", "Two"}, "both agents")

    def test_other_sessions_and_unmarked_windows_are_not_agents(self):
        subprocess.run([*self.tmux, "new-session", "-d", "-s", "work", "sleep 60"], check=True, env=self.env)
        subprocess.run([*self.tmux, "set-option", "-wq", "-t", "work", "@agent", "Fake"], check=True)
        self.up("up", "One")
        self.wait(lambda: [w["agent"] for w in self.windows()] == ["One"], "only One")
        self.wait(lambda: "Try" in "\n".join(ts.screen(self.member("One")["pane_id"], self.tmux)), "the prompt")
        snap = ts.snapshot(self.tmux)
        self.assertEqual([(s["name"], s["dir"]) for s in snap], [("Test", str(self.proj.resolve()))])
        page = ts.render(snap)
        self.assertIn("One", page)
        self.assertIn("Done: all tests pass.", page)


class Export(unittest.TestCase):
    """tmux-squad export: a squad-up folder from a tailshare squad's members on this machine."""

    def setUp(self):
        import json
        import sqlite3

        self.tmp = Path(tempfile.mkdtemp(prefix="tsx"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for d in ("app", "ios"):
            (self.tmp / d).mkdir()
        self.db = self.tmp / "bus.db"
        con = sqlite3.connect(self.db)
        con.executescript("CREATE TABLE squads(owner, id, doc, deleted);"
                          "CREATE TABLE squad_members(owner, squad, id, doc, deleted);"
                          "CREATE TABLE agents(id, squad, harness, last_seen);")
        con.execute("INSERT INTO squads VALUES('me', 'sq_1', ?, 0)", (json.dumps({"name": "My Squad"}),))
        def m(mid, name, folder, host="air", kind="claude", opts=None, instr="", order=0):
            con.execute("INSERT INTO squad_members VALUES('me', 'sq_1', ?, ?, 0)", (mid, json.dumps({
                "name": name, "kind": kind, "host": host, "folder": folder, "order": order, "instructions": instr,
                "start": {"options": opts or {}}})))
        m("m_pm", "PM", str(self.tmp / "app"), instr="You lead.", opts={"model": "opus", "effort": "high"}, order=1)
        m("m_dev", "Dev", str(self.tmp / "ios"), opts={"skip_permissions": True, "add_dirs": ["/x y"]}, order=2)
        m("m_cx", "Cx", str(self.tmp / "ios"), kind="codex", opts={"effort": "low"}, instr="Be brief.", order=3)
        m("m_far", "Far", str(self.tmp / "app"), host="other")
        m("m_gone", "Gone", str(self.tmp / "nope"))
        tag = lambda mid: json.dumps({"id": "sq_1", "member": mid}, sort_keys=True)
        con.executemany("INSERT INTO agents VALUES(?,?,?,?)", [
            ("old-pm", tag("m_pm"), "claude", 1), ("new-pm", tag("m_pm"), "claude", 2), ("empty-pm", tag("m_pm"), "claude", 3),
            ("cx-1", tag("m_cx"), "codex", 1)])
        # empty-pm is newest but never wrote a transcript (started, then ended before any turn)
        (self.tmp / "claude" / "projects" / "p").mkdir(parents=True)
        for sid in ("old-pm", "new-pm"):
            (self.tmp / "claude" / "projects" / "p" / f"{sid}.jsonl").write_text("{}\n")
        env = unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.tmp / "claude")})
        env.start()
        self.addCleanup(env.stop)
        con.commit()
        con.close()

    def test_writes_the_squad_file_instructions_and_resume_ids(self):
        out = ts.export("My Squad", "air", self.tmp / "sq", self.db)
        sq = self.tmp / "sq" / ".squad"
        lines = (sq / "squad").read_text().splitlines()
        self.assertIn("squad MySquad", lines)
        self.assertIn("env TAILSHARE_SQUAD=sq_1", lines)
        self.assertIn(f"agent PM claude {self.tmp / 'app'} keep --effort=high --model=opus", lines)
        self.assertIn(f"agent Dev claude {self.tmp / 'ios'} keep --dangerously-skip-permissions", lines)
        self.assertIn(f'agent Cx codex {self.tmp / "ios"} keep --config=model_reasoning_effort=low', lines)
        self.assertFalse(any(" Far " in x or " Gone " in x for x in lines))
        self.assertEqual(out["skipped"], ["Gone"])
        self.assertEqual(out["left_out"]["Dev"], ["add_dirs"])  # a space squad-up cannot pass
        self.assertIn("AGENTS.md", out["left_out"]["Cx"][0])
        pm = (sq / "PM.md").read_text()
        self.assertTrue(pm.startswith("You lead.\n\n## Reporting"))
        self.assertTrue((sq / "Dev.md").read_text().startswith("## Reporting"))
        self.assertFalse((sq / "Cx.md").exists())
        self.assertEqual((sq / "state" / "PM.id").read_text().strip(), "new-pm")
        self.assertEqual((sq / "state" / "Cx.id").read_text().strip(), "cx-1")
        self.assertFalse((sq / "state" / "Dev.id").exists())
        self.assertTrue(os.access(sq / "squad-up", os.X_OK))

    def test_never_overwrites_a_squad_file_and_needs_one_squad(self):
        ts.export("sq_1", "air", self.tmp / "sq", self.db)
        with self.assertRaises(ValueError):
            ts.export("sq_1", "air", self.tmp / "sq", self.db)
        with self.assertRaises(ValueError):
            ts.export("Nope", "air", self.tmp / "sq2", self.db)
        with self.assertRaises(ValueError):
            ts.export("sq_1", "nohost", self.tmp / "sq3", self.db)

    def test_hooks_find_the_member_by_squad_and_agent_name(self):
        from tailshare import agent

        self.assertEqual(agent.member_named("sq_1", "PM", self.db), "m_pm")
        self.assertEqual(agent.member_named("sq_1", "Nobody", self.db), "")
        self.assertEqual(agent.member_named("sq_1", "PM", self.tmp / "missing.db"), "")
        with unittest.mock.patch.dict(os.environ, {"TAILSHARE_SQUAD": "sq_1", "SQUAD_AGENT": "Dev"}, clear=False), \
                unittest.mock.patch.object(agent.bus, "STATE_DIR", self.tmp):
            os.environ.pop("TAILSHARE_MEMBER", None)
            self.assertEqual(agent.squad_tag(), {"id": "sq_1", "member": "m_dev"})


class Reader(unittest.TestCase):
    def test_last_line_skips_borders_prompt_and_footer(self):
        lines = ["● Wrote 3 files", "", "╭───╮", "│ > │", "╰───╯", "  ? for shortcuts", ""]
        self.assertEqual(ts.last_line(lines), "● Wrote 3 files")
        claude = ["● Fixed it.", "◐ medium · /effort", "────", "❯\u00a0Try \"create a util\"", "────",
                  "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"]
        self.assertEqual(ts.last_line(claude), "● Fixed it.")
        named = ["● Fixed it.", "          ✔ Update installed · /restart to apply", "──────── TailsharePM ─", "❯", "────"]
        self.assertEqual(ts.last_line(named), "● Fixed it.")
        codex = ["• Ran the tests.", "› Ask Codex to do anything", "  GPT-6 medium · ~/Projects/x",
                 "  ← for agents · ? for shortcuts"]
        self.assertEqual(ts.last_line(codex), "• Ran the tests.")
        self.assertEqual(ts.last_line([]), "")

    def test_state_names(self):
        self.assertEqual(ts.state({"pane_dead": "1", "@state": "running"}), "exited")
        self.assertEqual(ts.state({"pane_dead": "1", "@state": "failed"}), "failed")
        self.assertEqual(ts.state({"pane_dead": "0", "@state": "running", "@keep": "off"}), "stopping")
        self.assertEqual(ts.state({"pane_dead": "0", "@state": "waiting"}), "waiting")

    def test_attention(self):
        w = {"state": "running"}
        self.assertEqual(ts.attention(w, ["Do you want to proceed?", "❯ 1. Yes"]), "is asking something")
        self.assertEqual(ts.attention(w, ["✻ Working… (esc to interrupt)", "Do you want to see more?"]), "")
        self.assertEqual(ts.attention(w, ["● Done", "│ > │"]), "")

    def test_render_escapes(self):
        sq = [{"session": "squad-x", "name": "<X>", "dir": "/p", "members": [
            {"agent": "A<b>", "harness": "claude", "state": "failed", "tries": "5", "last": "<script>", "needs": "x"}]}]
        page = ts.render(sq)
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_init_never_overwrites_a_squad_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".squad").mkdir()
            (Path(d) / ".squad" / "squad").write_text("squad Mine\n")
            ts.init(Path(d), "Other")
            self.assertEqual((Path(d) / ".squad" / "squad").read_text(), "squad Mine\n")
            self.assertTrue(os.access(Path(d) / ".squad" / "squad-up", os.X_OK))

    def test_init_refuses_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".squad").mkdir()
            target = Path(d) / "elsewhere"
            target.write_text("keep me\n")
            (Path(d) / ".squad" / "squad-up").symlink_to(target)
            with self.assertRaises(ValueError):
                ts.init(Path(d), "X")
            self.assertEqual(target.read_text(), "keep me\n")

    def test_page_answers_only_its_own_host(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        srv = ThreadingHTTPServer(("127.0.0.1", 0), ts._Page)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request("GET", "/", headers={"Host": "evil.example:80"})
            self.assertEqual(c.getresponse().status, 421)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_the_script_never_kills_or_types(self):
        text = ts.SCRIPT.read_text()
        code = "\n".join(ln.split("#")[0] for ln in text.splitlines())  # comments may name what it never does
        for bad in ("kill-server", "kill-session", "kill-window", "kill-pane", "respawn-pane -k", "pkill", "killall",
                    "kill ", "send-keys"):
            self.assertNotIn(bad, code)



@unittest.skipUnless(TMUX, "needs tmux")
class LiveWindows(unittest.TestCase):
    """Every live window on a private tmux server: shells, programs, agents run by hand, and none of a gate's."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tsw", dir="/tmp"))
        self.sock = self.tmp / "s"
        self.tmux = [TMUX, "-f", os.devnull, "-S", str(self.sock)]
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(subprocess.run, [TMUX, "-S", str(self.sock), "kill-server"], capture_output=True)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.tmp), "TERM": "xterm"}
        t = self.tmux
        subprocess.run([*t, "new-session", "-d", "-s", "work", "-n", "sh", "sh"], check=True, env=env)
        subprocess.run([*t, "new-window", "-d", "-t", "work:1", "-n", "cc", "bash -c 'exec -a claude sleep 60'"],
                       check=True, env=env)
        subprocess.run([*t, "new-window", "-d", "-t", "work:2", "-n", "cx",
                        "sh -c 'bash -c \"exec -a codex sleep 60\"; :'"], check=True, env=env)
        subprocess.run([*t, "new-window", "-d", "-t", "work:3", "-n", "zz", "sleep 60"], check=True, env=env)
        subprocess.run([*t, "new-session", "-d", "-s", "tailshare-123-456", "sleep 60"], check=True, env=env)
        subprocess.run([*t, "link-window", "-s", "work:1", "-t", "tailshare-123-456:5"], check=True)
        subprocess.run([*t, "new-session", "-d", "-t", "work", "-s", "work-2"], check=True, env=env)  # a group

    def rows(self):
        end = time.time() + 10
        while time.time() < end:
            rows = ts.windows(self.tmux)
            if [r["kind"] for r in rows] == ["shell", "claude", "codex", "program"]:
                return rows
            time.sleep(0.2)
        self.fail(f"kinds: {[(r['name'], r['kind'], r['command']) for r in ts.windows(self.tmux)]}")

    def test_every_window_once_with_its_kind_and_none_of_a_gates(self):
        rows = self.rows()
        self.assertEqual([(r["session"], r["index"], r["name"]) for r in rows],
                         [("work", 0, "sh"), ("work", 1, "cc"), ("work", 2, "cx"), ("work", 3, "zz")])
        self.assertEqual({tuple(r["sessions"]) for r in rows}, {("work", "work-2")})
        self.assertEqual({r["socket"] for r in rows}, {str(self.sock)})
        self.assertTrue(all(r["state"] == "running" and r["pane_id"].startswith("%") for r in rows))
        self.assertNotIn("agent", rows[1])

    def test_the_bus_answers_only_the_owners_devices_and_names_registered_panes(self):
        from tailshare import bus
        from tests.test_bus import StaticNet, make_bus

        net = StaticNet("air", usermap={"100.1.1.1": "nick@example.com", "100.2.2.2": "eve@example.com"})
        b = make_bus("air", net)
        pane = self.rows()[1]["pane_id"]
        b.register({"id": "A", "harness": "claude", "cwd": "/x/api", "pid": os.getpid(),
                    "tmux": {"socket": str(self.sock), "pane": pane, "target": "", "bin": TMUX}})
        b.register({"id": "B", "harness": "claude", "cwd": "/x/old", "pid": 999999,  # a dead process that had %0
                    "tmux": {"socket": str(self.sock), "pane": self.rows()[0]["pane_id"], "target": "", "bin": TMUX}})
        real = ts.windows
        with unittest.mock.patch.object(ts, "windows", lambda _t: real(self.tmux)):
            out = b.live_windows("100.1.1.1")
            with self.assertRaises(bus.BusError):
                b.live_windows("100.2.2.2")
        self.assertEqual([r.get("address", "") for r in out["windows"]], ["", "air/claude/api", "", ""])
        self.assertEqual(out["socket"], str(self.sock))
        self.assertEqual(b.hello()["windows"], 1)

    def test_the_process_tree(self):
        procs = ({1: [2], 2: [3], 3: [4]}, {1: "zsh", 2: "bash", 3: "node", 4: "x"})
        self.assertEqual(ts.harness_under(1, procs), "")
        self.assertEqual(ts.harness_under(1, ({1: [2]}, {1: "zsh", 2: "codex"})), "codex")
        self.assertEqual(ts.harness_under(7, ({}, {7: "claude"})), "claude")
        deep = ({i: [i + 1] for i in range(1, 10)}, {9: "claude"})
        self.assertEqual(ts.harness_under(1, deep), "")  # not past a few levels

if __name__ == "__main__":
    unittest.main()
