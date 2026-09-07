# tailshare

Drop a file into the terminal, get a link that works on every device on your tailnet.

```
tailshare            # interactive TUI
tailshare install    # one-time: launchd agent + tailscale serve
```

## How it works

```
iTerm2 drop / clipboard ──▶ ~/Public/ts-share ──▶ 127.0.0.1:8787 (tailshare serve)
                                                         │
                                        tailscale serve ─┴─▶ https://macbook-air.<tailnet>.ts.net/
```

* Files land in `~/Public/ts-share` (override with `TAILSHARE_DIR`).
* `tailshare serve` is a small HTTP server bound to localhost, with an index page that
  works on phones. `tailscale serve` fronts it with HTTPS, tailnet-only.
* `tailshare install` writes `~/Library/LaunchAgents/com.nicholas.tailshare.plist`
  (KeepAlive, RunAtLoad) so the server survives reboots, and points `tailscale serve /`
  at the port. Logs: `~/Library/Logs/tailshare.log`.

## TUI

| key | action |
|-----|--------|
| drag a file/folder into the window | shared the moment it lands, link copied |
| `p` or `enter` on the empty prompt | share the clipboard: screenshot, copied image, or files copied in Finder |
| `/` | type or paste a path, `enter` to share |
| `enter` / `c` | copy link of the selected file |
| `o` | open in browser |
| `k` | QR code for your phone |
| `s` | send with Taildrop to another device |
| `r` | reveal in Finder |
| `⌫` | stop sharing (deletes the copy) |
| `S` | start / restart the share server |
| `?` | help · `q` quit |

Folders are zipped before sharing. Name collisions get a `-2`, `-3` suffix.

## Non-interactive

```
tailshare add ~/Desktop/report.pdf -c   # share, print URL, copy it
tailshare paste -c                      # share the clipboard
tailshare ls                            # list with URLs
tailshare rm report.pdf
tailshare status
```

## Install / develop

```
uv tool install --editable .    # puts `tailshare` on PATH (~/.local/bin)
```

Requires macOS, Python ≥ 3.11 (uv fetches it), the Tailscale app (CLI is found inside
`Tailscale.app`), and MagicDNS + HTTPS enabled on the tailnet.
