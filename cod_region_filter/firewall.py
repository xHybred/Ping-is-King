"""Windows Firewall enforcement, used instead of a packet driver.

Block rules are scoped to the game executable and to UDP, so the rest of the
machine is untouched. Rules are named with one prefix and rebuilt together;
Windows Firewall gives block rules priority over allow rules, so service
endpoints stay reachable by leaving their ranges out of the block list rather
than by adding allow rules on top.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time

PREFIX = "COD Ping King Block"
CHUNK = 600
MIN_GAP_S = 3.0


def _hidden_start() -> tuple[int, subprocess.STARTUPINFO | None]:
    """Keep the console helper invisible; a GUI app's child otherwise opens one."""
    if os.name != "nt":
        return 0, None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return 0x08000000, info


def available() -> bool:
    return os.name == "nt"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class Firewall(threading.Thread):
    """Applies the latest block set on a worker thread, coalescing requests."""

    def __init__(self, log=None) -> None:
        super().__init__(name="firewall", daemon=True)
        self.log = log or (lambda *_: None)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._pending: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        self._signature: tuple | None = None
        self._rules = 0
        self._applying = False
        self._error = ""
        self._last_ms = 0
        self._last_apply = 0.0

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "available": available(),
                "rules": self._rules,
                "applying": self._applying,
                "error": self._error,
                "last_ms": self._last_ms,
            }

    def sync(self, exes: tuple[str, ...] | list[str], cidrs: tuple[str, ...] | list[str]) -> None:
        exes = tuple(sorted({exe for exe in exes if exe}))
        cidrs = tuple(sorted(set(cidrs)))
        signature = (exes, hashlib.sha1("\n".join(cidrs).encode()).hexdigest())
        with self._lock:
            if signature == self._signature and self._pending is None:
                return
            self._signature = signature
            self._pending = (exes, cidrs)
        self._wake.set()

    def clear(self) -> None:
        self.sync((), ())

    def close(self) -> None:
        self.clear()
        self._stop_event.set()
        self._wake.set()
        if self.is_alive():
            self.join(timeout=8)

    def run(self) -> None:
        while not self._stop_event.is_set():
            if not self._wake.wait(0.5):
                continue
            self._wake.clear()
            # Let a burst of changes settle into one rule set.
            if self._stop_event.wait(0.35):
                break
            delay = self._last_apply + MIN_GAP_S - time.monotonic()
            if delay > 0 and self._stop_event.wait(delay):
                break
            with self._lock:
                pending = self._pending
                self._pending = None
            if pending is None or self._stop_event.is_set():
                continue
            self._last_apply = time.monotonic()
            self._apply(*pending)

    def _apply(self, exes: tuple[str, ...], cidrs: tuple[str, ...]) -> None:
        started = time.monotonic()
        with self._lock:
            self._applying = True
            self._error = ""
        rules = 0
        try:
            lines = [
                "$ErrorActionPreference = 'Stop'",
                "Get-NetFirewallRule -DisplayName 'COD Ping King Block *' "
                "-ErrorAction SilentlyContinue | Remove-NetFirewallRule",
            ]
            if exes and cidrs:
                index = 0
                for exe in exes:
                    for start in range(0, len(cidrs), CHUNK):
                        chunk = cidrs[start : start + CHUNK]
                        addresses = ",".join(_ps_quote(cidr) for cidr in chunk)
                        lines.append(
                            "New-NetFirewallRule "
                            f"-DisplayName 'COD Ping King Block {index}' "
                            "-Direction Outbound -Action Block -Protocol UDP "
                            f"-Program {_ps_quote(exe)} "
                            f"-RemoteAddress @({addresses}) "
                            "-Profile Any -Enabled True | Out-Null"
                        )
                        index += 1
                rules = index
            script = "\n".join(lines)
            path = os.path.join(tempfile.gettempdir(), "cod-ping-king-firewall.ps1")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(script)
            flags, startupinfo = _hidden_start()
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=flags,
                startupinfo=startupinfo,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "unknown error").strip().splitlines()
                raise RuntimeError(detail[-1] if detail else "firewall command failed")
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            self.log(f"firewall failed: {type(exc).__name__}: {exc}")
            rules = 0
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            with self._lock:
                self._rules = rules
                self._applying = False
                self._last_ms = elapsed
            if rules:
                self.log(f"firewall: {rules} block rules, {len(cidrs)} ranges in {elapsed}ms")
            elif exes:
                self.log("firewall: no rules applied")
            else:
                self.log("firewall: rules cleared")
