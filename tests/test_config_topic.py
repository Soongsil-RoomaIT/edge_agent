"""클라우드 config 토픽으로 자동제어 기준값 변경."""

import pytest

from edge_agent.agent import EdgeAgent
from edge_agent.config import AgentConfig, Thresholds, apply_threshold_update
from edge_agent.devices import MockSensorReader
from edge_agent.storage import LocalStore


def make_agent(store: LocalStore | None = None) -> EdgeAgent:
    return EdgeAgent(AgentConfig(), MockSensorReader(seed=0), {}, store or LocalStore(":memory:"))


def events(agent: EdgeAgent) -> list[str]:
    return [r["type"] for r in agent.store.unsynced_events()]


def test_partial_update_keeps_other_values():
    t = apply_threshold_update(Thresholds(), {"co2High": 900, "humidityHigh": 65})

    assert t.co2_high == 900 and t.humidity_high == 65
    assert t.pm25_high == Thresholds().pm25_high


@pytest.mark.parametrize(
    "update",
    [
        {"co2High": 5000},  # 안전 범위 밖
        {"pm25High": 5},
        {"co2High": "900"},  # 숫자가 아님
        {"co2High": True},
        {"windowSpeed": 3},  # 모르는 키
        {},
        None,
    ],
)
def test_invalid_updates_are_rejected(update):
    with pytest.raises(ValueError):
        apply_threshold_update(Thresholds(), update)


def test_config_is_applied_and_reported():
    agent = make_agent()

    agent._handle_config({"version": 1, "thresholds": {"co2High": 900}})

    assert agent.cfg.thresholds.co2_high == 900
    assert events(agent) == ["config_applied"]


def test_invalid_config_keeps_old_thresholds():
    agent = make_agent()

    agent._handle_config({"version": 1, "thresholds": {"co2High": 900, "pm25High": 500}})

    assert agent.cfg.thresholds == Thresholds()  # 하나라도 잘못되면 전체 거부
    assert events(agent) == ["config_rejected"]


def test_stale_version_is_ignored():
    agent = make_agent()
    agent._handle_config({"version": 5, "thresholds": {"co2High": 900}})

    agent._handle_config({"version": 4, "thresholds": {"co2High": 1200}})

    assert agent.cfg.thresholds.co2_high == 900


def test_same_retained_config_on_reconnect_is_silent():
    agent = make_agent()
    msg = {"version": 2, "thresholds": {"co2High": 900}}

    agent._handle_config(msg)
    agent._handle_config(msg)

    assert events(agent) == ["config_applied"]


def test_applied_config_survives_restart(tmp_path):
    db = tmp_path / "edge.db"
    agent = make_agent(LocalStore(db))
    agent._handle_config({"version": 3, "thresholds": {"humidityHigh": 60}})
    agent.store.close()

    restarted = make_agent(LocalStore(db))

    assert restarted.cfg.thresholds.humidity_high == 60
    assert restarted._config_version == 3
