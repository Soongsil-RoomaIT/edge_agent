"""기기 상태 메시지 (프론트엔드 device.state, stateSource 로 전달)."""

from edge_agent.agent import EdgeAgent
from edge_agent.config import AgentConfig
from edge_agent.connectivity import Mode
from edge_agent.devices import (
    AIR_PURIFIER,
    DOOR,
    WINDOW,
    MockActuator,
    MockDoorCloser,
    MockDoorSensor,
    MockSensorReader,
)
from edge_agent.storage import LocalStore

from test_commands import FakeMqtt


class LimitSwitchWindow:
    """hardware WindowActuator 흉내: 리밋 스위치로 위치를 안다."""

    name = WINDOW

    def __init__(self, position: str) -> None:
        self.position = position
        self.fault = None

    @property
    def is_on(self) -> bool:
        return self.position == "open"

    def set(self, on: bool) -> None:
        self.position = "open" if on else "closed"


class UnknownPurifier:
    name = AIR_PURIFIER
    state = "unknown"
    fault = None

    @property
    def is_on(self) -> bool:
        raise RuntimeError("appliance state unknown")

    def set(self, on: bool) -> None:
        pass


def make_agent(actuators, door_sensor=None, online=True) -> tuple[EdgeAgent, FakeMqtt]:
    agent = EdgeAgent(
        AgentConfig(),
        MockSensorReader(seed=0),
        actuators,
        LocalStore(":memory:"),
        door_sensor=door_sensor,
    )
    agent.mqtt = FakeMqtt()
    if online:
        agent.monitor.mark_connected()
        agent.monitor.update()
        assert agent.monitor.mode is Mode.ONLINE
    return agent, agent.mqtt


def states(mqtt: FakeMqtt) -> list[dict]:
    return [p for t, p, _ in mqtt.sent if t == mqtt.topic_state]


def test_state_sources_and_values():
    door = MockDoorSensor(is_open=True)
    agent, mqtt = make_agent(
        {
            WINDOW: LimitSwitchWindow("closed"),
            AIR_PURIFIER: MockActuator(AIR_PURIFIER),
            DOOR: MockDoorCloser(door),
        },
        door_sensor=door,
    )
    agent._check_door()

    agent._publish_state()

    (payload,) = states(mqtt)
    devices = payload["devices"]
    assert devices[WINDOW] == {"on": False, "position": "closed", "source": "SENSOR", "fault": None}
    assert devices[AIR_PURIFIER] == {"on": False, "source": "ASSUMED", "fault": None}
    assert devices[DOOR] == {"on": True, "source": "SENSOR", "fault": None}
    assert "reportedAt" in payload


def test_state_is_retained_and_sent_only_on_change():
    window = LimitSwitchWindow("closed")
    agent, mqtt = make_agent({WINDOW: window})

    agent._publish_state()
    agent._publish_state()  # 변화 없음
    window.set(True)
    agent._publish_state()

    assert len(states(mqtt)) == 2
    assert states(mqtt)[1]["devices"][WINDOW]["on"] is True
    assert all(retain for t, _, retain in mqtt.sent if t == mqtt.topic_state)


def test_state_not_sent_while_offline():
    agent, mqtt = make_agent({WINDOW: LimitSwitchWindow("closed")}, online=False)

    agent._publish_state()

    assert states(mqtt) == []


def test_unknown_appliance_state_is_null():
    agent, mqtt = make_agent({AIR_PURIFIER: UnknownPurifier()})

    agent._publish_state()

    assert states(mqtt)[0]["devices"][AIR_PURIFIER]["on"] is None


def test_fault_is_reported():
    window = LimitSwitchWindow("unknown")
    window.fault = "limit switch not reached"
    agent, mqtt = make_agent({WINDOW: window})

    agent._publish_state()

    w = states(mqtt)[0]["devices"][WINDOW]
    assert w["on"] is None
    assert w["fault"] == "limit switch not reached"


def test_state_is_resent_after_reconnect():
    agent, mqtt = make_agent({WINDOW: LimitSwitchWindow("closed")})
    agent._publish_state()

    agent._last_state = None  # _check_mode 가 ONLINE 복귀 시 하는 일
    agent._publish_state()

    assert len(states(mqtt)) == 2
