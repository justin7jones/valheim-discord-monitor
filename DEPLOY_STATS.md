# Deploying the public stats page

The monitor records every login, logout and death into a small SQLite database and
regenerates a self-contained `index.html` on an interval. You serve that one file
from any web server. Target here: `https://valheimstats.virtualtabletops.com`.

Nothing but Python's standard library is needed for the database and the page
(`sqlite3` is built in). No database server, no framework.

## 1. Configure

In `config.json`, alongside the existing `source`/`discord` blocks:

```json
"database": { "path": "valheim_stats.db", "enabled": true },
"stats_site": {
  "output": "/var/www/valheimstats/index.html",
  "render_interval_seconds": 60,
  "refresh_seconds": 120,
  "top_n": 10,
  "timezone_label": "server time"
}
```

- `database.path` — the .db file (relative to the monitor's working dir, or absolute).
- `stats_site.output` — where to write the page. Point it at the directory your web
  server serves for the subdomain.
- `render_interval_seconds` — how often the running monitor rewrites the page (only
  when something changed). `refresh_seconds` — how often browsers auto-refresh.

The monitor must be able to write `stats_site.output`. Create the dir and give it to
the user pm2 runs as:

```bash
sudo mkdir -p /var/www/valheimstats
sudo chown vttadmin:vttadmin /var/www/valheimstats
```

## 2. (Optional) Backfill history

Seed the database from the log you already have (from the panel's Console tab →
Download, or the Files tab). No Discord posts are sent:

```bash
python3 valheim_discord_monitor.py --config config.json --backfill /path/to/valheim_console.log
```

Run it again with a longer log any time; sessions already closed are just re-inserted,
so prefer one clean backfill. Then restart the monitor.

## 3. Restart the monitor

```bash
pm2 restart valheim-monitor && pm2 logs valheim-monitor
```

You should see `... recording stats` in the startup line and `Rendered stats page ->
/var/www/valheimstats/index.html` shortly after. Confirm the file exists:

```bash
ls -l /var/www/valheimstats/index.html
```

## 4. DNS

Add an A record for the subdomain pointing at this server's public IP (the same box
that serves your other sites):

```
valheimstats.virtualtabletops.com.   A   <your server IP>
```

## 5. Web server

### nginx
```nginx
server {
    listen 80;
    server_name valheimstats.virtualtabletops.com;
    root /var/www/valheimstats;
    index index.html;
    location / { try_files $uri $uri/ =404; }
}
```
Then TLS with certbot:
```bash
sudo certbot --nginx -d valheimstats.virtualtabletops.com
```

### Caddy (automatic HTTPS)
```
valheimstats.virtualtabletops.com {
    root * /var/www/valheimstats
    file_server
}
```

## 6. Belt-and-suspenders regeneration (optional)

The monitor already regenerates the page. If you want the page to refresh even while
the monitor is stopped, add a cron entry that renders from the DB:

```cron
*/2 * * * * cd /home/vttadmin/valheim-discord-monitor && /usr/bin/python3 stats_site.py --db valheim_stats.db --out /var/www/valheimstats/index.html --config config.json
```

## Notes

- The page is static HTML with no scripts and no inputs — safe to expose publicly with
  no authentication. Player names are HTML-escaped.
- Times shown are the server's own clock (the log's timestamps). Set
  `stats_site.timezone_label` to whatever you want printed in the footer.
- The DB is a single file — copy it to back up all history. WAL mode is on, so also copy
  `*.db-wal`/`*.db-shm` if the monitor is running, or stop it first.
- A player who was already online when the monitor (re)starts won't have a login row, so
  that in-progress session isn't counted until their next visit. Everything after start
  is exact.
