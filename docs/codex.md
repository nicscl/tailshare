# Codex sessions

Codex sessions are agents and squad members like Claude Code sessions. This page is the field contract for
apps and the setup for a machine.

## Setup

```sh
tailshare agent install-codex      # hooks in ~/.codex/hooks.json + `codex mcp add tailshare -- tailshare mcp`
```

Codex runs a new hook only after the person trusts it: open `/hooks` in Codex once and trust tailshare's.
tailshare never marks its own hooks trusted. `uninstall-codex` removes only tailshare's hooks (yours stay; a
backup is kept as `hooks.json.bak-tailshare*`) and the MCP server.

The hooks do what Claude's do:
- `SessionStart`: register, tell the session its address, its squad and messages that waited.
- `UserPromptSubmit`: show new messages with the prompt.
- State hooks (`curl` to the local mailbox, always exit 0) on `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
  `PostToolUse`, `PermissionRequest`, `Stop`, `SessionEnd`: `working`, `waiting_input`/`permission`, `idle`, `ended`.

Nothing wakes an idle Codex session from the background, so a Codex agent waits for mail in the foreground with
`tailshare agent watch --once`, or reads it with the next prompt.

## Identity

- The agent id is the Codex thread id (`CODEX_THREAD_ID`, or `session_id` in a hook). The address's session
  part is the thread name, or `TAILSHARE_AGENT_NAME` (set by tailshare-start) while the thread has none.
- A Codex started from a Claude session's shell (or the reverse) inherits both sessions' variables. The
  nearest `claude` or `codex` process above the caller decides which one it is.
- The MCP server (`tailshare mcp`) finds its session the same way, so `report`, `send`, `inbox` and `squad`
  work from Codex.

## Fields

| Where | Field | Values |
|---|---|---|
| `/hello` `agents[]` | `harness` | `claude`, `codex`, `cli` |
| `/hello` `agents[]` | `session_id` | the Claude session id, or the Codex thread id |
| `/hello` `agents[]` | `tmux`, `state` | same shape for both |
| squad members | `kind` | `claude`, `codex` |
| member reports (`GET …/report`) | `harness` | the member's `kind` |
| dead sessions (`/start/dead`) | `kind` | `claude`, `codex` |
| sessions (`/sessions`) | `harness`, `id` | `id` is the Codex thread id |

## Starting and resuming (tailshare-start)

`kinds.codex.options` in `~/.tailshare/start.json` may list:

| Option | Value | Codex flag |
|---|---|---|
| `model` | `[A-Za-z0-9._-]` word | `--model` |
| `effort` | word, e.g. `low`, `medium`, `high`, `xhigh` | `-c model_reasoning_effort="…"` |
| `approval` | `untrusted`, `on-request`, `never` | `--ask-for-approval` |
| `sandbox` | `read-only`, `workspace-write`, `danger-full-access` | `--sandbox` |
| `skip_permissions` | bool (needs `skip_permissions` in the config) | `--dangerously-bypass-approvals-and-sandbox` |
| `add_dirs` | folders under the roots | `--add-dir` |
| `resume` | a thread id this host lists as dead | `codex resume <id>` |

`/hello` `start.models.codex` lists the models this host's Codex offers in its picker (from
`~/.codex/models_cache.json`; empty when unknown), present when `model` is allowed.

A plan's `session` is `""` for a new Codex member (Codex picks the thread id when it starts) and the thread id
when resuming. The member's agent appears once the session's first hook registers it. Codex has no name flag:
the name comes from `TAILSHARE_AGENT_NAME`.

`tailshare sessions` finds Codex threads by id like Claude sessions. `sessions launch` resumes one too
(`codex resume <id>` in a tmux window, as tailshare-start does; see [sessions.md](sessions.md)), and a device
reopens one through tailshare-start with `options.resume`.
