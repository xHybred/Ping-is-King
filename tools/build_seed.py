"""Turn the raw Geomate CoD IP list into a /24 seed catalog.

The raw list is a scrape of observed game traffic and contains private,
loopback, multicast and broadcast addresses. Those are dropped here so the
catalog never carries unrouteable rows.
"""

from __future__ import annotations

import ipaddress
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "cod_servers.txt"
OUT = ROOT / "data" / "seed_ranges.json"

# Public resolvers that show up in traffic captures but are never game servers.
RESOLVERS = {
    "8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9",
    "208.67.222.222", "208.67.220.220", "64.233.184.188",
}


def main() -> None:
    counts: Counter[str] = Counter()
    dropped = Counter()

    for line in RAW.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            addr = ipaddress.ip_address(line)
        except ValueError:
            dropped["invalid"] += 1
            continue
        if addr.version != 4:
            dropped["ipv6"] += 1
            continue
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
            or addr.is_link_local
        ):
            dropped["non-routable"] += 1
            continue
        if str(addr) in RESOLVERS:
            dropped["resolver"] += 1
            continue
        counts[str(ipaddress.ip_network(f"{addr}/24", strict=False))] += 1

    ranges = [
        {"cidr": cidr, "ip_count": n}
        for cidr, n in sorted(counts.items(), key=lambda kv: ipaddress.ip_network(kv[0]))
    ]
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "https://github.com/hudra0/geomate (files/etc/geomate.d/cod_servers.txt)",
        "note": "Observed Call of Duty endpoint addresses, collapsed to /24. "
                "Not authoritative and not geographically resolved.",
        "ranges": ranges,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    print(f"ranges: {len(ranges)}")
    print(f"addresses: {sum(counts.values())}")
    for reason, n in dropped.most_common():
        print(f"dropped {reason}: {n}")


if __name__ == "__main__":
    main()
