from cod_region_filter.catalog import Catalog, cidr_for
from cod_region_filter.engine import MATCH_IDLE_END_S, MATCH_MIN_SECONDS, Engine
from cod_region_filter.policy import AllowSpec, Policy, PolicyEngine, SoftMode


def geo(city, region_code, lat, lon):
    return {
        "status": "success",
        "city": city,
        "regionName": city + " Metro",
        "region": region_code,
        "country": "United States",
        "countryCode": "US",
        "lat": lat,
        "lon": lon,
    }


def setup(tmp_path, policy=None):
    cat = Catalog(tmp_path / "catalog.db")
    for ip, g in [
        ("203.0.113.10", geo("Columbus", "OH", 39.96, -83.0)),
        ("198.51.100.10", geo("Los Angeles", "CA", 34.05, -118.24)),
    ]:
        cat.observe(ip, 100)
        cat.flush()
        cat.resolve_location(cidr_for(ip), g)
    policy = policy or Policy(allow=AllowSpec(regions=["US-OH"]))
    eng = Engine(cat, PolicyEngine(policy))
    eng.policy.set_clusters(cat.clusters())
    return cat, eng


def test_located_outside_allowlist_is_denied(tmp_path):
    cat, eng = setup(tmp_path)
    verdict = eng.observe("198.51.100.10", 3074, "outbound", 200, now=1000.0)
    assert verdict["allow"] is False
    cat.close()


def test_allowlisted_region_is_allowed(tmp_path):
    cat, eng = setup(tmp_path)
    verdict = eng.observe("203.0.113.10", 3074, "outbound", 200, now=1000.0)
    assert verdict["allow"] is True
    assert verdict["kind"] == "region"
    cat.close()


def test_filtering_off_allows_everything(tmp_path):
    cat, eng = setup(tmp_path)
    eng.set_filtering(False)
    verdict = eng.observe("198.51.100.10", 3074, "outbound", 200, now=1000.0)
    assert verdict == {"allow": True, "reason": "Filtering off", "kind": "off"}
    cat.close()


def test_rtt_estimated_from_outbound_inbound_gap(tmp_path):
    cat, eng = setup(tmp_path)
    eng.observe("203.0.113.10", 3074, "outbound", 200, now=1000.0)
    eng.observe("203.0.113.10", 3074, "inbound", 300, now=1000.022)
    flow = eng._flows[("203.0.113.10", 3074)]
    assert flow.rtt_ms() is not None
    assert 15 < flow.rtt_ms() < 30
    cat.close()


def test_match_detection_records_best_flow(tmp_path):
    cat, eng = setup(tmp_path)
    t = 1000.0
    for _ in range(45):
        eng.observe("203.0.113.10", 3074, "outbound", 200, now=t)
        eng.observe("203.0.113.10", 3074, "inbound", 400, now=t + 0.02)
        t += 0.1
    eng.tick(t + 0.6)
    assert eng.current_match() is not None
    assert eng.current_match()["city"] == "Columbus"
    eng.tick(t + MATCH_IDLE_END_S + 1)
    assert eng.current_match() is None
    assert len(cat.matches()) == 1
    assert cat.matches()[0]["city"] == "Columbus"
    cat.close()


def test_short_flow_is_not_a_match(tmp_path):
    cat, eng = setup(tmp_path)
    eng.observe("203.0.113.10", 3074, "outbound", 200, now=1000.0)
    eng.tick(1000.0 + MATCH_MIN_SECONDS)
    assert eng.current_match() is None
    cat.close()


def test_service_endpoint_never_becomes_match(tmp_path):
    cat, eng = setup(tmp_path)
    cat.mark_service(["203.0.113.10"])
    eng.policy.set_clusters(cat.clusters())
    t = 1000.0
    for _ in range(50):
        eng.observe("203.0.113.10", 3074, "outbound", 200, now=t)
        t += 0.1
    eng.tick(t)
    assert eng.current_match() is None
    cat.close()


def test_soft_mode_never_admits_located_denied_server(tmp_path):
    policy = Policy(
        allow=AllowSpec(regions=["US-OH"]),
        unknown="deny",
        soft_mode=SoftMode(enabled=True, relax_after_s=20),
    )
    cat, eng = setup(tmp_path, policy)
    eng._searching_since = 0.0
    verdict = eng.observe("198.51.100.10", 3074, "outbound", 200, now=25.0)
    assert verdict["allow"] is False
    assert verdict["kind"] == "allowlist"
    assert eng.soft_active is False
    cat.close()


def test_soft_mode_relaxes_after_timeout_for_unclassified(tmp_path):
    policy = Policy(
        allow=AllowSpec(regions=["US-OH"]),
        unknown="deny",
        soft_mode=SoftMode(enabled=True, relax_after_s=20),
    )
    cat, eng = setup(tmp_path, policy)
    eng._searching_since = 0.0
    verdict = eng.observe("192.0.2.55", 3074, "outbound", 200, now=25.0)
    assert verdict["allow"] is True
    assert verdict["kind"] == "soft"
    assert eng.soft_active is True
    cat.close()


def test_soft_mode_disabled_keeps_blocking(tmp_path):
    policy = Policy(
        allow=AllowSpec(regions=["US-OH"]),
        unknown="deny",
        soft_mode=SoftMode(enabled=False),
    )
    cat, eng = setup(tmp_path, policy)
    eng._searching_since = 0.0
    verdict = eng.observe("192.0.2.55", 3074, "outbound", 200, now=1000.0)
    assert verdict["allow"] is False
    cat.close()


def test_soft_pin_holds_unclassified_endpoint_for_match(tmp_path):
    policy = Policy(
        allow=AllowSpec(regions=["US-OH"]),
        unknown="deny",
        soft_mode=SoftMode(enabled=True, relax_after_s=20),
    )
    cat, eng = setup(tmp_path, policy)
    eng._searching_since = 0.0
    t = 25.0
    for _ in range(45):
        eng.observe("192.0.2.55", 3074, "outbound", 200, now=t)
        t += 0.1
    eng.tick(t + 0.6)
    assert eng.current_match() is not None
    assert eng.soft_active is False
    assert eng._soft_pinned_ip == "192.0.2.55"
    verdict = eng.observe("192.0.2.55", 3074, "outbound", 200, now=t + 0.1)
    assert verdict["kind"] == "soft"
    assert verdict["allow"] is True
    cat.close()


def test_state_reports_counters(tmp_path):
    cat, eng = setup(tmp_path)
    eng.observe("203.0.113.10", 3074, "outbound", 200, now=1000.0)
    eng.observe("198.51.100.10", 3074, "outbound", 200, now=1000.0)
    state = eng.state()
    assert state["filtering"] is True
    assert state["allowed_packets"] == 1
    assert state["denied_packets"] == 1
    assert len(state["recent"]) == 1
    cat.close()
