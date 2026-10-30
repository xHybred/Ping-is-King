"""Autostart through Task Scheduler.

The HKCU Run key cannot start this app. CODPingKing.exe is manifest-elevated,
and Explorer's startup path cannot show a UAC prompt, so Windows refuses the
launch with error 740 and the process never appears. A logon task with highest
privileges starts it elevated without a prompt, which is what a background
packet filter needs.
"""

from __future__ import annotations

import subprocess
import sys

TASK_NAME = "COD Ping King"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUES = ("CODPingKing", "CodRegionFilter")

_NO_WINDOW = 0x08000000


def command() -> str:
    """The command the task runs, quoted for Task Scheduler's /TR."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    return f'"{sys.executable}" -m cod_region_filter --minimized'


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "mbcs"):
        try:
            return data.decode(encoding, errors="replace").strip()
        except LookupError:
            continue
    return data.decode("latin-1", errors="replace").strip()


def _schtasks(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["schtasks", *args],
            capture_output=True,
            creationflags=_NO_WINDOW if sys.platform == "win32" else 0,
            timeout=25,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, _decode((proc.stdout or b"") + (proc.stderr or b""))


def clear_run_entries() -> None:
    """Remove autostart values written by builds that predate the logon task."""
    if sys.platform != "win32":
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            for name in RUN_VALUES:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    except OSError:
        pass


def enabled() -> bool:
    if sys.platform != "win32":
        return False
    code, _ = _schtasks("/Query", "/TN", TASK_NAME)
    return code == 0


def enable() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Startup is only supported on Windows."
    code, output = _schtasks(
        "/Create",
        "/TN",
        TASK_NAME,
        "/TR",
        command(),
        "/SC",
        "ONLOGON",
        "/RL",
        "HIGHEST",
        "/F",
    )
    if code != 0:
        detail = output.splitlines()[-1] if output else ""
        return False, detail or "Windows did not accept the startup task."
    clear_run_entries()
    return True, ""


def disable() -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Startup is only supported on Windows."
    code, output = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    clear_run_entries()
    if code != 0 and enabled():
        return False, output or "Windows did not remove the startup task."
    return True, ""


def sync() -> None:
    """Re-create the task if the setting survived an upgrade without it."""
    if sys.platform != "win32":
        return
    if not enabled():
        enable()
    clear_run_entries()
