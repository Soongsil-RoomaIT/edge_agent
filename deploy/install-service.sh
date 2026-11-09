#!/usr/bin/env bash
# 라즈베리파이에서 edge agent 를 systemd 서비스로 등록한다.
# 부팅하면 자동으로 켜지고, 죽으면 3초 뒤 다시 켜진다.
#
#   bash deploy/install-service.sh                     # edge_agent/.venv 사용 (Mock)
#   bash deploy/install-service.sh ../hardware/.venv   # hardware 와 같은 venv (실제 하드웨어)
#
# 지우기:  sudo systemctl disable --now edge-agent && sudo rm /etc/systemd/system/edge-agent.service
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$(cd "${1:-$REPO/.venv}" 2>/dev/null && pwd)" || { echo "venv 가 없음: ${1:-$REPO/.venv}"; exit 1; }
PY="$VENV/bin/python"
CONFIG="$REPO/config/config.toml"

[ -x "$PY" ] || { echo "python 이 없음: $PY"; exit 1; }
"$PY" -c "import edge_agent" 2>/dev/null \
  || { echo "이 venv 에 edge_agent 가 설치되지 않음:  $PY -m pip install -e $REPO"; exit 1; }
[ -f "$CONFIG" ] \
  || { echo "설정 파일이 없음:  cp $REPO/config/config.example.toml $CONFIG"; exit 1; }

USER_NAME="$(id -un)"

# 시간 동기화(time-sync.target)는 기다리지 않는다. 인터넷 없이 부팅해도 오프라인 자동제어는 돌아야 한다.
# (시계가 틀린 동안은 원격 명령의 expiresAt 판단이 틀릴 수 있다. NTP 가 맞춰지면 정상.)
sudo tee /etc/systemd/system/edge-agent.service >/dev/null <<EOF
[Unit]
Description=Room care edge agent
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$REPO
ExecStart=$PY -m edge_agent --config $CONFIG
Restart=always
RestartSec=3
# SIGTERM 을 받으면 MQTT offline 전송, GPIO 정리 후 종료한다
TimeoutStopSec=15
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now edge-agent
echo
systemctl --no-pager --lines=5 status edge-agent || true
echo
echo "로그 보기:  journalctl -u edge-agent -f"
