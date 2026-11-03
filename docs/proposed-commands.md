# Proposed commands

> Draft contract only. No endpoint, executor, key approval, host setting or grant is enabled by this document.
> HiveAppManager must review this contract before implementation. Source: Hive `squads.md` §9.14 and PM's brief.

An agent may propose a command from its own host's fixed catalogue. The owner's device reads the host's plan,
computes its hash, and signs approval with the installed start key after Face ID. The host validates that exact
plan and records its single execution durably before running anything. An agent's reason is labelled as its
words; descriptions, commands, steps, risks and lookups come from the host.

## Decisions still pending

- Nicholas must confirm directly that agents may propose SSH key approvals and that the owner's Face ID may
  authorize them. Until then, the key-approval entry is unavailable and no key-approval implementation starts.
- Nicholas must decide whether merge/deploy and launchd restart belong in v1. The Manager recommends v1 limited
  to key approval, squad apply and agent restart. This contract does not resolve that choice.
- Review of this document is not permission to merge, deploy, restart agents, alter a host or install keys.
  Existing human checks remain in force until an explicitly approved implementation replaces a particular path.

## Catalogue and planning

The catalogue is shipped host code, never supplied by a proposal or downloaded from an agent. Each entry has an
id, integer revision, parameter schema, planner, executor and fixed risk tags. The planner resolves identifiers
against authoritative host state and produces every field below. Unrecognized parameters or catalogue ids are
refused. The proposal accepts parameters, never an arbitrary argv, account, folder, description or script.

| Catalogue id | Parameters and host lookup | Status / risks |
|---|---|---|
| `agent_restart` | exact roster member id; resolve the host's local roster, launcher and isolated agent server | proposed v1; no risk tag |
| `keys_approve` | exact request id; resolve the pending owner request and its complete key/grant/install plan | blocked on Nicholas; `keys` |
| `squad_apply` | squad id and suggestion id; resolve the suggestion on this host | route to suggestions UI/API; never a command execution |
| `merge_deploy` | exact repository id and PR ids; resolve pinned commits and ordered steps | scope pending; `launchd` when restarting |
| `launchd_restart` | exact approved service id from a host-owned list | scope pending; `launchd` |

There is no generic shell entry, free-form executable, script file, `eval`, `sh -c`, or privilege escalation.
Reject `sudo`, `doas` and `su`, including behind `env` or wrappers; a fixed absolute executable and structured
arguments must match the catalogue's plan exactly. Run only as the service's non-root account. The account,
absolute working folder and executable are resolved by the host. Use a fixed environment and PATH, not the
proposer's environment. Rebuilding the plan must detect changes to roster/launcher/config/target/executable
identity; no replacement target or changed command is authorized by an old hash.

`agent_restart` targets one roster member and its exact isolated tmux server, never all agents or the default
tmux server. If a restart includes the proposing agent or the service processing proposals, the durable execution
record still precedes dispatch. The contract never authorizes a peer to restart a host outside its approved list.

Key Review must render the full hashed lookup: requesting device and request id, both public-key fingerprints
when paired (computed locally from the hashed public keys), pair fingerprint/confirmation code, purpose/mode,
squad grant and the complete target-host list. Showing only one fingerprint while signing a pair or broader grant
is insufficient. The card may summarize, but no approval is enabled until Review includes all those values.

For `agent_restart`, the only parameter is `{member: <exact roster id>}`: ASCII `[A-Za-z0-9._-]{1,64}`,
not an address, command or prefix. Resolve exactly one row of the local roster under its owner-controlled
canonical path; refuse missing, duplicate, foreign-host or unconfigured entries. The captured ordered roster
record is `[id, host, name, folder, harness, launcher_path, tmux_server]`; strings are canonical stored values.
Its digest and raw roster/config/executable file digests are dependencies. `target` is the ordered array
`[host, id, tmux_server]`. The configured launcher must be an absolute verified executable and supply a fixed
argv template equivalent to `[launcher_path, "restart", id]`, with no extra flags from the proposer. Resolve its
interpreter as an executable dependency too when it is a script. `folder` must be absolute, owned by the service
account, and the roster's own folder; `account` is the running service account, never a parameter. The isolated
server must map to that one roster row and must not be the default server, wildcard or another member's server.
A launcher must offer a reviewed one-member restart implementation: a generic launcher without that guarantee
is `not_in_catalogue`. These are proposed planning bounds, not permission to edit today's roster or launcher.

For `keys_approve`, the lookup must use the host's owner-only request lookup (`GET /bus/v1/keys/request/<prefix>`),
not another device's enrollment poll. Resolve the full request id, all public keys in a pair, complete fingerprint
and displayed confirmation code, requested/granted purpose and mode, squad grant, and every target host. These
are part of the hashed plan. Never infer a stronger grant, silently change targets, or treat `approving` as an
executable pending request. Approval/executor integration remains blocked as above; do not bypass the current
CLI human check with an agent shell or expose the keys token to the proposer.

## Routes and ownership

All paths below start at `/bus/v1`. Proposals live only on their origin host and do not replicate. The host must
have one untagged tailnet owner. Reads and device writes require that same owner; no sharees, tagged devices or
other users. Owner identity is checked from the authenticated tailnet/device key, never from body fields.

| Request | Body / response |
|---|---|
| local `POST /proposals` | `{agent, catalogue, params, why}` → `200 {proposal}` |
| `GET /proposals` | `200 {host, proposals: [...]}` |
| `GET /proposals/<id>` | `200 {proposal}` |
| device `POST /proposals/<id>/approve` | signed approval below → `200 {proposal}` (including running or finished) |
| device `POST /proposals/<id>/decline` | signed decline below → `200 {proposal}` |

Only live local agent registrations may propose; resolve agent address, host, optional squad/member and optional
member-name snapshot on the server. `why` is required, cleaned single-line text, at most 600 characters. Additional
proposers may attach their own reasons to a deduplicated proposal. They cannot replace any host-owned plan field.
For `squad_apply`, return the suggestion reference `{suggestion: {host, squad, id}}` instead of creating a command
card: use the full suggestion review and existing signed apply, without Face ID. No command approval accepts it.

No local token route approves commands. An agent cannot approve or decline through its local API. A device may
send from an owner address that also runs tailshare, using the same signed device path as other owner devices.
The Mac app is read-only. Proposals from unbound agents remain visible in the owner's top-level NEEDS YOU group.

## Proposal shape

All timestamps are integer Unix seconds; ids are `pc_` followed by 22 base62 characters. Unknown fields or enum
values in a plan are refused by the client, not ignored. All host-produced strings are UTF-8, without control,
format/bidi, private-use, surrogate or line/paragraph-separator characters. Names/descriptions/steps are plain
text. Empty optional values use the specified empty string, empty array or null, never omitted hash fields.

```text
{
  id, host, created, expires, status,
  proposers: [{agent, host, squad: id|null, member: id|null, name: text|null, why, at}],
  plan: {
    catalogue, revision, account, folder, argv: [string, ...], description,
    steps: [{description, account, folder, argv: [string, ...]}, ...],
    risks: ["keys"|"permissions"|"launchd"|"delete", ...],
    dependencies: [{kind, id, sha256}, ...],
    key: null | {
      request, device_name, fingerprint, confirm, created, expires, purpose, mode,
      pubkey, plan_pubkey, squads: boolean, targets: [host, ...]
    }
  },
  plan_sha256: <64 lowercase hex>,
  result: null | {consumed, started: seconds|null, finished: seconds|null,
                  execution_started: boolean|null, exit_status: integer|null,
                  outcome: "running"|"done"|"failed"|"refused"|"interrupted", reason: text|null,
                  steps: [{index, status, started: seconds|null, finished: seconds|null,
                           exit_status: integer|null, reason: text|null}, ...],
                  output, output_bytes, dropped_bytes, truncated: boolean}
}
```

`expires = created + 600`. For a key request use the earlier of that and the request's expiry. Risk tags and
key targets are unique and sorted by UTF-8 bytes. Steps and argv retain execution order. The top-level argv is
the catalogue entry's concrete invocation, not a shell-joined multi-command string. Multi-step entries execute
only the displayed steps in order; a changed step requires a new plan. For a simple entry `steps` is empty.
Every nested step is checked against its catalogue just like the top-level invocation.

`dependencies` is a nonempty host-generated immutable snapshot, sorted by `(kind, id)` in UTF-8 byte order,
with no duplicate pair. Each `sha256` is lowercase SHA-256 of the dependency's exact captured bytes. `kind` is
`catalogue`, `roster`, `config`, `executable` or `target`. `id` is the canonical catalogue revision identifier,
absolute resolved file path or stable target identifier, never an agent's alias. Include the catalogue definition,
applicable roster record and config, and every actual executable/launcher used by the entry or its steps.
For files hash raw file bytes, not path, mtime, inode or file size alone: replacing the contents at the same path
invalidates the plan. A catalogue/roster/structured-target record is encoded as a fixed-schema ordered array
using this document's canonical encoding, with that array schema pinned by the catalogue revision. Directory,
agent-session, PR and key targets need a catalogue-defined stable identity snapshot, not just a display name.
Changing a symlink's resolved target also changes its canonical id. Missing/unreadable/unhashable dependencies
refuse planning or execution rather than omitting a digest. The exact `agent_restart` rules are below.

These descriptors are part of the plan hash and Review's details. Rebuild them at approval and immediately
before spawn. The executor must execute the verified executable object/immutable content or refuse if its
platform cannot prevent substitution between verification and spawn; a digest checked once and an unchecked
path executed later is insufficient. This binds the approval to the dependency snapshot rather than promising
that the path alone identifies code.

A proposal is `pending`, `stale`, `running`, `done`, `failed`, `refused`, `interrupted`, `expired` or `declined`. A dependency
change makes it stale. Do not mutate its hashed fields in place: the agent proposes a new plan and the owner
reviews it again. **Review Again** on a stale card reads that original proposal and explains that the agent must
submit a replacement; it never swaps a new hash into the same id or enables approval of the stale plan. More proposers/reasons or a run result do not change the plan. The app refreshes while Review
is open, shows full instructions/argv where applicable, and disables approval whenever its displayed hash fails.

## Exact plan hash

To avoid JSON object ordering differences, hash a fixed ordered array. Let `P` be:

```text
["tailshare-proposal-plan/1", id, host, created, expires,
 plan.catalogue, plan.revision, plan.account, plan.folder, plan.argv, plan.description,
 [[step.description, step.account, step.folder, step.argv], ...], plan.risks,
 [[dependency.kind, dependency.id, dependency.sha256], ...],
 null | [key.request, key.device_name, key.fingerprint, key.confirm, key.created, key.expires,
         key.purpose, key.mode, key.pubkey, key.plan_pubkey, key.squads, key.targets]]
```

Encode `P` with compact JSON arrays, no whitespace outside strings and no trailing newline, then UTF-8. Strings
escape only `"` as `\"` and `\` as `\\`; all other permitted characters are literal, including non-ASCII and `/`.
Control characters are forbidden, so named control escapes never arise. Do not normalize Unicode or escape
non-ASCII. Integers are base 10 without leading zeros or plus signs; booleans are `true`/`false`; null is `null`.
No floats, object values, optional elements or extra elements occur in this array. `plan_sha256` is lowercase
SHA-256 of exactly those bytes. Proposer/reason/result are explicitly outside the hash and must be visually
separated as attribution or output, never substituted for descriptions or steps.

The app reconstructs `P` from the fields it actually renders and compares its hash before Face ID. It signs only
the immutable approved hash. Both implementations must ship shared fixtures, including quotes, backslashes,
non-ASCII, empty optional fields, multiple steps and a full key lookup, before enabling the protocol.

## Signed approval and decline

Use SSHSIG, sha256, namespace `tailshare-proposals`, separate from squad writes, starts and key installation
approvals. The approval is signed by the owner's installed start key (`safe` or `skip`, not its paired plan key
or an attach/machine key). In Hive that key requires `.biometryCurrentSet` for each new approval. The server can
verify the signature and installed grant; it cannot itself attest that Face ID occurred.

Approve body:

```text
{v: 1, host, proposal: id, plan_sha256, expires, key: <installed start key id>, signature: <base64 SSHSIG blob>}
```

Sign these six UTF-8 lines joined by `\n`, with no final newline:

```text
tailshare-proposal-approve/1
<host>
<proposal id>
<plan_sha256>
<expires as decimal seconds>
<key id>
```

The path id, body proposal, body host and the server's own host must agree. The request accepts no query, fragment,
path parameters or alternative decoded path. Signature length/input limits and per-address verification limits
are as for squad writes. Verify the installed owner key on every request, including retries; revoked keys fail.
The host checks its own clock against the signed expiry, not the device's clock. The approval contains the target
host directly; it cannot be replayed on another host even if that host has the same key and proposal id.

Decline has the same body and signed lines, with first line `tailshare-proposal-decline/1`. It uses the paired
plan key of an installed owner's start key, so it does not require Face ID; it grants no execution. It is never
accepted as approval. The existing squads grant is unrelated to command execution. The device delays a decline
for 5 seconds to allow Undo before sending. Repeated identical declines return the declined proposal.

There is no short transport timestamp/nonce that would require signing again for a retry. The proposal id and
hash identify one immutable action, the expiry bounds first execution, and the persistent record prevents reuse.
A retry sends the exact same signed body and never prompts Face ID again. A new id or hash needs a new review
and signature. Never refresh the expiry of an already signed plan.

## Validate, consume, execute, recover

Under a per-proposal lock, verify owner/key/signature/host/id/hash and read the durable execution record first.
If that exact proposal/hash has already been consumed, return its stored running/final result without executing,
even after expiry. A different hash or conflicting action cannot replace that record. An unconsumed approval is
refused if the host time is at or after expiry, if the current catalogue/lookup rebuilt plan differs, or if an
entry is disabled or escalation appears. Recheck every host-owned dependency immediately before consumption.

Atomically change `pending` to `running` and commit the approval hash, key id, signed body, consumed time and
execution id to durable storage **before** creating a subprocess or dispatching a restart. Use a uniqueness
constraint/CAS and a durable SQLite transaction (`synchronous=FULL` or equivalent fsync guarantee). Concurrent
approvals, retries, restarts and other devices cannot win a second claim. No executor accepts an argv from the
request body; it loads the already committed plan by execution id.

The result records `consumed` as the committed claim time. `started` remains null until dispatch is known to
have begun. Set `execution_started` false for a confirmed refusal before any spawn, true after confirmed spawn,
and null when recovery cannot establish whether execution began. Persist one step record per displayed step
(or one synthetic step at index 0 for a simple command). Step `status` is `not_started`, `running`, `done`,
`failed`, `refused` or `interrupted`; indexes are zero-based and refer to the displayed execution order. Persist
the running claim before each step's dispatch and the exit status before advancing to the next step. Unstarted
steps remain `not_started`; no step is automatically resumed after restart.

The worker runs only the committed catalogue plan. Recheck authorization/plan dependencies after claiming and
before spawn; if they changed, finish the consumed record as `refused` with a reason and do not retry it
automatically. When no step spawned, `execution_started` is false and the app may say "Refused before starting."
If an earlier step ran, preserve its result, mark the refused step and never claim that nothing ran.
A multi-step run stops on the first failing step, records which steps completed, and is never silently resumed.

On restart, a consumed record with no known completed result becomes `interrupted` with null exit status and the
output retained so far. Do not spawn it again, reattach by a reused PID or infer success. A crash between commit
and spawn can therefore mean nothing ran; a crash after spawn can leave an unknown result. This is **at most
once**, not guaranteed execution. The card must say "Execution outcome unknown on <host>. Check the host before proposing again.", not that
nothing ran. Review/RECENT show completed, failed, refused and interrupted step records without offering Resume. A new proposal
requires fresh host lookups and a new owner decision. Service restarts need this same recovery treatment.

Results are persisted on the origin host and survive app/service restart. A timeout or lost response never
clears consumption. The app GETs by id before resending the same signed approval. Retain proposal details/results
for 30 days; retain compact consumed-id/hash tombstones indefinitely (or until an explicit protocol epoch change
invalidates every old signature). A pruned used id returns `410 used`, never creates or executes a new proposal.

## Output and errors

Capture combined stdout/stderr in arrival order into a bounded ring, retaining the **last 65,536 bytes** on the
host. Drain pipes continuously so output cannot block execution or grow memory without bound. Persist the tail
and total byte count as it arrives, not just at process exit. Decode UTF-8 with replacement, sanitize ANSI,
control and bidi characters for display, and keep the final encoded displayed output within 65,536 bytes.
`output_bytes` counts all captured raw bytes; `dropped_bytes` includes bytes lost to truncation. No unbounded
log or temporary output file is allowed. The app additionally caps display at 2,000 lines and shows the omitted
count, with four preview lines on a result card. Output is untrusted data, never terminal instructions.

HTTP failures use `{error, reason?, proposal?}`; `error` is the discriminator, `reason` is display detail.

| HTTP | error | Meaning |
|---|---|---|
| 400 | `bad_request` | malformed schema/path/hash or prohibited parameters |
| 403 | `not_owner` / `signature` | wrong owner or missing/invalid/revoked/wrong-purpose key |
| 403 | `sudo` | privilege escalation / non-catalogue wrapper |
| 404 | `not_found` | unknown proposal or inaccessible local target |
| 409 | `stale` | rebuilt plan differs; no new execution consumed |
| 409 | `expired` | first approval arrived after host expiry |
| 409 | `used` | conflicting approval/decline after consumption; include current proposal |
| 409 | `not_in_catalogue` | unavailable entry or revision, including pending owner decisions |
| 429 | `too_many` | proposal/dedupe rate limits |
| 410 | `used` | detail was pruned but id is permanently consumed |

An exact approval retry returns `200 {proposal}` with its stored state rather than `409 used`. After dispatch,
errors are recorded as a failed/interrupted result; a network failure must never be reported as proof that
nothing ran. Expiry/stale/decline may transition only an unconsumed proposal, never a running/finished record.

## Dedupe, limits and reports

- Merge identical pending catalogue plan intent (catalogue/revision, command/steps, account, folder, risks and
  complete lookup) from any agent into one id. Exclude generated id/timestamps/reasons from the dedupe key, but
  do not ignore a changed lookup, expiry bound or target. Preserve the first proposal's id/created/expiry/hash.
- After decline, suppress the same intent for one hour; return `409 {error: "declined", retry_after: seconds}`
  to the agent without a new card. This rule is independent of the short Undo delay in the app.
- At most 3 pending per agent, 10 newly created proposals per agent per rolling 24 hours, and 20 pending per host.
  Attaching a distinct agent to a pending proposal counts against that agent's pending/new-intent quotas, so
  dedupe cannot bypass limits. A repeat by the same agent is idempotent and does not refresh the TTL.
- Expire unconsumed pending proposals at their signed expiry. Stale proposals still count until expired/declined,
  preventing an agent from bypassing quotas by changing dependencies.
- List pending/stale/running and those resolved in the last 24 hours; GET by id retains historical results as above.
- A `key_enroll` blocker may add optional `proposal: <pc_id>` alongside its existing request/host reference. The
  app fetches that proposal only from that owner host and checks its key lookup matches the blocker. Legacy key
  blockers and hosts without this protocol keep their current behavior. A missing proposal never approves a key.

Before implementation, Manager review must approve each entry's concrete configured paths and planner/executor,
restart recovery/UI copy, and shared cross-language hash/signature fixtures. Fixtures are still a required
implementation gate, not supplied by this prose draft. Nicholas's two owner decisions above stay
explicitly pending regardless of that review.
