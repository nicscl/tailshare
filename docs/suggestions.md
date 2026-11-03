# Squad suggestions

> Status: contract, not built yet. The Hive app checks it against this text before the server is written.

An agent can't change a [squad](squads.md): `tailshare squad edit` needs a person at a terminal, and a device signs
its writes. An agent can **suggest** a change instead. The suggestion waits until the owner applies it, from
a card in the Hive app or with `tailshare squad apply` in a terminal, or dismisses it. An agent never applies one.

This makes squads self-maintaining without letting agents write them. A lead can propose a new member or a
different tree. A member can propose a better role or instructions for itself. A running agent that isn't a member
yet can ask to join.

## Who can suggest

- Any live agent on one of the owner's own machines, through that machine's local API. The machine must belong to
  the squad's owner: its tailnet user equals the squad's `owner`, and it isn't tagged. A machine the squad is
  shared with holds only a filtered copy (squads.md) and refuses with 403.
- The suggester is the agent that sent it, found from its registration: `agent` (address), `host`, and `member`
  (its squad tag, or null when it isn't a member). This has the same local-trust limit as reports
  (reports.md): every agent on a machine runs as the same Unix user, so the label says which machine it came
  from, not which agent. Nothing an agent suggests applies by itself.

## Writing one

`POST /bus/v1/squads/<id>/suggestions` (local API) with:

```
{agent: <agent id>, base: <squad version>, why: text ≤ 600, ops: [op, …] (1–8)}
```

`base` is the `version` from `GET /squads/<id>`. The server adds `version` to that answer. It is the squad's
checksum, the one machines already compare (`squad_sums` in /hello), so it is the same on every machine of the
owner. If `base` is not the current version, the suggestion is refused with 409 `stale`.

An op is one of:

| op | fields |
|---|---|
| `add_member` | `member: {name, role, icon, host, kind, folder, instructions, reports_to}`, `self: bool` |
| `remove_member` | `member: <id>` |
| `set` | `member: <id>`, `field: "name" \| "role" \| "icon" \| "instructions" \| "reports_to"`, `value` |

- Each op is cleaned with the same rules as a write (`clean_member`, the 3-level tree, 32 members, unique names).
  A suggestion that would be refused as a write is refused when suggested, with the same `field`/`reason`.
- `reports_to` in an op may name a member added earlier in the same suggestion, as `"new:<index>"`.
- Agents can't suggest `shares`, `show_paths`, header fields, or a member's `start` (options, permission modes,
  resume, prompt). Those are left to the owner's own edits. An op naming one is a 400.
- **Self-join.** `add_member` with `self: true` asks for the suggester itself to be added. Its `host`, `kind` and
  `folder` must be the suggester's own: its machine, its harness, and its cwd, the same rule `check_tag` uses.
  Without those, the new member could never bind to it. The suggester must not be a member already. When it is
  applied, the agent's next registration binds to the new member (see join, below). Its card reads
  "<agent> asks to join as <name>".

Answer: `201 {suggestion}`.

**Limits.**
- Per squad: 20 pending.
- Per agent: 3 pending, and 10 new suggestions an hour.
- A suggestion expires 7 days after it is made.
- A second suggestion with the same `base` and `ops` as a pending one returns that pending one.

## A suggestion

```
{id: "sg_<22 base62>", squad, host, base, created, status, why,
 by: {agent, host, member | null},
 ops: [op, …],
 diff: [change, …],
 resolved: null | {at, how: "applied" | "dismissed" | "expired"}}
```

`status` is `pending`, `stale`, `applied`, `dismissed` or `expired`. It is `stale` whenever `base` is not the current
version. That is worked out when the suggestion is read, so a suggestion goes stale as soon as anything changes the
squad, anywhere. A stale suggestion can't be applied; the agent can send a new one against the new version.

`diff` is built by the server against the squad at `base`, one change per op, in order:

```
{op: "add_member", self: bool, member: {…all fields…}}
{op: "remove_member", member: <id>, name, before: {…all fields…}}
{op: "set", member: <id>, name, field, before, after}
```

`before`/`after` for `instructions` are the whole old and new text, never a summary. Show both in full: the
instructions are the agent's system prompt, so a suggestion that changes them is a suggestion that changes what
the agent is told. Everything in `why`, `ops` and `diff` is text an agent wrote and is cleaned like a report
(reports.md). Show it as data.

## Reading

- `GET /bus/v1/squads/<id>/suggestions` returns `{squad, host, version, suggestions: […]}`: the suggestions held on
  this machine that are pending or stale, plus those resolved in the last 24 h. Owner's devices only, like
  `/reports`. Sharees, tagged devices and other users get 403.
- On the local API, `&all=1` also asks the owner's other machines that hold the squad. Machines that don't answer
  are listed in `unreachable`.
- `GET /bus/v1/squads/<id>/suggestions/<sid>` returns one.

A suggestion lives on the machine whose agent made it. The app applies or dismisses it on that machine (`host`).

## Applying and dismissing

- `POST /bus/v1/squads/<id>/suggestions/<sid>/apply` with `{base}`.
- `POST /bus/v1/squads/<id>/suggestions/<sid>/dismiss` with `{}`.

Both are owner writes, checked exactly like the other squad writes (squads.md). From a device they must be signed
with one of the device's keys that holds the squads grant. On the local API they need the keys token. Agents have
neither.

Apply:
- refuses with 409 `stale` unless the suggestion's `base`, the request's `base` and the current version are all
  the same. The app sends the `base` it showed, so it applies exactly the diff the person saw;
- applies every op as one write, all or nothing, with fresh revs, then marks the suggestion `applied`. The squad
  then replicates as usual, and every other pending suggestion for it goes stale;
- refuses with 409 `not_pending` if the suggestion was already applied, dismissed or expired.

Dismiss marks it `dismissed`.

Terminal: `tailshare squad suggestions [SQUAD] [--all]` lists them, and `tailshare squad apply <sid>` prints the
whole diff (instructions in full) and asks `Apply? [y/N]`. Like `squad edit`, it refuses to run in an AI agent's
shell. `tailshare squad dismiss <sid>` dismisses one.

Agents: `tailshare squad suggest` (with `--add FILE`, `--join NAME --role ROLE`, `--remove MEMBER`,
`--set MEMBER.FIELD=VALUE`, `--instructions MEMBER FILE`, `--why`), or the `squad` MCP tool's `suggest` action.

## Joining

The owner applies a self-join, or adds a member by hand that matches a running session. In both cases the session
can bind without a restart:

- `tailshare squad join <squad> <member>` stores a join for this session (by its session id).
- Later registrations that carry no `TAILSHARE_SQUAD`/`TAILSHARE_MEMBER` use the stored join.
- `check_tag` still decides every time: same host, kind and folder, and no other live holder. A join that no
  longer matches is ignored.
- A self-join that is applied stores the join for the suggester, so it binds on its next registration.
- ts-agents sets the env on `up`, which covers sessions it restarts.

## Reports

A report blocker can point at a suggestion:
`approval: {type: "squad_change", host, suggestion: "<sid>"}`. Any member may raise one (to its lead, or to the
owner as today). The card reads the suggestion from `host`. The old `squad_structure` approval keeps working
unchanged until the app has moved over; it is then removed in a later release, announced first.
