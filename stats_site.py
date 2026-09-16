#!/usr/bin/env python3
"""
Render the public Valheim stats page (a single self-contained index.html) from the
SQLite database that the monitor fills.

    python stats_site.py --db valheim_stats.db --out /var/www/valheimstats/index.html

The monitor also calls render() directly on an interval. The file is written
atomically (temp + os.replace) so a web server never serves a half-written page.
Player names are user-controlled and this page is public, so every dynamic value
is HTML-escaped.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import time
from typing import Optional

import stats_db


# ---------------------------------------------------------------------------
# Formatting helpers.  Log timestamps were parsed as the server's wall clock and
# stored via timegm(), so time.gmtime(epoch) recovers that same wall clock.
# ---------------------------------------------------------------------------
def fmt_duration(seconds: Optional[float]) -> str:
    if not seconds or seconds < 0:
        return "0m"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h" if h else f"{d}d"
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m}m"


def fmt_hours(seconds: Optional[float]) -> str:
    return f"{(seconds or 0) / 3600:.1f}"


def fmt_date(epoch: Optional[int]) -> str:
    if not epoch:
        return "—"
    return time.strftime("%b %-d, %Y", time.gmtime(epoch))


def fmt_datetime(epoch: Optional[int]) -> str:
    if not epoch:
        return "—"
    return time.strftime("%b %-d, %-I:%M %p", time.gmtime(epoch))


def fmt_ago(epoch: Optional[int], now: Optional[int] = None) -> str:
    if not epoch:
        return "—"
    now = now or int(time.time())
    s = max(0, now - epoch)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def esc(v) -> str:
    return html.escape(str(v), quote=True)


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------
VALKNUT = (
    '<svg class="valknut" viewBox="0 0 100 92" aria-hidden="true">'
    '<g fill="none" stroke="currentColor" stroke-width="4" stroke-linejoin="round">'
    '<path d="M50 6 L15 66 L85 66 Z"/>'
    '<path d="M32 18 L-3 78 L67 78 Z" transform="translate(21 -6)"/>'
    '<path d="M50 20 L26 62 L74 62 Z"/>'
    '</g></svg>'
)


def rank_rows(rows, cols, empty="No sagas recorded yet."):
    """cols: list of (header, fn(row)->str, css_class)."""
    if not rows:
        return f'<tr><td class="empty" colspan="{len(cols) + 1}">{esc(empty)}</td></tr>'
    out = []
    for i, r in enumerate(rows, 1):
        medal = {1: "gold", 2: "silver", 3: "bronze"}.get(i, "")
        cells = [f'<td class="rank {medal}">{i}</td>']
        for _, fn, cls in cols:
            cells.append(f'<td class="{cls}">{fn(r)}</td>')
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "\n".join(out)


def leaderboard(title, subtitle, rows, cols, empty):
    head = "".join(f'<th class="{cls}">{esc(h)}</th>' for h, _, cls in cols)
    return f"""
    <section class="board">
      <div class="board-head">
        <h2>{esc(title)}</h2>
        <p>{esc(subtitle)}</p>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th class="rank">#</th>{head}</tr></thead>
          <tbody>
            {rank_rows(rows, cols, empty)}
          </tbody>
        </table>
      </div>
    </section>"""


def render_html(db_path: str, cfg: dict) -> str:
    server_name = cfg.get("server_name", "Valheim Server")
    tz_label = (cfg.get("stats_site") or {}).get("timezone_label", "server time")
    top_n = int((cfg.get("stats_site") or {}).get("top_n", 10))
    now = int(time.time())

    conn = stats_db.connect(db_path)
    try:
        lb = stats_db.player_leaderboard(conn, top_n)
        deaths = stats_db.top_deaths(conn, top_n)
        visits = stats_db.top_sessions(conn, top_n)
        longest = stats_db.longest_single_sessions(conn, top_n)
        srv = stats_db.server_stats(conn)
        online = stats_db.currently_online(conn)
        recent = stats_db.recent_activity(conn, 12)
        last_event = conn.execute("SELECT value FROM meta WHERE key='last_event_at'").fetchone()
    finally:
        conn.close()
    last_event_at = int(last_event["value"]) if last_event and last_event["value"] else None

    max_play = max((r["total_seconds"] or 0 for r in lb), default=0) or 1

    # --- playtime board with inline ember bars ---
    def play_bar(r):
        pct = 100 * (r["total_seconds"] or 0) / max_play
        return (f'<div class="bar-cell"><span class="bar" style="width:{pct:.1f}%"></span>'
                f'<span class="bar-val">{esc(fmt_duration(r["total_seconds"]))}</span></div>')

    play_board = leaderboard(
        "Most Time in Midgard", "Total hours across all sessions", lb,
        [("Viking", lambda r: f'<span class="name">{esc(r["player"])}</span>', "name-col"),
         ("Playtime", play_bar, "num bar-col"),
         ("Sessions", lambda r: esc(r["sessions"]), "num hide-sm")],
        "No one has set foot in this world yet.")

    death_board = leaderboard(
        "Fallen Most Often", "Deaths — Odin keeps the tally", deaths,
        [("Viking", lambda r: f'<span class="name">{esc(r["player"])}</span>', "name-col"),
         ("Deaths", lambda r: f'<span class="death-num">{esc(r["deaths"])}</span>', "num")],
        "No deaths recorded — a cautious clan.")

    visit_board = leaderboard(
        "Most Visits", "Separate play sessions", visits,
        [("Viking", lambda r: f'<span class="name">{esc(r["player"])}</span>', "name-col"),
         ("Sessions", lambda r: esc(r["sessions"]), "num")],
        "No visits recorded yet.")

    longest_board = leaderboard(
        "Longest Single Session", "One unbroken stretch online", longest,
        [("Viking", lambda r: f'<span class="name">{esc(r["player"])}</span>', "name-col"),
         ("Length", lambda r: esc(fmt_duration(r["duration_seconds"])), "num"),
         ("When", lambda r: esc(fmt_date(r["login_at"])), "num hide-sm")],
        "No sessions recorded yet.")

    # --- server stat tiles ---
    tiles = [
        ("Total Play Hours", fmt_hours(srv.get("total_seconds")), "hours logged across everyone"),
        ("Vikings Seen", str(srv.get("unique_players", 0)), "distinct player names"),
        ("Peak Online", str(srv.get("peak_online", 0)),
         f'at once · {fmt_date(srv.get("peak_online_at"))}' if srv.get("peak_online_at") else "at once"),
        ("Total Sessions", str(srv.get("total_sessions", 0)), "logins recorded"),
        ("Total Deaths", str(srv.get("total_deaths", 0)), "and counting"),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="tile-val">{esc(v)}</div>'
        f'<div class="tile-label">{esc(label)}</div><div class="tile-sub">{esc(sub)}</div></div>'
        for label, v, sub in tiles)

    # --- currently online panel ---
    if online:
        online_items = "".join(
            f'<li><span class="pip"></span><span class="name">{esc(o["player"])}</span>'
            f'<span class="since">since {esc(fmt_datetime(o["login_at"]))}</span></li>'
            for o in online)
        online_html = (f'<div class="online-head"><span class="pip"></span>'
                       f'<strong>{len(online)}</strong> in Midgard now</div>'
                       f'<ul class="online-list">{online_items}</ul>')
    else:
        online_html = ('<div class="online-head offline"><span class="pip"></span>'
                       'The halls are quiet</div>'
                       '<p class="online-empty">No vikings are online right now.</p>')

    # --- recent activity ---
    kind_word = {"login": "arrived", "logout": "departed", "death": "fell"}
    kind_cls = {"login": "in", "logout": "out", "death": "die"}
    recent_html = "".join(
        f'<li class="ev {kind_cls.get(r["kind"], "")}">'
        f'<span class="ev-name">{esc(r["player"])}</span> '
        f'<span class="ev-kind">{esc(kind_word.get(r["kind"], r["kind"]))}</span>'
        f'<span class="ev-time">{esc(fmt_ago(r["at"], now))}</span></li>'
        for r in recent) or '<li class="ev empty">Nothing has happened yet.</li>'

    refresh = int((cfg.get("stats_site") or {}).get("refresh_seconds", 120))
    updated = fmt_datetime(now)

    return TEMPLATE.format(
        title=esc(f"{server_name} — Stats"),
        server_name=esc(server_name),
        valknut=VALKNUT,
        online_html=online_html,
        tiles=tiles_html,
        play_board=play_board,
        death_board=death_board,
        visit_board=visit_board,
        longest_board=longest_board,
        recent=recent_html,
        updated=esc(updated),
        tz_label=esc(tz_label),
        refresh=refresh,
        last_event_ago=esc(fmt_ago(last_event_at, now)),
        year=time.strftime("%Y", time.gmtime(now)),
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{refresh}">
<title>{title}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;600;700&family=Spectral:ital,wght@0,400;0,500;1,400&display=swap" rel="stylesheet">
<style>
  :root {{
    --ground:#12161a; --ground2:#0d1013;
    --stone:#1b2127; --stone2:#222a31; --edge:#333e49; --edge2:#3f4c58;
    --bone:#e8e3d6; --muted:#9aa6b0; --faint:#6d7982;
    --ember:#e0a13c; --ember-dim:#a9761f; --moss:#87bd6d; --blood:#cf5b52; --rune:#79a6cf;
    --shadow:0 2px 4px rgba(0,0,0,.4);
  }}
  * {{ box-sizing:border-box; }}
  html, body {{ margin:0; }}
  body {{
    background:
      radial-gradient(1200px 600px at 50% -10%, #1d2831 0%, transparent 60%),
      linear-gradient(180deg, var(--ground) 0%, var(--ground2) 100%);
    background-attachment:fixed;
    color:var(--bone);
    font-family:"Spectral", Georgia, "Times New Roman", serif;
    font-size:16px; line-height:1.55;
    min-height:100vh;
  }}
  .wrap {{ max-width:1080px; margin:0 auto; padding:0 20px 64px; }}
  a {{ color:var(--rune); }}

  /* Header */
  header {{ text-align:center; padding:48px 0 8px; }}
  .valknut {{ width:52px; height:48px; color:var(--ember); filter:drop-shadow(0 0 10px rgba(224,161,60,.35)); }}
  h1 {{
    font-family:"Cinzel", serif; font-weight:700;
    font-size:clamp(1.9rem, 5vw, 3rem); letter-spacing:.02em; margin:.2em 0 .1em;
    text-wrap:balance; color:var(--bone);
    text-shadow:0 1px 0 #000;
  }}
  .eyebrow {{
    font-family:"Cinzel", serif; text-transform:uppercase; letter-spacing:.32em;
    font-size:.72rem; color:var(--ember); margin:0;
  }}
  .tagline {{ color:var(--muted); margin:.2em 0 0; font-style:italic; }}

  /* Online banner */
  .online {{
    margin:28px auto 0; max-width:520px;
    background:linear-gradient(180deg, var(--stone2), var(--stone));
    border:1px solid var(--edge); border-radius:10px; padding:16px 20px; box-shadow:var(--shadow);
  }}
  .online-head {{ font-family:"Cinzel",serif; font-size:1.05rem; display:flex; align-items:center; gap:10px; justify-content:center; }}
  .online-head strong {{ color:var(--moss); font-size:1.2rem; }}
  .online-head.offline {{ color:var(--muted); }}
  .pip {{ width:9px; height:9px; border-radius:50%; background:var(--moss);
          box-shadow:0 0 8px var(--moss); flex:none; }}
  .offline .pip {{ background:var(--faint); box-shadow:none; }}
  .online-list {{ list-style:none; margin:12px 0 0; padding:0; display:flex; flex-direction:column; gap:6px; }}
  .online-list li {{ display:flex; align-items:center; gap:10px; font-size:.95rem; }}
  .online-list .name {{ color:var(--bone); }}
  .online-list .since {{ color:var(--faint); font-size:.82rem; margin-left:auto; }}
  .online-empty {{ text-align:center; color:var(--faint); margin:8px 0 0; font-size:.9rem; }}

  /* Stat tiles */
  .tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(150px,1fr)); gap:14px; margin:34px 0 10px; }}
  .tile {{
    background:var(--stone); border:1px solid var(--edge); border-top:2px solid var(--ember-dim);
    border-radius:8px; padding:18px 16px; text-align:center;
  }}
  .tile-val {{ font-family:"Cinzel",serif; font-weight:700; font-size:2rem; color:var(--ember);
               font-variant-numeric:tabular-nums; line-height:1.1; }}
  .tile-label {{ font-family:"Cinzel",serif; text-transform:uppercase; letter-spacing:.1em;
                 font-size:.72rem; color:var(--bone); margin-top:6px; }}
  .tile-sub {{ color:var(--faint); font-size:.78rem; margin-top:2px; }}

  /* Boards */
  .board-lead {{ margin-top:22px; }}
  .board-lead .bar-col {{ width:52%; }}
  .boards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px,1fr)); gap:22px; margin-top:22px; }}
  .board {{ background:var(--stone); border:1px solid var(--edge); border-radius:10px; overflow:hidden; box-shadow:var(--shadow); }}
  .board-head {{ padding:16px 20px 12px; border-bottom:1px solid var(--edge); background:linear-gradient(180deg, var(--stone2), transparent); }}
  .board-head h2 {{ font-family:"Cinzel",serif; font-weight:600; font-size:1.15rem; margin:0; color:var(--bone); }}
  .board-head p {{ margin:2px 0 0; color:var(--muted); font-size:.82rem; }}
  .table-wrap {{ overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:.95rem; }}
  th, td {{ padding:10px 20px; text-align:left; }}
  thead th {{ font-family:"Cinzel",serif; font-weight:500; text-transform:uppercase; letter-spacing:.08em;
              font-size:.68rem; color:var(--faint); border-bottom:1px solid var(--edge); }}
  tbody tr {{ border-bottom:1px solid rgba(51,62,73,.5); }}
  tbody tr:last-child {{ border-bottom:none; }}
  tbody tr:nth-child(odd) td {{ background:rgba(255,255,255,.015); }}
  .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .rank {{ width:2.4em; color:var(--faint); font-variant-numeric:tabular-nums; font-weight:600; }}
  .rank.gold {{ color:var(--ember); }}
  .rank.silver {{ color:#cdd3d9; }}
  .rank.bronze {{ color:#c08457; }}
  .name {{ color:var(--bone); }}
  .death-num {{ color:var(--blood); font-weight:600; }}
  .empty {{ color:var(--faint); text-align:center; font-style:italic; padding:22px; }}

  /* Ember bars in the playtime board */
  .bar-col {{ width:46%; }}
  .bar-cell {{ position:relative; display:flex; align-items:center; justify-content:flex-end; gap:8px; min-width:120px; }}
  .bar {{ position:absolute; left:0; top:50%; transform:translateY(-50%); height:60%;
          background:linear-gradient(90deg, var(--ember-dim), var(--ember)); border-radius:3px; opacity:.35; }}
  .bar-val {{ position:relative; z-index:1; font-variant-numeric:tabular-nums; }}

  /* Recent activity */
  .recent {{ margin-top:30px; background:var(--stone); border:1px solid var(--edge); border-radius:10px; padding:18px 22px; }}
  .recent h2 {{ font-family:"Cinzel",serif; font-weight:600; font-size:1.05rem; margin:0 0 10px; }}
  .feed {{ list-style:none; margin:0; padding:0; display:flex; flex-direction:column; }}
  .ev {{ display:flex; align-items:baseline; gap:8px; padding:7px 0; border-bottom:1px solid rgba(51,62,73,.4); font-size:.92rem; }}
  .ev:last-child {{ border-bottom:none; }}
  .ev-name {{ color:var(--bone); }}
  .ev-kind {{ color:var(--muted); }}
  .ev.in .ev-kind {{ color:var(--moss); }}
  .ev.out .ev-kind {{ color:var(--faint); }}
  .ev.die .ev-kind {{ color:var(--blood); }}
  .ev-time {{ margin-left:auto; color:var(--faint); font-size:.8rem; }}

  footer {{ text-align:center; color:var(--faint); font-size:.8rem; margin-top:40px; line-height:1.7; }}
  footer .dot {{ opacity:.5; }}

  @media (max-width:520px) {{
    .hide-sm {{ display:none; }}
    th, td {{ padding:9px 14px; }}
    .wrap {{ padding:0 14px 48px; }}
  }}
  @media (prefers-reduced-motion:reduce) {{ * {{ scroll-behavior:auto; }} }}
</style>
</head>
<body>
  <div class="wrap">
    <header>
      {valknut}
      <p class="eyebrow">Chronicle of</p>
      <h1>{server_name}</h1>
      <p class="tagline">Sagas of the bold, tallies of the fallen &middot; updated live</p>
      <div class="online">{online_html}</div>
    </header>

    <div class="tiles">{tiles}</div>

    <div class="board-lead">{play_board}</div>

    <div class="boards">
      {death_board}
      {longest_board}
      {visit_board}
    </div>

    <div class="recent">
      <h2>Recent Deeds</h2>
      <ul class="feed">{recent}</ul>
    </div>

    <footer>
      Updated {updated} ({tz_label}) &middot; last activity {last_event_ago}<br>
      <span class="dot">Refreshes automatically &middot; {year}</span>
    </footer>
  </div>
</body>
</html>"""


def render(db_path: str, out_path: str, cfg: dict) -> None:
    """Render the page and write it atomically to out_path."""
    html_text = render_html(db_path, cfg)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html_text)
    os.replace(tmp, out_path)


def main():
    ap = argparse.ArgumentParser(description="Render the Valheim stats page from the database.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True, help="Path to index.html to write")
    ap.add_argument("--config", help="Optional config.json (for server_name, stats_site options)")
    ap.add_argument("--server-name", default=None)
    args = ap.parse_args()
    cfg = {}
    if args.config and os.path.exists(args.config):
        with open(args.config) as f:
            cfg = json.load(f)
    if args.server_name:
        cfg["server_name"] = args.server_name
    render(args.db, args.out, cfg)
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
