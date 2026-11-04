"""Edge Agent 메인 루프.

    센서 측정 -> SQLite 저장 -> (온라인) 클라우드 전송 / (오프라인) 로컬 자동제어

- 측정값은 항상 먼저 로컬에 저장하고, 전송 성공분만 synced 로 표시한다.
- 연결 상태는 측정 주기와 별개로 TICK 마다 확인해서 10초 이내 전환(NFR03)을 보장한다.
- 재연결되면 밀린 데이터를 전송하고 "reconnected" 이벤트를 보낸다 (클라우드가 FCM 알림 발송).
- 문 열림/닫힘이 바뀌면 바로 이벤트를 보낸다 (클라우드의 현관문 상태 기반 자동화에 사용).
  문 자동 닫기(서보)는 hardware 쪽 DoorAutoCloser 가 모드와 상관없이 로컬에서 처리한다.
- 원격 조작 명령마다 ack 를 보내고(commandId 로 중복 실행 방지), 기기 상태가 바뀌면
  state 메시지(retained)를 보낸다.
- 클라우드가 보낸 기준값(config)은 안전 범위를 검사한 뒤 적용하고 로컬에 저장한다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from typing import Any

from . import auto_control
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
RECENT_ACKS = 100  # 중복 명령 판별용으로 기억할 commandId 개수


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
        self._actuator_lock = threading.Lock()
        self._stop = threading.Event()
        self._offline_since: float | None = None
        self._sensor_failures = 0
        # 자동제어를 건너뛴 액추에이터와 그 사유. 같은 사유는 이벤트를 한 번만 남긴다.
        self._actuator_issues: dict[str, str] = {}
        # commandId -> 보낸 ack. QoS 1 은 같은 명령이 두 번 올 수 있어서 다시 실행하지 않는다.
        self._recent_acks: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._last_state: dict[str, Any] | None = None  # 마지막으로 보낸 기기 상태
        self._config_version: int | None = None
        self._load_saved_thresholds()

        # paho 는 실제 라즈베리파이/개발 PC 에서만 필요하므로 여기서 import
        from .mqtt_client import EdgeMqttClient

        self.mqtt = EdgeMqttClient(
            cfg.mqtt,
            cfg.device_id,
            on_connected=self.monitor.mark_connected,
            on_disconnected=self.monitor.mark_disconnected,
            on_command=self._handle_command,
            on_config=self._handle_config,
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
                self._check_mode()
                self._check_door()
                self._publish_state()

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

    # --- steps ----------------------------------------------------------

    def _check_mode(self) -> None:
        changed = self.monitor.update()
        if changed is Mode.OFFLINE:
            self._offline_since = time.time()
            log.warning("cloud unreachable -> OFFLINE auto-control mode")
            self.store.log_event("mode_changed", mode="offline")
        elif changed is Mode.ONLINE:
            offline_seconds = (
                round(time.time() - self._offline_since) if self._offline_since else None
            )
            self._offline_since = None
            self._last_state = None  # 오프라인 동안 바뀐 상태를 다시 보낸다
            log.info("cloud reachable -> ONLINE mode")
            self.store.log_event("mode_changed", mode="online")
            self.store.log_event("reconnected", offline_seconds=offline_seconds)
            self._flush_backlog()

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
                    # current 가 None 이면 창문이 중간/미확인 위치 -> 목표로 다시 보낸다 (set 은 멱등)
                    act.set(d.on)
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

    def _flush_backlog(self) -> None:
        """로컬에 쌓인 미전송 측정값/이벤트를 클라우드로 전송."""
        sent: list[int] = []
        for row in self.store.unsynced_readings():
            payload = {
                "measuredAt": row["measured_at"],
                "temperature": row["temperature"],
                "humidity": row["humidity"],
                "co2": row["co2"],
                "pm25": row["pm25"],
                "pm10": row["pm10"],  # 이전 버전 DB 에 남은 행은 null
            }
            if not self.mqtt.publish_json(self.mqtt.topic_telemetry, payload):
                break
            sent.append(row["id"])
        self.store.mark_readings_synced(sent)

        sent = []
        for row in self.store.unsynced_events():
            payload = {
                "occurredAt": row["occurred_at"],
                "type": row["type"],
                "detail": json.loads(row["detail"]),  # 문자열이 아니라 JSON 객체로 전송
            }
            if not self.mqtt.publish_json(self.mqtt.topic_events, payload):
                break
            sent.append(row["id"])
        self.store.mark_events_synced(sent)

    # --- device state ---------------------------------------------------

    def _device_states(self) -> dict[str, dict[str, Any]]:
        """기기별 상태. on=None 은 상태를 모름.

        source: SENSOR  = 센서로 확인한 상태 (창문 리밋 스위치, 문 터치 센서)
                ASSUMED = 마지막 명령으로 추정한 상태 (버튼 누르는 서보, Mock)
        """
        states: dict[str, dict[str, Any]] = {}
        with self._actuator_lock:
            for name, act in self.actuators.items():
                if name == DOOR and self.door_sensor is not None:
                    continue  # 문은 아래에서 센서값으로
                states[name] = self._describe_actuator(act)
        if self.door_sensor is not None:
            states[DOOR] = {"on": self._door_open, "source": "SENSOR", "fault": None}
        return states

    @staticmethod
    def _describe_actuator(act: Actuator) -> dict[str, Any]:
        info: dict[str, Any] = {}
        position = getattr(act, "position", None)
        if position is not None:
            info["on"] = {"open": True, "closed": False}.get(position)
            info["position"] = position
            source = "SENSOR"
        else:
            if getattr(act, "state", None) == "unknown":
                info["on"] = None
            else:
                try:
                    info["on"] = bool(act.is_on)
                except Exception:
                    info["on"] = None
            source = "ASSUMED"
        info["source"] = getattr(act, "state_source", source)
        info["fault"] = getattr(act, "fault", None) or None
        return info

    def _publish_state(self) -> None:
        """기기 상태가 바뀌었으면 state 토픽으로 보낸다 (온라인일 때만)."""
        if self.monitor.mode is not Mode.ONLINE:
            return
        states = self._device_states()
        if states == self._last_state:
            return
        payload = {"reportedAt": time.time(), "devices": states}
        if self.mqtt.publish_json(self.mqtt.topic_state, payload, retain=True):
            self._last_state = states

    # --- remote control -------------------------------------------------

    def _handle_command(self, cmd: dict[str, Any]) -> None:
        """클라우드 원격 조작. 예: {"commandId": "c-123", "actuator": "window", "on": true}

        처리 결과를 ack 토픽으로 보낸다. status:
            accepted  액추에이터에 명령을 보냄 (서보는 비동기라 실제 완료는 state 메시지로 확인)
            rejected  잘못된 명령 (알 수 없는 기기, on 이 bool 이 아님, 문 열기)
            failed    액추에이터 오류
        """
        command_id = cmd.get("commandId")
        if command_id is not None and command_id in self._recent_acks:
            log.info("duplicate command %s ignored, re-sending ack", command_id)
            self._publish_ack(self._recent_acks[command_id])
            return

        ack = self._execute_command(cmd)
        ack["ackedAt"] = time.time()
        if command_id is not None:
            self._recent_acks[command_id] = ack
            while len(self._recent_acks) > RECENT_ACKS:
                self._recent_acks.popitem(last=False)
        self._publish_ack(ack)

    def _execute_command(self, cmd: dict[str, Any]) -> dict[str, Any]:
        name, on = cmd.get("actuator"), cmd.get("on")
        ack: dict[str, Any] = {"commandId": cmd.get("commandId"), "actuator": name, "on": on}

        act = self.actuators.get(name)
        error = None
        if act is None:
            error = f"unknown actuator: {name}"
        elif not isinstance(on, bool):
            error = "'on' must be true or false"
        elif name == DOOR and on:
            error = "door can only be closed"
        if error is not None:
            log.warning("rejected command %s: %s", cmd, error)
            return {**ack, "status": "rejected", "error": error}

        try:
            with self._actuator_lock:
                act.set(on)
        except Exception as e:
            log.error("remote command: %s -> %s failed: %s", name, on, e)
            self.store.log_event(
                "actuator_error", actuator=name, on=on, error=str(e), commandId=ack["commandId"]
            )
            return {**ack, "status": "failed", "error": str(e)}

        log.info("remote command: %s -> %s", name, on)
        self.store.log_event("remote_command", actuator=name, on=on, commandId=ack["commandId"])
        return {**ack, "status": "accepted"}

    def _publish_ack(self, ack: dict[str, Any]) -> None:
        # 명령은 온라인일 때만 오므로 ack 는 로컬에 쌓지 않고 바로 보낸다
        if not self.mqtt.publish_json(self.mqtt.topic_ack, ack):
            log.warning("failed to send ack for command %s", ack.get("commandId"))

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
        #   TouchDoorSensor + DoorCloserServo + DoorAutoCloser
        #   ServoButtonDehumidifier (bundle 의 dehumidifier=True, 없으면 습도는 창문 환기로 처리)
        raise NotImplementedError("real hardware drivers are not wired yet")

    return EdgeAgent(cfg, sensor, actuators, LocalStore(cfg.db_path), door_sensor=door)
