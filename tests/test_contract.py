"""cloud-server src/validation.js (zod, strict) 의 roomait-v1 스키마와 같은지 확인.

클라우드는 strict 스키마라 키가 하나라도 더 있거나 빠지면 메시지 전체를 버린다.
기준: cloud-server 679cf13 (2026-10-21)
"""

import re
import uuid

from conftest import command, make_agent

from edge_agent.devices import AIR_PURIFIER, WINDOW, MockActuator, Reading

# zod z.string().datetime({ offset: true })
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


class StuffySensor:
    def read(self) -> Reading:
        return Reading(measured_at=0.0, temperature=24.0, humidity=50.0, co2=1500.0, pm25=10.0, pm10=20.0)


def test_sensors_schema():
    agent = make_agent(online=True)
    agent.store.save_reading(agent.sensor.read())
    agent._flush_backlog()

    (p,) = agent.mqtt.on(agent.mqtt.topic_sensors)
    assert set(p) == {"measuredAt", "temperature", "humidity", "co2", "pm25", "pm10"}
    assert ISO.match(p["measuredAt"])
    assert -80 <= p["temperature"] <= 100 and 0 <= p["humidity"] <= 100


def test_devices_schema():
    agent = make_agent({WINDOW: MockActuator(WINDOW), AIR_PURIFIER: MockActuator(AIR_PURIFIER)}, online=True)
    agent._publish_devices()

    for p in agent.mqtt.on(agent.mqtt.topic_devices):
        assert set(p) == {"id", "state", "updatedAt"}
        assert p["state"] in {"OPEN", "CLOSED", "ON", "OFF"}
        assert ISO.match(p["updatedAt"])


def test_status_schema():
    agent = make_agent(online=True)
    for p in agent.mqtt.on(agent.mqtt.topic_status):
        assert set(p) == {"online", "offlineMode"}
        assert isinstance(p["online"], bool) and isinstance(p["offlineMode"], bool)


def test_ack_schema():
    agent = make_agent({WINDOW: MockActuator(WINDOW)}, online=True)
    cmd = command("window-1", "OPEN")
    agent._handle_command(cmd)
    agent._check_pending()

    (p,) = agent.mqtt.acks()
    assert set(p) == {"commandId", "status", "reason", "executedAt"}
    uuid.UUID(p["commandId"])
    assert p["status"] in {"EXECUTED", "FAILED"}
    assert len(p["reason"]) <= 500
    assert ISO.match(p["executedAt"])
    assert p["executedAt"] <= cmd["expiresAt"]  # 클라우드는 만료 후 EXECUTED 를 거부


def test_recovery_schema():
    agent = make_agent({WINDOW: MockActuator(WINDOW)}, sensor=StuffySensor(), online=True)
    agent._on_broker_disconnected()
    agent.monitor._disconnected_since -= 10
    agent._check_mode()
    agent._sample()
    agent._check_pending()
    agent._on_broker_connected()
    agent._check_mode()

    (p,) = agent.mqtt.on(agent.mqtt.topic_recovery)
    assert set(p) == {"offlineSince", "recoveredAt", "localActions"}
    assert ISO.match(p["offlineSince"]) and ISO.match(p["recoveredAt"])
    for a in p["localActions"]:
        assert set(a) == {"executedAt", "deviceId", "state", "reason"}
        assert 1 <= len(a["reason"]) <= 500
        assert p["offlineSince"] <= a["executedAt"] <= p["recoveredAt"]
