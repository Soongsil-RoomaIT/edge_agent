"""use_mock_devices = false 일 때 hardware 레포 HardwareBundle 연결.

노트북에는 GPIO 가 없으므로 roomcare_hw.bundle 을 가짜 모듈로 바꿔서 확인한다.
"""

from __future__ import annotations

import sys
import types

import pytest
from conftest import FakeMqtt, command, go_online

from edge_agent.agent import build_agent
from edge_agent.config import AgentConfig, HardwareConfig, load_config
from edge_agent.devices import AIR_PURIFIER, WINDOW, MockActuator, MockDoorSensor, MockSensorReader


class SensorWindow(MockActuator):
    state_source = "SENSOR"

    def set(self, on: bool) -> None:
        pass  # Pressing the button has no effect on the reading until the sensor changes.

    def update_sensor(self, is_open: bool) -> None:
        super().set(is_open)


class FakeBundle:
    instances: list["FakeBundle"] = []
    fail_with: Exception | None = None

    def __init__(self, **kwargs) -> None:
        if FakeBundle.fail_with is not None:
            raise FakeBundle.fail_with
        self.kwargs = kwargs
        self.closed = False
        self.sensor = MockSensorReader(seed=0)
        self.actuators = {}
        if kwargs.get("window_touch"):
            self.actuators[WINDOW] = SensorWindow(WINDOW)
        elif kwargs["window_motor"] or kwargs.get("window_button"):
            self.actuators[WINDOW] = MockActuator(WINDOW)
        if kwargs["purifier"]:
            self.actuators[AIR_PURIFIER] = MockActuator(AIR_PURIFIER)
        self.door_sensor = MockDoorSensor() if kwargs["door_sensor"] else None
        FakeBundle.instances.append(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_hw(monkeypatch):
    FakeBundle.instances = []
    FakeBundle.fail_with = None
    pkg = types.ModuleType("roomcare_hw")
    bundle = types.ModuleType("roomcare_hw.bundle")
    bundle.HardwareBundle = FakeBundle
    monkeypatch.setitem(sys.modules, "roomcare_hw", pkg)
    monkeypatch.setitem(sys.modules, "roomcare_hw.bundle", bundle)
    return FakeBundle


def hw_config(tmp_path, **hardware) -> AgentConfig:
    return AgentConfig(
        use_mock_devices=False, db_path=str(tmp_path / "edge.db"), hardware=HardwareConfig(**hardware)
    )


def test_bundle_gets_config_values(fake_hw, tmp_path):
    cfg = hw_config(
        tmp_path, temperature_model="DHT11", window_motor=True, purifier=True, purifier_initial="off"
    )

    agent = build_agent(cfg)

    (hw,) = fake_hw.instances
    assert hw.kwargs["temperature_model"] == "DHT11"
    assert hw.kwargs["purifier_initial"] is False
    assert hw.kwargs["dehumidifier_initial"] is None  # unknown
    assert set(agent.actuators) == {WINDOW, AIR_PURIFIER}
    assert agent.sensor is hw.sensor
    assert agent.door_sensor is None
    assert "door_magnetic" not in hw.kwargs
    assert "window_touch" not in hw.kwargs


def test_window_touch_reports_and_acks_observed_state(fake_hw, tmp_path):
    cfg = hw_config(
        tmp_path, button_panel=True, window_button=True, window_touch=True, touch_verified=True
    )
    cfg.db_path = ":memory:"
    agent = build_agent(cfg)
    (hw,) = fake_hw.instances
    assert hw.kwargs["window_touch"] is True and hw.kwargs["touch_verified"] is True
    agent.mqtt = FakeMqtt()
    go_online(agent)
    try:
        agent._publish_devices()
        agent._handle_command(command("window-1", "OPEN"))
        agent._check_pending()
        assert agent.mqtt.acks() == []

        agent.actuators[WINDOW].update_sensor(True)
        agent._check_pending()
        agent._publish_devices()
        updates = agent.mqtt.on(agent.mqtt.topic_devices)
        assert [(p["id"], p["state"], p["stateSource"]) for p in updates] == [
            ("window-1", "CLOSED", "SENSOR"),
            ("window-1", "OPEN", "SENSOR"),
        ]
        (ack,) = agent.mqtt.acks()
        assert (ack["status"], ack["stateSource"]) == ("EXECUTED", "SENSOR")
    finally:
        agent.store.close()


def test_window_touch_requires_installation_verification(fake_hw, tmp_path):
    with pytest.raises(ValueError, match="window_touch.*touch_verified"):
        build_agent(hw_config(tmp_path, button_panel=True, window_button=True, window_touch=True))

    assert fake_hw.instances == []


def test_window_touch_rejects_old_hardware(fake_hw, tmp_path, monkeypatch):
    monkeypatch.setattr(SensorWindow, "state_source", None)
    cfg = hw_config(
        tmp_path, button_panel=True, window_button=True, window_touch=True, touch_verified=True
    )
    with pytest.raises(RuntimeError, match="latest hardware package"):
        build_agent(cfg)

    assert fake_hw.instances[0].closed


def test_magnetic_door_reports_state_without_closer(fake_hw, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'use_mock_devices = false\ndb_path = ":memory:"\n[hardware]\n'
        "door_sensor = true\ndoor_magnetic = true\ndoor_verified = true\n",
        encoding="utf-8",
    )
    agent = build_agent(load_config(path))
    (hw,) = fake_hw.instances
    assert hw.kwargs["door_magnetic"] is True
    assert hw.kwargs["door_verified"] is True
    assert hw.kwargs["door_closer"] is False
    assert agent.door_sensor is hw.door_sensor
    assert not agent.actuators

    agent.mqtt = FakeMqtt()
    go_online(agent)
    try:
        agent._check_door()
        agent._check_door()
        hw.door_sensor.is_open = True
        agent._check_door()
        hw.door_sensor.is_open = False
        agent._check_door()

        updates = agent.mqtt.on(agent.mqtt.topic_devices)
        assert [(update["id"], update["state"]) for update in updates] == [
            ("door-1", "CLOSED"),
            ("door-1", "OPEN"),
            ("door-1", "CLOSED"),
        ]
    finally:
        agent.store.close()


def test_magnetic_door_requires_door_sensor(fake_hw, tmp_path):
    with pytest.raises(ValueError, match="door_magnetic.*door_sensor"):
        build_agent(hw_config(tmp_path, door_magnetic=True))

    assert fake_hw.instances == []


def test_dht_only_uses_starter_profile(fake_hw, tmp_path):
    build_agent(hw_config(tmp_path, temperature_model="DHT11", dht_only=True))

    (hw,) = fake_hw.instances
    assert hw.kwargs["starter"] is True
    assert hw.kwargs["temperature_model"] == "DHT11"


def test_full_sensor_set_is_default(fake_hw, tmp_path):
    build_agent(hw_config(tmp_path))

    assert fake_hw.instances[0].kwargs["starter"] is False


def test_dht_only_with_sht31_is_rejected_before_touching_gpio(fake_hw, tmp_path):
    with pytest.raises(ValueError, match="dht_only"):
        build_agent(hw_config(tmp_path, dht_only=True))

    assert fake_hw.instances == []


def test_button_panel_config_is_passed(fake_hw, tmp_path):
    cfg = hw_config(
        tmp_path,
        button_panel=True,
        window_button=True,
        window_initial="closed",
        purifier=True,
        purifier_initial="off",
    )

    agent = build_agent(cfg)

    kw = fake_hw.instances[0].kwargs
    assert kw["button_panel"] is True
    assert kw["window_button"] is True
    assert kw["window_initial"] is False
    assert kw["window_motor"] is False
    assert set(agent.actuators) == {WINDOW, AIR_PURIFIER}


def test_button_panel_options_not_sent_when_off(fake_hw, tmp_path):
    # 버튼 서보 구성을 모르는 예전 hardware 레포에 모르는 인자를 넘기지 않는다
    build_agent(hw_config(tmp_path))

    assert "button_panel" not in fake_hw.instances[0].kwargs


@pytest.mark.parametrize(
    "hardware, message",
    [
        (dict(button_panel=True, window_motor=True), "window_motor"),
        (dict(window_button=True), "button_panel"),
        (dict(button_panel=True, window_button=True, window_initial="half"), "window_initial"),
    ],
)
def test_bad_button_panel_config_is_rejected_before_touching_gpio(fake_hw, tmp_path, hardware, message):
    with pytest.raises(ValueError, match=message):
        build_agent(hw_config(tmp_path, **hardware))

    assert fake_hw.instances == []


def test_actuators_are_off_by_default(fake_hw, tmp_path):
    agent = build_agent(hw_config(tmp_path))

    assert agent.actuators == {}
    assert all(
        fake_hw.instances[0].kwargs[k] is False
        for k in ("window_motor", "purifier", "dehumidifier", "door_sensor", "door_closer")
    )


def test_bundle_is_closed_when_agent_stops(fake_hw, tmp_path):
    agent = build_agent(hw_config(tmp_path))
    hw = fake_hw.instances[0]
    assert not hw.closed

    agent._run_cleanups()

    assert hw.closed


def test_bad_initial_state_is_rejected_before_touching_gpio(fake_hw, tmp_path):
    with pytest.raises(ValueError, match="purifier_initial"):
        build_agent(hw_config(tmp_path, purifier=True, purifier_initial="maybe"))

    assert fake_hw.instances == []


def test_bundle_error_propagates(fake_hw, tmp_path):
    fake_hw.fail_with = ValueError("door closer requires verified door sensor")

    with pytest.raises(ValueError, match="door closer"):
        build_agent(hw_config(tmp_path, door_closer=True))


def test_missing_hardware_package_explains_how_to_install(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "roomcare_hw", None)  # import 실패

    with pytest.raises(RuntimeError, match="pip install -e ../hardware"):
        build_agent(hw_config(tmp_path))


def test_hardware_section_is_loaded_from_toml(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        'use_mock_devices = false\n[hardware]\nwindow_motor = true\npurifier_initial = "on"\n',
        encoding="utf8",
    )

    cfg = load_config(path)

    assert cfg.hardware.window_motor is True
    assert cfg.hardware.bundle_kwargs()["purifier_initial"] is True


def test_example_config_loads():
    cfg = load_config("config/config.example.toml")

    assert cfg.use_mock_devices is True
    assert cfg.hardware.bundle_kwargs()["window_motor"] is False
