# Squads

A squad is a named group of agents, each with a role: "AppDev, the iOS app, a Claude on the Air in
~/Projects/app". It says who works on what and where; the agents in it learn their role and their
squadmates' addresses when they start. A squad is stored on every machine of its owner and synced between
them, so it can be read and edited from any of them, or from a device of yours through the Hive app.

A squad does nothing by itself. Starting a member needs starting turned on on that machine
([start.md](start.md)), and an agent that is already running is not changed when its squad is.

## From the terminal

```sh
tailshare squad                  # this session's squad and role (for an agent: "am I in a squad?")
tailshare squad ls               # your squads, and how many of each are running
tailshare squad show Tailshare   # one squad by name or id: members, where they run, their addresses
tailshare squad new Tailshare    # make one
tailshare squad edit Tailshare   # edit it as JSON in $VISUAL / $EDITOR
tailshare squad join Tailshare AppDev   # make this running session that member (below)
tailshare squad leave            # undo a join
```

All of them take `--json`. `new` and `edit` refuse to run in an AI agent's shell and need a terminal: a squad
says which agents run where and with which instructions, so a person changes it. Agents read squads, through
`tailshare squad` or the `squad` MCP tool.

`edit` opens the squad as JSON (header fields and a `members` list). Reorder members by moving them, add one
by adding an object without an `id`, remove one by deleting it. Only what you changed is saved, one write per
member, each checked against the version you opened: if something changed somewhere else in between, the
edit stops with "changed somewhere else" and you run it again.

## What a squad holds

Header: `name` (required, up to 40 characters), `color` (`honey`, `teal`, `indigo`, `pink`, `green` or
`slate`), `icon` and `shares`.

Each member: `name`, `role` (up to 40 each), `icon`, `host` (the machine it runs on), `kind` (`claude` or
`codex`), `folder` (an absolute path on that host), `instructions` (up to 4 KB; a session started from the squad
gets them as Claude's appended system prompt or Codex's developer instructions), and `start`: `options` (as in start.md) and `prompt` (up to 8 KB,
the first message).

**The tree.** A member's `reports_to` is the id of another member of the same squad, or null. A member that
someone reports to is a lead. The owner writes it like any member field, and the write is refused (`400`, `field:
"reports_to"`) if it names itself or no live member, makes a loop, or puts anyone more than 3 levels down (the
member's own reports count). Edits on two machines can still merge into a knot, or a lead can be removed: a member
whose chain up loops, runs past 3 levels or ends at a removed member reads as top-level, everywhere.

Up to 32 members, 50 squads per owner and 64 KB per squad. Names and roles lose control and bidi characters.

**Icons** are a small SVG subset, the same one the Hive app draws: `svg`, `g`, `path`, `circle`, `ellipse`,
`rect`, `line`, `polyline`, `polygon`; geometry, `transform`, `fill`/`stroke` as `#rgb`, `#rrggbb` or a few
names, `stroke-width`, `opacity`, `fill-rule`, `stroke-linecap`, `stroke-linejoin`. `viewBox` is required;
`width` and `height` are accepted and dropped. No text, links, images, styles, scripts, comments, entities or
other namespaces; only characters XML 1.0 allows (no NUL), no byte-order mark, UTF-8 only, and at most a plain `<?xml version="1.0" encoding="UTF-8"?>` (1.1, `standalone`, or no encoding also do); at most 8 KB, 200 elements, 10 levels and 2000 path segments. Anything else is refused,
not cleaned up, and what is stored is re-written in one canonical form.

**From marks.** `instructions_from` and `start.prompt_from` hold a tailnet login when that text was copied
from someone else's shared squad, so the app can show "from alex". They are labels the writer sets, not a
check. Your own edit of the text clears the mark; `tailshare squad edit` does that for you.

## Sharing

A share lets another tailnet user see a squad: `{"user": "alex@example.com", "can": ["see"], "show_paths":
false}`. They get a copy without your share list and without members' start options (which hold folders, a
resume id and models): of `start` only the prompt, `skip`, whether the member runs without asking, and `auto`, whether it runs in Claude's auto mode. Unless
`show_paths` is true, each folder is cut down to its last part. They cannot change it, and their machines do not store it. Seeing is all a share allows: it
grants no messaging, attaching or starting.

## Starting a member

A start request ([start.md](start.md)) may name a member instead of spelling out the session:

```json
{"squad": {"id": "sq_…", "member": "m_…"}, "device_name": "iPhone"}
```

The machine reads the member from its own copy of the squad. The member's `host` must be this machine, and
the request may not also set `kind`, `cwd`, `name`, `instructions` or `options`: those come from the squad. A
`prompt` in the request replaces the squad's. The folder must still be inside this machine's `roots`, and
every option must still be allowed by its `start.json`: a squad never widens what a machine accepts.

The session runs in tmux session `squad-<name>`, one window per member, with `TAILSHARE_SQUAD` and
`TAILSHARE_MEMBER` set, and registers on the bus as that member. The bus keeps an agent's squad tag only if
the member runs on this machine, is of the agent's kind and has its folder, and no other live agent holds the
tag (a `claude -p` run in a member's shell inherits the variables, and is not the member). An agent counts
as live for 90 s after it was last heard from: a member that crashed and is started again within that time
runs without its tag, as the gate also says `running` until then. A member that already has a live agent is
refused (`running`). The plan says `changed` when the member's instructions differ from the last time it was
started here.

## Joining

A session that was not started from the squad (one already running, or started by ts-agents) can become a member
without a restart: `tailshare squad join <squad> <member>` (squad and member by name or id). The bus checks it
the same way as a tag from the environment: the member runs on this machine, is of the session's kind and has
its folder, and no other live agent holds it. Otherwise it says why (`409 not_that_member` with a `reason`). A
session that is already another member is refused (`409 already_a_member`); `tailshare squad leave` first. A
session started as a member stays that member.

The join is stored for the session's id, so later registrations without `TAILSHARE_SQUAD`/`TAILSHARE_MEMBER`
keep it; `check_tag` still decides each time, and a join that no longer matches is not used. Variables from the
environment are tried first, and they win: when the session started as the member registers while one that only
joined it holds it (the real one was quiet for more than 90 s), the joiner's join is dropped and the member goes
back to the started session. `leave` doesn't undo a member a session was started as. Joins of sessions the bus no longer knows are dropped when it prunes.

A join only labels a session: it changes no squad and needs no keys token. It is `POST
/bus/v1/squads/<id>/join` `{agent, member}` and `POST /bus/v1/squads/<id>/leave` `{agent}`, on the local API
only (the peer API answers 404). As with reports (reports.md), every agent on a machine runs as the same Unix
user, so the API can't tell which session asks. A process could name another session's id, but only for a member
that session matches and nobody else holds, and leave only for a session that is running. Run it for your own
session only.

## How it syncs

Each machine keeps its owner's squads in `~/.tailshare/bus.db`. The header and each member are separate
registers with a rev `[time, host]`; the higher rev wins, so edits to different members on different machines
both survive, and every machine ends up with the same squad. A rev more than two minutes ahead of the
machine's clock is refused. Deleted squads and members are kept as tombstones for 30 days so the delete
spreads.

`/hello` carries, per squad, the highest rev and a digest of all revs. During discovery a machine compares
them with its own and, when either differs, fetches the squad from that peer. It only does so from machines of
the same tailnet user, and only keeps squads owned by that user; tagged machines have no squads.

## API

On the local API (`127.0.0.1`) and the peer API, under `/bus/v1`:

| | |
|---|---|
| `GET /squads` | your squads, or for another user the ones shared with them |
| `GET /squads/<id>` | one squad; `?tombstones=1` (owner only) includes deleted members |
| `PUT /squads/<id>` | create: `{"base_rev": null, "name": …, "members": [...]}` |
| `PATCH /squads/<id>` | change header fields, or `{"deleted": true}` |
| `PATCH /squads/<id>/members/<member>` | change a member, create one (`base_rev: null`) or delete one |

Every write carries `base_rev`, the rev it was read at. A stale one gets `409 {"error": "conflict", "squad"
| "member": <current>}`; invalid input gets `400 {"error": "bad_request", "field": …, "reason": …}`. Writes are
for the owner only; any other write, a sharee's for one, gets 403. The owner writes:

- on the local API, a caller that sends `~/.tailshare/keys.token` as `X-Tailshare-Keys`, as `tailshare squad
  edit` does. Every process of that account can read the file, an agent's included: the check keeps agents
  from editing squads by accident or through the plain API, not one set on reading the token.
- on the peer API, a device of the same tailnet user (the Hive app), with each write signed by its plan key
  installed on this machine with the squads grant (below). Unsigned, a machine never writes there: neither this one, by its tailnet
  address, nor one that runs tailshare (it has answered `/hello`). Those write through their own local API, so
  the token cannot be stepped around through the peer API.

### Signed writes

A device signs each squad write with the plan key of a start key installed on the machine it writes to. The
plan key needs no Face ID, so saving does not prompt. The key must be the owner's (enrolled from the owner's
device) and not removed. It must also hold the squads grant. The person gives it when approving the start key,
with `tailshare keys approve --squads` or by answering yes when asked, and a signed approval carries it to the
other machines. A start key approved without it gets it later on each machine with `tailshare keys grant-squads <key_id>`, run by the person there: it asks for the end of the pair fingerprint the device shows, as approving does, and changes only that machine (`--off` takes it back). Once a start request is approved, its poll (`GET /keys/enroll/<id>`) carries `squads`, whether
the grant was given, so the app can say so before a save; that is a hint, and the machine's 403 decides. No
other key signs squad writes: not the start key, which asks for Face ID, and not a pane key,
which the app keeps as a file in iSH. A key enrolled from a computer gets the grant the same way, only when the
person gives it. Any of the owner's addresses may send a signed write, so the iPad, whose iSH runs tailshare, writes
the same way as the iPhone. The signed message is UTF-8, seven lines joined by `\n` with no newline at the end:

```
tailshare-squad-write/1
<host>          the machine's name, as its /hello "host"
<METHOD>        PUT or PATCH
<path>          /bus/v1/squads/<id>[/members/<member>], as sent, without the query
<time>          unix seconds
<nonce>         32 lowercase hex characters, new for each write
<body sha256>   lowercase hex, of the exact body bytes sent
```

The path is the raw one in the request line, not decoded or normalized: sign exactly the bytes you send. The
query string is not signed, so a write with one, or with `;params` or a `#fragment` (even a bare `?`, `;` or
`#`), is refused with `400 bad_request`, field `query`. It is signed as `ssh-keygen -Y sign -n tailshare-squads` signs (SSHSIG, sha256). It is sent in headers:
`X-Tailshare-Key` (the key id, `k` and 12 hex), `X-Tailshare-Time`, `X-Tailshare-Nonce` and
`X-Tailshare-Signature` (the SSHSIG blob in base64, without the armor lines). The time may be at most 60 s from
the machine's clock, and a nonce is used once. A refusal is `403 {"error": "signature", "reason": …}`, where
the reason is one of these:

- `unsigned`
- `bad_signature`, which also covers another machine, path, method or body
- `expired`, which carries the machine's `now`
- `replayed`
- `revoked`
- `unknown_key`, when no key by that id of the owner is installed here
- `no_grant`, when the key is installed but was not granted squad edits, or is not a start key's plan key

Each signature check runs `ssh-keygen`, so one address gets 30 a minute. Past that the answer is `429 {"error":
"signature", "reason": "rate_limited"}`.

Each agent in `/hello` carries `squad: {id, member}` when it is a squad member, for the owner's machines and
devices only.
