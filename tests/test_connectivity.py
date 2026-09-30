from edge_agent.connectivity import ConnectivityMonitor, Mode


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_starts_offline_and_goes_online_on_connect():
    clock = FakeClock()
    m = ConnectivityMonitor(offline_grace=2.0, clock=clock)
    assert m.mode is Mode.OFFLINE

    m.mark_connected()
    assert m.update() is Mode.ONLINE
    assert m.update() is None


def test_switches_offline_only_after_grace():
    clock = FakeClock()
    m = ConnectivityMonitor(offline_grace=2.0, clock=clock)
    m.mark_connected()
    m.update()

    clock.t = 10.0
    m.mark_disconnected()
    clock.t = 11.9
    assert m.update() is None
    assert m.mode is Mode.ONLINE

    clock.t = 12.0
    assert m.update() is Mode.OFFLINE


def test_short_blip_is_ignored():
    clock = FakeClock()
    m = ConnectivityMonitor(offline_grace=2.0, clock=clock)
    m.mark_connected()
    m.update()

    clock.t = 5.0
    m.mark_disconnected()
    clock.t = 6.0
    m.mark_connected()
    clock.t = 20.0
    assert m.update() is None
    assert m.mode is Mode.ONLINE


def test_repeated_disconnect_callbacks_keep_first_timestamp():
    clock = FakeClock()
    m = ConnectivityMonitor(offline_grace=2.0, clock=clock)
    m.mark_connected()
    m.update()

    clock.t = 10.0
    m.mark_disconnected()
    clock.t = 11.5
    m.mark_disconnected()  # 재연결 실패 콜백이 또 와도 끊긴 시점은 10.0 유지
    clock.t = 12.0
    assert m.update() is Mode.OFFLINE
