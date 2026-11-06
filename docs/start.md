# Starting sessions from a device

A device of yours (the Hive app on a phone or iPad) can start a Claude Code or Codex session on one of your
machines, on a tmux server of its own there, and then attach to it like any other agent. It is **off** on every
machine until that machine's own config turns it on, and it needs a separate key that a person approves.

## Turning it on

Starting is off unless `~/.tailshare/start.json` exists, belongs to you, is a regular file (not a symlink) and
is not writable by group or others. Nothing syncs it: each machine has its own. You do not have to write it by
hand. Either of these writes it for you:

- the app, with a start key already installed on that machine: one tap on the `enable_start` fix (below), which
  asks for Face ID like any start;
- `tailshare start enable` in your own terminal on that machine (it refuses in an AI agent's shell and without a
  terminal, and asks before changing anything). `tailshare start` alone shows what the machine takes, or why not.

On a machine with no start.json, enabling writes these defaults, mode 0600: your `~/Projects` as the only root
(if it is a plain folder of yours; else none, and the next fix is `add_root`), Claude and Codex with every option
except `skip_permissions`, `skip_permissions` and `auto` off, `max_running` 12. On a machine whose start.json says
`"enabled": false`, it only sets `enabled` back to true. Both are written whole (a temp file renamed into place,
never through a symlink) under a lock.

### Changing it from a device

With the start key (Face ID; the plan key is refused with `no_grant`), a device sends one change alone, `{"v":1,
"<change>": {…}}`, on the same `tailshare-start` channel as a dry run:

| change | does |
|---|---|
| `{"enable": {}}` | turns starting on, as above |
| `{"add_root": {"folder": "/Users/me/Work"}}` | adds a root: an absolute folder held to the same rules as a squad folder outside the roots (docs/squads.md, "Starting a member"): a plain folder of this user in the home folder, not the home folder, `~/Library`, a hidden folder of the home folder or a `.git` folder, with no symlink on the path and nobody else able to write to it or a folder above it. Nothing changes if a root already covers it. |
| `{"allow": {"kind": "codex"}}` | allows an agent, with every option but `skip_permissions` |
| `{"allow": {"kind": "claude", "option": "effort"}}` | allows one more option of an allowed agent |
| `{"revive": {"on": false}}` | turns the revive watchdog off (or on): see "Revive" |
| `{"trust_codex_hooks": {}}` | trusts tailshare's own Codex hooks here (docs/codex.md, "Trusting the hooks"); works with starting off |

The answer is `{"v":1, "ok":true, "start":{…}}` with the new /hello `start` block, or a refusal with a `fix`.
Changes count against the key's confirm rate. A device never changes `skip_permissions`, `auto`, `bin`,
`models` or `max_running`, and never allows the `skip_permissions` option (`not_allowed`, fix `terminal`):
skipping stays a person's choice at the machine.

On the owner's devices, a machine where starting is off sends `start_off` in /hello in place of `start`: the
refusal it would give, with its fix, e.g. `{"error":"off","reason":"…","fix":{"action":"enable_start"}}`. A tagged
machine sends neither.

```json
{
  "enabled": true,
  "roots": ["~/Projects"],
  "max_running": 12,
  "skip_permissions": false,
  "auto": false,
  "kinds": {
    "claude": {"options": ["permission_mode", "model", "effort", "resume", "add_dirs", "skip_permissions"]},
    "codex": {"options": ["approval", "sandbox", "model"], "bin": "/opt/homebrew/bin/codex"}
  }
}
```

- `roots`: the only folders a session may start in, or below (after resolving symlinks and `..`). `/` is not
  accepted as a root. One exception: a squad member's own folder, named by the owner's signed squad, may be
  outside them if it is a plain folder of this user in the home folder (docs/squads.md, "Starting a member").
- `kinds`: which agents may be started, and which options a device may set for each. A kind or option missing
  here is refused. `bin` is optional: by default the agent is looked up in `~/.local/bin`, `~/.claude/local`,
  `/opt/homebrew/bin`, `/usr/local/bin` and `/usr/bin`, and must not be writable by others. `models` is optional:
  more model values to offer and accept for that kind, e.g. `"claude": {"options": ["model"], "models": ["fable"]}`.
- `max_running`: how many tailshare-started agents may run at once (1 to 100, default 12).
- `skip_permissions`: whether a session may run with permissions skipped at all (see below). Default false.
- `auto`: whether a Claude session may run in auto mode (`permission_mode: auto`, see below). Default false.
- `revive`: whether the bus reopens a started session whose pane died without the session ending (see "Revive",
  below). Default true; `false` turns it off. `revive_per_session` (0 to 100, default 3) and `revive_per_host` (0 to
  1000, default 10) cap the reopens an hour.

tailshare running as root never starts anything.

## The start key

The key a device uses to attach to panes can never start sessions. A device that wants to start them enrolls
a second key with `purpose: "start"`, through the same `POST /bus/v1/keys/enroll` flow as in
[ssh-keys.md](ssh-keys.md), and says what it wants: `want: ["start"]`, or `["start", "skip"]`.

```sh
tailshare keys pending                         # a start key says "start key" (and "wants skip")
tailshare keys approve 3f2a91c0                # grants what it asked for
tailshare keys approve 3f2a91c0 --start=safe   # grants less: start, never skip permissions
```

A start request carries two keys: the start key (`pubkey`) and its **plan key** (`plan_pubkey`, a different
key). `keys approve` shows both fingerprints and installs both, as a pair. The code the person types is the
last 6 characters of the **pair fingerprint**, which the device shows instead of the start key's:
`"SHA256:" + base64(sha256(start key blob ‖ plan key blob))` without `=` padding, each blob the decoded base64
field of its key line (both P-256, so of fixed length). It covers both keys, so a request that pairs someone
else's start key with its own plan key does not get the device's code. The request view and `keys pending`
carry `pair_fingerprint`. A pending request with the same start key and another plan key blocks approving
either one until the wrong one is denied. The plan key can only dry-run:
the gate never confirms a plan or attaches to anything for it. Both lines share the key id, so a plan made with
the plan key belongs to the pair and the start key can confirm it. Removing the key (`tailshare keys rm`, or
the device revoking it) removes both lines.

You can grant less than the device asked for, never more. An attach request cannot be approved as a start
key, nor a start request as an attach key or a shell. The installed lines are:

```
restrict,command="<python> -I -m tailshare ssh-gate --start=safe --key=k3f2a91c0d1e2" ecdsa-sha2-nistp256 AAAA... tailshare:k3f2a91c0d1e2 iPhone
restrict,command="<python> -I -m tailshare ssh-gate --start=safe --key=k3f2a91c0d1e2 --plan" ecdsa-sha2-nistp256 BBBB... tailshare:k3f2a91c0d1e2 plan iPhone
```

`<python>` is the interpreter tailshare runs with, and `-I` keeps `PYTHONPATH`, `PYTHONSTARTUP` and the current
folder from loading code before the gate starts. With no plain interpreter path to write, the line runs
`/usr/bin/false`.

`restrict` means no pty, no forwarding and no `~/.ssh/rc`. The gate runs `tailshare-start` and nothing else. Once
the gate runs, nothing the client sent in its environment changes where or how a session starts: it takes HOME
from the password database, uses a fixed PATH and drops `TAILSHARE_*`/`TMUX*`/`PYTHON*` variables. sshd still
runs the line through your login shell, so that shell's startup files (`~/.zshenv`, say) run first and see any
variables sshd accepted: keep `AcceptEnv` to what you need and `PermitUserEnvironment` off.

In the Hive app both keys live in the Secure Enclave. The start key needs Face ID on each use; the plan key
does not, so dry runs cost no Face ID and a start costs one per host's confirm (the app confirms a multi-host
launch back to back under one Face ID). A copy of the plan key can only make plans, and at most
128 open plans per key (`rate` past that). That protects against someone using your unlocked phone, or a copy of the app's iSH
files. It does not protect against a compromised phone or app, and the machine cannot check that a person was
present: it only sees a valid signature.

## Dry run, then confirm

The device runs `ssh -T <you>@<machine> tailshare-start` with one JSON request on stdin (at most 256 KB) and
reads one JSON line back (at most 512 KB). The exit status is 0 only if every item succeeded.

**Dry run.** Up to 32 starts at once:

```json
{"v": 1, "dry_run": true, "starts": [
  {"kind": "claude", "cwd": "/Users/nick/Projects/app", "name": "AppDev",
   "options": {"model": "opus", "permission_mode": "acceptEdits"},
   "instructions": "You are AppDev...", "prompt": "Pick up the login bug", "device_name": "Nick's iPhone"}]}
```

Each result is either `{"plan_id", "expires", "plan"}` or `{"error", "field"?, "reason"?}`. The plan is what
would run: the host, kind, folder, name, `skip`, the exact argv (the instructions and the prompt appear once, as the
plan's `instructions` and `prompt`, with `<instructions>` and `<prompt>` standing for them in the argv), the environment and the tmux window.
Nothing has started yet. Plans are stored in `~/.tailshare/start.db` (owner-only).

**Confirm.** `{"v": 1, "confirm": ["<plan_id>", ...]}`. For each plan the machine checks again, from scratch:
the plan must belong to this key, be unused and less than 5 minutes old, and building it again now (the config,
the folder, the grant) must give exactly the same plan, or the answer is `changed`. Then it starts it and
answers `{"plan_id", "session", "tmux": {"socket", "pane", "bin"}}`. A plan runs once: confirming it again
returns the first answer with `"already": true`.

The device then waits for the new agent to check in on the bus, and attaches through `/hello` as usual. It
never attaches to the returned pane directly.

### How the session is started

- By the bus (`tailshare serve`), not by the ssh gate. The gate runs under sshd, in an audit session of its own,
  and an agent started there could not reach the login keychain, where Claude keeps its sign-in. So the gate
  checks the confirm and approves the plan, then asks the bus, which runs in your GUI session, over a unix
  socket in `~/.tailshare/launch/` (0700, this user only) to launch it. The request names only the plan. The
  bus loads it from `start.db` itself and launches it only if the gate approved it in the last 30 seconds and
  nobody launched it before, so nothing that reaches the socket can launch more than a plan a device signed
  and confirmed, once. Without the bus running, the start is refused (`no_bus`); it never falls back to
  launching under sshd.
- On its own tmux server, `/tmp/tmux-<uid>/ts-<name>` (the one `tmux -L ts-<name>` reaches), never your
  default server: a crash or `kill-server` there takes down one agent, not all of them. The server holds one
  session, `tailshare-start` (or `squad-<squad>` for a squad member), with one window named after the agent.
  The folder must be this user's and closed to others (0700), or the start is refused (`no_tmux`).
- If a server of that name already has any session, the start is refused (`running`), so a start never lands
  inside a server it did not make (such as one `ts-agents` runs).
- The server, and so the agent, starts with a clean environment: HOME, USER, LOGNAME, SHELL, a fixed PATH,
  LANG and TERM, plus the variables below. Nothing from the ssh login or the gate's own process.
- Its pane is (socket, pane) everywhere, in the answer, in `/hello` and at the gate: `%N` alone is never enough,
  since every server numbers its panes from `%0`.
- As argv, never through a shell. Every option is one `--flag=value` element, so a value can never be read as
  another flag. The prompt is one element after `--`. Names lose control and format characters and must then
  be 1 to 64 characters; effort must match `[A-Za-z0-9._-]{1,64}`; model must match `[A-Za-z0-9._:\[\]-]{1,64}`, not
  start with `-`, and be one of the host's models (`start.models`, below; else `not_allowed`, field `options.model`); `resume` must be a UUID; `add_dirs`
  must be under the roots like the folder.
- Claude Code gets `--name`, `--session-id` (chosen at the dry run, so the device knows it),
  `--append-system-prompt` for the instructions, and the options. Codex gets `--cd`, the instructions as
  `--config=developer_instructions=<TOML string>`, and the options.
- The environment adds `TAILSHARE_AGENT_NAME` and `TAILSHARE_STARTED_BY` (the device's name, cleaned).
- The same name cannot be started twice while it is still running (`running`).

## Skipping permissions

These all count as skipping permissions:

| kind | values |
|---|---|
| claude | `skip_permissions: true`, `permission_mode: bypassPermissions` |
| codex | `skip_permissions: true`, `sandbox: danger-full-access` |

Each needs all three: `skip_permissions: true` in the machine's config, the key granted `--start=skip`, and the
device asking for it. The plan says `"skip": true`, and the app shows it in red. An agent with permissions
skipped runs any command without asking.

This is the one place tailshare starts sessions that may skip permissions. Relaunching an old session
([sessions.md](sessions.md)) still never accepts `bypassPermissions`.

## Auto mode

Claude's `permission_mode: auto` is its own mode, not skipping permissions: Claude decides which actions to
take without asking, with its own checks. It needs `auto: true` in the machine's config and `permission_mode`
among the options allowed for claude; it does not need the `--start=skip` grant. The plan says `"auto": true`
so the app can label it. Without `auto: true` it is refused (`not_allowed`, field `options.permission_mode`).

## Limits and errors

- 32 items per request; 6 confirms a minute per key; 128 open (unconfirmed) plans per key (`rate`, with
  `retry_after` in seconds).
- `max_running` started agents at once, counted over every `ts-*` server plus windows that older versions
  started on the default server (`limit`, with `max_running` as a number). A dry run says how many run now
  (`running`, when tmux can tell) and the `max_running` at the top, and the items past what still fits get `limit`
  instead of a plan; a confirm checks again and stops at the limit.

| error | meaning |
|---|---|
| `bad_request` | malformed request or field (`field` says which) |
| `not_allowed` | the config does not allow this kind, option or folder |
| `no_grant` / `no_skip_grant` | the key cannot start (or is the plan key, confirming) / cannot skip permissions |
| `off` | starting is off on this machine |
| `not_installed` / `no_tmux` | the agent / tmux is not installed |
| `running` | a session with this name is already running (or, with `options.resume`, that session is) |
| `not_dead` / `cwd` | `options.resume` names no session in this host's dead list / not in its own folder |
| `limit` / `rate` | too many sessions / too many confirms |
| `expired` / `unknown_plan` / `changed` | dry run again |
| `root` | tailshare runs as root here |
| `failed` | tmux refused; `reason` says why |
| `no_bus` | the bus is not running on this host (or did not say its name), so nothing was planned or launched |

### Fixes

Every refusal, at the top or in a result, carries `fix`: what the app may offer as one tap. `action` is one of:

| action | the app | from |
|---|---|---|
| `enable_start` | sends `{"enable":{}}` | `off` with no start.json, or `"enabled": false` |
| `add_root` | sends `{"add_root":{"folder": fix.folder}}`; without `folder`, asks for one first | a folder or `add_dirs` entry outside the roots, or `off` with field `roots` |
| `allow_kind` | sends `{"allow":{"kind": fix.kind}}`; without `kind`, asks which | `not_allowed` kind, or `off` with field `kinds` |
| `allow_option` | sends `{"allow":{"kind": fix.kind, "option": fix.option}}` | `not_allowed` option |
| `retry` | waits `fix.after` seconds | `rate` |
| `dry_run_again` | dry-runs the same starts again | `expired`, `unknown_plan`, `changed` |
| `close_sessions` | offers to close running sessions | `limit` |
| `open_session` | opens the running member | `running` for a squad member |
| `terminal` | says it needs the person at that machine; `fix.command` when there is one | skipping or auto mode the machine or key does not allow, `not_installed`, `no_tmux`, `root`, `no_bus` (`tailshare install`), a start.json that is not sound |
| `none` | shows `reason` | anything else (a malformed request, a folder that does not exist) |

A refusal outside the roots also says `folder`: the resolved path, as the device sent it. A `not_allowed` kind or
option also says `kind` (and `option`).

## Reopening a dead session

`GET /bus/v1/sessions/dead` (devices of the machine's own tailnet user only, header `X-Tailshare: 1`) lists the agent
sessions this machine ran in the last 7 days whose tmux pane is gone (or taken by a newer agent), newest first, at most
50:

```json
{"host": "macbook-air", "max_age": 604800, "limit": 50, "sessions": [
  {"id": "<session uuid>", "kind": "claude", "address": "macbook-air/claude/app/appdev", "name": "AppDev",
   "folder": "app", "cwd": "/Users/me/projects/app", "title": "AppDev", "squad": {"id": "…", "member": "…"} | null,
   "last_seen": 1793390000.0, "ended": false, "resumable": true}]}
```

- `ended` is true when the session ended (its SessionEnd hook, or Close from a device), with `ended_at`, epoch
  seconds. It is false when the pane went without that: a crash, a killed server or a `kill-pane`, which is what
  the revive watchdog reopens. Older tailshare leaves it out.

- `last_seen` is epoch seconds. `name` is the session's name (`--name` or `/rename`), which a reopen keeps; `title`
  is the same, at most 120 characters.
- Only sessions with an id to resume by (Claude's session uuid, Codex's thread id), never an agent that is online,
  and never one on a `ts-*` server that `tailshare start` did not make (ts-agents brings those back itself).
  `tailshare start` made it when its pane is in a `tailshare-start` or `squad-*` session, or when it is a squad
  member that start has started on this host (start.db `member_starts`) whose recorded session is only a gate's.
  A device viewing a pane links its window into a gate's own session (`tailshare-<pane>-<pid>`); the agent row
  keeps its own session, never the gate's.
- `resumable: false` comes with a `reason`: `kind` (a harness start does not know: Claude and Codex sessions
  reopen), `off` (starting is off here, or that kind's options do not include `resume`), `cwd` (its folder is gone or outside the roots), `name`.
- `/hello` has `"sessions_dead": 1` where the route exists, and for the owner's devices each Claude or Codex agent
  carries `session_id`, so a device can tell a live session from a dead one and a started tab from its agent, and
  `cwd`, the absolute folder it works in (a squad member's `folder`). Other users' devices get neither.

To reopen one, a device sends an ordinary start item with `options.resume` set to the id, `cwd` the recorded
`cwd` and `name` the recorded `name`; it is signed, dry-run and confirmed like any start and runs on its own server.
A squad member reopens as that member (its squad's tmux session, `TAILSHARE_SQUAD`/`TAILSHARE_MEMBER`). Several at
once are just several items (at most 32).

## Revive

The bus looks every 30 s for a session it would list as dead (above) that is resumable now, whose agent was last seen
within 15 minutes, and that did not end: no `SessionEnd`, no Close. That is a crash: tmux died, or its pane or
server was killed. It reopens it as a device's Reopen would: a start that resumes it, in its own folder under its
own name, re-checked against start.json now. It uses the options the session was started with here (start.db
`launched`; a new Codex thread's id is known only when it registers, so its start is recorded then, if it registers
from the pane that start made, in the same folder, within 10 min, once per start), and never more than the start key that approved that start may do now: it skips permissions only if it
was started skipping and that key is still installed here as a skip key. A key approved again without skip, or one
that was removed, means no reopen at all (`stopped: key`, no try spent), and the owner is told as past a cap
(below). Reopening it without skip would not be the same session. If the key is approved again as a skip key
while the session is still fresh enough to reopen (15 min), the next look reopens it with skip, on its own. A
session started before launches were recorded gets only `resume`, without skipping. A squad member reopens as
that member. Only Claude sessions reopen, as above.

A crash is anything that takes the pane away without the session ending: a tmux crash, a killed server, and also
a `tmux kill-pane` or `kill-server` by hand. To stop one for good, end it (`/exit`, or Close from a device), or turn
`revive` off.

It is bounded so that a crash loop ends up with the owner:

- backoff: a session's 2nd reopen in an hour waits 30 s after its 1st, the 3rd 2 min, then 10 min;
- `revive_per_session` reopens of one session an hour, where dying within 2 min of a reopen (or never coming up)
  counts twice; `revive_per_host` an hour on the host, where every session on one tmux server dying at once counts
  as one;
- past a cap, it stops reopening and tells the owner once an hour: a squad member gets a `to: owner` question
  blocker with id `revive` in its report, which shows in the board's Needs you. Every session it stopped, members
  and single sessions alike, is also in /hello `start.revive_stopped` (below).
  The blocker never pushes out the member's own open blockers: it replaces older resolved ones, and with 8 open
  already it is only logged. A Reopen from a device still works.

For the owner's devices, /hello says what it did:

- `agents[].revived`: `{at, count}`, the last reopen of that session and how many in the last hour. It is absent
  when the session was never reopened.
- `start.revive_stopped`: `[{session, name, crashes, at, why}]`, the sessions it stopped reopening in the last
  24 h, where `why` is `session` or `host` (a cap) or `key` (its start key). `crashes` counts this hour's reopens plus
  the crash it did not reopen. An entry goes once the session ends, comes back up or is past 15 min. It is absent
  when there are none.

A device turns it on or off with the start key (Face ID), like the other changes above: `{"v":1,"revive":{"on":
false}}`. That writes only `revive`. Turning it on never widens anything, since a reopen never does more than its
start key may now. The caps stay a person's, in start.json.

Every reopen, refusal and stop is a `revive …` line in tailshare.log. A refusal (`limit`, a folder now outside the
roots) counts as a try, so it backs off too.

## Keep running (squad members)

Every squad member runs until tailshare stops it. Each host looks after its own members: at serve start, on wake,
when a peer comes back and every 30 s it checks each member whose `host` is this machine, and starts the ones that
are down. A `/exit`, a clean quit and a crash all count as down. Only a stop ends one:

- `tailshare agent stop <member>` / `keep` / `restart` (on the member's host, or by a lead from anywhere), or
- Hive's Stop, Keep running and Restart: `POST /squads/<sid>/members/<mid>/run` `{"run":"stopped"|"keep"|"restart"}`,
  signed like every squad write, sent to the member's host;
- a device's Close of a member tailshare started.

Who may: the owner (the local API token, or a signed request from a device), and the member's leads in the squad
(its `reports_to` chain), never the member itself. A lead on another machine sends it there (peer
`POST /bus/v1/member/run`), and that machine checks the lead really runs where the request came from.
A stop closes the member's pane only if tailshare started it; one started by hand keeps running, said as
`still_running`, and is not started again once it ends.

How it starts one: it resumes the member's last session (its own tag, or an untagged session of its kind, folder
and name), as a device's Reopen would; if that does not come up, the next try starts fresh in the same folder and
name. Never a second copy: a member counts as running while its agent is live, or quiet but its process (pid, or
its tmux pane) still runs, or quiet with neither a pid nor a pane to look at (outside tmux, pid 0) for less than
30 min, or a session started by hand matches it, or a window tailshare started for it is still
waiting (up to 2 min, then `stuck`, still left alone: it may wait on a prompt). A member whose pane belongs to
ts-agents (`tmux -L ts-<name>`) is left to ts-agents while that pane is there.

The start key rule of Revive (above) holds: a restart never does more than the key that approved the member's
last start may do now. A removed key, or a skip start whose key is now only safe, means no restart (`blocked`,
`why: key`) and a Needs you item. A member started by hand restarts safe.

Failures back off 0, 30 s, 1, 2, 4, 8 min, then every 10 min, forever. After 5 in a row the owner gets one
`to: owner` question blocker, id `keep`, with the last output of its pane; it clears once the member stays up
10 min, which also resets the count. `start.json` must be there and allow the member, else `blocked`, `why: off`.

On upgrade, members that were not running then are set `stopped` (by `upgrade`), so nothing old starts at once;
Keep running starts them.

/hello `keep` (owner's devices) is one entry per member: `{squad, member, name, state, …}` with `state` one of
`running`, `starting`, `restarting` (`attempt`, `next_at`, `needs_you`), `blocked` (`why`), `stopped` (`by`, `at`,
`still_running`) or `host_offline` (`host`, `since`) for members of a machine that is off. The desired state is
in this host's start.db (`member_run`, `member_keep`). Members it keeps are not Revive's.

## Closing a session

The start key (with Face ID; never the plan key) may also end sessions `tailshare start` started here, on the same
`tailshare-start` command:

```json
{"v": 1, "close": ["<session uuid>", …]}
{"v": 1, "results": [{"session_id": "<session uuid>", "closed": true, "tmux": {"socket": "…/ts-appdev", "pane": "%0"}}]}
```

One to 32 ids per call, each counted against the same per-key rate as confirms (`rate`, with `retry_after`). The
bus, which launched it, kills the agent's pane (its per-agent server exits with its last pane), waits for the pane to
go, and marks the session ended, so it is in `/sessions/dead` at once and a reopen of it is not refused as `running`.
Only a live agent (seen within 90 s, not ended) whose pane is on this user's own `ts-*` server, in a
`tailshare-start` or `squad-*` session: never the user's own panes, never ts-agents' servers. Starting must be on.

| `error` | meaning |
|---|---|
| `bad_request` | malformed request, or `close` with `starts`/`confirm`/`dry_run`, or an id that is not a session uuid |
| `no_grant` | the key cannot start, or it is the plan key |
| `off` / `root` / `no_bus` / `no_tmux` | as for starts |
| `not_running` | no live session with this id here, or its pane is gone already |
| `not_started` | the session runs here, but `tailshare start` did not start it |
| `failed` | the pane was still there after 5 s; `reason` says why |

## What a device can see

`/hello` carries a `start` block (`kinds`, the options per kind, whether `skip` is possible and, when claude's options include
`permission_mode`, `permission_modes: {"claude": [...]}`, the modes a device may pick: `auto` only with `auto:
true`, `bypassPermissions` only with `skip_permissions: true`; and, for each kind whose options include `model`,
`models: {"<kind>": [...]}`, the only values a start takes, at most 30: start.json's `models` first, then Claude's
aliases (`opus`, `sonnet`, `haiku`, `opus[1m]`, `sonnet[1m]`) or the models Codex's own picker lists
(`~/.codex/models_cache.json`), then the full ids that the owner's sessions of that kind on this host ran in the last
30 days, newest first, as `/hello`'s `agents[].model` names them; not those of someone ssh'd in, and none on a
tagged machine) only to devices
of the machine's own tailnet user, and only when starting is on. `GET /bus/v1/sessions/start/folders` lists the
folders under the roots (two levels deep, at most 200, no hidden folders or symlinks) to the same devices only.
