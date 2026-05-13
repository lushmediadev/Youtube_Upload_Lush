from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import WorkerConfig


_last_bandwidth_sample: tuple[float, int] | None = None
_last_cpu_sample: tuple[int, int] | None = None
_RETRYABLE_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def _load_percent() -> int:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as handle:
            first_line = handle.readline().strip()
    except OSError:
        return 0

    if not first_line.startswith("cpu "):
        return 0

    parts = first_line.split()[1:]
    if len(parts) < 4:
        return 0

    try:
        counters = [int(value) for value in parts]
    except ValueError:
        return 0

    idle_ticks = counters[3] + (counters[4] if len(counters) > 4 else 0)
    total_ticks = sum(counters)

    global _last_cpu_sample
    previous_sample = _last_cpu_sample
    _last_cpu_sample = (total_ticks, idle_ticks)
    if previous_sample is None:
        return 0

    previous_total, previous_idle = previous_sample
    total_delta = total_ticks - previous_total
    idle_delta = idle_ticks - previous_idle
    if total_delta <= 0:
        return 0

    busy_ratio = 1 - (max(0, idle_delta) / total_delta)
    return max(0, min(100, int(round(busy_ratio * 100))))


def _disk_usage() -> tuple[float, float]:
    usage = shutil.disk_usage("/")
    total_gb = round(usage.total / (1024 ** 3), 2)
    used_gb = round((usage.total - usage.free) / (1024 ** 3), 2)
    return used_gb, total_gb


def _memory_usage() -> tuple[float, float, int]:
    mem_total_kb = 0
    mem_available_kb = 0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_available_kb = int(line.split()[1])
    except (OSError, ValueError):
        return 0.0, 0.0, 0

    if mem_total_kb <= 0:
        return 0.0, 0.0, 0

    used_kb = max(0, mem_total_kb - mem_available_kb)
    total_gb = round(mem_total_kb / (1024 ** 2), 2)
    used_gb = round(used_kb / (1024 ** 2), 2)
    percent = max(0, min(100, int(round((used_kb / mem_total_kb) * 100))))
    return used_gb, total_gb, percent


def _network_total_bytes() -> int:
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as handle:
            lines = handle.readlines()[2:]
    except OSError:
        return 0

    total_bytes = 0
    for raw_line in lines:
        if ":" not in raw_line:
            continue
        interface_name, counters = raw_line.split(":", 1)
        iface = interface_name.strip()
        if iface == "lo":
            continue
        parts = counters.split()
        if len(parts) < 16:
            continue
        try:
            received_bytes = int(parts[0])
            transmitted_bytes = int(parts[8])
        except ValueError:
            continue
        total_bytes += received_bytes + transmitted_bytes
    return total_bytes


def _bandwidth_kbps() -> float:
    global _last_bandwidth_sample

    now = time.monotonic()
    total_bytes = _network_total_bytes()
    if total_bytes <= 0:
        _last_bandwidth_sample = (now, max(total_bytes, 0))
        return 0.0

    previous_sample = _last_bandwidth_sample
    _last_bandwidth_sample = (now, total_bytes)
    if previous_sample is None:
        return 0.0

    previous_time, previous_total = previous_sample
    elapsed_seconds = now - previous_time
    if elapsed_seconds <= 0:
        return 0.0

    delta_bytes = max(0, total_bytes - previous_total)
    return round((delta_bytes / elapsed_seconds) / 1024, 2)


def worker_auth_headers(config: WorkerConfig) -> dict[str, str]:
    return {
        "x-worker-id": config.worker_id,
        "x-worker-secret": config.shared_secret,
    }


def _persist_worker_limits(config: WorkerConfig) -> None:
    env_path = Path(os.getenv("WORKER_ENV_FILE", "/etc/youtube-upload-worker.env")).expanduser()
    try:
        original_lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    replacements = {
        "WORKER_THREADS": str(max(1, int(config.threads or 1))),
        "WORKER_CAPACITY": str(max(1, int(config.capacity or config.threads or 1))),
    }
    seen: set[str] = set()
    updated_lines: list[str] = []
    changed = False
    for line in original_lines:
        key, separator, _value = line.partition("=")
        normalized_key = key.strip()
        if separator and normalized_key in replacements:
            updated_line = f"{normalized_key}={replacements[normalized_key]}"
            updated_lines.append(updated_line)
            seen.add(normalized_key)
            changed = changed or updated_line != line
            continue
        updated_lines.append(line)
    for key, value in replacements.items():
        if key in seen:
            continue
        updated_lines.append(f"{key}={value}")
        changed = True
    if not changed:
        return
    try:
        env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    except OSError:
        return


def _sync_worker_limits_from_response(config: WorkerConfig, payload: dict[str, Any]) -> None:
    worker = payload.get("worker") or {}
    previous_threads = max(1, int(config.threads or 1))
    previous_capacity = max(1, int(config.capacity or config.threads or 1))
    try:
        resolved_threads = max(1, int(worker.get("threads") or config.threads or 1))
    except (TypeError, ValueError):
        resolved_threads = max(1, int(config.threads or 1))
    try:
        resolved_capacity = max(1, int(worker.get("capacity") or resolved_threads))
    except (TypeError, ValueError):
        resolved_capacity = max(1, resolved_threads)
    config.threads = resolved_threads
    config.capacity = resolved_capacity
    if resolved_threads != previous_threads or resolved_capacity != previous_capacity:
        _persist_worker_limits(config)


def _is_retryable_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, httpx.RequestError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return response is not None and response.status_code in _RETRYABLE_STATUS_CODES
    return False


def is_worker_missing_error(exc: Exception) -> bool:
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    response = exc.response
    request = exc.request
    if response is None or request is None:
        return False
    request_url = str(request.url)
    return response.status_code == 404 and ("/api/workers/" in request_url or "/api/live-workers/" in request_url)


def is_worker_deleted_error(exc: Exception) -> bool:
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    response = exc.response
    request = exc.request
    if response is None or request is None:
        return False
    request_url = str(request.url)
    if response.status_code != 403 or ("/api/workers/" not in request_url and "/api/live-workers/" not in request_url):
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    detail = str(payload.get("detail") or "").strip().lower()
    return "đã bị xoá khỏi hệ thống" in detail or "da bi xoa khoi he thong" in detail


def _retry_delay_seconds(config: WorkerConfig, attempt: int) -> float:
    capped_attempt = max(0, min(attempt - 1, 6))
    delay = config.network_retry_base_seconds * (2 ** capped_attempt)
    return max(config.network_retry_base_seconds, min(config.network_retry_max_seconds, delay))


def _request_with_retry(
    client: httpx.Client,
    config: WorkerConfig,
    method: str,
    url: str,
    *,
    operation: str,
    retry_forever: bool = True,
    max_attempts: int | None = None,
    **kwargs: Any,
) -> httpx.Response:
    attempt = 0
    while True:
        attempt += 1
        try:
            request_kwargs = dict(kwargs)
            request_kwargs.setdefault("follow_redirects", True)
            response = client.request(method, url, **request_kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:
            if not _is_retryable_error(exc):
                raise
            if not retry_forever and max_attempts is not None and attempt >= max_attempts:
                raise
            delay_seconds = _retry_delay_seconds(config, attempt)
            print(
                f"[control_plane] {operation} failed (attempt {attempt}): {exc}. "
                f"retrying in {delay_seconds:.1f}s",
                flush=True,
            )
            time.sleep(delay_seconds)


@dataclass
class YouTubeUploadTarget:
    job_id: str
    channel_id: str
    channel_name: str
    title: str
    description: str | None
    connection_mode: str = "browser_profile"
    browser_profile_key: str | None = None
    browser_profile_path: str | None = None


@dataclass
class BrowserSessionAssignment:
    session_id: str
    purpose: str
    status: str
    profile_key: str
    display_number: int
    vnc_port: int
    web_port: int
    debug_port: int
    access_password: str
    current_url: str | None
    current_title: str | None
    detected_channel_id: str | None
    detected_channel_name: str | None
    novnc_url: str | None
    start_url: str | None


@dataclass
class BrowserProfileCleanupAssignment:
    profile_key: str


@dataclass
class LiveStreamRuntimeState:
    stream_id: str
    status: str
    should_stop: bool
    playback_mode: str = "stream"
    stop_requested_at: str | None = None
    ended_at: str | None = None
    updated_at: str | None = None


@dataclass
class DecommissionAssignment:
    operation_id: str
    app_dir: str
    runtime_dir: str


class LiveStreamStoppedError(RuntimeError):
    def __init__(self, stream_id: str, status: str, message: str | None = None) -> None:
        self.stream_id = str(stream_id)
        self.status = str(status or "").strip().lower() or "stopped"
        resolved_message = (message or "").strip() or f"Luồng live {self.stream_id} đã dừng trên control-plane ({self.status})."
        super().__init__(resolved_message)


def register_worker(client: httpx.Client, config: WorkerConfig) -> None:
    _, total_gb = _disk_usage()
    _request_with_retry(
        client,
        config,
        "POST",
        "/api/workers/register",
        operation="register_worker",
        json={
            "worker_id": config.worker_id,
            "name": config.worker_name,
            "shared_secret": config.shared_secret,
            "manager_name": config.manager_name,
            "group": config.group,
            "capacity": config.capacity,
            "threads": config.threads,
            "disk_total_gb": total_gb,
            "public_base_url": config.browser_public_base_url,
            "browser_session_enabled": config.browser_session_enabled,
            "browser_display_base": config.browser_display_base,
            "browser_vnc_port_base": config.browser_vnc_port_base,
            "browser_web_port_base": config.browser_web_port_base,
            "browser_debug_port_base": config.browser_debug_port_base,
        },
    )


def heartbeat_worker(
    client: httpx.Client,
    config: WorkerConfig,
    *,
    active_job_ids: list[str] | None = None,
) -> None:
    used_gb, total_gb = _disk_usage()
    ram_used_gb, ram_total_gb, ram_percent = _memory_usage()
    _request_with_retry(
        client,
        config,
        "POST",
        "/api/workers/heartbeat",
        operation="heartbeat_worker",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "load_percent": _load_percent(),
            "ram_percent": ram_percent,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "bandwidth_kbps": _bandwidth_kbps(),
            "disk_used_gb": used_gb,
            "disk_total_gb": total_gb,
            "threads": config.threads,
            "status": "online",
            "public_base_url": config.browser_public_base_url,
            "browser_session_enabled": config.browser_session_enabled,
            "browser_display_base": config.browser_display_base,
            "browser_vnc_port_base": config.browser_vnc_port_base,
            "browser_web_port_base": config.browser_web_port_base,
            "browser_debug_port_base": config.browser_debug_port_base,
            "active_job_ids": active_job_ids or [],
        },
    )


def register_live_worker(client: httpx.Client, config: WorkerConfig) -> None:
    _, total_gb = _disk_usage()
    response = _request_with_retry(
        client,
        config,
        "POST",
        "/api/live-workers/register",
        operation="register_live_worker",
        json={
            "worker_id": config.worker_id,
            "name": config.worker_name,
            "shared_secret": config.shared_secret,
            "manager_name": config.manager_name,
            "group": config.group,
            "capacity": config.capacity,
            "threads": config.threads,
            "disk_total_gb": total_gb,
        },
    )
    _sync_worker_limits_from_response(config, response.json())


def heartbeat_live_worker(
    client: httpx.Client,
    config: WorkerConfig,
    *,
    active_stream_ids: list[str] | None = None,
) -> None:
    used_gb, total_gb = _disk_usage()
    ram_used_gb, ram_total_gb, ram_percent = _memory_usage()
    response = _request_with_retry(
        client,
        config,
        "POST",
        "/api/live-workers/heartbeat",
        operation="heartbeat_live_worker",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "load_percent": _load_percent(),
            "ram_percent": ram_percent,
            "ram_used_gb": ram_used_gb,
            "ram_total_gb": ram_total_gb,
            "bandwidth_kbps": _bandwidth_kbps(),
            "disk_used_gb": used_gb,
            "disk_total_gb": total_gb,
            "threads": config.threads,
            "status": "online",
            "active_stream_ids": active_stream_ids or [],
        },
    )
    _sync_worker_limits_from_response(config, response.json())


def poll_browser_sessions(
    client: httpx.Client,
    config: WorkerConfig,
) -> tuple[list[BrowserSessionAssignment], list[BrowserProfileCleanupAssignment]]:
    response = _request_with_retry(
        client,
        config,
        "POST",
        "/api/workers/browser-sessions/poll",
        operation="poll_browser_sessions",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
        },
    )
    payload = response.json()
    sessions: list[BrowserSessionAssignment] = []
    for item in payload.get("sessions") or []:
        sessions.append(
            BrowserSessionAssignment(
                session_id=str(item["session_id"]),
                purpose=str(item.get("purpose") or "channel_connect"),
                status=str(item["status"]),
                profile_key=str(item["profile_key"]),
                display_number=int(item["display_number"]),
                vnc_port=int(item["vnc_port"]),
                web_port=int(item["web_port"]),
                debug_port=int(item["debug_port"]),
                access_password=str(item["access_password"]),
                current_url=item.get("current_url"),
                current_title=item.get("current_title"),
                detected_channel_id=item.get("detected_channel_id"),
                detected_channel_name=item.get("detected_channel_name"),
                novnc_url=item.get("novnc_url"),
                start_url=item.get("start_url"),
            )
        )
    cleanup_profiles = [
        BrowserProfileCleanupAssignment(profile_key=str(item.get("profile_key") or "").strip())
        for item in (payload.get("cleanup_profiles") or [])
        if str(item.get("profile_key") or "").strip()
    ]
    return sessions, cleanup_profiles


def ack_browser_profile_cleanup(
    client: httpx.Client,
    config: WorkerConfig,
    profile_keys: list[str],
) -> None:
    if not profile_keys:
        return
    _request_with_retry(
        client,
        config,
        "POST",
        "/api/workers/browser-profiles/cleanup-ack",
        operation="ack_browser_profile_cleanup",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "profile_keys": profile_keys,
        },
    )


def poll_decommission_task(
    client: httpx.Client,
    config: WorkerConfig,
) -> DecommissionAssignment | None:
    response = _request_with_retry(
        client,
        config,
        "POST",
        "/api/workers/decommission/poll",
        operation="poll_decommission_task",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
        },
    )
    payload = response.json()
    task = payload.get("task") or {}
    operation_id = str(task.get("id") or "").strip()
    if not operation_id:
        return None
    return DecommissionAssignment(
        operation_id=operation_id,
        app_dir=str(task.get("app_dir") or "/opt/youtube-upload-lush").strip() or "/opt/youtube-upload-lush",
        runtime_dir=str(task.get("runtime_dir") or "/opt/youtube-upload-lush-runtime").strip() or "/opt/youtube-upload-lush-runtime",
    )


def complete_decommission_task(
    client: httpx.Client,
    config: WorkerConfig,
    operation_id: str,
    *,
    status: str,
    message: str | None = None,
) -> None:
    _request_with_retry(
        client,
        config,
        "POST",
        f"/api/workers/decommission/{operation_id}/complete",
        operation=f"complete_decommission_task:{operation_id}",
        retry_forever=False,
        max_attempts=max(1, config.progress_retry_attempts),
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "status": status,
            "message": message,
        },
    )


def sync_browser_session(
    client: httpx.Client,
    config: WorkerConfig,
    session_id: str,
    *,
    status: str,
    novnc_url: str | None = None,
    current_url: str | None = None,
    current_title: str | None = None,
    detected_channel_id: str | None = None,
    detected_channel_name: str | None = None,
    last_error: str | None = None,
    profile_path: str | None = None,
    session_path: str | None = None,
    password_file: str | None = None,
    xvfb_pid: int | None = None,
    openbox_pid: int | None = None,
    chromium_pid: int | None = None,
    x11vnc_pid: int | None = None,
    websockify_pid: int | None = None,
) -> None:
    _request_with_retry(
        client,
        config,
        "POST",
        f"/api/workers/browser-sessions/{session_id}/sync",
        operation=f"sync_browser_session:{session_id}",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "status": status,
            "novnc_url": novnc_url,
            "current_url": current_url,
            "current_title": current_title,
            "detected_channel_id": detected_channel_id,
            "detected_channel_name": detected_channel_name,
            "last_error": last_error,
            "profile_path": profile_path,
            "session_path": session_path,
            "password_file": password_file,
            "xvfb_pid": xvfb_pid,
            "openbox_pid": openbox_pid,
            "chromium_pid": chromium_pid,
            "x11vnc_pid": x11vnc_pid,
            "websockify_pid": websockify_pid,
        },
    )


def claim_job(client: httpx.Client, config: WorkerConfig) -> dict[str, Any] | None:
    response = _request_with_retry(
        client,
        config,
        "POST",
        "/api/workers/claim",
        operation="claim_job",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
        },
    )
    payload = response.json()
    return payload.get("job")


def claim_live_stream(client: httpx.Client, config: WorkerConfig) -> dict[str, Any] | None:
    response = _request_with_retry(
        client,
        config,
        "POST",
        "/api/live-workers/claim",
        operation="claim_live_stream",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
        },
    )
    payload = response.json()
    return payload.get("stream")


def update_job_progress(
    client: httpx.Client,
    config: WorkerConfig,
    job_id: str,
    *,
    status: str,
    progress: int,
    message: str | None = None,
) -> None:
    _request_with_retry(
        client,
        config,
        "POST",
        f"/api/workers/jobs/{job_id}/progress",
        operation=f"update_job_progress:{job_id}",
        retry_forever=False,
        max_attempts=config.progress_retry_attempts,
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "status": status,
            "progress": progress,
            "message": message,
        },
    )


def update_live_stream_progress(
    client: httpx.Client,
    config: WorkerConfig,
    stream_id: str,
    *,
    status: str,
    progress: int,
    message: str | None = None,
) -> None:
    _request_with_retry(
        client,
        config,
        "POST",
        f"/api/live-workers/streams/{stream_id}/progress",
        operation=f"update_live_stream_progress:{stream_id}",
        retry_forever=False,
        max_attempts=config.progress_retry_attempts,
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "status": status,
            "progress": progress,
            "message": message,
        },
    )


def get_live_stream_runtime_state(
    client: httpx.Client,
    config: WorkerConfig,
    stream_id: str,
) -> LiveStreamRuntimeState:
    response = _request_with_retry(
        client,
        config,
        "GET",
        f"/api/live-workers/streams/{stream_id}",
        operation=f"get_live_stream_runtime_state:{stream_id}",
        retry_forever=False,
        max_attempts=1,
        headers=worker_auth_headers(config),
    )
    payload = response.json()
    return LiveStreamRuntimeState(
        stream_id=str(payload.get("stream_id") or stream_id),
        status=str(payload.get("status") or "scheduled"),
        should_stop=bool(payload.get("should_stop")),
        playback_mode=str(payload.get("playback_mode") or "stream"),
        stop_requested_at=payload.get("stop_requested_at"),
        ended_at=payload.get("ended_at"),
        updated_at=payload.get("updated_at"),
    )


def upload_job_thumbnail(
    client: httpx.Client,
    config: WorkerConfig,
    job_id: str,
    *,
    file_name: str,
    payload: bytes,
    content_type: str = "image/jpeg",
) -> None:
    _request_with_retry(
        client,
        config,
        "POST",
        f"/api/workers/jobs/{job_id}/thumbnail",
        operation=f"upload_job_thumbnail:{job_id}",
        headers={
            **worker_auth_headers(config),
            "x-file-name": file_name,
            "content-type": content_type,
        },
        content=payload,
    )


def complete_job(
    client: httpx.Client,
    config: WorkerConfig,
    job_id: str,
    *,
    output_url: str | None = None,
    message: str | None = None,
) -> None:
    _request_with_retry(
        client,
        config,
        "POST",
        f"/api/workers/jobs/{job_id}/complete",
        operation=f"complete_job:{job_id}",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "output_url": output_url,
            "message": message,
        },
    )


def complete_live_stream(
    client: httpx.Client,
    config: WorkerConfig,
    stream_id: str,
    *,
    message: str | None = None,
) -> None:
    _request_with_retry(
        client,
        config,
        "POST",
        f"/api/live-workers/streams/{stream_id}/complete",
        operation=f"complete_live_stream:{stream_id}",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "message": message,
        },
    )


def fail_job(client: httpx.Client, config: WorkerConfig, job_id: str, *, message: str) -> None:
    _request_with_retry(
        client,
        config,
        "POST",
        f"/api/workers/jobs/{job_id}/fail",
        operation=f"fail_job:{job_id}",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "message": message,
        },
    )


def fail_live_stream(client: httpx.Client, config: WorkerConfig, stream_id: str, *, message: str) -> None:
    _request_with_retry(
        client,
        config,
        "POST",
        f"/api/live-workers/streams/{stream_id}/fail",
        operation=f"fail_live_stream:{stream_id}",
        json={
            "worker_id": config.worker_id,
            "shared_secret": config.shared_secret,
            "message": message,
        },
    )


def get_job_youtube_target(client: httpx.Client, config: WorkerConfig, job_id: str) -> YouTubeUploadTarget:
    response = _request_with_retry(
        client,
        config,
        "GET",
        f"/api/workers/jobs/{job_id}/youtube-target",
        operation=f"get_job_youtube_target:{job_id}",
        headers=worker_auth_headers(config),
    )
    payload = response.json()
    return YouTubeUploadTarget(
        job_id=str(payload["job_id"]),
        channel_id=str(payload["channel_id"]),
        channel_name=str(payload["channel_name"]),
        title=str(payload["title"]),
        description=payload.get("description"),
        connection_mode=str(payload.get("connection_mode") or "browser_profile"),
        browser_profile_key=payload.get("browser_profile_key"),
        browser_profile_path=payload.get("browser_profile_path"),
    )
