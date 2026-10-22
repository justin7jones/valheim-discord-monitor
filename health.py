#!/usr/bin/env python3
"""
Independent up/down monitoring for the game server.

The log-based detector only notices a *graceful* stop: Valheim writes
OnApplicationQuit / ZNet Shutdown, and we post "restarting". When the host node
dies, nothing is written at all -- the log simply stops -- so that detector stays
silent through the entire outage. This module watches from the outside instead.

Two independent signals, because neither is trustworthy alone:

  LOW.MS API   GET /v1/servers/{id} -> status. Authoritative when it answers, but it
               needs the servers:read scope, the field is an undocumented free-form
               string, and the API itself can be down.
  Log freshness Valheim writes "Connections N ZDOS" every 10 minutes even with nobody
               playing, so a log that has not advanced in ~25 minutes means the server
               is not running -- whatever any API says.

Combining them covers the cases either one misses:

  API says down               -> DOWN. A recognised down word is authoritative: waiting
                                 for the log to go stale would delay the alert by up to
                                 log_stale (25 min), defeating a 5-minute check. The one
                                 exception is a log line written in the last couple of
                                 minutes (log_fresh_override_seconds), which proves the
                                 server is alive right now and the status is mislabelled.
  API says up   + log stale   -> DOWN (hung/frozen server, or a stalled log feed)
  API unusable  + log stale   -> DOWN (works with no API scope at all)
  API unusable  + log fresh   -> UP

Stdlib only.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("valheim-monitor.health")

# The status field is free-form, so classify loosely and log anything unrecognised.
UP_WORDS = {"running", "online", "started", "active", "up", "ok", "healthy", "live"}
DOWN_WORDS = {"offline", "stopped", "crashed", "suspended", "terminated", "error",
              "failed", "down", "unavailable", "node_offline", "dead"}
TRANSIENT_WORDS = {"starting", "stopping", "restarting", "installing", "provisioning",
                   "updating", "pending", "queued", "transferring"}


def classify(status: Optional[str]) -> str:
    """-> 'up' | 'down' | 'unknown' for a status string of unknown vocabulary."""
    if not status:
        return "unknown"
    s = str(status).strip().lower().replace("-", "_")
    if s in UP_WORDS:
        return "up"
    if s in DOWN_WORDS:
        return "down"
    if s in TRANSIENT_WORDS:
        return "unknown"          # mid-transition: let the log decide
    return "unknown"


def human(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m}m" if m else f"{h}h"


class ServerHealth:
    def __init__(self, cfg: dict, api, log_age: Callable[[], Optional[float]],
                 notify: Callable[[str, str], None], suppress: Callable[[], bool] = lambda: False,
                 clock: Callable[[], float] = time.time, state_path: Optional[str] = None):
        self.api = api                      # maintenance.PublicAPI or None
        self.log_age = log_age              # seconds since the newest log line, or None
        self.notify, self.suppress = notify, suppress
        self.clock = clock
        self.interval = float(cfg.get("check_interval_seconds", 300))
        self.confirm = max(1, int(cfg.get("confirm_checks", 2)))
        self.log_stale = float(cfg.get("log_stale_seconds", 1500))
        # Only a very recent log line overrides an explicit "down" from the API; Valheim
        # writes every ~10 min, so anything older cannot distinguish "alive" from
        # "died 5 minutes ago", and we would rather alert than miss an outage.
        self.fresh_override = float(cfg.get("log_fresh_override_seconds", 120))
        self.state_path = Path(state_path or cfg.get("state_file", "health_state.json"))
        self.last_check = 0.0
        self.bad_streak = 0
        self.api_warned = False
        self.unknown_seen: set = set()
        st = self._load()
        self.down_since: Optional[float] = st.get("down_since")
        self.alerted: bool = bool(st.get("alerted"))

    # -- state ---------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except Exception:
            return {}

    def _save(self):
        try:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"down_since": self.down_since, "alerted": self.alerted}))
            tmp.replace(self.state_path)
        except Exception as e:  # noqa: BLE001
            log.debug("health state not saved: %s", e)

    # -- signals -------------------------------------------------------------
    def api_status(self) -> tuple[str, str]:
        """-> (verdict, detail). verdict is 'up' | 'down' | 'unknown'."""
        if self.api is None:
            return "unknown", "no API key configured"
        try:
            raw = (self.api.server() or {}).get("status")
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "403" in msg and not self.api_warned:
                self.api_warned = True
                log.warning("Health: the LOW.MS key lacks the servers:read scope, so status "
                            "checks fall back to log freshness only (%s)", msg)
            return "unknown", f"API unavailable ({msg[:90]})"
        verdict = classify(raw)
        if verdict == "unknown" and raw and raw not in self.unknown_seen:
            self.unknown_seen.add(raw)
            log.info("Health: unrecognised LOW.MS status %r — relying on the log instead", raw)
        return verdict, f"LOW.MS reports '{raw}'" if raw else "LOW.MS reported no status"

    def assess(self) -> tuple[bool, str]:
        """-> (is_down, human-readable detail)."""
        verdict, detail = self.api_status()
        age = self.log_age()
        fresh = age is not None and age <= self.log_stale
        age_txt = "no log timestamp yet" if age is None else f"log last wrote {human(age)} ago"

        if verdict == "down":
            if age is not None and age <= self.fresh_override:
                # A line written seconds ago proves it is alive; the status is mislabelled.
                log.info("Health: %s but %s — treating as up", detail, age_txt)
                return False, f"{detail}, but {age_txt}"
            return True, f"{detail}; {age_txt}"
        if not fresh and age is not None:
            why = "the server is not writing to its log" if verdict == "up" else detail
            return True, f"{why}; {age_txt}"
        if verdict == "up":
            return False, f"{detail}; {age_txt}"
        return False, f"{detail}; {age_txt}"      # nothing says it's down

    # -- loop ----------------------------------------------------------------
    def observe(self, ev) -> None:
        """Fast recovery: a fresh boot line means it is back, before the next check."""
        if ev.kind in ("server_online", "login") and self.down_since is not None:
            self._recover("the server is writing to its log again")

    def owns_offline(self) -> bool:
        """True when this module is posting outage alerts, so the log-derived
        'offline' message should stay quiet (and 'back online' while we are down)."""
        return True

    def absorbing_online(self) -> bool:
        return self.down_since is not None

    def _recover(self, detail: str):
        was = self.down_since
        alerted = self.alerted
        self.down_since, self.alerted, self.bad_streak = None, False, 0
        self._save()
        if alerted:
            dur = human(self.clock() - was) if was else "a while"
            self.notify("server_recovered", f"back up after {dur} — {detail}")

    def tick(self) -> Optional[str]:
        now = self.clock()
        if now - self.last_check < self.interval:
            return None
        self.last_check = now
        if self.suppress():
            self.bad_streak = 0
            return "skip: maintenance"
        down, detail = self.assess()
        if down:
            self.bad_streak += 1
            if self.down_since is None:
                self.down_since = now
            if not self.alerted and self.bad_streak >= self.confirm:
                self.alerted = True
                self._save()
                log.warning("Server appears DOWN: %s", detail)
                self.notify("server_down", detail)
                return f"down: {detail}"
            self._save()
            return f"down ({self.bad_streak}/{self.confirm}): {detail}"
        if self.down_since is not None:
            self._recover(detail)
            return f"recovered: {detail}"
        self.bad_streak = 0
        return f"up: {detail}"

    # -- one-shot report -----------------------------------------------------
    def report(self) -> list[str]:
        verdict, detail = self.api_status()
        age = self.log_age()
        down, why = self.assess()
        return [
            f"API status   : {detail} -> {verdict}",
            f"Log freshness: " + ("no timestamp yet" if age is None
                                  else f"{human(age)} ago (stale after {human(self.log_stale)})"),
            f"Verdict      : {'DOWN' if down else 'UP'} — {why}",
            f"Alerting     : every {human(self.interval)}, after {self.confirm} bad check(s) in a row"
            + (f"; currently down since {human(self.clock() - self.down_since)} ago" if self.down_since else ""),
        ]
