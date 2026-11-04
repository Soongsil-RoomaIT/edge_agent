"""오프라인 모드 자동제어 규칙.

온라인일 때는 클라우드(기상청 API, 현관문 상태 등)가 제어를 결정하고,
통신이 끊겼을 때만 엣지가 로컬 센서값만으로 판단한다.

기준값은 모두 '초과' 기준이다 (프론트엔드 상태 등급과 같은 경계).

제습기가 연결되어 있으면 습도는 제습기로 처리하고 창문은 CO2/온도로만 연다.
(습한 날 창문을 열면 오히려 습해질 수 있고, 창문을 열고 제습기를 돌리면 낭비다.)
제습기가 없으면 예전처럼 습도가 높을 때 창문을 열어 환기한다.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from .config import Thresholds
from .devices import AIR_PURIFIER, DEHUMIDIFIER, WINDOW, Reading


@dataclass(frozen=True)
class Decision:
    actuator: str
    on: bool
    reason: str


def decide(
    reading: Reading, t: Thresholds, available: Collection[str] | None = None
) -> list[Decision]:
    """센서값으로 각 액추에이터의 목표 상태를 결정한다.

    available: 연결된 액추에이터 이름. None 이면 제습기가 없는 것으로 본다.
    """
    has_dehumidifier = available is not None and DEHUMIDIFIER in available

    dust_reasons = [
        f"{name} {value} > {limit}"
        for name, value, limit in (
            ("pm25", reading.pm25, t.pm25_high),
            ("pm10", getattr(reading, "pm10", None), t.pm10_high),
        )
        if value is not None and value > limit
    ]
    stuffy = reading.co2 > t.co2_high
    humid = reading.humidity > t.humidity_high
    hot = reading.temperature > t.temperature_high

    decisions: list[Decision] = []

    if dust_reasons:
        # 미세먼지가 높으면 환기보다 우선: 창문 닫고 공기청정기 가동
        reason = ", ".join(dust_reasons)
        decisions.append(Decision(WINDOW, False, reason))
        decisions.append(Decision(AIR_PURIFIER, True, reason))
    else:
        ventilate = [
            name
            for name, flag in (
                ("co2", stuffy),
                ("humidity", humid and not has_dehumidifier),
                ("temperature", hot),
            )
            if flag
        ]
        if ventilate:
            decisions.append(Decision(WINDOW, True, "ventilate: " + ", ".join(ventilate)))
        else:
            decisions.append(Decision(WINDOW, False, "air quality normal"))
        decisions.append(Decision(AIR_PURIFIER, False, "dust normal"))

    if has_dehumidifier:
        reason = (
            f"humidity {reading.humidity} > {t.humidity_high}" if humid else "humidity normal"
        )
        decisions.append(Decision(DEHUMIDIFIER, humid, reason))

    return decisions
