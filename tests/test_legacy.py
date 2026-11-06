import subprocess

from cod_region_filter import legacy


def test_unload_driver_stops_and_deletes_every_service(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(legacy.subprocess, "run", fake_run)
    monkeypatch.setattr(legacy.time, "sleep", lambda _s: None)

    assert legacy.unload_driver() is True
    assert [args for args, _ in calls] == [
        ["sc.exe", "stop", "WinDivert"],
        ["sc.exe", "delete", "WinDivert"],
        ["sc.exe", "stop", "WinDivert1.4"],
        ["sc.exe", "delete", "WinDivert1.4"],
    ]
    assert calls[0][1]["creationflags"] == 0x08000000


def test_unload_driver_reports_a_service_that_survives(monkeypatch):
    logged = []

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 5, "Access is denied.", "")

    monkeypatch.setattr(legacy.subprocess, "run", fake_run)
    monkeypatch.setattr(legacy.time, "sleep", lambda _s: None)

    assert legacy.unload_driver(logged.append) is False
    assert any("Access is denied." in line for line in logged)
