"""원격 조작 명령의 ack (프론트엔드 command.ack 로 전달)."""

from edge_agent.agent import EdgeAgent
from edge_agent.config import AgentConfig
from edge_agent.devices import DOOR, WINDOW, MockActuator, MockDoorCloser, MockDoorSensor, MockSensorReader
from edge_agent.storage import LocalStore


class FakeMqtt:
    topic_telemetry = "t/telemetry"
    topic_events = "t/events"
    topic_state = "t/state"
    topic_ack = "t/ack"

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict, bool]] = []

    def publish_json(self, topic: str, payload: dict, qos: int = 1, retain: bool = False) -> bool:
        self.sent.append((topic, payload, retain))
        return True

    def acks(self) -> list[dict]:
        return [p for t, p, _ in self.sent if t == self.topic_ack]


class CountingWindow(MockActuator):
    def __init__(self) -> None:
        super().__init__(WINDOW)
        self.calls = 0

    def set(self, on: bool) -> None:
        self.calls += 1
        super().set(on)


class BrokenWindow(MockActuator):
    def __init__(self) -> None:
        super().__init__(WINDOW)

    def set(self, on: bool) -> None:
        raise RuntimeError("servo stalled")


def make_agent(actuators) -> tuple[EdgeAgent, FakeMqtt]:
    agent = EdgeAgent(AgentConfig(), MockSensorReader(seed=0), actuators, LocalStore(":memory:"))
    agent.mqtt = FakeMqtt()
    return agent, agent.mqtt


def test_accepted_command_is_acked_with_command_id():
    window = CountingWindow()
    agent, mqtt = make_agent({WINDOW: window})

    agent._handle_command({"commandId": "c-1", "actuator": WINDOW, "on": True})

    (ack,) = mqtt.acks()
    assert ack["commandId"] == "c-1"
    assert ack["status"] == "accepted"
    assert ack["actuator"] == WINDOW and ack["on"] is True
    assert "ackedAt" in ack
    assert window.is_on is True


def test_duplicate_command_is_not_executed_twice():
    # QoS 1 은 같은 메시지가 다시 올 수 있다 -> 실행은 한 번, ack 는 다시 보냄
    window = CountingWindow()
    agent, mqtt = make_agent({WINDOW: window})

    agent._handle_command({"commandId": "c-1", "actuator": WINDOW, "on": True})
    agent._handle_command({"commandId": "c-1", "actuator": WINDOW, "on": True})

    assert window.calls == 1
    assert len(mqtt.acks()) == 2
    assert mqtt.acks()[0] == mqtt.acks()[1]


def test_unknown_actuator_is_rejected():
    agent, mqtt = make_agent({})

    agent._handle_command({"commandId": "c-2", "actuator": "tv", "on": True})

    (ack,) = mqtt.acks()
    assert ack["status"] == "rejected"
    assert "unknown actuator" in ack["error"]


def test_non_boolean_on_is_rejected():
    agent, mqtt = make_agent({WINDOW: CountingWindow()})

    agent._handle_command({"commandId": "c-3", "actuator": WINDOW, "on": "yes"})

    assert mqtt.acks()[0]["status"] == "rejected"


def test_door_open_is_rejected():
    door = MockDoorSensor(is_open=False)
    agent, mqtt = make_agent({DOOR: MockDoorCloser(door)})

    agent._handle_command({"commandId": "c-4", "actuator": DOOR, "on": True})

    ack = mqtt.acks()[0]
    assert ack["status"] == "rejected"
    assert ack["error"] == "door can only be closed"


def test_actuator_failure_is_acked_as_failed():
    agent, mqtt = make_agent({WINDOW: BrokenWindow()})

    agent._handle_command({"commandId": "c-5", "actuator": WINDOW, "on": True})

    ack = mqtt.acks()[0]
    assert ack["status"] == "failed"
    assert ack["error"] == "servo stalled"
    types = [r["type"] for r in agent.store.unsynced_events()]
    assert types == ["actuator_error"]


def test_command_without_id_still_works():
    window = CountingWindow()
    agent, mqtt = make_agent({WINDOW: window})

    agent._handle_command({"actuator": WINDOW, "on": True})
    agent._handle_command({"actuator": WINDOW, "on": True})

    assert window.calls == 2  # id 가 없으면 중복 판별을 못 한다
    assert mqtt.acks()[0]["commandId"] is None
