"""Build the bundled community seed from cod-server-list's raw JSON.

The upstream file is 14 MB of per-IP rows. The catalog only needs one entry per
/24, so collapse it here and keep the download out of the app package.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cod_region_filter.catalog_update import parse_geo_list  # noqa: E402

RAW = ROOT / "data" / "raw" / "cod_servers_all.json"
OUT = ROOT / "data" / "seed_community.json"


def main() -> int:
    if not RAW.exists():
        print(f"missing {RAW}")
        return 1
    records = parse_geo_list(RAW.read_bytes())
    OUT.write_text(
        json.dumps({"source": "cod-server-list", "records": records}, separators=(",", ":")),
        encoding="utf-8",
    )
    located = sum(1 for r in records if r["city"])
    print(f"{len(records)} ranges ({located} located) -> {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
