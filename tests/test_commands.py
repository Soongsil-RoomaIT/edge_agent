"""원격 명령 (roomait-v1 commands -> ack)."""

import time

from conftest import command, event_types, make_agent

from edge_agent.devices import AIR_PURIFIER, DOOR, WINDOW, MockActuator, MockDoorCloser, MockDoorSensor
from edge_agent.storage import LocalStore


class SlowWindow:
    """hardware WindowActuator 흉내: set() 은 바로 돌아오고, 모터가 움직인 뒤 position 이 바뀐다."""

    name = WINDOW

    def __init__(self) -> None:
        self.position = "closed"
        self.fault = None
        self.is_moving = False
        self.calls: list[bool] = []

    @property
    def is_on(self) -> bool:
        return self.position == "open"

    def set(self, on: bool) -> None:
        self.calls.append(on)
        self.is_moving = True
        self.position = "opening" if on else "closing"

    def arrive(self) -> None:
        self.position = "open" if self.position == "opening" else "closed"
        self.is_moving = False


class BrokenWindow(MockActuator):
    def __init__(self) -> None:
        super().__init__(WINDOW)

    def set(self, on: bool) -> None:
        raise RuntimeError("servo stalled")


def test_ack_is_sent_only_after_target_is_reached():
    window = SlowWindow()
    agent = make_agent({WINDOW: window}, online=True)
    cmd = command("window-1", "OPEN")

    agent._handle_command(cmd)
    agent._check_pending()
    assert agent.mqtt.acks() == []  # 아직 움직이는 중

    window.arrive()
    agent._check_pending()

    (ack,) = agent.mqtt.acks()
    assert ack["commandId"] == cmd["commandId"]
    assert ack["status"] == "EXECUTED"
    assert ack["executedAt"].endswith("Z")
    assert set(ack) == {"commandId", "status", "reason", "executedAt"}


def test_not_reaching_target_before_expiry_fails():
    window = SlowWindow()
    agent = make_agent({WINDOW: window}, online=True)
    agent._handle_command(command("window-1", "OPEN", ttl=0.05))

    time.sleep(0.1)
    agent._check_pending()

    (ack,) = agent.mqtt.acks()
    assert ack["status"] == "FAILED"
    assert "deadline" in ack["reason"]


def test_fault_while_moving_fails():
    window = SlowWindow()
    agent = make_agent({WINDOW: window}, online=True)
    agent._handle_command(command("window-1", "OPEN"))

    window.fault = "obstruction"
    agent._check_pending()

    assert agent.mqtt.acks()[0]["status"] == "FAILED"
    assert "obstruction" in agent.mqtt.acks()[0]["reason"]


def test_duplicate_command_runs_once_and_resends_ack():
    window = SlowWindow()
    agent = make_agent({WINDOW: window}, online=True)
    cmd = command("window-1", "OPEN")

    agent._handle_command(cmd)
    agent._handle_command(cmd)  # 실행 중 중복 -> 무시
    window.arrive()
    agent._check_pending()
    agent._handle_command(cmd)  # 완료 후 중복 -> ack 재전송

    assert window.calls == [True]
    assert len(agent.mqtt.acks()) == 2
    assert agent.mqtt.acks()[0] == agent.mqtt.acks()[1]


def test_duplicate_after_restart_is_not_executed_again(tmp_path):
    db = tmp_path / "edge.db"
    cmd = command("window-1", "OPEN")
    first = make_agent({WINDOW: SlowWindow()}, store=LocalStore(db), online=True)
    first._handle_command(cmd)  # 완료 전에 재시작
    first.store.close()

    window = SlowWindow()
    restarted = make_agent({WINDOW: window}, store=LocalStore(db), online=True)
    restarted._handle_command(cmd)

    assert window.calls == []
    (ack,) = restarted.mqtt.acks()
    assert ack["status"] == "FAILED"
    assert "restart" in ack["reason"]


def test_expired_command_is_not_executed():
    window = SlowWindow()
    agent = make_agent({WINDOW: window}, online=True)

    agent._handle_command(command("window-1", "OPEN", ttl=-1))

    assert window.calls == []
    assert agent.mqtt.acks()[0]["status"] == "FAILED"
    assert "expired" in agent.mqtt.acks()[0]["reason"]


def test_unknown_device_fails():
    agent = make_agent({}, online=True)

    agent._handle_command(command("tv-1", "ON"))

    assert "unknown device" in agent.mqtt.acks()[0]["reason"]


def test_action_must_match_device_type():
    agent = make_agent({AIR_PURIFIER: MockActuator(AIR_PURIFIER)}, online=True)

    agent._handle_command(command("purifier-1", "OPEN"))

    assert agent.mqtt.acks()[0]["status"] == "FAILED"


def test_door_open_is_rejected():
    door = MockDoorSensor(is_open=False)
    agent = make_agent({DOOR: MockDoorCloser(door)}, online=True)

    agent._handle_command(command("door-1", "OPEN"))

    assert agent.mqtt.acks()[0]["reason"] == "door can only be closed"


def test_door_close_is_confirmed_by_door_sensor():
    door = MockDoorSensor(is_open=True)
    agent = make_agent({DOOR: MockDoorCloser(door)}, door_sensor=door, online=True)

    agent._handle_command(command("door-1", "CLOSE"))
    agent._check_pending()

    assert door.is_open is False
    assert agent.mqtt.acks()[0]["status"] == "EXECUTED"


def test_actuator_exception_fails_immediately():
    agent = make_agent({WINDOW: BrokenWindow()}, online=True)

    agent._handle_command(command("window-1", "OPEN"))

    assert agent.mqtt.acks()[0]["status"] == "FAILED"
    assert agent.mqtt.acks()[0]["reason"] == "servo stalled"
    assert "actuator_error" in event_types(agent)


def test_newer_command_supersedes_running_one():
    window = SlowWindow()
    agent = make_agent({WINDOW: window}, online=True)
    first, second = command("window-1", "OPEN"), command("window-1", "CLOSE")

    agent._handle_command(first)
    agent._handle_command(second)

    (ack,) = agent.mqtt.acks()
    assert ack["commandId"] == first["commandId"]
    assert ack["status"] == "FAILED" and "superseded" in ack["reason"]


def test_command_without_id_is_ignored():
    window = SlowWindow()
    agent = make_agent({WINDOW: window}, online=True)
    cmd = command("window-1", "OPEN")
    del cmd["commandId"]

    agent._handle_command(cmd)

    assert window.calls == []
    assert agent.mqtt.acks() == []
