"""The player count in message footers, across both server flavours."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import valheim_discord_monitor as V

R = []
def ck(n, c, d=""):
    R.append((n, bool(c))); print(("PASS " if c else "FAIL ") + n + ("" if c else f"   <- {d}"))

def counts(parser, lines):
    """Feed lines; return [(kind, player, count)] for player events."""
    out = []
    for l in lines:
        for ev in parser.feed(l):
            if ev.kind in ("login", "logout"):
                out.append((ev.kind, ev.player, ev.extra.get("count")))
    return out

CONN = lambda t, n: f"10/25/2026 {t}:  Connections {n} ZDOS:81  sent:0 recv:0"
ZDO  = lambda t, who, o=1234: f"10/25/2026 {t}: Got character ZDOID from {who} : {o}:7"
GONE = lambda t, o=1234: f"10/25/2026 {t}: Destroying abandoned non persistent zdo 1234:1 owner {o}"

# ---- crossplay: the live case. No "Player joined ... now N" line exists at all,
# so the only count line is Connections, written every 10 minutes.
p = V.ValheimLogParser()
got = counts(p, [CONN("12:00:00", 0), ZDO("12:06:16", "Jorsten", 111)])
ck("lone player joining an empty crossplay server -> 1 (was 0)", got == [("login", "Jorsten", 1)], got)

got = counts(p, [ZDO("12:40:23", "Dame Helcrys", 222)])
ck("second player -> 2 (was 1)", got == [("login", "Dame Helcrys", 2)], got)

got = counts(p, [GONE("13:44:27", 222)])
ck("one leaves -> 1", got == [("logout", "Dame Helcrys", 1)], got)
got = counts(p, [GONE("13:44:39", 111)])
ck("last leaves -> 0", got == [("logout", "Jorsten", 0)], got)

# a stale Connections line must not drag the count back down
p = V.ValheimLogParser()
counts(p, [CONN("14:00:00", 0), ZDO("14:01:00", "A", 1), ZDO("14:02:00", "B", 2)])
got = counts(p, [ZDO("14:03:00", "C", 3)])
ck("stale 'Connections 0' doesn't undercount later joins", got == [("login", "C", 3)], got)

# ---- direct Steam: the join line DOES arrive first; must not double-count
p = V.ValheimLogParser()
got = counts(p, [CONN("15:00:00", 0),
                 '10/25/2026 15:10:00: Player joined server "x" that has join code 1, now 1 player(s)',
                 ZDO("15:10:20", "Grindel", 9)])
ck("direct-Steam 'now 1' then ZDOID -> 1, not 2", got == [("login", "Grindel", 1)], got)

# ---- straight after a restart there is no authoritative count yet
p = V.ValheimLogParser()
got = counts(p, ["10/25/2026 13:45:48: Game server connected",
                 ZDO("13:46:22", "Jorsten", 111), ZDO("13:47:19", "Dame Helcrys", 222)])
ck("after a restart, logins still report a count (was blank)",
   got == [("login", "Jorsten", 1), ("login", "Dame Helcrys", 2)], got)

# ---- players already online before the monitor started: keep the server's figure
p = V.ValheimLogParser()
got = counts(p, [CONN("16:00:00", 3), ZDO("16:01:00", "Newcomer", 77)])
ck("untracked players already on -> keeps the server's higher count", got == [("login", "Newcomer", 3)], got)

print(f"\n{sum(o for _, o in R)}/{len(R)} passed")
sys.exit(0 if all(o for _, o in R) else 1)
