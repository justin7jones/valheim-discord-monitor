"""End-to-end: the real monitor loop detecting a node outage that writes NO shutdown line."""
import json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SID = "4172733f-5e43-4663-b627-fd7405f0feb1"
state = {"status": "running", "posts": [], "lock": threading.Lock()}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        if self.path == "/webhook":
            with state["lock"]:
                for e in body.get("embeds", []):
                    state["posts"].append(e["description"])
            return self._send(204, {})
        self._send(404, {})

    def do_GET(self):
        if self.path == f"/v1/servers/{SID}":
            return self._send(200, {"id": SID, "name": "virtualtabletops", "status": state["status"]})
        self._send(404, {"error": {"code": "not_found", "message": "x"}})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"

d = tempfile.mkdtemp()
logf = os.path.join(d, "console.log")
cfg = {
    "server_name": "TestRealm", "poll_interval_seconds": 1, "state_file": os.path.join(d, "mon.json"),
    "offline_grace_seconds": 2,
    "events": ["login", "logout", "death", "server_restart", "server_online", "server_offline"],
    "discord": {"webhook_url": base + "/webhook", "embeds": True},
    "source": {"type": "file", "path": logf, "server_id": SID},
    "health": {"enabled": True, "check_interval_seconds": 2, "confirm_checks": 2,
               "log_stale_seconds": 60, "log_fresh_override_seconds": 3, "base_url": base,
               "state_file": os.path.join(d, "health.json")},
}
json.dump(cfg, open(os.path.join(d, "config.json"), "w"))


def append(line):
    with open(logf, "a") as f:
        f.write(line + "\n")


# The log's clock == real UTC here, so "stale" is measured straightforwardly.
def stamp(offset=0):
    return time.strftime("%m/%d/%Y %H:%M:%S", time.gmtime(time.time() + offset))


append(f"{stamp()}: Game server connected")
append(f"{stamp()}:  Connections 0 ZDOS:81  sent:0 recv:0")

env = {**os.environ, "LOWMS_API_KEY": "lowms_test"}
p = subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "valheim_discord_monitor.py"),
                      "--config", os.path.join(d, "config.json"), "--from-start"],
                     env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

time.sleep(4)
with state["lock"]:
    early = list(state["posts"])

# --- the outage: the node dies. No shutdown line is ever written; the log just stops. ---
state["status"] = "offline"
time.sleep(9)                      # past the fresh-override, so the API verdict stands
with state["lock"]:
    during = list(state["posts"])

time.sleep(5)
with state["lock"]:
    after_down = list(state["posts"])

# --- recovery: node returns and the server boots (fresh log line) ---
state["status"] = "running"
append(f"{stamp()}: Game server connected")
append(f"{stamp()}:  Connections 0 ZDOS:81  sent:0 recv:0")
time.sleep(6)
p.terminate()
out = p.communicate(timeout=10)[0]
with state["lock"]:
    posts = list(state["posts"])

print("Discord posts:")
for x in posts:
    print("   ", x)

downs = [x for x in posts if "appears to be DOWN" in x]
recs = [x for x in posts if "back up after" in x]
checks = [
    ("no alert while healthy", not any("DOWN" in x for x in early)),
    ("outage alerted even though NO shutdown line was written", len(downs) == 1),
    ("alert names the LOW.MS status", downs and "offline" in downs[0]),
    ("exactly one down alert (no repeats)", len(downs) == 1),
    ("recovery posted once", len(recs) == 1),
    ("recovery reports the downtime", recs and "back up after" in recs[0]),
    ("no duplicate 'is offline' from the log detector", not any("is offline" in x for x in posts)),
]
for n, ok in checks:
    print(("PASS " if ok else "FAIL ") + n)
if not all(ok for _, ok in checks):
    print("---- monitor output ----"); print(out[-3000:])
print(f"{sum(bool(ok) for _, ok in checks)}/{len(checks)} passed")
