from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cod_region_filter import updates


class FakeResponse:
    def __init__(self, status=200, payload=None, body=b"", headers=None):
        self.status_code = status
        self._payload = payload
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload

    def iter_content(self, size):
        for index in range(0, len(self._body), size):
            yield self._body[index:index + size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_version_comparison_ignores_prefix_and_trailing_zeroes():
    assert updates.version_tuple("v2.1.0") == (2, 1)
    assert updates.is_newer("2.1.0", "2.0.4")
    assert not updates.is_newer("v2.0.4", "2.0.4")
    assert not updates.is_newer("2.0.3", "2.0.4")


def test_latest_release_picks_the_named_installer():
    body = ("MZ" + "0" * 32).encode()
    digest = hashlib.sha256(body).hexdigest()
    release = {
        "tag_name": "v2.1.0",
        "html_url": "https://example.invalid/release",
        "body": "notes",
        "assets": [
            {"name": "checksums.txt", "browser_download_url": "https://example.invalid/sums"},
            {
                "name": "COD-Ping-King-2.1.0.exe",
                "browser_download_url": "https://example.invalid/setup.exe",
                "size": len(body),
                "digest": "sha256:" + digest,
            },
        ],
    }
    session = FakeSession(FakeResponse(payload=release))
    found = updates.latest_release(session)
    assert found["version"] == "2.1.0"
    assert found["url"] == "https://example.invalid/setup.exe"
    assert found["sha256"] == digest
    assert found["size"] == len(body)


def test_latest_release_reports_missing_assets():
    session = FakeSession(FakeResponse(payload={"tag_name": "v2.1.0", "assets": []}))
    with pytest.raises(updates.UpdateError):
        updates.latest_release(session)


def test_download_verifies_checksum_and_keeps_the_file(tmp_path):
    body = b"MZ" + b"x" * 4096
    digest = hashlib.sha256(body).hexdigest()
    target = tmp_path / "setup.exe"
    seen = []
    session = FakeSession(FakeResponse(body=body, headers={"Content-Length": str(len(body))}))
    updates.download(
        "https://example.invalid/setup.exe",
        target,
        digest,
        len(body),
        lambda done, total: seen.append((done, total)),
        session,
    )
    assert target.read_bytes() == body
    assert seen[-1] == (len(body), len(body))
    assert not target.with_suffix(".exe.part").exists()


def test_download_rejects_a_bad_checksum_without_leaving_files(tmp_path):
    body = b"MZ" + b"x" * 128
    target = tmp_path / "setup.exe"
    session = FakeSession(FakeResponse(body=body))
    with pytest.raises(updates.UpdateError):
        updates.download("https://example.invalid/setup.exe", target, "0" * 64, 0, None, session)
    assert not target.exists()
    assert not target.with_suffix(".exe.part").exists()


def test_launch_installer_writes_a_hidden_silent_helper(tmp_path, monkeypatch):
    installer = tmp_path / "COD-Ping-King-2.1.0.exe"
    installer.write_bytes(b"MZ" + b"x" * 64)
    exe = tmp_path / "CODPingKing.exe"
    launched = {}
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: launched.update(args=a[0], kwargs=k))
    monkeypatch.setattr(updates.time, "sleep", lambda _s: None)
    monkeypatch.setattr(updates, "update_dir", lambda: tmp_path / "updates")
    monkeypatch.setattr(updates, "result_path", lambda: tmp_path / "result.json")

    updates.launch_installer(installer, exe, 1234, "2.1.0", ["-m", "cod_region_filter"])

    script = (tmp_path / "updates" / "apply-update.ps1").read_text(encoding="utf-8")
    assert "Wait-Process -Id 1234" in script
    assert "/VERYSILENT" in script
    assert "/SUPPRESSMSGBOXES" in script
    assert "-m" in script
    assert "Start-Process -FilePath $exe" in script
    assert launched["args"][0] == "powershell.exe"
    assert launched["kwargs"]["creationflags"] & 0x08000000
    assert launched["kwargs"]["startupinfo"].wShowWindow == updates.subprocess.SW_HIDE


def test_read_result_returns_and_removes_the_file(tmp_path, monkeypatch):
    path = tmp_path / "update-result.json"
    path.write_text(json.dumps({"exit_code": 0, "version": "2.1.0"}), encoding="utf-8")
    monkeypatch.setattr(updates, "result_path", lambda: path)
    assert updates.read_result()["exit_code"] == 0
    assert not path.exists()


def test_frozen_relaunch_does_not_pass_an_empty_argument_list(tmp_path, monkeypatch):
    installer = tmp_path / "COD-Ping-King-2.1.0.exe"
    installer.write_bytes(b"MZ" + b"x" * 64)
    exe = tmp_path / "CODPingKing.exe"
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(updates.time, "sleep", lambda _s: None)
    monkeypatch.setattr(updates, "update_dir", lambda: tmp_path / "updates")
    monkeypatch.setattr(updates, "result_path", lambda: tmp_path / "result.json")

    updates.launch_installer(installer, exe, 1234, "2.1.0")

    script = (tmp_path / "updates" / "apply-update.ps1").read_text(encoding="utf-8")
    assert "Start-Process -FilePath $exe }" in script
    assert "-ArgumentList @()" not in script
