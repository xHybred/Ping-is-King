from cod_region_filter.catalog import Catalog, cidr_for
from cod_region_filter.engine import MATCH_IDLE_END_S, MATCH_MIN_SECONDS, Engine
from cod_region_filter.policy import AllowSpec, DenySpec, Policy, PolicyEngine, SoftMode


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
    cat, eng = setup(tmp_path, Policy(allow=AllowSpec(regions=["US-OH"]), strict=True))
    verdict = eng.observe("198.51.100.10", 3074, "outbound", 200, now=1000.0)
    assert verdict["allow"] is False
    cat.close()


def test_unclassified_endpoint_waits_for_a_session(tmp_path):
    policy = Policy(allow=AllowSpec(regions=["US-OH"]), unknown="deny")
    cat, eng = setup(tmp_path, policy)
    t = 1000.0
    verdict = eng.observe("192.0.2.55", 30000, "outbound", 200, now=t)
    assert verdict["allow"] is True
    assert verdict["kind"] == "grace"
    for _ in range(5):
        t += 0.1
        eng.observe("192.0.2.55", 30000, "outbound", 200, now=t)
    verdict = eng.observe("192.0.2.55", 30000, "outbound", 200, now=t + 0.1)
    assert verdict["allow"] is True
    for _ in range(6):
        t += 0.1
        verdict = eng.observe("192.0.2.55", 30000, "outbound", 200, now=t)
    assert verdict["allow"] is False
    assert verdict["kind"] == "unknown"
    cat.close()


def test_located_endpoint_is_dropped_on_the_first_packet(tmp_path):
    cat, eng = setup(tmp_path)
    verdict = eng.observe("198.51.100.10", 30000, "outbound", 200, now=1000.0)
    assert verdict["allow"] is False
    assert verdict["kind"] == "allowlist"
    cat.close()


def test_idle_gap_restarts_the_search_guard(tmp_path):
    policy = Policy(
        allow=AllowSpec(regions=["US-OH"]),
        unknown="deny",
        soft_mode=SoftMode(enabled=True, relax_after_s=45),
    )
    cat, eng = setup(tmp_path, policy)
    eng._searching_since = 1000.0
    eng.observe("192.0.2.55", 30000, "outbound", 200, now=1000.0)
    verdict = eng.observe("192.0.2.55", 30000, "outbound", 200, now=1060.0)
    assert verdict["kind"] == "soft"
    assert eng.state()["soft_active"] is True
    verdict = eng.observe("192.0.2.55", 30000, "outbound", 200, now=1200.0)
    assert eng.state()["soft_active"] is False
    assert verdict["kind"] == "grace"
    cat.close()


def test_platform_port_traffic_is_never_dropped_while_idle(tmp_path):
    cat, eng = setup(tmp_path)
    verdict = eng.observe("198.51.100.10", 3074, "outbound", 60, now=1000.0)
    assert verdict == {"allow": True, "reason": "Platform traffic", "kind": "platform"}
    cat.close()


def test_busy_platform_flow_is_not_a_match_and_stays_allowed(tmp_path):
    policy = Policy(allow=AllowSpec(regions=["US-OH"]), unknown="deny")
    cat, eng = setup(tmp_path, policy)
    t = 1000.0
    for _ in range(40):
        verdict = eng.observe("198.51.100.10", 3074, "outbound", 200, now=t)
        t += 0.1
    assert verdict["kind"] == "platform"
    eng.tick(now=t)
    assert eng.current_match() is None
    cat.close()


def test_known_server_range_is_dropped_on_the_first_probe(tmp_path):
    cat = Catalog(tmp_path / "known.db")
    cat.import_geo(
        [{
            "cidr": "198.51.100.0/24",
            "city": "Los Angeles",
            "region": "California",
            "region_code": "CA",
            "country": "United States",
            "country_code": "US",
            "lat": 34.05,
            "lon": -118.24,
        }],
        "community_geo",
        0.6,
    )
    eng = Engine(cat, PolicyEngine(Policy(allow=AllowSpec(regions=["US-OH"]))))
    eng.policy.set_clusters(cat.clusters())
    # Steering happens at the probe: a listed datacenter never answers, so the
    # game cannot select it and then get dropped out of a live match.
    verdict = eng.observe("198.51.100.10", 30000, "outbound", 60, now=1000.0)
    assert verdict["allow"] is False
    assert verdict["kind"] == "allowlist"
    cat.close()


def test_known_server_heartbeat_on_3074_is_control_traffic(tmp_path):
    cat = Catalog(tmp_path / "known3074.db")
    cat.import_geo(
        [{
            "cidr": "198.51.100.0/24",
            "city": "Los Angeles",
            "region": "California",
            "region_code": "CA",
            "country": "United States",
            "country_code": "US",
            "lat": 34.05,
            "lon": -118.24,
        }],
        "community_geo",
        0.6,
    )
    eng = Engine(cat, PolicyEngine(Policy(allow=AllowSpec(regions=["US-OH"]))))
    eng.policy.set_clusters(cat.clusters())
    # The recurring 3074 blocks in support logs were one heartbeat a minute,
    # not a match. Dropping it just disturbs the lobby.
    verdict = eng.observe("198.51.100.10", 3074, "outbound", 60, now=1000.0)
    assert verdict == {"allow": True, "reason": "Platform traffic", "kind": "platform"}
    cat.close()


def test_explicit_deny_still_blocks_a_platform_port(tmp_path):
    policy = Policy(
        allow=AllowSpec(regions=["US-OH"]),
        deny=DenySpec(cidrs=["198.51.100.0/24"]),
    )
    cat, eng = setup(tmp_path, policy)
    # Recent matches writes this rule, so blocking a 3074 server still works.
    verdict = eng.observe("198.51.100.10", 3074, "outbound", 60, now=1000.0)
    assert verdict["allow"] is False
    assert verdict["kind"] == "deny"
    cat.close()


def test_provider_block_does_not_drop_relay_traffic(tmp_path):
    cat = Catalog(tmp_path / "relay.db")
    cat.import_geo(
        [{
            "cidr": "198.51.100.0/24",
            "city": "Los Angeles",
            "region": "California",
            "region_code": "CA",
            "country": "United States",
            "country_code": "US",
            "lat": 34.05,
            "lon": -118.24,
            "asn": "AS20473",
            "provider": "The Constant Company",
        }],
        "community_geo",
        0.6,
    )
    policy = Policy(
        allow=AllowSpec(regions=["US-OH"]),
        deny=DenySpec(providers=["Vultr"]),
    )
    eng = Engine(cat, PolicyEngine(policy))
    eng.policy.set_clusters(cat.clusters())
    # Steam's datagram relays answer above 27050, and a host block must not
    # leave the game stuck on "Waiting for relay".
    relay = eng.observe("198.51.100.10", 27056, "outbound", 60, now=1000.0)
    assert relay == {"allow": True, "reason": "Platform traffic", "kind": "platform"}
    match = eng.observe("198.51.100.10", 30000, "outbound", 60, now=1000.0)
    assert match["allow"] is False
    assert match["kind"] == "provider"
    cat.close()


def test_explicit_deny_applies_immediately_in_safe_scope(tmp_path):
    policy = Policy(allow=AllowSpec(regions=["US-OH"]), deny=DenySpec(cidrs=["198.51.100.0/24"]))
    cat, eng = setup(tmp_path, policy)
    verdict = eng.observe("198.51.100.10", 30000, "outbound", 200, now=1000.0)
    assert verdict["allow"] is False
    assert verdict["kind"] == "deny"
    cat.close()


def test_denied_short_match_is_reported_as_cancelled(tmp_path):
    policy = Policy(allow=AllowSpec(regions=["US-OH"]), deny=DenySpec(cidrs=["198.51.100.0/24"]))
    cat, eng = setup(tmp_path, policy)
    t = 1000.0
    for _ in range(80):
        eng.observe("198.51.100.10", 30000, "outbound", 200, now=t)
        t += 0.1
    eng.tick(now=t)
    assert eng.current_match() is not None
    eng.tick(now=t + 16.0)
    state = eng.state()
    assert state["cancelled_total"] == 1
    assert state["last_cancelled"]["cidr"] == "198.51.100.0/24"
    assert state["last_cancelled"]["duration_s"] < 60
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
        eng.observe("203.0.113.10", 30000, "outbound", 200, now=t)
        eng.observe("203.0.113.10", 30000, "inbound", 400, now=t + 0.02)
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
        strict=True,
    )
    cat, eng = setup(tmp_path, policy)
    eng._searching_since = 0.0
    verdict = eng.observe("198.51.100.10", 30000, "outbound", 200, now=25.0)
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
    verdict = eng.observe("192.0.2.55", 30000, "outbound", 200, now=25.0)
    assert verdict["allow"] is True
    assert verdict["kind"] == "soft"
    assert eng.soft_active is True
    cat.close()


def test_soft_mode_disabled_keeps_blocking(tmp_path):
    policy = Policy(
        allow=AllowSpec(regions=["US-OH"]),
        unknown="deny",
        soft_mode=SoftMode(enabled=False),
        strict=True,
    )
    cat, eng = setup(tmp_path, policy)
    eng._searching_since = 0.0
    verdict = eng.observe("192.0.2.55", 30000, "outbound", 200, now=1000.0)
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
        eng.observe("192.0.2.55", 30000, "outbound", 200, now=t)
        t += 0.1
    eng.tick(t + 0.6)
    assert eng.current_match() is not None
    assert eng.soft_active is False
    assert eng._soft_pinned_ip == "192.0.2.55"
    verdict = eng.observe("192.0.2.55", 30000, "outbound", 200, now=t + 0.1)
    assert verdict["kind"] == "soft"
    assert verdict["allow"] is True
    cat.close()


def test_state_reports_counters(tmp_path):
    cat, eng = setup(tmp_path, Policy(allow=AllowSpec(regions=["US-OH"]), strict=True))
    eng.observe("203.0.113.10", 3074, "outbound", 200, now=1000.0)
    eng.observe("198.51.100.10", 3074, "outbound", 200, now=1000.0)
    state = eng.state()
    assert state["filtering"] is True
    assert state["allowed_packets"] == 1
    assert state["denied_packets"] == 1
    assert len(state["recent"]) == 1
    cat.close()
