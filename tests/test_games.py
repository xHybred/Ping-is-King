"""Executable names change between Call of Duty releases, so the matcher has
to accept new ones without code changes and still ignore lookalikes."""

from __future__ import annotations

import pytest

from cod_region_filter import games


def test_matches_current_titles():
    for name in (
        "cod.exe",
        "ModernWarfare.exe",
        "BlackOpsColdWar.exe",
        "cod_hq.exe",
        "cod24-cod.exe",
        "cod25-cod.exe",
        "cod26-cod.exe",
    ):
        assert games.matches(name), name


def test_matches_rejects_campaign_and_lookalikes():
    for name in ("sp24-cod.exe", "sp25-cod.exe", "code.exe", "notepad.exe", ""):
        assert games.matches(name) == "", name


def test_find_games_filters_process_list(monkeypatch):
    processes = [
        {"pid": 10, "name": "cod25-cod.exe", "exe": r"C:\Games\CoD\cod25-cod.exe"},
        {"pid": 11, "name": "sp25-cod.exe", "exe": r"C:\Games\CoD\sp25-cod.exe"},
        {"pid": 12, "name": "steam.exe", "exe": r"C:\Steam\steam.exe"},
    ]
    monkeypatch.setattr(games.psutil, "process_iter", lambda attrs: [_Proc(p) for p in processes])

    found = games.find_games()

    assert [game["pid"] for game in found] == [10]
    assert found[0]["match"] == "name pattern"


def test_find_game_prefers_process_with_udp_ports(monkeypatch):
    processes = [
        {"pid": 20, "name": "cod_hq.exe", "exe": r"C:\Games\CoD\cod_hq.exe"},
        {"pid": 21, "name": "cod25-cod.exe", "exe": r"C:\Games\CoD\cod25-cod.exe"},
    ]
    monkeypatch.setattr(games.psutil, "process_iter", lambda attrs: [_Proc(p) for p in processes])
    monkeypatch.setattr(games, "udp_ports_map", lambda pids: {20: [], 21: [3074]})

    game = games.find_game()

    assert game["pid"] == 21


class _Proc:
    """Minimal psutil.Process stand-in for process_iter."""

    def __init__(self, info: dict) -> None:
        self.info = info


@pytest.mark.parametrize("name", ["cod.exe", "COD24-COD.EXE"])
def test_matching_is_case_insensitive(name):
    assert games.matches(name)
