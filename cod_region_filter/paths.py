"""Filesystem locations.

Everything writable lives under %LOCALAPPDATA% / %APPDATA% so the app works
when installed under Program Files and survives upgrades.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DIR_NAME = "cod-ping-king"
LEGACY_APP_DIR_NAME = "cod-region-filter"


def _base(env: str, fallback: Path) -> Path:
    raw = os.environ.get(env)
    return Path(raw) if raw else fallback


def migrate_legacy() -> None:
    """Move data from the pre-rename folder so upgrades keep policy and history."""
    for env, fallback in (
        ("LOCALAPPDATA", Path.home() / "AppData" / "Local"),
        ("APPDATA", Path.home() / "AppData" / "Roaming"),
    ):
        new = _base(env, fallback) / APP_DIR_NAME
        old = _base(env, fallback) / LEGACY_APP_DIR_NAME
        if new.exists() or not old.exists():
            continue
        try:
            shutil.move(str(old), str(new))
        except OSError:
            # A file may be locked by an old copy still running; a copy is
            # enough, the legacy folder is cleaned up by the installer.
            try:
                shutil.copytree(old, new)
            except OSError:
                pass


def local_data_dir() -> Path:
    return _base("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_DIR_NAME


def roaming_dir() -> Path:
    return _base("APPDATA", Path.home() / "AppData" / "Roaming") / APP_DIR_NAME


def db_path() -> Path:
    return local_data_dir() / "catalog.db"


def policy_path() -> Path:
    return roaming_dir() / "policy.yaml"


def config_path() -> Path:
    return roaming_dir() / "config.json"


def log_path() -> Path:
    return local_data_dir() / "app.log"


def resource_root() -> Path:
    """Directory holding bundled read-only data, frozen or from source."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def resource(name: str) -> Path:
    return resource_root() / name


def ui_dir() -> Path:
    return Path(__file__).resolve().parent / "ui"
