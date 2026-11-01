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
