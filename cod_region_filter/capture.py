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
        game = games.find_game()
        if not game:
            self._pid = None
            self._ports = ()
            self._status = "waiting for game"
            return False
        ports = tuple(games.udp_ports(game["pid"]))
        changed = game["pid"] != self._pid or ports != self._ports
        self._pid = game["pid"]
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
