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

# A stand-in for claude/codex. Its folder's `mode` file says what it shows: `idle` (an empty prompt), `ask`
# (a permission question), `exit N`. Typed lines go to `typed`; argv to `argv`.
FAKE = r"""#!/bin/bash
d=$PWD
printf '%s\n' "$*" >> "$d/argv"
while :; do
  mode=$(cat "$d/mode" 2>/dev/null || echo idle)
  case "$mode" in
    exit*) exit "${mode#exit }" ;;
    ask) printf '\033[2J\033[H Bash command\n   rm -rf build\n Do you want to proceed?\n ❯ 1. Yes\n   2. No\n' ;;
    *) printf '\033[2J\033[H● Done: all tests pass.\n╭────────╮\n│ >      │\n╰────────╯\n  ? for shortcuts\n' ;;
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

    def test_stop_at_the_idle_prompt_types_exit_and_the_window_stays(self):
        self.up()
        self.wait(lambda: "> " in "\n".join(ts.screen(self.member("One")["pane_id"], self.tmux)), "the prompt")
        self.assertIn("asked to exit", self.up("stop", "One"))
        self.wait(lambda: self.member("One")["state"] == "stopped", "One stopped")
        self.assertEqual((self.proj / "one" / "typed").read_text(), "idle|/exit\n")
        self.assertEqual(self.member("Two")["state"], "running")
        self.assertIn("stopped", self.up("status"))
        self.up("start", "One")
        self.wait(lambda: self.member("One")["state"] == "running", "One runs again")

    def test_stop_while_it_asks_types_nothing_until_it_is_idle(self):
        self.mode("one", "ask")
        self.up()
        self.wait(lambda: "Do you want" in "\n".join(ts.screen(self.member("One")["pane_id"], self.tmux)), "the ask")
        self.assertEqual(ts.attention(self.member("One"), ts.screen(self.member("One")["pane_id"], self.tmux)),
                         "is asking something")
        self.assertIn("stops when it finishes", self.up("stop", "One"))
        self.assertEqual(self.member("One")["state"], "stopping")
        self.assertIn("stops when it finishes", self.up("status"))
        time.sleep(4)
        self.assertFalse((self.proj / "one" / "typed").exists())  # nothing landed as an answer
        self.mode("one", "idle")
        self.wait(lambda: self.member("One")["state"] == "stopped", "One stopped once idle", secs=20)
        self.assertEqual((self.proj / "one" / "typed").read_text(), "idle|/exit\n")

    def test_start_cancels_a_pending_stop(self):
        self.mode("one", "ask")
        self.up()
        self.wait(lambda: "Do you want" in "\n".join(ts.screen(self.member("One")["pane_id"], self.tmux)), "the ask")
        self.up("stop", "One")
        self.up("start", "One")
        self.mode("one", "idle")
        time.sleep(5)
        self.assertEqual(self.member("One")["state"], "running")
        self.assertFalse((self.proj / "one" / "typed").exists())

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
        snap = ts.snapshot(self.tmux)
        self.assertEqual([(s["name"], s["dir"]) for s in snap], [("Test", str(self.proj.resolve()))])
        page = ts.render(snap)
        self.assertIn("One", page)
        self.assertIn("Done: all tests pass.", page)


class Reader(unittest.TestCase):
    def test_last_line_skips_borders_prompt_and_footer(self):
        lines = ["● Wrote 3 files", "", "╭───╮", "│ > │", "╰───╯", "  ? for shortcuts", ""]
        self.assertEqual(ts.last_line(lines), "● Wrote 3 files")
        self.assertEqual(ts.last_line([]), "")

    def test_state_names(self):
        self.assertEqual(ts.state({"pane_dead": "1", "@state": "running"}), "exited")
        self.assertEqual(ts.state({"pane_dead": "1", "@state": "failed"}), "failed")
        self.assertEqual(ts.state({"pane_dead": "0", "@state": "running", "@keep": "off", "@stopping": "1"}), "stopping")
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

    def test_the_script_never_kills(self):
        text = ts.SCRIPT.read_text()
        code = "\n".join(ln.split("#")[0] for ln in text.splitlines())  # comments may name what it never does
        for bad in ("kill-server", "kill-session", "kill-window", "kill-pane", "respawn-pane -k", "pkill", "killall",
                    "kill "):
            self.assertNotIn(bad, code)


if __name__ == "__main__":
    unittest.main()
