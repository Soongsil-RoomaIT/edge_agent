from edge_agent.auto_control import decide
from edge_agent.config import Thresholds
from edge_agent.devices import AIR_PURIFIER, WINDOW, Reading


def reading(**kw) -> Reading:
    base = dict(measured_at=0.0, temperature=24.0, humidity=50.0, co2=600.0, pm25=10.0, pm10=20.0)
    base.update(kw)
    return Reading(**base)


def targets(r: Reading) -> dict[str, bool]:
    return {d.actuator: d.on for d in decide(r, Thresholds())}


def test_normal_air_keeps_everything_off():
    assert targets(reading()) == {WINDOW: False, AIR_PURIFIER: False}


def test_high_co2_opens_window():
    assert targets(reading(co2=1200)) == {WINDOW: True, AIR_PURIFIER: False}


def test_high_humidity_opens_window():
    assert targets(reading(humidity=80))[WINDOW] is True


def test_dust_overrides_ventilation():
    # CO2 가 높아도 미세먼지가 나쁘면 창문을 닫고 공기청정기를 켠다
    assert targets(reading(co2=1500, pm25=60)) == {WINDOW: False, AIR_PURIFIER: True}


def test_high_pm10_alone_triggers_dust_mode():
    assert targets(reading(pm10=120)) == {WINDOW: False, AIR_PURIFIER: True}


def test_thresholds_are_exclusive():
    # 기준값과 같으면 아직 '보통' (환경부 등급: PM2.5 35 이하 보통, PM10 80 이하 보통) -> 켜는 결정 없음
    t = targets(reading(pm25=35, pm10=80, co2=1000, humidity=70))
    assert True not in t.values()


# --- 히스테리시스 (켜기/끄기 기준 사이는 현재 상태 유지) -------------------------


def test_purifier_stays_as_is_between_on_and_off():
    # 35 초과면 켬, 21(=35x0.6) 이하면 끔, 그 사이는 결정 없음
    assert targets(reading(pm25=36))[AIR_PURIFIER] is True
    assert AIR_PURIFIER not in targets(reading(pm25=30))
    assert targets(reading(pm25=21))[AIR_PURIFIER] is False


def test_pm10_must_also_drop_to_turn_purifier_off():
    assert AIR_PURIFIER not in targets(reading(pm25=10, pm10=60))  # 60 > 48(=80x0.6)


def test_window_stays_as_is_between_open_and_close():
    assert targets(reading(co2=1100))[WINDOW] is True
    assert WINDOW not in targets(reading(co2=900))  # 800 < 900 <= 1000
    assert targets(reading(co2=800))[WINDOW] is False


def test_window_close_needs_temperature_margin():
    assert WINDOW not in targets(reading(temperature=29))  # 28 < 29 <= 30
    assert targets(reading(temperature=28))[WINDOW] is False


def test_without_dehumidifier_humidity_must_drop_to_close_window():
    assert targets(reading(humidity=71))[WINDOW] is True
    assert WINDOW not in targets(reading(humidity=65))  # 60 < 65 <= 70
    assert targets(reading(humidity=60))[WINDOW] is False


def test_value_wobbling_around_threshold_does_not_toggle():
    # 34 -> 36 -> 34 -> 36 : 처음 켠 뒤로는 끄는 결정이 안 나온다
    seq = [targets(reading(pm25=v)).get(AIR_PURIFIER) for v in (36, 34, 36, 34)]
    assert seq == [True, None, True, None]


# --- 제습기 ---------------------------------------------------------------

from edge_agent.devices import DEHUMIDIFIER  # noqa: E402

WITH_DEHUMIDIFIER = {WINDOW, AIR_PURIFIER, DEHUMIDIFIER}


def targets_with_dehumidifier(r: Reading) -> dict[str, bool]:
    return {d.actuator: d.on for d in decide(r, Thresholds(), WITH_DEHUMIDIFIER)}


def test_high_humidity_runs_dehumidifier_instead_of_opening_window():
    assert targets_with_dehumidifier(reading(humidity=80)) == {
        WINDOW: False,
        AIR_PURIFIER: False,
        DEHUMIDIFIER: True,
    }


def test_normal_humidity_turns_dehumidifier_off():
    assert targets_with_dehumidifier(reading())[DEHUMIDIFIER] is False


def test_dust_and_humidity_together():
    # 창문 닫고 공기청정기 + 제습기
    assert targets_with_dehumidifier(reading(humidity=80, pm25=60)) == {
        WINDOW: False,
        AIR_PURIFIER: True,
        DEHUMIDIFIER: True,
    }


def test_co2_still_opens_window_with_dehumidifier():
    t = targets_with_dehumidifier(reading(co2=1500, humidity=80))
    assert t[WINDOW] is True and t[DEHUMIDIFIER] is True


def test_dehumidifier_stays_as_is_between_on_and_off():
    assert DEHUMIDIFIER not in targets_with_dehumidifier(reading(humidity=65))  # 60 < 65 <= 70
    assert targets_with_dehumidifier(reading(humidity=60))[DEHUMIDIFIER] is False


def test_window_ignores_humidity_when_dehumidifier_present():
    # 습도 65 는 제습기 히스테리시스 구간이지만 창문은 습도를 보지 않으므로 닫는다
    assert targets_with_dehumidifier(reading(humidity=65))[WINDOW] is False
