# Valheim → Discord monitor (no mods)

Posts Valheim server activity to a Discord channel without installing anything
on the game server, so Steam achievements keep working. Two modes:

| Mode | Needs | Events |
|---|---|---|
| **Count mode** (`a2s`) | Only the server's IP — polls the Steam query port (game port + 1) | player joined / left (count only), server online / offline |
| **Log mode** (`ftp`, `sftp`, `file`, `http`, `nexus`) | Read access to `valheim_console.log` | named **login**, **logout**, **death**, respawn |

Count mode works on any host, crossplay or not, and is the fallback when the
host gives you no file access (LOW.MS's Nexus panel currently offers no FTP/SFTP
and no console-read API). Log mode is richer if you can get at the log.

Python 3.9+, no third-party packages (SFTP is the one optional extra).

## Count mode (quick start)

1. Check the query port answers from wherever the monitor will run:
   ```bash
   python a2s_probe.py YOUR.SERVER.IP          # prints "name — 2/10 players (v0.220.5 …)"
   ```
   Valheim's query port is the game port + 1 (2456 → 2457). If you get no
   reply, the host may block UDP queries — ask them to open it.
2. Create a Discord webhook (channel → Edit Channel → Integrations → Webhooks).
3. `cp config.example.json config.json`, fill in `host` and `webhook_url`.
4. `python valheim_discord_monitor.py --test-webhook`, then
   `python valheim_discord_monitor.py`.

The monitor polls every `poll_interval_seconds` (15 s default), posts when the
count changes, and marks the server offline after `offline_after` consecutive
failed queries (3 default) so a single dropped packet doesn't cause a false
alarm. Nothing is posted on start-up. Messages use `{who}` ("A viking" / "2
vikings"), `{count}`, `{max}` and `{server}` placeholders.

Limitations: A2S carries no names (Valheim returns an empty player list) and no
death information. Two players swapping within one poll interval shows as no
change.

## Log mode

Vanilla Valheim already prints everything needed to `valheim_console.log`
(on LOW.MS: **Files → game → valheim_console.log**). From your server:

| Event   | Log line |
|---------|----------|
| Login   | `Got character ZDOID from Bjorn : 1234567890:67` (first non-zero ZDOID for a name) |
| Death   | `Got character ZDOID from Bjorn : 0:0` |
| Respawn | next non-zero ZDOID for that name |
| Logout  | `Destroying abandoned non persistent zdo … owner 1234567890` (owner id matches the player), or `Closing socket …` on direct-Steam servers, or `Player disconnected … now 0 player(s)` |

The monitor tails the log (by byte offset, so restarts never re-post), runs the
lines through a small state machine, and posts an embed to a Discord webhook.

### Setup

1. **Discord webhook** — in your Discord server: channel → Edit Channel →
   Integrations → Webhooks → New Webhook → Copy Webhook URL.
2. Copy `config.example.json` to `config.json`, paste the webhook URL and set
   `server_name`.
3. Pick a log source (below) and fill in the `source` block.
4. Test:
   ```bash
   python valheim_discord_monitor.py --test-webhook        # posts a hello to Discord
   python valheim_discord_monitor.py --replay sample_console.log   # parser dry-run, prints events
   ```
5. Run it:
   ```bash
   python valheim_discord_monitor.py --config config.json
   ```
   Secrets can be given as environment variables instead of in the file:
   `DISCORD_WEBHOOK_URL`, `VALHEIM_LOG_USER`, `VALHEIM_LOG_PASSWORD`, `NEXUS_TOKEN`.

By default the monitor starts at the **end** of the log (only new events post).
Use `--from-start` once if you want it to replay the existing file.

### Log sources

#### `ftp`
For hosts that offer FTP. Set `host`, and `path` to the console log (on
LOW.MS it is `game/valheim_console.log`; the layout may put it under a
`<ip>_<port>/` directory — run discovery to find out):

```bash
python valheim_discord_monitor.py --config config.json --discover
```
This walks the FTP tree and prints every `*.log` / `*console*` file with its size.
Set `"tls": true` if the host requires FTPS.

LOW.MS's Nexus panel does not currently offer FTP/SFTP (confirmed with their
support, Sept 2026) — use count mode there.

#### `nexus` (LOW.MS panel API)
The panel's Console tab reads
`GET https://api.prod.nexus.low.ms/user/servers/<server_id>/daemon/console?lines=N`
with an Auth0 bearer token. The `nexus` source polls that endpoint and
de-duplicates by line overlap. Your server id is the UUID in the panel URL.

The catch: this is the panel's *internal* endpoint and only accepts the
panel's own short-lived Auth0 session token, not the `lowms_…` API keys from
the public API (those return "Invalid token" here, and the public v1 API has no
console-read endpoint as of Sept 2026). It's fine for testing with a token
copied from your browser's dev tools, but not for running unattended.

#### `file`
For running the monitor on the same machine as the server, or on any log
file you sync locally.

#### `sftp` / `http`
SFTP needs `pip install paramiko`. `http` polls any URL that returns the raw
log text (supports `Range` requests if the server does).

## Running it permanently

**Docker**
```bash
docker run -d --name valheim-monitor --restart unless-stopped \
  -v "$PWD":/app -w /app python:3.12-alpine \
  python valheim_discord_monitor.py --config config.json
```

**systemd** (`/etc/systemd/system/valheim-monitor.service`)
```ini
[Unit]
Description=Valheim Discord monitor
After=network-online.target

[Service]
WorkingDirectory=/opt/valheim-monitor
ExecStart=/usr/bin/python3 valheim_discord_monitor.py --config config.json
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Windows** — Task Scheduler → "At startup" → `pythonw.exe valheim_discord_monitor.py --config config.json`,
or run it in a terminal.

## Options

| Config key | Default | Meaning |
|---|---|---|
| `events` | mode default | Log mode: `login`, `logout`, `death`, `respawn`, `server_up`. Count mode: `player_joined`, `player_left`, `server_online`, `server_offline`. |
| `poll_interval_seconds` | 15 | How often to poll. |
| `source.offline_after` | 3 | Count mode: failed queries in a row before "offline". |
| `discord.embeds` | true | Coloured embed vs plain text. |
| `discord.show_player_count` | true | Footer with the current online count. |
| `discord.messages` | see example | Per-event templates; `{player}`, `{server}`, `{who}`, `{count}`, `{max}` placeholders. |
| `state_file` | `monitor_state.json` | Where the read offset is remembered. |

## Notes
- Names come from the character, not the Steam account.
- On a PlayFab/crossplay server a disconnect (clean or timeout) shows up as the
  `Destroying abandoned … owner <id>` line; the monitor emits one logout per player.
- If the server restarts, the log is truncated and the monitor resets automatically.

## License

MIT — see [LICENSE](LICENSE).
