"""GitHub release checks, verified downloads and silent installer launch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

import requests

REPO = "xHybred/Ping-is-King"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
USER_AGENT = "COD-Ping-King-Updater"
TIMEOUT = (10, 60)
CHUNK = 1024 * 256


class UpdateError(RuntimeError):
    """A failure that can be shown to the user as-is."""


def version_tuple(text: str) -> tuple[int, ...]:
    parts = [int(part) for part in re.findall(r"\d+", text or "")]
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    return version_tuple(latest) > version_tuple(current)


def parse_digest(asset: dict) -> str:
    digest = str(asset.get("digest") or "")
    if digest.lower().startswith("sha256:"):
        return digest.split(":", 1)[1].strip().lower()
    return ""


def pick_installer(assets: list[dict], version: str) -> dict | None:
    wanted = f"cod-ping-king-{version}.exe".lower()
    candidates = [
        asset
        for asset in assets
        if str(asset.get("name", "")).lower().endswith(".exe")
        and not str(asset.get("name", "")).lower().endswith(".sha256.exe")
    ]
    if not candidates:
        return None
    for asset in candidates:
        if str(asset.get("name", "")).lower() == wanted:
            return asset
    for asset in candidates:
        if "cod-ping-king" in str(asset.get("name", "")).lower():
            return asset
    return candidates[0]


def latest_release(session: requests.Session | None = None) -> dict:
    """Return the newest public release, or raise UpdateError."""
    http = session or requests
    try:
        response = http.get(
            API_URL,
            timeout=TIMEOUT,
            headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        raise UpdateError(f"Could not reach GitHub: {exc}") from exc
    if response.status_code == 404:
        raise UpdateError("No releases are published yet.")
    if response.status_code == 403:
        raise UpdateError("GitHub rate limit reached. Try again later.")
    if response.status_code >= 400:
        raise UpdateError(f"GitHub returned {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise UpdateError("GitHub returned an unreadable response.") from exc
    tag = str(data.get("tag_name") or "")
    if not tag:
        raise UpdateError("The latest release has no version tag.")
    asset = pick_installer(list(data.get("assets") or []), tag.lstrip("vV"))
    if asset is None:
        raise UpdateError("The latest release has no installer attached.")
    return {
        "version": tag.lstrip("vV"),
        "tag": tag,
        "page": str(data.get("html_url") or ""),
        "notes": str(data.get("body") or ""),
        "url": str(asset.get("browser_download_url") or ""),
        "name": str(asset.get("name") or ""),
        "size": int(asset.get("size") or 0),
        "sha256": parse_digest(asset),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _looks_like_installer(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"MZ"
    except OSError:
        return False


def download(
    url: str,
    target: Path,
    expected_sha256: str = "",
    expected_size: int = 0,
    on_progress: Callable[[int, int], None] | None = None,
    session: requests.Session | None = None,
) -> Path:
    """Download to target.part, verify, then rename. Never leaves a partial exe."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")
    for stale in (part, target):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    http = session or requests
    digest = hashlib.sha256()
    written = 0
    try:
        with http.get(url, stream=True, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}) as response:
            if response.status_code >= 400:
                raise UpdateError(f"Download failed with HTTP {response.status_code}.")
            total = int(response.headers.get("Content-Length") or expected_size or 0)
            with part.open("wb") as handle:
                for block in response.iter_content(CHUNK):
                    if not block:
                        continue
                    handle.write(block)
                    digest.update(block)
                    written += len(block)
                    if on_progress:
                        on_progress(written, total)
        if written == 0:
            raise UpdateError("The downloaded installer was empty.")
        if expected_size and written != expected_size:
            raise UpdateError("The download stopped early. Try again.")
        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256.lower():
            raise UpdateError("The downloaded installer did not match its checksum.")
        if not _looks_like_installer(part):
            raise UpdateError("The downloaded file is not a Windows installer.")
        part.replace(target)
        return target
    except Exception:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def result_path() -> Path:
    from . import paths

    return paths.local_data_dir() / "update-result.json"


def read_result() -> dict:
    path = result_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    try:
        path.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else {}


def update_dir() -> Path:
    from . import paths

    return paths.local_data_dir() / "updates"


def installer_path(version: str) -> Path:
    safe = re.sub(r"[^0-9A-Za-z._-]", "", version) or "latest"
    return update_dir() / f"COD-Ping-King-{safe}.exe"


def _hidden_start() -> tuple[int, subprocess.STARTUPINFO | None]:
    if os.name != "nt":
        return 0, None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return 0x08000000, info


def launch_installer(
    installer: Path, exe: Path, pid: int, version: str, relaunch_args: list[str] | None = None
) -> None:
    """Spawn a hidden helper that waits for this process, installs, then relaunches."""
    if not installer.is_file():
        raise UpdateError("The installer is missing. Check for updates again.")
    if not _looks_like_installer(installer):
        raise UpdateError("The downloaded installer is invalid.")
    script = update_dir() / "apply-update.ps1"
    result = result_path()

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    extra = "".join(quote(arg) + "," for arg in (relaunch_args or []))
    arg_list = f"@({extra})" if extra else "@()"

    body = f"""$ErrorActionPreference = 'SilentlyContinue'
Wait-Process -Id {pid} -Timeout 120
Start-Sleep -Milliseconds 800
$p = Start-Process -FilePath {quote(str(installer))} -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS') -Wait -PassThru
$code = 0
if ($p) {{ $code = $p.ExitCode }}
@{{ exit_code = $code; version = {quote(version)}; at = (Get-Date).ToString('o') }} | ConvertTo-Json | Set-Content -Path {quote(str(result))} -Encoding UTF8
Start-Sleep -Milliseconds 1000
$exe = {quote(str(exe))}
if (-not (Test-Path $exe)) {{
  $fallback = Join-Path $env:ProgramFiles 'Hybred Tools\\COD PiK\\CODPingKing.exe'
  if (Test-Path $fallback) {{ $exe = $fallback }}
}}
if (Test-Path $exe) {{ Start-Process -FilePath $exe{(' -ArgumentList ' + arg_list) if extra else ''} }}
"""
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    flags, startup = _hidden_start()
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ],
        creationflags=flags,
        startupinfo=startup,
        close_fds=True,
    )
    # The helper waits for this PID, so give it a moment to start before exit.
    time.sleep(0.2)
