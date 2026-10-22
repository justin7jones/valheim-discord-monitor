"""Branch tests for health.py — outside-in up/down detection."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import health as H

R = []
def ck(name, cond, detail=""):
    R.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"   <- {detail}"))

class Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t
    def adv(self, s): self.t += s

class Api:
    def __init__(self, status="running", raises=None): self.status, self.raises = status, raises
    def server(self):
        if self.raises: raise RuntimeError(self.raises)
        return {"status": self.status}

class Ev:
    def __init__(self, kind): self.kind = kind

def make(status="running", age=60.0, raises=None, cfg=None, suppress=False,
         feed=(None, None)):
    fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd); os.unlink(path)
    clk = Clock(); notes = []
    state = {"age": age, "feed": feed}
    h = H.ServerHealth({"check_interval_seconds": 300, "confirm_checks": 2,
                        "log_stale_seconds": 1500, **(cfg or {})},
                       Api(status, raises), lambda: state["age"],
                       lambda k, d: notes.append((k, d)),
                       (lambda: suppress), clock=clk, state_path=path,
                       feed_status=lambda: state["feed"])
    return h, clk, notes, state, path

def tick(h, clk, n=1, step=300):
    out = None
    for _ in range(n):
        clk.adv(step); out = h.tick()
    return out

# ---------------------------------------------------------------- classification
for word, want in (("running", "up"), ("online", "up"), ("offline", "down"), ("stopped", "down"),
                   ("error", "down"), ("starting", "unknown"), ("wibble", "unknown"), (None, "unknown")):
    ck(f"classify({word!r}) -> {want}", H.classify(word) == want, H.classify(word))

# ---------------------------------------------------------------- THE REAL CASE
# Node dies: API says offline, log stops advancing.
h, clk, notes, st, _ = make(status="offline", age=60)
st["age"] = 2000                       # log went stale
r = tick(h, clk)
ck("node down, 1st check -> not alerted yet (debounce)", not notes and "down (1/2)" in r, (r, notes))
r = tick(h, clk)
ck("node down, 2nd check -> ALERTS", [k for k, _ in notes] == ["server_down"], notes)
ck("  ...alert names the API status", "offline" in notes[0][1], notes[0][1])
ck("  ...alert names the stale log", "log last wrote" in notes[0][1], notes[0][1])
r = tick(h, clk, n=3)
ck("  ...no repeat alerts while it stays down", len(notes) == 1, notes)
# recovery
h.api.status = "running"; st["age"] = 30
r = tick(h, clk)
ck("recovers -> posts once", [k for k, _ in notes] == ["server_down", "server_recovered"], notes)
ck("  ...recovery reports how long it was down", "back up after" in notes[1][1], notes[1][1])
r = tick(h, clk, n=2)
ck("  ...and nothing further", len(notes) == 2, notes)

# ---------------------------------------------------------------- no false alarms
h, clk, notes, st, _ = make(status="running", age=60)
tick(h, clk, n=5)
ck("healthy server never alerts", notes == [], notes)

# a routine restart: brief transitional status, log still fresh
h, clk, notes, st, _ = make(status="starting", age=120)
tick(h, clk, n=3)
ck("transitional status + fresh log -> no alert", notes == [], notes)

# the API lies / uses a word we don't know, but the log proves it is alive
h, clk, notes, st, _ = make(status="offline", age=60)
r = tick(h, clk, n=3)
ck("API says offline but log is fresh -> trusts the log, no alert", notes == [], (r, notes))


# ---------------------------------------------------------------- prompt detection
# The real failure mode: node dies at T. At T+5m the log is only 5 min old (still
# "fresh" by log_stale=25m) but the API says offline -> must alert at the 2nd check,
# i.e. ~10 minutes, not 25.
h, clk, notes, st, _ = make(status="offline", age=60)
st["age"] = 300                      # 5 minutes since the last log line
tick(h, clk)
st["age"] = 600
r = tick(h, clk)
ck("API-reported outage alerts in 2 checks, without waiting for log staleness",
   [k for k, _ in notes] == ["server_down"], (r, notes))

# ...but a line written seconds ago still vetoes a mislabelled status
h, clk, notes, st, _ = make(status="offline", age=20)
tick(h, clk, n=4)
ck("a log line from seconds ago still overrides a mislabelled 'offline'", notes == [], notes)

# ---------------------------------------------------------------- degraded modes
# key lacks servers:read -> 403; log staleness alone must still catch the outage
h, clk, notes, st, _ = make(raises="HTTP 403 missing_scope: needs servers:read", age=60)
st["age"] = 3000
tick(h, clk, n=2)
ck("no servers:read scope -> still detects the outage from the log", [k for k, _ in notes] == ["server_down"], notes)
ck("  ...and warns about the scope only once", h.api_warned)

# API unreachable but log fresh -> no alert
h, clk, notes, st, _ = make(raises="connection refused", age=120)
tick(h, clk, n=4)
ck("API unreachable + fresh log -> no alert", notes == [], notes)

# no API at all
h, clk, notes, st, path = make(age=120)
h.api = None
st["age"] = 4000
tick(h, clk, n=2)
ck("no API configured -> log-only detection still alerts", [k for k, _ in notes] == ["server_down"], notes)

# hung server: API still says running, log frozen
h, clk, notes, st, _ = make(status="running", age=60)
st["age"] = 2400
tick(h, clk, n=2)
ck("hung server (API up, log frozen) -> alerts", [k for k, _ in notes] == ["server_down"], notes)
ck("  ...and says it stopped writing to its log", "not writing to its log" in notes[0][1], notes[0][1])

# ---------------------------------------------------------------- maintenance
h, clk, notes, st, _ = make(status="offline", age=60, suppress=True)
st["age"] = 3000
r = tick(h, clk, n=4)
ck("silent while our own maintenance runs", notes == [] and r == "skip: maintenance", (r, notes))

# ---------------------------------------------------------------- fast recovery + absorbing
h, clk, notes, st, _ = make(status="offline", age=60)
st["age"] = 3000
tick(h, clk, n=2)
ck("down and alerted", [k for k, _ in notes] == ["server_down"], notes)
ck("  ...absorbs the log's 'back online' while down", h.absorbing_online())
h.observe(Ev("server_online"))
ck("  ...a boot line recovers immediately (no 5-min wait)",
   [k for k, _ in notes] == ["server_down", "server_recovered"], notes)
ck("  ...stops absorbing once recovered", not h.absorbing_online())

# ---------------------------------------------------------------- persistence
h, clk, notes, st, path = make(status="offline", age=60)
st["age"] = 3000
tick(h, clk, n=2)
ck("alerted before restart", len(notes) == 1, notes)
st2 = {"age": 3000}
h2 = H.ServerHealth({"check_interval_seconds": 300, "confirm_checks": 2, "log_stale_seconds": 1500},
                    Api("offline"), lambda: st2["age"], lambda k, d: notes.append((k, d)),
                    clock=clk, state_path=path)
tick(h2, clk, n=3)
ck("monitor restart does NOT re-alert for the same outage", len(notes) == 1, notes)
h2.api.status = "running"; st2["age"] = 30
tick(h2, clk)
ck("  ...and still reports recovery after the restart",
   [k for k, _ in notes] == ["server_down", "server_recovered"], notes)

# ---------------------------------------------------------------- interval respected
h, clk, notes, st, _ = make(status="running", age=60)
tick(h, clk)                      # first check runs
clk.adv(60)                       # ...then a tick inside the interval is a no-op
ck("ticks inside the interval are free", h.tick() is None)


# ============ the live 2026-10-22 case: restarted mid-outage, console 502 ============
print()
# API says 'active' (a service-level word, not runtime), log never readable at all.
ck("classify('active') -> up", H.classify("active") == "up")
h, clk, notes, st, _ = make(status="active", age=None, feed=(None, None))
st["age"] = None
r = tick(h, clk)
ck("fresh start, no log yet -> not down immediately", not notes, (r, notes))
clk.adv(1300)                                   # 20+ min with still no log line
r = tick(h, clk); r = tick(h, clk)
ck("never able to read the log -> ALERTS instead of claiming health",
   [k for k, _ in notes] == ["server_down"], (r, notes))
ck("  ...and says it can't read the log", "no log line read" in notes[0][1], notes[0][1])

# with the feed openly erroring, it doesn't wait the full 25-minute window
h, clk, notes, st, _ = make(status="active", age=None,
                            feed=(None, "LOW.MS console 502 daemon_unavailable"))
st["age"] = None
clk.adv(620)                                    # past feed_error_grace (10 min), not log_stale
tick(h, clk); r = tick(h, clk)
ck("a 502 feed error alerts after ~10 min, not 25", [k for k, _ in notes] == ["server_down"], (r, notes))
ck("  ...and quotes the feed error", "daemon_unavailable" in notes[0][1], notes[0][1])
ck("  ...wording points at the log, not a guess", "can't read the server's log" in notes[0][1], notes[0][1])

# feed recovers -> back up
st["feed"] = (0.0, None); st["age"] = 20
r = tick(h, clk)
ck("feed recovers -> posts recovery", [k for k, _ in notes] == ["server_down", "server_recovered"], notes)

# a healthy server whose feed blipped once is not an outage
h, clk, notes, st, _ = make(status="active", age=30, feed=(30.0, None))
tick(h, clk, n=4)
ck("healthy feed + fresh log -> never alerts", notes == [], notes)

# feed error but the log is still fresh (cached window) -> trust the log
h, clk, notes, st, _ = make(status="active", age=60, feed=(60.0, "502 daemon_unavailable"))
tick(h, clk, n=3)
ck("transient feed error with a fresh log -> no alert", notes == [], notes)

# ---------------------------------------------------------------- report
h, clk, notes, st, _ = make(status="offline", age=3000)
print("\n".join("   | " + x for x in h.report()))

print(f"\n{sum(o for _, o in R)}/{len(R)} passed")
sys.exit(0 if all(o for _, o in R) else 1)
