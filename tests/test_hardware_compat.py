"""hardware 레포 드라이버와 함께 쓸 때의 호환성.

hardware 의 실제 클래스는 GPIO 가 필요하므로, 같은 속성(position / state / fault)을 가진
가짜 객체로 동작을 확인한다.
"""

from dataclasses import dataclass

from edge_agent.agent import EdgeAgent
from edge_agent.config import AgentConfig
from edge_agent.devices import AIR_PURIFIER, WINDOW
from edge_agent.storage import LocalStore


@dataclass(frozen=True)
class HardwareReading:
    """hardware/roomcare_hw/types.py 의 Reading (pm10 없음)."""

    measured_at: float
    temperature: float
    humidity: float
    co2: float
    pm25: float


class FixedSensor:
    def __init__(self, **values) -> None:
        base = dict(measured_at=0.0, temperature=24.0, humidity=50.0, co2=600.0, pm25=10.0)
        base.update(values)
        self.reading = HardwareReading(**base)

    def read(self):
        return self.reading


class FakeWindow:
    """hardware WindowActuator 흉내: position + fault, is_on = position == 'open'."""

    name = WINDOW

    def __init__(self, position: str) -> None:
        self.position = position
        self.fault = None
        self.calls: list[bool] = []

    @property
    def is_on(self) -> bool:
        return self.position == "open"

    def set(self, on: bool) -> None:
        self.calls.append(on)


class UnknownPurifier:
    """hardware ServoButtonPurifier 흉내: 상태를 모르면 is_on 이 예외."""

    name = AIR_PURIFIER
    state = "unknown"
    fault = None

    def __init__(self) -> None:
        self.calls: list[bool] = []

    @property
    def is_on(self) -> bool:
        raise RuntimeError("appliance state unknown; confirm state or add feedback")

    def set(self, on: bool) -> None:
        self.calls.append(on)


def make_agent(sensor, actuators) -> EdgeAgent:
    # 시작 모드가 OFFLINE 이라 _sample() 이 자동제어까지 수행한다
    return EdgeAgent(AgentConfig(), sensor, actuators, LocalStore(":memory:"))


def event_types(agent: EdgeAgent) -> list[str]:
    return [r["type"] for r in agent.store.unsynced_events()]


def test_reading_without_pm10_is_saved_as_null():
    agent = make_agent(FixedSensor(), {})

    agent._sample()

    (row,) = agent.store.unsynced_readings()
    assert row["pm10"] is None
    assert row["pm25"] == 10.0


def test_window_in_unknown_position_is_sent_to_target():
    # is_on=False 지만 닫힌 게 아니라 중간 위치 -> 닫기 명령을 다시 보내야 한다
    window = FakeWindow("unknown")
    agent = make_agent(FixedSensor(), {WINDOW: window})

    agent._sample()

    assert window.calls == [False]


def test_closed_window_is_not_commanded_again():
    window = FakeWindow("closed")
    agent = make_agent(FixedSensor(), {WINDOW: window})

    agent._sample()

    assert window.calls == []


def test_faulted_window_is_skipped_and_reported_once():
    window = FakeWindow("open")
    window.fault = "limit switch not reached"
    agent = make_agent(FixedSensor(), {WINDOW: window})

    for _ in range(3):
        agent._sample()

    assert window.calls == []
    assert event_types(agent).count("actuator_unavailable") == 1


def test_unknown_appliance_is_not_toggled_blindly():
    purifier = UnknownPurifier()
    agent = make_agent(FixedSensor(pm25=60), {AIR_PURIFIER: purifier})

    for _ in range(3):
        agent._sample()

    assert purifier.calls == []
    assert event_types(agent).count("actuator_unavailable") == 1
    assert "actuator_error" not in event_types(agent)


def test_recovery_after_fault_is_reported_and_control_resumes():
    window = FakeWindow("closed")
    window.fault = "obstruction"
    agent = make_agent(FixedSensor(co2=1500), {WINDOW: window})

    agent._sample()
    window.fault = None
    agent._sample()

    types = event_types(agent)
    assert types.index("actuator_unavailable") < types.index("actuator_available")
    assert window.calls == [True]  # 복구 후 CO2 높음 -> 창문 열기
