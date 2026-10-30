from cod_region_filter.catalog import (
    Catalog,
    RangeIndex,
    RangeRecord,
    cidr_for,
    cluster_key,
    region_key,
)


def record(cidr, city="Columbus", region_code="OH"):
    return RangeRecord(
        cidr=cidr,
        city=city,
        region="Ohio",
        region_code=region_code,
        country="United States",
        country_code="US",
        lat=40.0,
        lon=-83.0,
        is_service=False,
        cluster=cluster_key({"country_code": "US", "region_code": region_code, "city": city}),
        region_key=region_key({"country_code": "US", "region_code": region_code}),
    )


def test_cidr_for_collapses_to_slash_24():
    assert cidr_for("203.0.113.77") == "203.0.113.0/24"
    assert cidr_for("2001:db8::1") == "2001:db8::/48"


def test_range_index_uses_longest_prefix():
    index = RangeIndex([record("1.2.0.0/16"), record("1.2.3.0/24", "Cleveland")])
    found = index.find("1.2.3.4")
    assert found is not None
    assert found.cidr == "1.2.3.0/24"
    assert index.find("1.2.9.4").cidr == "1.2.0.0/16"
    assert index.find("9.9.9.9") is None


def test_range_index_ignores_invalid_ip():
    index = RangeIndex([record("1.2.3.0/24")])
    assert index.find("not-an-ip") is None


def location(city, region_code, lat, lon, country_code="US"):
    return {
        "status": "success",
        "city": city,
        "regionName": city + " Metro",
        "region": region_code,
        "country": "United States",
        "countryCode": country_code,
        "lat": lat,
        "lon": lon,
    }


def test_observe_flush_and_cluster(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    cat.observe("203.0.113.10", 1200)
    cat.observe("203.0.113.11", 800)
    cat.flush()
    cat.resolve_location("203.0.113.0/24", location("Columbus", "OH", 39.96, -83.0))
    clusters = cat.clusters()
    assert len(clusters) == 1
    assert clusters[0]["city"] == "Columbus"
    assert clusters[0]["region_key"] == "US-OH"
    assert clusters[0]["bytes"] == 2000
    cat.close()


def test_clusters_group_by_region_and_city(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    for ip, geo in [
        ("203.0.113.10", location("Columbus", "OH", 39.96, -83.0)),
        ("203.0.114.10", location("Columbus", "OH", 39.97, -83.01)),
        ("198.51.100.10", location("Los Angeles", "CA", 34.05, -118.24)),
    ]:
        cat.observe(ip, 100)
        cat.flush()
        cat.resolve_location(cidr_for(ip), geo)
    clusters = cat.clusters()
    assert len(clusters) == 2
    by_city = {c["city"]: c for c in clusters}
    assert by_city["Columbus"]["server_count"] == 2
    assert by_city["Los Angeles"]["region_key"] == "US-CA"
    assert isinstance(by_city["Columbus"]["lat"], float)
    cat.close()


def test_cluster_order_is_deterministic(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    for ip, geo, size in [
        ("203.0.113.10", location("Columbus", "OH", 39.96, -83.0), 500),
        ("198.51.100.10", location("Los Angeles", "CA", 34.05, -118.24), 900),
    ]:
        cat.observe(ip, size)
        cat.flush()
        cat.resolve_location(cidr_for(ip), geo)
    first = [c["key"] for c in cat.clusters()]
    second = [c["key"] for c in cat.clusters()]
    assert first == second
    assert first[0].endswith("Los Angeles")
    cat.close()


def test_resolve_location_keeps_valid_zero_coordinates(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    cat.observe("203.0.113.10", 100)
    cat.flush()
    cat.resolve_location("203.0.113.0/24", location("Accra", "AA", 0.0, 0.0, "GH"))
    assert len(cat.clusters()) == 1
    cat.close()


def test_mark_service_excludes_range_from_clusters(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    cat.observe("203.0.113.10", 100)
    cat.flush()
    cat.resolve_location("203.0.113.0/24", location("Columbus", "OH", 39.96, -83.0))
    cat.mark_service(["203.0.113.10"])
    assert cat.clusters() == []
    assert cat.index().find("203.0.113.10").is_service is True
    cat.close()


def test_record_and_read_matches(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    cat.record_match(
        {
            "remote_ip": "203.0.113.10",
            "cidr": "203.0.113.0/24",
            "city": "Columbus",
            "region": "Ohio",
            "country": "United States",
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:01:00+00:00",
            "duration_s": 60,
            "bytes": 5000,
            "rtt_ms": 22,
            "allowed": True,
            "reason": "Allowed by region",
        }
    )
    rows = cat.matches()
    assert len(rows) == 1
    assert rows[0]["city"] == "Columbus"
    assert rows[0]["allowed"] == 1
    cat.close()


def test_stats_and_export(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    cat.observe("203.0.113.10", 300)
    cat.flush()
    cat.resolve_location("203.0.113.0/24", location("Columbus", "OH", 39.96, -83.0))
    stats = cat.stats()
    assert stats["ranges"] == 1
    assert stats["observed_ranges"] == 1
    out = tmp_path / "catalog.csv"
    cat.export_csv(out)
    text = out.read_text(encoding="utf-8")
    assert "203.0.113.0/24" in text
    assert "Columbus" in text
    cat.close()


def test_export_does_not_write_partial_file_on_empty_catalog(tmp_path):
    cat = Catalog(tmp_path / "catalog.db")
    out = tmp_path / "catalog.csv"
    cat.export_csv(out)
    assert out.exists()
    cat.close()

def test_seed_includes_bundled_community_list(tmp_path):
    import json
    from cod_region_filter import paths

    records = json.loads(
        paths.resource("data/seed_community.json").read_text(encoding="utf-8")
    )["records"]
    assert len(records) > 6000
    target = next(r for r in records if r["city"])

    catalog = Catalog(tmp_path / "catalog.db")
    assert catalog.seed() > 6000
    row = catalog.index().find(target["cidr"].replace(".0/24", ".7"))
    assert row is not None
    assert row.city == target["city"]
    catalog.close()
