from edge_agent.agent import EdgeAgent
from edge_agent.config import AgentConfig
from edge_agent.devices import DOOR, MockDoorCloser, MockDoorSensor, MockSensorReader
from edge_agent.storage import LocalStore


def make_agent(door: MockDoorSensor) -> EdgeAgent:
    return EdgeAgent(
        AgentConfig(),
        MockSensorReader(seed=0),
        {DOOR: MockDoorCloser(door)},
        LocalStore(":memory:"),
        door_sensor=door,
    )


def events(agent: EdgeAgent) -> list[tuple[str, str]]:
    return [(r["type"], r["detail"]) for r in agent.store.unsynced_events()]


def test_door_events_only_on_change():
    door = MockDoorSensor(is_open=False)
    agent = make_agent(door)

    agent._check_door()  # 첫 상태 보고
    agent._check_door()  # 변화 없음
    door.is_open = True
    agent._check_door()
    door.is_open = False
    agent._check_door()

    assert [t for t, _ in events(agent)] == ["door_closed", "door_opened", "door_closed"]
    assert '"initial": true' in events(agent)[0][1]
    assert '"initial": false' in events(agent)[1][1]


def test_remote_close_command_closes_door():
    door = MockDoorSensor(is_open=True)
    agent = make_agent(door)

    agent._handle_command({"actuator": DOOR, "on": False})

    assert door.is_open is False


def test_remote_open_command_is_ignored_by_closer():
    door = MockDoorSensor(is_open=False)
    agent = make_agent(door)

    agent._handle_command({"actuator": DOOR, "on": True})

    assert door.is_open is False
