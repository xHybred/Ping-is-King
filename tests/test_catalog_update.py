import json

from cod_region_filter.catalog import Catalog
from cod_region_filter.catalog_update import parse_geo_list, parse_ip_list


def test_parse_ip_list_drops_non_public_and_collapses():
    text = "\n".join(
        [
            "# comment",
            "10.0.0.5",
            "127.0.0.1",
            "203.0.113.9",
            "8.8.8.8",
            "45.63.1.10",
            "45.63.1.99",
            "45.63.1.10:3074",
        ]
    )
    assert parse_ip_list(text) == ["45.63.1.0/24"]


def test_parse_geo_list_picks_majority_and_normalises_state():
    payload = json.dumps(
        {
            "data": [
                {
                    "ip": "104.156.254.26",
                    "ip_type": 4,
                    "city": "Columbus",
                    "subdivision": "Ohio",
                    "country": "United States",
                    "country_abbr": "US",
                    "latitude": 39.96,
                    "longitude": -83.0,
                },
                {
                    "ip": "104.156.254.27",
                    "ip_type": 4,
                    "city": "Columbus",
                    "subdivision": "Ohio",
                    "country": "United States",
                    "country_abbr": "US",
                    "latitude": 39.96,
                    "longitude": -83.0,
                },
                {
                    "ip": "104.156.254.28",
                    "ip_type": 4,
                    "city": "Cleveland",
                    "subdivision": "Ohio",
                    "country": "United States",
                    "country_abbr": "US",
                    "latitude": 41.5,
                    "longitude": -81.7,
                },
                {"ip": "10.0.0.1", "ip_type": 4, "city": "Nowhere"},
            ]
        }
    ).encode()
    records = parse_geo_list(payload)
    assert len(records) == 1
    record = records[0]
    assert record["cidr"] == "104.156.254.0/24"
    assert record["city"] == "Columbus"
    assert record["region_code"] == "OH"
    assert record["country_code"] == "US"


def test_import_geo_merges_without_clobbering_known_location(tmp_path):
    catalog = Catalog(tmp_path / "catalog.db")
    catalog.import_geo(
        [{"cidr": "45.63.1.0/24", "city": "Ashburn", "region": "Virginia",
          "region_code": "VA", "country": "United States", "country_code": "US",
          "lat": 39.0, "lon": -77.5}],
        "seed",
        0.4,
    )
    catalog.import_geo(
        [{"cidr": "45.63.1.0/24", "city": "Dallas", "region": "Texas",
          "region_code": "TX", "country": "United States", "country_code": "US",
          "lat": 32.7, "lon": -96.8}],
        "community_geo",
        0.55,
    )
    row = catalog.index().find("45.63.1.5")
    assert row.city == "Ashburn"
    assert row.lat == 39.0
    stored = catalog._conn.execute(
        "SELECT confidence FROM ranges WHERE cidr = ?", ("45.63.1.0/24",)
    ).fetchone()
    assert stored["confidence"] == 0.55
    catalog.close()
