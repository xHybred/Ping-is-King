from cod_region_filter.policy import (
    AllowSpec,
    DenySpec,
    Policy,
    PolicyEngine,
    SoftMode,
    default_policy,
    dump,
    haversine_km,
    lint,
    parse,
)


def cluster(key, city, region_code="OH", country_code="US", lat=40.0, lon=-83.0, rtt=None):
    return {
        "key": key,
        "city": city,
        "region": "Ohio",
        "region_code": region_code,
        "region_key": f"{country_code}-{region_code}",
        "country": "United States",
        "country_code": country_code,
        "lat": lat,
        "lon": lon,
        "avg_rtt_ms": rtt,
        "server_count": 1,
    }


class Rec:
    def __init__(self, cidr, key, region_key="US-OH", is_service=False):
        self.cidr = cidr
        self.cluster = key
        self.region_key = region_key
        self.is_service = is_service
        self.city = "Columbus"


OHIO = cluster("US|OH|Columbus", "Columbus")
CALI = cluster("US|CA|Los Angeles", "Los Angeles", "CA", lat=34.05, lon=-118.24)


def engine(policy, clusters=(OHIO, CALI)):
    eng = PolicyEngine(policy)
    eng.set_clusters(list(clusters))
    return eng


def test_allowlisted_region_is_allowed():
    p = Policy(allow=AllowSpec(regions=["US-OH"]))
    eng = engine(p)
    verdict = eng.decide("1.2.3.4", Rec("1.2.3.0/24", OHIO["key"]), OHIO)
    assert verdict == {"allow": True, "reason": "Allowed by region", "kind": "region"}


def test_located_cluster_outside_allowlist_is_denied():
    p = Policy(allow=AllowSpec(regions=["US-OH"]))
    eng = engine(p)
    verdict = eng.decide("5.6.7.8", Rec("5.6.7.0/24", CALI["key"], "US-CA"), CALI)
    assert verdict["allow"] is False
    assert verdict["kind"] == "allowlist"


def test_deny_beats_allow():
    p = Policy(
        allow=AllowSpec(regions=["US-OH"]),
        deny=DenySpec(regions=["US-OH"]),
    )
    eng = engine(p)
    verdict = eng.decide("1.2.3.4", Rec("1.2.3.0/24", OHIO["key"]), OHIO)
    assert verdict["allow"] is False
    assert verdict["kind"] == "deny"


def test_service_endpoint_always_allowed():
    p = Policy(allow=AllowSpec(regions=["US-OH"]), deny=DenySpec(cidrs=["9.9.9.0/24"]))
    eng = engine(p)
    rec = Rec("9.9.9.0/24", OHIO["key"], is_service=True)
    assert eng.decide("9.9.9.9", rec, OHIO)["kind"] == "service"


def test_resolved_service_ip_always_allowed():
    p = Policy(allow=AllowSpec(regions=["US-OH"]), unknown="deny")
    eng = engine(p)
    eng.set_service_ips(["52.1.2.3"])
    assert eng.decide("52.1.2.3", None, None)["kind"] == "service"


def test_denied_cidr_applies_before_catalog_knows_the_ip():
    p = Policy(deny=DenySpec(cidrs=["20.165.189.0/24"]))
    eng = engine(p)
    verdict = eng.decide("20.165.189.150", None, None)
    assert verdict["allow"] is False
    assert verdict["kind"] == "deny"


def test_max_rtt_blocks_before_unknown_default():
    p = Policy(allow=AllowSpec(regions=["US-OH"]), max_rtt_ms=40)
    eng = engine(p)
    slow = cluster("US|TX|Dallas", "Dallas", "TX", lat=32.7, lon=-96.8, rtt=70)
    eng.set_clusters([OHIO, slow])
    verdict = eng.decide("4.4.4.4", Rec("4.4.4.0/24", slow["key"], "US-TX"), slow)
    assert verdict["kind"] == "rtt"
    assert verdict["allow"] is False


def test_unknown_cluster_follows_unknown_setting():
    p = Policy(allow=AllowSpec(regions=["US-OH"]), unknown="allow")
    eng = engine(p)
    assert eng.decide("8.8.4.4", None, None)["allow"] is True

    p.unknown = "deny"
    eng = engine(p)
    assert eng.decide("8.8.4.4", None, None)["allow"] is False


def test_radius_compiles_into_cluster_set():
    p = Policy(
        allow=AllowSpec(radius_km=300),
        home_lat=39.96,
        home_lon=-83.0,
    )
    eng = engine(p)
    assert OHIO["key"] in eng._radius_keys
    assert CALI["key"] not in eng._radius_keys
    verdict = eng.decide("1.2.3.4", Rec("1.2.3.0/24", OHIO["key"]), OHIO)
    assert verdict["reason"] == "Allowed by radius"


def test_radius_without_home_matches_nothing():
    p = Policy(allow=AllowSpec(radius_km=500))
    eng = engine(p)
    assert eng._radius_keys == set()


def test_haversine_known_distance():
    # Columbus to Los Angeles is roughly 3,200 km.
    d = haversine_km(39.96, -83.0, 34.05, -118.24)
    assert 3100 < d < 3300


def test_parse_accepts_unset_home():
    parsed, errors = parse("mode: allowlist\nhome:\n  lat: null\n  lon: null\n")
    assert errors == []
    assert parsed.home_lat is None


def test_parse_rejects_half_set_home():
    parsed, errors = parse("home:\n  lat: 39.9\n  lon: null\n")
    assert parsed is None
    assert any("home" in e for e in errors)


def test_parse_round_trip():
    text = dump(Policy(allow=AllowSpec(regions=["US-OH"]), home_lat=39.9, home_lon=-83.0))
    parsed, errors = parse(text)
    assert errors == []
    assert parsed.allow.regions == ["US-OH"]
    assert parsed.home_lat == 39.9


def test_parse_reports_syntax_error_with_line():
    parsed, errors = parse("mode: allowlist\n  bad indent: here")
    assert parsed is None
    assert errors and errors[0].startswith("line")


def test_parse_rejects_unknown_key():
    parsed, errors = parse("mode: allowlist\nbogus: 1")
    assert parsed is None
    assert any("bogus" in e for e in errors)


def test_parse_rejects_bad_region_type():
    parsed, errors = parse("allow:\n  regions: US-OH")
    assert parsed is None
    assert any("allow.regions" in e for e in errors)


def test_lint_warns_on_empty_allowlist():
    warnings = lint(Policy())
    assert warnings and "No regions" in warnings[0]


def test_fresh_install_blocks_nothing():
    p = default_policy()
    eng = engine(p)
    assert p.mode == "blocklist"
    assert eng.decide("203.0.113.9", None, None)["allow"] is True
    assert eng.summary()["empty_allowlist"] is False


def test_summary_counts_allowed_clusters():
    p = Policy(allow=AllowSpec(regions=["US-OH"]))
    eng = engine(p)
    summary = eng.summary()
    assert summary["mode"] == "allowlist"
    assert summary["clusters"] == 2
    assert summary["allowed_clusters"] == 1
    assert summary["empty_allowlist"] is False


def test_soft_mode_defaults():
    p = Policy(soft_mode=SoftMode())
    assert p.soft_mode.enabled is True
    assert p.soft_mode.relax_after_s == 45
