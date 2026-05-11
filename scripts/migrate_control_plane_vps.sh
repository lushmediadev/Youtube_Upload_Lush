#!/usr/bin/env bash
set -euo pipefail

# Run this script on the NEW control-plane VPS. It installs a fresh Git checkout,
# copies runtime state from the SOURCE VPS, then starts the web service.
# It never writes to the source host.

SOURCE_HOST="${SOURCE_HOST:-}"
SOURCE_USER="${SOURCE_USER:-root}"
SOURCE_SSH_PORT="${SOURCE_SSH_PORT:-22}"
SOURCE_APP_DIR="${SOURCE_APP_DIR:-/opt/youtube-upload-lush}"
SOURCE_RUNTIME_DIR="${SOURCE_RUNTIME_DIR:-/opt/youtube-upload-lush-runtime}"

APP_DIR="${APP_DIR:-/opt/youtube-upload-lush}"
RUNTIME_DIR="${RUNTIME_DIR:-/opt/youtube-upload-lush-runtime}"
REPO_URL="${REPO_URL:-https://github.com/lushmediadev/Youtube_Upload_Lush.git}"
BRANCH="${BRANCH:-main}"
SYSTEMD_SERVICE_NAME="${SYSTEMD_SERVICE_NAME:-youtube-upload-web.service}"
SERVICE_PORT="${SERVICE_PORT:-8000}"
SERVICE_USER="${SERVICE_USER:-root}"
START_APP="${START_APP:-1}"
SKIP_ENV="${SKIP_ENV:-0}"
SKIP_BACKEND_DATA="${SKIP_BACKEND_DATA:-0}"
SKIP_WORKER_DATA="${SKIP_WORKER_DATA:-1}"
SOURCE_SSH_OPTS="${SOURCE_SSH_OPTS:-}"

timestamp="$(date +%Y%m%d%H%M%S)"

usage() {
  cat <<'USAGE'
Usage:
  SOURCE_HOST=OLD_VPS_IP bash scripts/migrate_control_plane_vps.sh

Optional env:
  SOURCE_USER=root
  SOURCE_SSH_PORT=22
  SOURCE_APP_DIR=/opt/youtube-upload-lush
  SOURCE_RUNTIME_DIR=/opt/youtube-upload-lush-runtime
  APP_DIR=/opt/youtube-upload-lush
  RUNTIME_DIR=/opt/youtube-upload-lush-runtime
  REPO_URL=https://github.com/lushmediadev/Youtube_Upload_Lush.git
  BRANCH=main
  SYSTEMD_SERVICE_NAME=youtube-upload-web.service
  SERVICE_PORT=8000
  SKIP_ENV=1
  SKIP_BACKEND_DATA=1
  SKIP_WORKER_DATA=0
  START_APP=0
  SOURCE_SSH_OPTS="-i /root/.ssh/source_vps_key"

The target VPS must be able to SSH into SOURCE_HOST. Prefer SSH keys.
USAGE
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

if [ -z "$SOURCE_HOST" ]; then
  echo "ERROR: SOURCE_HOST is required." >&2
  usage >&2
  exit 2
fi

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing command: $1" >&2
    exit 2
  }
}

install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y git rsync openssh-client python3 python3-venv python3-pip sqlite3
}

ssh_source() {
  # shellcheck disable=SC2086
  ssh -p "$SOURCE_SSH_PORT" -o StrictHostKeyChecking=accept-new $SOURCE_SSH_OPTS "$SOURCE_USER@$SOURCE_HOST" "$@"
}

rsync_from_source() {
  local source_path="$1"
  local target_path="$2"
  local ssh_cmd
  ssh_cmd="ssh -p $SOURCE_SSH_PORT -o StrictHostKeyChecking=accept-new $SOURCE_SSH_OPTS"
  rsync -az --partial --info=progress2 -e "$ssh_cmd" "$SOURCE_USER@$SOURCE_HOST:$source_path" "$target_path"
}

ensure_git_checkout() {
  mkdir -p "$(dirname "$APP_DIR")"
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" fetch --prune origin "$BRANCH"
    git -C "$APP_DIR" checkout "$BRANCH"
    git -C "$APP_DIR" reset --hard "origin/$BRANCH"
    return
  fi

  if [ -e "$APP_DIR" ]; then
    local backup_dir="${APP_DIR}.pre-migrate-${timestamp}"
    echo "Backing up existing non-git app dir to $backup_dir"
    mv "$APP_DIR" "$backup_dir"
  fi

  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
}

backup_target_runtime() {
  if [ -e "$RUNTIME_DIR" ]; then
    mkdir -p "$RUNTIME_DIR/.backup"
    tar -C "$(dirname "$RUNTIME_DIR")" -czf "$RUNTIME_DIR/.backup/runtime-before-migrate-$timestamp.tgz" "$(basename "$RUNTIME_DIR")" \
      --exclude "$(basename "$RUNTIME_DIR")/.venv" \
      --exclude "$(basename "$RUNTIME_DIR")/.backup" || true
  fi
}

copy_runtime() {
  mkdir -p "$RUNTIME_DIR" "$RUNTIME_DIR/backend-data" "$RUNTIME_DIR/.backup"

  if [ "$SKIP_ENV" != "1" ]; then
    if ssh_source "test -f '$SOURCE_RUNTIME_DIR/.env'"; then
      if [ -f "$RUNTIME_DIR/.env" ]; then
        cp -a "$RUNTIME_DIR/.env" "$RUNTIME_DIR/.env.bak-$timestamp"
      fi
      rsync_from_source "$SOURCE_RUNTIME_DIR/.env" "$RUNTIME_DIR/.env"
    elif ssh_source "test -f '$SOURCE_APP_DIR/.env'"; then
      if [ -f "$RUNTIME_DIR/.env" ]; then
        cp -a "$RUNTIME_DIR/.env" "$RUNTIME_DIR/.env.bak-$timestamp"
      fi
      rsync_from_source "$SOURCE_APP_DIR/.env" "$RUNTIME_DIR/.env"
    else
      echo "WARN: source .env not found; bootstrap will create an example .env if needed."
    fi
  fi

  if [ "$SKIP_BACKEND_DATA" != "1" ]; then
    if ssh_source "test -d '$SOURCE_RUNTIME_DIR/backend-data'"; then
      rsync_from_source "$SOURCE_RUNTIME_DIR/backend-data/" "$RUNTIME_DIR/backend-data/"
    elif ssh_source "test -d '$SOURCE_APP_DIR/backend/data'"; then
      rsync_from_source "$SOURCE_APP_DIR/backend/data/" "$RUNTIME_DIR/backend-data/"
    else
      echo "WARN: source backend data not found; target DB will be initialized from code defaults."
    fi
  fi

  if [ "$SKIP_WORKER_DATA" = "0" ]; then
    if ssh_source "test -d '$SOURCE_RUNTIME_DIR/worker-data'"; then
      mkdir -p "$RUNTIME_DIR/worker-data"
      rsync_from_source "$SOURCE_RUNTIME_DIR/worker-data/" "$RUNTIME_DIR/worker-data/"
    else
      echo "WARN: source worker-data not found; skipped."
    fi
  fi
}

start_control_plane() {
  APP_DIR="$APP_DIR" \
  RUNTIME_DIR="$RUNTIME_DIR" \
  REPO_URL="$REPO_URL" \
  BRANCH="$BRANCH" \
  SYSTEMD_SERVICE_NAME="$SYSTEMD_SERVICE_NAME" \
  SERVICE_PORT="$SERVICE_PORT" \
  SERVICE_USER="$SERVICE_USER" \
  bash "$APP_DIR/scripts/bootstrap_host.sh"
}

verify_control_plane() {
  systemctl is-active "$SYSTEMD_SERVICE_NAME"
  if [ -f "$RUNTIME_DIR/backend-data/app_state.db" ]; then
    sqlite3 "$RUNTIME_DIR/backend-data/app_state.db" 'PRAGMA integrity_check;'
  fi
  python3 - <<PY
from urllib.request import urlopen
print(urlopen("http://127.0.0.1:${SERVICE_PORT}/api/health", timeout=10).read().decode())
PY
}

install_packages
need_cmd git
need_cmd rsync
need_cmd ssh
need_cmd sqlite3

ensure_git_checkout
backup_target_runtime
copy_runtime

if [ "$START_APP" = "1" ]; then
  start_control_plane
  verify_control_plane
else
  echo "START_APP=0, skipped service start."
fi

echo "Done. App: $APP_DIR"
echo "Runtime: $RUNTIME_DIR"
