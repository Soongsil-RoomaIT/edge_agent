# edge_agent

자취방 원격 케어 시스템의 **Edge Agent** (Raspberry Pi, Python).

- 센서(온습도, CO2, 미세먼지) 측정 → SQLite 로컬 저장 → MQTT로 클라우드 전송
- 클라우드 원격 조작 명령 수신 → 액추에이터(창문, 공기청정기, 제습기, 문 닫기) 구동, 처리 결과(ack)와 기기 상태 전송
- 클라우드에서 자동제어 기준값(config) 수신 → 안전 범위 확인 후 적용
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
| `roomcare/{deviceId}/telemetry` | 엣지 → 클라우드 | 센서 측정값 (2초마다) |
| `roomcare/{deviceId}/events` | 엣지 → 클라우드 | 모드 전환, 자동제어, 오류 등 ([이벤트 목록](#이벤트-목록)) |
| `roomcare/{deviceId}/state` | 엣지 → 클라우드 | 기기 상태 (retained, 바뀔 때만) |
| `roomcare/{deviceId}/ack` | 엣지 → 클라우드 | 원격 조작 명령 처리 결과 |
| `roomcare/{deviceId}/status` | 엣지 → 클라우드 | `online` / `offline` (retained, LWT) |
| `roomcare/{deviceId}/command` | 클라우드 → 엣지 | 원격 조작 명령 |
| `roomcare/{deviceId}/config` | 클라우드 → 엣지 | 자동제어 기준값 (**retained 로 보내기**) |

- 시간(`measuredAt`, `occurredAt`, `reportedAt`, `ackedAt`)은 Unix 시간(초, 소수점 포함)
- 단위: 온도 °C, 습도 %, CO2 ppm, `pm25`(초미세먼지)·`pm10`(미세먼지) µg/m³
- 기기 이름: `window`, `air_purifier`, `dehumidifier`, `door`
  (프론트의 `window-1`, `purifier-1`, `dehumidifier-1`, `door-1` 로 바꾸는 건 클라우드에서)

### telemetry / events

```json
{"measuredAt": 1759370000.12, "temperature": 24.3, "humidity": 52.1, "co2": 812, "pm25": 14.2, "pm10": 23.5}
{"occurredAt": 1759370003.48, "type": "mode_changed", "detail": {"mode": "offline"}}
```

- `pm10` 은 `null` 일 수 있음 (업데이트 전에 저장된 측정값, 또는 센서 드라이버가 PM10 을 아직 주지 않을 때)
- `detail` 은 JSON 객체

### command → ack

```json
{"commandId": "c-123", "actuator": "window", "on": true}
```

```json
{"commandId": "c-123", "actuator": "window", "on": true, "status": "accepted", "ackedAt": 1759370010.5}
{"commandId": "c-124", "actuator": "door", "on": true, "status": "rejected", "error": "door can only be closed", "ackedAt": 1759370011.2}
```

| status | 의미 |
|---|---|
| `accepted` | 액추에이터에 명령을 보냄. 서보는 비동기라 **실제로 움직였는지는 `state` 로 확인** |
| `rejected` | 잘못된 명령 (모르는 기기, `on` 이 true/false 가 아님, 문 열기) |
| `failed` | 액추에이터 오류 (`error` 에 사유) |

- `commandId` 는 클라우드가 만든다 (REST 의 Idempotency-Key 를 그대로 써도 됨).
  같은 `commandId` 가 다시 오면 (QoS 1 중복) **다시 실행하지 않고** 같은 ack 를 다시 보낸다. 최근 100개 기억.
- `commandId` 가 없어도 동작하지만 중복 실행을 막을 수 없다.
- `door` 는 서보로 **닫기만** 가능 (`"on": false`).
- 명령은 온라인일 때만 오므로 ack 는 로컬에 쌓지 않는다.

### state

```json
{
  "reportedAt": 1759370012.0,
  "devices": {
    "window":       {"on": false, "position": "closed", "source": "SENSOR",  "fault": null},
    "air_purifier": {"on": true,                        "source": "ASSUMED", "fault": null},
    "dehumidifier": {"on": null,                        "source": "ASSUMED", "fault": null},
    "door":         {"on": false,                       "source": "SENSOR",  "fault": null}
  }
}
```

- `on`: 창문/문은 열림, 가전은 켜짐. `null` 은 상태를 모름 (중간 위치, 가전 상태 미확인)
- `source`: `SENSOR` = 센서로 확인 (창문 리밋 스위치, 문 터치 센서), `ASSUMED` = 마지막 명령으로 추정 (버튼 누르는 서보)
  → 프론트의 `stateSource`
- `fault`: 고장 사유 (사람이 점검해야 함)
- 온라인일 때 상태가 바뀌면 보낸다. 재연결하면 현재 상태를 한 번 다시 보낸다. retained 라 클라우드가 늦게 붙어도 마지막 상태를 받는다.

### config

```json
{"version": 3, "thresholds": {"co2High": 900, "humidityHigh": 65}}
```

| 키 | 기본값 | 안전 범위 |
|---|---|---|
| `co2High` | 1000 | 600 ~ 2000 |
| `pm25High` | 35 | 15 ~ 75 |
| `pm10High` | 80 | 30 ~ 150 |
| `humidityHigh` | 70 | 40 ~ 85 |
| `temperatureHigh` | 30 | 24 ~ 35 |

- 바꿀 키만 보내도 된다. 하나라도 모르는 키이거나 범위 밖이면 **전체 거부** (`config_rejected` 이벤트).
- 적용하면 `config_applied` 이벤트를 보내고 SQLite 에 저장한다. 재시작해도 `config.toml` 보다 우선.
- `version` 은 클라우드가 1씩 올린다. 지금보다 낮은 version 은 무시 (늦게 도착한 옛 메시지).
- retained 로 보내면 엣지가 재연결할 때마다 최신 설정을 받는다 (같은 내용이면 조용히 넘어감).
- 사용자 설정과 AI 추천 기준값(사용자 승인 후)을 이 토픽으로 보낸다.

### 이벤트 목록

| type | detail | 발생 시점 |
|---|---|---|
| `agent_started` | | 에이전트 시작 |
| `mode_changed` | `mode` | 온라인 ↔ 오프라인 전환 |
| `reconnected` | `offline_seconds` | 통신 복구 (→ 사용자 알림) |
| `auto_control` | `actuator`, `on`, `reason` | 오프라인 자동제어 실행 |
| `remote_command` | `actuator`, `on`, `commandId` | 원격 조작 실행 |
| `door_opened` / `door_closed` | `initial` | 문 상태 변경 (`initial=true` 는 시작 시 첫 상태) |
| `sensor_error` / `sensor_recovered` | `error` / `failed_reads` | 센서 읽기 실패 / 복구 |
| `actuator_error` | `actuator`, `on`, `error`, (`commandId`) | 액추에이터 동작 실패 |
| `actuator_unavailable` | `actuator`, `reason` | 고장이나 상태를 몰라서 자동제어를 건너뜀 (사유가 바뀔 때만 1회) |
| `actuator_available` | `actuator` | 위 상태에서 복구되어 자동제어 재개 |
| `config_applied` | `version`, `thresholds` | 클라우드 기준값 적용 |
| `config_rejected` | `version`, `error` | 잘못된 기준값이라 거부 |

## 오프라인 자동제어 규칙

기준값은 모두 **초과** 기준 (같으면 보통).

| 조건 | 동작 |
|---|---|
| PM2.5 > 35 또는 PM10 > 80 | 창문 닫기 + 공기청정기 켜기 (환기보다 우선) |
| CO2 > 1000 또는 온도 > 30 | 창문 열기 |
| 습도 > 70 | 제습기 켜기. **제습기가 없으면** 창문 열기 |
| 모두 정상 | 창문 닫기, 공기청정기·제습기 끄기 |

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

로컬 테스트용 브로커는 라즈베리파이에 Mosquitto 를 설치해서 쓴다.

```bash
sudo apt install -y mosquitto mosquitto-clients
```

## 테스트

### 단위 테스트

```bash
pytest
```

### 동작 테스트 (라즈베리파이)

클라우드 역할은 `mosquitto_sub` / `mosquitto_pub` 로 대신한다.

```bash
mosquitto_sub -v -t 'roomcare/#'                                                      # 전송 데이터 보기
mosquitto_pub -t roomcare/room-001/command -m '{"commandId":"c-1","actuator":"window","on":true}'   # 원격 조작
mosquitto_pub -r -t roomcare/room-001/config -m '{"version":1,"thresholds":{"co2High":900}}'     # 기준값 변경
sudo systemctl stop mosquitto    # 통신 단절
sudo systemctl start mosquitto   # 통신 복구
```

| 테스트 | 방법 | 결과 |
|---|---|---|
| 데이터 전송 | `mosquitto_sub` 로 수신 확인 | ✅ 2초마다 telemetry 수신 |
| 원격 조작 | `mosquitto_pub` 로 창문 열기 명령 | ✅ window → True |
| 오프라인 전환 | 브로커 정지 | ✅ 약 2.3초 만에 OFFLINE (기준 10초) |
| 오프라인 자동제어 | 미세먼지 높은 상황 | ✅ 창문 닫고 공기청정기 켬 |
| 복구 | 브로커 재시작 | ✅ 밀린 데이터와 `reconnected` 이벤트 전송 |

테스트 환경: Raspberry Pi + 로컬 Mosquitto, Mock 센서/액추에이터 (2026-10-01)

> 브로커 정지는 연결이 정상적으로 닫혀 끊김이 바로 감지되는 경우다.
> 랜선/Wi-Fi 가 끊기는 경우는 keepalive 로 감지하므로 더 오래 걸린다 (설계상 ≤ 8.5초). 추가 확인 필요.
