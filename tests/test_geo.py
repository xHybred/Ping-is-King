from cod_region_filter.geo import _normalise


def test_normalise_maps_ipwho_fields_to_catalog_keys():
    data = {
        "success": True,
        "country": "United States",
        "country_code": "US",
        "region": "Wyoming",
        "region_code": "WY",
        "city": "Cheyenne",
        "latitude": 41.14,
        "longitude": -104.82,
    }
    result = _normalise(data)
    assert result["status"] == "success"
    assert result["regionName"] == "Wyoming"
    assert result["region"] == "WY"
    assert result["countryCode"] == "US"
    assert result["lat"] == 41.14
    assert result["lon"] == -104.82


def test_normalise_rejects_unsuccessful_lookup():
    assert _normalise({"success": False, "message": "Reserved range"}) is None
