"""Application state: owns the catalog, policy, engine and workers."""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import catalog_update, games, geo, paths, policy as policy_mod, services, titlebar
from .catalog import Catalog, cidr_for
from .capture import CaptureWorker, available as driver_available
from .engine import Engine
from .policy import PolicyEngine, parse as parse_policy, dump as dump_policy

APP_VERSION = "1.0.3"

DEFAULT_CONFIG = {
    "filtering": True,
    "start_with_windows": False,
    "auto_geolocate": True,
    "show_seed_clusters": False,
    "wizard_done": False,
    "minimize_to_tray": True,
    "streamer_mode": False,
    "theme": "midnight",
}

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "CODPingKing"
LEGACY_RUN_VALUE = "CodRegionFilter"


def clear_legacy_startup() -> None:
    """Drop the pre-rename autostart entry so it cannot launch an old path."""
    if sys.platform != "win32":
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            try:
                winreg.DeleteValue(key, LEGACY_RUN_VALUE)
            except FileNotFoundError:
                pass
    except OSError:
        pass


def _read_json(path: Path, fallback: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(fallback)
    merged = dict(fallback)
    merged.update({k: v for k, v in data.items() if k in fallback})
    return merged


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def set_run_at_startup(enabled: bool) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}" --minimized'
                else:
                    cmd = f'"{sys.executable}" -m cod_region_filter --minimized'
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def relaunch_as_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        if getattr(sys, "frozen", False):
            exe, params = sys.executable, ""
        else:
            exe, params = sys.executable, "-m cod_region_filter"
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return rc > 32
    except Exception:
        return False


class AppState:
    def __init__(self, log=None) -> None:
        self.log = log or (lambda *_: None)
        paths.migrate_legacy()
        self.config = _read_json(paths.config_path(), DEFAULT_CONFIG)
        self.config["theme"] = titlebar.normalise(self.config.get("theme", "midnight"))
        self.catalog = Catalog()
        if self.catalog.is_empty():
            seeded = self.catalog.seed()
            self.log(f"seeded {seeded} ranges")
        self.policy_engine = PolicyEngine(self._load_policy())
        self.engine = Engine(self.catalog, self.policy_engine, self.log)
        self.engine.set_filtering(bool(self.config["filtering"]))
        self.geo = geo.GeoWorker(self._on_geo, self.log)
        self.capture = CaptureWorker(self.engine, self.log)
        self.elevated = games.uac_enabled()
        self.driver = driver_available()
        self.game = games.find_game()
        self.policy_error = ""
        self._stop = threading.Event()
        self._tick_thread: threading.Thread | None = None
        self._shutdown_lock = threading.Lock()
        self._policy_lock = threading.RLock()
        self._clusters: list[dict] = []
        self._regions: list[dict] = []
        self._catalog_lock = threading.Lock()
        self._catalog_update = {"running": False, "message": "", "report": [], "finished_at": 0.0}
        self.refresh_catalog()
        self._pin_services()

    # -- setup -----------------------------------------------------------

    def _load_policy(self) -> policy_mod.Policy:
        path = paths.policy_path()
        if not path.exists():
            p = policy_mod.default_policy()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(dump_policy(p), encoding="utf-8")
            return p
        text = path.read_text(encoding="utf-8")
        parsed, errors = parse_policy(text)
        if parsed is None:
            self.policy_error = "; ".join(errors)
            self.log(f"policy rejected: {self.policy_error}")
            return policy_mod.default_policy()
        return parsed

    def _pin_services(self) -> None:
        try:
            ips = services.resolve_services()
        except Exception as exc:
            self.log(f"service pin skipped: {exc}")
            return
        if ips:
            self.catalog.mark_service(ips)
            self.policy_engine.set_service_ips(ips)

    def start(self) -> None:
        self.geo.start()
        self._tick_thread = threading.Thread(target=self._tick_loop, name="tick", daemon=True)
        self._tick_thread.start()
        if self.driver and self.elevated:
            self.capture.start()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._stop.is_set():
                return
            self._stop.set()
        self.geo.stop()
        self.capture.stop()
        thread = self._tick_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                # Do not close a database another thread is still writing to;
                # the process is on its way out anyway.
                self.log("tick thread did not stop in time")
                return
        try:
            self.catalog.flush()
        except Exception as exc:
            self.log(f"final flush failed: {exc}")
        self.catalog.close()

    # -- workers ---------------------------------------------------------

    def _tick_loop(self) -> None:
        last_flush = 0.0
        last_refresh = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            self.engine.tick()
            if now - last_flush > 2.0:
                last_flush = now
                self.catalog.flush()
            if now - last_refresh > 5.0:
                last_refresh = now
                self.refresh_catalog()
                if self.config.get("auto_geolocate", True):
                    self._queue_geo()
            self._stop.wait(1.0)

    def _queue_geo(self) -> None:
        rows = self.catalog.unlocated_observed(120)
        if rows:
            self.geo.submit(rows)

    def _on_geo(self, cidr: str, result: dict) -> None:
        self.catalog.resolve_location(cidr, result)
        self.refresh_catalog()

    def refresh_catalog(self) -> None:
        self._clusters = self.catalog.clusters()
        self._regions = self._group_regions(self._clusters)
        self.policy_engine.set_clusters(self._clusters)
        self._clusters_by_key = {c["key"]: c for c in self._clusters}

    @staticmethod
    def _group_regions(clusters: list[dict]) -> list[dict]:
        groups: dict[str, dict] = {}
        for c in clusters:
            key = c["region_key"]
            g = groups.get(key)
            if g is None:
                g = groups[key] = {
                    "key": key,
                    "region": c["region"],
                    "region_code": c["region_code"],
                    "country": c["country"],
                    "country_code": c["country_code"],
                    "cluster_count": 0,
                    "server_count": 0,
                    "bytes": 0,
                    "rtt_weighted": 0.0,
                    "rtt_weight": 0,
                    "lat_sum": 0.0,
                    "lon_sum": 0.0,
                    "observed": False,
                }
            g["cluster_count"] += 1
            g["server_count"] += c["server_count"]
            g["bytes"] += c["bytes"]
            g["lat_sum"] += c["lat"]
            g["lon_sum"] += c["lon"]
            g["observed"] = g["observed"] or c["observed"]
            if c.get("avg_rtt_ms"):
                weight = max(1, c["bytes"])
                g["rtt_weighted"] += c["avg_rtt_ms"] * weight
                g["rtt_weight"] += weight
        out = []
        for g in groups.values():
            n = max(1, g["cluster_count"])
            out.append(
                {
                    "key": g["key"],
                    "region": g["region"],
                    "region_code": g["region_code"],
                    "country": g["country"],
                    "country_code": g["country_code"],
                    "cluster_count": g["cluster_count"],
                    "server_count": g["server_count"],
                    "bytes": g["bytes"],
                    "avg_rtt_ms": round(g["rtt_weighted"] / g["rtt_weight"]) if g["rtt_weight"] else None,
                    "lat": g["lat_sum"] / n,
                    "lon": g["lon_sum"] / n,
                    "observed": g["observed"],
                }
            )
        out.sort(key=lambda r: (r["country_code"], r["region"]))
        return out

    # -- policy ----------------------------------------------------------

    def policy_text(self) -> str:
        return dump_policy(self.policy_engine.policy)

    def save_policy_text(self, text: str) -> dict:
        parsed, errors = parse_policy(text)
        if parsed is None:
            return {"ok": False, "errors": errors, "warnings": []}
        p = parsed
        if p.home_lat is None and self.policy_engine.policy.home_lat is not None:
            p.home_lat = self.policy_engine.policy.home_lat
            p.home_lon = self.policy_engine.policy.home_lon
        warnings = policy_mod.lint(p)
        with self._policy_lock:
            paths.policy_path().parent.mkdir(parents=True, exist_ok=True)
            paths.policy_path().write_text(text, encoding="utf-8")
            self.policy_engine.set_policy(p)
            self.policy_error = ""
        self.refresh_catalog()
        return {"ok": True, "errors": [], "warnings": warnings}

    def update_policy(self, mutate) -> dict:
        p = self.policy_engine.policy
        mutate(p)
        text = dump_policy(p)
        return self.save_policy_text(text)

    def set_regions(self, keys: list[str], enabled: bool) -> dict:
        def mutate(p: policy_mod.Policy) -> None:
            current = set(p.allow.regions)
            for key in keys:
                if enabled:
                    current.add(key)
                else:
                    current.discard(key)
            p.allow.regions = sorted(current)

        return self.update_policy(mutate)

    def set_region_blocked(self, key: str, blocked: bool) -> dict:
        def mutate(p: policy_mod.Policy) -> None:
            if blocked:
                p.deny.regions = sorted(set(p.deny.regions) | {key})
                p.allow.regions = [k for k in p.allow.regions if k != key]
            else:
                p.deny.regions = [k for k in p.deny.regions if k != key]
                # Only mirror the unblock into the allowlist when that is the
                # active mode; in blocklist mode it would silently seed allow rules.
                if p.mode == "allowlist":
                    p.allow.regions = sorted(set(p.allow.regions) | {key})

        return self.update_policy(mutate)

    def set_regions_blocked(self, keys: list[str], blocked: bool) -> dict:
        def mutate(p: policy_mod.Policy) -> None:
            denied = set(p.deny.regions)
            if blocked:
                denied |= set(keys)
                p.allow.regions = [k for k in p.allow.regions if k not in keys]
            else:
                denied -= set(keys)
                if p.mode == "allowlist":
                    p.allow.regions = sorted(set(p.allow.regions) | set(keys))
            p.deny.regions = sorted(denied)

        return self.update_policy(mutate)

    def clear_blocked_regions(self) -> dict:
        def mutate(p: policy_mod.Policy) -> None:
            p.deny.regions = []

        return self.update_policy(mutate)

    def block_server(self, ip: str, blocked: bool = True) -> dict:
        """Block or unblock the /24 a single match server lives in."""
        try:
            cidr = cidr_for(ip)
        except ValueError:
            return {"ok": False, "error": f"{ip} is not a valid address."}

        rec = self.catalog.index().find(ip)
        if blocked and (
            (rec is not None and rec.is_service)
            or self.policy_engine.is_service_ip(ip)
        ):
            # Blocking auth, lobby or CDN endpoints breaks online play entirely.
            return {
                "ok": False,
                "error": "This endpoint belongs to a game service and cannot be blocked.",
                "errors": ["Game service endpoints are always allowed."],
                "warnings": [],
            }

        def mutate(p: policy_mod.Policy) -> None:
            cidrs = set(p.deny.cidrs)
            if blocked:
                cidrs.add(cidr)
            else:
                cidrs.discard(cidr)
            p.deny.cidrs = sorted(cidrs)

        result = self.update_policy(mutate)
        result["cidr"] = cidr
        return result

    def set_radius(self, km: float) -> dict:
        def mutate(p: policy_mod.Policy) -> None:
            p.allow.radius_km = max(0.0, float(km))

        return self.update_policy(mutate)

    def set_home(self, lat: float, lon: float) -> dict:
        def mutate(p: policy_mod.Policy) -> None:
            p.home_lat = float(lat)
            p.home_lon = float(lon)

        return self.update_policy(mutate)

    def detect_home(self) -> dict:
        result = geo.lookup_self()
        if not result:
            return {"ok": False, "error": "Location lookup failed"}
        self.set_home(result["lat"], result["lon"])
        return {"ok": True, "home": result}

    def streamer_mode(self) -> bool:
        return bool(self.config.get("streamer_mode"))

    def home_payload(self) -> dict:
        p = self.policy_engine.policy
        home = {"lat": p.home_lat, "lon": p.home_lon, "label": "Not set", "hidden": False}
        if p.home_lat is None or p.home_lon is None:
            return home
        if self.streamer_mode():
            # Coordinates also drive the map marker and radius ring, so they are
            # withheld rather than merely relabelled.
            return {
                "lat": None,
                "lon": None,
                "label": "Hidden by streamer mode",
                "hidden": True,
            }
        home["label"] = f"{p.home_lat:.2f}, {p.home_lon:.2f}"
        # Cluster centroids give the coordinates a place name, so a home moved by
        # an accidental map click is obvious instead of just a number.
        nearest = None
        for cluster in self._clusters:
            if cluster.get("lat") is None or cluster.get("lon") is None:
                continue
            distance = policy_mod.haversine_km(
                p.home_lat, p.home_lon, cluster["lat"], cluster["lon"]
            )
            if nearest is None or distance < nearest[0]:
                nearest = (distance, cluster)
        if nearest and nearest[0] <= 150:
            place = ", ".join(
                part for part in (nearest[1].get("city"), nearest[1].get("region")) if part
            )
            if place:
                home["near"] = place
                home["label"] = f"{place} ({home['label']})"
        return home

    def clear_home(self) -> dict:
        def mutate(p: policy_mod.Policy) -> None:
            p.home_lat = None
            p.home_lon = None
            p.allow.radius_km = 0.0

        return self.update_policy(mutate)

    def apply_recommended(self, max_rtt: int = 40) -> dict:
        """Allow regions under the RTT threshold, or the nearest ones before any data exists."""

        home_lat = self.policy_engine.policy.home_lat
        home_lon = self.policy_engine.policy.home_lon

        def mutate(p: policy_mod.Policy) -> None:
            chosen = {
                r["key"] for r in self._regions
                if r.get("avg_rtt_ms") is not None and r["avg_rtt_ms"] <= max_rtt
            }
            if not chosen and home_lat is not None and home_lon is not None:
                located = [r for r in self._regions if r.get("lat") is not None]
                located.sort(
                    key=lambda r: policy_mod.haversine_km(home_lat, home_lon, r["lat"], r["lon"])
                )
                chosen = {r["key"] for r in located[:6]}
            if not chosen:
                # Fresh seed has no measured RTT and may not have a home yet; start
                # with the densest regions so the policy is never empty.
                densest = sorted(self._regions, key=lambda r: r["server_count"], reverse=True)
                chosen = {r["key"] for r in densest[:6]}
            p.allow.regions = sorted(chosen)
            p.mode = "allowlist"
            # An allowlist that admits unknown ranges is not an allowlist. New
            # ranges are denied until geolocation classifies them; soft mode
            # remains the explicit, time-limited escape hatch.
            p.unknown = "deny"

        result = self.update_policy(mutate)
        self.config["wizard_done"] = True
        _write_json(paths.config_path(), self.config)
        return result

    # -- settings --------------------------------------------------------

    def set_setting(self, key: str, value) -> dict:
        if key not in DEFAULT_CONFIG:
            return {"ok": False, "error": f"unknown setting {key}"}
        self.config[key] = value
        _write_json(paths.config_path(), self.config)

        if key == "filtering":
            self.engine.set_filtering(bool(value))
        elif key == "start_with_windows":
            set_run_at_startup(bool(value))
        elif key == "auto_geolocate" and value:
            self._queue_geo()
        return {"ok": True, "config": self.config}

    def set_theme(self, name: str) -> dict:
        name = titlebar.normalise(name)
        if name not in titlebar.THEMES:
            return {"ok": False, "error": f"unknown theme {name}"}
        result = self.set_setting("theme", name)
        titlebar.apply(name)
        return result

    def set_soft_mode(self, enabled: bool, seconds: int) -> dict:
        def mutate(p: policy_mod.Policy) -> None:
            p.soft_mode.enabled = bool(enabled)
            p.soft_mode.relax_after_s = max(10, int(seconds))

        return self.update_policy(mutate)

    def set_unknown(self, mode: str) -> dict:
        if mode not in ("allow", "deny"):
            return {"ok": False, "errors": ["unknown must be allow or deny"], "warnings": []}

        def mutate(p: policy_mod.Policy) -> None:
            p.unknown = mode

        return self.update_policy(mutate)

    # -- reporting -------------------------------------------------------

    def clusters_payload(self) -> list[dict]:
        policy = self.policy_engine.policy
        allowed_keys = set(self.policy_engine.allowed_cluster_keys())
        allow_regions = set(policy.allow.regions)
        allow_countries = set(policy.allow.countries)
        out = []
        for c in self._clusters:
            in_allow = (
                c["key"] in allowed_keys
                or c["region_key"] in allow_regions
                or c["country_code"] in allow_countries
            )
            item = dict(c)
            item["in_allow"] = in_allow
            item["allowed"] = self.policy_engine.decide("0.0.0.0", None, c)["allow"]
            out.append(item)
        return out

    def regions_payload(self) -> list[dict]:
        policy = self.policy_engine.policy
        clusters = self.clusters_payload()
        in_allow_regions = {c["region_key"] for c in clusters if c["in_allow"]}
        permitted_regions = {c["region_key"] for c in clusters if c["allowed"]}
        deny_regions = set(policy.deny.regions)
        deny_countries = set(policy.deny.countries)
        out = []
        for r in self._regions:
            item = dict(r)
            item["allowed"] = r["key"] in in_allow_regions
            item["blocked"] = (
                r["key"] in deny_regions
                or r["country_code"] in deny_countries
            )
            item["permitted"] = r["key"] in permitted_regions
            item["label"] = r["region"] or r["country"] or "Unknown"
            out.append(item)
        return out

    def servers_payload(self, query: str = "", limit: int = 400) -> list[dict]:
        index = self.catalog.index()
        streamer = self.streamer_mode()
        out = []
        for row in self.catalog.servers(query, limit):
            rec = index.find(row["cidr"].split("/")[0])
            verdict = self.policy_engine.decide(row["cidr"].split("/")[0], rec)
            row = dict(row)
            row["region_key"] = rec.region_key if rec else ""
            row["allowed"] = verdict["allow"]
            row["verdict"] = verdict["reason"]
            if streamer:
                row["cidr"] = "Hidden range"
            out.append(row)
        return out

    def matches_payload(self, limit: int = 10) -> list[dict]:
        rows = self.catalog.matches(limit)
        engine = self.policy_engine
        denied = set(engine.policy.deny.cidrs) if engine.policy else set()
        index = self.catalog.index()
        streamer = self.streamer_mode()
        out = []
        for row in rows:
            rec = index.find(row["remote_ip"])
            if rec is not None and rec.is_service:
                continue
            row["blocked"] = row.get("cidr") in denied
            if streamer:
                row["remote_ip"] = "Hidden"
                row["cidr"] = "Hidden range"
            out.append(row)
        return out

    def status(self) -> dict:
        game = games.find_game()
        self.game = game
        capture = self.capture.status
        if not self.elevated:
            capture["status"] = "administrator required"
        elif not self.driver:
            capture["status"] = "driver package missing"
        engine_state = self.engine.state()
        streamer = self.streamer_mode()
        if streamer:
            if engine_state.get("current"):
                engine_state["current"]["remote_ip"] = "Hidden"
                engine_state["current"]["cidr"] = "Hidden range"
            for verdict in engine_state.get("recent", []):
                verdict["ip"] = "Hidden"
        return {
            "state": engine_state,
            "game": game,
            "capture": capture,
            "elevated": self.elevated,
            "driver": self.driver,
            "policy_error": self.policy_error,
            "summary": self.policy_engine.summary(),
            "catalog_update": dict(self._catalog_update),
            "streamer": streamer,
        }

    def update_catalog(self) -> dict:
        with self._catalog_lock:
            if self._catalog_update["running"]:
                return {"ok": False, "error": "An update is already running."}
            self._catalog_update.update(
                running=True, message="Downloading public server lists...", report=[], finished_at=0.0
            )
        threading.Thread(target=self._run_catalog_update, name="catalog-update", daemon=True).start()
        return {"ok": True}

    def _run_catalog_update(self) -> None:
        try:
            data = catalog_update.collect()
            # Insert every range first so the unlocated ones become visible to
            # the geo worker; located rows then overwrite the blanks.
            self.catalog.import_geo(
                [{"cidr": cidr} for cidr in data["ranges"]], "community", 0.15
            )
            if data["records"]:
                self.catalog.import_geo(data["records"], "community_geo", 0.55)
            self.refresh_catalog()
            reachable = [entry for entry in data["report"] if entry["ok"]]
            if reachable:
                message = f"Catalog updated: {len(data['ranges']):,} ranges from {len(reachable)} source(s)."
            else:
                message = "No public source could be reached."
            report = data["report"]
        except Exception as exc:
            self.log(f"catalog update failed: {exc}")
            message = f"Catalog update failed: {exc}"
            report = []
        with self._catalog_lock:
            self._catalog_update.update(
                running=False, message=message, report=report, finished_at=time.time()
            )

    def export_csv(self) -> dict:
        target = paths.roaming_dir() / "server-catalog.csv"
        try:
            path = self.catalog.export_csv(target)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(path)}

    def export_discovered(self) -> dict:
        if self.streamer_mode():
            return {"ok": False, "error": "Turn off streamer mode to export server addresses."}
        rows = self.catalog.discovered_ranges()
        docs = Path.home() / "Documents"
        target_dir = docs if docs.is_dir() else Path.home()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = target_dir / f"cod-ping-king-discovered-{stamp}.json"
        payload = {
            "format": "cod-ping-king-discovered",
            "version": 1,
            "app_version": APP_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(rows),
            "ranges": rows,
        }
        try:
            target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(target), "count": len(rows)}

    def export_policy(self) -> dict:
        if self.streamer_mode():
            return {"ok": False, "error": "Turn off streamer mode to export your policy."}
        docs = Path.home() / "Documents"
        target_dir = docs if docs.is_dir() else Path.home()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = target_dir / f"cod-ping-king-policy-{stamp}.yaml"
        try:
            target.write_text(self.policy_text(), encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "path": str(target)}

    def reset_catalog(self) -> dict:
        self.catalog.reset()
        self.refresh_catalog()
        return {"ok": True}
