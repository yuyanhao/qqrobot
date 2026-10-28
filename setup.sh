#!/usr/bin/env bash
# Installs the problem-only bot. Does not erase existing configuration or data.
set -euo pipefail
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/qq-math-bot
if [[ ${EUID} -ne 0 ]]; then
  echo '请用 sudo bash setup.sh 运行。'
  exit 1
fi
if systemctl is-active --quiet qqbot; then
  echo '检测到旧 qqbot 服务仍在运行。请先 sudo systemctl stop qqbot，再重新执行本脚本，避免同一个机器人重复登录。'
  exit 1
fi
if [[ ! -f "$SOURCE_DIR/.env" && ! -f "$APP_DIR/.env" ]]; then
  echo '先把原机器人的 .env 复制到升级包目录，或复制 .env.example 为 .env 后填写密钥。'
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates \
  texlive-xetex texlive-lang-chinese texlive-latex-extra texlive-science fonts-noto-cjk
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
  echo '需要 Python 3.12 或更新版本。建议 Ubuntu 24.04/26.04，或使用本包 Docker 部署。'
  exit 1
fi
id qqbot >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin qqbot
install -d -m 755 "$APP_DIR"
install -d -o qqbot -g qqbot -m 700 "$APP_DIR/data"
service_was_active=false
restore_service() {
  local code=$?
  trap - EXIT
  if [[ $code -ne 0 && "$service_was_active" == true ]]; then
    if systemctl start qq-math-bot; then
      echo '安装未完成，已尝试重新启动服务；程序或依赖可能已部分更新，请检查上方错误及服务日志。'
    else
      echo '安装未完成且服务恢复失败，请查看 sudo journalctl -u qq-math-bot -n 100 --no-pager。'
    fi
  fi
  exit "$code"
}
trap restore_service EXIT
if systemctl is-active --quiet qq-math-bot; then
  service_was_active=true
  systemctl stop qq-math-bot
fi
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
  install -m 644 "$SOURCE_DIR"/*.py "$SOURCE_DIR/requirements.txt" "$APP_DIR/"
  cp -a "$SOURCE_DIR/skills" "$APP_DIR/"
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
  install -o root -g qqbot -m 640 "$SOURCE_DIR/.env" "$APP_DIR/.env"
fi
chown root:qqbot "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" -m compileall -q "$APP_DIR" -x '/\.venv/'
cat > /etc/systemd/system/qq-math-bot.service <<'UNIT'
[Unit]
Description=QQ Math and Physics Problem Bot
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=qqbot
Group=qqbot
WorkingDirectory=/opt/qq-math-bot
EnvironmentFile=/opt/qq-math-bot/.env
Environment=DATA_DIR=/opt/qq-math-bot/data
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1
Environment=OPENBLAS_NUM_THREADS=1
Environment=OMP_NUM_THREADS=1
ExecStart=/usr/bin/env DATA_DIR=/opt/qq-math-bot/data /opt/qq-math-bot/.venv/bin/python /opt/qq-math-bot/task_bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/qq-math-bot/data
MemoryHigh=1200M
MemoryMax=1536M
CPUQuota=150%
TasksMax=96
TimeoutStopSec=30
KillMode=control-group
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable qq-math-bot
systemctl restart qq-math-bot
if ! systemctl is-active --quiet qq-math-bot; then
  echo '服务启动失败，请查看 sudo journalctl -u qq-math-bot -n 100 --no-pager。'
  exit 1
fi
trap - EXIT
echo '服务已启动。查看状态：sudo systemctl status qq-math-bot'
echo '查看日志：sudo journalctl -u qq-math-bot -n 100 --no-pager'
echo '原 qqbot 服务与原目录保留。配置在 /opt/qq-math-bot/.env，修改后需重启新服务。'
