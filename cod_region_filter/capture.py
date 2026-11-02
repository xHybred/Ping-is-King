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

# The sniffer only feeds RTT and byte counters, so one copy in four is plenty.
# Forwarding every copy through the verdict path was competing with outbound
# re-injection and showed up as rubber-banding.
SNIFF_SAMPLE = 4

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
        self._attached_once = False
        self._handle = None
        self._open_filter = ""
        self._open_ports: tuple[int, ...] = ()
        self._opened_at = 0.0
        self._reopens = 0
        self._packets = 0
        self._sniff = None
        self._sniff_filter = ""
        self._sniffed = 0
        self._sniff_sampled = 0
        self._sniff_pending: dict[tuple[str, int], list[int]] = {}
        self._settle_logged = False
        self._target_lock = threading.Lock()
        self._ports_changed = threading.Event()
        self._hold_logged_at = 0.0
        self._slow_log_at = 0.0
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
        ports, pid, pids, game = self._snapshot()
        return {
            "available": available(),
            "running": self.is_alive() and self._handle is not None,
            "paused": not self._active.is_set(),
            "status": self._status,
            "error": self._error,
            "pid": pid,
            "pids": list(pids),
            "game": game,
            "ports": list(ports),
            "watched": list(filter_ports(ports, self._skip_3074)),
            "attach_mode": self._attach_mode,
            "packets": self._packets,
            "packets_per_second": rate,
            "reopens": self._reopens,
            "sniffed": self._sniffed,
            "sniff_sampled": self._sniff_sampled,
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
        if self._attached_once:
            # The wait exists for the game's launch socket churn. Reapplying it
            # whenever a voice socket appeared detached filtering for ten
            # seconds in the middle of a match.
            return True
        if ports != self._stable_ports:
            self._stable_ports = ports
            self._stable_since = now
            return False
        if self._stable_since is None:
            self._stable_since = now
            return False
        return now - self._stable_since >= ATTACH_SETTLE_S

    def _snapshot(self) -> tuple[tuple[int, ...], int | None, tuple[int, ...], dict | None]:
        with self._target_lock:
            return self._ports, self._pid, self._pids, self._game

    def _refresh_target(self) -> bool:
        """Read the game's sockets. Slow, so this never runs on the packet thread."""
        found = games.find_games()
        if not found:
            with self._target_lock:
                self._pid = None
                self._pids = ()
                self._game = None
                self._ports = ()
                self._stable_ports = ()
                self._stable_since = None
                self._attached_once = False
                self._settle_logged = False
                self._status = "waiting for game"
            return False

        ports_by_pid = games.udp_ports_map([game["pid"] for game in found])
        # A launcher and a game module can coexist; capture the union so the
        # filter does not depend on which one psutil lists first.
        primary = max(found, key=lambda game: (len(ports_by_pid.get(game["pid"], ())), game["pid"]))
        ports = tuple(sorted({port for values in ports_by_pid.values() for port in values}))
        with self._target_lock:
            previous = self._game or {}
            changed = (
                primary["pid"] != self._pid
                or primary["name"] != previous.get("name")
                or ports != self._ports
            )
            detected = (
                primary["name"] != previous.get("name")
                or primary["pid"] != previous.get("pid")
            )
            launched = self._pid is not None and primary["pid"] != self._pid
            if launched:
                # A new process is a new launch; settle again before attaching.
                self._attached_once = False
                self._settle_logged = False
            self._pid = primary["pid"]
            self._pids = tuple(sorted(game["pid"] for game in found))
            self._game = primary
            self._ports = ports
        if detected:
            self.log(
                f"game detected: {primary['name']} (pid {primary['pid']}, {primary['match']})"
            )
        return changed

    def _refresh_loop(self) -> None:
        """Keep the socket list current without stalling packet re-injection."""
        while not self._stop.is_set():
            try:
                if self._refresh_target():
                    self._ports_changed.set()
            except Exception as exc:
                self.log(f"target refresh failed: {type(exc).__name__}: {exc}")
            self._stop.wait(5.0)

    def _sniff_loop(self) -> None:
        """Mirror inbound packets without holding them. Used for RTT and bytes."""
        last_match_check = 0.0
        while not self._stop.is_set():
            if not self._active.is_set() or not self._attached.is_set():
                self._close_sniff()
                self._stop.wait(0.3)
                continue
            now = time.monotonic()
            if now - last_match_check > 1.0:
                last_match_check = now
                try:
                    in_match = self.engine.current_match() is not None
                except Exception:
                    in_match = False
                if in_match:
                    # During a match the estimator has enough samples, and a
                    # second handle copying every packet is the most expensive
                    # thing we do. Resume when the match ends.
                    self._close_sniff()
                    self._sniff_pending.clear()
                    self._stop.wait(1.0)
                    continue
            ports = filter_ports(self._snapshot()[0], self._skip_3074)
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
                        self.log(f"sniff open failed: {type(exc).__name__}: {exc}")
                        self._stop.wait(5.0)
                    continue
                self._sniff = handle
                self._sniff_filter = flt
            try:
                packet = self._sniff.recv()
            except Exception as exc:
                self._close_sniff()
                if not self._stop.is_set() and self._active.is_set():
                    # A sniff handle that keeps failing used to reopen every
                    # half second, which showed up as constant churn in the log.
                    self.log(f"sniff closed: {type(exc).__name__}")
                    self._stop.wait(3.0)
                continue
            self._sniffed += 1
            key = (packet.src_addr, packet.src_port)
            pending = self._sniff_pending.get(key)
            if pending is None:
                pending = self._sniff_pending[key] = [0, 0]
            pending[0] += 1
            pending[1] += len(packet.raw or b"")
            if pending[0] < SNIFF_SAMPLE:
                continue
            count, size = pending
            self._sniff_pending[key] = [0, 0]
            self._sniff_sampled += 1
            try:
                self.engine.observe_inbound(
                    packet.src_addr,
                    packet.src_port,
                    size,
                    packets=count,
                )
            except Exception as exc:
                self.log(f"sniff observe failed: {type(exc).__name__}: {exc}")

    def run(self) -> None:
        if pydivert is None:
            self._status = "driver package missing"
            self._error = "pydivert is not installed"
            return

        sniffer = threading.Thread(target=self._sniff_loop, name="sniff", daemon=True)
        sniffer.start()

        # Socket discovery is psutil-heavy. Keep it off the thread that has to
        # re-inject packets, or every refresh shows up as a hitch.
        self._refresh_target()
        refresher = threading.Thread(target=self._refresh_loop, name="capture-refresh", daemon=True)
        refresher.start()

        while not self._stop.is_set():
            if not self._active.is_set():
                self._close_handle()
                self._status = "paused"
                self._stop.wait(0.3)
                continue

            ports, _pid, _pids, _game = self._snapshot()
            watched = filter_ports(ports, self._skip_3074)
            if not ports:
                self._close_handle()
                self._stop.wait(2.0)
                continue
            if not watched:
                self._close_handle()
                self._status = "game running, no filterable UDP sockets"
                self._stop.wait(2.0)
                continue
            if not self._settled(time.monotonic(), watched):
                if not self._settle_logged:
                    self.log(
                        f"attach settle: waiting {ATTACH_SETTLE_S:.0f}s for "
                        f"{len(watched)} ports to hold still"
                    )
                    self._settle_logged = True
                self._close_handle()
                self._status = "waiting for game to settle"
                self._stop.wait(0.3)
                continue

            flt = port_filter(watched, "SrcPort")
            now = time.monotonic()
            reopen = self._handle is None or flt != self._open_filter
            if reopen and self._handle is not None and now - self._opened_at < REOPEN_MIN_INTERVAL_S:
                # A socket opened mid-session is not worth dropping the queued
                # packets a reopen costs; keep filtering the old set for now.
                if now - self._hold_logged_at > 15.0:
                    self.log(f"capture: holding {len(self._open_ports)}-port filter, {len(watched)} ports seen")
                    self._hold_logged_at = now
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
                    self.log(f"capture open failed: {type(exc).__name__}: {exc}")
                    self._stop.wait(5.0)
                    continue
                self._handle = handle
                self._open_filter = flt
                self._open_ports = watched
                self._opened_at = time.monotonic()
                self._reopens += 1
                self._attached_once = True
                self._settle_logged = False
                self._attached.set()
                self._status = "filtering"
                self.log(f"capture open: {len(watched)} ports, {len(flt)} filter chars")

            last_check = time.monotonic()
            try:
                while not self._stop.is_set() and self._active.is_set():
                    packet = self._handle.recv()
                    if not packet.is_outbound:
                        # The filter is source-port only, so this should not
                        # happen; release it untouched rather than hold it.
                        self._handle.send(packet)
                        continue
                    started = time.perf_counter()
                    self._packets += 1
                    verdict = self.engine.observe(
                        packet.dst_addr,
                        packet.dst_port,
                        "outbound",
                        len(packet.raw or b""),
                    )
                    if verdict["allow"]:
                        self._handle.send(packet)

                    spent = time.perf_counter() - started
                    now = time.monotonic()
                    if spent > 0.05 and now - self._slow_log_at > 5.0:
                        self._slow_log_at = now
                        self.log(f"capture slow: {spent * 1000:.0f}ms handling one packet")

                    if now - last_check > 1.0:
                        last_check = now
                        if self._ports_changed.is_set() and now - self._opened_at >= REOPEN_MIN_INTERVAL_S:
                            self._ports_changed.clear()
                            break
            except Exception as exc:
                if self._stop.is_set() or not self._active.is_set():
                    continue
                self._error = str(exc)
                self._status = "capture restarted"
                self.log(f"capture error: {type(exc).__name__}: {exc}")
                self._close_handle()
                self._stop.wait(2.0)
            finally:
                if self._stop.is_set() or not self._active.is_set():
                    self._close_handle()

        self._close_sniff()
        sniffer.join(timeout=2)
        refresher.join(timeout=2)
