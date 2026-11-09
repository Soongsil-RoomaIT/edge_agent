# 라즈베리파이 테스트 순서

파이를 켰을 때 위에서부터 따라 한다. 결과는 맨 아래 표에 적는다.

## 0. 준비

```bash
git clone https://github.com/Soongsil-RoomaIT/edge_agent.git   # 이미 있으면: cd edge_agent && git pull
cd edge_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp -n config/config.example.toml config/config.toml
pytest -q                                   # 노트북과 같은 결과가 나와야 함
sudo apt install -y mosquitto mosquitto-clients
timedatectl | grep synchronized             # yes 여야 원격 명령 expiresAt 판단이 맞음
```

## 1. 파이 안에서 v1 형식 확인 (브로커 = 파이)

`config/config.toml` 의 `[mqtt] host = "localhost"`. 터미널 3개.

```bash
# 터미널 1: 에이전트
python -m edge_agent

# 터미널 2: 나가는 메시지 보기
mosquitto_sub -v -t 'roomcare/#'
```

| 확인 | 기대 |
|---|---|
| `sensors` | 2초마다, `measuredAt` 이 `...Z` 로 끝나는 ISO 시각 |
| `status` | 3초마다 `{"online": true, "offlineMode": false}` |
| `devices` | 시작 시 `window-1`, `purifier-1`, `dehumidifier-1`, `door-1` 각각 한 번 |

```bash
# 터미널 3: 원격 명령 (창문 열기). expiresAt 은 30초 뒤
send() {
  mosquitto_pub -t roomcare/room-001/commands -m "{\"commandId\":\"$(cat /proc/sys/kernel/random/uuid)\",\"deviceId\":\"$1\",\"action\":\"$2\",\"reason\":\"test\",\"issuedAt\":\"$(date -u +%FT%TZ)\",\"expiresAt\":\"$(date -u -d '+30 sec' +%FT%TZ)\"}"
}
send window-1 OPEN          # -> ack EXECUTED, devices window-1 OPEN
send door-1 OPEN            # -> ack FAILED "door can only be closed"
send purifier-1 OPEN        # -> ack FAILED (가전에 OPEN 은 없음)
```

```bash
# 통신 끊김 / 복구
sudo systemctl stop mosquitto       # 에이전트 로그: 수 초 안에 OFFLINE
sudo systemctl start mosquitto      # recovery -> status -> devices 순서로 수신, 5초·15초 뒤 recovery 재전송
```

## 2. Wi-Fi 끊김 테스트 (브로커 = 노트북)

브로커가 파이 안에 있으면 Wi-Fi 를 끊어도 연결이 안 끊긴다. 그래서 브로커를 노트북에 띄운다.
지금까지는 브로커를 정상 종료하는 경우(연결이 바로 닫힘)만 확인했다. 이건 연결이 말없이 사라지는 경우라
keepalive 로 감지하며, 설계상 **8.5초 이내**여야 한다 (NFR03: 10초).

노트북 (PowerShell, cloud-server 폴더). 방화벽 허용 창이 뜨면 개인 네트워크 허용.

```powershell
docker run --rm -p 1883:1883 -v ${PWD}/config/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro eclipse-mosquitto:2
ipconfig    # 노트북 Wi-Fi IPv4 주소 확인
```

파이: `config/config.toml` 의 `[mqtt] host = "<노트북 IP>"` 로 바꾸고 에이전트 실행.

```bash
python -m edge_agent 2>&1 | tee wifi-test.log
```

다른 SSH 창에서 (SSH 도 Wi-Fi 라 끊기므로 30초 뒤 자동으로 다시 켠다)

```bash
sudo nohup sh -c 'date +%T.%N > /tmp/wifi-down; ip link set wlan0 down; sleep 30; ip link set wlan0 up' >/dev/null 2>&1 &
```

다시 접속한 뒤

```bash
cat /tmp/wifi-down                  # 끊은 시각
grep -m1 OFFLINE wifi-test.log      # 오프라인 전환 시각. 차이가 10초 이내면 통과
grep recovery wifi-test.log         # 복구 리포트 전송
```

유선(랜선)으로 SSH 중이면 `wlan0` 대신 랜선을 뽑아도 된다.

## 3. 클라우드 서버까지 연결 (선택)

노트북에서 cloud-server 를 `MQTT_PROTOCOL=roomait-v1` 로 띄우고 브로커 포트를 `0.0.0.0:1883` 으로 열면
파이 에이전트가 실제 서버에 붙는다. 프론트 대시보드에서 센서 값과 원격 조작까지 확인할 수 있다.
(2026-11-09 노트북 안에서 같은 구성으로 확인함: 원격 조작 EXECUTED, 끊김 2.2초 만에 오프라인 전환, 복구 리포트 수신)

## 4. 자동 실행 등록

```bash
bash deploy/install-service.sh                       # Mock
bash deploy/install-service.sh ../hardware/.venv     # 실제 하드웨어 (README "실제 하드웨어로 실행")
journalctl -u edge-agent -f
sudo reboot                                          # 부팅 후 자동으로 켜지는지
sudo systemctl kill -s KILL edge-agent               # 죽이면 3초 뒤 다시 켜지는지
```

## 결과

| 테스트 | 결과 | 비고 |
|---|---|---|
| pytest | | |
| sensors / status / devices 형식 | | |
| 원격 명령 ack (OPEN, 문 OPEN 거부) | | |
| 브로커 정지 → 오프라인 전환 시간 | | 이전: 2.3초 (2026-10-01, v1 전) |
| 복구 리포트 | | |
| Wi-Fi 끊김 → 오프라인 전환 시간 | | 기준 10초 |
| 부팅 시 자동 실행 / 죽으면 재시작 | | |
