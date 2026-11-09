"""Traffic observation from ETW, with no kernel driver.

The Windows kernel network provider reports every UDP send and receive with the
owning process id, so this replaces the packet capture that used to load
WinDivert. Ricochet closes the game when that driver is loaded, and there is no
legitimate way to hide a driver from it. ETW is a Windows logging facility, not
an injected component.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
import uuid

from . import games

try:
    import etw
except Exception:  # pywintrace missing, or not Windows
    etw = None

PROVIDER_NAME = "Microsoft-Windows-Kernel-Network"
PROVIDER_GUID = "{7DD42A49-5329-4832-8DFD-43D979153A88}"
SESSION_PREFIX = "CodPingKingEtw"

# Kernel-Network event ids used here. 42/43 are UDPv4 send/receive; 44/45 are
# the IPv6 pair. TCP is ignored because match traffic is UDP.
UDP_SEND = (42, 44)
UDP_RECV = (43, 45)


def available() -> bool:
    return os.name == "nt" and etw is not None


def _admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _stop_stale_sessions(log) -> None:
    """Remove sessions left by an earlier crash so the new one can start."""
    try:
        result = subprocess.run(
            ["logman", "query", "-ets"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=0x08000000,
        )
    except (OSError, subprocess.SubprocessError):
        return
    for line in result.stdout.splitlines():
        name = line.split(",")[0].strip()
        if not name.startswith(SESSION_PREFIX):
            continue
        try:
            subprocess.run(
                ["logman", "stop", name, "-ets"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=0x08000000,
            )
            log(f"etw: stopped stale session {name}")
        except (OSError, subprocess.SubprocessError):
            pass


class NetWatch(threading.Thread):
    """Feeds engine.observe from ETW for every process that looks like CoD."""

    def __init__(self, engine, log=None) -> None:
        super().__init__(name="netwatch", daemon=True)
        self.engine = engine
        self.log = log or (lambda *_: None)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._pids: tuple[int, ...] = ()
        self._exes: tuple[str, ...] = ()
        self._games: list[dict] = []
        self._ports: tuple[int, ...] = ()
        self._status = "starting"
        self._error = ""
        self._trace = None
        self._session = ""
        self._packets = 0
        self._started_at = 0.0
        self._event_error_at = 0.0

    # -- status -----------------------------------------------------------

    @property
    def status(self) -> dict:
        with self._lock:
            pids = self._pids
            games_found = list(self._games)
            ports = self._ports
            status = self._status
            error = self._error
            packets = self._packets
        elapsed = max(0.001, time.monotonic() - self._started_at) if self._started_at else 0
        game = games_found[0] if games_found else None
        return {
            "available": available(),
            "running": self.is_alive() and status == "watching",
            "status": status,
            "error": error,
            "pid": game["pid"] if game else None,
            "pids": list(pids),
            "game": game["name"] if game else "",
            "ports": list(ports),
            "watched": list(ports),
            "packets": packets,
            "packets_per_second": round(packets / elapsed, 1) if elapsed else 0,
        }

    def exe_paths(self) -> tuple[str, ...]:
        with self._lock:
            return self._exes

    # -- lifecycle --------------------------------------------------------

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        if not available():
            with self._lock:
                self._status = "capture unavailable"
                self._error = "pywintrace is not installed"
            return
        if not _admin():
            with self._lock:
                self._status = "administrator required"
                self._error = "ETW needs administrator rights"
            return

        self._started_at = time.monotonic()
        _stop_stale_sessions(self.log)
        self._session = f"{SESSION_PREFIX}{uuid.uuid4().hex[:8]}"
        try:
            provider = etw.ProviderInfo(
                PROVIDER_NAME,
                etw.GUID(PROVIDER_GUID),
                level=4,
                any_keywords=0x30,
            )
            self._trace = etw.ETW(
                session_name=self._session,
                providers=[provider],
                event_callback=self._on_event,
            )
            self._trace.start()
        except Exception as exc:
            with self._lock:
                self._status = "capture error"
                self._error = f"{type(exc).__name__}: {exc}"
            self.log(f"etw start failed: {type(exc).__name__}: {exc}")
            return

        self.log(f"etw: session {self._session} watching game UDP traffic")
        with self._lock:
            self._status = "waiting for game"
        while not self._stop_event.wait(5.0):
            self._refresh_targets()
        self._stop_event_trace()

    def _stop_trace(self) -> None:
        trace = self._trace
        self._trace = None
        if trace is not None:
            try:
                trace.stop()
            except Exception as exc:
                self.log(f"etw stop failed: {type(exc).__name__}: {exc}")

    def _refresh_targets(self) -> None:
        found = games.find_games()
        pids = tuple(game["pid"] for game in found)
        exes = tuple(sorted({game["exe"] for game in found if game.get("exe")}))
        ports = tuple(
            sorted({port for values in games.udp_ports_map(pids).values() for port in values})
        )
        with self._lock:
            changed = pids != self._pids
            self._games = found
            self._pids = pids
            self._exes = exes
            self._ports = ports
            self._status = "watching" if pids else "waiting for game"
        if changed:
            if pids:
                names = ", ".join(f"{g['name']} (pid {g['pid']})" for g in found)
                self.log(f"game detected: {names}")
            else:
                self.log("game closed")

    # -- events -----------------------------------------------------------

    def _on_event(self, event) -> None:
        # pywintrace hands the callback one (event_id, data) tuple, not two args.
        try:
            event_id, data = event
        except (TypeError, ValueError):
            return
        try:
            self._handle_event(event_id, data)
        except Exception as exc:
            now = time.monotonic()
            if now - self._event_error_at > 60.0:
                self._event_error_at = now
                self.log(f"etw event failed: {type(exc).__name__}: {exc}")

    def _handle_event(self, event_id: int, data: dict) -> None:
        if event_id not in UDP_SEND and event_id not in UDP_RECV:
            return
        payload = data or {}
        try:
            pid = int(payload.get("PID") or 0)
        except (TypeError, ValueError):
            return
        if pid not in self._pids:
            return
        try:
            size = int(payload.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if event_id in UDP_SEND:
            ip = str(payload.get("daddr") or "")
            port = int(payload.get("dport") or 0)
            direction = "outbound"
        else:
            ip = str(payload.get("saddr") or "")
            port = int(payload.get("sport") or 0)
            direction = "inbound"
        if not ip or not port:
            return
        if ":" in ip and ip.count(":") > 1:
            # Keep the engine's address parsing predictable; IPv6 is rare here.
            return
        with self._lock:
            self._packets += 1
        try:
            self.engine.observe(ip, port, direction, size)
        except Exception as exc:
            self.log(f"etw observe failed: {type(exc).__name__}: {exc}")
