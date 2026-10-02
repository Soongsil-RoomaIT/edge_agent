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
