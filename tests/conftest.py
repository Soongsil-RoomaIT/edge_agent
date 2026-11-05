"""테스트 공용 도구: 가짜 MQTT 클라이언트, v1 명령 만들기."""

from __future__ import annotations

import time
import uuid

from edge_agent.agent import EdgeAgent
from edge_agent.config import AgentConfig
from edge_agent.connectivity import Mode
from edge_agent.devices import MockSensorReader
from edge_agent.protocol import iso
from edge_agent.storage import LocalStore


class FakeMqtt:
    topic_sensors = "t/sensors"
    topic_devices = "t/devices"
    topic_status = "t/status"
    topic_recovery = "t/recovery"
    topic_ack = "t/ack"
    topic_events = "t/events"

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict, bool]] = []
        self.connected = True

    def publish_json(self, topic: str, payload: dict, qos: int = 1, retain: bool = False) -> bool:
        if not self.connected:
            return False
        self.sent.append((topic, payload, retain))
        return True

    def on(self, topic: str) -> list[dict]:
        return [p for t, p, _ in self.sent if t == topic]

    def acks(self) -> list[dict]:
        return self.on(self.topic_ack)


def command(device_id: str, action: str, *, ttl: float = 20.0, command_id: str | None = None) -> dict:
    now = time.time()
    return {
        "commandId": command_id or str(uuid.uuid4()),
        "deviceId": device_id,
        "action": action,
        "reason": "test",
        "issuedAt": iso(now),
        "expiresAt": iso(now + ttl),
    }


def make_agent(actuators=None, *, sensor=None, door_sensor=None, store=None, online=False, cfg=None):
    agent = EdgeAgent(
        cfg or AgentConfig(),
        sensor or MockSensorReader(seed=0),
        actuators or {},
        store or LocalStore(":memory:"),
        door_sensor=door_sensor,
    )
    agent.mqtt = FakeMqtt()
    if online:
        go_online(agent)
    return agent


def go_online(agent: EdgeAgent) -> None:
    agent._on_broker_connected()
    agent._check_mode()
    assert agent.monitor.mode is Mode.ONLINE


def event_types(agent: EdgeAgent) -> list[str]:
    return [r["type"] for r in agent.store.unsynced_events()]
