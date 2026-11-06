"""Cleanup for the WinDivert driver that 1.x used.

A loaded WinDivert driver makes current Call of Duty close on launch, so an
upgrade has to remove it even though nothing in this version opens a handle.
"""

from __future__ import annotations

import os
import subprocess
import time

DRIVER_SERVICES = ("WinDivert", "WinDivert1.4")


def unload_driver(log=None) -> bool:
    """Stop and delete the old WinDivert service, if it exists."""
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
