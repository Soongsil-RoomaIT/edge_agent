"""센서가 일부만 달린 구성 (예: DHT11 온습도만, CO2·PM 은 None).

없는 값은 0 으로 만들지 않고 null 로 저장·전송하고, 자동제어도 없는 값 때문에 멈추지 않는다.
(issue #8, cloud-server bbd284e 부터 5개 값 모두 null 허용)
"""

from __future__ import annotations

import sqlite3

from conftest import make_agent

from edge_agent.auto_control import decide
from edge_agent.config import Thresholds
from edge_agent.devices import AIR_PURIFIER, DEHUMIDIFIER, WINDOW, MockActuator, Reading
from edge_agent.storage import LocalStore


def dht_only(temperature: float = 24.3, humidity: float = 52.1) -> Reading:
    return Reading(measured_at=1762300000.0, temperature=temperature, humidity=humidity, co2=None, pm25=None)


class DhtOnlySensor:
    def __init__(self, **values) -> None:
        self.values = values

    def read(self) -> Reading:
        return dht_only(**self.values)


def targets(reading: Reading, available=(WINDOW, AIR_PURIFIER)) -> dict[str, bool]:
    return {d.actuator: d.on for d in decide(reading, Thresholds(), available)}


# --- 저장 / 전송 -------------------------------------------------------------


def test_dht_only_reading_is_sent_with_nulls():
    agent = make_agent(sensor=DhtOnlySensor(), online=True)

    agent._sample()
    agent._flush_backlog()

    (payload,) = agent.mqtt.on(agent.mqtt.topic_sensors)
    assert payload["temperature"] == 24.3
    assert payload["humidity"] == 52.1
    assert payload["co2"] is None and payload["pm25"] is None and payload["pm10"] is None
    assert agent.store.unsynced_readings() == []


def test_reading_without_pm10_is_sent_not_dropped():
    agent = make_agent(online=True)
    agent.store.save_reading(
        Reading(measured_at=1762300000.0, temperature=23.0, humidity=60.0, co2=900.0, pm25=15.0)
    )

    agent._flush_backlog()

    (payload,) = agent.mqtt.on(agent.mqtt.topic_sensors)
    assert payload["co2"] == 900.0 and payload["pm10"] is None


def test_offline_partial_readings_are_uploaded_after_recovery():
    agent = make_agent(sensor=DhtOnlySensor(), online=False)
    agent._sample()
    agent._sample()
    assert agent.mqtt.on(agent.mqtt.topic_sensors) == []

    agent._on_broker_connected()
    agent._check_mode()
    agent._flush_backlog()

    assert len(agent.mqtt.on(agent.mqtt.topic_sensors)) == 2


def test_old_database_with_not_null_columns_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE readings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            measured_at REAL    NOT NULL,
            temperature REAL    NOT NULL,
            humidity    REAL    NOT NULL,
            co2         REAL    NOT NULL,
            pm25        REAL    NOT NULL,
            synced      INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO readings (measured_at, temperature, humidity, co2, pm25) VALUES (1.0, 20.0, 50.0, 600.0, 10.0);
        """
    )
    conn.commit()
    conn.close()

    store = LocalStore(db)
    store.save_reading(dht_only())

    old, new = store.unsynced_readings()
    assert (old["temperature"], old["co2"], old["pm10"]) == (20.0, 600.0, None)  # 기존 행 보존
    assert (new["temperature"], new["co2"], new["pm25"]) == (24.3, None, None)
    assert old["gas_detected"] is None and new["gas_detected"] is None
    store.close()
    LocalStore(db).close()  # 이미 바뀐 DB 를 다시 열어도 문제없음


# --- 자동제어 ----------------------------------------------------------------


def test_normal_dht_only_reading_closes_window_and_does_not_crash():
    assert targets(dht_only()) == {WINDOW: False}


def test_purifier_is_left_alone_without_dust_sensor():
    # PM 센서가 없으면 켜지도 끄지도 않는다 (원격으로 켠 공기청정기를 근거 없이 끄지 않음)
    assert AIR_PURIFIER not in targets(dht_only(temperature=35.0, humidity=80.0))


def test_high_temperature_still_opens_window_without_co2_sensor():
    assert targets(dht_only(temperature=35.0))[WINDOW] is True


def test_high_humidity_runs_dehumidifier_without_other_sensors():
    t = targets(dht_only(humidity=80.0), available=(WINDOW, AIR_PURIFIER, DEHUMIDIFIER))
    assert t[DEHUMIDIFIER] is True


def test_no_known_values_makes_no_window_decision():
    empty = Reading(measured_at=0.0, temperature=None, humidity=None, co2=None, pm25=None)
    assert WINDOW not in targets(empty)


def test_agent_auto_control_runs_with_dht_only_sensor():
    window = MockActuator(WINDOW)
    window.set(True)
    agent = make_agent({WINDOW: window}, sensor=DhtOnlySensor())

    agent._sample()  # 예외 없이 돌아야 하고, 정상 범위라 창문을 닫는다
    agent._check_pending()

    assert window.is_on is False
