"""Bridge exposed to the webview UI as window.pywebview.api."""

from __future__ import annotations

import os
import threading
import time

from . import games, paths
from .state import APP_VERSION, AppState, relaunch_as_admin


class Api:
    def __init__(self, state: AppState) -> None:
        self.state = state

    def bootstrap(self) -> dict:
        status = self.state.status()
        streamer = status["streamer"]
        if streamer:
            shown_paths = {
                "policy": r"%APPDATA%\cod-ping-king\policy.yaml",
                "catalog": r"%LOCALAPPDATA%\cod-ping-king\catalog.db",
                "log": r"%LOCALAPPDATA%\cod-ping-king\app.log",
            }
        else:
            shown_paths = {
                "policy": str(paths.policy_path()),
                "catalog": str(paths.db_path()),
                "log": str(paths.log_path()),
            }
        return {
            "version": APP_VERSION,
            "elevated": status["elevated"],
            "driver": status["driver"],
            "driver_error": status["driver_error"],
            "game": status["game"],
            "config": self.state.config,
            "policy_text": "" if streamer else self.state.policy_text(),
            "streamer": streamer,
            "policy_error": status["policy_error"],
            "policy_home": self.state.home_payload(),
            "summary": status["summary"],
            "state": status["state"],
            "capture": status["capture"],
            "catalog_update": status["catalog_update"],
            "update": status["update"],
            "matches": self.state.matches_payload(10),
            "stats": self.state.catalog.stats(),
            "regions": self.state.regions_payload(),
            "clusters": self.state.clusters_payload(),
            "servers": self.state.servers_payload(limit=200),
            "providers": self.state.providers_payload(),
            "paths": shown_paths,
        }

    def poll(self) -> dict:
        status = self.state.status()
        return {
            "state": status["state"],
            "game": status["game"],
            "capture": status["capture"],
            "elevated": status["elevated"],
            "driver": status["driver"],
            "driver_error": status["driver_error"],
            "summary": status["summary"],
            "catalog_update": status["catalog_update"],
            "update": status["update"],
            "matches": self.state.matches_payload(10),
            "regions": self.state.regions_payload(),
        }

    def save_policy(self, text: str) -> dict:
        return self.state.save_policy_text(text)

    def validate_policy(self, text: str) -> dict:
        from .policy import lint, parse

        parsed, errors = parse(text or "")
        if parsed is None:
            return {"ok": False, "errors": errors, "warnings": []}
        return {"ok": True, "errors": [], "warnings": lint(parsed)}

    def set_region_blocked(self, key: str, blocked: bool) -> dict:
        return self.state.set_region_blocked(str(key), bool(blocked))

    def set_regions_blocked(self, keys: list[str], blocked: bool) -> dict:
        return self.state.set_regions_blocked(list(keys), bool(blocked))

    def clear_blocked_regions(self) -> dict:
        return self.state.clear_blocked_regions()

    def block_server(self, ip: str, blocked: bool = True) -> dict:
        return self.state.block_server(str(ip), bool(blocked))

    def set_regions(self, keys: list[str], enabled: bool) -> dict:
        return self.state.set_regions(list(keys), bool(enabled))

    def set_radius(self, km) -> dict:
        try:
            return self.state.set_radius(float(km))
        except (TypeError, ValueError):
            return {"ok": False, "errors": ["Radius must be a number"], "warnings": []}

    def set_home(self, lat, lon) -> dict:
        try:
            return self.state.set_home(float(lat), float(lon))
        except (TypeError, ValueError):
            return {"ok": False, "errors": ["Invalid coordinates"], "warnings": []}

    def detect_home(self) -> dict:
        return self.state.detect_home()

    def clear_home(self) -> dict:
        return self.state.clear_home()

    def apply_recommended(self, max_rtt: int = 40) -> dict:
        return self.state.apply_recommended(int(max_rtt))

    def set_setting(self, key: str, value) -> dict:
        return self.state.set_setting(key, value)

    def set_theme(self, name: str) -> dict:
        return self.state.set_theme(name)

    def set_soft_mode(self, enabled: bool, seconds: int) -> dict:
        return self.state.set_soft_mode(bool(enabled), int(seconds))

    def set_unknown(self, mode: str) -> dict:
        return self.state.set_unknown(mode)

    def set_provider_rule(self, provider: str, rule: str) -> dict:
        return self.state.set_provider_rule(provider, rule)

    def set_filtering(self, on: bool) -> dict:
        return self.state.set_setting("filtering", bool(on))

    def restart_capture(self) -> dict:
        return self.state.restart_capture()

    def export_csv(self) -> dict:
        return self.state.export_csv()

    def read_log(self) -> dict:
        return self.state.read_log()

    def export_discovered(self) -> dict:
        return self.state.export_discovered()

    def export_policy(self) -> dict:
        return self.state.export_policy()

    def export_diagnostics(self, text: str) -> dict:
        return self.state.export_diagnostics(text)

    def reset_catalog(self) -> dict:
        return self.state.reset_catalog()

    def remove_firewall_rules(self) -> dict:
        return self.state.remove_firewall_rules()

    def update_catalog(self) -> dict:
        return self.state.update_catalog()

    def check_updates(self) -> dict:
        return self.state.check_updates(manual=True)

    def download_update(self) -> dict:
        return self.state.download_update()

    def install_update(self) -> dict:
        result = self.state.install_update()
        if result.get("ok"):
            # The helper waits for this process to exit before running Setup.
            threading.Thread(target=self._quit_soon, name="update-quit", daemon=True).start()
        return result

    def open_folder(self, which: str = "policy") -> dict:
        target = {
            "policy": paths.roaming_dir(),
            "catalog": paths.local_data_dir(),
            "log": paths.local_data_dir(),
        }.get(which)
        if target is None:
            return {"ok": False, "error": "unknown folder"}
        try:
            os.startfile(str(target))  # type: ignore[attr-defined]
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    def relaunch_admin(self) -> dict:
        if games.uac_enabled():
            return {"ok": True, "already": True}
        ok = relaunch_as_admin()
        if ok:
            # the elevated instance takes over; give it a moment to start
            self.state.config["filtering"] = True
        return {"ok": ok}

    def quit(self) -> dict:
        # Answer the UI first, then exit from a worker. os._exit skips pywebview
        # teardown on purpose; the previous taskkill approach left ghost tray
        # icons and sometimes failed entirely.
        threading.Thread(target=self._quit_soon, name="api-quit", daemon=True).start()
        return {"ok": True}

    def _quit_soon(self) -> None:
        time.sleep(0.25)
        from .app import hard_exit

        hard_exit(self.state)
