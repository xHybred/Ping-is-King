import pytest


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    from cod_region_filter import state as state_mod

    monkeypatch.setattr(state_mod.services, "resolve_services", lambda: ["20.1.2.3"])
    app = state_mod.AppState()
    try:
        yield app
    finally:
        app.shutdown()


def _match(ip: str, cidr: str) -> dict:
    return {
        "remote_ip": ip,
        "cidr": cidr,
        "city": "Cheyenne",
        "region": "Wyoming",
        "country": "United States",
        "started_at": "2026-10-30T05:00:00+00:00",
        "ended_at": "2026-10-30T05:10:00+00:00",
        "duration_s": 600,
        "bytes": 1000,
        "rtt_ms": 53.0,
        "allowed": True,
        "reason": "Allowed by region",
    }


def test_service_endpoint_cannot_be_blocked(state):
    result = state.block_server("20.1.2.3")
    assert result["ok"] is False
    assert "service" in result["error"].lower()


def test_match_server_can_be_blocked_and_unblocked(state):
    blocked = state.block_server("45.63.1.10")
    assert blocked["ok"] is True
    assert blocked["cidr"] == "45.63.1.0/24"
    assert state.policy_engine.policy.deny.cidrs == ["45.63.1.0/24"]

    cleared = state.block_server("45.63.1.10", False)
    assert cleared["ok"] is True
    assert state.policy_engine.policy.deny.cidrs == []


def test_recommended_policy_denies_unclassified_endpoints(state):
    state.set_home(40.6171, -80.587)
    result = state.apply_recommended(40)
    assert result["ok"] is True
    assert state.policy_engine.policy.mode == "allowlist"
    assert state.policy_engine.policy.unknown == "deny"
    assert state.policy_engine.policy.allow.regions


def test_export_policy_writes_yaml_and_respects_streamer(state, tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Documents").mkdir()

    exported = state.export_policy()
    assert exported["ok"] is True
    text = Path(exported["path"]).read_text(encoding="utf-8")
    assert "allow:" in text

    state.set_setting("streamer_mode", True)
    blocked = state.export_policy()
    assert blocked["ok"] is False
    assert "streamer" in blocked["error"].lower()


def test_service_matches_are_not_listed(state):
    state.catalog.record_match(_match("20.1.2.3", "20.1.2.0/24"))
    assert state.matches_payload() == []


def test_streamer_mode_hides_identifying_details(state):
    from cod_region_filter.api import Api

    state.set_home(40.6171, -80.587)
    state.catalog.record_match(_match("45.63.1.10", "45.63.1.0/24"))
    state.set_setting("streamer_mode", True)
    base = 1000.0
    for i in range(80):
        state.engine.observe("45.63.1.10", 3074, "outbound", 200, now=base + i * 0.1)
    state.engine.tick(base + 8.0)

    boot = Api(state).bootstrap()
    assert boot["streamer"] is True
    assert boot["state"]["current"]["remote_ip"] == "Hidden"
    assert boot["state"]["current"]["cidr"] == "Hidden range"
    assert boot["policy_text"] == ""
    assert boot["policy_home"]["hidden"] is True
    assert boot["policy_home"]["lat"] is None
    assert boot["paths"]["policy"].startswith("%APPDATA%")
    assert boot["matches"][0]["remote_ip"] == "Hidden"
    assert boot["matches"][0]["cidr"] == "Hidden range"
    assert all(server["cidr"] == "Hidden range" for server in boot["servers"])

    state.set_setting("streamer_mode", False)
    visible = Api(state).bootstrap()
    assert visible["policy_text"] != ""
    assert visible["policy_home"]["lat"] == pytest.approx(40.6171)
    assert visible["matches"][0]["cidr"] == "45.63.1.0/24"
