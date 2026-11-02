from cod_region_filter.capture import driver_problem


def test_running_driver_has_no_problem():
    assert driver_problem({"running": True, "available": True, "status": "filtering"}) == ""


def test_waiting_for_game_is_not_a_driver_problem():
    status = {"running": False, "available": True, "status": "waiting for game"}
    assert driver_problem(status) == ""


def test_open_failure_reports_windivert_error():
    status = {"running": False, "available": True, "status": "driver error", "error": "Access is denied."}
    assert driver_problem(status) == "Access is denied."


def test_missing_package_explains_reinstall():
    status = {"running": False, "available": False}
    assert "missing" in driver_problem(status)


def test_filter_watches_every_game_port_including_3074():
    from cod_region_filter.capture import filter_ports

    assert filter_ports((3074, 27015, 3478, 30012, 44998)) == (3074, 27015, 3478, 30012, 44998)


def test_skip_3074_compatibility_switch():
    from cod_region_filter.capture import filter_ports

    assert filter_ports((3074, 30012), skip_3074=True) == (30012,)


def test_safe_attach_waits_for_stable_ports():
    from cod_region_filter.capture import CaptureWorker

    worker = CaptureWorker(engine=object(), attach_mode="safe")
    assert worker._settled(0.0, (30012,)) is False
    assert worker._settled(5.0, (30012,)) is False
    assert worker._settled(9.9, (30012, 30022)) is False
    assert worker._settled(20.0, (30012, 30022)) is True


def test_settle_is_not_reapplied_after_the_first_attach():
    from cod_region_filter.capture import CaptureWorker

    worker = CaptureWorker(engine=object(), attach_mode="safe")
    assert worker._settled(0.0, (30012,)) is False
    assert worker._settled(10.0, (30012,)) is True
    worker._attached_once = True
    # A voice socket appearing mid-match must not detach filtering for 10s.
    assert worker._settled(11.0, (30012, 30099)) is True


def test_immediate_attach_skips_the_wait():
    from cod_region_filter.capture import CaptureWorker

    worker = CaptureWorker(engine=object(), attach_mode="immediate")
    assert worker._settled(0.0, (30012,)) is True


def test_pause_marks_the_worker_paused():
    from cod_region_filter.capture import CaptureWorker

    worker = CaptureWorker(engine=object())
    worker.pause()
    assert worker.status["paused"] is True
    worker.resume()
    assert worker.status["paused"] is False


def test_unload_driver_stops_and_deletes_the_service(monkeypatch):
    from cod_region_filter import capture

    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    monkeypatch.setattr(capture.time, "sleep", lambda *_: None)
    assert capture.unload_driver() is True
    assert ["sc.exe", "stop", "WinDivert"] in calls
    assert ["sc.exe", "delete", "WinDivert"] in calls


def test_port_filter_targets_one_direction():
    from cod_region_filter.capture import port_filter

    assert port_filter((30012, 44998), "SrcPort") == "udp and (udp.SrcPort == 30012 or udp.SrcPort == 44998)"
    assert port_filter((30012,), "DstPort") == "udp and (udp.DstPort == 30012)"


def test_sniffed_inbound_does_not_count_as_a_verdict(tmp_path):
    from cod_region_filter.catalog import Catalog
    from cod_region_filter.engine import Engine
    from cod_region_filter.policy import AllowSpec, Policy, PolicyEngine

    catalog = Catalog(tmp_path / "catalog.db")
    policy = PolicyEngine(Policy(mode="allowlist", unknown="deny", allow=AllowSpec(regions=["US-OH"])))
    engine = Engine(catalog, policy)
    engine.observe("198.51.100.7", 30012, "outbound", 120, now=1000.0)
    engine.observe("198.51.100.7", 30012, "inbound", 120, now=1000.1, count=False)
    engine.observe_inbound("198.51.100.7", 30012, 480, packets=4, now=1000.2)
    state = engine.state()
    assert state["allowed_packets"] + state["denied_packets"] == 1
    assert engine._flows[("198.51.100.7", 30012)].inbound == 5
    catalog.close()


def test_catalog_version_changes_after_flush(tmp_path):
    from cod_region_filter.catalog import Catalog

    catalog = Catalog(tmp_path / "catalog.db")
    before = catalog.version
    catalog.observe("45.63.9.9", 500, 20.0)
    catalog.flush()
    assert catalog.version > before
    catalog.close()


def test_target_refresh_publishes_a_snapshot(monkeypatch):
    from cod_region_filter import capture

    monkeypatch.setattr(
        capture.games,
        "find_games",
        lambda: [{"pid": 10, "name": "cod.exe", "match": "exact name"}],
    )
    monkeypatch.setattr(
        capture.games,
        "udp_ports_map",
        lambda pids: {10: [30012, 3074]},
    )
    worker = capture.CaptureWorker(engine=object())
    assert worker._refresh_target() is True
    ports, pid, pids, game = worker._snapshot()
    assert ports == (3074, 30012)
    assert pid == 10
    assert pids == (10,)
    assert game["name"] == "cod.exe"


def test_read_log_returns_the_tail(tmp_path, monkeypatch):
    from cod_region_filter import paths, state as state_mod

    log = tmp_path / "app.log"
    log.write_text("x" * 300 + "END", encoding="utf-8")
    monkeypatch.setattr(paths, "log_path", lambda: log)
    monkeypatch.setattr(state_mod.paths, "log_path", lambda: log)
    app = state_mod.AppState.__new__(state_mod.AppState)
    result = app.read_log(limit=10)
    assert result["ok"] is True
    assert result["text"] == "xxxxxxxxEN" or result["text"].endswith("END")
    assert len(result["text"]) == 10
