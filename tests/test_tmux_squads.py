"""squad-up and the tmux squad reader, on a private tmux server with fake agents (never a real agent, never the
person's own tmux server)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
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
        self.assertEqual((self.proj / "one" / "argv").read_text().split()[:4], ["--session-id", sid, "--model", "x"])
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
        self.assertEqual((self.proj / "one" / "argv").read_text().split()[2:], ["--x", "*"])

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


class Reader(unittest.TestCase):
    def test_last_line_skips_borders_prompt_and_footer(self):
        lines = ["● Wrote 3 files", "", "╭───╮", "│ > │", "╰───╯", "  ? for shortcuts", ""]
        self.assertEqual(ts.last_line(lines), "● Wrote 3 files")
        claude = ["● Fixed it.", "◐ medium · /effort", "────", "❯\u00a0Try \"create a util\"", "────",
                  "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"]
        self.assertEqual(ts.last_line(claude), "● Fixed it.")
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


if __name__ == "__main__":
    unittest.main()
