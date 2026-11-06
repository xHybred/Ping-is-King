from cod_region_filter.netwatch import NetWatch


class FakeEngine:
    def __init__(self) -> None:
        self.calls = []

    def observe(self, ip, port, direction, size):
        self.calls.append((ip, port, direction, size))


def worker(engine):
    watch = NetWatch(engine)
    watch._pids = (123,)
    return watch


def test_udp_send_event_reaches_the_engine():
    engine = FakeEngine()
    worker(engine)._on_event(
        (42, {"PID": "123", "size": "100", "daddr": "45.63.9.9", "dport": "3074"})
    )
    assert engine.calls == [("45.63.9.9", 3074, "outbound", 100)]


def test_udp_receive_event_uses_the_source_address():
    engine = FakeEngine()
    worker(engine)._on_event(
        (43, {"PID": "123", "size": "80", "saddr": "45.63.9.9", "sport": "3074"})
    )
    assert engine.calls == [("45.63.9.9", 3074, "inbound", 80)]


def test_other_processes_and_events_are_ignored():
    engine = FakeEngine()
    watch = worker(engine)
    watch._on_event((42, {"PID": "999", "daddr": "45.63.9.9", "dport": "3074"}))
    watch._on_event((10, {"PID": "123", "daddr": "45.63.9.9", "dport": "3074"}))
    assert engine.calls == []


def test_bad_event_shape_is_ignored():
    engine = FakeEngine()
    watch = worker(engine)
    watch._on_event(None)
    watch._on_event(("nope", {}))
    watch._on_event((42, {}))
    assert engine.calls == []
