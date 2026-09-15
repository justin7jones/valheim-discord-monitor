#!/usr/bin/env python3
"""
Minimal Steam A2S_INFO query (stdlib only).

    python a2s_probe.py 51.81.167.39            # Valheim query port = game port + 1 (2457)
    python a2s_probe.py 51.81.167.39 2457

Prints the server name, player count and max players, or an error.
"""
from __future__ import annotations

import socket
import struct
import sys
import time

A2S_INFO_REQUEST = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"


def _read_cstring(buf: bytes, pos: int) -> tuple[str, int]:
    end = buf.index(b"\x00", pos)
    return buf[pos:end].decode("utf-8", errors="replace"), end + 1


def a2s_info(host: str, port: int, timeout: float = 3.0) -> dict:
    """Query a Source-engine-compatible server. Handles the 2020+ challenge handshake."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(A2S_INFO_REQUEST, (host, port))
        data, _ = s.recvfrom(4096)
        if data[4:5] == b"A":                       # challenge response
            challenge = data[5:9]
            s.sendto(A2S_INFO_REQUEST + challenge, (host, port))
            data, _ = s.recvfrom(4096)

    if data[:4] != b"\xFF\xFF\xFF\xFF" or data[4:5] != b"I":
        raise ValueError(f"unexpected A2S response header: {data[:5]!r}")

    pos = 6                                          # skip header + protocol byte
    name, pos = _read_cstring(data, pos)
    game_map, pos = _read_cstring(data, pos)
    folder, pos = _read_cstring(data, pos)
    game, pos = _read_cstring(data, pos)
    app_id, players, max_players, bots = struct.unpack_from("<HBBB", data, pos)
    pos += 5
    server_type, env, visibility, vac = struct.unpack_from("<ccBB", data, pos)
    pos += 4
    version, pos = _read_cstring(data, pos)
    return {
        "name": name, "map": game_map, "folder": folder, "game": game, "app_id": app_id,
        "players": players, "max_players": max_players, "bots": bots,
        "password": bool(visibility), "version": version,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 2457
    t0 = time.time()
    try:
        info = a2s_info(host, port)
    except socket.timeout:
        sys.exit(f"No reply from {host}:{port} within 3s — is the query port {port} (game port + 1)?")
    print(f"{info['name']}  —  {info['players']}/{info['max_players']} players  "
          f"(v{info['version']}, password={'yes' if info['password'] else 'no'}, {1000*(time.time()-t0):.0f} ms)")
