"""오프라인 모드 자동제어 규칙.

온라인일 때는 클라우드(기상청 API, 현관문 상태 등)가 제어를 결정하고,
통신이 끊겼을 때만 엣지가 로컬 센서값만으로 판단한다.

켜는 기준은 '초과' (같으면 보통, 프론트엔드 상태 등급과 같은 경계).

켜기와 끄기 기준을 다르게 둔다 (히스테리시스). 기준값 근처에서 값이 오르내릴 때
2초마다 켜고 끄기를 반복하지 않게 하려는 것이다. 공기청정기·제습기는 버튼을 누르는 서보라
반복하면 버튼이 계속 눌리고, 창문은 모터가 계속 왕복한다.
두 기준 사이에서는 결정을 내지 않아서 지금 상태가 유지된다.

| 기기      | 켜기(열기)                         | 끄기(닫기)                                  |
|-----------|-----------------------------------|---------------------------------------------|
| 공기청정기 | PM2.5 > pm25_high 또는 PM10 > pm10_high | PM2.5 <= pm25_high x 0.6 이고 PM10 <= pm10_high x 0.6 |
| 제습기    | 습도 > humidity_high               | 습도 <= humidity_off                         |
| 창문      | CO2 > co2_high 또는 온도 > temperature_high | CO2 <= co2_high - 200 이고 온도 <= temperature_high - 2 |
|           | (미세먼지가 높으면 무조건 닫기)        | (제습기가 없으면 습도 <= humidity_off 도)        |

공기청정기 끄기 비율 0.6 과 습도 켜기/끄기 분리는 cloud-server 의 온라인 자동화와 같다.

제습기가 연결되어 있으면 습도는 제습기로 처리하고 창문은 CO2/온도로만 연다.
(습한 날 창문을 열면 오히려 습해질 수 있고, 창문을 열고 제습기를 돌리면 낭비다.)
제습기가 없으면 습도가 높을 때 창문을 열어 환기한다.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from .config import Thresholds
from .devices import AIR_PURIFIER, DEHUMIDIFIER, WINDOW, Reading

DUST_OFF_RATIO = 0.6  # 미세먼지가 켜기 기준의 60% 이하가 되어야 공기청정기 끄기
CO2_MARGIN = 200.0  # ppm
TEMPERATURE_MARGIN = 2.0  # C


@dataclass(frozen=True)
class Decision:
    actuator: str
    on: bool
    reason: str


def decide(
    reading: Reading, t: Thresholds, available: Collection[str] | None = None
) -> list[Decision]:
    """센서값으로 액추에이터 목표 상태를 결정한다.

    켜기/끄기 기준 사이에 있는 기기는 결과에 없다 (지금 상태 유지).
    available: 연결된 액추에이터 이름. None 이면 제습기가 없는 것으로 본다.
    """
    has_dehumidifier = available is not None and DEHUMIDIFIER in available
    pm10 = getattr(reading, "pm10", None)

    dust_reasons = [
        f"{name} {value} > {limit}"
        for name, value, limit in (
            ("pm25", reading.pm25, t.pm25_high),
            ("pm10", pm10, t.pm10_high),
        )
        if value is not None and value > limit
    ]
    dust_clear = reading.pm25 <= t.pm25_high * DUST_OFF_RATIO and (
        pm10 is None or pm10 <= t.pm10_high * DUST_OFF_RATIO
    )
    humid = reading.humidity > t.humidity_high
    dry = reading.humidity <= t.humidity_off

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
                ("co2", reading.co2 > t.co2_high),
                ("humidity", humid and not has_dehumidifier),
                ("temperature", reading.temperature > t.temperature_high),
            )
            if flag
        ]
        calm = (
            reading.co2 <= t.co2_high - CO2_MARGIN
            and reading.temperature <= t.temperature_high - TEMPERATURE_MARGIN
            and (has_dehumidifier or dry)
        )
        if ventilate:
            decisions.append(Decision(WINDOW, True, "ventilate: " + ", ".join(ventilate)))
        elif calm:
            decisions.append(Decision(WINDOW, False, "air quality normal"))
        if dust_clear:
            decisions.append(Decision(AIR_PURIFIER, False, "dust normal"))

    if has_dehumidifier:
        if humid:
            reason = f"humidity {reading.humidity} > {t.humidity_high}"
            decisions.append(Decision(DEHUMIDIFIER, True, reason))
        elif dry:
            reason = f"humidity {reading.humidity} <= {t.humidity_off}"
            decisions.append(Decision(DEHUMIDIFIER, False, reason))

    return decisions
