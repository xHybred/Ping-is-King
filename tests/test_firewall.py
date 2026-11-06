import subprocess
import threading
import types

from cod_region_filter import firewall


def test_rule_writer_never_shows_a_console(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(firewall.subprocess, "run", fake_run)
    fw = firewall.Firewall()
    fw._apply(("C:\\game\\cod.exe",), ("203.0.113.0/24",))

    assert calls
    kwargs = calls[0][1]
    assert kwargs["creationflags"] & 0x08000000
    assert kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE


def test_rapid_changes_collapse_into_one_apply():
    fw = firewall.Firewall()
    applied = []
    done = threading.Event()

    def fake_apply(exes, cidrs):
        applied.append(cidrs)
        done.set()

    fw._apply = fake_apply
    fw.start()
    try:
        fw.sync(("C:\\game\\cod.exe",), ("198.51.100.0/24",))
        fw.sync(("C:\\game\\cod.exe",), ("203.0.113.0/24",))
        fw.sync(("C:\\game\\cod.exe",), ("192.0.2.0/24",))
        assert done.wait(3.0)
        assert applied == [("192.0.2.0/24",)]
    finally:
        fw._stop_event.set()
        fw._wake.set()
        fw.join(timeout=2.0)
