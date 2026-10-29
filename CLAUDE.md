# CLAUDE.md — working context for this repo

Read this before changing anything. It records what cost real debugging to learn, so
you don't rediscover it. Written 2026-10-29, after ~7 weeks of building this against a
live server.

## What this is

A no-mods Valheim → Discord monitor for one server, plus a SQLite stats database and a
public web page. It reads the game's console log, posts named events to Discord, keeps
play statistics, pulls Steam achievements, runs unattended backups/updates, and alerts
when the server goes down.

**It runs in production on this machine.** Changes here affect a live game server that
people are playing on.

## Deployment (this machine)

| | |
|---|---|
| Host | `vtt` (Ubuntu), user `vttadmin` |
| Checkout | `~/valheim-discord-monitor` |
| Process | pm2, **`valheim-monitor`** (the stats page is a separate pm2 app, `valheim-stats-web`) |
| Deploy | `git pull && pm2 restart valheim-monitor` |
| Page | https://valheimstats.virtualtabletops.com → `/var/www/valheimstats/index.html` |
| Game server | LOW.MS, id `4172733f-5e43-4663-b627-fd7405f0feb1`, `51.81.167.39:2456`, crossplay on |

`config.json` is **gitignored and holds live secrets** (Discord webhook, LOW.MS API key,
Steam API key, panel password). A `git pull` never updates it, so every new config block
is a manual edit. Always `python3 -c "import json;json.load(open('config.json'))"` before
restarting — a syntax error takes the monitor down. Never paste its contents into a chat
or commit it.

`pm2 restart` does **not** pick up new environment variables without `--update-env`.
Prefer keys in `config.json` to dodge that trap entirely.

## Safety rails

- The live server has players on it. **Never** restart, stop, update or restore the game
  server to "test" something. `maintenance.py` only acts when the server is empty, and
  that gate exists for a reason — don't bypass it.
- `--health-check`, `--maintenance-check`, `--render-site`, `--refresh-steam` and
  `--replay` are read-only/safe. Prefer them.
- Deleting a backup is irreversible. The code deletes only the oldest *unpinned* one, and
  only when the quota is full.
- Don't commit `config.json`, `*.db`, `monitor_state.json`, `maintenance_state.json`,
  `health_state.json`.

## Architecture

| File | Role |
|---|---|
| `valheim_discord_monitor.py` | log sources, parser state machine, Discord, main loop, CLI |
| `stats_db.py` | SQLite schema + queries (all `CREATE TABLE IF NOT EXISTS`) |
| `stats_site.py` | renders the static page (3 client-side tabs) |
| `steam.py` | Steam Web API (appid 892970) |
| `maintenance.py` | LOW.MS API clients, nightly backup, game updates |
| `health.py` | outside-in up/down detection and outage alerts |
| `nexus_login.py` | headless panel sign-in (only still needed for game updates) |

Log source is **`lowms`**: `GET /v1/servers/{id}/console?lines=N` on the documented public
API (scope `console:read`). The older `nexus` source drove a headless browser through
Auth0 and is legacy — don't go back to it for log reading.

## Hard-won gotchas — do not relearn these

**The log clock is not the system clock.** Log timestamps are the *game server's* wall
clock (America/Los_Angeles) stored as if they were UTC — about 7h behind this box. The
monitor records `meta.log_clock_offset`; the page subtracts it. Comparing a log timestamp
to `time.time()` without that offset overstates every "x ago" by 7 hours and makes a live
feed look dead. Steam unlock times are *true* epochs and must keep the real clock.

**A crossplay server writes no `Player joined … now N player(s)` line.** The only count
line is `Connections N ZDOS`, every 10 minutes. So the server's own count is up to 10
minutes stale at login — which is why the footer reports
`max(server count, players tracked)`. Historical logs in this project are from the
direct-Steam era and *do* have the join line, so they will not reproduce count bugs.

**`Game server connected` repeats** up to 7 times over ~2 minutes on a single boot. Only
the first is a new session; repeats within `BOOT_REANNOUNCE_WINDOW` (300s) are ignored.
Treating each as a boot spammed Discord *and* reset parser state, losing tracked players
and their SteamID links.

**A dead node writes nothing.** There is no shutdown line when the host fails, so the log
simply stops. That is why `health.py` watches from outside, and why it measures *silence*
(newest line, else last successful read, else start-up) rather than line age — an
unreadable log must never read as healthy.

**LOW.MS `status` lags.** Observed `active` when healthy, `failed` during an outage. It
is runtime-meaningful but was still `active` while the console already answered
`502 daemon_unavailable`. Never make it the only signal.

**The panel's update route moved.** The public API has **no** update endpoint. Updates run
the panel's own task: `POST /user/servers/{id}/tasks/update/run`. The older
`/servers/{id}/actions/update_server` now 404s but is *still shipped as dead code in the
panel's JS bundle*, so reading that bundle will mislead you. `update_server()` tries known
routes, remembers the winner, and treats "none accepted" as permanent.

**A rejected Steam API key 403s everything**, which once marked every player "private".
`check_key()` now aborts the refresh before touching any row.

**Steam privacy is settled — don't relitigate.** `communityvisibilitystate` describes the
*profile*; achievements are gated by the separate **Game details** setting. A Web API key
is not an authenticated session, so friending does not help. Scraping the public
achievements page does **not** work: a private player's page renders the full catalogue as
`0 of 53` with no dates, so scraping would record confident zeros. The only fix is the
player setting Game details → Public.

**Steam achievement icons** come back on the retired `steamcdn-a.akamaihd.net`; the working
form is `https://shared.fastly.steamstatic.com/community_assets/images/apps/<appid>/<hash>.jpg`.

## Tests

```bash
python3 tests/test_player_count.py   # footer counts, both server flavours
python3 tests/test_health.py         # outage detection branches
python3 tests/test_log_age.py        # silence measurement incl. DB fallback
python3 tests/e2e_health.py          # drives the real monitor through an outage
python3 valheim_discord_monitor.py --config config.example.json --replay sample_console.log
```

Run them before and after any change to the parser, health or stats code. They are fast
and need no network or live server.

**Gap:** the `maintenance.py` suites (85 branch + 12 end-to-end) were written in a scratch
directory and lost. If you touch `maintenance.py`, rebuild that coverage in `tests/` first.

**Test against the real logs, not just synthetic ones.** Several bugs here survived green
mock tests and only appeared against live data — the count bug hid because the archived
logs are the wrong server flavour, and the health blind spot hid because the tests always
had a log to read.

## Useful commands

```bash
# read-only diagnostics
python3 valheim_discord_monitor.py --config config.json --health-check
python3 valheim_discord_monitor.py --config config.json --maintenance-check
python3 valheim_discord_monitor.py --config config.json --render-site
python3 valheim_discord_monitor.py --config config.json --refresh-steam

pm2 logs valheim-monitor --lines 100 --nostream   # scrub secrets before sharing
pm2 restart valheim-monitor
```

When sharing logs, redact: `sed -E 's#https://discord\.com/api/webhooks/[^ "]*#<webhook>#g;
s/[A-Fa-f0-9]{32}/<key>/g; s/(lowms_)[A-Za-z0-9]+/\1<redacted>/g'`

## Open items

- Recovery message from `health.py` has not yet been observed in production (the down path
  has been).
- Rotate the Steam API key and Discord webhook — both were pasted into a chat transcript
  on 2026-09-20.
- LOW.MS native scheduled tasks (Backup 03:00 / Update 03:30) must stay **Paused**; they
  ignore whether anyone is playing.
- Not built, previously offered: server-native achievements derived from our own tables
  (hours, deaths, longest session, first-back-after-restart). Works for every player,
  including Xbox/GamePass players and anyone with a private Steam profile.
