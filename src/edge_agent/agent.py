"""Edge Agent 메인 루프.

    센서 측정 -> SQLite 저장 -> (온라인) 클라우드 전송 / (오프라인) 로컬 자동제어

- 측정값은 항상 먼저 로컬에 저장하고, 전송 성공분만 synced 로 표시한다.
- 연결 상태는 측정 주기와 별개로 TICK 마다 확인해서 10초 이내 전환(NFR03)을 보장한다.
- 재연결되면 밀린 데이터를 전송하고 "reconnected" 이벤트를 보낸다 (클라우드가 FCM 알림 발송).
- 문 열림/닫힘이 바뀌면 바로 이벤트를 보낸다 (클라우드의 현관문 상태 기반 자동화에 사용).
  문 자동 닫기(서보)는 hardware 쪽 DoorAutoCloser 가 모드와 상관없이 로컬에서 처리한다.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import auto_control
from .config import AgentConfig
from .connectivity import ConnectivityMonitor, Mode
from .devices import (
    AIR_PURIFIER,
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

        # paho 는 실제 라즈베리파이/개발 PC 에서만 필요하므로 여기서 import
        from .mqtt_client import EdgeMqttClient

        self.mqtt = EdgeMqttClient(
            cfg.mqtt,
            cfg.device_id,
            on_connected=self.monitor.mark_connected,
            on_disconnected=self.monitor.mark_disconnected,
            on_command=self._handle_command,
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
        for d in auto_control.decide(reading, self.cfg.thresholds):
            act = self.actuators.get(d.actuator)
            if act is None:
                continue
            try:
                with self._actuator_lock:
                    if act.is_on == d.on:
                        continue
                    act.set(d.on)
            except Exception as e:
                log.error("auto-control: %s -> %s failed: %s", d.actuator, d.on, e)
                self.store.log_event("actuator_error", actuator=d.actuator, on=d.on, error=str(e))
                continue
            log.info("auto-control: %s -> %s (%s)", d.actuator, d.on, d.reason)
            self.store.log_event("auto_control", actuator=d.actuator, on=d.on, reason=d.reason)

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
            }
            if not self.mqtt.publish_json(self.mqtt.topic_telemetry, payload):
                break
            sent.append(row["id"])
        self.store.mark_readings_synced(sent)

        sent = []
        for row in self.store.unsynced_events():
            payload = {"occurredAt": row["occurred_at"], "type": row["type"], "detail": row["detail"]}
            if not self.mqtt.publish_json(self.mqtt.topic_events, payload):
                break
            sent.append(row["id"])
        self.store.mark_events_synced(sent)

    # --- remote control -------------------------------------------------

    def _handle_command(self, cmd: dict[str, Any]) -> None:
        """클라우드 원격 조작. 예: {"actuator": "window", "on": true}"""
        name, on = cmd.get("actuator"), cmd.get("on")
        act = self.actuators.get(name)
        if act is None or not isinstance(on, bool):
            log.warning("ignored invalid command: %s", cmd)
            return
        try:
            with self._actuator_lock:
                act.set(on)
        except Exception as e:
            log.error("remote command: %s -> %s failed: %s", name, on, e)
            self.store.log_event("actuator_error", actuator=name, on=on, error=str(e))
            return
        log.info("remote command: %s -> %s", name, on)
        self.store.log_event("remote_command", actuator=name, on=on)


def build_agent(cfg: AgentConfig) -> EdgeAgent:
    if cfg.use_mock_devices:
        sensor: SensorReader = MockSensorReader()
        door = MockDoorSensor()
        actuators: dict[str, Actuator] = {
            WINDOW: MockActuator(WINDOW),
            AIR_PURIFIER: MockActuator(AIR_PURIFIER),
            DOOR: MockDoorCloser(door),
        }
    else:
        # TODO: hardware 레포의 실제 드라이버 연결
        #   RoomSensorReader, WindowActuator, ServoButtonPurifier,
        #   TouchDoorSensor + DoorCloserServo + DoorAutoCloser
        raise NotImplementedError("real hardware drivers are not wired yet")

    return EdgeAgent(cfg, sensor, actuators, LocalStore(cfg.db_path), door_sensor=door)
