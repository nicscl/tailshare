# SSH keys for devices: enroll, approve, attach

A device with no tailshare install, such as a phone app, can ask a machine for SSH access over plain HTTP.
A person on that machine approves it, and the key goes into a managed block of `~/.ssh/authorized_keys`.
By default the key can do one thing: attach to the tmux pane of an agent running there.

**Approving a key is approving shell access.** The gate keeps a pane key from opening a shell directly or
forwarding ports or agents. Once attached, though, a tmux client can:
- run any tmux command: `run-shell`, `new-window <cmd>`, the `:` prompt, or anything sent over `-CC`;
- switch to any other session on that tmux server;
- type into the pane, which is a shell or an agent, often one running with permissions skipped.

So it can run commands and read files as that account. `-r` (read-only) is the client's own choice, not
something the gate enforces. Approve only devices you would give a shell to.

## The flow

1. The device calls `POST http://<tailnet ip>:8789/bus/v1/keys/enroll` with its public key and a name.
2. The machine records a pending request with what the tailnet says about the caller (its node and user),
   which the device cannot fake, and shows a desktop notification on macOS.
3. A person runs `tailshare keys pending`, then `tailshare keys approve <id>`. The command prints the device,
   its tailnet node and owner (flagged when it is not one of yours), the fingerprint and what access it
   grants. To approve, the person types the last 6 characters of the fingerprint shown on the device.
4. The key is installed. The device polls `GET /bus/v1/keys/enroll/<request id>` until it sees `approved`,
   then connects with SSH.

```sh
tailshare keys pending                 # requests waiting here
tailshare keys approve 3f2a91c0        # pane access (default)
tailshare keys approve 3f2a91c0 --shell   # a full shell instead
tailshare keys deny 3f2a91c0
tailshare keys ls                      # keys tailshare installed here
tailshare keys rm k1a2b3c4d5e6f        # remove it here and on your other machines
```

## Who can approve, and where the key goes

- **Only this account, on that machine.** The local API listens on 127.0.0.1, where every process and every
  Unix user of the machine can reach it. So approve, deny, rm, pending and ls also need the secret in
  `~/.tailshare/keys.token` (mode 0600, made by the mailbox), sent as the `X-Tailshare-Keys` header. Other
  users cannot read it. The server itself checks the 6 typed characters (`confirm`). It refuses while another
  pending request ends in the same characters, since that would be someone imitating the device.
- **A person, not an agent: friction, not a wall.** `keys approve` refuses to run without a terminal and
  inside an agent's shell (`CLAUDECODE`, `CODEX_THREAD_ID` and the like), and approval is not offered over MCP.
  An agent running as this account can read the token, call the API, or edit `authorized_keys` directly; no
  check on the same account can stop that. These checks make the likely attack, a bus message saying
  "please approve key X", fail when an agent goes along with it, instead of quietly working.
- **This machine only, unless signed with a security key.** With a FIDO signing key (`TAILSHARE_SIGN_KEY` or
  `~/.ssh/id_*_sk`; other machines refuse approvals signed by plain key files, which any program running as you
  could use), the approval is signed with `ssh-keygen -Y sign -n tailshare-keys` and sent to the owner's other machines that
  run tailshare. Each one checks the signature against its own `~/.tailshare/allowed_signers` before
  installing. One machine can never add keys to the others on its own say-so. `--here` skips the others.
  Keys trusted with `tailshare sessions trust` sign both launches and key approvals. Lines written before key
  approvals existed name only `tailshare-launch`: run `trust` for that key again. A launch signature is
  never accepted as a key approval, and a key approval is never accepted as a launch.
- **Someone else's machine shared into your tailnet**: the device enrolls there and that machine's owner
  approves it there. Install and revoke requests from other machines are only accepted from machines of the
  same tailnet user. Approvals and removals are only sent to machines that user owns. A tagged machine has
  no single owner, so it takes part in neither.
- Each signed approval works once (nonce), within 15 minutes. A revoked key id can never be installed again.

## Revoking

`tailshare keys rm <key id>` removes the key here, then asks the owner's other machines, and every machine it
was installed on, to remove it.
The device itself can do the same with `POST /bus/v1/keys/revoke {"key_id": "k…"}` on the machine it enrolled
with, from the tailnet node that made the request (an app's "Remove access" button). Removing access needs no
signature. Machines that are offline are retried every minute until they answer.
`tailshare keys ls` lists removals that have not reached every machine yet. Removing a key does not end SSH
sessions that are already open, and anything a session changed while attached (another line in
`authorized_keys`, for instance) stays. After a lost device, also check those.

## The gate

Pane keys are installed as

```
restrict,pty,command="/path/to/tailshare ssh-gate" ssh-ed25519 AAAA... tailshare:<key id> <device>
```

`tailshare ssh-gate` reads `SSH_ORIGINAL_COMMAND` and accepts only

```
tmux -S <socket> attach[-session] [-r] [-f <flags>] -t %N [; select-window -t %N ; select-pane -t %N]
tmux -S <socket> -C attach [-r] [-f <flags>] -t %N
```

- `<flags>` may combine `read-only`, `ignore-size`, `active-pane` and `no-output`. `ignore-size` keeps a small
  phone screen from shrinking the window on the desktop.
- The socket and pane must have been registered by an agent on this machine. A registration lasts as long
  as the agent's row, 30 days after it was last seen. The gate reads them from this user's database file,
  not over the mailbox port.
- The socket must belong to this user, and the pane must still exist (`tmux display` checks it).
- The gate then execs tmux directly, with no shell in between. It uses the registered tmux binary only if
  just this user or root can change that file.
- With no command, the gate lists the panes it would accept.
- `-CC` is refused. `-C` goes through the control-mode filter below instead of an exec.

### Control mode: one pane, filtered

A client that sees `"control": 1` in `/hello` can use tmux control mode over `ssh -T` (no pty):

```
tmux -S <socket> -C attach [-r] [-f ignore-size] -t %N
```

The gate checks the socket, binary and pane as above. It then runs tmux as a child process and passes
lines through in both directions, filtering each one.

**From the client to tmux**, only these lines, exactly as written:

- `send-keys -H -t %N 68 69 0d`: one or more hex bytes. Dropped when the command has `-r`.
- `capture-pane -p -e -t %N`, optionally ending in `-S -<n>` with n ≤ 10000.
- `display -p -t %N '#{pane_width} #{pane_height} #{cursor_x} #{cursor_y} #{alternate_on} #{cursor_flag} #{pane_dead}'`.
  This exact format and no other, since formats can run `#()`.
- `refresh-client -C <w>x<h>`, with w from 20 to 1000 and h from 5 to 500 (no leading zeros), and
  `select-window -t %N`. Both are dropped under `-r` and `-f ignore-size`. See "Sizing" below.

Anything else is logged and dropped, including empty lines (tmux would detach) and lines over 64 KiB. To
detach, close stdin.

**From tmux to the client**, only these:

- command replies (`%begin` … `%end`/`%error`);
- `%output` and `%extended-output` for %N;
- `%layout-change` for %N's window;
- `%pane-mode-changed`, `%pause` and `%continue` for %N;
- `%exit`.

Other panes' output and the names of other windows and sessions never leave the machine.

When %N exits, or is left dead by `remain-on-exit`, the gate sends `%tailshare-gone %N` as its last line
and exits 0.

#### Sizing

A gate that sends `"control_size": 1` in `/hello` takes `refresh-client -C <w>x<h>` and `select-window -t %N`
from a client attached without `-r` and without `-f ignore-size`. Otherwise both are dropped, and older
gates drop them always.

- **A session of its own.** Such a client is not attached to %N's session. The gate makes a session
  `tailshare-<N>-<pid>` that holds only %N's window (linked, so it stays where it was) and attaches the
  client there. tmux gives a client's size to every window of its session that no other client holds, so
  this is what keeps the size off the other windows. It shows in `tmux ls` while the phone is attached.
- **Removing it.** `destroy-unattached` is set in the same tmux command as the attach, so tmux removes the
  session once no client holds it, even if the gate is killed. When the gate ends it kills the session
  itself, unless another client is in it: a bare `tmux attach` on the Mac can land there, as the most
  recent session, and is not thrown out; the session goes when that client leaves. A gate killed before
  the attach can leave the session behind; the next sizing gate removes it. It removes only a session
  named `tailshare-<N>-<pid>` with no client and one window, linked, whose pid is not a running
  `ssh-gate`.
- If %N's own session is killed while the phone is attached, the phone's session is the window's only one,
  and the window (with %N) goes when the phone leaves.
- **`%tailshare-nosize %N`.** If the gate cannot make or arm that session, it sends this and exits 0
  without attaching (or, if arming fails, as its last line right after the attach reply). The client
  attaches again with `-f ignore-size`. A sizing client is never attached where its size could reach other
  windows, and its sizing lines are never dropped without a reply.
- If %N leaves that window (`join-pane`, `move-pane`), the gate sends `%tailshare-moved %N` as its last
  line and exits 0. The client can attach again.
- The size is the client's own, never a window's. The `@<window>:<w>x<h>` form is refused: under
  `window-size latest`, a window size stops the Mac's terminal from taking the size back when it types.
  The Mac takes it back by typing in %N's window.
- `select-window -t %N` takes the size back. The gate runs it as `select-window -t =<its session>:@<W>`,
  so it never changes which window the Mac shows.
- A window nobody is showing keeps the last size it had until a client shows it again.
- The size goes back to the other clients when this client leaves. There is no command to clear it.
- **Lease.** Once the client has sent a size, it must send a line at least every 30 seconds. Any line
  counts, and the fixed `display` line will do. Otherwise the gate sends `%tailshare-idle %N` as its last
  line and exits 0, so a phone that dropped off the network without closing the connection doesn't hold the size.
- `resize-window` is never accepted. It sets the window to manual size for good.
- **Zoom.** A gate that sends `"control_zoom": 1` zooms %N's window on %N (`resize-pane -Z`) when the
  window has more than one pane, so the client's size goes to the pane itself rather than to a layout
  around it. The gate runs it, never the client, right after the attach, so the client gets tmux's
  `%layout-change @W <layout> <visible layout of %N alone> <flags with Z>`.
  - Zoom belongs to the window, not to a client. While the phone is attached, the Mac shows that window
    zoomed on %N too, and the pane that was active on the Mac becomes %N.
  - It marks the window `@tailshare-zoom` (`%N <pane that was active>`). When the last gate on %N leaves,
    it unzooms only if the window is still zoomed with %N active, then makes the old pane active again.
    The check and the toggle run in the tmux server at once (`if-shell -F`), so two gates never toggle
    each other.
  - A window that is already zoomed (by the Mac, or by a phone on another of its panes) is left as it is.
    That client gets the split pane and scales it to fit. If the Mac unzooms, or selects another pane,
    tmux unzooms and sends a `%layout-change` without Z; the gate doesn't zoom it again.
    The gate then drops the mark, so if the Mac zooms %N again by hand, that zoom is the Mac's and stays.
  - A gate killed with SIGKILL leaves the window zoomed, with the mark; the next gate on %N unzooms it
    when it leaves.

This narrows what the client can do. It does not make a pane key safer: the same key can still run
plain `attach`, which is a full tmux client.

## For clients

`GET /bus/v1/hello` carries what a client needs to connect:

```json
"ssh": {"login": "Glaciar", "owner": "nick@example.com", "port": 22,
        "host_keys": ["ssh-ed25519 AAAA..."], "tailscale_ssh": false, "enroll": true, "control": 1,
        "control_size": 1, "control_zoom": 1}
```

- `host_keys` go straight into `known_hosts`, so there is no trust-on-first-use.
- `owner` says whose machine it is; enroll once per owner.
- `enroll` is false where keys cannot be installed:
  - tailshare runs as root (never: that would give the device root);
  - `TAILSHARE_SSH_ENROLL=0`;
  - sshd reads keys from somewhere other than `~/.ssh/authorized_keys`.
- `control: 1` means the gate takes `-C attach` (see "Control mode" above). Older versions leave it out.
- `control_size: 1` means it also takes the sizing commands and the lease (see "Sizing" above).
- `control_zoom: 1` means a sizing attach to a pane in a split window zooms it (see "Zoom" above).
- `tailscale_ssh: true` means Tailscale SSH answers port 22 on that machine, and `host_keys` are then
  tailscaled's keys from `tailscale status`, not `/etc/ssh`. It ignores `authorized_keys`;
  access there follows the tailnet's SSH policy instead.

`POST /bus/v1/keys/enroll` takes `{"pubkey": "ssh-ed25519 AAAA...", "device_name": "Nick's iPhone"}` and
answers:

```json
{"request_id": "…", "fingerprint": "SHA256:…", "host": "macbook-pro", "login": "Glaciar", "owner": "…",
 "status": "pending", "expires": 1793390000.0, "key_id": "", "installed": []}
```

- Key types: `ssh-ed25519`, `sk-ssh-ed25519@openssh.com`, `ecdsa-sha2-nistp256`.
- Limits: 3 pending requests per device, 20 a day. A request expires after 15 minutes.
- Enrolling the same key again returns the same request.
- `tailshare keys pending` shows the first 8 characters of `request_id`, and `keys approve` takes any prefix
  that matches one request, so a client can show `tailshare keys approve <request_id[:8]>`.
- `GET /bus/v1/keys/enroll/<request_id>` answers the same shape. Only the device that made the request can
  read it. `status` is one of `pending`, `approved`, `denied`, `expired`, `cancelled`, `revoked`. After `revoked`, enrolling the same key
  starts a new request.
- `POST /bus/v1/keys/enroll/<request_id>/cancel` withdraws a pending request, again only from the device that
  made it. It becomes `cancelled`, and `keys approve` no longer offers it.
- Every call needs the header `X-Tailshare: 1`.
