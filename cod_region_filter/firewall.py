"""Windows Firewall enforcement, used instead of a packet driver.

Rules are scoped to the game executable and to UDP, and one rule is kept per
region so a policy change touches only the regions that changed. Windows
Firewall gives block rules priority over allow rules, so service endpoints stay
reachable by leaving their ranges out of the block list rather than by adding
allow rules on top.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time

PREFIX = "COD Ping King Block"
MIN_GAP_S = 3.0

# Rule groups are (name, cidrs) pairs. A region key is used as the name so a
# later sync can update that region without touching the others.
Groups = tuple[tuple[str, tuple[str, ...]], ...]


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


def _rule_name(group: str, exe_index: int) -> str:
    return f"{PREFIX} {group} {exe_index}"


class Firewall(threading.Thread):
    """Applies the latest block set on a worker thread, coalescing requests."""

    def __init__(self, log=None) -> None:
        super().__init__(name="firewall", daemon=True)
        self.log = log or (lambda *_: None)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._pending: tuple[tuple[str, ...], Groups] | None = None
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

    def sync(self, exes: tuple[str, ...] | list[str], groups: dict[str, list[str]]) -> None:
        exes = tuple(sorted({exe for exe in exes if exe}))
        cleaned: list[tuple[str, tuple[str, ...]]] = []
        for name, cidrs in sorted(groups.items()):
            values = tuple(sorted(set(cidrs)))
            if values:
                cleaned.append((name, values))
        groups = tuple(cleaned)
        signature = (
            exes,
            tuple((name, hashlib.sha1("\n".join(cidrs).encode()).hexdigest()) for name, cidrs in groups),
        )
        with self._lock:
            if signature == self._signature and self._pending is None:
                return
            self._signature = signature
            self._pending = (exes, groups)
        self._wake.set()

    def clear(self) -> None:
        self.sync((), {})

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

    def _apply(self, exes: tuple[str, ...], groups: Groups) -> None:
        started = time.monotonic()
        with self._lock:
            self._applying = True
            self._error = ""
        rules = 0
        try:
            lines = [
                "$ErrorActionPreference = 'Stop'",
                "$keep = @()",
            ]
            if exes and groups:
                for exe_index, exe in enumerate(exes):
                    for name, cidrs in groups:
                        rule = _rule_name(name, exe_index)
                        addresses = ",".join(_ps_quote(cidr) for cidr in cidrs)
                        lines.extend(
                            [
                                f"$name = {_ps_quote(rule)}",
                                "$keep += $name",
                                "$rule = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue",
                                "if ($null -eq $rule) {",
                                "  New-NetFirewallRule -DisplayName $name "
                                "-Direction Outbound -Action Block -Protocol UDP "
                                f"-Program {_ps_quote(exe)} "
                                f"-RemoteAddress @({addresses}) "
                                "-Profile Any -Enabled True | Out-Null",
                                "} else {",
                                "  $rule | Get-NetFirewallApplicationFilter "
                                f"| Set-NetFirewallApplicationFilter -Program {_ps_quote(exe)}",
                                "  $rule | Get-NetFirewallAddressFilter "
                                f"| Set-NetFirewallAddressFilter -RemoteAddress @({addresses})",
                                "  $rule | Set-NetFirewallRule -Enabled True -Action Block -Direction Outbound",
                                "}",
                            ]
                        )
                        rules += 1
            # Only remove rules that are no longer wanted, and only after the
            # new ones exist, so there is never a window with no block rules.
            lines.append(
                "Get-NetFirewallRule -DisplayName 'COD Ping King Block *' "
                "-ErrorAction SilentlyContinue | "
                "Where-Object { $keep -notcontains $_.DisplayName } | "
                "Remove-NetFirewallRule"
            )
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
                timeout=180,
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
                self.log(
                    f"firewall: {rules} block rules, "
                    f"{sum(len(cidrs) for _, cidrs in groups)} ranges in {elapsed}ms"
                )
            elif exes:
                self.log("firewall: no rules applied")
            else:
                self.log("firewall: rules cleared")
