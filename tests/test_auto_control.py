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
    # 기준값과 같으면 아직 '보통' (환경부 등급: PM2.5 35 이하 보통, PM10 80 이하 보통)
    assert targets(reading(pm25=35, pm10=80, co2=1000, humidity=70)) == {
        WINDOW: False,
        AIR_PURIFIER: False,
    }


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
