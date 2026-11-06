"""Traffic state: flow tracking, match detection, soft mode, verdicts."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .catalog import Catalog, cidr_for, provider_label
from .policy import PolicyEngine

MATCH_MIN_SECONDS = 5.0
MATCH_MIN_PACKETS = 40
MATCH_IDLE_END_S = 15.0
RTT_MAX_S = 0.5

# Safe scope. Nothing is dropped until a flow looks like a live session, so
# matchmaking probes and lobby chatter still get through.
SESSION_MIN_PACKETS = 12
SESSION_MIN_SECONDS = 0.8

# Remote ports used for control traffic rather than a match: the game's own
# port (Demonware lobby, party, matchmaking), Steam networking, and STUN for
# voice. These are never treated as a match, so a busy lobby relay cannot be
# mistaken for a server and dropped mid-queue. The upper bound is above Steam's
# documented 27050 because its datagram relays answer on 27051-27139, and
# dropping those leaves the game on "Waiting for relay".
PLATFORM_PORTS = frozenset({3074, 3478, 4379, 4380})
PLATFORM_PORT_RANGE = (27000, 27199)

# A denied match that ends sooner than this was cancelled while joining, not
# played. Worth telling the user about instead of silently returning to menu.
CANCEL_NOTICE_S = 60

# Quiet this long and the next packet counts as a new search rather than a
# continuation of an old one.
IDLE_RESET_S = 60.0


def is_platform_port(port: int) -> bool:
    return port in PLATFORM_PORTS or PLATFORM_PORT_RANGE[0] <= port <= PLATFORM_PORT_RANGE[1]


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
    cidr: str = ""

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
        self._current_rec = None
        self._last_match_ended = 0.0
        self._searching_since: float | None = None
        self._soft_active = False
        self._soft_pinned_ip: str | None = None
        self._soft_reason = ""
        self._denied_total = 0
        self._allowed_total = 0
        self._recent_verdicts: deque[dict] = deque(maxlen=50)
        self._denial_log: dict[str, float] = {}
        self._index_cache = None
        self._index_version = -1
        self._match_count = 0
        self._cancelled_total = 0
        self._last_cancelled: dict | None = None
        self._scan_start = 0.0
        self._scan_count = 0
        self._last_activity = 0.0

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
        self.log(f"search guard: {self._soft_reason}")
        return True

    def _touch_activity(self, now: float) -> None:
        """Restart the search guard after the game has been quiet for a while.

        The timer starts when filtering starts, so an app left open while the
        player sits in the menu used to count as a fruitless search and relax
        before the first queue.
        """
        if (
            self._filtering
            and self._current is None
            and self._last_activity
            and now - self._last_activity > IDLE_RESET_S
        ):
            if self._soft_active or self._searching_since is not None:
                self.log("search guard: idle, timer restarted")
            self._searching_since = now
            self._soft_active = False
            self._soft_pinned_ip = None
            self._soft_reason = ""
        self._last_activity = now

    # -- packet path -----------------------------------------------------

    def _record(self, ip: str):
        """Look up a range from the cached index.

        Catalog.index() takes the catalog lock and rebuilds on a data change.
        Doing that per packet puts the capture thread behind SQLite whenever
        the catalog moves, which shows up in game as packet bursts.
        """
        version = self.catalog.version
        if self._index_cache is None or self._index_version != version:
            self._index_cache = self.catalog.index()
            self._index_version = version
        return self._index_cache.find(ip)

    def observe(
        self,
        ip: str,
        port: int,
        direction: str,
        size: int,
        now: float | None = None,
        count: bool = True,
    ) -> dict:
        """Decide one packet. Called from the capture thread.

        ``count`` is False for sniffed inbound copies: they feed flow state and
        RTT but are not enforcement events, so they must not inflate the
        allowed/denied totals or the denial log.
        """
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._touch_activity(now)
            key = (ip, port)
            flow = self._flows.get(key)
            if flow is None:
                flow = self._flows[key] = Flow(
                    ip=ip, port=port, first_seen=now, last_seen=now, cidr=cidr_for(ip)
                )
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

            self.catalog.observe(ip, size, rtt_sample, cidr=flow.cidr)

            if not self._filtering:
                flow.verdict = {"allow": True, "reason": "Filtering off", "kind": "off"}
                return flow.verdict

            rec = self._record(ip)
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

            verdict = self._scope(flow, verdict, now, rec)

            flow.verdict = verdict
            if not count:
                return verdict
            if verdict["allow"]:
                self._allowed_total += 1
            else:
                self._denied_total += 1
                self._log_denial(ip, port, verdict, now, flow)
                self._recent_verdicts.appendleft(
                    {
                        "ip": ip,
                        "port": port,
                        "reason": verdict["reason"],
                        "at": _iso(time.time()),
                    }
                )
            return verdict

    def observe_inbound(self, ip: str, port: int, size: int, packets: int = 1, now: float | None = None) -> None:
        """Record sniffed inbound traffic without deciding anything.

        Called from the sniffer thread for every sampled copy. Running the full
        verdict path here competes with outbound re-injection for the engine
        lock and the GIL, which is what made some players rubber-band.
        """
        now = now if now is not None else time.monotonic()
        with self._lock:
            self._touch_activity(now)
            key = (ip, port)
            flow = self._flows.get(key)
            if flow is None:
                flow = self._flows[key] = Flow(
                    ip=ip, port=port, first_seen=now, last_seen=now, cidr=cidr_for(ip)
                )
            flow.last_seen = now
            flow.packets += packets
            flow.bytes += size
            flow.inbound += packets
            flow.last_in = now

            rtt_sample = None
            if flow.last_out is not None:
                gap = now - flow.last_out
                if 0 < gap <= RTT_MAX_S:
                    flow.rtt_samples.append(gap)
                    rtt_sample = gap * 1000

            self.catalog.observe(ip, size, rtt_sample, cidr=flow.cidr)

    def _scope(self, flow: Flow, verdict: dict, now: float, rec) -> dict:
        """Keep automatic filtering away from endpoints that are not a session.

        Matchmaking, lobby and party traffic is short-lived and often lands on
        cloud ranges the server lists have never seen. Dropping those broke
        matchmaking for some players, so an endpoint we cannot place gets a
        short grace window. Once we do know where it is, the policy applies on
        the first packet; letting a located, denied server run for a second was
        long enough for the game to commit and load the map.
        Explicit rules (deny lists, per-match blocks) always apply.
        """
        if verdict["allow"]:
            return verdict
        if verdict.get("kind") == "deny":
            return verdict
        if is_platform_port(flow.port):
            # Lobby, party and voice traffic shares these ports with match
            # servers. Dropping an idle heartbeat here breaks matchmaking, and
            # in practice the 3074 servers outside the allowlist were control
            # traffic, not matches. A match server on 3074 can still be blocked
            # from its row in Recent matches, which writes an explicit deny.
            # Provider rules land above as kind "provider", not "deny", so a
            # host block cannot strand relay traffic here.
            return {"allow": True, "reason": "Platform traffic", "kind": "platform"}
        if rec is not None and (rec.city or rec.known_server):
            return verdict
        busy = (
            flow.packets >= SESSION_MIN_PACKETS
            and now - flow.first_seen >= SESSION_MIN_SECONDS
        )
        if busy:
            return verdict
        return {"allow": True, "reason": "Not a game session yet", "kind": "grace"}

    def platform_ports(self, ip: str) -> list[int]:
        """Platform ports this endpoint has used, for block confirmations."""
        with self._lock:
            return sorted(
                {port for (flow_ip, port) in self._flows if flow_ip == ip and is_platform_port(port)}
            )

    def _log_denial(self, ip: str, port: int, verdict: dict, now: float, flow: Flow) -> None:
        last = self._denial_log.get(ip)
        if last is not None and now - last < 60.0:
            return
        if len(self._denial_log) > 512:
            self._denial_log.clear()
        self._denial_log[ip] = now
        elapsed = max(now - flow.first_seen, 0.001)
        rate = flow.packets / elapsed
        self.log(
            f"blocked {ip}:{port} ({verdict['reason']}, {verdict.get('kind', '?')}, "
            f"{flow.packets} pkts, {rate:.0f}/s)"
        )
        if now - self._scan_start > 2.0:
            self._scan_start = now
            self._scan_count = 0
        self._scan_count += 1
        if self._scan_count == 10:
            # A queue scan rejects dozens of datacenters in a second; say so
            # once instead of leaving a reader to count the lines.
            self.log("blocked 10 endpoints in under 2s (matchmaking scan)")

    # -- match detection -------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        finished: list[dict] = []
        started: list[Flow] = []
        with self._lock:
            self._prune(now)
            candidate = self._pick_candidate(now)
            if candidate is not None:
                if self._current is None:
                    self._current = candidate
                    self._current_started = now
                    self._current_rec = None
                    started.append(candidate)
                    if self._soft_active:
                        self._soft_pinned_ip = candidate.ip
                    self._soft_active = False
                    self._soft_reason = ""
                    self._searching_since = None
                elif candidate is not self._current:
                    if candidate.score() > self._current.score() * 1.5:
                        done = self._finish_match(now)
                        if done is not None:
                            finished.append(done)
                        self._current = candidate
                        self._current_started = now
                        self._current_rec = None
                        started.append(candidate)
            elif self._current is not None and now - self._current.last_seen > MATCH_IDLE_END_S:
                done = self._finish_match(now)
                if done is not None:
                    finished.append(done)

            if self._current is None and self._filtering and self._searching_since is None:
                self._searching_since = now

        # Recording a match writes and commits. Doing it outside the engine
        # lock keeps incoming packets from waiting on SQLite.
        for item in finished:
            self._record_finished(item)

        # Resolving the current server also reads the catalog. The dashboard
        # asks for it every two seconds, so looking it up there would put a
        # database read behind the packet thread's lock.
        for flow in started:
            rec = self.catalog.index().find(flow.ip)
            with self._lock:
                if self._current is flow:
                    self._current_rec = rec
            self.log(
                f"match start {flow.ip}:{flow.port} ({self._place(rec)})"
            )

    def _pick_candidate(self, now: float) -> Flow | None:
        best = None
        for flow in self._flows.values():
            if now - flow.last_seen > 3.0:
                continue
            if is_platform_port(flow.port):
                continue
            if flow.first_seen and now - flow.first_seen < MATCH_MIN_SECONDS:
                continue
            if flow.packets < MATCH_MIN_PACKETS:
                continue
            rec = self._record(flow.ip)
            if rec is not None and rec.is_service:
                continue
            if best is None or flow.score() > best.score():
                best = flow
        return best

    @staticmethod
    def _place(rec) -> str:
        if rec is None or not rec.city:
            return "unclassified"
        return ", ".join(part for part in (rec.city, rec.region) if part)

    def _finish_match(self, now: float) -> dict | None:
        flow = self._current
        self._current = None
        self._current_rec = None
        if flow is None:
            return None
        started = self._current_started
        match = {
            "remote_ip": flow.ip,
            "cidr": cidr_for(flow.ip),
            "city": "",
            "region": "",
            "country": "",
            "started_at": _iso(time.time() - (now - started)),
            "ended_at": _iso(time.time()),
            "duration_s": int(now - started),
            "bytes": flow.bytes,
            "rtt_ms": round(flow.rtt_ms()) if flow.rtt_ms() else None,
            "allowed": bool(flow.verdict.get("allow", True)),
            "reason": flow.verdict.get("reason", ""),
        }
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
        return {"match": match, "port": flow.port, "ip": flow.ip}

    def _record_finished(self, item: dict) -> None:
        match = item["match"]
        rec = self.catalog.index().find(match["remote_ip"])
        if rec is not None:
            match["city"] = rec.city
            match["region"] = rec.region
            match["country"] = rec.country
        self.catalog.record_match(match)
        self.log(
            f"match end {match['remote_ip']}:{item['port']} ({self._place(rec)}) "
            f"after {match['duration_s']}s allowed={str(match['allowed']).lower()} "
            f"({match['reason']})"
        )
        if not match["allowed"] and match["duration_s"] < CANCEL_NOTICE_S:
            with self._lock:
                self._cancelled_total += 1
                self._last_cancelled = {
                    "ip": match["remote_ip"],
                    "cidr": match["cidr"],
                    "city": match["city"],
                    "region": match["region"],
                    "country": match["country"],
                    "duration_s": match["duration_s"],
                    "reason": match["reason"],
                    "at": time.time(),
                }

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
            rec = self._current_rec
            rtt = flow.rtt_ms()
            return {
                "remote_ip": flow.ip,
                "port": flow.port,
                "cidr": cidr_for(flow.ip),
                "city": rec.city if rec else "",
                "region": rec.region if rec else "",
                "region_key": rec.region_key if rec else "",
                "country": rec.country if rec else "",
                "provider": provider_label(rec.provider, rec.asn) if rec else "",
                "asn": rec.asn if rec else 0,
                "duration_s": int(time.monotonic() - self._current_started),
                "bytes": flow.bytes,
                "rtt_ms": round(rtt) if rtt else None,
                "allowed": bool(flow.verdict.get("allow", True)),
                "reason": flow.verdict.get("reason", ""),
                "rule_kind": flow.verdict.get("kind", ""),
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
            "cancelled_total": self._cancelled_total,
            "last_cancelled": dict(self._last_cancelled) if self._last_cancelled else None,
            "current": self.current_match(),
            "recent": list(self._recent_verdicts)[:20],
        }
