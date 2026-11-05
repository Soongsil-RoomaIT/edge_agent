"""Cloud Server 와의 MQTT 메시지 계약 (roomait-v1).

cloud-server 레포 docs/MQTT.md 의 "완료 ACK를 지원하는 확장 계약" 을 따른다.
- 시각은 UTC ISO 8601 문자열 (예: 2026-11-05T03:00:00.123Z)
- 기기 id 는 프론트/클라우드와 같은 window-1, purifier-1, dehumidifier-1, door-1
- 상태는 창문/문 OPEN/CLOSED, 가전 ON/OFF

엣지 내부에서는 계속 액추에이터 이름(window 등)과 on(bool)을 쓰고, 여기서만 변환한다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .devices import AIR_PURIFIER, DEHUMIDIFIER, DOOR, WINDOW

# 엣지 액추에이터 이름 <-> 클라우드 기기 id
DEVICE_IDS = {
    WINDOW: "window-1",
    AIR_PURIFIER: "purifier-1",
    DEHUMIDIFIER: "dehumidifier-1",
    DOOR: "door-1",
}
ACTUATORS = {device_id: name for name, device_id in DEVICE_IDS.items()}

# 창문/문은 OPEN/CLOSED, 가전은 ON/OFF
_OPENABLE = {WINDOW, DOOR}
_ACTIONS = {
    WINDOW: {"OPEN": True, "CLOSE": False},
    DOOR: {"CLOSE": False},  # 서보로 닫기만 가능
    AIR_PURIFIER: {"ON": True, "OFF": False},
    DEHUMIDIFIER: {"ON": True, "OFF": False},
}


def iso(ts: float | None = None) -> str:
    """Unix 시각(초) -> UTC ISO 문자열 (밀리초, Z)."""
    if ts is None:
        ts = time.time()
    dt = datetime.fromtimestamp(ts, timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: Any) -> float:
    """ISO 문자열 -> Unix 시각(초). 시간대가 없으면 거부한다 (파이 시간대와 헷갈리지 않게)."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO 8601 string")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"invalid timestamp: {value}") from e
    if dt.tzinfo is None:
        raise ValueError(f"timestamp needs a timezone: {value}")
    return dt.timestamp()


def device_state(actuator: str, on: bool | None) -> str | None:
    """on -> OPEN/CLOSED/ON/OFF. 모르는 상태(None)는 None (계약에 unknown 값이 없어서 보내지 않음)."""
    if on is None:
        return None
    if actuator in _OPENABLE:
        return "OPEN" if on else "CLOSED"
    return "ON" if on else "OFF"


class CommandError(ValueError):
    """실행하면 안 되는 명령. 사유는 FAILED ack 의 reason 으로 보낸다."""


@dataclass(frozen=True)
class Command:
    command_id: str
    actuator: str
    on: bool
    expires_at: float  # unix


def parse_command(msg: dict[str, Any], now: float | None = None) -> Command:
    """commands 메시지를 검증한다.

    {"commandId", "deviceId", "action", "reason", "issuedAt", "expiresAt"}
    commandId 는 호출 쪽에서 먼저 확인한다 (없으면 ack 를 보낼 수 없음).
    """
    now = time.time() if now is None else now
    command_id = msg["commandId"]

    device_id = msg.get("deviceId")
    actuator = ACTUATORS.get(device_id) if isinstance(device_id, str) else None
    if actuator is None:
        raise CommandError(f"unknown device: {device_id}")

    action = msg.get("action")
    allowed = _ACTIONS[actuator]
    if action not in allowed:
        if actuator == DOOR and action == "OPEN":
            raise CommandError("door can only be closed")
        raise CommandError(f"invalid action for {device_id}: {action}")

    try:
        expires_at = parse_iso(msg.get("expiresAt"))
    except ValueError as e:
        raise CommandError(f"expiresAt: {e}") from e
    if expires_at <= now:
        raise CommandError("command expired before execution")

    return Command(command_id, actuator, allowed[action], expires_at)


def ack(command_id: str, executed: bool, reason: str, at: float | None = None) -> dict[str, Any]:
    return {
        "commandId": command_id,
        "status": "EXECUTED" if executed else "FAILED",
        "reason": reason[:500],
        "executedAt": iso(at),
    }


def status(offline_mode: bool, online: bool = True) -> dict[str, Any]:
    return {"online": online, "offlineMode": offline_mode}
