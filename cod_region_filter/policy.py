"""Policy file: parsing, validation, compilation and packet verdicts."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

import yaml

from .catalog import cidr_for

MODES = ("allowlist", "blocklist")
UNKNOWN_CHOICES = ("allow", "deny")


class PolicyError(ValueError):
    pass


@dataclass
class AllowSpec:
    regions: list[str] = field(default_factory=list)
    clusters: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    pins: list[str] = field(default_factory=list)
    radius_km: float = 0.0


@dataclass
class DenySpec:
    regions: list[str] = field(default_factory=list)
    clusters: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    cidrs: list[str] = field(default_factory=list)


@dataclass
class SoftMode:
    enabled: bool = True
    relax_after_s: int = 45


@dataclass
class Policy:
    mode: str = "allowlist"
    unknown: str = "allow"
    home_lat: float | None = None
    home_lon: float | None = None
    allow: AllowSpec = field(default_factory=AllowSpec)
    deny: DenySpec = field(default_factory=DenySpec)
    max_rtt_ms: int = 0
    soft_mode: SoftMode = field(default_factory=SoftMode)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _str_list(value, name: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.append(f"{name} must be a list of strings")
        return []
    return [v.strip() for v in value if v.strip()]


def _mapping(value, name: str, errors: list[str]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        errors.append(f"{name} must be a mapping")
        return {}
    return value


def parse(text: str) -> tuple[Policy | None, list[str]]:
    errors: list[str] = []
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}: " if mark else ""
        return None, [f"{where}{getattr(exc, 'problem', exc)}"]

    if not isinstance(raw, dict):
        return None, ["policy must be a mapping at the top level"]

    policy = Policy()
    policy.mode = str(raw.get("mode", "allowlist")).strip().lower()
    if policy.mode not in MODES:
        errors.append(f"mode must be one of {', '.join(MODES)}")
    policy.unknown = str(raw.get("unknown", "allow")).strip().lower()
    if policy.unknown not in UNKNOWN_CHOICES:
        errors.append(f"unknown must be one of {', '.join(UNKNOWN_CHOICES)}")

    home = _mapping(raw.get("home"), "home", errors)
    if home and home.get("lat") is not None and home.get("lon") is not None:
        try:
            policy.home_lat = float(home["lat"])
            policy.home_lon = float(home["lon"])
        except (KeyError, TypeError, ValueError):
            errors.append("home needs numeric lat and lon")
    elif home and (home.get("lat") is None) != (home.get("lon") is None):
        errors.append("home needs numeric lat and lon")

    allow = _mapping(raw.get("allow"), "allow", errors)
    policy.allow.regions = _str_list(allow.get("regions"), "allow.regions", errors)
    policy.allow.clusters = _str_list(allow.get("clusters"), "allow.clusters", errors)
    policy.allow.countries = [c.upper() for c in _str_list(allow.get("countries"), "allow.countries", errors)]
    policy.allow.pins = _str_list(allow.get("pins"), "allow.pins", errors)
    try:
        policy.allow.radius_km = float(allow.get("radius_km", 0) or 0)
        if policy.allow.radius_km < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("allow.radius_km must be a positive number")
        policy.allow.radius_km = 0.0

    deny = _mapping(raw.get("deny"), "deny", errors)
    policy.deny.regions = _str_list(deny.get("regions"), "deny.regions", errors)
    policy.deny.clusters = _str_list(deny.get("clusters"), "deny.clusters", errors)
    policy.deny.countries = [c.upper() for c in _str_list(deny.get("countries"), "deny.countries", errors)]
    policy.deny.cidrs = _str_list(deny.get("cidrs"), "deny.cidrs", errors)

    try:
        policy.max_rtt_ms = int(raw.get("max_rtt_ms", 0) or 0)
        if policy.max_rtt_ms < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("max_rtt_ms must be a positive integer")
        policy.max_rtt_ms = 0

    soft = _mapping(raw.get("soft_mode"), "soft_mode", errors)
    if soft:
        policy.soft_mode.enabled = bool(soft.get("enabled", True))
        try:
            policy.soft_mode.relax_after_s = max(10, int(soft.get("relax_after_s", 45)))
        except (TypeError, ValueError):
            errors.append("soft_mode.relax_after_s must be an integer")
            policy.soft_mode.relax_after_s = 45

    known = {
        "mode", "unknown", "home", "allow", "deny", "max_rtt_ms", "soft_mode",
    }
    for key in raw:
        if key not in known:
            errors.append(f"unknown key: {key}")

    return (None if errors else policy), errors


def dump(policy: Policy) -> str:
    data = {
        "mode": policy.mode,
        "unknown": policy.unknown,
        "home": {"lat": policy.home_lat, "lon": policy.home_lon},
        "allow": {
            "regions": sorted(policy.allow.regions),
            "clusters": sorted(policy.allow.clusters),
            "countries": sorted(policy.allow.countries),
            "radius_km": policy.allow.radius_km,
            "pins": sorted(policy.allow.pins),
        },
        "deny": {
            "regions": sorted(policy.deny.regions),
            "clusters": sorted(policy.deny.clusters),
            "countries": sorted(policy.deny.countries),
            "cidrs": sorted(policy.deny.cidrs),
        },
        "max_rtt_ms": policy.max_rtt_ms,
        "soft_mode": {
            "enabled": policy.soft_mode.enabled,
            "relax_after_s": policy.soft_mode.relax_after_s,
        },
    }
    header = (
        "# COD Ping King policy\n"
        "# mode: allowlist blocks every located server that is not allowed below.\n"
        "# unknown: what to do with endpoints the catalog has not located yet.\n"
        "# Edits apply immediately; the app validates and rejects broken files.\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, default_flow_style=False)


def lint(p: Policy) -> list[str]:
    warnings: list[str] = []
    if p.mode == "allowlist" and not (
        p.allow.regions or p.allow.clusters or p.allow.countries
        or p.allow.pins or p.allow.radius_km
    ):
        warnings.append("No regions are allowed. Every located server will be blocked.")
    if p.allow.radius_km and p.home_lat is None:
        warnings.append("Radius is set but no home location is known.")
    return warnings


def default_policy() -> Policy:
    # Blocklist with no deny rules blocks nothing, so a fresh install cannot
    # cut the game off before the setup wizard picks regions.
    return Policy(mode="blocklist")


class PolicyEngine:
    """Compiles allow/deny rules against the current catalog clusters."""

    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or default_policy()
        self._clusters: dict[str, dict] = {}
        self._radius_keys: set[str] = set()
        self._allowed: set[str] = set()
        self._denied: set[str] = set()
        self._pins: set[str] = set()
        self._service_ips: set[str] = set()

    def set_service_ips(self, ips: list[str]) -> None:
        self._service_ips = set(ips)

    def is_service_ip(self, ip: str) -> bool:
        return ip in self._service_ips

    def set_policy(self, policy: Policy) -> None:
        self.policy = policy
        self._compile()

    def set_clusters(self, clusters: Iterable[dict]) -> None:
        self._clusters = {c["key"]: c for c in clusters}
        self._compile()

    def _compile(self) -> None:
        p = self.policy
        self._radius_keys = set()
        if p.home_lat is not None and p.home_lon is not None and p.allow.radius_km > 0:
            for key, cluster in self._clusters.items():
                if cluster.get("lat") is None or cluster.get("lon") is None:
                    continue
                dist = haversine_km(p.home_lat, p.home_lon, cluster["lat"], cluster["lon"])
                if dist <= p.allow.radius_km:
                    self._radius_keys.add(key)

        self._allowed = set(p.allow.clusters) | self._radius_keys
        self._denied = set(p.deny.clusters)
        self._pins = set()

    def _allow_match(self, cluster: dict | None, rec) -> str | None:
        if cluster is None:
            return None
        p = self.policy
        if cluster["key"] in self._allowed:
            return "radius" if cluster["key"] in self._radius_keys else "cluster"
        if cluster.get("region_key") and cluster["region_key"] in p.allow.regions:
            return "region"
        if cluster.get("country_code") and cluster["country_code"].upper() in p.allow.countries:
            return "country"
        if rec is not None and rec.cidr in p.allow.pins:
            return "pin"
        return None

    def decide(self, ip: str, rec=None, cluster: dict | None = None) -> dict:
        p = self.policy
        if ip in self._service_ips:
            return {"allow": True, "reason": "Game service endpoint", "kind": "service"}
        if rec is not None and rec.is_service:
            return {"allow": True, "reason": "Game service endpoint", "kind": "service"}

        located = (rec is not None and bool(rec.city)) or bool(
            cluster is not None and cluster.get("city")
        )
        cluster = cluster or (self._clusters.get(rec.cluster) if rec is not None else None)

        if rec is not None and (rec.cidr in p.deny.cidrs):
            return {"allow": False, "reason": f"Denied range {rec.cidr}", "kind": "deny"}
        if rec is None:
            # A blocked range must stay blocked before it is ever catalogued.
            cidr = cidr_for(ip)
            if cidr in p.deny.cidrs:
                return {"allow": False, "reason": f"Denied range {cidr}", "kind": "deny"}
        if rec is not None and rec.cluster in self._denied:
            return {"allow": False, "reason": "Region denied", "kind": "deny"}
        if rec is not None and cluster is not None and cluster.get("region_key") in p.deny.regions:
            return {"allow": False, "reason": f"Denied region {cluster['region_key']}", "kind": "deny"}
        if rec is not None and cluster is not None and cluster.get("country_code") in p.deny.countries:
            return {"allow": False, "reason": f"Denied country {cluster['country_code']}", "kind": "deny"}

        allowed_by = self._allow_match(cluster, rec)
        if allowed_by:
            return {"allow": True, "reason": f"Allowed by {allowed_by}", "kind": allowed_by}

        if p.max_rtt_ms and cluster is not None and cluster.get("avg_rtt_ms"):
            if cluster["avg_rtt_ms"] > p.max_rtt_ms:
                return {"allow": False, "reason": f"RTT {cluster['avg_rtt_ms']} ms over limit", "kind": "rtt"}

        if p.mode == "allowlist":
            if located:
                return {"allow": False, "reason": "Not in allowlist", "kind": "allowlist"}
            return {
                "allow": p.unknown == "allow",
                "reason": "Unclassified endpoint" if p.unknown == "allow" else "Unclassified endpoint blocked",
                "kind": "unknown",
            }

        return {
            "allow": p.unknown == "allow",
            "reason": "Not in any rule",
            "kind": "unknown",
        }

    def allowed_cluster_keys(self) -> set[str]:
        return {
            key for key, cluster in self._clusters.items()
            if self.decide("0.0.0.0", None, cluster)["allow"]
        }

    def summary(self) -> dict:
        p = self.policy
        located = [
            c for c in self._clusters.values()
            if c.get("lat") is not None and c.get("city")
        ]
        allowed = 0
        for c in located:
            if self.decide("0.0.0.0", rec=None, cluster=c)["allow"]:
                allowed += 1
        return {
            "mode": p.mode,
            "unknown": p.unknown,
            "clusters": len(located),
            "allowed_clusters": allowed,
            "radius_keys": len(self._radius_keys),
            "max_rtt_ms": p.max_rtt_ms,
            "service_pins": len(self._service_ips),
            "empty_allowlist": p.mode == "allowlist" and not (
                p.allow.regions or p.allow.clusters or p.allow.countries
                or p.allow.pins or p.allow.radius_km
            ),
            "allow": {
                "regions": sorted(p.allow.regions),
                "clusters": sorted(p.allow.clusters),
                "countries": sorted(p.allow.countries),
                "pins": sorted(p.allow.pins),
                "radius_km": p.allow.radius_km,
            },
            "deny": {
                "regions": sorted(p.deny.regions),
                "clusters": sorted(p.deny.clusters),
                "countries": sorted(p.deny.countries),
                "cidrs": sorted(p.deny.cidrs),
            },
            "soft": {
                "enabled": p.soft_mode.enabled,
                "relax_after_s": p.soft_mode.relax_after_s,
            },
        }
