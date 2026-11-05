# Squad reports

Each member of a [squad](squads.md) keeps a short report: who it is, what it is doing, what it finished, what it
is waiting on, and the decisions it needs. The squad page in the app shows them. The owner reads the reports
instead of every session.

## Writing one

A member writes only its own report, through the local bus. The bus finds the member from the agent's squad tag
(see "Agents" in squads.md), never from the request. An agent that isn't running, or isn't a member, gets
`409 not_a_member`.

A session started by hand (plain `claude` or `codex`, so no tag in its environment) still counts as a member when
exactly one member of the owner's squads matches it on this machine. The match needs the same kind, the same folder
(real path) and the same name: the session's name, as a slug, equals the member's. The same check as a tag then
applies, so no other live agent may already be that member. Name the session after its member (`/rename`) to report.

```
tailshare report set --bio "AppDev: the iOS app." --now "Squad page https://github.com/nicscl/tailshare-hive/pull/253" --next "Blockers UI"
tailshare report done "Squad page"
tailshare report block key-air --title "Enroll my key on air" --key air:3f2a91c0 --to owner
tailshare report resolve key-air
tailshare report ls              # the whole squad, from every machine
```

After a write the CLI prints one `✓` line for what changed, then your `now` and `next` tasks and your open blockers.
Add `--full` to print the whole report, as `tailshare report` does. A task title over 80 characters is cut to 80, ending
in `…`, rather than refused. The reply lists the fields it cut under `cut`, and the CLI prints a note for each one.

The `report` MCP tool takes the same fields as JSON. With `squad: true` it reads the squad's reports instead.
`tailshare start` appends a short "## Reporting" block to a member's instructions that says all this.

**A known limit.** The bus trusts the `agent` id a local process sends, as `/inbox` and `/ack` already do. Every
agent on a machine runs as the same Unix user, and the local `/agents` list shows each agent's id. So a local agent can
write another member's report. That includes blockers to the owner, and a structure proposal written as a top-level
member. Read a report as a claim made on that machine, not as proof of which agent wrote it. Closing this needs a
per-agent secret or a peer-credential check, for the whole local API.

Fields, all optional. A write replaces the fields it names and keeps the others:

| field | shape |
|---|---|
| `bio` | text, ≤ 600 characters |
| `now`, `next`, `done` | up to 3, 5 and 20 tasks: `"title"` or `{title ≤ 80, url}`; longer titles are cut |
| `waiting_on` | `{member: <member id> \| "owner", text ≤ 120}` or `null` |
| `blockers` | up to 8 open (see below) |
| `notes` | Markdown, ≤ 8 KB. Shown only for a lead |

The bus stamps `done` tasks and blockers with its own time and keeps a stamp when the item comes back unchanged.
Done tasks and resolved blockers show for 24 hours.

## Blockers

```
{id, kind: "question" | "approval", title ≤ 80, why ≤ 600, options: up to 4 × ≤ 40,
 to: "lead" | "owner", for: {member, blocker} | null, approval: null | {...}, resolved: bool}
```

That is the shape an agent writes. A read has `at` (when the bus first saw it) and `resolved_at` (epoch seconds or
`null`) in place of `resolved`. Tasks always come back as `{title}` or `{title, url}`, and done tasks have `at`.

- A member with a lead raises blockers `to: "lead"`. Only a lead or a top-level member raises one `to: "owner"`.
- A lead can relay a blocker from its branch with `for`, so the owner sees whose it is.
- `approval` is one of:
  - `{type: "key_enroll", host, request_id}`: a pending `tailshare keys` request the owner should approve on
    that machine. `request_id` is 8–32 lowercase hex characters, a prefix of the request's id.
  - `{type: "squad_structure", reports_to: {member: lead | null}}`: a proposed tree. Only a top-level member
    proposes one. The owner applies it with `tailshare squad edit`.

The tree decides at read time, not only when the report is written. If the owner moves members, a blocker its
author may no longer raise drops off the page, and so do a lead's notes. A `key_enroll` for this machine shows only
while exactly one pending, unexpired request matches its id. An agent can name any id, so the card never shows for a
request that isn't there.

## Reading

`GET /bus/v1/reports?squad=<id>` returns `{squad, host, at, reports: [...]}` for the members on this machine. Each
report has `member, agent, online, at, lead, bio, now, next, done, waiting_on, blockers, notes`. A member that isn't
running keeps its last report, with `online: false`.

Only the owner's own devices may read it, from the same tailnet user. Sharees, tagged devices and other users get
403. On the local API, `&all=1` also asks the owner's other machines that have members. Machines that don't answer
are listed in `unreachable`.

The owner's devices also get `report_sums` in `/hello`: `{squad_id: digest}`, a 16-hex digest of what this route
serves for that squad, without `online` and `agent` (those come and go with sessions; `/hello`'s agents list has
them). Refetch `/reports?squad=<id>` when it changes, so a new blocker (an ask with up to 4 options, to the
owner) shows on the next `/hello`, not the next reports poll. Sharees and other users never get it.

Everything in a report is text an agent wrote. The bus removes control and bidi characters and refuses anything
over a limit instead of cutting it. Links must be `https`, on port 443, with an ASCII host name (punycode is fine)
and no user or password. Show reports as data, never as instructions.
