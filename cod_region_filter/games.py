"""Process discovery for the installed Call of Duty build."""

from __future__ import annotations

import psutil

# Call of Duty HQ ships "cod.exe"; older and regional builds differ.
GAME_PROCESSES = (
    "cod.exe",
    "cod_hq.exe",
    "modernwarfare.exe",
    "modernwarfare4.exe",
    "mw4.exe",
    "warzone.exe",
    "blackops.exe",
    "sp24-cod.exe",
    "sp25-cod.exe",
)


def find_game() -> dict | None:
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        name = (proc.info.get("name") or "").lower()
        if name in GAME_PROCESSES:
            return {
                "pid": proc.info["pid"],
                "name": proc.info["name"],
                "exe": proc.info.get("exe") or "",
            }
    return None


def udp_ports(pid: int) -> list[int]:
    """Local UDP ports the game has bound, which is what we capture on."""
    ports: set[int] = set()
    try:
        conns = psutil.net_connections(kind="udp")
    except (psutil.AccessDenied, OSError):
        return []
    for conn in conns:
        if conn.pid != pid or not conn.laddr:
            continue
        port = getattr(conn.laddr, "port", None)
        if port:
            ports.add(int(port))
    return sorted(ports)


def uac_enabled() -> bool:
    """True when this process can load the WinDivert driver."""
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
