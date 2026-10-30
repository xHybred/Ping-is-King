"""Approximate geolocation for observed endpoints.

Runs in a background thread so a slow or rate-limited lookup never blocks
packet handling. Failures are silent: an unresolved endpoint stays
unclassified and is handled by the policy's unknown rule.

ipwho.is is used over HTTPS because its free tier allows it. The previous
provider only offered plaintext on the free plan, which exposed lookups and
let a network path spoof locations. Free tier is 1,000 requests/day per
client address; only unclassified game ranges are ever looked up.
"""

from __future__ import annotations

import ipaddress
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://ipwho.is"
FIELDS = "success,country,country_code,region,region_code,city,latitude,longitude"
BATCH = 20
MIN_INTERVAL_S = 0.25
RATE_LIMIT_S = 300.0


def _normalise(data: dict) -> dict | None:
    """Map ipwho.is fields onto the keys the catalog already understands."""
    if not data.get("success"):
        return None
    return {
        "status": "success",
        "country": data.get("country") or "",
        "countryCode": data.get("country_code") or "",
        "regionName": data.get("region") or "",
        "region": data.get("region_code") or "",
        "city": data.get("city") or "",
        "lat": data.get("latitude"),
        "lon": data.get("longitude"),
    }


def _fetch(ip: str, timeout: float) -> dict | None:
    url = f"{BASE}/{urllib.parse.quote(ip)}?fields={FIELDS}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return _normalise(json.loads(resp.read().decode()))


def lookup(cidrs: list[str], timeout: float = 8.0) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for cidr in cidrs:
        net = ipaddress.ip_network(cidr, strict=False)
        probe = str(net.network_address + 1)
        try:
            result = _fetch(probe, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise
            continue
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if result:
            out[cidr] = result
        time.sleep(MIN_INTERVAL_S)
    return out


def lookup_self(timeout: float = 8.0) -> dict | None:
    """Approximate the user's location from their public address."""
    try:
        with urllib.request.urlopen(f"{BASE}/?fields={FIELDS}", timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not data.get("success"):
        return None
    return {
        "city": data.get("city") or "",
        "region": data.get("region") or "",
        "country": data.get("country") or "",
        "lat": data.get("latitude"),
        "lon": data.get("longitude"),
    }


class GeoWorker(threading.Thread):
    def __init__(self, on_result, log=None) -> None:
        super().__init__(name="geo", daemon=True)
        self._queue: queue.Queue[str] = queue.Queue()
        self._seen: set[str] = set()
        self._on_result = on_result
        self._log = log or (lambda *_: None)
        self._stop = threading.Event()
        self._last_call = 0.0
        self._lock = threading.Lock()

    def submit(self, cidrs: list[str]) -> None:
        with self._lock:
            for cidr in cidrs:
                if cidr not in self._seen:
                    self._seen.add(cidr)
                    self._queue.put(cidr)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            batch: list[str] = []
            try:
                batch.append(self._queue.get(timeout=1.0))
            except queue.Empty:
                continue
            while len(batch) < BATCH:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break

            wait = MIN_INTERVAL_S - (time.monotonic() - self._last_call)
            if wait > 0:
                self._stop.wait(wait)
            if self._stop.is_set():
                return

            try:
                self._last_call = time.monotonic()
                results = lookup(batch)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    # Free tier resets daily. Retrying immediately only deepens
                    # the limit, so park the queue until the next window.
                    self._log("geolocation rate limit reached; pausing lookups")
                    self._stop.wait(RATE_LIMIT_S)
                else:
                    self._log(f"geolocation failed: {exc}")
                with self._lock:
                    for cidr in batch:
                        self._seen.discard(cidr)
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._log(f"geolocation failed: {exc}")
                with self._lock:
                    for cidr in batch:
                        self._seen.discard(cidr)
                continue

            for cidr, res in results.items():
                try:
                    self._on_result(cidr, res)
                except Exception as exc:  # never kill the worker on a consumer bug
                    self._log(f"geolocation apply failed: {exc}")
