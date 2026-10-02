import sqlite3
import time

from edge_agent.devices import Reading
from edge_agent.storage import LocalStore


def make_reading(t: float) -> Reading:
    return Reading(measured_at=t, temperature=24.0, humidity=50.0, co2=600.0, pm25=10.0, pm10=20.0)


def test_unsynced_readings_until_marked():
    store = LocalStore(":memory:")
    ids = [store.save_reading(make_reading(time.time())) for _ in range(3)]

    assert [r["id"] for r in store.unsynced_readings()] == ids

    store.mark_readings_synced(ids[:2])
    assert [r["id"] for r in store.unsynced_readings()] == ids[2:]


def test_events_roundtrip():
    store = LocalStore(":memory:")
    eid = store.log_event("mode_changed", mode="offline")
    rows = store.unsynced_events()
    assert rows[0]["id"] == eid
    assert rows[0]["type"] == "mode_changed"
    assert '"offline"' in rows[0]["detail"]


def test_prune_only_removes_old_synced_rows():
    store = LocalStore(":memory:")
    old = time.time() - 10 * 86400
    old_synced = store.save_reading(make_reading(old))
    old_unsynced = store.save_reading(make_reading(old))
    store.mark_readings_synced([old_synced])

    store.prune(retention_days=7)

    remaining = [r["id"] for r in store.unsynced_readings()]
    assert remaining == [old_unsynced]


def test_migrates_old_db_without_pm10(tmp_path):
    # 라즈베리파이에 이미 만들어진 이전 버전 DB (pm10 컬럼 없음)
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT, measured_at REAL NOT NULL,
            temperature REAL NOT NULL, humidity REAL NOT NULL, co2 REAL NOT NULL,
            pm25 REAL NOT NULL, synced INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO readings (measured_at, temperature, humidity, co2, pm25)
            VALUES (1.0, 24.0, 50.0, 600.0, 10.0);
        """
    )
    conn.commit()
    conn.close()

    store = LocalStore(db)
    store.save_reading(make_reading(time.time()))

    rows = store.unsynced_readings()
    assert [r["pm10"] for r in rows] == [None, 20.0]
