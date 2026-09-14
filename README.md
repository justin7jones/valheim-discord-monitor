# Valheim → Discord monitor (no mods)

Watches the vanilla Valheim dedicated-server console log and posts **login**,
**logout** and **death** events to a Discord channel. Nothing is installed on
the game server, so Steam achievements keep working.

Single Python 3.9+ file, no third-party packages (SFTP is the one optional extra).

## How it works

Vanilla Valheim already prints everything needed to `valheim_console.log`
(on LOW.MS: **Files → game → valheim_console.log**). From your server:

| Event   | Log line |
|---------|----------|
| Login   | `Got character ZDOID from Hue Ap Sior : 3738881258:67` (first non-zero ZDOID for a name) |
| Death   | `Got character ZDOID from Hue Ap Sior : 0:0` |
| Respawn | next non-zero ZDOID for that name |
| Logout  | `Destroying abandoned non persistent zdo … owner 3738881258` (owner id matches the player), or `Closing socket …` on direct-Steam servers, or `Player disconnected … now 0 player(s)` |

The monitor tails the log (by byte offset, so restarts never re-post), runs the
lines through a small state machine, and posts an embed to a Discord webhook.

## Setup

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

## Log sources

### `ftp` (recommended for LOW.MS)
LOW.MS documents FTP access using your panel login. Set `host` to your
server IP, `path` to `/game/valheim_console.log` (the folder layout may put it
under a `<ip>_<port>/` directory — run discovery to find out):

```bash
python valheim_discord_monitor.py --config config.json --discover
```
This walks the FTP tree and prints every `*.log` / `*console*` file with its size.
Set `"tls": true` if the host requires FTPS.

If FTP turns out not to be enabled on the new Nexus panel, ask LOW.MS support
to enable FTP/SFTP for your server — it's the cleanest option — or use one of
the sources below.

### `nexus` (LOW.MS panel API)
The panel's Console tab reads
`GET https://api.prod.nexus.low.ms/user/servers/<server_id>/daemon/console?lines=N`
with an Auth0 bearer token. The `nexus` source polls that endpoint and
de-duplicates by line overlap. Your server id is the UUID in the panel URL.

The catch: the token is the panel's own short-lived access token (you can copy
it from your browser's dev tools → Network → any `api.prod.nexus.low.ms`
request → `Authorization` header). It expires, so this source is fine for
testing but needs a token refresh to run unattended. If LOW.MS adds API keys,
paste one here instead.

### `file`
For running the monitor on the same machine as the server, or on any log
file you sync locally.

### `sftp` / `http`
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
| `events` | `["login","logout","death"]` | Which events to post. Also available: `respawn`, `server_up`. |
| `poll_interval_seconds` | 10 | How often to check the log. |
| `discord.embeds` | true | Coloured embed vs plain text. |
| `discord.show_player_count` | true | Footer with the current online count. |
| `discord.messages` | see example | Per-event templates; `{player}` and `{server}` placeholders. |
| `state_file` | `monitor_state.json` | Where the read offset is remembered. |

## Notes
- Names come from the character, not the Steam account.
- On a PlayFab/crossplay server a disconnect (clean or timeout) shows up as the
  `Destroying abandoned … owner <id>` line; the monitor emits one logout per player.
- If the server restarts, the log is truncated and the monitor resets automatically.
