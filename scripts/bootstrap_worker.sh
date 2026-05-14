#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/youtube-upload-lush}"
RUNTIME_DIR="${RUNTIME_DIR:-/opt/youtube-upload-lush-runtime}"
REPO_URL="${REPO_URL:-https://github.com/lushmediadev/Youtube_Upload_Lush.git}"
BRANCH="${BRANCH:-main}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/git_runtime_layout.sh"

export DEBIAN_FRONTEND=noninteractive

apt_get_retry() {
  local max_attempts="${APT_GET_MAX_ATTEMPTS:-20}"
  local retry_seconds="${APT_GET_RETRY_SECONDS:-6}"
  local attempt=1
  local exit_code=0

  while true; do
    if apt-get "$@"; then
      return 0
    fi
    exit_code=$?
    if [ "$attempt" -ge "$max_attempts" ]; then
      return "$exit_code"
    fi
    echo "[apt-retry] apt-get $* failed with exit code $exit_code. Retrying in ${retry_seconds}s (${attempt}/${max_attempts})..." >&2
    attempt=$((attempt + 1))
    sleep "$retry_seconds"
  done
}

APT_METADATA_READY=0

disable_unattended_apt_upgrades() {
  echo "[apt-auto-upgrade] Disabling unattended apt upgrades for worker stability."
  mkdir -p /etc/apt/apt.conf.d
  cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
EOF
  cat >/etc/apt/apt.conf.d/99disable-auto-upgrades <<'EOF'
APT::Periodic::Enable "0";
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::Unattended-Upgrade "0";
APT::Periodic::AutocleanInterval "0";
EOF
  if command -v systemctl >/dev/null 2>&1; then
    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
    systemctl disable --now unattended-upgrades.service >/dev/null 2>&1 || true
    systemctl mask apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service >/dev/null 2>&1 || true
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi
}

ensure_apt_metadata() {
  local force_refresh="${1:-0}"
  if [ "$force_refresh" != "1" ] && [ "$APT_METADATA_READY" -eq 1 ]; then
    return 0
  fi
  apt_get_retry update
  APT_METADATA_READY=1
}

package_is_installed() {
  local package_name="$1"
  dpkg-query -W -f='${Status}' "$package_name" 2>/dev/null | grep -q '^install ok installed$'
}

install_packages_if_missing() {
  local missing_packages=()
  local package_name
  for package_name in "$@"; do
    if ! package_is_installed "$package_name"; then
      missing_packages+=("$package_name")
    fi
  done
  if [ "${#missing_packages[@]}" -eq 0 ]; then
    return 0
  fi
  ensure_apt_metadata
  apt_get_retry install -y "${missing_packages[@]}"
}

remove_packages_if_installed() {
  local installed_packages=()
  local package_name
  for package_name in "$@"; do
    if package_is_installed "$package_name"; then
      installed_packages+=("$package_name")
    fi
  done
  if [ "${#installed_packages[@]}" -eq 0 ]; then
    return 0
  fi
  apt_get_retry remove -y "${installed_packages[@]}"
}

disable_unattended_apt_upgrades
mkdir -p "$RUNTIME_DIR"
install_packages_if_missing ffmpeg git python3 python3-venv python3-pip
mkdir -p "$RUNTIME_DIR/.backup" "$RUNTIME_DIR/worker-data"
adopt_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".venv" ".venv"
adopt_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".backup" ".backup"
adopt_runtime_path "$APP_DIR" "$RUNTIME_DIR" "worker-data" "worker-data"
ensure_git_checkout "$APP_DIR" "$REPO_URL" "$BRANCH"
link_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".venv" ".venv"
link_runtime_path "$APP_DIR" "$RUNTIME_DIR" ".backup" ".backup"
link_runtime_path "$APP_DIR" "$RUNTIME_DIR" "worker-data" "worker-data"

cd "$APP_DIR"

if [ ! -d "$RUNTIME_DIR/.venv" ]; then
  python3 -m venv "$RUNTIME_DIR/.venv"
fi
. .venv/bin/activate

requirements_hash_file="$RUNTIME_DIR/.worker-agent-requirements.sha256"
current_requirements_hash="$(sha256sum workers/agent/requirements.txt | awk '{print $1}')"
stored_requirements_hash="$(cat "$requirements_hash_file" 2>/dev/null || true)"
if [ "$current_requirements_hash" != "$stored_requirements_hash" ]; then
  python -m pip install --upgrade pip
  python -m pip install -r workers/agent/requirements.txt
  printf '%s\n' "$current_requirements_hash" >"$requirements_hash_file"
else
  echo "[python-setup] worker requirements unchanged, skipping pip install."
fi

cat >/etc/systemd/system/youtube-upload-worker.service <<EOF
[Unit]
Description=Youtube Upload Lush Worker Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/youtube-upload-worker.env
ExecStart=$APP_DIR/.venv/bin/python -m workers.agent.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

if [ ! -f /etc/youtube-upload-worker.env ]; then
  cat >/etc/youtube-upload-worker.env <<'EOF'
CONTROL_PLANE_URL=https://your-domain.example
WORKER_SHARED_SECRET=replace-with-a-long-random-worker-secret
WORKER_ID=worker-01
WORKER_NAME=worker-01
WORKER_RUNTIME_MODE=upload
WORKER_MANAGER=system
WORKER_GROUP=workers
WORKER_CAPACITY=1
WORKER_THREADS=1
WORKER_HEARTBEAT_SECONDS=15
WORKER_POLL_SECONDS=5
WORKER_API_JITTER_SECONDS=3
WORKER_LIVE_BUSY_CLAIM_SECONDS=10
APP_TIMEZONE=Asia/Ho_Chi_Minh
WORKER_SIMULATE_JOBS=false
WORKER_EXECUTE_JOBS=false
WORKER_SIMULATE_STEP_SECONDS=2.5
WORKER_UPLOAD_TO_YOUTUBE=false
WORKER_DATA_DIR=/opt/youtube-upload-lush-runtime/worker-data
WORKER_JANITOR_INTERVAL_SECONDS=3600
WORKER_LIVE_STREAM_RETENTION_HOURS=1
WORKER_LIVE_STATE_RETENTION_HOURS=168
WORKER_LIVE_NORMALIZE_ENABLED=false
WORKER_LIVE_NORMALIZE_CONCURRENCY=1
WORKER_LIVE_NORMALIZE_THREADS=2
WORKER_LIVE_NORMALIZE_PRESET=veryfast
WORKER_LIVE_NORMALIZE_MAX_HEIGHT=2160
WORKER_LIVE_NORMALIZE_1080_MAXRATE_KBPS=6000
WORKER_LIVE_NORMALIZE_1440_MAXRATE_KBPS=13000
WORKER_LIVE_NORMALIZE_2160_MAXRATE_KBPS=20000
WORKER_LIVE_NORMALIZE_1080_CRF=23
WORKER_LIVE_NORMALIZE_1440_CRF=22
WORKER_LIVE_NORMALIZE_2160_CRF=21
WORKER_LIVE_NORMALIZE_AUDIO_BITRATE_KBPS=128
WORKER_NETWORK_RETRY_BASE_SECONDS=3
WORKER_NETWORK_RETRY_MAX_SECONDS=30
WORKER_NETWORK_RETRY_JITTER_SECONDS=3
WORKER_NETWORK_RETRY_LOG_INTERVAL_SECONDS=60
WORKER_PROGRESS_RETRY_ATTEMPTS=3
YOUTUBE_UPLOAD_CHUNK_BYTES=8388608
BROWSER_SESSION_ENABLED=0
BROWSER_SESSION_PUBLIC_BASE_URL=http://worker-public-ip
BROWSER_SESSION_DISPLAY_BASE=90
BROWSER_SESSION_VNC_PORT_BASE=15900
BROWSER_SESSION_WEB_PORT_BASE=16080
BROWSER_SESSION_DEBUG_PORT_BASE=19220
BROWSER_SESSION_BIND_HOST=0.0.0.0
BROWSER_SESSION_START_URL=https://studio.youtube.com
BROWSER_SESSION_NOVNC_WEB_DIR=/usr/share/novnc
BROWSER_SESSION_CHROMIUM_BIN=google-chrome-stable
EOF
  echo "Da tao /etc/youtube-upload-worker.env, vui long cap nhat gia tri truoc khi start service."
fi

set -a
. /etc/youtube-upload-worker.env
set +a

if [ "${BROWSER_SESSION_ENABLED:-0}" = "1" ]; then
  install_packages_if_missing xvfb openbox x11vnc websockify novnc ca-certificates curl gnupg wget || true

  # ---------- Install Google Chrome Stable (.deb) ----------
  # Why Chrome Stable instead of apt chromium-browser?
  #   - On Ubuntu 22.04+ the apt "chromium-browser" is a snap wrapper.
  #   - Snap chromium ignores --user-data-dir and stores ALL state
  #     globally, breaking multi-user profile isolation.
  #   - Google Chrome Stable is a native .deb that fully respects
  #     --user-data-dir, making profile isolation trivial.

  _install_google_chrome() {
    # If google-chrome-stable already installed — nothing to do
    if command -v google-chrome-stable >/dev/null 2>&1; then
      echo "[browser-setup] google-chrome-stable already installed."
      return 0
    fi

    echo "[browser-setup] Installing Google Chrome Stable..."

    # Remove snap chromium if present (conflicts with profile isolation)
    if command -v snap >/dev/null 2>&1 && snap list chromium >/dev/null 2>&1; then
      echo "[browser-setup] Removing snap chromium..."
      snap remove --purge chromium 2>/dev/null || true
    fi
    remove_packages_if_installed chromium-browser 2>/dev/null || true

    # Add Google's official apt repo
    if [ ! -f /usr/share/keyrings/google-chrome.gpg ]; then
      wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg 2>/dev/null
    fi
    if [ ! -f /etc/apt/sources.list.d/google-chrome.list ]; then
      echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list
    fi
    APT_METADATA_READY=0
    ensure_apt_metadata 1
    install_packages_if_missing google-chrome-stable

    echo "[browser-setup] Google Chrome Stable installed successfully."
  }
  _install_google_chrome

  # ---------- Remove stale ChromeDriver ----------
  # We intentionally let Selenium Manager resolve the matching driver
  # at runtime so Google Chrome auto-updates do not leave an old
  # chromedriver binary behind.
  rm -f /usr/local/bin/chromedriver /usr/bin/chromedriver 2>/dev/null || true
  remove_packages_if_installed chromium-chromedriver chromium-driver google-chrome-driver 2>/dev/null || true

  # ---------- Update env file ----------
  # Point BROWSER_SESSION_CHROMIUM_BIN to google-chrome-stable
  if command -v google-chrome-stable >/dev/null 2>&1; then
    if [ -f /etc/youtube-upload-worker.env ]; then
      if grep -q "^BROWSER_SESSION_CHROMIUM_BIN=" /etc/youtube-upload-worker.env; then
        sed -i "s|^BROWSER_SESSION_CHROMIUM_BIN=.*|BROWSER_SESSION_CHROMIUM_BIN=google-chrome-stable|" /etc/youtube-upload-worker.env
      else
        echo "BROWSER_SESSION_CHROMIUM_BIN=google-chrome-stable" >> /etc/youtube-upload-worker.env
      fi
    fi
    BROWSER_SESSION_CHROMIUM_BIN=google-chrome-stable
  fi

  # ---------- Verify required binaries ----------
  for binary in Xvfb openbox x11vnc websockify; do
    if ! command -v "$binary" >/dev/null 2>&1; then
      echo "Missing required browser-session binary: $binary" >&2
      exit 1
    fi
  done
  if [ ! -d "${BROWSER_SESSION_NOVNC_WEB_DIR:-/usr/share/novnc}" ] || [ ! -f "${BROWSER_SESSION_NOVNC_WEB_DIR:-/usr/share/novnc}/vnc.html" ]; then
    echo "Missing noVNC web assets at ${BROWSER_SESSION_NOVNC_WEB_DIR:-/usr/share/novnc}" >&2
    exit 1
  fi
  CHROME_BIN="${BROWSER_SESSION_CHROMIUM_BIN:-google-chrome-stable}"
  if ! command -v "$CHROME_BIN" >/dev/null 2>&1; then
    echo "Missing Chrome/Chromium binary: $CHROME_BIN" >&2
    exit 1
  fi
  echo "[browser-setup] Using browser: $CHROME_BIN ($(${CHROME_BIN} --version 2>/dev/null || echo 'unknown'))"
fi

systemctl daemon-reload
systemctl enable youtube-upload-worker.service
systemctl restart youtube-upload-worker.service
