from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def _ensure_directory(path: Path) -> Path:
    if path.is_symlink():
        target = Path(os.readlink(path))
        if not target.is_absolute():
            target = (path.parent / target).resolve(strict=False)
        target.mkdir(parents=True, exist_ok=True)
        return path
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class WorkerConfig:
    runtime_mode: str
    control_plane_url: str
    shared_secret: str
    worker_id: str
    worker_name: str
    manager_name: str
    group: str
    capacity: int
    threads: int
    heartbeat_seconds: int
    poll_seconds: int
    simulate_jobs: bool
    execute_jobs: bool
    simulate_step_seconds: float
    work_root: Path
    keep_job_dirs: bool
    ffmpeg_bin: str
    ffprobe_bin: str
    youtube_upload_enabled: bool
    youtube_upload_chunk_bytes: int
    browser_public_base_url: str
    browser_session_enabled: bool
    browser_display_base: int
    browser_vnc_port_base: int
    browser_web_port_base: int
    browser_debug_port_base: int
    live_normalize_enabled: bool
    live_normalize_concurrency: int
    live_normalize_threads: int
    live_normalize_preset: str
    live_normalize_max_height: int
    live_normalize_1080_maxrate_kbps: int
    live_normalize_1440_maxrate_kbps: int
    live_normalize_2160_maxrate_kbps: int
    live_normalize_1080_crf: int
    live_normalize_1440_crf: int
    live_normalize_2160_crf: int
    live_normalize_audio_bitrate_kbps: int
    network_retry_base_seconds: float
    network_retry_max_seconds: float
    progress_retry_attempts: int
    network_retry_jitter_seconds: float = 3.0
    network_retry_log_interval_seconds: float = 60.0
    api_jitter_seconds: float = 3.0
    live_busy_claim_interval_seconds: float = 10.0


def load_config() -> WorkerConfig:
    hostname = socket.gethostname().split(".")[0]
    work_root = _ensure_directory(
        Path(os.getenv("WORKER_DATA_DIR", "/opt/youtube-upload-lush/worker-data")).expanduser()
    )
    return WorkerConfig(
        runtime_mode=os.getenv("WORKER_RUNTIME_MODE", "upload").strip().lower() or "upload",
        control_plane_url=_env("CONTROL_PLANE_URL").rstrip("/"),
        shared_secret=_env("WORKER_SHARED_SECRET"),
        worker_id=os.getenv("WORKER_ID", hostname).strip() or hostname,
        worker_name=os.getenv("WORKER_NAME", hostname).strip() or hostname,
        manager_name=os.getenv("WORKER_MANAGER", "system").strip() or "system",
        group=os.getenv("WORKER_GROUP", "workers").strip() or "workers",
        capacity=int(os.getenv("WORKER_CAPACITY", "1")),
        threads=int(os.getenv("WORKER_THREADS", "1")),
        heartbeat_seconds=int(os.getenv("WORKER_HEARTBEAT_SECONDS", "15")),
        poll_seconds=int(os.getenv("WORKER_POLL_SECONDS", "5")),
        simulate_jobs=os.getenv("WORKER_SIMULATE_JOBS", "false").strip().lower() in {"1", "true", "yes", "on"},
        execute_jobs=os.getenv("WORKER_EXECUTE_JOBS", "false").strip().lower() in {"1", "true", "yes", "on"},
        simulate_step_seconds=float(os.getenv("WORKER_SIMULATE_STEP_SECONDS", "2.5")),
        work_root=work_root,
        keep_job_dirs=os.getenv("WORKER_KEEP_JOB_DIRS", "false").strip().lower() in {"1", "true", "yes", "on"},
        ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg",
        ffprobe_bin=os.getenv("FFPROBE_BIN", "ffprobe").strip() or "ffprobe",
        youtube_upload_enabled=os.getenv("WORKER_UPLOAD_TO_YOUTUBE", "false").strip().lower() in {"1", "true", "yes", "on"},
        youtube_upload_chunk_bytes=int(os.getenv("YOUTUBE_UPLOAD_CHUNK_BYTES", "8388608")),
        browser_public_base_url=os.getenv("BROWSER_SESSION_PUBLIC_BASE_URL", "").strip().rstrip("/"),
        browser_session_enabled=os.getenv("BROWSER_SESSION_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        browser_display_base=int(os.getenv("BROWSER_SESSION_DISPLAY_BASE", "90")),
        browser_vnc_port_base=int(os.getenv("BROWSER_SESSION_VNC_PORT_BASE", "5990")),
        browser_web_port_base=int(os.getenv("BROWSER_SESSION_WEB_PORT_BASE", "6090")),
        browser_debug_port_base=int(os.getenv("BROWSER_SESSION_DEBUG_PORT_BASE", "9222")),
        live_normalize_enabled=os.getenv("WORKER_LIVE_NORMALIZE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        live_normalize_concurrency=max(1, int(os.getenv("WORKER_LIVE_NORMALIZE_CONCURRENCY", "1"))),
        live_normalize_threads=max(1, int(os.getenv("WORKER_LIVE_NORMALIZE_THREADS", "2"))),
        live_normalize_preset=os.getenv("WORKER_LIVE_NORMALIZE_PRESET", "veryfast").strip() or "veryfast",
        live_normalize_max_height=max(720, int(os.getenv("WORKER_LIVE_NORMALIZE_MAX_HEIGHT", "2160"))),
        live_normalize_1080_maxrate_kbps=max(1000, int(os.getenv("WORKER_LIVE_NORMALIZE_1080_MAXRATE_KBPS", "6000"))),
        live_normalize_1440_maxrate_kbps=max(2000, int(os.getenv("WORKER_LIVE_NORMALIZE_1440_MAXRATE_KBPS", "13000"))),
        live_normalize_2160_maxrate_kbps=max(4000, int(os.getenv("WORKER_LIVE_NORMALIZE_2160_MAXRATE_KBPS", "20000"))),
        live_normalize_1080_crf=max(16, int(os.getenv("WORKER_LIVE_NORMALIZE_1080_CRF", "23"))),
        live_normalize_1440_crf=max(16, int(os.getenv("WORKER_LIVE_NORMALIZE_1440_CRF", "22"))),
        live_normalize_2160_crf=max(16, int(os.getenv("WORKER_LIVE_NORMALIZE_2160_CRF", "21"))),
        live_normalize_audio_bitrate_kbps=max(64, int(os.getenv("WORKER_LIVE_NORMALIZE_AUDIO_BITRATE_KBPS", "128"))),
        network_retry_base_seconds=float(os.getenv("WORKER_NETWORK_RETRY_BASE_SECONDS", "3")),
        network_retry_max_seconds=float(os.getenv("WORKER_NETWORK_RETRY_MAX_SECONDS", "30")),
        progress_retry_attempts=max(1, int(os.getenv("WORKER_PROGRESS_RETRY_ATTEMPTS", "3"))),
        network_retry_jitter_seconds=max(0.0, float(os.getenv("WORKER_NETWORK_RETRY_JITTER_SECONDS", "3"))),
        network_retry_log_interval_seconds=max(1.0, float(os.getenv("WORKER_NETWORK_RETRY_LOG_INTERVAL_SECONDS", "60"))),
        api_jitter_seconds=max(0.0, float(os.getenv("WORKER_API_JITTER_SECONDS", "3"))),
        live_busy_claim_interval_seconds=max(5.0, float(os.getenv("WORKER_LIVE_BUSY_CLAIM_SECONDS", "10"))),
    )
