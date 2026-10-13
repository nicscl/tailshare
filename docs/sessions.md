# Sessions: find, ask and launch agent sessions across the tailnet

tailshare doubles as a session manager for Claude Code and Codex: it finds old sessions on any of your
machines, lets one agent ask another session a question, and reopens sessions, including in the
background on another machine with permissions a person approved with a hardware key.

Status: steps 1–3 are built; step 4 is designed, not built.

| Step | What | Status |
|------|------|--------|
| 1 | Index and search sessions on every machine (CLI, MCP, local resume) | built |
| 2 | Ask a session a question: a throwaway copy with no tools answers | built |
| 3 | Launch a session in the background with real permissions, signed with `ssh-keygen -Y` | built |
| 4 | Approve launches from your phone (passkey / Face ID) | design only |

## 1. Index and search

Each mailbox indexes the transcripts **on its own machine** into `~/.tailshare/sessions.db`. There is no
central index, the same as for messages.

- **Claude Code**: `~/.claude/projects/<dir>/<session id>.jsonl` (top level only; subagent transcripts are
  skipped). Name = the `/rename` title, else the automatic title.
- **Codex**: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, names from `~/.codex/session_index.jsonl`.
- Indexed per session: id, folder, git branch, name, first and last prompt, number of prompts, start and
  last activity, and searchable text: the user's prompts and the agent's replies (capped at 256 KB per
  session). Tool calls and tool output are **not** indexed: they are large, noisy and the likeliest place for
  secrets.
- The index is refreshed every 60 s; only transcripts whose size or mtime changed are re-read (a full first
  scan of ~180 sessions / 140 MB takes about 3 s).
- Search is SQLite FTS5 (prefix matching, accents ignored, name weighted above folder above text), with a
  substring fallback for Pythons built without FTS5.

A search fans out: the local mailbox searches itself and every peer that answers, in parallel, and merges
the hits. A session is named by a **ref**, `host:id-prefix` (e.g. `prismo:3f2a91c0`). A session is
marked `● live` when it is registered on the bus right now.

```sh
tailshare sessions find stripe webhook          # every machine
tailshare sessions find --host prismo --harness codex
tailshare sessions show prismo:3f2a91c0
tailshare sessions open prismo:3f2a91c0         # resume here, or `ssh -t prismo` + resume for a remote one
```

MCP: `find_sessions(query, host, harness, limit)`.

**Who can search:** a peer's search is answered only if the asking machine belongs to the same tailnet
user as the answering one (checked from the connecting IP, like update triggers). Transcripts are private;
a machine shared into your tailnet by someone else can neither read yours nor be read by you.

## 2. Ask a session

```sh
tailshare sessions ask prismo:3f2a91c0 "why did you pick pg_dump over logical replication?"
```

MCP: `ask_session(session, question)`. The request goes to the mailbox of the session's machine, which
resumes a **throwaway fork** of the session, asks, and returns the answer:

- Claude Code: `claude -p --resume <id> --fork-session --no-session-persistence --strict-mcp-config --tools ""`
  (question on stdin). No tools at all, no MCP servers, nothing saved: the original transcript is not
  touched and no new session appears.
- Codex: `codex exec resume <id> --ephemeral` with `sandbox_mode="read-only"` and `approval_policy="never"`.
  Codex cannot turn tools off entirely; the read-only sandbox lets it read files but not write or reach
  the network.
- It runs in the session's folder, with `TAILSHARE_HOOKS=0` so the fork does not join the bus.
- At most two asks run at once per machine (each one is a model call that costs money); 5-minute timeout.
- The answer is framed for the asking agent as information from a peer, not instructions.

Any machine of the same tailnet user may ask; no approval needed. This was a deliberate choice: asking is
read-only and cheap to undo (there is nothing to undo).

## 3. Launch with a signed human approval

Launching resumes a session **in the background with real permissions** (`claude --bg --resume <id>
--permission-mode <mode> [prompt]`), so you or another agent can message it or `claude attach` to it later.
That is remote code execution, so it needs proof that a person approved this exact launch.

### The proof

A launch request is a small JSON document:

```json
{"v":1, "action":"launch", "signer":"nickqw12", "host":"prismo", "harness":"claude",
 "session":"3f2a91c0-…", "cwd":"/Users/prismo/nm-process-flow", "title":"nm-process-flow",
 "mode":"acceptEdits", "prompt":"run the migration tests and report",
 "nonce":"<128 random bits>", "issued":1791650000.0, "expires":1791650300.0}
```

It is signed with `ssh-keygen -Y sign -n tailshare-launch` (OpenSSH's `sshsig` format), ideally with a
FIDO security key (`ed25519-sk`), which only signs after a physical touch. The receiving machine checks:

1. The signature, with `ssh-keygen -Y verify` against **its own** `~/.tailshare/allowed_signers`. That file
   must belong to the user tailshare runs as and not be writable by others. No file → launching is off.
2. `host` is this machine; `expires` is in the future; the validity window is at most 15 minutes.
3. The nonce was never used before (stored until it expires): one signature, one launch.
4. The session exists here, is a Claude Code session, and its folder matches the signed `cwd`.
5. The mode is one of `manual`, `plan`, `dontAsk`, `acceptEdits`, `auto`. **`bypassPermissions` is never
   accepted**, signed or not.
6. The session is not running right now (on the bus or in `claude agents`): `--bg` would start a second copy.
   Message the running one instead.
7. tailshare is not running as root. The two servers run it as root, so they never launch.

Only then does it run `claude --bg`. Claude Code's own gate still applies on top: it refuses folders the
user never trusted.

```sh
ssh-keygen -t ed25519-sk -f ~/.ssh/id_ed25519_sk        # once, with a FIDO key plugged in
ssh prismo 'tailshare sessions trust -' < ~/.ssh/id_ed25519_sk.pub   # once per machine that may launch
tailshare sessions launch prismo:3f2a91c0 --mode acceptEdits --prompt "run the migration tests"
```

`launch` prints what you are approving (machine, folder, mode, prompt, validity) before it signs.

### What this does and does not protect

- **The key must be out of agents' reach.** Agents run as you, with a shell. A key in a file, without a
  passphrase, is a key every agent can use; the proof would then only prove that *something* on your
  machine signed. A FIDO key needs a touch for each signature; `trust` warns about keys that are not
  `sk-` keys. (Secure Enclave keys from apps like Secretive also need Touch ID per use but look like plain
  ECDSA keys; that is fine, but tailshare cannot tell.)
- **Enrollment is manual and local.** `allowed_signers` is never synced or written by tailshare over the
  network. If it were, one compromised machine, or one bad commit pulled by auto-update, could add its own
  key everywhere.
- **What you see is what you sign.** The summary is printed by tailshare from the signed document itself,
  not from text an agent wrote. With a touch-only key, though, you approve on the screen you are looking at:
  if an agent runs `tailshare sessions launch` in its own terminal, read the request in its output before you
  touch. Step 4 moves the approval to a screen agents cannot draw on.
- **It gates tailshare's launch path, not your machines.** An agent that already has a shell and your SSH
  keys can reach other machines without tailshare. The signature makes tailshare's own remote launching safe;
  it is not a sandbox.
- **It proves intent, not outcome.** The launched session can do whatever its permission mode allows.

### Not yet

- Codex sessions cannot be launched yet (only asked).
- Scoped grants ("acceptEdits in this folder for an hour, signed once") to avoid approval fatigue.

## 4. Approve from your phone (design)

The same signed document, but signed by a passkey (WebAuthn) instead of `ssh-keygen`:

1. An agent (or you) asks to launch. The local mailbox stores the request and sends your phone a
   notification (via ntfy, Pushover or Telegram, whichever is configured).
2. The notification opens an approval page served by tailshare over `https://<machine>.ts.net/` (WebAuthn
   needs HTTPS; tailshare already has it). The page is drawn by tailshare from the request: machine, folder,
   mode, prompt.
3. Face ID / Touch ID signs a WebAuthn assertion whose challenge is the hash of the canonical request. The
   assertion (authenticator data + client data + signature) replaces the `sshsig` signature.
4. The target machine verifies it against the passkey public keys enrolled in its `allowed_signers`
   equivalent, checking the user-verification flag, then runs the same checks as step 3.

Open questions: verifying WebAuthn ES256 signatures without a dependency (Python has no stdlib ECDSA;
`ssh-keygen` can verify `sk-ecdsa-sha2-nistp256@openssh.com` webauthn signatures, which may be enough), and
which notification channel to use.
