# Starting sessions from a device

A device of yours (the Hive app on a phone or iPad) can start a Claude Code or Codex session on one of your
machines, in a new tmux window there, and then attach to it like any other agent. It is **off** on every
machine until that machine's own config turns it on, and it needs a separate key that a person approves.

## Turning it on

Starting is off unless `~/.tailshare/start.json` exists, belongs to you, is a regular file (not a symlink) and
is not writable by group or others. tailshare never writes it and nothing syncs it: you write it on each
machine that should accept starts.

```json
{
  "enabled": true,
  "roots": ["~/Projects"],
  "max_running": 12,
  "skip_permissions": false,
  "kinds": {
    "claude": {"options": ["permission_mode", "model", "effort", "resume", "add_dirs", "skip_permissions"]},
    "codex": {"options": ["approval", "sandbox", "model"], "bin": "/opt/homebrew/bin/codex"}
  }
}
```

- `roots`: the only folders a session may start in, or below (after resolving symlinks and `..`). `/` is not
  accepted as a root.
- `kinds`: which agents may be started, and which options a device may set for each. A kind or option missing
  here is refused. `bin` is optional: by default the agent is looked up in `~/.local/bin`, `~/.claude/local`,
  `/opt/homebrew/bin`, `/usr/local/bin` and `/usr/bin`, and must not be writable by others.
- `max_running`: how many windows tailshare-started sessions may have open at once (1 to 100, default 12).
- `skip_permissions`: whether a session may run with permissions skipped at all (see below). Default false.

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

You can grant less than the device asked for, never more. An attach request cannot be approved as a start
key, nor a start request as an attach key or a shell. The installed line is:

```
restrict,command="<tailshare> ssh-gate --start=safe --key=k3f2a91c0d1e2" ecdsa-sha2-nistp256 AAAA... tailshare:k3f2a91c0d1e2 iPhone
```

`restrict` means no pty, no forwarding and no `~/.ssh/rc`. The gate runs `tailshare-start` and nothing else.

In the Hive app the start key lives in the Secure Enclave and needs Face ID on each use, so each SSH call to a
host takes one Face ID. That protects against someone using your unlocked phone, or a copy of the app's iSH
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
would run: the host, kind, folder, name, `skip`, the exact argv (the instructions appear once, as the plan's
`instructions`, with `<instructions>` standing for them in the argv), the environment and the tmux window.
Nothing has started yet. Plans are stored in `~/.tailshare/start.db` (owner-only).

**Confirm.** `{"v": 1, "confirm": ["<plan_id>", ...]}`. For each plan the machine checks again, from scratch:
the plan must belong to this key, be unused and less than 5 minutes old, and building it again now (the config,
the folder, the grant) must give exactly the same plan, or the answer is `changed`. Then it starts it and
answers `{"plan_id", "session", "tmux": {"socket", "pane", "bin"}}`. A plan runs once: confirming it again
returns the first answer with `"already": true`.

The device then waits for the new agent to check in on the bus, and attaches through `/hello` as usual. It
never attaches to the returned pane directly.

### How the session is started

- In a new window of tmux session `tailshare-start` on your default tmux server, named after the session.
- As argv, never through a shell. Every option is one `--flag=value` element, so a value can never be read as
  another flag. The prompt is one element after `--`. Names lose control and format characters and must then
  be 1 to 64 characters; model and effort must match `[A-Za-z0-9._-]{1,64}`; `resume` must be a UUID; `add_dirs`
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
| claude | `skip_permissions: true`, `permission_mode: bypassPermissions`, `permission_mode: auto` |
| codex | `skip_permissions: true`, `sandbox: danger-full-access` |

Each needs all three: `skip_permissions: true` in the machine's config, the key granted `--start=skip`, and the
device asking for it. The plan says `"skip": true`, and the app shows it in red. An agent with permissions
skipped runs any command without asking.

This is the one place tailshare starts sessions that may skip permissions. Relaunching an old session
([sessions.md](sessions.md)) still never accepts `bypassPermissions`.

## Limits and errors

- 32 items per request; 6 confirms a minute per key (`rate`, with `retry_after` in seconds).
- `max_running` windows across tailshare's sessions (`limit`).

| error | meaning |
|---|---|
| `bad_request` | malformed request or field (`field` says which) |
| `not_allowed` | the config does not allow this kind, option or folder |
| `no_grant` / `no_skip_grant` | the key cannot start / cannot skip permissions |
| `off` | starting is off on this machine |
| `not_installed` / `no_tmux` | the agent / tmux is not installed |
| `running` | a session with this name is already running |
| `limit` / `rate` | too many sessions / too many confirms |
| `expired` / `unknown_plan` / `changed` | dry run again |
| `root` | tailshare runs as root here |
| `failed` | tmux refused; `reason` says why |

## What a device can see

`/hello` carries a `start` block (`kinds`, the options per kind and whether `skip` is possible) only to devices
of the machine's own tailnet user, and only when starting is on. `GET /bus/v1/sessions/start/folders` lists the
folders under the roots (two levels deep, at most 200, no hidden folders or symlinks) to the same devices only.
