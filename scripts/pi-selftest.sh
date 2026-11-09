#!/usr/bin/env bash
# 라즈베리파이 동작 테스트 자동화. docs/pi-test.md 의 순서를 명령 하나로 실행하고 결과표를 만든다.
#
#   bash scripts/pi-selftest.sh                          # 준비 + pytest + v1 형식 + 원격 명령 + 끊김/복구
#   bash scripts/pi-selftest.sh --wifi 192.168.0.10      # + Wi-Fi 끊김 (브로커 = 노트북 IP)
#   bash scripts/pi-selftest.sh --install-service        # 끝나고 systemd 자동 실행 등록
#
# Wi-Fi 테스트는 네트워크를 30초 끊으므로 SSH 도 끊긴다. 이렇게 실행하고 다시 접속해서 결과를 본다.
#   nohup bash scripts/pi-selftest.sh --wifi 192.168.0.10 > selftest.out 2>&1 &
#   (다시 접속 후) cat selftest-result.md
#
# 테스트는 항상 Mock 장치와 임시 DB 로 돌린다. config/config.toml 이 실제 하드웨어 설정이어도 기기는 움직이지 않는다.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
WIFI_BROKER=""
INSTALL_SERVICE=""
SERVICE_VENV=""
while [ $# -gt 0 ]; do
  case "$1" in
    --wifi) WIFI_BROKER="${2:?--wifi 다음에 브로커 IP}"; shift 2 ;;
    --install-service) INSTALL_SERVICE=1; if [ -n "${2:-}" ] && [ "${2#--}" = "$2" ]; then SERVICE_VENV="$2"; shift; fi; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "모르는 옵션: $1"; exit 2 ;;
  esac
done

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
WORK="$(mktemp -d)"
RESULT="$REPO/selftest-result.md"
PREFIX="roomcare/room-001"
AGENT_PID=""; SUB_PID=""; DOWN_IFACE=""
ROWS=()
FAILED=0

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
row()  {
  ROWS+=("| $1 | $2 | $3 |"); printf '  %s %s  %s\n' "$2" "$1" "$3"
  if [ "$2" = "❌" ]; then
    FAILED=1
    if [ -s "$WORK/agent.log" ]; then
      echo "     에이전트 로그 마지막 5줄:"; tail -5 "$WORK/agent.log" | sed 's/^/       /'
    fi
  fi
}
now()  { date +%s.%N; }
since() { awk -v a="$1" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}'; }

cleanup() {
  [ -n "$DOWN_IFACE" ] && $SUDO ip link set "$DOWN_IFACE" up 2>/dev/null
  [ -n "$AGENT_PID" ] && kill "$AGENT_PID" 2>/dev/null
  [ -n "$SUB_PID" ] && kill "$SUB_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

# --- 로컬 브로커 (systemd 가 없으면 직접 실행: Docker 등) ---------------------------
have_systemd_mosquitto() { command -v systemctl >/dev/null && systemctl cat mosquitto.service >/dev/null 2>&1; }
broker_start() {
  if have_systemd_mosquitto; then $SUDO systemctl start mosquitto
  else pgrep -x mosquitto >/dev/null || mosquitto -d -p 1883; fi
  for _ in $(seq 1 30); do mosquitto_pub -h localhost -t selftest/ping -m x 2>/dev/null && return 0; sleep 0.2; done
  return 1
}
broker_stop() {
  if have_systemd_mosquitto; then $SUDO systemctl stop mosquitto; else pkill -x mosquitto; fi
}

start_agent() {  # $1 = 브로커 주소
  cat > "$WORK/config.toml" <<EOF
device_id = "room-001"
db_path = "$WORK/edge.db"
use_mock_devices = true
[mqtt]
host = "$1"
port = 1883
EOF
  rm -f "$WORK/edge.db"
  "$PY" -m edge_agent --config "$WORK/config.toml" > "$WORK/agent.log" 2>&1 &
  AGENT_PID=$!
}
stop_agent() { [ -n "$AGENT_PID" ] && kill "$AGENT_PID" 2>/dev/null; wait "$AGENT_PID" 2>/dev/null; AGENT_PID=""; }
start_sub() {  # $1 = 브로커 주소
  mosquitto_sub -h "$1" -v -t "$PREFIX/#" >> "$WORK/msgs.log" 2>/dev/null &
  SUB_PID=$!
}
stop_sub() { [ -n "$SUB_PID" ] && kill "$SUB_PID" 2>/dev/null; wait "$SUB_PID" 2>/dev/null; SUB_PID=""; }
wait_log() {  # $1 = 패턴, $2 = 최대 초. 찾으면 0
  # printf 로 소수점까지 출력 (print 는 1.79421e+09 처럼 잘려서 대기 시간이 틀어진다)
  local end; end=$(awk -v t="$(now)" -v d="$2" 'BEGIN{printf "%.3f", t+d}')
  while awk -v t="$(now)" -v e="$end" 'BEGIN{exit !(t<e)}'; do
    grep -q "$1" "$WORK/agent.log" 2>/dev/null && return 0
    sleep 0.1
  done
  return 1
}
send() {  # $1 = deviceId, $2 = action, $3 = commandId
  mosquitto_pub -h localhost -t "$PREFIX/commands" -m "{\"commandId\":\"$3\",\"deviceId\":\"$1\",\"action\":\"$2\",\"reason\":\"selftest\",\"issuedAt\":\"$(date -u +%FT%TZ)\",\"expiresAt\":\"$(date -u -d '+30 sec' +%FT%TZ)\"}"
}
check() {  # msgs.log 를 검사하는 파이썬. 출력: 결과|비고
  "$PY" - "$WORK/msgs.log" "$@" <<'PYEOF'
import json, re, sys
path, what, *args = sys.argv[1:]
iso = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
msgs = []
for line in open(path, encoding="utf8"):
    topic, _, payload = line.rstrip("\n").partition(" ")
    try:
        msgs.append((topic.rsplit("/", 1)[-1], json.loads(payload)))
    except ValueError:
        pass
def on(kind): return [p for k, p in msgs if k == kind]
ok = lambda cond, note: print(("✅" if cond else "❌") + "|" + note)
if what == "sensors":
    s = on("sensors")
    good = s and all(set(p) == {"measuredAt", "temperature", "humidity", "co2", "pm25", "pm10"} and iso.match(p["measuredAt"]) for p in s)
    ok(len(s) >= 2 and good, f"{len(s)}개 수신, 형식 {'정상' if good else '오류'}")
elif what == "status":
    s = on("status")
    ok(bool(s) and s[-1] == {"online": True, "offlineMode": False}, f"{len(s)}개, 마지막 {s[-1] if s else '-'}")
elif what == "devices":
    ids = {p.get("id") for p in on("devices")}
    want = {"window-1", "purifier-1", "dehumidifier-1", "door-1"}
    ok(want <= ids, ", ".join(sorted(ids)) or "없음")
elif what == "ack":
    cid, status, reason = args
    acks = [p for p in on("ack") if p.get("commandId") == cid]
    a = acks[0] if acks else {}
    ok(a.get("status") == status and reason in a.get("reason", ""), f"{a.get('status', '응답 없음')} {a.get('reason', '')}".strip())
elif what == "recovery":
    r = on("recovery")
    first = r[0] if r else {}
    ok(bool(r) and set(first) == {"offlineSince", "recoveredAt", "localActions"},
       f"{len(r)}번 수신 (재전송 포함), 로컬 동작 {len(first.get('localActions', []))}건" if r else "수신 못 함")
PYEOF
}
add() { local label=$1; shift; IFS='|' read -r mark note <<< "$(check "$@")"; row "$label" "$mark" "$note"; }

# --- 0. 준비 ------------------------------------------------------------------
log "준비"
if ! command -v mosquitto >/dev/null || ! command -v mosquitto_sub >/dev/null; then
  $SUDO apt-get install -y mosquitto mosquitto-clients >/dev/null || { echo "mosquitto 설치 실패"; exit 1; }
fi
[ -x .venv/bin/python ] || python3 -m venv .venv || { echo "venv 생성 실패 (sudo apt install python3-venv)"; exit 1; }
PY="$REPO/.venv/bin/python"
"$PY" -m pip install -q -e ".[dev]" || { echo "pip install 실패"; exit 1; }
[ -f config/config.toml ] || cp config/config.example.toml config/config.toml
if command -v timedatectl >/dev/null; then
  sync=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)
  [ "$sync" = "yes" ] && row "시간 동기화" "✅" "NTP 동기화됨" || row "시간 동기화" "⚠️" "안 됨: 원격 명령 expiresAt 판단이 틀릴 수 있음"
fi

# --- 1. 단위 테스트 -------------------------------------------------------------
log "pytest"
if out=$("$PY" -m pytest -q 2>&1); then row "pytest" "✅" "$(echo "$out" | tail -1)"
else row "pytest" "❌" "$(echo "$out" | tail -1)"; fi

# --- 2. v1 형식 (브로커 = 파이) ---------------------------------------------------
log "v1 메시지 형식 (로컬 브로커)"
broker_start || { row "로컬 브로커" "❌" "mosquitto 시작 실패"; }
: > "$WORK/msgs.log"
start_sub localhost
start_agent localhost
if wait_log "ONLINE" 10; then
  sleep 6
  add "sensors 형식" sensors; add "status heartbeat" status; add "devices 4개" devices
else
  row "브로커 연결" "❌" "10초 안에 ONLINE 안 됨"
fi

# --- 3. 원격 명령 ---------------------------------------------------------------
log "원격 명령"
C1=$(cat /proc/sys/kernel/random/uuid); C2=$(cat /proc/sys/kernel/random/uuid); C3=$(cat /proc/sys/kernel/random/uuid)
send window-1 OPEN "$C1"; send door-1 OPEN "$C2"; send purifier-1 OPEN "$C3"
sleep 3
add "창문 열기 → EXECUTED" ack "$C1" EXECUTED "reached OPEN"
add "문 열기 거부" ack "$C2" FAILED "door can only be closed"
add "공기청정기에 OPEN 거부" ack "$C3" FAILED "invalid action"

# --- 4. 끊김 / 복구 (브로커 정지) ---------------------------------------------------
log "통신 끊김 / 복구 (브로커 정지)"
stop_sub
t0=$(now); broker_stop
if wait_log "OFFLINE auto-control" 15; then
  sec=$(since "$t0")
  awk -v s="$sec" 'BEGIN{exit !(s<=10)}' && row "브로커 정지 → 오프라인" "✅" "${sec}초 (기준 10초)" || row "브로커 정지 → 오프라인" "❌" "${sec}초 (기준 10초)"
else
  row "브로커 정지 → 오프라인" "❌" "15초 안에 전환 안 됨"
fi
sleep 3
: > "$WORK/msgs.log"
broker_start; start_sub localhost
sleep 18   # 재연결 + 복구 리포트 재전송(5초, 15초)
add "복구 리포트" recovery
stop_agent; stop_sub

# --- 5. Wi-Fi 끊김 (브로커 = 노트북) -------------------------------------------------
if [ -n "$WIFI_BROKER" ]; then
  log "Wi-Fi 끊김 (브로커 $WIFI_BROKER)"
  IFACE=$(ip route get "$WIFI_BROKER" 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit}}')
  if [ -z "$IFACE" ]; then
    row "Wi-Fi 끊김" "❌" "$WIFI_BROKER 로 가는 경로 없음"
  elif ! mosquitto_pub -h "$WIFI_BROKER" -t selftest/ping -m x 2>/dev/null; then
    row "Wi-Fi 끊김" "❌" "$WIFI_BROKER:1883 접속 안 됨 (노트북 브로커·방화벽 확인)"
  else
    start_agent "$WIFI_BROKER"
    if wait_log "ONLINE" 10; then
      sleep 3
      t0=$(now); DOWN_IFACE="$IFACE"; $SUDO ip link set "$IFACE" down
      if wait_log "OFFLINE auto-control" 20; then
        sec=$(since "$t0")
        awk -v s="$sec" 'BEGIN{exit !(s<=10)}' && row "Wi-Fi 끊김 → 오프라인 ($IFACE)" "✅" "${sec}초 (기준 10초)" || row "Wi-Fi 끊김 → 오프라인 ($IFACE)" "❌" "${sec}초 (기준 10초)"
      else
        row "Wi-Fi 끊김 → 오프라인 ($IFACE)" "❌" "20초 안에 전환 안 됨"
      fi
      sleep 5
      $SUDO ip link set "$IFACE" up; DOWN_IFACE=""
      if wait_log "recovery report sent" 60; then row "Wi-Fi 복구" "✅" "재연결 후 복구 리포트 전송"
      else row "Wi-Fi 복구" "❌" "60초 안에 재연결 안 됨"; fi
    else
      row "Wi-Fi 끊김" "❌" "노트북 브로커에 ONLINE 안 됨"
    fi
    stop_agent
  fi
fi

# --- 결과 ---------------------------------------------------------------------
{
  echo "# 파이 동작 테스트 결과"
  echo
  echo "$(date '+%F %T') · $(hostname) · $(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)"
  echo
  echo "| 테스트 | 결과 | 비고 |"
  echo "|---|---|---|"
  printf '%s\n' "${ROWS[@]}"
} > "$RESULT"
cp "$WORK/agent.log" "$REPO/selftest-agent.log" 2>/dev/null
log "결과 ($RESULT)"
cat "$RESULT"

# --- 6. 자동 실행 등록 ------------------------------------------------------------
if [ -n "$INSTALL_SERVICE" ]; then
  if [ "$FAILED" -eq 0 ]; then
    log "systemd 자동 실행 등록"
    bash "$REPO/deploy/install-service.sh" ${SERVICE_VENV:+"$SERVICE_VENV"}
  else
    echo; echo "실패한 테스트가 있어서 자동 실행 등록은 건너뜀"
  fi
fi
exit "$FAILED"
