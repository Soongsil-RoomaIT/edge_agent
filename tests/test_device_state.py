"""기기 상태 (roomait-v1 devices) 와 status heartbeat."""

from conftest import make_agent

from edge_agent.devices import (
    AIR_PURIFIER,
    DOOR,
    WINDOW,
    MockActuator,
    MockDoorCloser,
    MockDoorSensor,
)


class LimitSwitchWindow:
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


def devices(agent) -> dict[str, str]:
    return {p["id"]: p["state"] for p in agent.mqtt.on(agent.mqtt.topic_devices)}


def test_snapshot_is_sent_per_device_on_connect():
    door = MockDoorSensor(is_open=True)
    agent = make_agent(
        {
            WINDOW: LimitSwitchWindow("closed"),
            AIR_PURIFIER: MockActuator(AIR_PURIFIER),
            DOOR: MockDoorCloser(door),
        },
        door_sensor=door,
        online=True,
    )
    agent._check_door()
    agent._publish_devices()

    assert devices(agent) == {"window-1": "CLOSED", "purifier-1": "OFF", "door-1": "OPEN"}
    msg = agent.mqtt.on(agent.mqtt.topic_devices)[0]
    assert set(msg) == {"id", "state", "updatedAt"}
    assert all(not retain for t, _, retain in agent.mqtt.sent if t == agent.mqtt.topic_devices)


def test_only_changed_device_is_sent_again():
    window = LimitSwitchWindow("closed")
    agent = make_agent({WINDOW: window, AIR_PURIFIER: MockActuator(AIR_PURIFIER)}, online=True)
    agent._publish_devices()
    agent._publish_devices()
    assert len(agent.mqtt.on(agent.mqtt.topic_devices)) == 2

    window.set(True)
    agent._publish_devices()

    msgs = agent.mqtt.on(agent.mqtt.topic_devices)
    assert len(msgs) == 3
    assert msgs[-1]["id"] == "window-1" and msgs[-1]["state"] == "OPEN"


def test_unknown_or_faulted_state_is_not_sent():
    window = LimitSwitchWindow("unknown")
    agent = make_agent({WINDOW: window, AIR_PURIFIER: UnknownPurifier()}, online=True)

    agent._publish_devices()

    assert devices(agent) == {}  # 계약에 unknown 값이 없어서 보내지 않는다


def test_devices_not_sent_while_offline():
    agent = make_agent({WINDOW: LimitSwitchWindow("closed")})

    agent._publish_devices()

    assert devices(agent) == {}


def test_snapshot_is_resent_after_reconnect():
    agent = make_agent({WINDOW: LimitSwitchWindow("closed")}, online=True)
    agent._publish_devices()
    assert len(agent.mqtt.on(agent.mqtt.topic_devices)) == 1

    agent._on_broker_disconnected()
    agent.monitor._disconnected_since -= 10  # 유예 시간 지남
    agent._check_mode()
    agent._on_broker_connected()
    agent._check_mode()
    agent._publish_devices()

    assert len(agent.mqtt.on(agent.mqtt.topic_devices)) == 2


def test_status_heartbeat():
    agent = make_agent({}, online=True)

    statuses = agent.mqtt.on(agent.mqtt.topic_status)
    assert statuses[-1] == {"online": True, "offlineMode": False}

    agent._publish_status()  # 주기 전에는 다시 안 보냄
    assert len(agent.mqtt.on(agent.mqtt.topic_status)) == len(statuses)

    agent._next_status = 0
    agent._publish_status()
    assert len(agent.mqtt.on(agent.mqtt.topic_status)) == len(statuses) + 1


def test_status_not_sent_without_broker():
    agent = make_agent({})

    agent._publish_status()

    assert agent.mqtt.on(agent.mqtt.topic_status) == []
