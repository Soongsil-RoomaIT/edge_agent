"""클라우드로 나가는 MQTT 메시지 형식 (cloud-server roomait-v1 계약)."""

from datetime import datetime

from conftest import make_agent

from edge_agent import protocol
from edge_agent.devices import Reading


def test_sensors_payload_matches_contract():
    agent = make_agent()
    agent.store.save_reading(agent.sensor.read())

    agent._flush_backlog()

    (payload,) = agent.mqtt.on(agent.mqtt.topic_sensors)
    assert set(payload) == {"measuredAt", "temperature", "humidity", "co2", "pm25", "pm10"}
    assert payload["measuredAt"].endswith("Z")
    datetime.fromisoformat(payload["measuredAt"])
    assert payload["pm10"] >= payload["pm25"]


def test_reading_without_pm10_is_not_sent_but_not_retried():
    agent = make_agent()
    agent.store.save_reading(
        Reading(measured_at=0.0, temperature=24.0, humidity=50.0, co2=600.0, pm25=10.0)
    )

    agent._flush_backlog()

    assert agent.mqtt.on(agent.mqtt.topic_sensors) == []
    assert agent.store.unsynced_readings() == []


def test_event_detail_is_json_object_and_time_is_iso():
    agent = make_agent()
    agent.store.log_event("mode_changed", mode="offline")

    agent._flush_backlog()

    (payload,) = agent.mqtt.on(agent.mqtt.topic_events)
    assert payload["detail"] == {"mode": "offline"}
    assert payload["occurredAt"].endswith("Z")


def test_iso_roundtrip():
    ts = 1762300000.123
    assert abs(protocol.parse_iso(protocol.iso(ts)) - ts) < 0.001


def test_timestamp_without_timezone_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        protocol.parse_iso("2026-11-05T12:00:00")


def test_device_state_names():
    assert protocol.device_state("window", True) == "OPEN"
    assert protocol.device_state("door", False) == "CLOSED"
    assert protocol.device_state("air_purifier", True) == "ON"
    assert protocol.device_state("dehumidifier", False) == "OFF"
    assert protocol.device_state("window", None) is None
