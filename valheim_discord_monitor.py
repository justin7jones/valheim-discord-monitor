#!/usr/bin/env python3
"""
Valheim -> Discord event monitor (no mods required).

Tails the vanilla Valheim dedicated-server console log (locally, over FTP/SFTP,
via the LOW.MS panel API, or any HTTP endpoint that returns the raw log text),
detects player login / logout / death events, and posts them to a Discord
webhook.

Only the Python standard library is required for file / ftp / http / nexus
sources. SFTP needs `pip install paramiko`.

Usage:
    python valheim_discord_monitor.py --config config.json
    python valheim_discord_monitor.py --config config.json --discover     # list candidate log files on the FTP server
    python valheim_discord_monitor.py --config config.json --replay sample.log   # dry-run the parser on a file
    python valheim_discord_monitor.py --config config.json --test-webhook # send a test message to Discord
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ftplib import FTP, FTP_TLS, error_perm
from typing import Iterator, Optional

log = logging.getLogger("valheim-monitor")

# ---------------------------------------------------------------------------
# Log line patterns (vanilla dedicated server, Steam and PlayFab/crossplay)
# ---------------------------------------------------------------------------
# Optional "MM/DD/YYYY HH:MM:SS: " prefix that the server prints on most lines.
_TS = r"^\s*(?:\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}:\s*)?"

# Character spawn / despawn. `owner` is the peer's ZDO owner id for this session.
RE_ZDOID = re.compile(_TS + r"Got character ZDOID from (?P<name>.+?) : (?P<owner>-?\d+):(?P<n>\d+)\s*$")
# Logout on PlayFab-relayed servers: the peer's non-persistent ZDOs get destroyed.
RE_ABANDONED = re.compile(_TS + r"Destroying abandoned non persistent zdo \S+ owner (?P<owner>-?\d+)")
# Logout on direct-Steam servers.
RE_CLOSE = re.compile(_TS + r"Closing socket (?P<id>\S+)")
RE_CONNECT_STEAM = re.compile(_TS + r"Got connection SteamID (?P<id>\S+)")
RE_CONNECT_PLAYFAB = re.compile(_TS + r"PlayFab listen socket child connected to remote player (?P<id>\S+)")
RE_PLATFORM_ID = re.compile(_TS + r"PlayFab socket with remote ID playfab/(?P<pf>\S+) received local Platform ID (?P<platform>\S+)")
RE_JOINED = re.compile(_TS + r"Player joined server \"(?P<server>.*)\".*?(?:now|currently) (?P<count>\d+) player")
RE_DISCONNECTED = re.compile(_TS + r"Player disconnected from server \"(?P<server>.*)\".*?(?:now|currently) (?P<count>\d+) player")
RE_READY = re.compile(_TS + r"Game server connected")
RE_TIMEOUT = re.compile(_TS + r"ZRpc timeout detected")


@dataclass
class Event:
    kind: str                   # login | logout | death | respawn | server_up | player_count
    player: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class ParserState:
    online: dict = field(default_factory=dict)       # character name -> owner id
    owner_to_name: dict = field(default_factory=dict)
    dead: set = field(default_factory=set)
    pending_ids: list = field(default_factory=list)  # Steam connection ids not yet paired with a name
    id_to_name: dict = field(default_factory=dict)   # Steam connection id -> name
    player_count: Optional[int] = None


class ValheimLogParser:
    def __init__(self):
        self.s = ParserState()

    def _logout(self, name: str) -> Event:
        owner = self.s.online.pop(name, None)
        self.s.owner_to_name.pop(owner, None)
        self.s.dead.discard(name)
        return Event("logout", name, {"count": len(self.s.online)})

    def feed(self, line: str) -> Iterator[Event]:
        line = line.rstrip("\r\n")
        if not line:
            return

        m = RE_ZDOID.search(line)
        if m:
            name, owner, n = m.group("name").strip(), m.group("owner"), m.group("n")
            if owner == "0" and n == "0":
                if name in self.s.online and name not in self.s.dead:
                    self.s.dead.add(name)
                    yield Event("death", name)
                return
            if name in self.s.dead:
                self.s.dead.discard(name)
                yield Event("respawn", name)
                return
            if name in self.s.online:
                return
            self.s.online[name] = owner
            self.s.owner_to_name[owner] = name
            if self.s.pending_ids:
                self.s.id_to_name[self.s.pending_ids.pop(0)] = name
            yield Event("login", name, {"count": len(self.s.online)})
            return

        m = RE_ABANDONED.search(line)
        if m:
            name = self.s.owner_to_name.get(m.group("owner"))
            if name:
                yield self._logout(name)
            return

        m = RE_CONNECT_STEAM.search(line) or RE_CONNECT_PLAYFAB.search(line)
        if m:
            cid = m.group("id")
            if cid not in self.s.pending_ids:
                self.s.pending_ids.append(cid)
            return

        m = RE_CLOSE.search(line)
        if m:
            cid = m.group("id")
            if cid in self.s.pending_ids:
                self.s.pending_ids.remove(cid)
                return
            name = self.s.id_to_name.pop(cid, None)
            if name and name in self.s.online:
                yield self._logout(name)
            return

        m = RE_JOINED.search(line) or RE_DISCONNECTED.search(line)
        if m:
            self.s.player_count = int(m.group("count"))
            yield Event("player_count", None, {"count": self.s.player_count, "server": m.group("server")})
            # If the server says nobody is online, reconcile anything we still think is online.
            if self.s.player_count == 0:
                for name in list(self.s.online):
                    yield self._logout(name)
            return

        if RE_READY.search(line):
            self.s = ParserState()
            yield Event("server_up")
            return


# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------
class Discord:
    COLORS = {"login": 0x57F287, "logout": 0x95A5A6, "death": 0xED4245, "respawn": 0xFEE75C, "server_up": 0x5865F2}
    EMOJI = {"login": "🟢", "logout": "⚪", "death": "💀", "respawn": "🔥", "server_up": "🛡️"}
    DEFAULT_MESSAGES = {
        "login": "**{player}** has arrived in {server}.",
        "logout": "**{player}** has left {server}.",
        "death": "**{player}** has died. Odin is watching.",
        "respawn": "**{player}** has respawned.",
        "server_up": "{server} is online.",
    }

    def __init__(self, webhook_url: str, username: str = "Valheim", show_count: bool = True,
                 use_embeds: bool = True, messages: Optional[dict] = None):
        self.url = webhook_url
        self.username = username
        self.show_count = show_count
        self.use_embeds = use_embeds
        self.messages = {**self.DEFAULT_MESSAGES, **(messages or {})}

    def post(self, ev: Event, server_name: str, event_filter: set) -> None:
        if ev.kind not in event_filter or ev.kind not in self.messages:
            return
        text = self.messages[ev.kind].format(player=ev.player, server=server_name, **ev.extra)
        emoji = self.EMOJI.get(ev.kind, "")
        footer = f"{ev.extra['count']} player(s) online" if self.show_count and "count" in ev.extra else None
        if self.use_embeds:
            embed = {"description": f"{emoji} {text}", "color": self.COLORS.get(ev.kind, 0),
                     "timestamp": datetime.now(timezone.utc).isoformat()}
            if footer:
                embed["footer"] = {"text": footer}
            payload = {"username": self.username, "embeds": [embed]}
        else:
            payload = {"username": self.username, "content": f"{emoji} {text}" + (f"  ({footer})" if footer else "")}
        self.send(payload)

    def send(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json",
                                                                  "User-Agent": "valheim-discord-monitor/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
                return
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry = float(e.headers.get("Retry-After", "2"))
                    log.warning("Discord rate limited; sleeping %.1fs", retry)
                    time.sleep(retry)
                    continue
                log.error("Discord HTTP %s: %s", e.code, e.read()[:200])
                return
            except Exception as e:
                log.warning("Discord post failed (%s), retrying", e)
                time.sleep(2 * (attempt + 1))
        log.error("Giving up on Discord post: %s", payload)


# ---------------------------------------------------------------------------
# Log sources
# ---------------------------------------------------------------------------
# Byte-offset sources implement size() and read_from(offset).
# Line-window sources implement fetch_lines() and are de-duplicated by overlap.

class LocalFileSource:
    def __init__(self, path: str):
        self.path = path

    def size(self) -> int:
        return os.path.getsize(self.path)

    def read_from(self, offset: int) -> bytes:
        with open(self.path, "rb") as f:
            f.seek(offset)
            return f.read()


class FTPSource:
    def __init__(self, host: str, port: int, user: str, password: str, path: str, tls: bool = False, passive: bool = True):
        self.host, self.port, self.user, self.password, self.path = host, port, user, password, path
        self.tls, self.passive = tls, passive
        self._ftp: Optional[FTP] = None

    def _conn(self) -> FTP:
        if self._ftp is not None:
            try:
                self._ftp.voidcmd("NOOP")
                return self._ftp
            except Exception:
                self._ftp = None
        ftp = FTP_TLS() if self.tls else FTP()
        ftp.connect(self.host, self.port, timeout=20)
        ftp.login(self.user, self.password)
        if self.tls:
            ftp.prot_p()  # type: ignore[attr-defined]
        ftp.set_pasv(self.passive)
        self._ftp = ftp
        return ftp

    def size(self) -> int:
        ftp = self._conn()
        ftp.voidcmd("TYPE I")
        return ftp.size(self.path) or 0

    def read_from(self, offset: int) -> bytes:
        ftp = self._conn()
        buf = io.BytesIO()
        ftp.voidcmd("TYPE I")
        ftp.retrbinary(f"RETR {self.path}", buf.write, rest=offset)
        return buf.getvalue()

    def discover(self, root: str = "/", patterns=("*.log", "*.txt", "*console*", "*output*"), max_depth: int = 5):
        """Walk the FTP tree and print files that look like logs, largest first."""
        ftp = self._conn()
        found: list = []

        def walk(d: str, depth: int):
            if depth > max_depth:
                return
            try:
                entries = list(ftp.mlsd(d))
                for name, facts in entries:
                    if name in (".", ".."):
                        continue
                    full = f"{d.rstrip('/')}/{name}"
                    if facts.get("type") == "dir":
                        walk(full, depth + 1)
                    elif any(fnmatch.fnmatch(name.lower(), p) for p in patterns):
                        found.append((int(facts.get("size", 0)), full))
                return
            except (error_perm, AttributeError):
                pass
            try:
                names = ftp.nlst(d)
            except error_perm:
                return
            for n in names:
                full = n if n.startswith("/") else f"{d.rstrip('/')}/{n}"
                base = full.rsplit("/", 1)[-1]
                if base in (".", ".."):
                    continue
                try:
                    ftp.cwd(full)
                    ftp.cwd("/")
                    walk(full, depth + 1)
                except error_perm:
                    if any(fnmatch.fnmatch(base.lower(), p) for p in patterns):
                        try:
                            ftp.voidcmd("TYPE I")
                            found.append((ftp.size(full) or 0, full))
                        except Exception:
                            found.append((0, full))

        walk(root, 0)
        found.sort(reverse=True)
        print("Candidate log files (size, path):")
        for size, path in found:
            print(f"  {size:>12}  {path}")
        if not found:
            print("  (none found — try --discover-root with a different directory)")


class SFTPSource:
    def __init__(self, host: str, port: int, user: str, password: str, path: str):
        try:
            import paramiko  # noqa: F401
        except ImportError:
            sys.exit("SFTP source requires paramiko:  pip install paramiko")
        self.host, self.port, self.user, self.password, self.path = host, port, user, password, path
        self._sftp = None

    def _conn(self):
        import paramiko
        if self._sftp is not None:
            try:
                self._sftp.stat(self.path)
                return self._sftp
            except Exception:
                self._sftp = None
        t = paramiko.Transport((self.host, self.port))
        t.connect(username=self.user, password=self.password)
        self._sftp = paramiko.SFTPClient.from_transport(t)
        return self._sftp

    def size(self) -> int:
        return self._conn().stat(self.path).st_size

    def read_from(self, offset: int) -> bytes:
        with self._conn().open(self.path, "rb") as f:
            f.seek(offset)
            return f.read()


class HTTPSource:
    """Polls a URL that returns the raw log text. Uses Range requests when the server honours them."""

    def __init__(self, url: str, headers: Optional[dict] = None):
        self.url = url
        self.headers = headers or {}
        self._cache: bytes = b""

    def _fetch(self, offset: int = 0):
        hdrs = dict(self.headers)
        if offset:
            hdrs["Range"] = f"bytes={offset}-"
        req = urllib.request.Request(self.url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read(), r.status == 206

    def size(self) -> int:
        self._cache, _ = self._fetch(0)
        return len(self._cache)

    def read_from(self, offset: int) -> bytes:
        data, partial = self._fetch(offset)
        return data if partial else data[offset:]


class NexusConsoleSource:
    """
    LOW.MS 'Nexus' panel console endpoint:
        GET https://api.prod.nexus.low.ms/user/servers/<server_id>/daemon/console?lines=N
        Authorization: Bearer <token>
    Returns the last N console lines. The token is the short-lived Auth0 access
    token the panel itself uses (see README). This is a line-window source.
    """

    def __init__(self, server_id: str, token: str, lines: int = 300,
                 base_url: str = "https://api.prod.nexus.low.ms"):
        self.url = f"{base_url}/user/servers/{server_id}/daemon/console?lines={lines}"
        self.token = token

    def fetch_lines(self) -> list[str]:
        req = urllib.request.Request(self.url, headers={"Authorization": f"Bearer {self.token}",
                                                        "Accept": "application/json, text/plain"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="replace")
            ctype = r.headers.get("Content-Type", "")
        if "json" in ctype:
            data = json.loads(raw)
            # Accept a few plausible shapes: ["line", ...], {"lines": [...]}, {"data": [...]}, {"data": {"lines": [...]}}
            for key in ("lines", "data", "output", "console"):
                if isinstance(data, dict) and key in data:
                    data = data[key]
                    if isinstance(data, dict) and "lines" in data:
                        data = data["lines"]
                    break
            if isinstance(data, str):
                return data.splitlines()
            if isinstance(data, list):
                return [x if isinstance(x, str) else (x.get("line") or x.get("message") or x.get("text") or json.dumps(x))
                        for x in data]
            raise ValueError(f"Unrecognised console JSON shape: {type(data).__name__}")
        return raw.splitlines()


# ---------------------------------------------------------------------------
# Tailers
# ---------------------------------------------------------------------------
class OffsetTailer:
    """Tails a byte-offset source, persisting the offset so restarts don't re-post."""

    def __init__(self, source, state_path: str, start_at_end: bool = True):
        self.source, self.state_path = source, state_path
        self.buffer = b""
        self.offset = self._load()
        if self.offset is None:
            self.offset = self.source.size() if start_at_end else 0
            self._save()

    def _load(self) -> Optional[int]:
        try:
            with open(self.state_path) as f:
                return int(json.load(f)["offset"])
        except Exception:
            return None

    def _save(self):
        with open(self.state_path, "w") as f:
            json.dump({"offset": self.offset, "saved": time.time()}, f)

    def poll(self) -> Iterator[str]:
        size = self.source.size()
        if size < self.offset:
            log.info("Log rotated/truncated (size %d < offset %d); restarting from 0", size, self.offset)
            self.offset, self.buffer = 0, b""
        if size == self.offset:
            return
        data = self.source.read_from(self.offset)
        if not data:
            return
        self.offset += len(data)
        self.buffer += data
        *lines, self.buffer = self.buffer.split(b"\n")
        self._save()
        for raw in lines:
            yield raw.decode("utf-8", errors="replace")


class WindowTailer:
    """Tails a source that returns the last N lines, emitting only lines not seen before (overlap match)."""

    OVERLAP = 8

    def __init__(self, source, start_at_end: bool = True):
        self.source = source
        self.prev: list[str] = self.source.fetch_lines() if start_at_end else []

    def poll(self) -> Iterator[str]:
        cur = self.source.fetch_lines()
        if not cur:
            return
        new_start = 0
        if self.prev:
            k = min(self.OVERLAP, len(self.prev))
            tail = self.prev[-k:]
            # Find the last position in `cur` where the previous tail ends.
            for i in range(len(cur) - k, -1, -1):
                if cur[i:i + k] == tail:
                    new_start = i + k
                    break
            else:
                log.warning("No overlap with previous window — the log moved more than %d lines between polls; "
                            "some events may have been missed. Consider a shorter poll interval or more lines.",
                            len(cur))
        self.prev = cur
        for line in cur[new_start:]:
            yield line


def build_source(cfg: dict):
    src = cfg["source"]
    t = src["type"].lower()
    if t == "file":
        return LocalFileSource(src["path"])
    if t == "ftp":
        return FTPSource(src["host"], int(src.get("port", 21)), src["user"], src["password"], src["path"],
                         tls=bool(src.get("tls", False)), passive=bool(src.get("passive", True)))
    if t == "sftp":
        return SFTPSource(src["host"], int(src.get("port", 22)), src["user"], src["password"], src["path"])
    if t == "http":
        return HTTPSource(src["url"], src.get("headers"))
    if t == "nexus":
        return NexusConsoleSource(src["server_id"], src["token"], int(src.get("lines", 300)),
                                  src.get("base_url", "https://api.prod.nexus.low.ms"))
    sys.exit(f"Unknown source type: {t}")


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = json.load(f)
    cfg.setdefault("discord", {})
    cfg.setdefault("source", {})
    # Secrets may be supplied via environment variables instead of the file.
    for env, section, key in (("DISCORD_WEBHOOK_URL", "discord", "webhook_url"),
                              ("VALHEIM_LOG_USER", "source", "user"),
                              ("VALHEIM_LOG_PASSWORD", "source", "password"),
                              ("NEXUS_TOKEN", "source", "token")):
        if os.environ.get(env):
            cfg[section][key] = os.environ[env]
    return cfg


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--discover", action="store_true", help="List candidate log files on the FTP server and exit")
    ap.add_argument("--discover-root", default="/", help="Directory to start --discover from")
    ap.add_argument("--replay", metavar="FILE", help="Parse a local log file and print events (no Discord posts unless --post)")
    ap.add_argument("--post", action="store_true", help="With --replay, actually post to Discord")
    ap.add_argument("--test-webhook", action="store_true", help="Send a test message to the Discord webhook and exit")
    ap.add_argument("--from-start", action="store_true", help="On first run, process the whole existing log instead of only new lines")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    server_name = cfg.get("server_name", "the server")
    events = set(cfg.get("events", ["login", "logout", "death"]))
    d = cfg["discord"]
    discord = Discord(d.get("webhook_url", ""), d.get("username", "Valheim"),
                      show_count=d.get("show_player_count", True), use_embeds=d.get("embeds", True),
                      messages=d.get("messages"))

    if args.replay:
        parser = ValheimLogParser()
        with open(args.replay, encoding="utf-8", errors="replace") as f:
            for line in f:
                for ev in parser.feed(line):
                    print(f"{ev.kind:12} {ev.player or '':24} {ev.extra}")
                    if args.post:
                        discord.post(ev, server_name, events)
        return

    if args.test_webhook:
        if not discord.url:
            sys.exit("No Discord webhook URL configured")
        discord.send({"username": discord.username, "content": f"✅ Valheim monitor connected for **{server_name}**."})
        print("Test message sent.")
        return

    source = build_source(cfg)
    if args.discover:
        if not isinstance(source, FTPSource):
            sys.exit("--discover only works with the ftp source type")
        source.discover(args.discover_root)
        return

    if not discord.url:
        sys.exit("No Discord webhook URL configured (config discord.webhook_url or DISCORD_WEBHOOK_URL)")

    interval = float(cfg.get("poll_interval_seconds", 10))
    if hasattr(source, "fetch_lines"):
        tailer = WindowTailer(source, start_at_end=not args.from_start)
    else:
        tailer = OffsetTailer(source, cfg.get("state_file", "monitor_state.json"), start_at_end=not args.from_start)
    parser = ValheimLogParser()
    log.info("Monitoring %s source for %s; posting %s every %.0fs", cfg["source"]["type"], server_name, sorted(events), interval)

    backoff = interval
    while True:
        try:
            for line in tailer.poll():
                log.debug("LOG: %s", line)
                for ev in parser.feed(line):
                    log.info("EVENT %-8s %s %s", ev.kind, ev.player or "", ev.extra)
                    discord.post(ev, server_name, events)
            backoff = interval
        except KeyboardInterrupt:
            log.info("Stopping")
            return
        except Exception as e:
            log.warning("Poll failed: %s (retrying in %.0fs)", e, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        time.sleep(interval)


if __name__ == "__main__":
    main()
