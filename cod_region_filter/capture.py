"""WinDivert capture loop.

Outbound packets are intercepted and re-injected after the policy allows them;
inbound packets are only sniffed, so the receive path never waits for Python.
Denied outbound packets are dropped instead of re-injected. Closing the handle
on exit releases anything still queued, so a crash fails open rather than
leaving the machine without network.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

from . import games
try:
    import pydivert
except Exception:  # driver package or DLL missing
    pydivert = None


# A game that was just launched opens and closes sockets for a while. Attaching
# during that churn means reopening the handle, and each reopen briefly stops
# re-injecting packets, which can drop the game's own login traffic.
ATTACH_SETTLE_S = 10.0

# A port that appears mid-session is not worth dropping packets for; letting
# the set drift for a few seconds avoids a reopen every time the game opens a
# voice or party socket.
REOPEN_MIN_INTERVAL_S = 15.0

DRIVER_SERVICES = ("WinDivert", "WinDivert1.4")


def available() -> bool:
    return pydivert is not None


def filter_ports(ports, skip_3074: bool = False) -> tuple[int, ...]:
    """Ports to intercept.

    Every UDP port the game owns is watched. 3074 is the game's main socket,
    so excluding it here silently stops filtering match traffic; the engine's
    platform guard is about the remote port, not this one. The skip flag is
    only for the compatibility switch.
    """
    return tuple(port for port in ports if not (skip_3074 and port == 3074))


def port_filter(ports, field: str) -> str:
    """WinDivert expression for one direction.

    Outbound packets carry the local port as their source, inbound packets
    carry it as their destination.
    """
    parts = [f"udp.{field} == {port}" for port in ports]
    return "udp and (" + " or ".join(parts) + ")"


def unload_driver(log=None) -> bool:
    """Stop the WinDivert service so a closed app leaves no driver behind."""
    log = log or (lambda *_: None)
    if os.name != "nt":
        return False
    stopped = False
    for name in DRIVER_SERVICES:
        for verb in ("stop", "delete"):
            try:
                result = subprocess.run(
                    ["sc.exe", verb, name],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    creationflags=0x08000000,
                )
                if result.returncode == 0:
                    stopped = True
                else:
                    detail = (result.stdout or result.stderr or "").strip().splitlines()
                    log(f"sc {verb} {name}: {detail[0] if detail else result.returncode}")
            except Exception as exc:
                log(f"sc {verb} {name} failed: {exc}")
        time.sleep(0.4)
    return stopped


def driver_problem(status: dict) -> str:
    """Human-readable reason the packet driver cannot run, or "" when it is fine."""
    if status.get("running"):
        return ""
    if not status.get("available", True):
        return "The packet driver library is missing from this install. Reinstall the app."
    if status.get("status") == "driver error":
        return status.get("error") or "WinDivert could not start."
    return ""


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        engine,
        log=None,
        attach_mode: str = "safe",
        active: bool = True,
        skip_3074: bool = False,
    ) -> None:
        super().__init__(name="capture", daemon=True)
        self.engine = engine
        self.log = log or (lambda *_: None)
        self._stop = threading.Event()
        self._active = threading.Event()
        if active:
            self._active.set()
        self._attached = threading.Event()
        self._handle = None
        self._open_filter = ""
        self._opened_at = 0.0
        self._reopens = 0
        self._packets = 0
        self._sniff = None
        self._sniff_filter = ""
        self._pid: int | None = None
        self._pids: tuple[int, ...] = ()
        self._game: dict | None = None
        self._ports: tuple[int, ...] = ()
        self._attach_mode = "immediate" if attach_mode == "immediate" else "safe"
        self._skip_3074 = bool(skip_3074)
        self._stable_ports: tuple[int, ...] = ()
        self._stable_since: float | None = None
        self._status = "starting"
        self._error = ""

    @property
    def status(self) -> dict:
        elapsed = time.monotonic() - self._opened_at if self._opened_at else 0.0
        rate = round(self._packets / elapsed, 1) if elapsed > 0 else 0.0
        return {
            "available": available(),
            "running": self.is_alive() and self._handle is not None,
            "paused": not self._active.is_set(),
            "status": self._status,
            "error": self._error,
            "pid": self._pid,
            "pids": list(self._pids),
            "game": self._game,
            "ports": list(self._ports),
            "watched": list(filter_ports(self._ports, self._skip_3074)),
            "attach_mode": self._attach_mode,
            "packets": self._packets,
            "packets_per_second": rate,
            "reopens": self._reopens,
        }

    def stop(self) -> None:
        self._stop.set()
        self._close_handle()

    def pause(self) -> None:
        """Detach from the game. The handle closes, so nothing is intercepted."""
        self._active.clear()
        self._close_handle()

    def resume(self) -> None:
        self._active.set()

    def set_attach_mode(self, mode: str) -> None:
        self._attach_mode = "immediate" if mode == "immediate" else "safe"
        self._stable_since = None
        self._stable_ports = ()

    def set_skip_3074(self, skip: bool) -> None:
        self._skip_3074 = bool(skip)

    def _close_handle(self) -> None:
        handle, self._handle = self._handle, None
        self._open_filter = ""
        self._attached.clear()
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _close_sniff(self) -> None:
        handle, self._sniff = self._sniff, None
        self._sniff_filter = ""
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _settled(self, now: float, ports: tuple[int, ...]) -> bool:
        if self._attach_mode == "immediate":
            return True
        if ports != self._stable_ports:
            self._stable_ports = ports
            self._stable_since = now
            return False
        if self._stable_since is None:
            self._stable_since = now
            return False
        return now - self._stable_since >= ATTACH_SETTLE_S

    def _refresh_target(self) -> bool:
        found = games.find_games()
        if not found:
            self._pid = None
            self._pids = ()
            self._game = None
            self._ports = ()
            self._stable_ports = ()
            self._stable_since = None
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
        return changed

    def _sniff_loop(self) -> None:
        """Mirror inbound packets without holding them. Used for RTT and bytes."""
        while not self._stop.is_set():
            if not self._active.is_set() or not self._attached.is_set():
                self._close_sniff()
                self._stop.wait(0.3)
                continue
            ports = filter_ports(self._ports, self._skip_3074)
            if not ports:
                self._stop.wait(0.5)
                continue
            flt = port_filter(ports, "DstPort")
            if self._sniff is None or flt != self._sniff_filter:
                self._close_sniff()
                try:
                    # SNIFF is 1 in WinDivert and stable; older pydivert builds
                    # do not expose the enum, so fall back to the raw value.
                    flags = getattr(getattr(pydivert, "Flag", None), "SNIFF", 1)
                    handle = pydivert.WinDivert(flt, priority=0, flags=flags)
                    handle.open()
                except Exception as exc:
                    if not self._stop.is_set():
                        self.log(f"sniff open failed: {exc}")
                        self._stop.wait(5.0)
                    continue
                self._sniff = handle
                self._sniff_filter = flt
            try:
                packet = self._sniff.recv()
            except Exception:
                self._close_sniff()
                if not self._stop.is_set() and self._active.is_set():
                    self._stop.wait(0.5)
                continue
            try:
                self.engine.observe(
                    packet.src_addr,
                    packet.src_port,
                    "inbound",
                    len(packet.raw or b""),
                    count=False,
                )
            except Exception as exc:
                self.log(f"sniff observe failed: {exc}")

    def run(self) -> None:
        if pydivert is None:
            self._status = "driver package missing"
            self._error = "pydivert is not installed"
            return

        sniffer = threading.Thread(target=self._sniff_loop, name="sniff", daemon=True)
        sniffer.start()

        while not self._stop.is_set():
            if not self._active.is_set():
                self._close_handle()
                self._status = "paused"
                self._stop.wait(0.3)
                continue

            self._refresh_target()
            ports = filter_ports(self._ports, self._skip_3074)
            if not self._ports:
                self._close_handle()
                self._stop.wait(2.0)
                continue
            if not ports:
                self._close_handle()
                self._status = "game running, no filterable UDP sockets"
                self._stop.wait(2.0)
                continue
            if not self._settled(time.monotonic(), ports):
                self._close_handle()
                self._status = "waiting for game to settle"
                self._stop.wait(0.3)
                continue

            flt = port_filter(ports, "SrcPort")
            now = time.monotonic()
            reopen = self._handle is None or flt != self._open_filter
            if reopen and self._handle is not None and now - self._opened_at < REOPEN_MIN_INTERVAL_S:
                # A socket opened mid-session is not worth dropping the queued
                # packets a reopen costs; keep filtering the old set for now.
                reopen = False
            if reopen:
                # Close first: two live handles on the same filter would each
                # re-inject their own copy and duplicate every packet.
                self._close_handle()
                try:
                    handle = pydivert.WinDivert(flt, priority=0)
                    handle.open()
                except Exception as exc:
                    self._status = "driver error"
                    self._error = str(exc)
                    self.log(f"capture open failed: {exc}")
                    self._stop.wait(5.0)
                    continue
                self._handle = handle
                self._open_filter = flt
                self._opened_at = time.monotonic()
                self._reopens += 1
                self._attached.set()
                self._status = "filtering"
                self.log(f"capture open: {flt}")

            last_refresh = time.monotonic()
            try:
                while not self._stop.is_set() and self._active.is_set():
                    packet = self._handle.recv()
                    if not packet.is_outbound:
                        # The filter is source-port only, so this should not
                        # happen; release it untouched rather than hold it.
                        self._handle.send(packet)
                        continue
                    self._packets += 1
                    verdict = self.engine.observe(
                        packet.dst_addr,
                        packet.dst_port,
                        "outbound",
                        len(packet.raw or b""),
                    )
                    if verdict["allow"]:
                        self._handle.send(packet)

                    if time.monotonic() - last_refresh > 5.0:
                        last_refresh = time.monotonic()
                        if self._refresh_target():
                            break
            except Exception as exc:
                if self._stop.is_set() or not self._active.is_set():
                    continue
                self._error = str(exc)
                self._status = "capture restarted"
                self.log(f"capture error: {exc}")
                self._close_handle()
                self._stop.wait(2.0)
            finally:
                if self._stop.is_set() or not self._active.is_set():
                    self._close_handle()

        self._close_sniff()
        sniffer.join(timeout=2)
