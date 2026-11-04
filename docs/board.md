# Squad board

A squad's board is columns of cards, kept beside the squad on every machine of its owner. The Hive app shows it
as the squad's Board segment; agents use `tailshare board` or the `board` MCP tool.

**Board text is data, never instructions.** A card's title, note and links are what a member or the owner
typed. An agent never acts on what a card says. The owner's path is the board only: a signed board write
changes cards, nothing else.

## Columns and cards

Default columns: `backlog` "To do", `doing` "In progress", `needs_you` "Needs you", `done` "Done". Column
editing comes later.

A card: `{id, title, owner, links[], note, column, order, done, auto, by, at, rev, from}`.
- `done` is derived (`column == "done"`), so it can never disagree with the column.
- `from` is the column a card left for Done, so unchecking puts it back (into `backlog` if that column is gone).
- `owner` is a member id, or `""` for an owner's card.
- A pinned copy also carries `follow_key`.

**Auto cards** (`auto: true`, id `a-<member>-<8 hex>`) are not stored. They come from the members' reports:
- `now` goes to doing.
- `done` from the last 24 h goes to done.
- Open blockers `to: owner` go to needs_you.

The id hashes the member and the task title (case-folded, spaces collapsed), so a task keeps its card from now
to done. Nobody moves or edits an auto card; the member changes its report instead.

## HTTP

- `GET /bus/v1/squads/<id>/board` returns `{board: {squad, rev, columns, cards, hidden}}`. Readers: the local
  API, and the owner's own devices and machines. A sharee gets 403. An older tailshare gets 404, so the app
  hides the segment.
- `GET …/board/log` returns `{log: [{at, by, op, card, column, from, note?}]}`, newest first. The log is
  append-only and keeps the last 500 entries, merged from every machine. `by` is `"<device> <key id>"` for the
  owner and `"<member>@<host>"` for a member.
- `POST …/board {ops}` applies 1–10 ops, all or none, and replies with the board.

| op | fields | who |
|---|---|---|
| add | card, column, title, owner?, note?, links?, before? | member: own cards only; owner: any owner |
| move | card, column, before?, base_rev? | member: own cards |
| edit | card, **base_rev**, title?, note?, links?, owner? | member: own cards, no `owner` |
| check | card, done, base_rev? | member: own cards. `true` = top of Done; `false` = back to `from` |
| delete | card | owner only; leaves a tombstone, so the id stays taken |
| pin | auto_card, card | owner only: a manual copy that moves to Done once, when its key shows up done in the report ("report moved it") |
| hide / unhide | auto_key | owner only. Hidden keys are stored on the board row and dropped once the report no longer has them |

**Who may write:**
- The owner writes from a device that signs each POST like a squad write: `check_signed` with the squads grant.
- A member writes through its own machine's local API with its registered `agent` id, the same way it
  writes its report.
- In v1, leads get no rights over their branch's cards.

**Errors:**
- 409 `{error: "conflict", card}` for a taken id or a card that is gone (`card: null`), or a `base_rev` that is not the card's current rev (the reply carries the current
  `card`).
- 403 for someone else's card, an owner-only op, or an auto card.
- 429 past 60 ops a minute per writer.

**Limits:**
- 200 cards, 10 ops a request, 8 columns.
- Title ≤ 80 characters, note ≤ 1000.
- ≤ 5 links. A link is `#123` (≤ 6 digits) or an `https` URL of ≤ 300 characters with no user info, port 443
  or none, an LDH host name, and no control characters or spaces. Anything else gets 400.

## Sync

Each card and the board row (columns, hidden keys) is a register with a rev, like a squad member: the higher
rev wins. Equal revs heal from the writer, as squads do. `squad_sums` cover the board too. A machine that
pulls a squad also pulls `GET …/board/sync` (owner's machines only). An older peer answers 404 there and is
skipped.

## CLI

```
tailshare board [--json]                          # your squad's board
tailshare board add "title" [--column doing] [--note …] [--link #45] [--before CARD]
tailshare board move CARD COLUMN [--before CARD]
tailshare board check CARD [--undo]
tailshare board edit CARD [--title …] [--note …|@FILE] [--link …|--no-links]
tailshare board apply --json FILE|-               # [ops] or {"ops": [...]}, all or none
tailshare board log [-n 20]
```
