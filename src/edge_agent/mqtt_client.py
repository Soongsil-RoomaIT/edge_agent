"""MQTT 클라이언트 래퍼 (paho-mqtt 2.x).

토픽 구조 (백엔드와 합의 필요):
    {prefix}/{device_id}/telemetry  엣지 -> 클라우드  센서 측정값
    {prefix}/{device_id}/events     엣지 -> 클라우드  모드 전환, 자동제어 실행 등
    {prefix}/{device_id}/status     엣지 -> 클라우드  "online"/"offline" (retained, LWT)
    {prefix}/{device_id}/command    클라우드 -> 엣지  액추에이터 원격 조작
"""

from __future__ import annotations

import json
import logging
import ssl
from typing import Any, Callable

import paho.mqtt.client as mqtt

from .config import MqttConfig

log = logging.getLogger(__name__)


class EdgeMqttClient:
    def __init__(
        self,
        cfg: MqttConfig,
        device_id: str,
        on_connected: Callable[[], None],
        on_disconnected: Callable[[], None],
        on_command: Callable[[dict[str, Any]], None],
    ) -> None:
        self._cfg = cfg
        base = f"{cfg.topic_prefix}/{device_id}"
        self.topic_telemetry = f"{base}/telemetry"
        self.topic_events = f"{base}/events"
        self.topic_status = f"{base}/status"
        self.topic_command = f"{base}/command"

        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._on_command = on_command

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"edge-{device_id}")
        if cfg.username:
            c.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        # 비정상 종료 시 브로커가 대신 offline 을 알려준다.
        c.will_set(self.topic_status, "offline", qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=5)
        c.on_connect = self._handle_connect
        c.on_disconnect = self._handle_disconnect
        c.on_message = self._handle_message
        self._client = c

    def start(self) -> None:
        # connect_async + loop_start: 브로커가 꺼져 있어도 바로 반환하고 백그라운드에서 재시도
        self._client.connect_async(self._cfg.host, self._cfg.port, keepalive=self._cfg.keepalive)
        self._client.loop_start()

    def stop(self) -> None:
        if self._client.is_connected():
            self._client.publish(self.topic_status, "offline", qos=1, retain=True).wait_for_publish(2)
        self._client.disconnect()
        self._client.loop_stop()

    def publish_json(self, topic: str, payload: dict[str, Any], qos: int = 1) -> bool:
        """연결되어 있고 큐잉에 성공하면 True. 실패하면 호출 쪽에서 로컬에 남겨둔다."""
        if not self._client.is_connected():
            return False
        info = self._client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=qos)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # --- paho callbacks (네트워크 스레드에서 호출됨) -------------------------

    def _handle_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected")
        client.subscribe(self.topic_command, qos=1)
        client.publish(self.topic_status, "online", qos=1, retain=True)
        self._on_connected()

    def _handle_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        log.warning("MQTT disconnected: %s", reason_code)
        self._on_disconnected()

    def _handle_message(self, client, userdata, msg) -> None:
        try:
            payload = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            log.warning("invalid command payload: %r", msg.payload)
            return
        self._on_command(payload)
