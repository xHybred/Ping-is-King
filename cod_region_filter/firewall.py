"""Windows Firewall enforcement, used instead of a packet driver.

Rules are scoped to the game executable and to UDP, and one rule is kept per
region so a policy change touches only the regions that changed. Windows
Firewall gives block rules priority over allow rules, so service endpoints stay
reachable by leaving their ranges out of the block list rather than by adding
allow rules on top.

Rules are disabled when filtering stops and re-enabled when it resumes. That
keeps a restart fast: rebuilding six thousand addresses takes tens of seconds,
while enabling an existing rule set takes well under one.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import paths

PREFIX = "COD Ping King Block"
MIN_GAP_S = 3.0
STATE_NAME = "firewall-state.json"
# Windows writes a rule slowly, and a full catalog is six thousand addresses.
# Few large rules finish; one rule per region (about eighty) timed out at 180s.
CHUNK = 1000

# Rule groups are (name, cidrs) pairs. Region sets are flattened into chunk
# names before they reach the rule writer.
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


def _chunk(groups: Groups) -> Groups:
    """Flatten the region sets into a few large rules.

    The firewall API cost scales with the number of rules much more than with
    the addresses inside one, and a region-per-rule set timed out on first run.
    """
    flat = sorted({cidr for _, cidrs in groups for cidr in cidrs})
    return tuple(
        (f"Set {index // CHUNK + 1}", tuple(flat[index : index + CHUNK]))
        for index in range(0, len(flat), CHUNK)
    )


def _signature_key(exes: tuple[str, ...], groups: Groups) -> str:
    body = json.dumps({"exes": exes, "groups": groups}, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def _state_path() -> Path:
    return paths.local_data_dir() / STATE_NAME


def _load_state() -> tuple[str, int]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", 0
    key = data.get("signature")
    rules = data.get("rules")
    if not isinstance(key, str) or not isinstance(rules, int) or rules < 0:
        return "", 0
    return key, rules


def _save_state(key: str, rules: int) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"signature": key, "rules": rules}), encoding="utf-8")


class Firewall(threading.Thread):
    """Applies the latest block set on a worker thread, coalescing requests."""

    def __init__(self, log=None) -> None:
        super().__init__(name="firewall", daemon=True)
        self.log = log or (lambda *_: None)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._pending: tuple[tuple[str, ...], Groups, str] | None = None
        self._signature = ""
        self._applied: dict[str, tuple[str, tuple[str, ...]]] | None = None
        self._saved_key, self._saved_rules = _load_state()
        self._enabled = False
        self._rules = 0
        self._applying = False
        self._error = ""
        self._last_ms = 0
        self._last_apply = 0.0
        self._retry_at = 0.0
        self._remove_requested = False

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "available": available(),
                "rules": self._rules,
                "applying": self._applying,
                "error": self._error,
                "last_ms": self._last_ms,
                "enabled": self._enabled,
            }

    def sync(
        self, exes: tuple[str, ...] | list[str], groups: dict[str, list[str]]
    ) -> None:
        exes = tuple(sorted({exe for exe in exes if exe}))
        cleaned: list[tuple[str, tuple[str, ...]]] = []
        for name, cidrs in sorted(groups.items()):
            values = tuple(sorted(set(cidrs)))
            if values:
                cleaned.append((name, values))
        groups = tuple(cleaned)
        groups = _chunk(groups)
        key = _signature_key(exes, groups)
        with self._lock:
            if (
                key == self._signature
                and self._pending is None
                and not self._remove_requested
            ):
                if not exes and not groups:
                    return
                if self._enabled:
                    return
                if time.monotonic() < self._retry_at:
                    return
            self._signature = key
            self._pending = (exes, groups, key)
        self._wake.set()

    def clear(self) -> None:
        """Delete every rule this app owns, for uninstall or a manual cleanup."""
        with self._lock:
            self._pending = None
            self._remove_requested = True
        self._wake.set()

    def close(self) -> None:
        # Leave the rules on disk but disabled. Rebuilding them next launch is
        # what took tens of seconds; enabling them is instant.
        if not self.is_alive():
            with self._lock:
                self._pending = None
            self._stop_event.set()
            return
        self.sync((), {})
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            with self._lock:
                idle = self._pending is None and not self._applying
            if idle and not self.is_alive():
                break
            if idle:
                break
            time.sleep(0.05)
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
                remove = self._remove_requested
                self._remove_requested = False
                pending = self._pending
                self._pending = None
            if remove:
                self._last_apply = time.monotonic()
                self._remove_rules()
                continue
            if pending is None or self._stop_event.is_set():
                continue
            self._last_apply = time.monotonic()
            self._apply(*pending)

    def _run_script(self, script: str) -> None:
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
            detail = (
                (result.stderr or result.stdout or "unknown error").strip().splitlines()
            )
            raise RuntimeError(detail[-1] if detail else "firewall command failed")

    def _desired(
        self, exes: tuple[str, ...], groups: Groups
    ) -> dict[str, tuple[str, tuple[str, ...]]]:
        desired: dict[str, tuple[str, tuple[str, ...]]] = {}
        for exe_index, exe in enumerate(exes):
            for name, cidrs in groups:
                desired[_rule_name(name, exe_index)] = (exe, cidrs)
        return desired

    def _enable_only(
        self, desired: dict[str, tuple[str, tuple[str, ...]]], key: str
    ) -> bool:
        """Enable an existing rule set without rewriting its addresses."""
        lines = ["$ErrorActionPreference = 'Stop'"]
        for name in desired:
            lines.append(f"Enable-NetFirewallRule -DisplayName {_ps_quote(name)}")
        try:
            self._run_script("\n".join(lines))
        except Exception as exc:
            self.log(f"firewall enable failed: {type(exc).__name__}: {exc}")
            return False
        self._applied = dict(desired)
        self._enabled = True
        self._retry_at = 0.0
        self._rules = len(desired)
        self._saved_key = key
        self._saved_rules = len(desired)
        _save_state(key, len(desired))
        return True

    def _apply(self, exes: tuple[str, ...], groups: Groups, key: str) -> None:
        started = time.monotonic()
        with self._lock:
            self._applying = True
            self._error = ""
        desired = self._desired(exes, groups)
        logged = False
        try:
            if not desired:
                self._suspend()
                return
            # A launch after a clean exit can reuse the stored rule set. If the
            # rules were removed behind our back, the enable call fails and the
            # full rewrite below runs instead.
            if (
                self._applied is None
                and key == self._saved_key
                and self._saved_rules == len(desired)
                and self._enable_only(desired, key)
            ):
                elapsed = int((time.monotonic() - started) * 1000)
                with self._lock:
                    self._rules = len(desired)
                    self._last_ms = elapsed
                self.log(f"firewall: {len(desired)} rules enabled in {elapsed}ms")
                logged = True
                return

            changed = {
                name: value
                for name, value in desired.items()
                if self._applied is None or self._applied.get(name) != value
            }
            if self._applied is not None and not changed:
                if self._enable_only(desired, key):
                    elapsed = int((time.monotonic() - started) * 1000)
                    with self._lock:
                        self._last_ms = elapsed
                    self.log(
                        f"firewall: {len(desired)} rules re-enabled in {elapsed}ms"
                    )
                    logged = True
                    return

            lines = [
                "$ErrorActionPreference = 'Stop'",
                "$keep = @()",
            ]
            for name in desired:
                lines.append(f"$keep += {_ps_quote(name)}")
            for rule, (exe, cidrs) in changed.items():
                addresses = ",".join(_ps_quote(cidr) for cidr in cidrs)
                lines.extend(
                    [
                        f"$name = {_ps_quote(rule)}",
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
            # Enable every desired rule. Rules that were only disabled by a pause
            # are not in $changed but still need to come back on.
            lines.append(
                "foreach ($n in $keep) { Enable-NetFirewallRule -DisplayName $n }"
            )
            # Only remove rules that are no longer wanted, and only after the
            # new ones exist, so there is never a window with no block rules.
            lines.append(
                "Get-NetFirewallRule -DisplayName 'COD Ping King Block *' "
                "-ErrorAction SilentlyContinue | "
                "Where-Object { $keep -notcontains $_.DisplayName } | "
                "Remove-NetFirewallRule"
            )
            self._run_script("\n".join(lines))
            self._applied = dict(desired)
            self._enabled = True
            self._retry_at = 0.0
            self._rules = len(desired)
            self._saved_key = key
            self._saved_rules = len(desired)
            _save_state(key, len(desired))
        except Exception as exc:
            self._applied = None
            # A failed build can take minutes. Retrying every sync turns that
            # into a loop of PowerShell processes, so wait before trying again.
            self._retry_at = time.monotonic() + 60.0
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            self.log(f"firewall failed: {type(exc).__name__}: {exc}")
        finally:
            elapsed = int((time.monotonic() - started) * 1000)
            with self._lock:
                self._rules = len(desired) if self._applied is not None else 0
                self._applying = False
                self._last_ms = elapsed
            if self._applied is not None and desired and not logged:
                self.log(
                    f"firewall: {len(desired)} block rules, "
                    f"{sum(len(cidrs) for _, cidrs in groups)} ranges in {elapsed}ms"
                )

    def _suspend(self) -> None:
        started = time.monotonic()
        script = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "Disable-NetFirewallRule -DisplayName 'COD Ping King Block *'\n"
        )
        try:
            self._run_script(script)
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            self.log(f"firewall suspend failed: {type(exc).__name__}: {exc}")
            return
        with self._lock:
            self._enabled = False
            self._rules = 0
            self._last_ms = int((time.monotonic() - started) * 1000)
        self.log("firewall: rules suspended")

    def _remove_rules(self) -> None:
        script = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "Get-NetFirewallRule -DisplayName 'COD Ping King Block *' "
            "| Remove-NetFirewallRule\n"
        )
        try:
            self._run_script(script)
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            self.log(f"firewall remove failed: {type(exc).__name__}: {exc}")
            return
        with self._lock:
            self._applied = {}
            self._enabled = False
            self._rules = 0
            self._saved_key = ""
            self._saved_rules = 0
        _save_state("", 0)
        self.log("firewall: rules removed")
