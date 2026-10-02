"""센서/액추에이터 인터페이스.

실제 드라이버는 hardware 레포에서 구현하고, 여기서는 그 인터페이스만 정의한다.
하드웨어가 준비되기 전에는 Mock 구현으로 개발/테스트한다.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Reading:
    measured_at: float  # unix time
    temperature: float  # C
    humidity: float  # %
    co2: float  # ppm
    pm25: float  # ug/m3, 초미세먼지
    pm10: float  # ug/m3, 미세먼지


class SensorReader(Protocol):
    def read(self) -> Reading: ...


class DoorSensor(Protocol):
    @property
    def is_open(self) -> bool: ...


class Actuator(Protocol):
    """켜기/끄기 두 상태로 다루는 액추에이터. 창문/문은 on=열림."""

    name: str

    @property
    def is_on(self) -> bool: ...

    def set(self, on: bool) -> None: ...


# 액추에이터 이름 (MQTT 명령, 자동제어 규칙에서 공통으로 사용)
WINDOW = "window"
AIR_PURIFIER = "air_purifier"
DOOR = "door"  # 서보로 닫기만 가능 (set(False))


class MockSensorReader:
    """실내 환경을 흉내 내는 가짜 센서."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._co2 = 600.0
        self._pm25 = 15.0

    def read(self) -> Reading:
        self._co2 = max(400.0, self._co2 + self._rng.uniform(-40, 60))
        self._pm25 = max(0.0, self._pm25 + self._rng.uniform(-3, 3))
        # PM10 은 PM2.5 를 포함하므로 항상 그보다 크거나 같다
        pm10 = self._pm25 * self._rng.uniform(1.4, 1.9)
        return Reading(
            measured_at=time.time(),
            temperature=round(self._rng.uniform(22, 27), 1),
            humidity=round(self._rng.uniform(40, 65), 1),
            co2=round(self._co2),
            pm25=round(self._pm25, 1),
            pm10=round(pm10, 1),
        )


class MockActuator:
    def __init__(self, name: str) -> None:
        self.name = name
        self._on = False

    @property
    def is_on(self) -> bool:
        return self._on

    def set(self, on: bool) -> None:
        self._on = on


class MockDoorSensor:
    def __init__(self, is_open: bool = False) -> None:
        self.is_open = is_open


class MockDoorCloser:
    """문 닫기 서보 흉내. set(False) 면 문이 닫힌다. 열기는 불가."""

    name = DOOR

    def __init__(self, sensor: MockDoorSensor) -> None:
        self._sensor = sensor

    @property
    def is_on(self) -> bool:
        return self._sensor.is_open

    def set(self, on: bool) -> None:
        if not on:
            self._sensor.is_open = False
