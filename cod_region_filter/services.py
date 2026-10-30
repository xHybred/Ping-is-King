"""Service endpoints that must never be filtered.

Blocking auth or telemetry produces "unable to connect to online services",
which looks like the tool is broken. Lobby, ownership and content hosts are
resolved once at start and pinned as allowed.

Content delivery (texture streaming, patches) runs over TCP 443, which the
capture filter never touches, but the hosts are pinned too so a future port
change cannot break it.
"""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor

SERVICE_HOSTS = (
    # Demonware: auth, lobby and matchmaking
    "demonware.net",
    "prod.demonware.net",
    "accounts.demonware.net",
    # Platform and ownership
    "activision.com",
    "callofduty.com",
    "battle.net",
    "blizzard.com",
    "steamcommunity.com",
    "steampowered.com",
    "xboxlive.com",
    "playstation.net",
    # Content delivery and texture streaming
    "cloudfront.net",
    "akamaihd.net",
    "akamaized.net",
    "llnwd.net",
    "steamcontent.com",
    "blz-contentstack.com",
)


def _resolve(host: str) -> list[str]:
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]
    except OSError:
        return []


def resolve_services() -> list[str]:
    ips: set[str] = set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_resolve, host) for host in SERVICE_HOSTS]
        for future in futures:
            try:
                ips.update(future.result(timeout=3))
            except Exception:
                continue
    return sorted(ips)
