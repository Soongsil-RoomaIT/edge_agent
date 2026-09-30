# edge_agent

자취방 원격 케어 시스템의 **Edge Agent** (Raspberry Pi, Python).

- 센서(온습도, CO2, 미세먼지) 측정 → SQLite 로컬 저장 → MQTT로 클라우드 전송
- 클라우드 원격 조작 명령 수신 → 액추에이터(창문, 공기청정기) 구동
- **통신 단절 시 10초 이내 오프라인 자동제어 모드 전환** (NFR03), 복구 시 밀린 데이터 전송 + 복구 이벤트 발행

## 구조

```
src/edge_agent/
├── __main__.py      # 진입점 (python -m edge_agent)
├── agent.py         # 메인 루프: 측정 / 저장 / 전송 / 모드 전환
├── connectivity.py  # 온라인/오프라인 모드 판단
├── auto_control.py  # 오프라인 자동제어 규칙
├── storage.py       # SQLite (미전송 데이터 버퍼)
├── mqtt_client.py   # MQTT 클라이언트 (paho-mqtt)
├── devices.py       # 센서/액추에이터 인터페이스 + Mock
└── config.py        # 설정 로딩
```

실제 센서/액추에이터 드라이버는 [`hardware`](https://github.com/Soongsil-RoomaIT/hardware) 레포에서 구현하고,
`devices.py` 의 `SensorReader` / `Actuator` 인터페이스에 맞춰 연결한다. 그 전까지는 Mock 으로 동작한다.

## MQTT 토픽 (백엔드와 합의 필요)

| 토픽 | 방향 | 내용 |
|---|---|---|
| `roomcare/{deviceId}/telemetry` | 엣지 → 클라우드 | `{measuredAt, temperature, humidity, co2, pm25}` |
| `roomcare/{deviceId}/events` | 엣지 → 클라우드 | `{occurredAt, type, detail}` — `mode_changed`, `reconnected`, `auto_control`, `remote_command` |
| `roomcare/{deviceId}/status` | 엣지 → 클라우드 | `online` / `offline` (retained, LWT) |
| `roomcare/{deviceId}/command` | 클라우드 → 엣지 | `{"actuator": "window", "on": true}` |

## 오프라인 전환 시간 (NFR03: 10초 이내)

| 단계 | 기본값 |
|---|---|
| MQTT 끊김 감지 (약 1.5 × keepalive) | ≤ 6s (`keepalive = 4`) |
| 유예 시간 (순간 끊김 무시) | 2s (`offline_grace`) |
| 상태 확인 주기 | 0.5s |
| **합계** | **≤ 8.5s** |

## 실행

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (라즈베리파이: source .venv/bin/activate)
pip install -e ".[dev]"
copy config\config.example.toml config\config.toml
python -m edge_agent
```

로컬 테스트용 브로커가 필요하면 Mosquitto 를 띄운다 (`docker run -p 1883:1883 eclipse-mosquitto:2`, 익명 접속 허용 설정 필요).

## 테스트

```bash
pytest
```
