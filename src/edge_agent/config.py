"""설정 로딩. config/config.toml (없으면 기본값) 을 읽는다."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
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


# 클라우드 config 메시지의 키 (camelCase) -> Thresholds 필드
THRESHOLD_KEYS = {
    "co2High": "co2_high",
    "pm25High": "pm25_high",
    "pm10High": "pm10_high",
    "humidityHigh": "humidity_high",
    "temperatureHigh": "temperature_high",
}

# 원격으로 바꿀 수 있는 기준값의 안전 범위. 잘못된 값(예: CO2 5000)으로
# 자동제어가 사실상 꺼지거나 창문이 계속 열리는 것을 막는다.
SAFE_RANGES = {
    "co2_high": (600.0, 2000.0),
    "pm25_high": (15.0, 75.0),
    "pm10_high": (30.0, 150.0),
    "humidity_high": (40.0, 85.0),
    "temperature_high": (24.0, 35.0),
}


def apply_threshold_update(current: Thresholds, update: object) -> Thresholds:
    """클라우드에서 받은 기준값 변경을 검증해서 새 Thresholds 를 돌려준다.

    일부 키만 보내도 된다. 하나라도 잘못되면 ValueError (전체 거부).
    """
    if not isinstance(update, dict) or not update:
        raise ValueError("thresholds must be a non-empty object")
    changes: dict[str, float] = {}
    for key, value in update.items():
        name = THRESHOLD_KEYS.get(key)
        if name is None:
            raise ValueError(f"unknown threshold: {key}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        low, high = SAFE_RANGES[name]
        if not low <= value <= high:
            raise ValueError(f"{key}={value} outside safe range {low:g}..{high:g}")
        changes[name] = float(value)
    return replace(current, **changes)


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
