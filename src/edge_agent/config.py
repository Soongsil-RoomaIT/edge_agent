"""설정 로딩. config/config.toml (없으면 기본값) 을 읽는다."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    # 브로커 단절 감지 속도에 직접 영향. paho 는 약 1.5 x keepalive 안에 끊김을 감지한다.
    keepalive: int = 4
    topic_prefix: str = "roomcare"


@dataclass
class Thresholds:
    co2_high: float = 1000.0  # ppm
    pm25_high: float = 35.0  # ug/m3, 초과 시 환경부 '나쁨'
    pm10_high: float = 80.0  # ug/m3, 초과 시 환경부 '나쁨'
    humidity_high: float = 70.0  # %
    temperature_high: float = 30.0  # C


@dataclass
class AgentConfig:
    device_id: str = "room-001"
    sample_interval: float = 2.0  # 센서 측정 주기(초). NFR: 5초 이내 전달
    # 끊김 감지 후 오프라인 모드로 넘어가기까지의 유예 시간(초).
    # 감지 시간(~1.5 x keepalive = 6s) + 유예(2s) + 측정 주기(2s) <= 10s 가 되도록 잡는다. (NFR03)
    offline_grace: float = 2.0
    db_path: str = "data/edge.db"
    retention_days: int = 7
    use_mock_devices: bool = True
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)


def load_config(path: str | Path = "config/config.toml") -> AgentConfig:
    path = Path(path)
    if not path.exists():
        return AgentConfig()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    mqtt = MqttConfig(**raw.pop("mqtt", {}))
    thresholds = Thresholds(**raw.pop("thresholds", {}))
    return AgentConfig(**raw, mqtt=mqtt, thresholds=thresholds)
