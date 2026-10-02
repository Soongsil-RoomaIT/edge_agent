"""클라우드로 나가는 MQTT 메시지 형식 (백엔드와의 계약)."""

from edge_agent.agent import EdgeAgent
from edge_agent.config import AgentConfig
from edge_agent.devices import MockSensorReader
from edge_agent.storage import LocalStore


class FakeMqtt:
    topic_telemetry = "t/telemetry"
    topic_events = "t/events"

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def publish_json(self, topic: str, payload: dict) -> bool:
        self.sent.append((topic, payload))
        return True


def make_agent() -> tuple[EdgeAgent, FakeMqtt]:
    agent = EdgeAgent(AgentConfig(), MockSensorReader(seed=0), {}, LocalStore(":memory:"))
    fake = FakeMqtt()
    agent.mqtt = fake
    return agent, fake


def test_telemetry_includes_pm10():
    agent, mqtt = make_agent()
    agent.store.save_reading(agent.sensor.read())

    agent._flush_backlog()

    (topic, payload), = mqtt.sent
    assert topic == "t/telemetry"
    assert set(payload) == {"measuredAt", "temperature", "humidity", "co2", "pm25", "pm10"}
    assert payload["pm10"] >= payload["pm25"]


def test_event_detail_is_json_object_not_string():
    agent, mqtt = make_agent()
    agent.store.log_event("mode_changed", mode="offline")

    agent._flush_backlog()

    (topic, payload), = mqtt.sent
    assert topic == "t/events"
    assert payload["detail"] == {"mode": "offline"}
