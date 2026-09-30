"""센서/액추에이터가 실패해도 에이전트가 죽지 않는지 확인."""

from edge_agent.agent import EdgeAgent
from edge_agent.config import AgentConfig
from edge_agent.devices import WINDOW, MockActuator, Reading
from edge_agent.storage import LocalStore


class FlakySensor:
    """지정한 횟수만큼 실패한 뒤 정상값을 돌려준다."""

    def __init__(self, failures: int) -> None:
        self.failures = failures

    def read(self) -> Reading:
        if self.failures > 0:
            self.failures -= 1
            raise IOError("PMS7003: valid frame not received")
        return Reading(measured_at=0.0, temperature=24.0, humidity=50.0, co2=1500.0, pm25=10.0)


class BrokenActuator(MockActuator):
    def set(self, on: bool) -> None:
        raise OSError("GPIO busy")


def make_agent(sensor, actuators) -> EdgeAgent:
    return EdgeAgent(AgentConfig(), sensor, actuators, LocalStore(":memory:"))


def event_types(agent: EdgeAgent) -> list[str]:
    return [row["type"] for row in agent.store.unsynced_events()]


def test_sensor_failure_is_logged_once_and_recovery_is_reported():
    agent = make_agent(FlakySensor(failures=3), {WINDOW: MockActuator(WINDOW)})

    for _ in range(3):
        agent._sample()
    assert agent.store.unsynced_readings() == []
    assert event_types(agent) == ["sensor_error"]  # 연속 실패는 한 번만 기록

    agent._sample()
    assert len(agent.store.unsynced_readings()) == 1
    # 복구 후에는 정상 흐름대로 자동제어까지 수행된다 (CO2 1500 -> 창문 열기)
    assert event_types(agent) == ["sensor_error", "sensor_recovered", "auto_control"]


def test_actuator_failure_in_auto_control_does_not_crash():
    agent = make_agent(FlakySensor(failures=0), {WINDOW: BrokenActuator(WINDOW)})

    agent._sample()  # 시작은 OFFLINE 모드 -> CO2 1500 이라 창문 열기 시도

    assert "actuator_error" in event_types(agent)


def test_actuator_failure_in_remote_command_does_not_crash():
    agent = make_agent(FlakySensor(failures=0), {WINDOW: BrokenActuator(WINDOW)})

    agent._handle_command({"actuator": WINDOW, "on": True})

    assert event_types(agent) == ["actuator_error"]
