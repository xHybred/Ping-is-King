"""Traffic state: flow tracking, match detection, soft mode, verdicts."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .catalog import Catalog, cidr_for
from .policy import PolicyEngine

MATCH_MIN_SECONDS = 5.0
MATCH_MIN_PACKETS = 40
MATCH_IDLE_END_S = 15.0
RTT_MAX_S = 0.5


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


@dataclass
class Flow:
    ip: str
    port: int
    first_seen: float
    last_seen: float
    packets: int = 0
    bytes: int = 0
    outbound: int = 0
    inbound: int = 0
    last_out: float | None = None
    last_in: float | None = None
    rtt_samples: deque[float] = field(default_factory=lambda: deque(maxlen=32))
    verdict: dict = field(default_factory=dict)

    def rtt_ms(self) -> float | None:
        if not self.rtt_samples:
            return None
        ordered = sorted(self.rtt_samples)
        return ordered[len(ordered) // 2] * 1000

    def score(self) -> float:
        return float(self.bytes)

    def label(self) -> str:
        return f"{self.ip}:{self.port}"


class Engine:
    def __init__(self, catalog: Catalog, policy: PolicyEngine, log=None) -> None:
        self.catalog = catalog
        self.policy = policy
        self.log = log or (lambda *_: None)
        self._lock = threading.RLock()
        self._flows: dict[tuple[str, int], Flow] = {}
        self._filtering = True
        self._current: Flow | None = None
        self._current_started = 0.0
        self._last_match_ended = 0.0
        self._searching_since: float | None = None
        self._soft_active = False
        self._soft_pinned_ip: str | None = None
        self._soft_reason = ""
        self._denied_total = 0
        self._allowed_total = 0
        self._recent_verdicts: deque[dict] = deque(maxlen=50)
        self._match_count = 0

    # -- controls --------------------------------------------------------

    def set_filtering(self, on: bool) -> None:
        with self._lock:
            self._filtering = bool(on)
            if on:
                self._searching_since = time.monotonic()
            else:
                self._soft_active = False
                self._searching_since = None

    @property
    def filtering(self) -> bool:
        return self._filtering

    @property
    def soft_active(self) -> bool:
        return self._soft_active

    def _soft_relaxed(self, now: float | None = None) -> bool:
        p = self.policy.policy
        if not p.soft_mode.enabled:
            return False
        if self._soft_active:
            return True
        if self._searching_since is None:
            return False
        now = now if now is not None else time.monotonic()
        if now - self._searching_since < p.soft_mode.relax_after_s:
            return False
        self._soft_active = True
        self._soft_reason = f"No match for {p.soft_mode.relax_after_s}s; unclassified endpoints allowed"
        return True

    # -- packet path -----------------------------------------------------

    def observe(self, ip: str, port: int, direction: str, size: int, now: float | None = None) -> dict:
        """Decide one packet. Called from the capture thread."""
        now = now if now is not None else time.monotonic()
        with self._lock:
            key = (ip, port)
            flow = self._flows.get(key)
            if flow is None:
                flow = self._flows[key] = Flow(ip=ip, port=port, first_seen=now, last_seen=now)
            flow.last_seen = now
            flow.packets += 1
            flow.bytes += size

            rtt_sample = None
            if direction == "outbound":
                flow.outbound += 1
                flow.last_out = now
            else:
                flow.inbound += 1
                flow.last_in = now
                if flow.last_out is not None:
                    gap = now - flow.last_out
                    if 0 < gap <= RTT_MAX_S:
                        flow.rtt_samples.append(gap)
                        rtt_sample = gap * 1000

            self.catalog.observe(ip, size, rtt_sample)

            if not self._filtering:
                flow.verdict = {"allow": True, "reason": "Filtering off", "kind": "off"}
                return flow.verdict

            rec = self.catalog.index().find(ip)
            cluster = self.policy._clusters.get(rec.cluster) if rec is not None else None
            unclassified = rec is None or not rec.city
            if self._soft_pinned_ip == ip and unclassified:
                verdict = {"allow": True, "reason": "Soft mode held for current match", "kind": "soft"}
            else:
                verdict = self.policy.decide(ip, rec, cluster)

            # Soft mode is a search guard, not a policy override: it may admit
            # endpoints we know nothing about, never one we have located and
            # deliberately excluded.
            if not verdict["allow"] and verdict.get("kind") == "unknown" and self._soft_relaxed(now):
                verdict = {"allow": True, "reason": "Soft mode", "kind": "soft"}

            flow.verdict = verdict
            if verdict["allow"]:
                self._allowed_total += 1
            else:
                self._denied_total += 1
                self._recent_verdicts.appendleft(
                    {
                        "ip": ip,
                        "port": port,
                        "reason": verdict["reason"],
                        "at": _iso(time.time()),
                    }
                )
            return verdict

    # -- match detection -------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._prune(now)
            candidate = self._pick_candidate(now)
            if candidate is not None:
                if self._current is None:
                    self._current = candidate
                    self._current_started = candidate.first_seen
                    if self._soft_active:
                        self._soft_pinned_ip = candidate.ip
                    self._soft_active = False
                    self._soft_reason = ""
                    self._searching_since = None
                elif candidate is not self._current:
                    if candidate.score() > self._current.score() * 1.5:
                        self._finish_match(now)
                        self._current = candidate
                        self._current_started = candidate.first_seen
            elif self._current is not None and now - self._current.last_seen > MATCH_IDLE_END_S:
                self._finish_match(now)

            if self._current is None and self._filtering and self._searching_since is None:
                self._searching_since = now

    def _pick_candidate(self, now: float) -> Flow | None:
        best = None
        for flow in self._flows.values():
            if now - flow.last_seen > 3.0:
                continue
            if flow.first_seen and now - flow.first_seen < MATCH_MIN_SECONDS:
                continue
            if flow.packets < MATCH_MIN_PACKETS:
                continue
            rec = self.catalog.index().find(flow.ip)
            if rec is not None and rec.is_service:
                continue
            if best is None or flow.score() > best.score():
                best = flow
        return best

    def _finish_match(self, now: float) -> None:
        flow = self._current
        self._current = None
        if flow is None:
            return
        rec = self.catalog.index().find(flow.ip)
        match = {
            "remote_ip": flow.ip,
            "cidr": cidr_for(flow.ip),
            "city": rec.city if rec else "",
            "region": rec.region if rec else "",
            "country": rec.country if rec else "",
            "started_at": _iso(time.time() - (now - self._current_started)),
            "ended_at": _iso(time.time()),
            "duration_s": int(now - self._current_started),
            "bytes": flow.bytes,
            "rtt_ms": round(flow.rtt_ms()) if flow.rtt_ms() else None,
            "allowed": bool(flow.verdict.get("allow", True)),
            "reason": flow.verdict.get("reason", ""),
        }
        self.catalog.record_match(match)
        self._recent_verdicts.appendleft(
            {
                "ip": flow.ip,
                "port": flow.port,
                "reason": f"Match ended after {match['duration_s']}s",
                "at": match["ended_at"],
            }
        )
        self._match_count += 1
        self._last_match_ended = now
        self._soft_pinned_ip = None
        self._searching_since = now if self._filtering else None

    def _prune(self, now: float) -> None:
        stale = [k for k, f in self._flows.items() if now - f.last_seen > 60.0]
        for k in stale:
            if self._flows[k] is self._current:
                continue
            del self._flows[k]

    # -- reporting -------------------------------------------------------

    def current_match(self) -> dict | None:
        with self._lock:
            flow = self._current
            if flow is None:
                return None
            rec = self.catalog.index().find(flow.ip)
            rtt = flow.rtt_ms()
            return {
                "remote_ip": flow.ip,
                "port": flow.port,
                "cidr": cidr_for(flow.ip),
                "city": rec.city if rec else "",
                "region": rec.region if rec else "",
                "region_key": rec.region_key if rec else "",
                "country": rec.country if rec else "",
                "duration_s": int(time.monotonic() - self._current_started),
                "bytes": flow.bytes,
                "rtt_ms": round(rtt) if rtt else None,
                "allowed": bool(flow.verdict.get("allow", True)),
                "reason": flow.verdict.get("reason", ""),
            }

    def state(self) -> dict:
        searching = (
            int(time.monotonic() - self._searching_since) if self._searching_since else None
        )
        return {
            "filtering": self._filtering,
            "soft_active": self._soft_active,
            "soft_reason": self._soft_reason,
            "searching_s": searching,
            "allowed_packets": self._allowed_total,
            "denied_packets": self._denied_total,
            "match_count": self._match_count,
            "current": self.current_match(),
            "recent": list(self._recent_verdicts)[:20],
        }
