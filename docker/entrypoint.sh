#!/usr/bin/env bash
set -euo pipefail

mkdir -p /data/config /data/logs /data/runtime /data/runtime/pyrogram
if [[ ! -f "${BLACKSEEDS_FILE:-/data/config/blackseeds.txt}" ]]; then
  touch "${BLACKSEEDS_FILE:-/data/config/blackseeds.txt}"
fi

BOOTSTRAP_MODE="false"
if [[ -n "${CONFIG_PATH:-}" && ! -f "${CONFIG_PATH}" ]]; then
  echo "[WARN] 未检测到配置文件: ${CONFIG_PATH}，将自动写入模板并仅启动管理面板引导配置。"
  mkdir -p "$(dirname "${CONFIG_PATH}")"
  cp /app/config-example.json "${CONFIG_PATH}"
  BOOTSTRAP_MODE="true"
fi
export BOOTSTRAP_MODE

wait_for_services() {
  python - <<'PY'
import os, sys, time
from pathlib import Path

sys.path.insert(0, '/app')

ready = True

def wait_tcp(host: str, port: int, label: str, timeout: int = 60):
    import socket
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                print(f"[INFO] {label} 已就绪: {host}:{port}")
                return True
        except Exception as e:
            last = e
            time.sleep(2)
    print(f"[WARN] 等待 {label} 超时: {host}:{port} ({last})")
    return False

cfg_path = Path(os.getenv('CONFIG_PATH', '/data/config/config.json'))
if not cfg_path.exists():
    raise SystemExit(0)

try:
    import json
    cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
except Exception as e:
    print(f"[WARN] 读取 config 失败，跳过依赖等待: {e}")
    raise SystemExit(0)

checks = [
    (cfg.get('host'), int(cfg.get('port') or 3306), 'MySQL'),
    (cfg.get('mspostgre_host'), int(cfg.get('mspostgre_port') or 5432), 'PostgreSQL'),
]
for host, port, label in checks:
    if host:
        ok = wait_tcp(str(host), int(port), label)
        ready = ready and ok
raise SystemExit(0 if ready else 0)
PY
}

if [[ "${ENABLE_CRON:-false}" == "true" ]]; then
  cat >/tmp/lyrebird-cron <<'CRONEOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
0 * * * * cd /app && python -m cron.bgimg >> /data/logs/cron.log 2>&1
0 0 1 * * cd /app && python cron_permonthfree.py >> /data/logs/cron.log 2>&1
CRONEOF
  crontab /tmp/lyrebird-cron
  cron
fi

if [[ $# -gt 0 ]]; then
  exec "$@"
fi

BOT_PID=""
WEB_PID=""
cleanup() {
  local code=$?
  if [[ -n "${BOT_PID}" ]] && kill -0 "${BOT_PID}" 2>/dev/null; then kill "${BOT_PID}" 2>/dev/null || true; fi
  if [[ -n "${WEB_PID}" ]] && kill -0 "${WEB_PID}" 2>/dev/null; then kill "${WEB_PID}" 2>/dev/null || true; fi
  wait || true
  exit $code
}
trap cleanup SIGINT SIGTERM EXIT

if [[ "${ADMIN_PANEL_ENABLED:-true}" == "true" ]]; then
  python /app/web_admin.py &
  WEB_PID=$!
  echo "[INFO] 管理面板已启动，PID=${WEB_PID}，端口=${ADMIN_PANEL_PORT:-47521}"
fi

if [[ "${BOOTSTRAP_MODE}" != "true" ]]; then
  wait_for_services
  python /app/main.py &
  BOT_PID=$!
  echo "[INFO] Bot 已启动，PID=${BOT_PID}"
else
  echo "[WARN] 当前为引导模式，Bot 暂不启动。请先通过面板完善 config.json 后重启容器。"
fi

while true; do
  if [[ -n "${BOT_PID}" ]] && ! kill -0 "${BOT_PID}" 2>/dev/null; then
    echo "[FATAL] Bot 进程已退出，容器将停止" >&2
    wait "${BOT_PID}" || true
    exit 1
  fi
  if [[ -n "${WEB_PID}" ]] && ! kill -0 "${WEB_PID}" 2>/dev/null; then
    echo "[ERROR] 管理面板进程已退出，尝试重启一次" >&2
    python /app/web_admin.py &
    WEB_PID=$!
  fi
  sleep 5
done
