# edge_agent

자취방 원격 케어 시스템의 **Edge Agent** (Raspberry Pi, Python).

- 센서(온습도, CO2, 미세먼지) 측정 → SQLite 로컬 저장 → MQTT로 클라우드 전송
- 클라우드 원격 명령 수신 → 액추에이터(창문, 공기청정기, 제습기, 문 닫기) 구동 → **실제 도달 확인 후** ack, 기기 상태 전송
- 클라우드에서 자동제어 기준값(config) 수신 → 안전 범위 확인 후 적용
- **통신 단절 시 10초 이내 오프라인 자동제어 모드 전환** (NFR03), 복구 시 오프라인 동안 완료한 동작 리포트 + 밀린 데이터 전송

## 구조

```
src/edge_agent/
├── __main__.py      # 진입점 (python -m edge_agent)
├── agent.py         # 메인 루프: 측정 / 저장 / 전송 / 모드 전환
├── connectivity.py  # 온라인/오프라인 모드 판단
├── auto_control.py  # 오프라인 자동제어 규칙
├── storage.py       # SQLite (미전송 데이터, 받은 명령, 오프라인 동작 기록, 설정)
├── protocol.py      # 클라우드 메시지 계약 (roomait-v1): 기기 id, 시각, 명령 검증
├── mqtt_client.py   # MQTT 클라이언트 (paho-mqtt)
├── devices.py       # 센서/액추에이터 인터페이스 + Mock (실제 드라이버는 hardware 레포)
└── config.py        # 설정 로딩
```

실제 센서/액추에이터 드라이버는 [`hardware`](https://github.com/Soongsil-RoomaIT/hardware) 레포(`roomcare_hw`)에 있고,
`use_mock_devices = false` 면 그 `HardwareBundle` 로 연결한다 ([실제 하드웨어로 실행](#실제-하드웨어로-실행)).
기본값은 Mock 이라 노트북에서도 그대로 돌아간다.

## MQTT 계약 (roomait-v1)

[cloud-server `docs/MQTT.md`](https://github.com/Soongsil-RoomaIT/cloud-server/blob/main/docs/MQTT.md) 의
**완료 ACK를 지원하는 확장 계약**을 따른다. 클라우드는 `MQTT_PROTOCOL=roomait-v1` 로 실행해야 한다.

- 토픽: `roomcare/{deviceId}/<suffix>` (클라우드 `MQTT_TOPIC_PREFIX=roomcare/room-001`)
- 시각: UTC ISO 8601 문자열 (`2026-11-05T03:00:00.123Z`)
- 기기 id: `window-1`, `purifier-1`, `dehumidifier-1`, `door-1`
- 상태: 창문·문 `OPEN`/`CLOSED`, 가전 `ON`/`OFF`
- 클라우드는 strict 스키마라 키가 더 있거나 빠지면 메시지를 버린다 (`tests/test_contract.py` 로 확인)

| suffix | 방향 | payload | 언제 |
|---|---|---|---|
| `sensors` | 엣지 → 클라우드 | `{measuredAt, temperature, humidity, co2, pm25, pm10}` | 2초마다 (오프라인 동안은 쌓았다가 복구 시) |
| `devices` | 엣지 → 클라우드 | `{id, state, updatedAt}` | 기기마다. 상태가 바뀔 때 + 연결/복구 시 전체 |
| `status` | 엣지 → 클라우드 | `{online, offlineMode}` | 3초마다 heartbeat. LWT `{online:false, offlineMode:true}` (retained) |
| `recovery` | 엣지 → 클라우드 | `{offlineSince, recoveredAt, localActions:[{executedAt, deviceId, state, reason}]}` | 복구 시 `status` 보다 먼저 |
| `ack` | 엣지 → 클라우드 | `{commandId, status: EXECUTED\|FAILED, reason, executedAt}` | 명령 완료/실패 시 |
| `events` | 엣지 → 클라우드 | `{occurredAt, type, detail}` | 진단용 (클라우드는 아직 구독 안 함) |
| `commands` | 클라우드 → 엣지 | `{commandId, deviceId, action, reason, issuedAt, expiresAt}` | 원격 조작 |
| `config` | 클라우드 → 엣지 | `{version, thresholds}` | 오프라인 자동제어 기준값 (retained 권장, 클라우드 미구현) |
| `cloud-status` | 클라우드 → 엣지 | `{online}` | 클라우드 서버 heartbeat (선택, 클라우드 미구현) |

### commands → ack

```json
{"commandId": "6f1c...-uuid", "deviceId": "window-1", "action": "OPEN", "reason": "사용자 요청",
 "issuedAt": "2026-11-05T03:00:00.000Z", "expiresAt": "2026-11-05T03:00:20.000Z"}
```

```json
{"commandId": "6f1c...-uuid", "status": "EXECUTED", "reason": "window reached OPEN", "executedAt": "2026-11-05T03:00:07.512Z"}
```

- **ack 는 기기가 실제로 목표 상태에 도달했을 때** 보낸다. 서보·모터는 비동기라 `set()` 직후에는 아직 움직이는 중이다.
  - 창문: 리밋 스위치 `position` 이 `open`/`closed` 가 되고 `is_moving` 이 끝나면
  - 문: 터치센서가 닫힘을 감지하면
  - 공기청정기·제습기: 버튼 서보 동작이 끝나고 상태가 목표와 같으면
- `expiresAt` 까지 도달을 확인하지 못하면 `FAILED`. 고장(`fault`)이 나면 바로 `FAILED`.
- **같은 `commandId` 는 한 번만 실행**한다. SQLite 에 기록해서 재시작 후에도 유지된다.
  다시 오면 이미 보낸 ack 를 재전송하고, 실행 중이면 무시한다. 실행 중 재시작된 명령은 다시 실행하지 않고 `FAILED`.
- `expiresAt` 이 이미 지났으면 실행하지 않고 `FAILED`. **파이 시계가 맞아야 한다** (NTP, Raspberry Pi OS 기본 켜짐).
- 같은 기기에 새 명령이 오면 이전 명령은 `FAILED` (`superseded by a newer command`).
- 잘못된 명령도 `FAILED` 와 사유: 모르는 기기, 기기에 맞지 않는 action (`purifier-1` 에 `OPEN` 등), 문 `OPEN`.
- `commandId` 가 없으면 ack 를 보낼 수 없어서 무시한다.

### devices

```json
{"id": "window-1", "state": "CLOSED", "updatedAt": "2026-11-05T03:00:07.600Z"}
```

- 상태를 모르는 기기(창문 중간 위치, 재시작 후 가전 상태 미확인, 고장)는 보내지 않는다. 계약에 unknown 값이 없다.
- 공기청정기·제습기는 버튼을 누르는 서보라 상태가 **마지막 동작으로 추정한 값**이다.

### recovery

```json
{"offlineSince": "2026-11-05T03:00:00.000Z", "recoveredAt": "2026-11-05T03:05:00.000Z",
 "localActions": [{"executedAt": "2026-11-05T03:01:10.000Z", "deviceId": "window-1", "state": "OPEN", "reason": "ventilate: co2"}]}
```

- `localActions` 에는 오프라인 자동제어로 **실제 완료를 확인한 동작만** 넣는다. 명령만 보내고 도달하지 못한 동작은 빠진다.
- 한 번 보낸 동작은 다음 복구 때 다시 보내지 않는다.
- 부팅 후 처음 연결될 때는 보내지 않는다 (그 전에 온라인이었던 적이 없음).
- 같은 리포트를 5초, 15초 뒤 한 번씩 다시 보낸다. 엣지가 클라우드보다 먼저 재연결되면 클라우드가 구독하기 전에 보낸 리포트를 놓치기 때문이다. 클라우드는 `offlineSince`/`recoveredAt` 이 같은 리포트를 한 번만 처리한다.

### status heartbeat 와 클라우드 heartbeat

- 클라우드는 10초 동안 엣지 메시지가 없으면 오프라인으로 본다. 센서가 고장 나도 `status` 가 3초마다 가므로 오프라인으로 오해하지 않는다.
- 브로커는 붙어 있는데 엣지가 오프라인 모드면 (아래 클라우드 heartbeat 끊김) `{online: true, offlineMode: true}` 를 보낸다.
- **클라우드 서버만 죽고 브로커는 살아 있으면** 엣지는 브로커 연결만으로는 알 수 없다.
  `cloud_heartbeat_timeout` 을 켜면 `cloud-status` 가 그 시간 동안 안 올 때 오프라인 모드로 전환한다.
  - 클라우드가 1~2초마다 `{"online": true}` 를 보내고, MQTT LWT 로 `{"online": false}` (retained) 를 걸어야 한다.
  - 기본값 0 = 사용 안 함 (클라우드가 아직 보내지 않음). 켤 때는 `5` 권장 (5 + 유예 2 + 0.5 = 7.5초 ≤ 10초).

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
| `humidityOff` | 60 | 30 ~ 80 (`humidityHigh` 보다 작아야 함) |
| `temperatureHigh` | 30 | 24 ~ 35 |

- 바꿀 키만 보내도 된다. 하나라도 모르는 키이거나 범위 밖이면 **전체 거부** (`config_rejected` 이벤트).
- 적용하면 `config_applied` 이벤트를 남기고 SQLite 에 저장한다. 재시작해도 `config.toml` 보다 우선.
- `version` 은 클라우드가 1씩 올린다. 지금보다 낮은 version 은 무시. 같은 내용이 다시 오면 조용히 넘어간다.
- cloud-server·프론트 자동제어 설정 이름도 그대로 받는다: `humidityOn` (= `humidityHigh`), `humidityOff`, `co2Open` (= `co2High`), `pm25On` (= `pm25High`). 설정 저장 시 이 4개를 그대로 보내면 된다 (`enabled`, `mode`, `rainProbability` 는 빼고).
- 클라우드는 기준 '이상', 엣지는 '초과' 로 판단해서 기준값과 정확히 같을 때만 다르다.

### events (진단용)

| type | detail | 발생 시점 |
|---|---|---|
| `agent_started` | | 에이전트 시작 |
| `mode_changed` | `mode` | 온라인 ↔ 오프라인 전환 |
| `reconnected` | `offline_seconds` | 통신 복구 |
| `auto_control` | `actuator`, `on`, `reason` | 오프라인 자동제어 명령 (완료는 `recovery` 로) |
| `remote_command` | `actuator`, `on`, `commandId` | 원격 명령 실행 시작 (완료는 `ack` 로) |
| `door_opened` / `door_closed` | `initial` | 문 상태 변경 (`initial=true` 는 시작 시 첫 상태) |
| `sensor_error` / `sensor_recovered` | `error` / `failed_reads` | 센서 읽기 실패 / 복구 |
| `actuator_error` | `actuator`, `on`, `error`, `commandId` | 동작 실패, 목표 미도달, 고장 |
| `actuator_unavailable` | `actuator`, `reason` | 고장이나 상태를 몰라서 자동제어를 건너뜀 (사유가 바뀔 때만 1회) |
| `actuator_available` | `actuator` | 위 상태에서 복구되어 자동제어 재개 |
| `config_applied` | `version`, `thresholds` | 클라우드 기준값 적용 |
| `config_rejected` | `version`, `error` | 잘못된 기준값이라 거부 |

### PM10

클라우드는 5개 측정값이 모두 있어야 받는다. `pm10` 이 없는 측정값(hardware 드라이버가 아직 PM10 을 주지 않음)은
**보내지 않는다** (없는 값을 만들지 않음). hardware `Reading` 에 `pm10` 이 추가되어야 실제 센서 값이 클라우드에 저장된다.

## 오프라인 자동제어 규칙

켜는 기준은 **초과** (같으면 보통). 켜기와 끄기 기준을 다르게 둬서(히스테리시스), 기준값 근처에서
값이 오르내려도 2초마다 버튼 서보를 누르거나 창문 모터를 왕복시키지 않는다. 두 기준 사이에서는 지금 상태를 유지한다.

| 기기 | 켜기 (열기) | 끄기 (닫기) |
|---|---|---|
| 공기청정기 | PM2.5 > 35 또는 PM10 > 80 | PM2.5 ≤ 21 그리고 PM10 ≤ 48 (켜기 기준의 60%) |
| 제습기 | 습도 > 70 | 습도 ≤ 60 (`humidity_off`) |
| 창문 | CO2 > 1000 또는 온도 > 30 (제습기가 없으면 습도 > 70 도) | CO2 ≤ 800 그리고 온도 ≤ 28 (제습기가 없으면 습도 ≤ 60 도) |

- 미세먼지가 높으면 환기보다 우선해서 창문을 닫는다.
- 제습기가 있으면 습도는 제습기로 처리하고 창문은 CO2·온도로만 연다.
- 공기청정기 60% 기준과 습도 켜기/끄기 분리는 cloud-server 온라인 자동화와 같다.

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

## 실제 하드웨어로 실행

라즈베리파이에 `edge_agent` 와 `hardware` 를 형제 폴더로 두고 같은 venv 에 둘 다 설치한다.
venv 만드는 방법(`--system-site-packages`, lgpio)은 hardware README 를 따른다.

```bash
cd hardware && source .venv/bin/activate
pip install -e '.[hardware]'
pip install -e ../edge_agent
cd ../edge_agent
```

`config/config.toml`

```toml
use_mock_devices = false

[hardware]
temperature_model = "SHT31"
window_motor = true            # 배선·기구 확인한 것만 true
purifier = true
purifier_initial = "off"       # 실제 전원을 눈으로 보고 on / off
door_sensor = true
door_verified = true
door_closer = true
```

```bash
python -m edge_agent
```

- 액추에이터는 기본으로 모두 꺼져 있다. 켜지 않은 기기는 원격 명령에 `FAILED` (`... is not connected`) 로 답한다.
- `purifier_initial` / `dehumidifier_initial` 이 `unknown` 이면 버튼 서보를 누르지 않는다 (상태를 모르고 누르면 반대로 될 수 있음).
- 문 10분 자동 닫기는 클라우드 정책이다. hardware 의 `DoorAutoCloser` 는 같이 켜지 않는다.
- 종료(Ctrl+C, `systemctl stop`) 시 GPIO 와 센서 수집 스레드를 정리한다.
- hardware 의 `examples/edge_agent_hardware.py` 도 같은 일을 하지만, 이 방법은 설정 파일 하나로 끝난다.

## 라즈베리파이 자동 실행

```bash
bash deploy/install-service.sh                       # edge_agent/.venv (Mock)
bash deploy/install-service.sh ../hardware/.venv     # 실제 하드웨어
journalctl -u edge-agent -f                          # 로그
```

부팅하면 자동으로 켜지고, 죽으면 3초 뒤 다시 켜진다. 인터넷 없이 부팅해도 시작한다 (오프라인 자동제어).

## 테스트

### 단위 테스트

```bash
pytest
```

### 동작 테스트 (라즈베리파이)

순서와 명령어는 [docs/pi-test.md](docs/pi-test.md) (v1 형식 확인, Wi-Fi 끊김, 자동 실행 등록).

이전 결과 (roomait-v1 전 형식)

| 테스트 | 방법 | 결과 |
|---|---|---|
| 데이터 전송 | `mosquitto_sub` 로 수신 확인 | ✅ 2초마다 telemetry 수신 |
| 원격 조작 | `mosquitto_pub` 로 창문 열기 명령 | ✅ window → True |
| 오프라인 전환 | 브로커 정지 | ✅ 약 2.3초 만에 OFFLINE (기준 10초) |
| 오프라인 자동제어 | 미세먼지 높은 상황 | ✅ 창문 닫고 공기청정기 켬 |
| 복구 | 브로커 재시작 | ✅ 밀린 데이터와 `reconnected` 이벤트 전송 |

테스트 환경: Raspberry Pi + 로컬 Mosquitto, Mock 센서/액추에이터 (2026-10-01, roomait-v1 전환 전 형식)

> 브로커 정지는 연결이 정상적으로 닫혀 끊김이 바로 감지되는 경우다.
> 랜선/Wi-Fi 가 끊기는 경우는 keepalive 로 감지하므로 더 오래 걸린다 (설계상 ≤ 8.5초). 추가 확인 필요.
