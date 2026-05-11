#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/youtube-upload-lush}"
RUNTIME_DIR="${RUNTIME_DIR:-/opt/youtube-upload-lush-runtime}"
REPO_URL="${REPO_URL:-https://github.com/lushmediadev/Youtube_Upload_Lush.git}"
BRANCH="${BRANCH:-main}"
DEPLOY_MODE="${DEPLOY_MODE:-systemd}"
SYSTEMD_SERVICE_NAME="${SYSTEMD_SERVICE_NAME:-youtube-upload-web.service}"
SYSTEMD_SERVICE_DESCRIPTION="${SYSTEMD_SERVICE_DESCRIPTION:-Youtube Upload Lush Web App}"
SERVICE_USER="${SERVICE_USER:-root}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/git_runtime_layout.sh"

render_systemd_service() {
  local service_unit_path="$1"
  local service_port="$2"

  {
    printf '%s\n' '[Unit]'
    printf 'Description=%s\n' "$SYSTEMD_SERVICE_DESCRIPTION"
    printf '%s\n' 'After=network-online.target'
    printf '%s\n' 'Wants=network-online.target'
    printf '\n'
    printf '%s\n' '[Service]'
    printf '%s\n' 'Type=simple'
    printf 'WorkingDirectory=%s\n' "$APP_DIR"
    printf 'ExecStart=%s/.venv/bin/python -m uvicorn backend.app.main:app --host 0.0.0.0 --port %s\n' "$APP_DIR" "$service_port"
    printf '%s\n' 'Restart=always'
    printf '%s\n' 'RestartSec=5'
    printf '%s\n' 'Environment=PYTHONUNBUFFERED=1'
    printf 'User=%s\n' "$SERVICE_USER"
    printf '\n'
    printf '%s\n' '[Install]'
    printf '%s\n' 'WantedBy=multi-user.target'
  } >"$service_unit_path"
}

mkdir -p "$RUNTIME_DIR"
mkdir -p "$RUNTIME_DIR/.backup" "$RUNTIME_DIR/backend-data"

install_base_packages
adopt_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".env" ".env"
adopt_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".venv" ".venv"
adopt_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".backup" ".backup"
adopt_runtime_path "$APP_DIR" "$RUNTIME_DIR" "backend/data" "backend-data"
ensure_git_checkout "$APP_DIR" "$REPO_URL" "$BRANCH"

rm -rf "$APP_DIR/.venv"
if [ ! -d "$RUNTIME_DIR/.venv" ]; then
  python3 -m venv "$RUNTIME_DIR/.venv"
fi

link_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".env" ".env"
link_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".venv" ".venv"
link_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".backup" ".backup"
link_runtime_path "$APP_DIR" "$RUNTIME_DIR" "backend/data" "backend-data"

cd "$APP_DIR"

if [ ! -f "$RUNTIME_DIR/.env" ]; then
  cp .env.example "$RUNTIME_DIR/.env"
  echo "Da tao $RUNTIME_DIR/.env, vui long cap nhat secret truoc khi start."
fi

set +u
. "$RUNTIME_DIR/.env"
set -u

SERVICE_PORT="${SERVICE_PORT:-${PORT:-8000}}"

if [ "${BROWSER_SESSION_ENABLED:-0}" = "1" ]; then
  apt-get update
  apt-get install -y xvfb openbox x11vnc websockify novnc chromium-browser || true
  if ! command -v chromium-browser >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1 && ! command -v google-chrome >/dev/null 2>&1; then
    if command -v snap >/dev/null 2>&1; then
      snap install chromium
    fi
  fi
fi

if [ "$DEPLOY_MODE" = "docker" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    apt-get update
    apt-get install -y docker.io docker-compose-v2
  fi

  if [ ! -f .env.production ]; then
    cp .env.production.example .env.production
    echo "Da tao .env.production, vui long cap nhat secret truoc khi start."
  fi

  docker compose -f infra/docker/host/docker-compose.yml up -d --build
  exit 0
fi

./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r backend/requirements.txt

render_systemd_service "/etc/systemd/system/$SYSTEMD_SERVICE_NAME" "$SERVICE_PORT"
systemctl daemon-reload
systemctl enable "$SYSTEMD_SERVICE_NAME"
systemctl restart "$SYSTEMD_SERVICE_NAME"
systemctl --no-pager --full status "$SYSTEMD_SERVICE_NAME" | sed -n '1,12p'
