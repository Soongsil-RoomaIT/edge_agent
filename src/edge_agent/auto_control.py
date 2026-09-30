"""오프라인 모드 자동제어 규칙.

온라인일 때는 클라우드(기상청 API, 현관문 상태 등)가 제어를 결정하고,
통신이 끊겼을 때만 엣지가 로컬 센서값만으로 판단한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Thresholds
from .devices import AIR_PURIFIER, WINDOW, Reading


@dataclass(frozen=True)
class Decision:
    actuator: str
    on: bool
    reason: str


def decide(reading: Reading, t: Thresholds) -> list[Decision]:
    """센서값으로 각 액추에이터의 목표 상태를 결정한다."""
    dusty = reading.pm25 >= t.pm25_high
    stuffy = reading.co2 >= t.co2_high
    humid = reading.humidity >= t.humidity_high
    hot = reading.temperature >= t.temperature_high

    decisions: list[Decision] = []

    # 미세먼지가 높으면 환기보다 우선: 창문 닫고 공기청정기 가동
    if dusty:
        decisions.append(Decision(WINDOW, False, f"pm25 {reading.pm25} >= {t.pm25_high}"))
        decisions.append(Decision(AIR_PURIFIER, True, f"pm25 {reading.pm25} >= {t.pm25_high}"))
        return decisions

    if stuffy or humid or hot:
        reasons = [
            name
            for name, flag in (("co2", stuffy), ("humidity", humid), ("temperature", hot))
            if flag
        ]
        decisions.append(Decision(WINDOW, True, "ventilate: " + ", ".join(reasons)))
    else:
        decisions.append(Decision(WINDOW, False, "air quality normal"))

    decisions.append(Decision(AIR_PURIFIER, False, "pm25 normal"))
    return decisions
