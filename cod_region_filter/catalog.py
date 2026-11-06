"""Local catalog of endpoint ranges, observed traffic and match history."""

from __future__ import annotations

import csv
import ipaddress
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS ranges (
    cidr          TEXT PRIMARY KEY,
    city          TEXT NOT NULL DEFAULT '',
    region        TEXT NOT NULL DEFAULT '',
    region_code   TEXT NOT NULL DEFAULT '',
    country       TEXT NOT NULL DEFAULT '',
    country_code  TEXT NOT NULL DEFAULT '',
    lat           REAL,
    lon           REAL,
    asn           INTEGER NOT NULL DEFAULT 0,
    provider      TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'observed',
    confidence    REAL NOT NULL DEFAULT 0.5,
    is_service    INTEGER NOT NULL DEFAULT 0,
    match_server  INTEGER NOT NULL DEFAULT 0,
    packets       INTEGER NOT NULL DEFAULT 0,
    bytes         INTEGER NOT NULL DEFAULT 0,
    rtt_ms        REAL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ranges_lookup ON ranges (country_code, region_code, city);
CREATE TABLE IF NOT EXISTS matches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_ip   TEXT NOT NULL,
    cidr        TEXT NOT NULL,
    city        TEXT NOT NULL DEFAULT '',
    region      TEXT NOT NULL DEFAULT '',
    country     TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    duration_s  INTEGER NOT NULL,
    bytes       INTEGER NOT NULL,
    rtt_ms      REAL,
    allowed     INTEGER NOT NULL,
    reason      TEXT NOT NULL DEFAULT ''
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cidr_for(ip: str) -> str:
    addr = ipaddress.ip_address(ip)
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False))


def cluster_key(row: dict) -> str:
    return f"{row.get('country_code') or 'XX'}|{row.get('region_code') or ''}|{row.get('city') or ''}"


def region_key(row: dict) -> str:
    return f"{row.get('country_code') or 'XX'}-{row.get('region_code') or ''}".rstrip("-")


def asn_value(value) -> int:
    """Accept 8075, "8075" or "AS8075"; anything else becomes 0."""
    text = str(value or "").strip().upper()
    if text.startswith("AS"):
        text = text[2:]
    try:
        return int(text or 0)
    except ValueError:
        return 0


def provider_label(provider: str, asn: int = 0) -> str:
    """Collapse hosting brand names onto the family a player would recognise.

    The public lists mix corporate names with regional product strings
    ("AWS EC2 (af-south-1)", "Vultr Holdings, LLC"), so grouping by the raw
    string splits one provider across dozens of rows.
    """
    name = (provider or "").strip()
    low = name.lower()
    if not asn:
        asn = asn_value(name)
    if "vultr" in low or "constant company" in low or asn == 20473:
        return "Vultr"
    if low.startswith("aws") or "amazon" in low or asn == 16509:
        return "Amazon Web Services"
    if "google" in low or asn in (15169, 396982):
        return "Google Cloud"
    if "microsoft" in low or asn == 8075:
        return "Microsoft Azure"
    if "akamai" in low:
        return "Akamai"
    if "cloudflare" in low:
        return "Cloudflare"
    return name


@dataclass(frozen=True)
class RangeRecord:
    cidr: str
    city: str
    region: str
    region_code: str
    country: str
    country_code: str
    lat: float | None
    lon: float | None
    is_service: bool
    cluster: str
    region_key: str
    source: str = "seed"
    asn: int = 0
    provider: str = ""
    match_server: int = 0

    @property
    def known_server(self) -> bool:
        """True when we have reason to treat this range as a game server.

        Public lists count, and so does a range this install has already seen a
        match on. Ranges that were merely observed during a matchmaking scan are
        not enough on their own.
        """
        return self.source in ("seed", "community_geo") or bool(self.match_server)


class RangeIndex:
    """Longest-prefix lookup over catalogued ranges.

    Ranges are all /24 (or /48) today, but the index keeps start/end so a
    later import of a wider published range does not break lookups.
    """

    def __init__(self, records: Iterable[RangeRecord] = ()) -> None:
        self._nets: dict[tuple[int, int, int], RangeRecord] = {}
        for rec in records:
            try:
                net = ipaddress.ip_network(rec.cidr, strict=False)
            except ValueError:
                continue
            self._nets[(net.version, int(net.network_address), net.prefixlen)] = rec

    def put(self, rec: RangeRecord) -> None:
        try:
            net = ipaddress.ip_network(rec.cidr, strict=False)
        except ValueError:
            return
        self._nets[(net.version, int(net.network_address), net.prefixlen)] = rec

    def drop(self, cidr: str) -> None:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            return
        self._nets.pop((net.version, int(net.network_address), net.prefixlen), None)

    def find(self, ip: str) -> RangeRecord | None:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        bits = 32 if addr.version == 4 else 128
        value = int(addr)
        for prefix in range(bits, -1, -1):
            mask = ((1 << prefix) - 1) << (bits - prefix) if prefix else 0
            rec = self._nets.get((addr.version, value & mask, prefix))
            if rec is not None:
                return rec
        return None


class Catalog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL keeps readers off the writer's back and NORMAL avoids an fsync per
        # commit. The packet thread should never wait on a disk flush.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=2000")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        self._pending: dict[str, dict] = {}
        self._index = RangeIndex()
        self._dirty_cidrs: set[str] = set()
        self._version = 0
        self._touch()

    def _migrate(self) -> None:
        """Add columns introduced after a catalog was first created.

        Existing installs keep their observed ranges and match history; the
        provider, ASN and match-server columns are new, and bundled seed data
        fills the location fields in later.
        """
        have = {row["name"] for row in self._conn.execute("PRAGMA table_info(ranges)")}
        if "asn" not in have:
            self._conn.execute("ALTER TABLE ranges ADD COLUMN asn INTEGER NOT NULL DEFAULT 0")
        if "provider" not in have:
            self._conn.execute("ALTER TABLE ranges ADD COLUMN provider TEXT NOT NULL DEFAULT ''")
        if "match_server" not in have:
            self._conn.execute(
                "ALTER TABLE ranges ADD COLUMN match_server INTEGER NOT NULL DEFAULT 0"
            )

    def _touch(self) -> None:
        """Mark the whole index stale and bump the version readers cache against."""
        self._index_dirty = True
        self._version += 1

    def _touch_range(self, cidr: str) -> None:
        """Re-read one range. A full index rebuild costs 50-90ms, which the
        packet thread cannot afford while a match is running."""
        self._dirty_cidrs.add(cidr)
        self._version += 1

    @property
    def version(self) -> int:
        return self._version

    def close(self) -> None:
        with self._lock:
            self.flush()
            self._conn.close()

    # -- seeding ---------------------------------------------------------

    def is_empty(self) -> bool:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM ranges").fetchone()
        return not row or row["n"] == 0

    def seed(self) -> int:
        """Import bundled seed ranges, geolocated where available."""
        seed_file = paths.resource("data/seed_ranges.json")
        if not seed_file.exists():
            return 0
        seed = json.loads(seed_file.read_text(encoding="utf-8"))
        geo: dict[str, dict] = {}
        geo_file = paths.resource("data/seed_geo.json")
        if geo_file.exists():
            for row in json.loads(geo_file.read_text(encoding="utf-8")).get("locations", []):
                geo[row["cidr"]] = row

        now = utcnow()
        rows = []
        for item in seed.get("ranges", []):
            cidr = item["cidr"]
            loc = geo.get(cidr, {})
            rows.append(
                (
                    cidr,
                    loc.get("city", ""),
                    loc.get("region", ""),
                    loc.get("region_code", ""),
                    loc.get("country", ""),
                    loc.get("country_code", ""),
                    loc.get("lat"),
                    loc.get("lon"),
                    0,
                    loc.get("org") or loc.get("isp") or "",
                    "seed",
                    0.4 if loc else 0.15,
                    0,
                    0,
                    0,
                    None,
                    now,
                    now,
                )
            )
        with self._lock:
            self._conn.executemany(
                "INSERT OR IGNORE INTO ranges (cidr, city, region, region_code, country, "
                "country_code, lat, lon, asn, provider, source, confidence, is_service, "
                "packets, bytes, rtt_ms, first_seen, last_seen) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._conn.commit()
            self._touch()

        community = paths.resource("data/seed_community.json")
        if community.exists():
            payload = json.loads(community.read_text(encoding="utf-8"))
            count = self.import_geo(payload.get("records", []), "community_geo", 0.55)
            return len(rows) + count
        return len(rows)

    def missing_provider(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM ranges WHERE is_service = 0 AND provider = ''"
        ).fetchone()
        return row["n"] if row else 0

    def backfill_providers(self) -> int:
        """Merge ASN and provider into ranges that were catalogued without them."""
        community = paths.resource("data/seed_community.json")
        if not community.exists():
            return 0
        payload = json.loads(community.read_text(encoding="utf-8"))
        return self.import_geo(payload.get("records", []), "community_geo", 0.55)

    # -- observation -----------------------------------------------------

    def observe(self, ip: str, size: int, rtt_ms: float | None = None, cidr: str | None = None) -> str:
        """Buffer traffic for a remote IP and return its /24 cidr."""
        cidr = cidr or cidr_for(ip)
        with self._pending_lock:
            slot = self._pending.get(cidr)
            if slot is None:
                slot = self._pending[cidr] = {"packets": 0, "bytes": 0, "rtt": []}
            slot["packets"] += 1
            slot["bytes"] += size
            if rtt_ms is not None and 0 < rtt_ms < 2000:
                slot["rtt"].append(rtt_ms)
                if len(slot["rtt"]) > 20:
                    slot["rtt"].pop(0)
        return cidr

    def flush(self) -> None:
        # Swap first, then write. observe() can keep counting into the fresh
        # buffer while SQLite commits; it never waits for the disk.
        with self._pending_lock:
            if not self._pending:
                return
            pending, self._pending = self._pending, {}
        with self._lock:
            now = utcnow()
            inserted: list[str] = []
            for cidr, slot in pending.items():
                row = self._conn.execute(
                    "SELECT rtt_ms FROM ranges WHERE cidr = ?", (cidr,)
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO ranges (cidr, source, confidence, packets, bytes, rtt_ms, "
                        "first_seen, last_seen) VALUES (?, 'observed', 0.5, ?, ?, ?, ?, ?)",
                        (cidr, slot["packets"], slot["bytes"],
                         sum(slot["rtt"]) / len(slot["rtt"]) if slot["rtt"] else None,
                         now, now),
                    )
                    inserted.append(cidr)
                else:
                    new_rtt = row["rtt_ms"]
                    if slot["rtt"]:
                        sample = sum(slot["rtt"]) / len(slot["rtt"])
                        new_rtt = sample if new_rtt is None else (new_rtt * 0.7 + sample * 0.3)
                    self._conn.execute(
                        "UPDATE ranges SET packets = packets + ?, bytes = bytes + ?, "
                        "rtt_ms = ?, last_seen = ? WHERE cidr = ?",
                        (slot["packets"], slot["bytes"], new_rtt, now, cidr),
                    )
            self._conn.commit()
            # Traffic counters are not part of a decision, so an update must
            # not invalidate the index. A new range still has to be visible.
            for cidr in inserted:
                self._touch_range(cidr)

    def resolve_location(self, cidr: str, geo: dict) -> None:
        if not geo or geo.get("status") != "success":
            return
        with self._lock:
            asn = asn_value(geo.get("asn"))
            provider = (geo.get("provider") or "").strip()
            self._conn.execute(
                "UPDATE ranges SET city = ?, region = ?, region_code = ?, country = ?, "
                "country_code = ?, lat = ?, lon = ?, "
                "asn = CASE WHEN ? > 0 THEN ? ELSE asn END, "
                "provider = CASE WHEN ? != '' THEN ? ELSE provider END, "
                "confidence = MAX(confidence, 0.7) WHERE cidr = ?",
                (
                    geo.get("city") or "",
                    geo.get("regionName") or geo.get("region") or "",
                    geo.get("region") or "",
                    geo.get("country") or "",
                    geo.get("countryCode") or "",
                    geo.get("lat"),
                    geo.get("lon"),
                    asn,
                    asn,
                    provider,
                    provider,
                    cidr,
                ),
            )
            self._conn.commit()
            self._touch_range(cidr)

    def import_geo(self, records: Iterable[dict], source: str, confidence: float) -> int:
        """Merge third-party locations without overwriting better existing data."""
        now = utcnow()
        rows = [
            (
                rec["cidr"],
                rec.get("city") or "",
                rec.get("region") or "",
                rec.get("region_code") or "",
                rec.get("country") or "",
                rec.get("country_code") or "",
                rec.get("lat"),
                rec.get("lon"),
                asn_value(rec.get("asn")),
                rec.get("provider") or "",
                source,
                confidence,
                0,
                0,
                0,
                None,
                now,
                now,
            )
            for rec in records
            if rec.get("cidr")
        ]
        if not rows:
            return 0
        with self._lock:
            self._conn.executemany(
                "INSERT INTO ranges (cidr, city, region, region_code, country, country_code, "
                "lat, lon, asn, provider, source, confidence, is_service, packets, bytes, rtt_ms, "
                "first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(cidr) DO UPDATE SET "
                "city = CASE WHEN ranges.city = '' THEN excluded.city ELSE ranges.city END, "
                "region = CASE WHEN ranges.region = '' THEN excluded.region ELSE ranges.region END, "
                "region_code = CASE WHEN ranges.region_code = '' THEN excluded.region_code "
                "ELSE ranges.region_code END, "
                "country = CASE WHEN ranges.country = '' THEN excluded.country ELSE ranges.country END, "
                "country_code = CASE WHEN ranges.country_code = '' THEN excluded.country_code "
                "ELSE ranges.country_code END, "
                "lat = CASE WHEN ranges.lat IS NULL THEN excluded.lat ELSE ranges.lat END, "
                "lon = CASE WHEN ranges.lon IS NULL THEN excluded.lon ELSE ranges.lon END, "
                "asn = CASE WHEN ranges.asn = 0 THEN excluded.asn ELSE ranges.asn END, "
                "provider = CASE WHEN ranges.provider = '' THEN excluded.provider "
                "ELSE ranges.provider END, "
                "confidence = MAX(ranges.confidence, excluded.confidence), "
                "last_seen = excluded.last_seen",
                rows,
            )
            self._conn.commit()
            self._touch()
        return len(rows)

    def mark_service(self, ips: Iterable[str]) -> int:
        now = utcnow()
        count = 0
        with self._lock:
            for ip in ips:
                try:
                    cidr = cidr_for(ip)
                except ValueError:
                    continue
                self._conn.execute(
                    "INSERT INTO ranges (cidr, source, confidence, is_service, first_seen, last_seen) "
                    "VALUES (?, 'override', 1.0, 1, ?, ?) "
                    "ON CONFLICT(cidr) DO UPDATE SET is_service = 1",
                    (cidr, now, now),
                )
                count += 1
            self._conn.commit()
            self._touch()
        return count

    # -- reads -----------------------------------------------------------

    @staticmethod
    def _range_record(r: sqlite3.Row) -> RangeRecord:
        return RangeRecord(
            cidr=r["cidr"],
            city=r["city"],
            region=r["region"],
            region_code=r["region_code"],
            country=r["country"],
            country_code=r["country_code"],
            lat=r["lat"],
            lon=r["lon"],
            is_service=bool(r["is_service"]),
            cluster=cluster_key(dict(r)),
            region_key=region_key(dict(r)),
            source=r["source"],
            asn=r["asn"] or 0,
            provider=r["provider"] or "",
            match_server=r["match_server"] or 0,
        )

    def index(self) -> RangeIndex:
        with self._lock:
            if self._index_dirty:
                rows = self._conn.execute(
                    "SELECT cidr, city, region, region_code, country, country_code, lat, lon, "
                    "is_service, source, asn, provider, match_server "
                    "FROM ranges"
                ).fetchall()
                self._index = RangeIndex(self._range_record(r) for r in rows)
                self._index_dirty = False
                self._dirty_cidrs.clear()
            elif self._dirty_cidrs:
                for cidr in self._dirty_cidrs:
                    row = self._conn.execute(
                        "SELECT cidr, city, region, region_code, country, country_code, lat, lon, "
                        "is_service, source, asn, provider, match_server "
                        "FROM ranges WHERE cidr = ?",
                        (cidr,),
                    ).fetchone()
                    if row is None:
                        self._index.drop(cidr)
                    else:
                        self._index.put(self._range_record(row))
                self._dirty_cidrs.clear()
            return self._index

    def clusters(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM ranges WHERE is_service = 0"
        ).fetchall()
        groups: dict[str, dict] = {}
        for r in rows:
            row = dict(r)
            if row["lat"] is None or row["lon"] is None:
                continue
            key = cluster_key(row)
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "key": key,
                    "city": row["city"],
                    "region": row["region"],
                    "region_code": row["region_code"],
                    "country": row["country"],
                    "country_code": row["country_code"],
                    "lat_sum": 0.0,
                    "lon_sum": 0.0,
                    "cidrs": [],
                    "server_count": 0,
                    "packets": 0,
                    "bytes": 0,
                    "rtt_weighted": 0.0,
                    "rtt_weight": 0,
                    "max_confidence": 0.0,
                    "last_seen": "",
                }
            g["lat_sum"] += row["lat"]
            g["lon_sum"] += row["lon"]
            g["cidrs"].append(row["cidr"])
            g["server_count"] += 1
            g["packets"] += row["packets"]
            g["bytes"] += row["bytes"]
            if row["rtt_ms"]:
                weight = max(1, row["packets"])
                g["rtt_weighted"] += row["rtt_ms"] * weight
                g["rtt_weight"] += weight
            g["max_confidence"] = max(g["max_confidence"], row["confidence"])
            g["last_seen"] = max(g["last_seen"], row["last_seen"])

        out = []
        for g in groups.values():
            n = max(1, g["server_count"])
            out.append(
                {
                    "key": g["key"],
                    "label": g["city"] or "Unknown city",
                    "city": g["city"],
                    "region": g["region"],
                    "region_code": g["region_code"],
                    "region_key": f"{g['country_code'] or 'XX'}-{g['region_code'] or ''}".rstrip("-"),
                    "country": g["country"],
                    "country_code": g["country_code"],
                    "lat": g["lat_sum"] / n,
                    "lon": g["lon_sum"] / n,
                    "cidrs": g["cidrs"],
                    "server_count": g["server_count"],
                    "packets": g["packets"],
                    "bytes": g["bytes"],
                    "avg_rtt_ms": round(g["rtt_weighted"] / g["rtt_weight"]) if g["rtt_weight"] else None,
                    "observed": g["packets"] > 0,
                    "confidence": round(g["max_confidence"], 2),
                    "last_seen": g["last_seen"],
                }
            )
        out.sort(key=lambda c: (-c["bytes"], c["country_code"], c["region_code"], c["city"]))
        return out

    def unlocated_observed(self, limit: int = 120) -> list[str]:
        rows = self._conn.execute(
            "SELECT cidr FROM ranges WHERE packets > 0 AND is_service = 0 "
            "AND (city = '' OR lat IS NULL) ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["cidr"] for r in rows]

    def servers(self, query: str = "", limit: int = 400) -> list[dict]:
        sql = (
            "SELECT cidr, city, region, region_code, country, country_code, lat, lon, source, "
            "asn, provider, confidence, is_service, packets, bytes, rtt_ms, "
            "last_seen FROM ranges "
            "WHERE is_service = 0"
        )
        params: list = []
        if query:
            like = f"%{query}%"
            sql += (
                " AND (cidr LIKE ? OR city LIKE ? OR region LIKE ? OR country LIKE ? "
                "OR provider LIKE ?)"
            )
            params += [like, like, like, like, like]
        sql += " ORDER BY bytes DESC, last_seen DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def providers(self) -> dict:
        """Server counts and observed quality grouped by the hosting provider."""
        rows = self._conn.execute(
            "SELECT provider, asn, region_code, country_code, packets, bytes, rtt_ms "
            "FROM ranges WHERE is_service = 0"
        ).fetchall()
        groups: dict[str, dict] = {}
        unknown = 0
        for row in rows:
            raw = (row["provider"] or "").strip()
            if not raw:
                unknown += 1
                continue
            asn = row["asn"] or 0
            name = provider_label(raw, asn)
            key = name.lower()
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "provider": name,
                    "asns": {},
                    "servers": 0,
                    "regions": set(),
                    "packets": 0,
                    "bytes": 0,
                    "rtt_weighted": 0.0,
                    "rtt_weight": 0,
                }
            if asn:
                g["asns"][asn] = g["asns"].get(asn, 0) + 1
            g["servers"] += 1
            if row["region_code"] and row["country_code"]:
                g["regions"].add(f"{row['country_code']}-{row['region_code']}")
            g["packets"] += row["packets"]
            g["bytes"] += row["bytes"]
            if row["rtt_ms"]:
                weight = max(1, row["packets"])
                g["rtt_weighted"] += row["rtt_ms"] * weight
                g["rtt_weight"] += weight
        out = []
        for g in groups.values():
            out.append(
                {
                    "provider": g["provider"],
                    "asn": max(g["asns"], key=g["asns"].get) if g["asns"] else 0,
                    "servers": g["servers"],
                    "regions": len(g["regions"]),
                    "packets": g["packets"],
                    "bytes": g["bytes"],
                    "avg_rtt_ms": round(g["rtt_weighted"] / g["rtt_weight"])
                    if g["rtt_weight"]
                    else None,
                }
            )
        out.sort(key=lambda p: (-p["servers"], p["provider"]))
        return {"providers": out, "unknown": unknown}

    def record_match(self, match: dict) -> None:
        self.flush()
        with self._lock:
            self._conn.execute(
                "INSERT INTO matches (remote_ip, cidr, city, region, country, started_at, "
                "ended_at, duration_s, bytes, rtt_ms, allowed, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    match["remote_ip"], match["cidr"], match["city"], match["region"],
                    match["country"], match["started_at"], match["ended_at"],
                    match["duration_s"], match["bytes"], match.get("rtt_ms"),
                    1 if match["allowed"] else 0, match.get("reason", ""),
                ),
            )
            self._conn.execute(
                "UPDATE ranges SET match_server = 1 WHERE cidr = ?", (match["cidr"],)
            )
            self._conn.commit()
            self._touch_range(match["cidr"])

    def discovered_ranges(self) -> list[dict]:
        """Ranges this install has actually seen traffic from, bundled or not."""
        self.flush()
        rows = self._conn.execute(
            "SELECT cidr, city, region, region_code, country, country_code, asn, provider, "
            "lat, lon, source, packets, bytes, rtt_ms, first_seen, last_seen FROM ranges "
            "WHERE is_service = 0 AND packets > 0 "
            "ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def matches(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM matches ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) AS ranges, SUM(CASE WHEN packets > 0 THEN 1 ELSE 0 END) AS seen, "
            "SUM(bytes) AS bytes FROM ranges WHERE is_service = 0"
        ).fetchone()
        return {
            "ranges": row["ranges"] or 0,
            "observed_ranges": row["seen"] or 0,
            "bytes": row["bytes"] or 0,
        }

    def export_csv(self, target: Path) -> Path:
        self.flush()
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self._conn.execute(
            "SELECT cidr, city, region, country, country_code, provider, asn, lat, lon, "
            "packets, bytes, rtt_ms, source, confidence, last_seen FROM ranges "
            "ORDER BY bytes DESC"
        ).fetchall()
        with target.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([c for c in rows[0].keys()] if rows else [
                "cidr", "city", "region", "country", "country_code", "provider", "asn",
                "lat", "lon", "packets", "bytes", "rtt_ms", "source", "confidence", "last_seen",
            ])
            for r in rows:
                writer.writerow(list(r))
        return target

    def reset(self) -> None:
        with self._pending_lock:
            self._pending.clear()
        with self._lock:
            self._conn.executescript("DELETE FROM ranges; DELETE FROM matches;")
            self._conn.commit()
            self._touch()
