import subprocess
import threading
import time
import types

from cod_region_filter import firewall


def _new_firewall(monkeypatch):
    monkeypatch.setattr(firewall, "_load_state", lambda: ("", 0))
    monkeypatch.setattr(firewall, "_save_state", lambda key, rules: None)
    return firewall.Firewall()


def test_rule_writer_never_shows_a_console(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(firewall.subprocess, "run", fake_run)
    fw = _new_firewall(monkeypatch)
    exes = ("C:\\game\\cod.exe",)
    groups = (("US-NJ", ("203.0.113.0/24",)),)
    fw._apply(exes, groups, firewall._signature_key(exes, groups))

    assert calls
    kwargs = calls[0][1]
    assert kwargs["creationflags"] & 0x08000000
    assert kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE
    assert firewall._rule_name("US-NJ", 0) == "COD Ping King Block US-NJ 0"


def test_rapid_changes_collapse_into_one_apply(monkeypatch):
    fw = _new_firewall(monkeypatch)
    applied = []
    done = threading.Event()

    def fake_apply(exes, groups, key):
        applied.append(groups)
        done.set()

    fw._apply = fake_apply
    fw.start()
    try:
        fw.sync(("C:\\game\\cod.exe",), {"A": ["198.51.100.0/24"]})
        fw.sync(("C:\\game\\cod.exe",), {"B": ["203.0.113.0/24"]})
        fw.sync(("C:\\game\\cod.exe",), {"C": ["192.0.2.0/24"]})
        assert done.wait(3.0)
        assert applied == [(("Set 1", ("192.0.2.0/24",)),)]
    finally:
        fw._stop_event.set()
        fw._wake.set()
        fw.join(timeout=2.0)


def test_only_changed_regions_are_rewritten(monkeypatch):
    scripts = []
    fw = _new_firewall(monkeypatch)
    monkeypatch.setattr(fw, "_run_script", lambda script: scripts.append(script))
    exes = ("C:\\game\\cod.exe",)
    first = (("US-NJ", ("203.0.113.0/24",)), ("US-OH", ("198.51.100.0/24",)))
    fw._apply(exes, first, firewall._signature_key(exes, first))
    scripts.clear()

    second = (("US-NJ", ("203.0.113.0/24",)), ("US-OH", ("192.0.2.0/24",)))
    fw._apply(exes, second, firewall._signature_key(exes, second))

    assert len(scripts) == 1
    assert "$name = 'COD Ping King Block US-OH 0'" in scripts[0]
    assert "$name = 'COD Ping King Block US-NJ 0'" not in scripts[0]
    assert "$keep += 'COD Ping King Block US-NJ 0'" in scripts[0]


def test_catalog_ranges_are_split_into_few_large_rules():
    groups = (
        ("US-NJ", tuple(f"203.0.{i}.0/24" for i in range(1200))),
        ("US-OH", tuple(f"198.51.{i}.0/24" for i in range(1300))),
    )

    chunks = firewall._chunk(groups)

    assert len(chunks) == 3
    assert all(len(cidrs) <= firewall.CHUNK for _, cidrs in chunks)
    assert all(name.startswith("Set ") for name, _ in chunks)


def test_suspend_disables_and_clear_removes(monkeypatch):
    scripts = []
    fw = _new_firewall(monkeypatch)
    monkeypatch.setattr(fw, "_run_script", lambda script: scripts.append(script))

    fw._apply((), (), "")
    assert "Disable-NetFirewallRule" in scripts[-1]

    fw._remove_rules()
    assert "Remove-NetFirewallRule" in scripts[-1]


def test_a_saved_rule_set_is_enabled_without_rewriting(monkeypatch):
    exes = ("C:\\game\\cod.exe",)
    groups = (("US-NJ", ("203.0.113.0/24",)),)
    key = firewall._signature_key(exes, groups)
    monkeypatch.setattr(firewall, "_load_state", lambda: (key, 1))
    monkeypatch.setattr(firewall, "_save_state", lambda key, rules: None)
    scripts = []
    fw = firewall.Firewall()
    monkeypatch.setattr(fw, "_run_script", lambda script: scripts.append(script))

    fw._apply(exes, groups, key)

    assert len(scripts) == 1
    assert "Enable-NetFirewallRule" in scripts[0]
    assert "New-NetFirewallRule" not in scripts[0]
    assert fw.status["rules"] == 1


def test_failed_apply_waits_a_minute_before_retrying(monkeypatch):
    fw = _new_firewall(monkeypatch)

    def boom(script):
        raise subprocess.TimeoutExpired("powershell.exe", 180)

    monkeypatch.setattr(fw, "_run_script", boom)
    fw._apply(("C:\\game\\cod.exe",), (("Set 1", ("203.0.113.0/24",)),), "key")

    assert "TimeoutExpired" in fw.status["error"]
    assert fw._retry_at > time.monotonic() + 50
