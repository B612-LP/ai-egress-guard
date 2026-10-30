# ai-egress-guard

A tiny bash watcher that keeps **one Clash/mihomo proxy group pointed at a working ChatGPT egress**, without fighting the person using the laptop.

When a proxy group is on auto-select, it tends to drift onto a node that is banned, rate-limited, or just dead for `chatgpt.com` — the model then sits there spinning. This script checks the egress every 30s and, only when it keeps failing, walks candidate nodes, switches the group to the first one that actually answers, and restores your original node when nothing works.

> ⚠️ It verifies **egress reachability only** (HTTP 200/401 from the probe URL). It is not a model, account, quota or long-stream health check. If the script says `healthy` and ChatGPT still spins, the problem is elsewhere.

## Why it is not just `curl` + `PUT`

- **Coarse filter never touches the selector.** It uses mihomo's per-node `/delay` probe, so in-flight requests are not cut while scanning. Only candidates that pass get a real group switch.
- **Doesn't mistake a dead network for a dead proxy.** Two independent HTTPS controls must return exact expected bodies before it is allowed to blame a node.
- **Doesn't race the user.** Every write re-checks the selector first; an external switch makes it stand down (`selection_changed`). The start node is restored on any exit path.
- **Two probes before acting**, 2s apart, so a blip is not a failover.
- **Cooldown after a total miss** (120s by default), so it doesn't thrash the selector.
- **Single instance** via `flock`, inherited across `exec`.
- **State is a JSON file**, not something a menu has to scrape out of logs.

## Requirements

- macOS or Linux, `bash`, `python3`, `curl`
- Clash Verge (mihomo) or any mihomo-compatible core with an **external controller** (Unix socket or HTTP) and a URL-test/fallback group
- The mixed proxy port reachable (default `127.0.0.1:7897`)

## Install

```sh
git clone https://github.com/<you>/ai-egress-guard.git
cd ai-egress-guard
chmod +x ai-egress-guard.sh

# read-only check first: never switches anything
./ai-egress-guard.sh --check-only
cat ~/.local/state/ai-egress-guard/state.json
```

Then wire it into a timer. macOS launchd example:

```xml
<!-- ~/Library/LaunchAgents/com.local.ai-egress-guard.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.local.ai-egress-guard</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>/Users/you/ai-egress-guard/ai-egress-guard.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>30</integer>
  <key>StandardErrorPath</key><string>/tmp/ai-egress-guard.err</string>
</dict></plist>
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.local.ai-egress-guard.plist
```

Linux: a systemd timer or `*/1 * * * * sleep 30; ...` cron works; just keep a single instance (the script's `flock` handles it).

## Configuration

Everything is an environment variable; nothing is hard-coded to one machine.

| Variable | Default | Meaning |
|---|---|---|
| `AI_EGRESS_GROUP` | `AI-AUTO` | Proxy group to guard |
| `AI_EGRESS_PROXY` | `http://127.0.0.1:7897` | Mixed proxy port used for the egress probe |
| `AI_EGRESS_PROBE_URL` | `https://chatgpt.com/backend-api/me` | Probe target; 200 **or** 401 counts as reachable |
| `AI_EGRESS_SOCK_GLOB` | `/var/folders/*/*/T/verge-mihomo.sock` | External-controller socket path glob |
| `CLASH_SECRET` | *(empty)* | Controller secret, if you set one |
| `AI_EGRESS_STATE_DIR` | `~/.local/state/ai-egress-guard` | State + lock directory |
| `AI_EGRESS_LOG` | `~/Library/Logs/ai-egress-guard.log` | Human-readable log |
| `AI_EGRESS_COOLDOWN` | `120` | Seconds to wait after a total miss |
| `AI_EGRESS_EXTRA_NODES` | *(empty)* | Extra candidates, space/newline separated (candidates otherwise come from the group's own member list) |

## Status file

`$AI_EGRESS_STATE_DIR/state.json`:

```json
{"version":1,"status":"healthy","node":"🇯🇵 Japan 02","checked_at":1793300000,"success_at":1793300000}
```

`status` is one of `healthy`, `network_unknown`, `scanning`, `unavailable`, `probe_failed`, `control_error`, `selection_changed`, `restore_failed`, `interrupted`. See [AGENTS.md](AGENTS.md) for what each one means and what an agent should do next.

## For AI agents

If you are an agent asked to fix "ChatGPT won't load", read [AGENTS.md](AGENTS.md) — it is the operation contract: check prerequisites, run `--check-only` first, read `state.json`, never edit the user's Clash config, never fight a manual node pick.

## Limitations

- Only handles **one** group and **one** probe URL.
- Coarse filter trusts mihomo's delay, which counts a 403 ban page as a latency → a banned node can pass the filter and fail the real probe (it is then skipped, just slower).
- macOS-only socket glob by default; on Linux pass an HTTP controller or your own socket glob.
- IPv6/TUN-only setups are only as good as the probe: if the system TUN captures traffic, the probe proves that path works, nothing more.
- Not a replacement for a properly configured fallback group. It is a safety net, not a load balancer.

## License

MIT
