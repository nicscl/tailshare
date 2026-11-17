# Squad suggestions

> Status: implementation in PR #42; app and server use this contract.

An agent can't change a [squad](squads.md): `tailshare squad edit` needs a person at a terminal, and a device signs
its writes. An agent can **suggest** a change instead. The suggestion waits until the owner applies it, from
a card in the Hive app or with `tailshare squad apply` in a terminal, or dismisses it. An agent never applies one.

This makes squads self-maintaining without letting agents write them. A lead can propose a new member or a
different tree. A member can propose a better role or instructions for itself. A running agent that isn't a member
yet can ask to join.

## Who can suggest

- Any live agent on one of the owner's own machines, through that machine's local API. The machine must belong to
  the squad's owner: its tailnet user equals the squad's `owner`, and it isn't tagged. A machine of another
  user never holds the squad: squads only sync between the owner's machines (squads.md). It answers 404, as for
  any squad it doesn't have.
- The suggester is the agent that sent it, found from its registration: `agent` (address), `host`, and `member`
  (its squad tag, or null when it isn't a member). This has the same local-trust limit as reports
  (reports.md): every agent on a machine runs as the same Unix user, so the label says which machine it came
  from, not which agent. Nothing an agent suggests applies by itself.

## Writing one

`POST /bus/v1/squads/<id>/suggestions` (local API) with:

```
{agent: <agent id>, base: <squad version>, why: text ≤ 600, ops: [op, …] (1–8), nonce?: <intent key>}
```

**Correcting a proposal: reuse its nonce.** `nonce` (1–64 of `A-Z a-z 0-9 . _ : -`) names what the suggestion is
for. Without one the server picks a fresh one; every suggestion returns its `nonce`. A new suggestion with the
same nonce, from the same agent (its address, so it survives a resume), in the same squad:
- **replaces** that agent's pending one (stale included): the old one becomes `superseded`, with `superseded_by`
  set to the new id, and can no longer be applied or dismissed (409 `not_pending`). The new one has
  `supersedes`. One pending suggestion per intent: the latest wins;
- with none pending but one already **applied**, links to it as `corrects`, for the owner to see it is a
  correction. That changes nothing about either one.

CLI: `tailshare squad suggest … --nonce <key>`; MCP: the `squad` tool's `nonce`.

The nonce is not a secret: every view shows it. "The same agent" is its address, and an address is reused once
the session holding it is no longer running, so a later session with the same name that reads the nonce can
replace that pending suggestion. Nothing applies without the owner either way: the owner sees the replacement.

`base` is the top-level `version` from `GET /squads/<id>` (beside `squad`, not inside it).
It is the squad's checksum, the one machines already compare (`squad_sums` in /hello), so it is the same on every machine of the
owner. If `base` is not the current version, the suggestion is refused with 409 `stale`.

An op is one of:

| op | fields |
|---|---|
| `add_member` | `member: {name, role, icon, host, kind, folder, instructions, reports_to}`, `self: bool` |
| `remove_member` | `member: <id>` |
| `set` | `member: <id>`, `field: "name" \| "role" \| "icon" \| "instructions" \| "reports_to"`, `value` |

- An `add_member` that isn't a self-join may only place the new member on one of the owner's own machines: its
  `host` must be a machine of the squad's owner (the same tailnet user, not tagged). Otherwise 400 with
  `field: "host"`.
- Each op is cleaned with the same rules as a write (`clean_member`, the 3-level tree, 32 members).
  A suggestion that would be refused as a write is refused when suggested, with the same `field`/`reason`.
  Suggestions also require unique final member names: collapse whitespace runs, then Unicode lowercase;
  no accent folding. This is checked when suggested and again when applied.
- Removing a member that others still report to is refused. Set their `reports_to` first, in earlier ops.
- `reports_to` in an op may name a member added earlier in the same suggestion, as `"new:<index>"`.
- Agents can't suggest `shares`, `show_paths`, header fields, or a member's `start` (options, permission modes,
  resume, prompt). Those are left to the owner's own edits. An op naming one is a 400.
- **Self-join.** `add_member` with `self: true` asks for the suggester itself to be added. Its `host`, `kind` and
  `folder` must be the suggester's own: its machine, its harness, and its cwd, the same rule `check_tag` uses.
  Without those, the new member could never bind to it. The suggester must not be a member already. When it is
  applied, the agent is that member from its next ts-agents `up` (see Joining, below). Its card reads
  "<agent> asks to join as <name>".

Answer: `200 {suggestion}`.

**Limits.**
- Per squad: 20 pending. Stale suggestions do not consume pending quotas.
- Per agent: 3 pending across squads, and 10 new suggestions an hour (including ones now stale). One that
  replaces a pending one does not add to the 3, but it does count toward the 10 an hour.
- A suggestion expires 7 days after it is made.
- A second suggestion with the same `base` and `ops` as a pending one returns that pending one.

## A suggestion

```
{id: "sg_<22 base62>", squad, host, base, created, status, why,
 by: {agent, host, member | null, name?: <member name at base>},
 ops: [op, …],
 diff: [change, …],
 nonce, supersedes: <sid> | null, superseded_by: <sid> | null, corrects: <sid> | null,
 resolved: null | {at, how: "applied" | "dismissed" | "expired" | "superseded"}}
```

`by.member` is the suggester's member **id** (or null), not its name. The optional `by.name` snapshots the member name at
`base`, so removed or renamed authors keep that label. Older suggestions without it fall back to `by.agent`;
the app must not substitute a later member name.

`status` is `pending`, `stale`, `applied`, `dismissed`, `expired` or `superseded`. It is `stale` whenever `base` is not the current
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
- refuses with 409 `not_pending` if the suggestion was already applied, dismissed, expired or superseded.

Dismiss marks it `dismissed`, including a stale suggestion. A resolved or expired one returns 409
`not_pending` with `{suggestion}`. HTTP errors use the `error` field (`stale` or `not_pending`); `reason`
is optional explanatory detail. Successful apply returns `{suggestion, squad}` with the full updated squad;
there is no new-member-id mapping. Match an applied self-join by exact host/kind/folder from its op in that
snapshot. Successful dismiss returns `{suggestion}`.

Terminal: `tailshare squad suggestions [SQUAD] [--all]` lists them, and `tailshare squad apply <sid>` prints the
whole diff (instructions in full) and asks `Apply? [y/N]`. Like `squad edit`, it refuses to run in an AI agent's
shell. `tailshare squad dismiss <sid>` dismisses one, with the same human check. Apply and dismiss must run on the
suggestion's host; use Hive to act from another device. Listing defaults to this session's squad, or all local
squads when the session is unbound. `--all` also queries other owner machines.

Agents: `tailshare squad suggest [SQUAD]` (with `--add FILE`, `--join NAME --role ROLE`, `--remove MEMBER`,
`--set MEMBER.FIELD=VALUE`, `--instructions MEMBER FILE`, `--why`), or the `squad` MCP tool's `suggest` action.

CLI operations are ordered: additions, self-join, sets, instructions, removals. Reparenting with `--set`
therefore precedes removal. For other dependent changes, use the MCP `suggest` action with an explicitly
ordered `ops` array. `--set` values
are text except `reports_to`: a member name/id, `new:<index>`, or `null` to clear it. `--add` reads one member
JSON object per file. MCP takes `action: "suggest"`, optional `squad`, required `why` and `ops`; omitting the
action keeps the existing read behavior.

## Joining

A session becomes a member through its environment: ts-agents sets `TAILSHARE_SQUAD`/`TAILSHARE_MEMBER` when it
brings an agent `up`, as the start gate does for sessions it starts. `check_tag` decides on every registration:
same host, kind and folder, and no other live holder. A member that crashed and comes back up gets its member
back on its next registration.

So once a self-join is applied, the suggester is that member from its next `up`. The member's `host`, `kind` and
`folder` are the suggester's own, so the env binds it. There is no `squad join` for a running session; one was
written and parked (#41).

## Reports

A report blocker can point at a suggestion:
`approval: {type: "squad_change", host, suggestion: "<sid>"}`. Any member may raise one (to its lead, or to the
owner as today). The card reads the suggestion from `host`. The old `squad_structure` approval keeps working
unchanged until the app has moved over; it is then removed in a later release, announced first.
