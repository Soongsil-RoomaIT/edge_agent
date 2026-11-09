"""use_mock_devices = false 일 때 hardware 레포 HardwareBundle 연결.

노트북에는 GPIO 가 없으므로 roomcare_hw.bundle 을 가짜 모듈로 바꿔서 확인한다.
"""

import sys
import types

import pytest

from edge_agent.agent import build_agent
from edge_agent.config import AgentConfig, HardwareConfig, load_config
from edge_agent.devices import AIR_PURIFIER, WINDOW, MockActuator, MockDoorSensor, MockSensorReader


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
        if kwargs["window_motor"]:
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
