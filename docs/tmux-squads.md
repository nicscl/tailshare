# Squads on plain tmux (squad-up)

A squad is a normal tmux session on the default tmux server: `squad-<name>`, with one window per agent. Anyone
with tmux can create one, attach to it and read it, with or without tailshare. tmux itself holds the state. There
is no database and no reconciler. This sits next to the older `tailshare start`/keep path and replaces nothing
yet.

`up`, `start`, `stop` and `status` always use the default server, even when run from a pane of another tmux
server: they drop an inherited `$TMUX`, which would otherwise build a second squad on that server with a duplicate
agent on the same session id. `SQUAD_TMUX` picks another server on purpose. Only the runner inside an agent's
window trusts `$TMUX`.

**The tooling never kills anything.** It never runs kill-server, kill-session, kill-window or kill-pane, and never
`respawn-pane -k`. tmux refuses `respawn-pane` without `-k` on a live pane. To stop an agent, it types the agent's
own `/exit` into the window, and only while the agent sits idle at its prompt.

## The squad file

`<project>/.squad/squad`, checked in:

```
squad Protos
env TAILSHARE_HOOKS=0               # optional, for every agent
agent ProtosPM     claude .       keep
agent QueueMonitor claude ./queue keep --model opus
agent Reviewer     codex  .       once
```

- `agent <Name> <claude|codex> <folder> [keep|once] [args...]`.
- `keep` (the default) restarts the agent when it exits. `once` runs it a single time.
- Folders are relative to the project.
- Extra args and env values are split on spaces, with no quoting and no globbing. They cannot hold spaces.
- Agent names are unique; a repeated name uses the first line.

**The squad file is code.** It decides which program runs, with which flags (skip-permissions included) and
environment. Review a change to it like a change to a Makefile. `env` lines may not set PATH, DYLD_*, LD_*,
NODE_OPTIONS, NODE_PATH, BASH_ENV, ENV, PYTHONPATH or PYTHONSTARTUP: the runner refuses to start.
- Claude instructions go in `.squad/<Name>.md` and are passed with `--append-system-prompt`. For Codex, use the
  folder's AGENTS.md.

`tailshare tmux-squad init [folder]` copies `squad-up` into `.squad/` and writes a starter squad file if there is
none. It never overwrites a squad file. It replaces the script atomically, so running windows keep reading the
old copy, and it refuses when any of `.squad`, `squad-up`, `squad` or `.gitignore` is a symlink. Without tailshare, copy `tailshare/squad-up` there by hand.

## Commands

```sh
.squad/squad-up up [Name...]    # create what is missing; start agents whose window has stopped
.squad/squad-up status          # agents, state, tries, folder
.squad/squad-up stop Name       # auto-restart off; asks the agent to exit once it is idle
.squad/squad-up start Name      # auto-restart on; runs a stopped or failed agent again, in its window
```

`up` is safe to run again, and two at once are safe: a lock in `.squad/state` lets one change the squad at a
time (a lock whose owner died is taken over), and a window `up` just created counts as its agent before its runner has started.
- A missing session is created.
- A missing agent gets a window.
- An agent whose pane has ended is respawned.
- Everything else is left as it is.

The script finds the squad by walking up from the current folder, or from `-C <folder>`.

## Convention (what tailshare and Hive read)

| option | on | value |
|---|---|---|
| `@squad`, `@squad_dir` | session | squad name, project folder |
| `@agent` | window | agent name. **Only a window with `@agent` is an agent.** |
| `@harness`, `@cwd` | window | `claude`/`codex`, the agent's folder |
| `@keep` | window | `on`/`off`: auto-restart |
| `@state` | window | `running`, `waiting` (between tries), `failed` (retry limit), `stopped` |
| `@stopping` | window | set while a stop waits for the agent to go idle |
| `@tries`, `@last_exit`, `@resume` | window | retry count, last exit code, session id |

Discovery is one `tmux list-windows -a` with those formats (`tailshare tmux-squad ls`). Resume ids are also kept
in `.squad/state/<Name>.id` (git-ignored), because tmux options do not survive the server.

## Restarts and Stop

Each agent window runs `squad-up run <Name>`, which works like this:
- It starts the agent, and resumes its own session when there is one.
  - Claude gets a fixed `--session-id` the first time and `--resume <id>` afterwards.
  - For Codex, the id is the session file that this run's own Codex process holds open (`lsof`, or /proc on
    Linux). If none was seen, it falls back to the oldest session file for that folder created during the run
    that no other agent in the squad already claims. Two Codex agents in one folder keep their own sessions.
- When the agent exits and `@keep` is on, it waits and starts it again. The waits are 10 s, 20 s, 40 s and so on,
  at most 10 min.
- A run of at least 5 min resets the count.
- After 5 short runs in a row, it gives up and sets `@state failed`. The pane stays, dead and showing its last
  output (`remain-on-exit`), and nothing else is touched.
- `squad-up start` runs it again.
- These limits can be changed with SQUAD_MAX_TRIES, SQUAD_BACKOFF_SECS, SQUAD_BACKOFF_MAX and SQUAD_HEALTHY_SECS.

`stop` turns `@keep` off first. What happens next depends on what the agent is doing:
- **Idle at its prompt:** `stop` types `/exit` (`/quit` for Codex), and the window ends as `stopped`.
- **Working, or asking a question** (a permission prompt, `(y/n)`, a numbered choice): nothing is typed, because
  it would land as an answer. Status shows **stops when it finishes**. A background `tmux run-shell -b` waiter
  types the exit as soon as the agent is idle.
- **`start` before then:** the pending stop is cancelled.

Idle means all of, and anything else counts as busy. It fails closed: a screen it does not recognise means
"did not stop yet", never "typed into something".
- the screen shows none of: "esc to interrupt/cancel", "Enter to confirm", "Do you want", "Would you like",
  `(y/n)`, "Press enter", a numbered choice, or a footer naming background work (`· 1 shell`, "to manage");
- from the prompt line down, the screen matches a known idle shape (an allowlist). The prompt line holds nothing,
  or only dim text (Claude and Codex's placeholder; read with colours, `capture-pane -e`). Every line below it is
  empty, a box border, or one of these footers:
  - Claude: `⏵⏵ <mode> (shift+tab to cycle)` with an optional `· PR #N` and `· ← N agents`; `← for agents · ? for
    shortcuts`; `? for shortcuts`;
  - Codex: `<model> [effort] · <folder>`; `NN% context left`.

  A new footer, a menu or a draft (normal text on any line, also after an empty first line) blocks the stop until
  the allowlist learns it;
- when tailshare's hooks report the agent's state (`/bus/v1/agents` on 127.0.0.1, by its resume id), it is
  `idle`. Hooks can only hold a stop back, never allow one: Claude's Stop hook says idle while a background shell
  of its own still runs;
- no client showing the agent's window, from any session (grouped, linked, a viewer's), did anything in the
  last 60 s (`client_activity`, SQUAD_QUIET_SECS).

Right before typing, the waiter reads `@keep` and its own `@stop_waiter` again, so a `start` in between wins.
This is still a screen heuristic, so it waits rather than guesses: when in doubt the agent keeps running.

## tmux-resurrect

resurrect saves window names and folders but not user options. Restored windows come back as shells without
`@agent`, so they are never taken for agents. When `up` sees an agent missing and a window named like it without
`@agent`, it renames that window `<name>~restored` and leaves it running. It then creates the real window, which
resumes from `.squad/state`. Nothing in `~/.tmux.conf` is changed.

## The default page

`tailshare tmux-squad page` prints it; `tailshare tmux-squad serve` serves it on 127.0.0.1:8792. It shows:
- every squad on this tmux server;
- each agent's state, its tries, and the last real line on its screen;
- a **Needs you** list: failed, exited without restarting, or asking something.

It reads tmux only (`list-windows`, `capture-pane -p`). It runs no scripts and loads nothing from elsewhere.
It answers only requests whose Host is `127.0.0.1:<port>` or `localhost:<port>` (421 otherwise), so a web page
cannot read it through DNS rebinding.

## Not in this step

- The SSH gate and admin key.
- Hive.
- The bus member list from tmux.
- Per-squad own pages.
- Starting at login: nothing starts at login. A later per-squad switch can run `squad-up up` at login.
