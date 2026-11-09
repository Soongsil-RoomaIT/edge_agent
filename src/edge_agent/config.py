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
    humidity_high: float = 70.0  # %, 초과하면 제습기 켜기 (제습기 없으면 환기)
    humidity_off: float = 60.0  # %, 이하로 내려가야 제습기 끄기 (켜기/끄기 사이는 현재 상태 유지)
    temperature_high: float = 30.0  # C


# 클라우드 config 메시지의 키 (camelCase) -> Thresholds 필드
THRESHOLD_KEYS = {
    "co2High": "co2_high",
    "pm25High": "pm25_high",
    "pm10High": "pm10_high",
    "humidityHigh": "humidity_high",
    "humidityOff": "humidity_off",
    "temperatureHigh": "temperature_high",
    # cloud-server / 프론트 자동제어 설정 이름 (settings) 도 그대로 받는다
    "co2Open": "co2_high",
    "pm25On": "pm25_high",
    "humidityOn": "humidity_high",
}

# 원격으로 바꿀 수 있는 기준값의 안전 범위. 잘못된 값(예: CO2 5000)으로
# 자동제어가 사실상 꺼지거나 창문이 계속 열리는 것을 막는다.
SAFE_RANGES = {
    "co2_high": (600.0, 2000.0),
    "pm25_high": (15.0, 75.0),
    "pm10_high": (30.0, 150.0),
    "humidity_high": (40.0, 85.0),
    "humidity_off": (30.0, 80.0),
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
    result = replace(current, **changes)
    if result.humidity_off >= result.humidity_high:
        raise ValueError(
            f"humidityOff ({result.humidity_off:g}) must be below humidityHigh ({result.humidity_high:g})"
        )
    return result


@dataclass
class HardwareConfig:
    """use_mock_devices = false 일 때 hardware 레포 HardwareBundle 에 넘기는 값.

    액추에이터는 기본으로 모두 꺼져 있다. 배선과 기구 동작을 직접 확인한 것만 켠다.
    """

    temperature_model: str = "SHT31"  # SHT31 | DHT11 | DHT22
    window_motor: bool = False
    purifier: bool = False
    # 버튼 서보 가전은 상태를 모르면 누르지 않는다. 실제 전원을 보고 "on" / "off" 로 적는다.
    purifier_initial: str = "unknown"  # on | off | unknown
    dehumidifier: bool = False
    dehumidifier_initial: str = "unknown"
    door_sensor: bool = False
    door_verified: bool = False  # 터치센서 설치 확인 완료
    door_closer: bool = False  # door_sensor + door_verified 필요

    def bundle_kwargs(self) -> dict:
        def initial(name: str, value: str) -> bool | None:
            mapping = {"on": True, "off": False, "unknown": None}
            if value not in mapping:
                raise ValueError(f"hardware.{name} must be on, off or unknown (got {value!r})")
            return mapping[value]

        if self.temperature_model not in ("SHT31", "DHT11", "DHT22"):
            raise ValueError(f"hardware.temperature_model: {self.temperature_model!r}")
        return {
            "temperature_model": self.temperature_model,
            "window_motor": self.window_motor,
            "purifier": self.purifier,
            "purifier_initial": initial("purifier_initial", self.purifier_initial),
            "dehumidifier": self.dehumidifier,
            "dehumidifier_initial": initial("dehumidifier_initial", self.dehumidifier_initial),
            "door_sensor": self.door_sensor,
            "door_verified": self.door_verified,
            "door_closer": self.door_closer,
        }


@dataclass
class AgentConfig:
    device_id: str = "room-001"
    sample_interval: float = 2.0  # 센서 측정 주기(초). NFR: 5초 이내 전달
    # 끊김 감지 후 오프라인 모드로 넘어가기까지의 유예 시간(초).
    # 감지 시간(~1.5 x keepalive = 6s) + 유예(2s) + 측정 주기(2s) <= 10s 가 되도록 잡는다. (NFR03)
    offline_grace: float = 2.0
    db_path: str = "data/edge.db"
    retention_days: int = 7
    # status heartbeat 주기(초). 클라우드는 10초 동안 아무 메시지가 없으면 오프라인으로 본다.
    status_interval: float = 3.0
    # 클라우드 서버 heartbeat(cloud-status) 가 이 시간(초) 동안 안 오면 오프라인 모드로 전환.
    # 브로커는 살아 있고 클라우드 서버만 죽은 경우를 잡는다. 0 = 사용 안 함 (클라우드가 아직 안 보냄)
    # 켤 때는 timeout + offline_grace + 0.5 <= 10 이 되게 (예: 5)
    cloud_heartbeat_timeout: float = 0.0
    use_mock_devices: bool = True
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    thresholds: Thresholds = field(default_factory=Thresholds)
    hardware: HardwareConfig = field(default_factory=HardwareConfig)


def load_config(path: str | Path = "config/config.toml") -> AgentConfig:
    path = Path(path)
    if not path.exists():
        return AgentConfig()

    with path.open("rb") as f:
        raw = tomllib.load(f)

    mqtt = MqttConfig(**raw.pop("mqtt", {}))
    thresholds = Thresholds(**raw.pop("thresholds", {}))
    hardware = HardwareConfig(**raw.pop("hardware", {}))
    return AgentConfig(**raw, mqtt=mqtt, thresholds=thresholds, hardware=hardware)
