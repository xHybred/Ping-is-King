"""WinDivert capture loop.

Denied packets are dropped instead of re-injected. Closing the handle on
exit releases anything still queued, so a crash fails open rather than
leaving the machine without network.
"""

from __future__ import annotations

import threading
import time

from . import games

try:
    import pydivert
except Exception:  # driver package or DLL missing
    pydivert = None


def available() -> bool:
    return pydivert is not None


class CaptureWorker(threading.Thread):
    def __init__(self, engine, log=None) -> None:
        super().__init__(name="capture", daemon=True)
        self.engine = engine
        self.log = log or (lambda *_: None)
        self._stop = threading.Event()
        self._handle = None
        self._pid: int | None = None
        self._pids: tuple[int, ...] = ()
        self._game: dict | None = None
        self._ports: tuple[int, ...] = ()
        self._status = "starting"
        self._error = ""

    @property
    def status(self) -> dict:
        return {
            "available": available(),
            "running": self.is_alive() and self._handle is not None,
            "status": self._status,
            "error": self._error,
            "pid": self._pid,
            "pids": list(self._pids),
            "game": self._game,
            "ports": list(self._ports),
        }

    def stop(self) -> None:
        self._stop.set()
        handle = self._handle
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _filter(self) -> str:
        if not self._ports:
            return ""
        parts = []
        for port in self._ports:
            parts.append(f"udp.DstPort == {port}")
            parts.append(f"udp.SrcPort == {port}")
        return "udp and (" + " or ".join(parts) + ")"

    def _refresh_target(self) -> bool:
        found = games.find_games()
        if not found:
            self._pid = None
            self._pids = ()
            self._game = None
            self._ports = ()
            self._status = "waiting for game"
            return False

        ports_by_pid = games.udp_ports_map([game["pid"] for game in found])
        # A launcher and a game module can coexist; capture the union so the
        # filter does not depend on which one psutil lists first.
        primary = max(found, key=lambda game: (len(ports_by_pid.get(game["pid"], ())), game["pid"]))
        ports = tuple(sorted({port for values in ports_by_pid.values() for port in values}))
        previous = self._game or {}
        changed = (
            primary["pid"] != self._pid
            or primary["name"] != previous.get("name")
            or ports != self._ports
        )
        if primary["name"] != previous.get("name"):
            self.log(
                f"game detected: {primary['name']} (pid {primary['pid']}, {primary['match']})"
            )
        self._pid = primary["pid"]
        self._pids = tuple(sorted(game["pid"] for game in found))
        self._game = primary
        self._ports = ports
        if not ports:
            self._status = "game running, no UDP sockets yet"
            return False
        self._status = "filtering"
        return changed

    def run(self) -> None:
        if pydivert is None:
            self._status = "driver package missing"
            self._error = "pydivert is not installed"
            return

        reopen = True
        while not self._stop.is_set():
            if reopen:
                self._refresh_target()
                if not self._ports:
                    self._stop.wait(3.0)
                    continue

                flt = self._filter()
                try:
                    self._handle = pydivert.WinDivert(flt, priority=0)
                    self._handle.open()
                    self.log(f"capture open: {flt}")
                except Exception as exc:
                    self._status = "driver error"
                    self._error = str(exc)
                    self.log(f"capture open failed: {exc}")
                    self._handle = None
                    self._stop.wait(5.0)
                    continue
                reopen = False

            last_refresh = time.monotonic()
            try:
                while not self._stop.is_set():
                    packet = self._handle.recv()
                    outbound = packet.is_outbound
                    ip = packet.dst_addr if outbound else packet.src_addr
                    port = packet.dst_port if outbound else packet.src_port
                    verdict = self.engine.observe(
                        ip, port, "outbound" if outbound else "inbound", len(packet.raw or b"")
                    )
                    if verdict["allow"]:
                        self._handle.send(packet)

                    if time.monotonic() - last_refresh > 5.0:
                        last_refresh = time.monotonic()
                        if self._refresh_target():
                            reopen = True
                            break
            except Exception as exc:
                if self._stop.is_set():
                    break
                self._error = str(exc)
                self._status = "capture restarted"
                self.log(f"capture error: {exc}")
                reopen = True
                self._stop.wait(2.0)
            finally:
                handle, self._handle = self._handle, None
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass
