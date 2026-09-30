import time

from edge_agent.devices import Reading
from edge_agent.storage import LocalStore


def make_reading(t: float) -> Reading:
    return Reading(measured_at=t, temperature=24.0, humidity=50.0, co2=600.0, pm25=10.0)


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
