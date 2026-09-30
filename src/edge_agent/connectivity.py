"""클라우드 연결 상태 추적 및 온라인/오프라인 모드 전환 판단 (NFR03)."""

from __future__ import annotations

import enum
import threading
from typing import Callable


class Mode(enum.Enum):
    ONLINE = "online"  # 클라우드가 제어 결정
    OFFLINE = "offline"  # 엣지 단독 자동제어


class ConnectivityMonitor:
    """MQTT 연결/끊김 이벤트를 받아 모드 전환 시점을 판단한다.

    - 끊긴 뒤 offline_grace 초가 지나면 OFFLINE 으로 전환 (잠깐 끊겼다 붙는 경우 무시)
    - 다시 연결되면 즉시 ONLINE 으로 전환
    MQTT 콜백 스레드와 메인 루프 스레드에서 동시에 호출되므로 락으로 보호한다.
    """

    def __init__(self, offline_grace: float, clock: Callable[[], float]) -> None:
        self._grace = offline_grace
        self._clock = clock
        self._lock = threading.Lock()
        self._connected = False
        # 시작 시점부터 연결이 안 되어 있으면 그때부터 끊긴 것으로 본다.
        self._disconnected_since: float | None = clock()
        self._mode = Mode.OFFLINE

    @property
    def mode(self) -> Mode:
        return self._mode

    def mark_connected(self) -> None:
        with self._lock:
            self._connected = True
            self._disconnected_since = None

    def mark_disconnected(self) -> None:
        with self._lock:
            if self._connected or self._disconnected_since is None:
                self._disconnected_since = self._clock()
            self._connected = False

    def update(self) -> Mode | None:
        """현재 상태를 평가하고, 모드가 바뀌었으면 새 모드를, 아니면 None 을 돌려준다."""
        with self._lock:
            if self._connected:
                new_mode = Mode.ONLINE
            elif self._clock() - self._disconnected_since >= self._grace:
                new_mode = Mode.OFFLINE
            else:
                new_mode = self._mode

            if new_mode is self._mode:
                return None
            self._mode = new_mode
            return new_mode
