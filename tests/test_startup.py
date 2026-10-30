"""Autostart is a Task Scheduler entry, because the Run key cannot launch an
elevated exe (Windows drops it with error 740)."""

from __future__ import annotations

from types import SimpleNamespace

from cod_region_filter import startup


def _capture(monkeypatch, returncode=0, output=b""):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout=output, stderr=b"")

    monkeypatch.setattr(startup.subprocess, "run", fake_run)
    monkeypatch.setattr(startup, "clear_run_entries", lambda: None)
    return calls


def test_enable_creates_highest_privilege_logon_task(monkeypatch):
    calls = _capture(monkeypatch)

    ok, error = startup.enable()

    assert ok and error == ""
    cmd = calls[0]
    assert cmd[0] == "schtasks"
    assert "/Create" in cmd and "/F" in cmd
    assert "/SC" in cmd and "ONLOGON" in cmd
    assert "/RL" in cmd and "HIGHEST" in cmd
    assert cmd[cmd.index("/TN") + 1] == startup.TASK_NAME
    assert "--minimized" in cmd[cmd.index("/TR") + 1]


def test_enable_reports_windows_error(monkeypatch):
    _capture(monkeypatch, returncode=1, output=b"ERROR: Access is denied.\n")

    ok, error = startup.enable()

    assert not ok
    assert "Access is denied" in error


def test_disable_treats_missing_task_as_success(monkeypatch):
    _capture(monkeypatch, returncode=1, output=b"ERROR: The system cannot find the file specified.\n")
    monkeypatch.setattr(startup, "enabled", lambda: False)

    ok, _ = startup.disable()

    assert ok


def test_disable_reports_task_that_survives(monkeypatch):
    _capture(monkeypatch, returncode=1, output=b"ERROR: Access is denied.\n")
    monkeypatch.setattr(startup, "enabled", lambda: True)

    ok, error = startup.disable()

    assert not ok
    assert "Access is denied" in error


def test_sync_creates_missing_task(monkeypatch):
    created = []
    monkeypatch.setattr(startup, "enabled", lambda: False)
    monkeypatch.setattr(startup, "enable", lambda: created.append(True) or (True, ""))
    monkeypatch.setattr(startup, "clear_run_entries", lambda: None)

    startup.sync()

    assert created == [True]
