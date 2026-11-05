"""Edge Agent 메인 루프.

    센서 측정 -> SQLite 저장 -> (온라인) 클라우드 전송 / (오프라인) 로컬 자동제어

- 측정값은 항상 먼저 로컬에 저장하고, 전송 성공분만 synced 로 표시한다.
- 연결 상태는 측정 주기와 별개로 TICK 마다 확인해서 10초 이내 전환(NFR03)을 보장한다.
  브로커 연결에 더해, 설정하면 클라우드 서버 heartbeat 도 본다 (브로커만 살아 있는 경우).
- 원격 명령은 commandId 를 SQLite 에 기록해 한 번만 실행하고, expiresAt 이 지났으면 실행하지 않는다.
  ack 는 명령을 받았을 때가 아니라 기기가 실제로 목표 상태에 도달했을 때 보낸다.
- 기기 상태는 바뀔 때와 연결될 때 devices 로 보낸다. status heartbeat 는 주기적으로 보낸다.
- 복구되면 오프라인 동안 실제 완료한 자동제어 동작을 recovery 로 먼저 보내고, 밀린 측정값을 보낸다.
- 클라우드가 보낸 기준값(config)은 안전 범위를 검사한 뒤 적용하고 로컬에 저장한다.
- 문 열림/닫힘이 바뀌면 바로 이벤트와 기기 상태를 보낸다.
  문 10분 자동 닫기 정책은 클라우드가 담당한다 (hardware DoorAutoCloser 와 동시에 켜지 않는다).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any

from . import auto_control, protocol
from .config import AgentConfig, Thresholds, apply_threshold_update
from .connectivity import ConnectivityMonitor, Mode
from .devices import (
    AIR_PURIFIER,
    DEHUMIDIFIER,
    DOOR,
    WINDOW,
    Actuator,
    DoorSensor,
    MockActuator,
    MockDoorCloser,
    MockDoorSensor,
    MockSensorReader,
    SensorReader,
)
from .storage import LocalStore

log = logging.getLogger(__name__)

TICK = 0.5  # 연결 상태 확인 주기(초)
AUTO_ACTION_TIMEOUT = 30.0  # 자동제어 동작이 목표에 도달하기를 기다리는 시간(초). 창문 모터 15초 + 여유


@dataclass
class PendingAction:
    """set() 을 보냈고 목표 상태 도달을 기다리는 동작."""

    actuator: str
    on: bool
    deadline: float  # unix
    reason: str
    command_id: str | None = None  # None = 자동제어


class EdgeAgent:
    def __init__(
        self,
        cfg: AgentConfig,
        sensor: SensorReader,
        actuators: dict[str, Actuator],
        store: LocalStore,
        door_sensor: DoorSensor | None = None,
    ) -> None:
        self.cfg = cfg
        self.sensor = sensor
        self.actuators = actuators
        self.store = store
        self.door_sensor = door_sensor
        self._door_open: bool | None = None  # None = 아직 한 번도 안 읽음
        self.monitor = ConnectivityMonitor(cfg.offline_grace, clock=time.monotonic)
        self._actuator_lock = threading.RLock()
        self._stop = threading.Event()
        self._offline_since: float | None = None
        self._sensor_failures = 0
        self._pm10_warned = False
        # 자동제어를 건너뛴 액추에이터와 그 사유. 같은 사유는 이벤트를 한 번만 남긴다.
        self._actuator_issues: dict[str, str] = {}
        self._pending: dict[str, PendingAction] = {}  # 액추에이터 이름 -> 진행 중인 동작
        self._last_states: dict[str, str] = {}  # 기기 id -> 마지막으로 보낸 상태
        self._next_status = 0.0
        self._broker_up = False
        self._cloud_seen: float | None = None  # 마지막 클라우드 heartbeat (monotonic)
        self._config_version: int | None = None
        self._load_saved_thresholds()

        # paho 는 실제 라즈베리파이/개발 PC 에서만 필요하므로 여기서 import
        from .mqtt_client import EdgeMqttClient

        self.mqtt = EdgeMqttClient(
            cfg.mqtt,
            cfg.device_id,
            on_connected=self._on_broker_connected,
            on_disconnected=self._on_broker_disconnected,
            on_command=self._handle_command,
            on_config=self._handle_config,
            on_cloud_status=self._handle_cloud_status,
        )

    # --- lifecycle ------------------------------------------------------

    def run(self) -> None:
        log.info("edge agent starting (device_id=%s)", self.cfg.device_id)
        log.info("starting in OFFLINE mode until MQTT connects")
        self.store.log_event("agent_started")
        self.mqtt.start()
        next_sample = time.monotonic()
        next_prune = time.monotonic()
        try:
            while not self._stop.is_set():
                self._check_cloud_lease()
                self._check_mode()
                self._check_door()
                self._check_pending()
                self._publish_devices()
                self._publish_status()

                now = time.monotonic()
                if now >= next_sample:
                    self._sample()
                    next_sample = now + self.cfg.sample_interval
                if now >= next_prune:
                    self.store.prune(self.cfg.retention_days)
                    next_prune = now + 3600

                self._stop.wait(TICK)
        finally:
            self.mqtt.stop()
            self.store.close()
            log.info("edge agent stopped")

    def stop(self) -> None:
        self._stop.set()

    # --- connectivity ---------------------------------------------------

    def _cloud_alive(self) -> bool:
        timeout = self.cfg.cloud_heartbeat_timeout
        if timeout <= 0:
            return True
        seen = self._cloud_seen
        return seen is not None and time.monotonic() - seen < timeout

    def _on_broker_connected(self) -> None:
        self._broker_up = True
        self._next_status = 0.0  # 바로 status 를 보낸다
        if self._cloud_alive():
            self.monitor.mark_connected()

    def _on_broker_disconnected(self) -> None:
        self._broker_up = False
        self.monitor.mark_disconnected()

    def _handle_cloud_status(self, msg: dict[str, Any]) -> None:
        """클라우드 서버 heartbeat. 예: {"online": true}. 클라우드 LWT 는 {"online": false}."""
        if msg.get("online") is True:
            self._cloud_seen = time.monotonic()
            if self._broker_up:
                self.monitor.mark_connected()
        elif msg.get("online") is False:
            self._cloud_seen = None
            self.monitor.mark_disconnected()

    def _check_cloud_lease(self) -> None:
        """브로커는 붙어 있는데 클라우드 heartbeat 가 끊긴 경우를 끊김으로 본다."""
        if not self._broker_up or self.cfg.cloud_heartbeat_timeout <= 0:
            return
        if self._cloud_alive():
            self.monitor.mark_connected()
        else:
            self.monitor.mark_disconnected()  # 이미 끊김 상태면 아무것도 안 바뀜

    def _check_mode(self) -> None:
        changed = self.monitor.update()
        if changed is Mode.OFFLINE:
            self._offline_since = time.time()
            log.warning("cloud unreachable -> OFFLINE auto-control mode")
            self.store.log_event("mode_changed", mode="offline")
            self._next_status = 0.0  # 브로커가 살아 있으면 offlineMode=true 를 바로 알림
        elif changed is Mode.ONLINE:
            now = time.time()
            offline_since, self._offline_since = self._offline_since, None
            log.info("cloud reachable -> ONLINE mode")
            self.store.log_event("mode_changed", mode="online")
            if offline_since is not None:
                self.store.log_event("reconnected", offline_seconds=round(now - offline_since))
                self._send_recovery(offline_since, now)  # status 보다 먼저
            self._last_states.clear()  # 기기 상태 스냅샷을 다시 보낸다
            self._next_status = 0.0
            self._publish_status()
            self._flush_backlog()

    def _send_recovery(self, offline_since: float, recovered_at: float) -> None:
        rows = self.store.unreported_local_actions(offline_since, recovered_at)
        payload = {
            "offlineSince": protocol.iso(offline_since),
            "recoveredAt": protocol.iso(recovered_at),
            "localActions": [
                {
                    "executedAt": protocol.iso(r["executed_at"]),
                    "deviceId": protocol.DEVICE_IDS[r["actuator"]],
                    "state": protocol.device_state(r["actuator"], bool(r["on_state"])),
                    "reason": r["reason"][:500],
                }
                for r in rows
                if r["actuator"] in protocol.DEVICE_IDS
            ],
        }
        if self.mqtt.publish_json(self.mqtt.topic_recovery, payload):
            self.store.mark_local_actions_reported([r["id"] for r in rows])
            log.info("recovery report sent (%d local actions)", len(payload["localActions"]))

    def _publish_status(self) -> None:
        """status heartbeat. 브로커에 붙어 있으면 오프라인 모드여도 보낸다 (offlineMode=true)."""
        if not self._broker_up or time.monotonic() < self._next_status:
            return
        payload = protocol.status(offline_mode=self.monitor.mode is Mode.OFFLINE)
        if self.mqtt.publish_json(self.mqtt.topic_status, payload):
            self._next_status = time.monotonic() + self.cfg.status_interval

    # --- sensing --------------------------------------------------------

    def _check_door(self) -> None:
        if self.door_sensor is None:
            return
        try:
            is_open = self.door_sensor.is_open
        except Exception as e:
            log.debug("door sensor read failed: %s", e)
            return
        if is_open == self._door_open:
            return

        initial = self._door_open is None
        self._door_open = is_open
        log.info("door %s%s", "opened" if is_open else "closed", " (initial)" if initial else "")
        self.store.log_event("door_opened" if is_open else "door_closed", initial=initial)
        if self.monitor.mode is Mode.ONLINE:
            self._publish_devices()
            self._flush_backlog()  # 측정 주기를 기다리지 않고 바로 전송

    def _sample(self) -> None:
        try:
            reading = self.sensor.read()
        except Exception as e:
            # 센서 하나가 실패해도 에이전트는 계속 돌아야 한다 (통신 감시, 원격 조작 유지)
            self._sensor_failures += 1
            if self._sensor_failures == 1:
                log.warning("sensor read failed: %s", e)
                self.store.log_event("sensor_error", error=str(e))
            else:
                log.debug("sensor read failed (%d in a row): %s", self._sensor_failures, e)
            return

        if self._sensor_failures:
            log.info("sensor recovered after %d failed reads", self._sensor_failures)
            self.store.log_event("sensor_recovered", failed_reads=self._sensor_failures)
            self._sensor_failures = 0

        self.store.save_reading(reading)

        if self.monitor.mode is Mode.OFFLINE:
            self._apply_auto_control(reading)
        else:
            self._flush_backlog()

    # --- offline auto control -------------------------------------------

    def _apply_auto_control(self, reading) -> None:
        for d in auto_control.decide(reading, self.cfg.thresholds, self.actuators.keys()):
            act = self.actuators.get(d.actuator)
            if act is None:
                continue
            try:
                with self._actuator_lock:
                    current, issue = self._inspect_actuator(act)
                    if issue is not None:
                        self._report_actuator_issue(d.actuator, issue)
                        continue
                    self._clear_actuator_issue(d.actuator)
                    if current == d.on:
                        continue
                    pending = self._pending.get(d.actuator)
                    if pending is not None and pending.on == d.on:
                        continue  # 이미 같은 목표로 움직이는 중
                    # current 가 None 이면 창문이 중간/미확인 위치 -> 목표로 다시 보낸다 (set 은 멱등)
                    act.set(d.on)
                    self._track(
                        PendingAction(d.actuator, d.on, time.time() + AUTO_ACTION_TIMEOUT, d.reason)
                    )
            except Exception as e:
                log.error("auto-control: %s -> %s failed: %s", d.actuator, d.on, e)
                self.store.log_event("actuator_error", actuator=d.actuator, on=d.on, error=str(e))
                continue
            log.info("auto-control: %s -> %s (%s)", d.actuator, d.on, d.reason)
            self.store.log_event("auto_control", actuator=d.actuator, on=d.on, reason=d.reason)

    @staticmethod
    def _inspect_actuator(act: Actuator) -> tuple[bool | None, str | None]:
        """(현재 상태, 자동제어를 하면 안 되는 사유) 를 돌려준다.

        - 고장(fault)            -> 명령하지 않음. 사람이 점검해야 한다.
        - 창문 position           -> "closed" 만 닫힘으로 본다. is_on=False 는 중간 위치일 수도 있다.
        - 가전 state "unknown"    -> 명령하지 않음. 토글 버튼이라 모르는 상태에서 누르면 반대로 될 수 있다.
        """
        fault = getattr(act, "fault", None)
        if fault:
            return None, f"fault: {fault}"

        position = getattr(act, "position", None)
        if position is not None:
            if position == "fault":
                return None, "fault: position"
            return {"open": True, "closed": False}.get(position), None

        state = getattr(act, "state", None)
        if state == "unknown":
            return None, "state unknown"
        try:
            return act.is_on, None
        except Exception as e:  # hardware 는 상태를 모르면 HardwareFault 를 낸다
            return None, f"state error: {e}"

    def _report_actuator_issue(self, name: str, issue: str) -> None:
        if self._actuator_issues.get(name) == issue:
            return
        self._actuator_issues[name] = issue
        log.warning("auto-control skipped for %s: %s", name, issue)
        self.store.log_event("actuator_unavailable", actuator=name, reason=issue)

    def _clear_actuator_issue(self, name: str) -> None:
        if self._actuator_issues.pop(name, None) is not None:
            log.info("auto-control resumed for %s", name)
            self.store.log_event("actuator_available", actuator=name)

    # --- completion tracking --------------------------------------------

    def _track(self, action: PendingAction) -> None:
        """진행 중인 동작 등록. 같은 기기의 이전 동작은 새 동작으로 대체된다."""
        with self._actuator_lock:
            old = self._pending.pop(action.actuator, None)
            if old is not None and old.command_id is not None:
                # 원격 명령은 결과를 꼭 알려줘야 한다. 자동제어끼리는 조용히 대체.
                self._send_ack(protocol.ack(old.command_id, False, "superseded by a newer command"))
            self._pending[action.actuator] = action

    def _check_pending(self) -> None:
        """set() 을 보낸 동작이 목표 상태에 도달했는지 확인한다.

        서보/모터는 비동기라 set() 이 돌아온 시점에는 아직 움직이는 중이다.
        """
        now = time.time()
        with self._actuator_lock:
            for action in list(self._pending.values()):
                act = self.actuators.get(action.actuator)
                if act is None:
                    self._finish(action, False, "device disconnected")
                    continue
                current, issue = self._inspect_actuator(act)
                moving = bool(getattr(act, "is_moving", False))
                if issue is not None and issue.startswith("fault"):
                    self._finish(action, False, issue)
                elif current == action.on and not moving:
                    state = protocol.device_state(action.actuator, action.on)
                    self._finish(action, True, f"{action.actuator} reached {state}")
                elif now > action.deadline:
                    self._finish(action, False, "target state not confirmed before deadline")

    def _finish(self, action: PendingAction, ok: bool, reason: str) -> None:
        with self._actuator_lock:
            if self._pending.get(action.actuator) is action:
                del self._pending[action.actuator]
        now = time.time()
        if ok:
            log.info("%s -> %s done (%s)", action.actuator, action.on, reason)
        else:
            log.warning("%s -> %s failed: %s", action.actuator, action.on, reason)
            self.store.log_event(
                "actuator_error",
                actuator=action.actuator,
                on=action.on,
                error=reason,
                commandId=action.command_id,
            )
        if action.command_id is not None:
            self._send_ack(protocol.ack(action.command_id, ok, reason, now))
        elif ok:
            # 자동제어로 실제 완료한 동작 -> 복구 시 recovery 리포트로
            self.store.add_local_action(now, action.actuator, action.on, action.reason)

    # --- device state ---------------------------------------------------

    def _device_states(self) -> dict[str, str]:
        """기기 id -> OPEN/CLOSED/ON/OFF. 상태를 모르는 기기는 빠진다."""
        states: dict[str, str] = {}
        with self._actuator_lock:
            for name, act in self.actuators.items():
                if name == DOOR and self.door_sensor is not None:
                    continue  # 문은 아래에서 센서값으로
                current, _ = self._inspect_actuator(act)
                state = protocol.device_state(name, current)
                if state is not None and name in protocol.DEVICE_IDS:
                    states[protocol.DEVICE_IDS[name]] = state
        if self.door_sensor is not None and self._door_open is not None:
            states[protocol.DEVICE_IDS[DOOR]] = protocol.device_state(DOOR, self._door_open)
        return states

    def _publish_devices(self) -> None:
        """바뀐 기기 상태를 devices 토픽으로 보낸다 (온라인일 때만, 기기마다 한 메시지)."""
        if self.monitor.mode is not Mode.ONLINE:
            return
        for device_id, state in self._device_states().items():
            if self._last_states.get(device_id) == state:
                continue
            payload = {"id": device_id, "state": state, "updatedAt": protocol.iso()}
            if self.mqtt.publish_json(self.mqtt.topic_devices, payload):
                self._last_states[device_id] = state

    # --- upload ---------------------------------------------------------

    def _flush_backlog(self) -> None:
        """로컬에 쌓인 미전송 측정값/이벤트를 클라우드로 전송."""
        sent: list[int] = []
        for row in self.store.unsynced_readings():
            if row["pm10"] is None:
                # 클라우드는 5개 값이 모두 있어야 받는다. 없는 값을 만들어 보내지 않는다.
                if not self._pm10_warned:
                    log.warning("reading without pm10 is not sent (sensor driver gives no PM10)")
                    self._pm10_warned = True
                sent.append(row["id"])
                continue
            payload = {
                "measuredAt": protocol.iso(row["measured_at"]),
                "temperature": row["temperature"],
                "humidity": row["humidity"],
                "co2": row["co2"],
                "pm25": row["pm25"],
                "pm10": row["pm10"],
            }
            if not self.mqtt.publish_json(self.mqtt.topic_sensors, payload):
                break
            sent.append(row["id"])
        self.store.mark_readings_synced(sent)

        sent = []
        for row in self.store.unsynced_events():
            payload = {
                "occurredAt": protocol.iso(row["occurred_at"]),
                "type": row["type"],
                "detail": json.loads(row["detail"]),  # 문자열이 아니라 JSON 객체로 전송
            }
            if not self.mqtt.publish_json(self.mqtt.topic_events, payload):
                break
            sent.append(row["id"])
        self.store.mark_events_synced(sent)

    # --- remote control -------------------------------------------------

    def _handle_command(self, msg: dict[str, Any]) -> None:
        """클라우드 원격 명령 (MQTT 네트워크 스레드에서 호출).

        {"commandId": "uuid", "deviceId": "window-1", "action": "OPEN",
         "reason": "...", "issuedAt": "...", "expiresAt": "..."}

        - 같은 commandId 는 한 번만 실행 (SQLite 에 기록, 재시작 후에도 유지). 다시 오면 보낸 ack 를 재전송.
        - expiresAt 이 지났거나 잘못된 명령은 실행하지 않고 FAILED ack.
        - 실행하면 목표 상태 도달을 확인한 뒤 EXECUTED ack (_check_pending).
        """
        command_id = msg.get("commandId")
        if not isinstance(command_id, str) or not command_id or len(command_id) > 100:
            log.warning("ignored command without valid commandId: %s", msg)
            return

        if not self.store.save_command(command_id, msg):
            self._handle_duplicate(command_id)
            return

        try:
            cmd = protocol.parse_command(msg)
        except protocol.CommandError as e:
            log.warning("rejected command %s: %s", command_id, e)
            self._send_ack(protocol.ack(command_id, False, str(e)))
            return

        act = self.actuators.get(cmd.actuator)
        if act is None:
            self._send_ack(protocol.ack(command_id, False, f"{cmd.actuator} is not connected"))
            return

        try:
            with self._actuator_lock:
                act.set(cmd.on)
                self._track(
                    PendingAction(
                        cmd.actuator, cmd.on, cmd.expires_at, "remote command", command_id
                    )
                )
        except Exception as e:
            log.error("remote command: %s -> %s failed: %s", cmd.actuator, cmd.on, e)
            self.store.log_event(
                "actuator_error", actuator=cmd.actuator, on=cmd.on, error=str(e), commandId=command_id
            )
            self._send_ack(protocol.ack(command_id, False, str(e)))
            return

        log.info("remote command %s: %s -> %s", command_id, cmd.actuator, cmd.on)
        self.store.log_event(
            "remote_command", actuator=cmd.actuator, on=cmd.on, commandId=command_id
        )

    def _handle_duplicate(self, command_id: str) -> None:
        row = self.store.get_command(command_id)
        if row is not None and row["ack"] is not None:
            log.info("duplicate command %s, re-sending ack", command_id)
            self.mqtt.publish_json(self.mqtt.topic_ack, json.loads(row["ack"]))
            return
        with self._actuator_lock:
            running = any(a.command_id == command_id for a in self._pending.values())
        if running:
            log.info("duplicate command %s ignored (still running)", command_id)
            return
        # 실행 중에 에이전트가 재시작된 명령. 다시 실행하지 않는다.
        self._send_ack(protocol.ack(command_id, False, "interrupted by edge restart; check device state"))

    def _send_ack(self, ack: dict[str, Any]) -> None:
        self.store.finish_command(ack["commandId"], ack)
        # 명령은 온라인일 때만 오므로 ack 는 로컬에 쌓지 않고 바로 보낸다.
        # 못 보내면 클라우드는 타임아웃으로 처리하고, 같은 명령이 다시 오면 저장된 ack 를 재전송한다.
        if not self.mqtt.publish_json(self.mqtt.topic_ack, ack):
            log.warning("failed to send ack for command %s", ack["commandId"])

    # --- config ---------------------------------------------------------

    def _load_saved_thresholds(self) -> None:
        """이전에 클라우드에서 받은 기준값이 있으면 config.toml 보다 우선 적용."""
        saved = self.store.get_setting("thresholds")
        if not saved:
            return
        try:
            self.cfg.thresholds = Thresholds(**saved["values"])
        except (KeyError, TypeError) as e:
            log.warning("ignored saved thresholds: %s", e)
            return
        self._config_version = saved.get("version")
        log.info("loaded thresholds from cloud config (version %s)", self._config_version)

    def _handle_config(self, msg: dict[str, Any]) -> None:
        """클라우드 설정. 예: {"version": 3, "thresholds": {"co2High": 900, "humidityHigh": 65}}

        version 이 지금보다 낮으면 무시한다 (늦게 도착한 옛 메시지).
        retained 메시지라 재연결마다 같은 내용이 다시 오는데, 바뀐 게 없으면 조용히 넘어간다.
        """
        version = msg.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            version = None
        if (
            version is not None
            and self._config_version is not None
            and version < self._config_version
        ):
            log.info("ignored stale config version %s (< %s)", version, self._config_version)
            return

        try:
            thresholds = apply_threshold_update(self.cfg.thresholds, msg.get("thresholds"))
        except ValueError as e:
            log.warning("rejected config version %s: %s", version, e)
            self.store.log_event("config_rejected", version=version, error=str(e))
            return

        if thresholds == self.cfg.thresholds and version == self._config_version:
            return
        # 참조를 통째로 바꾸므로 메인 루프는 옛 값 또는 새 값 중 하나만 본다
        self.cfg.thresholds = thresholds
        self._config_version = version
        self.store.set_setting("thresholds", {"version": version, "values": asdict(thresholds)})
        log.info("applied config version %s: %s", version, thresholds)
        self.store.log_event("config_applied", version=version, thresholds=asdict(thresholds))


def build_agent(cfg: AgentConfig) -> EdgeAgent:
    if cfg.use_mock_devices:
        sensor: SensorReader = MockSensorReader()
        door = MockDoorSensor()
        actuators: dict[str, Actuator] = {
            WINDOW: MockActuator(WINDOW),
            AIR_PURIFIER: MockActuator(AIR_PURIFIER),
            DEHUMIDIFIER: MockActuator(DEHUMIDIFIER),
            DOOR: MockDoorCloser(door),
        }
    else:
        # TODO: hardware 레포의 실제 드라이버 연결
        #   RoomSensorReader, WindowActuator, ServoButtonPurifier,
        #   TouchDoorSensor + DoorCloserServo (DoorAutoCloser 는 쓰지 않음: 클라우드가 담당)
        #   ServoButtonDehumidifier (bundle 의 dehumidifier=True, 없으면 습도는 창문 환기로 처리)
        raise NotImplementedError("real hardware drivers are not wired yet")

    return EdgeAgent(cfg, sensor, actuators, LocalStore(cfg.db_path), door_sensor=door)
