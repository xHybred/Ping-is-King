"""Refresh the catalog from public Call of Duty server lists.

Two community sources are used:

* Geomate's cod_servers.txt - plain addresses, no location. Geomate is an
  OpenWrt geo-filter with a large, actively maintained CoD list.
* zFF2/cod-server-list - per-IP geolocated records (city, subdivision,
  country, lat/lon). Collapsed to /24 here because that is the catalog unit.

Neither list is authoritative: game servers move between cloud ranges, and
both projects say their data can be stale. Records are imported with lower
confidence than ip-api lookups of addresses this machine has actually seen,
and the catalog merges rather than overwrites.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections import Counter

import requests

log = logging.getLogger(__name__)

TIMEOUT_S = 45
MAX_BYTES = 64 * 1024 * 1024

SOURCES = (
    {
        "name": "Geomate",
        "kind": "ip_list",
        "url": "https://raw.githubusercontent.com/hudra0/geomate/main/files/etc/geomate.d/cod_servers.txt",
        "home": "https://github.com/hudra0/geomate",
    },
    {
        "name": "cod-server-list",
        "kind": "geo_json",
        "url": "https://raw.githubusercontent.com/zFF2/cod-server-list/main/cod_servers_all.json",
        "home": "https://github.com/zFF2/cod-server-list",
    },
)

# ip-api returns two-letter codes while the community list returns full state
# names. Without normalising, "Ohio" and "OH" become separate regions.
US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
}

CA_PROVINCES = {
    "Alberta": "AB", "British Columbia": "BC", "Manitoba": "MB",
    "New Brunswick": "NB", "Newfoundland and Labrador": "NL",
    "Northwest Territories": "NT", "Nova Scotia": "NS", "Nunavut": "NU",
    "Ontario": "ON", "Prince Edward Island": "PE", "Quebec": "QC",
    "Saskatchewan": "SK", "Yukon": "YT",
}


def _region_code(country_code: str, subdivision: str) -> str:
    if not subdivision:
        return ""
    if country_code == "US":
        return US_STATES.get(subdivision, subdivision)
    if country_code == "CA":
        return CA_PROVINCES.get(subdivision, subdivision)
    return subdivision


def _public_ip(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return addr.is_global and not addr.is_multicast


# The Geomate list contains a few public resolvers (8.8.8.8 and friends) that
# are not game servers, so is_global alone is not enough.
RESOLVERS = {
    "8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9",
    "149.112.112.112", "208.67.222.222", "208.67.220.220",
    "64.6.64.6", "64.6.65.6", "77.88.8.8", "77.88.8.1",
}


def _cidr(ip: str) -> str:
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


def parse_ip_list(text: str) -> list[str]:
    cidrs = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # Some lists carry "ip:port"; only the address is useful here.
        ip = line.rsplit(":", 1)[0] if line.count(":") == 1 else line
        if _public_ip(ip) and ip not in RESOLVERS:
            cidrs.add(_cidr(ip))
    return sorted(cidrs)


def parse_geo_list(payload: bytes) -> list[dict]:
    data = json.loads(payload.decode("utf-8")).get("data", [])
    per_range: dict[str, Counter] = {}
    for row in data:
        if row.get("ip_type") not in (4, None):
            continue
        ip = row.get("ip")
        if not ip or not _public_ip(ip):
            continue
        city = (row.get("city") or "").strip()
        subdivision = (row.get("subdivision") or "").strip()
        country = (row.get("country") or "").strip()
        cc = (row.get("country_abbr") or "").strip().upper()
        lat, lon = row.get("latitude"), row.get("longitude")
        if lat is None or lon is None:
            continue
        key = (city, subdivision, country, cc, round(float(lat), 3), round(float(lon), 3))
        per_range.setdefault(_cidr(ip), Counter())[key] += 1

    records = []
    for cidr, votes in per_range.items():
        (city, subdivision, country, cc, lat, lon), _ = votes.most_common(1)[0]
        records.append(
            {
                "cidr": cidr,
                "city": city,
                "region": subdivision,
                "region_code": _region_code(cc, subdivision),
                "country": country,
                "country_code": cc,
                "lat": lat,
                "lon": lon,
            }
        )
    return records


def _download(url: str) -> bytes:
    with requests.get(url, timeout=TIMEOUT_S, stream=True) as response:
        response.raise_for_status()
        chunks = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("source is larger than expected")
            chunks.append(chunk)
    return b"".join(chunks)


def collect() -> dict:
    """Download every source. Network failures are reported, not raised."""
    report = []
    ranges: set[str] = set()
    records: list[dict] = []

    for source in SOURCES:
        entry = {"name": source["name"], "home": source["home"], "ok": False, "detail": ""}
        try:
            payload = _download(source["url"])
            if source["kind"] == "ip_list":
                found = parse_ip_list(payload.decode("utf-8", "replace"))
                ranges.update(found)
                entry["detail"] = f"{len(found):,} ranges"
            else:
                found = parse_geo_list(payload)
                records.extend(found)
                ranges.update(rec["cidr"] for rec in found)
                entry["detail"] = f"{len(found):,} located ranges"
            entry["ok"] = True
        except Exception as exc:  # one dead source must not stop the other
            entry["detail"] = str(exc)[:180]
            log.warning("catalog source %s failed: %s", source["name"], exc)
        report.append(entry)

    return {"ranges": sorted(ranges), "records": records, "report": report}
