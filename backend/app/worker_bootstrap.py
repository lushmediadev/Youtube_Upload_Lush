from __future__ import annotations

import io
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock, Thread, current_thread
from typing import Callable

try:
    import paramiko
except ImportError:  # pragma: no cover - runtime dependency on deploy host
    paramiko = None

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT_DIR / "scripts"
BOOTSTRAP_SCRIPT_PATH = SCRIPT_DIR / "bootstrap_worker.sh"
GIT_LAYOUT_SCRIPT_PATH = SCRIPT_DIR / "git_runtime_layout.sh"
DECOMMISSION_SCRIPT_PATH = SCRIPT_DIR / "decommission_worker.sh"
load_dotenv(ROOT_DIR / ".env")
DEFAULT_REPO_URL = "https://github.com/lushmediadev/Youtube_Upload_Lush.git"
DEFAULT_BRANCH = "main"
DEFAULT_APP_DIR = "/opt/youtube-upload-lush"
DEFAULT_RUNTIME_DIR = "/opt/youtube-upload-lush-runtime"
DEFAULT_LIVE_APP_DIR = "/opt/youtube-upload-lush-live-worker"
DEFAULT_LIVE_RUNTIME_DIR = "/opt/youtube-upload-lush-live-worker-runtime"
logger = logging.getLogger(__name__)
_OPERATION_THREADS: dict[str, Thread] = {}
_OPERATION_THREADS_LOCK = Lock()


class WorkerBootstrapError(RuntimeError):
    pass


def _normalize_runtime_mode(value: str | None) -> str:
    return "live" if str(value or "").strip().lower() == "live" else "upload"


def _resolve_default_app_dir(runtime_mode: str = "upload") -> str:
    normalized_runtime_mode = _normalize_runtime_mode(runtime_mode)
    if normalized_runtime_mode == "live":
        return (
            str(
                os.getenv(
                    "WORKER_BOOTSTRAP_LIVE_APP_DIR",
                    os.getenv("WORKER_BOOTSTRAP_APP_DIR", DEFAULT_LIVE_APP_DIR),
                )
            ).strip()
            or DEFAULT_LIVE_APP_DIR
        )
    return (
        str(
            os.getenv(
                "WORKER_BOOTSTRAP_UPLOAD_APP_DIR",
                os.getenv("WORKER_BOOTSTRAP_APP_DIR", DEFAULT_APP_DIR),
            )
        ).strip()
        or DEFAULT_APP_DIR
    )


def _resolve_default_runtime_dir(runtime_mode: str = "upload") -> str:
    normalized_runtime_mode = _normalize_runtime_mode(runtime_mode)
    if normalized_runtime_mode == "live":
        return (
            str(
                os.getenv(
                    "WORKER_BOOTSTRAP_LIVE_RUNTIME_DIR",
                    os.getenv("WORKER_BOOTSTRAP_RUNTIME_DIR", DEFAULT_LIVE_RUNTIME_DIR),
                )
            ).strip()
            or DEFAULT_LIVE_RUNTIME_DIR
        )
    return (
        str(
            os.getenv(
                "WORKER_BOOTSTRAP_UPLOAD_RUNTIME_DIR",
                os.getenv("WORKER_BOOTSTRAP_RUNTIME_DIR", DEFAULT_RUNTIME_DIR),
            )
        ).strip()
        or DEFAULT_RUNTIME_DIR
    )


@dataclass(slots=True)
class WorkerBootstrapRequest:
    vps_ip: str
    ssh_user: str
    control_plane_url: str
    shared_secret: str
    worker_id: str
    worker_name: str
    runtime_mode: str = "upload"
    manager_name: str = "system"
    group: str = ""
    password: str | None = None
    ssh_private_key: str | None = None
    repo_url: str = DEFAULT_REPO_URL
    branch: str = DEFAULT_BRANCH
    app_dir: str = field(default_factory=lambda: _resolve_default_app_dir("upload"))
    runtime_dir: str = field(default_factory=lambda: _resolve_default_runtime_dir("upload"))
    browser_public_base_url: str | None = None
    capacity: int = 1
    threads: int = 1
    heartbeat_seconds: int = 15
    poll_seconds: int = 5
    connect_timeout: int = 20


@dataclass(slots=True)
class WorkerBootstrapResult:
    worker_id: str
    worker_name: str
    vps_ip: str
    service_enabled: str
    service_active: str


@dataclass(slots=True)
class WorkerDecommissionRequest:
    vps_ip: str
    ssh_user: str
    password: str | None = None
    ssh_private_key: str | None = None
    app_dir: str = field(default_factory=lambda: _resolve_default_app_dir("upload"))
    runtime_dir: str = field(default_factory=lambda: _resolve_default_runtime_dir("upload"))
    connect_timeout: int = 20


@dataclass(slots=True)
class WorkerDecommissionResult:
    vps_ip: str
    service_enabled: str
    service_active: str


def _worker_ssh_short_timeout_seconds() -> int:
    raw_value = str(os.getenv("WORKER_SSH_SHORT_TIMEOUT_SECONDS", "60")).strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = 60
    return max(15, value)


def _worker_bootstrap_command_timeout_seconds() -> int:
    raw_value = str(os.getenv("WORKER_BOOTSTRAP_COMMAND_TIMEOUT_SECONDS", "1800")).strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = 1800
    return max(300, value)


def _worker_decommission_command_timeout_seconds() -> int:
    raw_value = str(os.getenv("WORKER_DECOMMISSION_COMMAND_TIMEOUT_SECONDS", "900")).strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = 900
    return max(180, value)


def _worker_install_max_concurrency() -> int:
    raw_value = str(os.getenv("WORKER_INSTALL_MAX_CONCURRENCY", "4")).strip()
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = 4
    return max(1, value)


def suggest_next_worker_id(
    existing_worker_ids: list[str] | tuple[str, ...] | set[str],
    *,
    prefix: str = "worker",
) -> str:
    normalized_prefix = str(prefix or "").strip()
    if not normalized_prefix:
        raise ValueError("worker_id prefix is required")

    used_suffixes: set[int] = set()
    pattern = re.compile(rf"{re.escape(normalized_prefix)}-(\d+)", flags=re.IGNORECASE)
    for raw_value in existing_worker_ids:
        match = pattern.fullmatch(str(raw_value or "").strip())
        if not match:
            continue
        suffix = int(match.group(1))
        if suffix > 0:
            used_suffixes.add(suffix)

    next_suffix = 1
    while next_suffix in used_suffixes:
        next_suffix += 1
    return f"{normalized_prefix}-{next_suffix:02d}"


def build_worker_bootstrap_control_plane_url(fallback_url: str | None = None) -> str:
    configured = str(os.getenv("WORKER_BOOTSTRAP_CONTROL_PLANE_URL", "")).strip()
    if configured:
        return configured.rstrip("/")
    fallback = str(fallback_url or "").strip()
    if fallback:
        return fallback.rstrip("/")
    raise WorkerBootstrapError("Chưa có WORKER_BOOTSTRAP_CONTROL_PLANE_URL để bootstrap worker.")


def normalize_ssh_user(value: str | None, *, default: str = "root") -> str:
    raw_value = str(value or "").strip()
    normalized_default = str(default or "root").strip() or "root"
    if not raw_value:
        return normalized_default
    if raw_value.lower().startswith("ssh "):
        parts = raw_value.split()
        raw_value = parts[-1] if parts else ""
    if "@" in raw_value:
        raw_value = raw_value.split("@", 1)[0]
    raw_value = raw_value.strip()
    return raw_value or normalized_default


def build_worker_bootstrap_request(
    *,
    vps_ip: str,
    ssh_user: str,
    shared_secret: str,
    control_plane_url: str,
    worker_id: str,
    manager_name: str,
    password: str | None = None,
    ssh_private_key: str | None = None,
    repo_url: str | None = None,
    branch: str | None = None,
    runtime_mode: str = "upload",
) -> WorkerBootstrapRequest:
    normalized_runtime_mode = _normalize_runtime_mode(runtime_mode)
    normalized_ip = str(vps_ip or "").strip()
    if not normalized_ip:
        raise WorkerBootstrapError("VPS IP là bắt buộc.")
    normalized_user = normalize_ssh_user(ssh_user)
    normalized_key = str(ssh_private_key or "").strip()
    normalized_password = str(password or "").strip()
    if not normalized_key and not normalized_password:
        raise WorkerBootstrapError("Cần nhập password hoặc SSH key để bootstrap worker.")
    return WorkerBootstrapRequest(
        vps_ip=normalized_ip,
        ssh_user=normalized_user,
        password=normalized_password or None,
        ssh_private_key=normalized_key or None,
        control_plane_url=build_worker_bootstrap_control_plane_url(control_plane_url),
        shared_secret=str(shared_secret or "").strip(),
        worker_id=str(worker_id or "").strip(),
        worker_name=normalized_ip,
        runtime_mode=normalized_runtime_mode,
        manager_name=str(manager_name or "").strip() or "system",
        group="",
        repo_url=str(repo_url or os.getenv("WORKER_BOOTSTRAP_REPO_URL", DEFAULT_REPO_URL)).strip() or DEFAULT_REPO_URL,
        branch=str(branch or os.getenv("WORKER_BOOTSTRAP_BRANCH", DEFAULT_BRANCH)).strip() or DEFAULT_BRANCH,
        app_dir=_resolve_default_app_dir(normalized_runtime_mode),
        runtime_dir=_resolve_default_runtime_dir(normalized_runtime_mode),
        browser_public_base_url=f"http://{normalized_ip}",
    )


def build_worker_decommission_request(
    *,
    vps_ip: str,
    ssh_user: str,
    password: str | None = None,
    ssh_private_key: str | None = None,
    runtime_mode: str = "upload",
) -> WorkerDecommissionRequest:
    normalized_runtime_mode = _normalize_runtime_mode(runtime_mode)
    normalized_ip = str(vps_ip or "").strip()
    if not normalized_ip:
        raise WorkerBootstrapError("VPS IP là bắt buộc.")
    normalized_user = normalize_ssh_user(ssh_user)
    normalized_key = str(ssh_private_key or "").strip()
    normalized_password = str(password or "").strip()
    if not normalized_key and not normalized_password:
        raise WorkerBootstrapError("Cần nhập password hoặc SSH key để gỡ worker khỏi VPS.")
    return WorkerDecommissionRequest(
        vps_ip=normalized_ip,
        ssh_user=normalized_user,
        password=normalized_password or None,
        ssh_private_key=normalized_key or None,
        app_dir=_resolve_default_app_dir(normalized_runtime_mode),
        runtime_dir=_resolve_default_runtime_dir(normalized_runtime_mode),
    )


def _ensure_runtime_dependency() -> None:
    if paramiko is None:
        raise WorkerBootstrapError("Thiếu dependency `paramiko`. Hãy cài lại backend requirements trước.")
    if not BOOTSTRAP_SCRIPT_PATH.exists():
        raise WorkerBootstrapError(f"Không tìm thấy script bootstrap: {BOOTSTRAP_SCRIPT_PATH}")
    if not GIT_LAYOUT_SCRIPT_PATH.exists():
        raise WorkerBootstrapError(f"Không tìm thấy script runtime layout: {GIT_LAYOUT_SCRIPT_PATH}")
    if not DECOMMISSION_SCRIPT_PATH.exists():
        raise WorkerBootstrapError(f"Không tìm thấy script decommission: {DECOMMISSION_SCRIPT_PATH}")


def _load_private_key(raw_key: str):
    key_text = str(raw_key or "").strip()
    if not key_text:
        raise WorkerBootstrapError("SSH key rỗng.")
    last_error: Exception | None = None
    for key_type in (
        getattr(paramiko, "Ed25519Key", None),
        getattr(paramiko, "RSAKey", None),
        getattr(paramiko, "ECDSAKey", None),
        getattr(paramiko, "DSSKey", None),
    ):
        if key_type is None:
            continue
        try:
            return key_type.from_private_key(io.StringIO(key_text))
        except Exception as exc:  # pragma: no cover - parser branch depends on key type
            last_error = exc
    raise WorkerBootstrapError(f"SSH key không hợp lệ: {last_error or 'unknown error'}")


def _connect_client(request: WorkerBootstrapRequest | WorkerDecommissionRequest):
    _ensure_runtime_dependency()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {
        "hostname": request.vps_ip,
        "username": request.ssh_user,
        "timeout": request.connect_timeout,
        "banner_timeout": request.connect_timeout,
        "auth_timeout": request.connect_timeout,
        "look_for_keys": False,
        "allow_agent": False,
    }
    if request.ssh_private_key:
        connect_kwargs["pkey"] = _load_private_key(request.ssh_private_key)
    else:
        connect_kwargs["password"] = request.password
    try:
        client.connect(**connect_kwargs)
        return client
    except paramiko.AuthenticationException as exc:
        client.close()
        if request.ssh_private_key:
            raise WorkerBootstrapError(
                f"Đăng nhập SSH vào VPS {request.vps_ip} thất bại. SSH key đang lưu trong hệ thống có thể không còn đúng với máy thật."
            ) from exc
        raise WorkerBootstrapError(
            f"Đăng nhập SSH vào VPS {request.vps_ip} thất bại. Password VPS đang lưu trong hệ thống có thể không còn đúng với máy thật. Hãy cập nhật lại trường Password VPS rồi thử lại."
        ) from exc
    except Exception as exc:  # pragma: no cover - network/runtime path
        client.close()
        raise WorkerBootstrapError(f"Không kết nối được VPS {request.vps_ip}: {exc}") from exc


def _tail_text(value: str, *, max_lines: int = 24, max_chars: int = 2400) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    lines = normalized.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    clipped = "\n".join(lines).strip()
    if len(clipped) > max_chars:
        clipped = clipped[-max_chars:].strip()
    return clipped


def _remote_command_error_message(command: str, stdout_text: str, stderr_text: str) -> str:
    snippet = _tail_text(stderr_text) or _tail_text(stdout_text)
    if snippet:
        return snippet
    return f"Remote command failed: {command}"


def _run_remote_command(
    client,
    command: str,
    *,
    timeout_seconds: int | None = None,
) -> tuple[int, str, str]:
    effective_timeout = int(timeout_seconds or _worker_ssh_short_timeout_seconds())
    stdin, stdout, stderr = client.exec_command(command)
    channel = stdout.channel
    stdout_chunks = bytearray()
    stderr_chunks = bytearray()
    deadline = time.monotonic() + effective_timeout if effective_timeout > 0 else None

    while True:
        while channel.recv_ready():
            stdout_chunks.extend(channel.recv(4096))
        while channel.recv_stderr_ready():
            stderr_chunks.extend(channel.recv_stderr(4096))
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break
        if deadline is not None and time.monotonic() >= deadline:
            channel.close()
            stdout_text = stdout_chunks.decode("utf-8", errors="replace")
            stderr_text = stderr_chunks.decode("utf-8", errors="replace")
            raise WorkerBootstrapError(
                f"Lệnh từ xa chạy quá {effective_timeout}s.\n{_remote_command_error_message(command, stdout_text, stderr_text)}"
            )
        time.sleep(0.2)

    exit_code = channel.recv_exit_status()
    stdout_text = stdout_chunks.decode("utf-8", errors="replace")
    stderr_text = stderr_chunks.decode("utf-8", errors="replace")
    return exit_code, stdout_text, stderr_text


def _upload_remote_script(sftp, local_path: Path, remote_path: str) -> None:
    content = local_path.read_bytes().replace(b"\r\n", b"\n")
    with sftp.file(remote_path, "wb") as remote_file:
        remote_file.write(content)


def _build_worker_env_file(request: WorkerBootstrapRequest) -> str:
    browser_public_base_url = str(request.browser_public_base_url or f"http://{request.vps_ip}").rstrip("/")
    runtime_mode = str(request.runtime_mode or "upload").strip().lower() or "upload"
    runtime_dir = str(request.runtime_dir or _resolve_default_runtime_dir()).strip().rstrip("/") or _resolve_default_runtime_dir()
    worker_data_dir = f"{runtime_dir}/worker-data"
    browser_session_enabled = runtime_mode != "live"
    youtube_upload_enabled = runtime_mode != "live"
    janitor_interval_seconds = 600 if runtime_mode == "live" else 3600
    live_stream_retention_hours = 1 if runtime_mode == "live" else 6
    live_state_retention_hours = os.getenv("WORKER_LIVE_STATE_RETENTION_HOURS", "168").strip() or "168"
    return "\n".join(
        [
            f"CONTROL_PLANE_URL={request.control_plane_url}",
            f"WORKER_SHARED_SECRET={request.shared_secret}",
            f"WORKER_ID={request.worker_id}",
            f"WORKER_NAME={request.worker_name}",
            f"WORKER_RUNTIME_MODE={request.runtime_mode}",
            f"WORKER_MANAGER={request.manager_name}",
            "WORKER_GROUP=",
            f"WORKER_CAPACITY={request.capacity}",
            f"WORKER_THREADS={request.threads}",
            f"WORKER_HEARTBEAT_SECONDS={request.heartbeat_seconds}",
            f"WORKER_POLL_SECONDS={request.poll_seconds}",
            "WORKER_BROWSER_SESSION_POLL_SECONDS=15",
            "WORKER_DECOMMISSION_POLL_SECONDS=60",
            "APP_TIMEZONE=Asia/Ho_Chi_Minh",
            "WORKER_SIMULATE_JOBS=false",
            "WORKER_EXECUTE_JOBS=true",
            "WORKER_SIMULATE_STEP_SECONDS=2.5",
            f"WORKER_UPLOAD_TO_YOUTUBE={'true' if youtube_upload_enabled else 'false'}",
            f"WORKER_DATA_DIR={worker_data_dir}",
            "WORKER_KEEP_JOB_DIRS=false",
            f"WORKER_JANITOR_INTERVAL_SECONDS={janitor_interval_seconds}",
            f"WORKER_LIVE_STREAM_RETENTION_HOURS={live_stream_retention_hours}",
            f"WORKER_LIVE_STATE_RETENTION_HOURS={live_state_retention_hours}",
            f"WORKER_LIVE_NORMALIZE_ENABLED={os.getenv('WORKER_LIVE_NORMALIZE_ENABLED', 'false').strip() or 'false'}",
            f"WORKER_LIVE_NORMALIZE_CONCURRENCY={os.getenv('WORKER_LIVE_NORMALIZE_CONCURRENCY', '1').strip() or '1'}",
            f"WORKER_LIVE_NORMALIZE_THREADS={os.getenv('WORKER_LIVE_NORMALIZE_THREADS', '2').strip() or '2'}",
            f"WORKER_LIVE_NORMALIZE_PRESET={os.getenv('WORKER_LIVE_NORMALIZE_PRESET', 'veryfast').strip() or 'veryfast'}",
            f"WORKER_LIVE_NORMALIZE_MAX_HEIGHT={os.getenv('WORKER_LIVE_NORMALIZE_MAX_HEIGHT', '2160').strip() or '2160'}",
            f"WORKER_LIVE_NORMALIZE_1080_MAXRATE_KBPS={os.getenv('WORKER_LIVE_NORMALIZE_1080_MAXRATE_KBPS', '6000').strip() or '6000'}",
            f"WORKER_LIVE_NORMALIZE_1440_MAXRATE_KBPS={os.getenv('WORKER_LIVE_NORMALIZE_1440_MAXRATE_KBPS', '13000').strip() or '13000'}",
            f"WORKER_LIVE_NORMALIZE_2160_MAXRATE_KBPS={os.getenv('WORKER_LIVE_NORMALIZE_2160_MAXRATE_KBPS', '20000').strip() or '20000'}",
            f"WORKER_LIVE_NORMALIZE_1080_CRF={os.getenv('WORKER_LIVE_NORMALIZE_1080_CRF', '23').strip() or '23'}",
            f"WORKER_LIVE_NORMALIZE_1440_CRF={os.getenv('WORKER_LIVE_NORMALIZE_1440_CRF', '22').strip() or '22'}",
            f"WORKER_LIVE_NORMALIZE_2160_CRF={os.getenv('WORKER_LIVE_NORMALIZE_2160_CRF', '21').strip() or '21'}",
            f"WORKER_LIVE_NORMALIZE_AUDIO_BITRATE_KBPS={os.getenv('WORKER_LIVE_NORMALIZE_AUDIO_BITRATE_KBPS', '128').strip() or '128'}",
            "YOUTUBE_UPLOAD_CHUNK_BYTES=8388608",
            f"BROWSER_SESSION_ENABLED={1 if browser_session_enabled else 0}",
            f"BROWSER_SESSION_PUBLIC_BASE_URL={browser_public_base_url}",
            "BROWSER_SESSION_DISPLAY_BASE=90",
            "BROWSER_SESSION_VNC_PORT_BASE=15900",
            "BROWSER_SESSION_WEB_PORT_BASE=16080",
            "BROWSER_SESSION_DEBUG_PORT_BASE=19220",
            "BROWSER_SESSION_BIND_HOST=0.0.0.0",
            "BROWSER_SESSION_START_URL=https://studio.youtube.com",
            "BROWSER_SESSION_NOVNC_WEB_DIR=/usr/share/novnc",
            "BROWSER_SESSION_CHROMIUM_BIN=google-chrome-stable",
            "WORKER_NETWORK_RETRY_BASE_SECONDS=3",
            "WORKER_NETWORK_RETRY_MAX_SECONDS=30",
            "WORKER_NETWORK_RETRY_JITTER_SECONDS=3",
            "WORKER_NETWORK_RETRY_LOG_INTERVAL_SECONDS=60",
            "WORKER_PROGRESS_RETRY_ATTEMPTS=3",
            "WORKER_API_JITTER_SECONDS=3",
            "WORKER_LIVE_BUSY_CLAIM_SECONDS=10",
            "",
        ]
    )


def bootstrap_worker_via_ssh(
    request: WorkerBootstrapRequest,
    *,
    progress: Callable[[str], None] | None = None,
) -> WorkerBootstrapResult:
    if not request.shared_secret:
        raise WorkerBootstrapError("Worker shared secret đang rỗng.")
    client = _connect_client(request)
    temp_dir = ""
    try:
        if progress:
            progress(f"Đang kết nối SSH tới VPS {request.vps_ip}...")
        exit_code, stdout, stderr = _run_remote_command(
            client,
            "mktemp -d /tmp/youtube-worker-bootstrap-XXXXXX",
            timeout_seconds=_worker_ssh_short_timeout_seconds(),
        )
        if exit_code != 0:
            raise WorkerBootstrapError(stderr.strip() or stdout.strip() or "Không tạo được thư mục tạm trên VPS.")
        temp_dir = stdout.strip()
        if not temp_dir:
            raise WorkerBootstrapError("Không nhận được đường dẫn thư mục tạm trên VPS.")

        sftp = client.open_sftp()
        try:
            if progress:
                progress("Đang tải script cài đặt và cấu hình worker lên VPS...")
            remote_bootstrap_script = f"{temp_dir}/bootstrap_worker.sh"
            remote_layout_script = f"{temp_dir}/git_runtime_layout.sh"
            remote_env_file = f"{temp_dir}/youtube-upload-worker.env"
            _upload_remote_script(sftp, BOOTSTRAP_SCRIPT_PATH, remote_bootstrap_script)
            _upload_remote_script(sftp, GIT_LAYOUT_SCRIPT_PATH, remote_layout_script)
            with sftp.file(remote_env_file, "w") as remote_file:
                remote_file.write(_build_worker_env_file(request))
        finally:
            sftp.close()

        sudo_prefix = "" if request.ssh_user == "root" else "sudo "
        chmod_command = "chmod 755 {bootstrap} {layout}".format(
            bootstrap=shlex.quote(remote_bootstrap_script),
            layout=shlex.quote(remote_layout_script),
        )
        install_env_command = "{sudo}install -m 600 {src} /etc/youtube-upload-worker.env".format(
            sudo=sudo_prefix,
            src=shlex.quote(remote_env_file),
        )
        bootstrap_command = (
            "{sudo}env APP_DIR={app_dir} RUNTIME_DIR={runtime_dir} REPO_URL={repo_url} BRANCH={branch} "
            "bash {script}"
        ).format(
            sudo=sudo_prefix,
            app_dir=shlex.quote(request.app_dir),
            runtime_dir=shlex.quote(request.runtime_dir),
            repo_url=shlex.quote(request.repo_url),
            branch=shlex.quote(request.branch),
            script=shlex.quote(remote_bootstrap_script),
        )
        steps = (
            (chmod_command, "Đang chuẩn hóa quyền thực thi script trên VPS...", _worker_ssh_short_timeout_seconds()),
            (install_env_command, "Đang ghi file cấu hình worker vào hệ thống...", _worker_ssh_short_timeout_seconds()),
            (bootstrap_command, "Đang clone repo, cài dependency và bật worker service...", _worker_bootstrap_command_timeout_seconds()),
        )
        for command, step_message, timeout_seconds in steps:
            if progress:
                progress(step_message)
            step_name = "bootstrap"
            if command == chmod_command:
                step_name = "chmod"
            elif command == install_env_command:
                step_name = "install-env"
            step_started_at = time.monotonic()
            exit_code, stdout, stderr = _run_remote_command(client, command, timeout_seconds=timeout_seconds)
            logger.info(
                "worker_bootstrap_step_completed worker_id=%s vps_ip=%s step=%s duration_seconds=%.2f exit_code=%s",
                str(request.worker_id or "").strip(),
                str(request.vps_ip or "").strip(),
                step_name,
                time.monotonic() - step_started_at,
                exit_code,
            )
            if exit_code != 0:
                raise WorkerBootstrapError(_remote_command_error_message(command, stdout, stderr))

        if progress:
            progress("Đang kiểm tra trạng thái systemd của worker...")
        _, enabled_stdout, _ = _run_remote_command(
            client,
            f"{sudo_prefix}systemctl is-enabled youtube-upload-worker.service",
            timeout_seconds=_worker_ssh_short_timeout_seconds(),
        )
        _, active_stdout, active_stderr = _run_remote_command(
            client,
            f"{sudo_prefix}systemctl is-active youtube-upload-worker.service",
            timeout_seconds=_worker_ssh_short_timeout_seconds(),
        )
        service_enabled = enabled_stdout.strip() or "unknown"
        service_active = active_stdout.strip() or active_stderr.strip() or "unknown"
        if service_active != "active":
            _, status_stdout, status_stderr = _run_remote_command(
                client,
                f"{sudo_prefix}systemctl status youtube-upload-worker.service --no-pager -n 40",
                timeout_seconds=_worker_ssh_short_timeout_seconds(),
            )
            raise WorkerBootstrapError(
                status_stderr.strip() or status_stdout.strip() or "Worker service không active sau bootstrap."
            )
        return WorkerBootstrapResult(
            worker_id=request.worker_id,
            worker_name=request.worker_name,
            vps_ip=request.vps_ip,
            service_enabled=service_enabled,
            service_active=service_active,
        )
    finally:
        if temp_dir:
            try:
                _run_remote_command(client, f"rm -rf {shlex.quote(temp_dir)}", timeout_seconds=_worker_ssh_short_timeout_seconds())
            except Exception:
                logger.debug("bootstrap_tempdir_cleanup_failed vps_ip=%s temp_dir=%s", request.vps_ip, temp_dir, exc_info=True)
        client.close()


def decommission_worker_via_ssh(
    request: WorkerDecommissionRequest,
    *,
    progress: Callable[[str], None] | None = None,
) -> WorkerDecommissionResult:
    client = _connect_client(request)
    temp_dir = ""
    try:
        if progress:
            progress(f"Đang kết nối SSH tới VPS {request.vps_ip}...")
        exit_code, stdout, stderr = _run_remote_command(
            client,
            "mktemp -d /tmp/youtube-worker-decommission-XXXXXX",
            timeout_seconds=_worker_ssh_short_timeout_seconds(),
        )
        if exit_code != 0:
            raise WorkerBootstrapError(stderr.strip() or stdout.strip() or "Không tạo được thư mục tạm trên VPS.")
        temp_dir = stdout.strip()
        if not temp_dir:
            raise WorkerBootstrapError("Không nhận được đường dẫn thư mục tạm trên VPS.")

        remote_script = f"{temp_dir}/decommission_worker.sh"
        sftp = client.open_sftp()
        try:
            if progress:
                progress("Đang tải script gỡ worker lên VPS...")
            _upload_remote_script(sftp, DECOMMISSION_SCRIPT_PATH, remote_script)
        finally:
            sftp.close()

        sudo_prefix = "" if request.ssh_user == "root" else "sudo "
        chmod_command = f"chmod 755 {shlex.quote(remote_script)}"
        decommission_command = "{sudo}env APP_DIR={app_dir} RUNTIME_DIR={runtime_dir} bash {script}".format(
            sudo=sudo_prefix,
            app_dir=shlex.quote(request.app_dir),
            runtime_dir=shlex.quote(request.runtime_dir),
            script=shlex.quote(remote_script),
        )
        steps = (
            (chmod_command, "Đang chuẩn hóa quyền thực thi script gỡ BOT...", _worker_ssh_short_timeout_seconds()),
            (decommission_command, "Đang dừng service và dọn app worker khỏi VPS...", _worker_decommission_command_timeout_seconds()),
        )
        for command, step_message, timeout_seconds in steps:
            if progress:
                progress(step_message)
            exit_code, stdout, stderr = _run_remote_command(client, command, timeout_seconds=timeout_seconds)
            if exit_code != 0:
                raise WorkerBootstrapError(_remote_command_error_message(command, stdout, stderr))

        if progress:
            progress("Đang xác nhận worker service đã được gỡ khỏi hệ thống...")
        enabled_code, enabled_stdout, enabled_stderr = _run_remote_command(
            client,
            f"{sudo_prefix}systemctl is-enabled youtube-upload-worker.service",
            timeout_seconds=_worker_ssh_short_timeout_seconds(),
        )
        active_code, active_stdout, active_stderr = _run_remote_command(
            client,
            f"{sudo_prefix}systemctl is-active youtube-upload-worker.service",
            timeout_seconds=_worker_ssh_short_timeout_seconds(),
        )
        service_enabled = (enabled_stdout.strip() or enabled_stderr.strip() or "not-found") if enabled_code != 0 else (
            enabled_stdout.strip() or "unknown"
        )
        service_active = (active_stdout.strip() or active_stderr.strip() or "inactive") if active_code != 0 else (
            active_stdout.strip() or "unknown"
        )
        return WorkerDecommissionResult(
            vps_ip=request.vps_ip,
            service_enabled=service_enabled,
            service_active=service_active,
        )
    finally:
        if temp_dir:
            try:
                _run_remote_command(client, f"rm -rf {shlex.quote(temp_dir)}", timeout_seconds=_worker_ssh_short_timeout_seconds())
            except Exception:
                logger.debug("decommission_tempdir_cleanup_failed vps_ip=%s temp_dir=%s", request.vps_ip, temp_dir, exc_info=True)
        client.close()


def _run_worker_install_operation_legacy_unused(store, operation_id: str, request: WorkerBootstrapRequest) -> None:
    raise RuntimeError("_run_worker_install_operation_legacy_unused should never be called")
    try:
        def report(message: str) -> None:
            store.update_worker_operation(operation_id, status="running", message=message)

        report(f"Đang kết nối SSH tới {request.vps_ip} và chuẩn bị cài worker...")
        result = bootstrap_worker_via_ssh(request, progress=report)
        store.update_worker_operation(
            operation_id,
            status="awaiting_registration",
            message=(
                f"Đã cài worker service trên {result.vps_ip}. Đang chờ BOT "
                "kết nối lại với control-plane..."
            ),
        )
    except Exception as exc:
        logger.exception(
            "worker_install_operation_failed operation_id=%s worker_id=%s vps_ip=%s",
            str(operation_id or "").strip(),
            str(request.worker_id or "").strip(),
            str(request.vps_ip or "").strip(),
        )
        store.fail_worker_operation(operation_id, message=str(exc))


def _run_worker_install_operation(store, operation_id: str, request: WorkerBootstrapRequest) -> None:
    try:
        def report(message: str) -> None:
            store.update_worker_operation(operation_id, status="running", message=message)

        task_snapshot = next(
            (
                item
                for item in store.get_worker_operation_snapshots()
                if str(item.get("id") or "").strip() == str(operation_id or "").strip()
            ),
            {},
        )
        if str(task_snapshot.get("operation_reason") or "").strip() == "workspace_conversion":
            source_workspace_mode = str(task_snapshot.get("conversion_source_workspace_mode") or "").strip() or "upload"
            decommission_request = build_worker_decommission_request(
                vps_ip=request.vps_ip,
                ssh_user=request.ssh_user,
                password=request.password,
                ssh_private_key=request.ssh_private_key,
                runtime_mode=source_workspace_mode,
            )
            report(f"Đang gỡ workspace {source_workspace_mode} cũ trên {request.vps_ip} trước khi cài lại BOT...")
            decommission_worker_via_ssh(decommission_request, progress=report)

        report(f"Đang kết nối SSH tới {request.vps_ip} và chuẩn bị cài worker...")
        result = bootstrap_worker_via_ssh(request, progress=report)
        try:
            store.update_worker_operation(
                operation_id,
                status="awaiting_registration",
                message=(
                    f"Đã cài worker service trên {result.vps_ip}. Đang chờ BOT "
                    "kết nối lại với control-plane..."
                ),
            )
        except KeyError:
            try:
                store._find_workspace_worker(request.worker_id, workspace_mode=request.runtime_mode)
            except KeyError:
                raise
            logger.info(
                "worker_install_operation_already_completed operation_id=%s worker_id=%s vps_ip=%s workspace_mode=%s",
                str(operation_id or "").strip(),
                str(request.worker_id or "").strip(),
                str(result.vps_ip or request.vps_ip or "").strip(),
                str(request.runtime_mode or "").strip() or "upload",
            )
    except Exception as exc:
        logger.exception(
            "worker_install_operation_failed operation_id=%s worker_id=%s vps_ip=%s",
            str(operation_id or "").strip(),
            str(request.worker_id or "").strip(),
            str(request.vps_ip or "").strip(),
        )
        store.fail_worker_operation(operation_id, message=str(exc))


def _run_worker_decommission_operation(
    store,
    operation_id: str,
    worker_id: str,
    request: WorkerDecommissionRequest,
) -> None:
    def should_delete_local_record_after_failure(exc: Exception) -> bool:
        message = str(exc or "").strip()
        return message.startswith(f"Không kết nối được VPS {request.vps_ip}:")

    try:
        def report(message: str) -> None:
            store.update_worker_operation(operation_id, status="running", message=message)

        report(f"Đang kết nối SSH tới {request.vps_ip} và chuẩn bị gỡ BOT...")
        decommission_worker_via_ssh(request, progress=report)
        store.finalize_decommissioned_bot(worker_id, operation_id)
    except WorkerBootstrapError as exc:
        if should_delete_local_record_after_failure(exc):
            logger.warning(
                "worker_decommission_unreachable_local_delete operation_id=%s worker_id=%s vps_ip=%s error=%s",
                str(operation_id or "").strip(),
                str(worker_id or "").strip(),
                str(request.vps_ip or "").strip(),
                str(exc),
            )
            store.update_worker_operation(
                operation_id,
                status="running",
                message=(
                    f"Không kết nối được VPS {request.vps_ip}. VPS có thể đã hết hạn hoặc không còn truy cập được; "
                    "control-plane sẽ xoá bản ghi BOT local."
                ),
            )
            store.finalize_decommissioned_bot(worker_id, operation_id, reason_override="ssh_unreachable")
            return
        logger.exception(
            "worker_decommission_operation_failed operation_id=%s worker_id=%s vps_ip=%s",
            str(operation_id or "").strip(),
            str(worker_id or "").strip(),
            str(request.vps_ip or "").strip(),
        )
        store.fail_worker_operation(operation_id, message=str(exc))
    except Exception as exc:
        logger.exception(
            "worker_decommission_operation_failed operation_id=%s worker_id=%s vps_ip=%s",
            str(operation_id or "").strip(),
            str(worker_id or "").strip(),
            str(request.vps_ip or "").strip(),
        )
        store.fail_worker_operation(operation_id, message=str(exc))


def _run_tracked_operation(operation_id: str, target: Callable[..., None], *args) -> None:
    try:
        target(*args)
    finally:
        with _OPERATION_THREADS_LOCK:
            current = _OPERATION_THREADS.get(operation_id)
            if current is current_thread():
                _OPERATION_THREADS.pop(operation_id, None)


def _start_operation_thread(
    operation_id: str,
    *,
    name: str,
    target: Callable[..., None],
    args: tuple,
) -> Thread:
    with _OPERATION_THREADS_LOCK:
        current = _OPERATION_THREADS.get(operation_id)
        if current is not None and current.is_alive():
            return current
        thread = Thread(
            target=_run_tracked_operation,
            args=(operation_id, target, *args),
            name=name,
            daemon=True,
        )
        _OPERATION_THREADS[operation_id] = thread
        thread.start()
        return thread


def is_worker_operation_thread_active(operation_id: str) -> bool:
    normalized_id = str(operation_id or "").strip()
    if not normalized_id:
        return False
    with _OPERATION_THREADS_LOCK:
        current = _OPERATION_THREADS.get(normalized_id)
        return current is not None and current.is_alive()


def _occupied_install_operation_ids(store) -> set[str]:
    occupied_ids: set[str] = set()
    operation_map = {
        str(task.get("id") or "").strip(): task
        for task in store.get_worker_operation_snapshots()
    }
    with _OPERATION_THREADS_LOCK:
        active_threads = {
            operation_id
            for operation_id, thread in _OPERATION_THREADS.items()
            if thread.is_alive()
        }
    for operation_id in active_threads:
        task = operation_map.get(operation_id)
        if task is None:
            continue
        if str(task.get("kind") or "").strip() != "install":
            continue
        status = str(task.get("status") or "").strip()
        if status in {"completed", "failed"}:
            continue
        occupied_ids.add(operation_id)
    for operation_id, task in operation_map.items():
        if str(task.get("kind") or "").strip() != "install":
            continue
        if str(task.get("status") or "").strip() != "awaiting_registration":
            continue
        occupied_ids.add(operation_id)
    return occupied_ids


def start_worker_install_operation(
    *,
    store,
    request: WorkerBootstrapRequest,
    ssh_user: str,
    auth_mode: str = "password",
    password: str | None = None,
    ssh_private_key: str | None = None,
    manager_id: str | None,
    manager_name: str,
    group: str = "",
    requested_by: str = "system",
    requested_role: str = "system",
    requested_user_id: str | None = None,
    post_install_config: dict | None = None,
    replace_other_workspace_mode: str | None = None,
    workspace_conversion: dict | None = None,
) -> dict:
    task = store.enqueue_worker_install_operation(
        worker_id=request.worker_id,
        worker_name=request.worker_name,
        vps_ip=request.vps_ip,
        threads=request.threads,
        ssh_user=ssh_user,
        control_plane_url=request.control_plane_url,
        auth_mode=auth_mode,
        password=password,
        ssh_private_key=ssh_private_key,
        manager_id=manager_id,
        manager_name=manager_name,
        group=group,
        workspace_mode=request.runtime_mode,
        requested_by=requested_by,
        requested_role=requested_role,
        requested_user_id=requested_user_id,
        post_install_config=post_install_config,
        replace_other_workspace_mode=replace_other_workspace_mode,
        workspace_conversion=workspace_conversion,
    )
    ensure_worker_operation_threads(store)
    return task


def start_worker_decommission_operation(
    *,
    store,
    worker_id: str,
    request: WorkerDecommissionRequest,
    ssh_user: str,
    workspace_mode: str = "upload",
    requested_by: str = "system",
    requested_role: str = "system",
) -> dict:
    task = store.enqueue_worker_decommission_operation(
        worker_id=worker_id,
        vps_ip=request.vps_ip,
        ssh_user=ssh_user,
        transport="ssh",
        app_dir=request.app_dir,
        runtime_dir=request.runtime_dir,
        workspace_mode=workspace_mode,
        requested_by=requested_by,
        requested_role=requested_role,
    )
    _start_operation_thread(
        str(task.get("id") or ""),
        name=f"worker-decommission-{worker_id}",
        target=_run_worker_decommission_operation,
        args=(store, str(task.get("id") or ""), worker_id, request),
    )
    return task


def _install_request_from_task(store, task: dict[str, str]) -> WorkerBootstrapRequest:
    worker_id = str(task.get("worker_id") or "").strip()
    workspace_mode = str(task.get("workspace_mode") or "").strip() or "upload"
    profile = store.get_worker_connection_profile(worker_id, workspace_mode=workspace_mode)
    auth_mode = str(profile.get("auth_mode") or "password").strip().lower() or "password"
    control_plane_url = str(task.get("control_plane_url") or "").strip()
    request = build_worker_bootstrap_request(
        vps_ip=str(profile.get("vps_ip") or task.get("vps_ip") or "").strip(),
        ssh_user=str(profile.get("ssh_user") or "root").strip() or "root",
        password=(str(profile.get("password") or "").strip() or None) if auth_mode != "ssh_key" else None,
        ssh_private_key=(str(profile.get("ssh_private_key") or "").strip() or None) if auth_mode == "ssh_key" else None,
        shared_secret=store.get_worker_shared_secret(),
        control_plane_url=build_worker_bootstrap_control_plane_url(control_plane_url),
        worker_id=worker_id,
        manager_name=str(task.get("manager_name") or "").strip() or "system",
        runtime_mode=workspace_mode,
    )
    requested_threads = (
        store._normalize_live_worker_threads(task.get("threads") or 1)
        if workspace_mode == "live"
        else max(1, int(task.get("threads") or 1))
    )
    request.threads = requested_threads
    request.capacity = requested_threads
    return request


def _decommission_request_from_task(store, task: dict[str, str]) -> WorkerDecommissionRequest:
    worker_id = str(task.get("worker_id") or "").strip()
    workspace_mode = str(task.get("workspace_mode") or "").strip() or "upload"
    profile = store.get_worker_connection_profile(worker_id, workspace_mode=workspace_mode)
    auth_mode = str(profile.get("auth_mode") or "password").strip().lower() or "password"
    return build_worker_decommission_request(
        vps_ip=str(profile.get("vps_ip") or task.get("vps_ip") or "").strip(),
        ssh_user=str(profile.get("ssh_user") or "root").strip() or "root",
        password=(str(profile.get("password") or "").strip() or None) if auth_mode != "ssh_key" else None,
        ssh_private_key=(str(profile.get("ssh_private_key") or "").strip() or None) if auth_mode == "ssh_key" else None,
        runtime_mode=workspace_mode,
    )


def ensure_worker_operation_threads(store) -> None:
    snapshots = store.get_worker_operation_snapshots()
    install_limit = _worker_install_max_concurrency()
    occupied_install_ids = _occupied_install_operation_ids(store)
    available_install_slots = max(0, install_limit - len(occupied_install_ids))

    for task in snapshots:
        operation_id = str(task.get("id") or "").strip()
        kind = str(task.get("kind") or "").strip()
        status = str(task.get("status") or "").strip()
        if not operation_id or status in {"completed", "failed", "awaiting_registration"}:
            continue
        try:
            if kind == "install":
                if operation_id in occupied_install_ids:
                    continue
                if available_install_slots <= 0:
                    continue
                request = _install_request_from_task(store, task)
                _start_operation_thread(
                    operation_id,
                    name=f"worker-install-{request.worker_id}",
                    target=_run_worker_install_operation,
                    args=(store, operation_id, request),
                )
                occupied_install_ids.add(operation_id)
                available_install_slots -= 1
            elif kind == "decommission" and str(task.get("transport") or "ssh").strip() == "ssh":
                request = _decommission_request_from_task(store, task)
                worker_id = str(task.get("worker_id") or "").strip()
                _start_operation_thread(
                    operation_id,
                    name=f"worker-decommission-{worker_id}",
                    target=_run_worker_decommission_operation,
                    args=(store, operation_id, worker_id, request),
                )
        except Exception as exc:
            logger.exception(
                "worker_operation_resume_failed operation_id=%s worker_id=%s kind=%s",
                operation_id,
                str(task.get("worker_id") or "").strip(),
                kind or "unknown",
            )
            store.fail_worker_operation(operation_id, message=str(exc))
