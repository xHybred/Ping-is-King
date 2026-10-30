"""Process discovery for Call of Duty titles since Modern Warfare (2019).

Titles rename their executable between releases: MW2019 and Warzone 1 run
ModernWarfare.exe, Cold War runs BlackOpsColdWar.exe, and everything from
Vanguard on shares cod.exe or a prefixed build such as cod25-cod.exe.
Matching the name shape means the next rename works without a code change.

Campaign builds (sp24-cod.exe and friends) are skipped on purpose. They never
join a match, so capturing their sockets would only widen the filter.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

import psutil

# Titles whose executable does not follow the "<prefix>-cod.exe" shape.
GAME_PROCESSES = (
    "modernwarfare.exe",  # Modern Warfare (2019), Warzone 1
    "blackopscoldwar.exe",  # Black Ops Cold War
    "cod.exe",  # Vanguard, MWII, MWIII, BO6 through CoD HQ
    "cod_hq.exe",  # CoD HQ launcher
    "warzone.exe",  # standalone Warzone builds
)

# cod24-cod.exe, cod25-cod.exe, and whatever the next one is called.
_PREFIXED_COD = re.compile(r"^[a-z0-9]+-cod\.exe$")

# sp24-cod.exe, sp25-cod.exe: campaign only, excluded before the pattern runs.
_CAMPAIGN = re.compile(r"^sp[0-9]+-cod\.exe$")


def matches(name: str) -> str:
    """Why this process name looks like a game, or "" when it does not."""
    name = (name or "").lower()
    if not name:
        return ""
    if _CAMPAIGN.match(name):
        return ""
    if name in GAME_PROCESSES:
        return "exact name"
    if _PREFIXED_COD.match(name):
        return "name pattern"
    return ""


def find_games() -> list[dict]:
    found = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        name = proc.info.get("name") or ""
        match = matches(name)
        if not match:
            continue
        found.append(
            {
                "pid": proc.info["pid"],
                "name": proc.info["name"],
                "exe": proc.info.get("exe") or "",
                "match": match,
            }
        )
    return found


def find_game() -> dict | None:
    """The running title most likely to be in a match.

    CoD HQ can leave a launcher and a game module alive together, so prefer
    the process that actually holds UDP sockets.
    """
    found = find_games()
    if not found:
        return None
    if len(found) == 1:
        return found[0]
    ports = udp_ports_map([game["pid"] for game in found])
    found.sort(key=lambda game: (len(ports.get(game["pid"], ())), game["pid"]), reverse=True)
    return found[0]


def udp_ports(pid: int) -> list[int]:
    """Local UDP ports the game has bound, which is what we capture on."""
    return udp_ports_map([pid]).get(pid, [])


def udp_ports_map(pids: Iterable[int]) -> dict[int, list[int]]:
    """Ports per PID in one pass, because one net_connections call is cheaper."""
    wanted = set(pids)
    if not wanted:
        return {}
    ports: dict[int, set[int]] = {pid: set() for pid in wanted}
    try:
        conns = psutil.net_connections(kind="udp")
    except (psutil.AccessDenied, OSError):
        return {}
    for conn in conns:
        if conn.pid not in wanted or not conn.laddr:
            continue
        port = getattr(conn.laddr, "port", None)
        if port:
            ports[conn.pid].add(int(port))
    return {pid: sorted(values) for pid, values in ports.items()}


def uac_enabled() -> bool:
    """True when this process can load the WinDivert driver."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
