"""MQTT 클라이언트 래퍼 (paho-mqtt 2.x).

토픽 구조 (roomait-v1, cloud-server docs/MQTT.md). base = {prefix}/{device_id}
    {base}/sensors        엣지 -> 클라우드  센서 측정값
    {base}/devices        엣지 -> 클라우드  기기 상태 (기기마다, 바뀔 때 + 연결 시)
    {base}/status         엣지 -> 클라우드  {online, offlineMode} heartbeat. LWT 는 retained
    {base}/recovery       엣지 -> 클라우드  복구 시 오프라인 동안 실제 완료한 동작
    {base}/ack            엣지 -> 클라우드  원격 명령 완료/실패
    {base}/events         엣지 -> 클라우드  진단용 이벤트 (센서 오류, 문 열림 등)
    {base}/commands       클라우드 -> 엣지  원격 명령
    {base}/config         클라우드 -> 엣지  오프라인 자동제어 기준값 (retained 권장)
    {base}/cloud-status   클라우드 -> 엣지  클라우드 서버 heartbeat (선택, cloud_heartbeat_timeout)
"""

from __future__ import annotations

import json
import logging
import ssl
from typing import Any, Callable

import paho.mqtt.client as mqtt

from . import protocol
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
        on_config: Callable[[dict[str, Any]], None] | None = None,
        on_cloud_status: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._cfg = cfg
        base = f"{cfg.topic_prefix}/{device_id}"
        self.topic_sensors = f"{base}/sensors"
        self.topic_devices = f"{base}/devices"
        self.topic_status = f"{base}/status"
        self.topic_recovery = f"{base}/recovery"
        self.topic_ack = f"{base}/ack"
        self.topic_events = f"{base}/events"
        self.topic_commands = f"{base}/commands"
        self.topic_config = f"{base}/config"
        self.topic_cloud_status = f"{base}/cloud-status"

        self._on_connected = on_connected
        self._on_disconnected = on_disconnected
        self._handlers: dict[str, Callable[[dict[str, Any]], None] | None] = {
            self.topic_commands: on_command,
            self.topic_config: on_config,
            self.topic_cloud_status: on_cloud_status,
        }

        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"edge-{device_id}")
        if cfg.username:
            c.username_pw_set(cfg.username, cfg.password)
        if cfg.tls:
            c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        # 비정상 종료 시 브로커가 대신 offline 을 알려준다.
        c.will_set(self.topic_status, self._offline_payload(), qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=5)
        c.on_connect = self._handle_connect
        c.on_disconnect = self._handle_disconnect
        c.on_message = self._handle_message
        self._client = c

    @staticmethod
    def _offline_payload() -> str:
        return json.dumps(protocol.status(offline_mode=True, online=False))

    @property
    def connected(self) -> bool:
        return self._client.is_connected()

    def start(self) -> None:
        # connect_async + loop_start: 브로커가 꺼져 있어도 바로 반환하고 백그라운드에서 재시도
        self._client.connect_async(self._cfg.host, self._cfg.port, keepalive=self._cfg.keepalive)
        self._client.loop_start()

    def stop(self) -> None:
        if self._client.is_connected():
            self._client.publish(
                self.topic_status, self._offline_payload(), qos=1, retain=True
            ).wait_for_publish(2)
        self._client.disconnect()
        self._client.loop_stop()

    def publish_json(
        self, topic: str, payload: dict[str, Any], qos: int = 1, retain: bool = False
    ) -> bool:
        """연결되어 있고 큐잉에 성공하면 True. 실패하면 호출 쪽에서 로컬에 남겨둔다."""
        if not self._client.is_connected():
            return False
        info = self._client.publish(
            topic, json.dumps(payload, ensure_ascii=False), qos=qos, retain=retain
        )
        return info.rc == mqtt.MQTT_ERR_SUCCESS

    # --- paho callbacks (네트워크 스레드에서 호출됨) -------------------------

    def _handle_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning("MQTT connect failed: %s", reason_code)
            return
        log.info("MQTT connected")
        client.subscribe([(topic, 1) for topic, h in self._handlers.items() if h is not None])
        self._on_connected()

    def _handle_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        log.warning("MQTT disconnected: %s", reason_code)
        self._on_disconnected()

    def _handle_message(self, client, userdata, msg) -> None:
        handler = self._handlers.get(msg.topic)
        if handler is None:
            return
        try:
            payload = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            log.warning("invalid payload on %s: %r", msg.topic, msg.payload)
            return
        if not isinstance(payload, dict):
            log.warning("payload on %s is not an object: %r", msg.topic, payload)
            return
        try:
            handler(payload)
        except Exception:  # 콜백 예외로 네트워크 스레드가 죽지 않게
            log.exception("handler for %s failed", msg.topic)
