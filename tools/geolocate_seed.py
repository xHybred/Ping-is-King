"""Resolve the seed /24 list to approximate locations with ip-api.

Run once at build time. ip-api's free tier allows 15 batch requests per
minute, so this deliberately spaces requests out and writes progress after
every batch. The app never geolocates the whole catalog at runtime; it only
resolves endpoints it actually sees.
"""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "seed_ranges.json"
OUT = ROOT / "data" / "seed_geo.json"
ENDPOINT = "http://ip-api.com/batch"
FIELDS = "status,message,country,countryCode,region,regionName,city,lat,lon,isp,org,as"
BATCH = 100
PAUSE_S = 4.4


def query(batch: list[str]) -> list[dict]:
    body = json.dumps(batch).encode()
    req = urllib.request.Request(
        f"{ENDPOINT}?fields={FIELDS}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> None:
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    cidrs = [row["cidr"] for row in seed["ranges"]]

    done: dict[str, dict] = {}
    if OUT.exists():
        for row in json.loads(OUT.read_text(encoding="utf-8"))["locations"]:
            done[row["cidr"]] = row

    pending = [c for c in cidrs if c not in done]
    print(f"{len(done)} cached, {len(pending)} to resolve")

    for start in range(0, len(pending), BATCH):
        chunk = pending[start:start + BATCH]
        probes = [str(ipaddress.ip_network(c)[1]) for c in chunk]

        for _attempt in range(4):
            try:
                results = query(probes)
                break
            except urllib.error.HTTPError as exc:
                wait = 20 if exc.code == 429 else 5
                print(f"http {exc.code}, retry in {wait}s")
                time.sleep(wait)
            except Exception as exc:  # network flake, keep going
                print(f"request failed: {exc}, retry in 10s")
                time.sleep(10)
        else:
            print("giving up on this batch")
            continue

        for cidr, res in zip(chunk, results, strict=False):
            if res.get("status") != "success":
                continue
            done[cidr] = {
                "cidr": cidr,
                "city": res.get("city") or "",
                "region": res.get("regionName") or "",
                "region_code": res.get("region") or "",
                "country": res.get("country") or "",
                "country_code": res.get("countryCode") or "",
                "lat": res.get("lat"),
                "lon": res.get("lon"),
                "org": res.get("org") or "",
            }

        OUT.write_text(
            json.dumps(
                {
                    "source": "ip-api.com",
                    "note": "Approximate datacenter location, resolved from the seed /24 list. "
                            "Observations in the local catalog override this.",
                    "locations": sorted(done.values(), key=lambda r: ipaddress.ip_network(r["cidr"])),
                },
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"{len(done)}/{len(cidrs)} resolved")
        time.sleep(PAUSE_S)


if __name__ == "__main__":
    main()
