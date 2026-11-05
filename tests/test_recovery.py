"""복구 리포트 (roomait-v1 recovery) 와 클라우드 heartbeat."""

from conftest import event_types, make_agent

from edge_agent.config import AgentConfig
from edge_agent.connectivity import Mode
from edge_agent.devices import WINDOW, MockActuator, Reading


class StuffySensor:
    def read(self) -> Reading:
        return Reading(measured_at=0.0, temperature=24.0, humidity=50.0, co2=1500.0, pm25=10.0, pm10=20.0)


def go_offline(agent) -> None:
    agent._on_broker_disconnected()
    agent.monitor._disconnected_since -= 10  # 유예 시간 지남
    agent._check_mode()
    assert agent.monitor.mode is Mode.OFFLINE


def test_recovery_lists_completed_local_actions_before_status():
    window = MockActuator(WINDOW)
    agent = make_agent({WINDOW: window}, sensor=StuffySensor(), online=True)
    go_offline(agent)

    agent._sample()  # CO2 1500 -> 창문 열기
    agent._check_pending()  # 완료 확인 -> local action 기록
    agent.mqtt.sent.clear()
    agent._on_broker_connected()
    agent._check_mode()

    topics = [t for t, _, _ in agent.mqtt.sent]
    assert topics.index(agent.mqtt.topic_recovery) < topics.index(agent.mqtt.topic_status)
    (report,) = agent.mqtt.on(agent.mqtt.topic_recovery)
    assert report["offlineSince"] <= report["recoveredAt"]
    (action,) = report["localActions"]
    assert action["deviceId"] == "window-1"
    assert action["state"] == "OPEN"
    assert "co2" in action["reason"]
    assert report["offlineSince"] <= action["executedAt"] <= report["recoveredAt"]


def test_action_not_completed_is_not_reported():
    class StuckWindow(MockActuator):
        def set(self, on: bool) -> None:
            pass  # 명령은 받았지만 움직이지 않음

    agent = make_agent({WINDOW: StuckWindow(WINDOW)}, sensor=StuffySensor(), online=True)
    go_offline(agent)
    agent._sample()
    agent._check_pending()
    agent._on_broker_connected()
    agent._check_mode()

    (report,) = agent.mqtt.on(agent.mqtt.topic_recovery)
    assert report["localActions"] == []


def test_reported_actions_are_not_sent_twice():
    agent = make_agent({WINDOW: MockActuator(WINDOW)}, sensor=StuffySensor(), online=True)
    go_offline(agent)
    agent._sample()
    agent._check_pending()
    agent._on_broker_connected()
    agent._check_mode()

    go_offline(agent)
    agent._on_broker_connected()
    agent._check_mode()

    reports = agent.mqtt.on(agent.mqtt.topic_recovery)
    assert len(reports) == 2
    assert reports[1]["localActions"] == []


def test_first_connection_after_boot_sends_no_recovery():
    agent = make_agent({}, online=True)

    assert agent.mqtt.on(agent.mqtt.topic_recovery) == []
    assert "reconnected" not in event_types(agent)


def test_auto_control_does_not_resend_while_moving():
    class SlowWindow(MockActuator):
        def __init__(self) -> None:
            super().__init__(WINDOW)
            self.calls = 0

        def set(self, on: bool) -> None:
            self.calls += 1  # 아직 도착 안 함

    window = SlowWindow()
    agent = make_agent({WINDOW: window}, sensor=StuffySensor())
    agent._sample()
    agent._sample()

    assert window.calls == 1


# --- 클라우드 heartbeat ------------------------------------------------------


def lease_agent():
    return make_agent({}, cfg=AgentConfig(cloud_heartbeat_timeout=5.0))


def test_broker_alone_is_not_enough_when_lease_enabled():
    agent = lease_agent()

    agent._on_broker_connected()
    agent._check_mode()

    assert agent.monitor.mode is Mode.OFFLINE


def test_cloud_heartbeat_brings_agent_online():
    agent = lease_agent()
    agent._on_broker_connected()

    agent._handle_cloud_status({"online": True})
    agent._check_mode()

    assert agent.monitor.mode is Mode.ONLINE


def test_missing_cloud_heartbeat_goes_offline_with_broker_up():
    agent = lease_agent()
    agent._on_broker_connected()
    agent._handle_cloud_status({"online": True})
    agent._check_mode()

    agent._cloud_seen -= 10  # heartbeat 끊김
    agent._check_cloud_lease()
    agent.monitor._disconnected_since -= 10  # 유예 시간 지남
    agent._check_mode()

    assert agent.monitor.mode is Mode.OFFLINE
    # 브로커는 살아 있으니 오프라인 모드임을 바로 알린다
    agent._publish_status()
    assert agent.mqtt.on(agent.mqtt.topic_status)[-1] == {"online": True, "offlineMode": True}


def test_cloud_lwt_goes_offline():
    agent = lease_agent()
    agent._on_broker_connected()
    agent._handle_cloud_status({"online": True})
    agent._check_mode()

    agent._handle_cloud_status({"online": False})
    agent.monitor._disconnected_since -= 10
    agent._check_mode()

    assert agent.monitor.mode is Mode.OFFLINE
