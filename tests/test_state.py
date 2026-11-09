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


def test_remove_firewall_rules_requires_paused_filtering(state, monkeypatch):
    cleared = []
    monkeypatch.setattr(state.firewall, "clear", lambda: cleared.append(True))

    state.engine.set_filtering(True)
    blocked = state.remove_firewall_rules()
    assert blocked["ok"] is False
    assert cleared == []

    state.engine.set_filtering(False)
    assert state.remove_firewall_rules()["ok"] is True
    assert cleared == [True]


def test_manual_deny_ranges_are_sent_to_the_firewall(state):
    state.catalog.observe("45.63.1.10", 400)
    state.catalog.flush()
    state._clusters = []

    result = state.block_server("45.63.1.10")

    assert result["ok"] is True
    assert state._firewall_groups() == {"Manual": ["45.63.1.0/24"]}


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


def test_export_discovered_only_includes_observed(state, tmp_path, monkeypatch):
    import json
    from pathlib import Path

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Documents").mkdir()
    state.catalog.observe("45.63.9.9", 1200, 30.0)
    state.catalog.flush()

    exported = state.export_discovered()
    assert exported["ok"] is True
    payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
    assert payload["format"] == "cod-ping-king-discovered"
    assert payload["count"] == len(payload["ranges"]) == 1
    assert payload["ranges"][0]["cidr"] == "45.63.9.0/24"

    state.set_setting("streamer_mode", True)
    blocked = state.export_discovered()
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
        state.engine.observe("45.63.1.10", 30000, "outbound", 200, now=base + i * 0.1)
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


def test_pausing_filtering_clears_firewall(state, monkeypatch):
    calls = []
    monkeypatch.setattr(state.firewall, "sync", lambda exes, groups: calls.append((exes, groups)))
    state.engine.set_filtering(True)

    state.set_setting("filtering", False)

    assert calls == [((), {})]
    assert state.engine.filtering is False


def test_installer_failure_code_is_reported(state, monkeypatch):
    from cod_region_filter import state as state_mod

    monkeypatch.setattr(state_mod.updates, "read_result", lambda: {"exit_code": 5})

    state._read_update_result()

    assert "code 5" in state._update_payload()["error"]


def test_theme_names_accept_legacy_values(state):
    assert state.set_theme("dark-coffee")["ok"] is True
    assert state.config["theme"] == "ember"
    assert state.set_theme("onyx")["ok"] is True
    assert state.config["theme"] == "onyx"
    assert state.set_theme("dark")["ok"] is False


def test_provider_rule_moves_between_lists_and_normalises(state):
    assert state.set_provider_rule("AS20473", "allow")["ok"] is True
    assert state.policy_engine.policy.allow.providers == ["Vultr"]
    assert state.set_provider_rule("Vultr", "deny")["ok"] is True
    assert state.policy_engine.policy.allow.providers == []
    assert state.policy_engine.policy.deny.providers == ["Vultr"]
    assert state.set_provider_rule("Vultr", "none")["ok"] is True
    assert state.policy_engine.policy.deny.providers == []
    assert state.set_provider_rule("Vultr", "bogus")["ok"] is False
