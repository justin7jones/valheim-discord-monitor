"""The newest-log-line probe, including the database fallback used after a restart."""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stats_db, valheim_discord_monitor as V

R = []
def ck(n, c, d=""):
    R.append((n, bool(c))); print(("PASS " if c else "FAIL ") + n + ("" if c else f"   <- {d}"))

class P:            # a monitor process that has parsed nothing yet
    last_ts = None
class D:
    url = None

OFF = 7 * 3600      # the game server's clock runs behind UTC, as on the live box
db = tempfile.mktemp(suffix=".db")
st = stats_db.Store(db, source="t")
real = int(time.time()); logclk = real - OFF
st.login("Jorsten", logclk - 11000)
st.logout("Jorsten", logclk - 10800)          # newest event: 3h ago
st.record_clock_offset(logclk, real)
cfg = {"health": {"enabled": True}, "source": {}, "maintenance": {}}

h = V.build_health(cfg, None, D(), "T", P(), st, feed_status=lambda: (None, "502 daemon_unavailable"))
age = h.log_age()
ck("after a restart, silence comes from the database, not process start-up",
   age is not None and 2.9 < age / 3600 < 3.1, age)
ck("  ...and the clock offset is applied (not 7h out)", age is None or age / 3600 < 4, age)
down, why = h.assess()
ck("  ...so a long-silent server reads as DOWN", down, why)

# a live process that HAS parsed a line prefers it over the database
class P2:
    last_ts = logclk - 30
h2 = V.build_health(cfg, None, D(), "T", P2(), st)
ck("a parsed line wins over the database", 0 <= h2.log_age() < 120, h2.log_age())
ck("  ...and reads as UP", not h2.assess()[0], h2.assess()[1])

# no database at all: falls back to process start-up rather than crashing
h3 = V.build_health(cfg, None, D(), "T", P(), None)
ck("no database -> no timestamp, no crash", h3.log_age() is None)

st.close(); os.unlink(db)
print(f"\n{sum(o for _, o in R)}/{len(R)} passed")
sys.exit(0 if all(o for _, o in R) else 1)
