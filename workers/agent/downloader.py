from __future__ import annotations

import mimetypes
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

import httpx

from .config import WorkerConfig
from .control_plane import worker_auth_headers


DownloadProgressCallback = Callable[[float, str | None], None]


class DownloadProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def _positive_float_env(name: str, default: float, *, minimum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _positive_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def _remote_download_stall_timeout_seconds() -> float:
    return _positive_float_env("WORKER_REMOTE_DOWNLOAD_STALL_TIMEOUT_SECONDS", 180.0, minimum=10.0)


def _remote_download_attempts() -> int:
    return _positive_int_env("WORKER_REMOTE_DOWNLOAD_ATTEMPTS", 3)


def _remote_download_retry_delay_seconds() -> float:
    return _positive_float_env("WORKER_REMOTE_DOWNLOAD_RETRY_DELAY_SECONDS", 5.0, minimum=0.0)


def _remote_http_timeout() -> httpx.Timeout:
    stall_timeout = _remote_download_stall_timeout_seconds()
    return httpx.Timeout(connect=min(30.0, stall_timeout), read=stall_timeout, write=stall_timeout, pool=30.0)


def _sanitize_filename(file_name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", (file_name or "").strip())
    value = value.strip(".-")
    return value or "download.bin"


def _filename_from_content_disposition(content_disposition: str | None) -> str | None:
    if not content_disposition:
        return None
    match = re.search(r'filename="?([^";]+)"?', content_disposition)
    if match:
        return _sanitize_filename(match.group(1))
    return None


def _extension_from_response(response: httpx.Response) -> str:
    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    guessed = mimetypes.guess_extension(content_type) or ""
    return guessed if guessed != ".jpe" else ".jpg"


def _emit_download_progress(
    progress_callback: DownloadProgressCallback | None,
    ratio: float,
    message: str | None,
) -> None:
    if not progress_callback:
        return
    progress_callback(max(0.0, min(1.0, ratio)), message)


def _download_via_stream(
    url: str,
    destination: Path,
    *,
    progress_callback: DownloadProgressCallback | None = None,
    label: str = "asset",
) -> Path:
    with httpx.Client(follow_redirects=True, timeout=_remote_http_timeout()) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            file_name = _filename_from_content_disposition(response.headers.get("content-disposition"))
            if not destination.suffix:
                destination = destination.with_suffix(_extension_from_response(response))
            if file_name:
                destination = destination.with_name(file_name)

            total_bytes = int(response.headers.get("content-length") or "0") or 0
            received_bytes = 0
            _emit_download_progress(progress_callback, 0.0, f"Dang tai {label}")
            with destination.open("wb") as file_obj:
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    file_obj.write(chunk)
                    if total_bytes <= 0:
                        continue
                    received_bytes += len(chunk)
                    ratio = min(received_bytes / total_bytes, 0.999)
                    _emit_download_progress(
                        progress_callback,
                        ratio,
                        f"Dang tai {label} {int(ratio * 100)}%",
                    )
            _emit_download_progress(progress_callback, 1.0, f"Da tai xong {label}")
    return destination


def _is_google_drive_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return (
        "drive.google.com" in host
        or "docs.google.com" in host
        or "drive.usercontent.google.com" in host
    )


def _google_drive_file_id(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query_id = next((value for value in query.get("id", []) if value), "")
    if query_id:
        return query_id

    patterns = (
        r"/file/d/([A-Za-z0-9_-]+)",
        r"/d/([A-Za-z0-9_-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, parsed.path)
        if match:
            return match.group(1)
    return None


def _looks_like_html_response(response: httpx.Response) -> bool:
    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    return content_type == "text/html"


def _download_google_drive_direct(
    file_id: str,
    destination: Path,
    *,
    progress_callback: DownloadProgressCallback | None = None,
    label: str = "asset",
) -> Path:
    candidate_urls = [
        f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
        f"https://drive.google.com/uc?export=download&id={file_id}",
        f"https://drive.google.com/uc?id={file_id}",
    ]
    last_error: Exception | None = None
    for candidate_url in candidate_urls:
        try:
            with httpx.Client(follow_redirects=True, timeout=_remote_http_timeout()) as client:
                with client.stream("GET", candidate_url) as response:
                    response.raise_for_status()
                    if _looks_like_html_response(response):
                        raise RuntimeError("Google Drive tra ve trang HTML thay vi file media.")

                    file_name = _filename_from_content_disposition(response.headers.get("content-disposition"))
                    resolved_destination = destination
                    if not resolved_destination.suffix:
                        resolved_destination = resolved_destination.with_suffix(_extension_from_response(response))
                    if file_name:
                        resolved_destination = resolved_destination.with_name(file_name)

                    total_bytes = int(response.headers.get("content-length") or "0") or 0
                    received_bytes = 0
                    _emit_download_progress(progress_callback, 0.0, f"Dang tai {label}")
                    with resolved_destination.open("wb") as file_obj:
                        for chunk in response.iter_bytes():
                            if not chunk:
                                continue
                            file_obj.write(chunk)
                            if total_bytes <= 0:
                                continue
                            received_bytes += len(chunk)
                            ratio = min(received_bytes / total_bytes, 0.999)
                            _emit_download_progress(
                                progress_callback,
                                ratio,
                                f"Dang tai {label} {int(ratio * 100)}%",
                            )
                    _emit_download_progress(progress_callback, 1.0, f"Da tai xong {label}")
                    return resolved_destination
        except Exception as exc:
            last_error = exc
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("Khong the tai asset tu Google Drive.")


def _terminate_download_process(process: DownloadProcess) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
        return
    except subprocess.TimeoutExpired:
        pass
    process.kill()
    process.wait(timeout=5.0)


def _download_activity_size(target_path: Path) -> int:
    candidates = [target_path, *target_path.parent.glob(f"{target_path.name}*.part")]
    total_size = 0
    for candidate in candidates:
        try:
            total_size += candidate.stat().st_size
        except OSError:
            continue
    return total_size


def _wait_for_download_process(
    process: DownloadProcess,
    target_path: Path,
    *,
    stall_timeout_seconds: float,
    poll_interval_seconds: float = 0.8,
    progress_callback: DownloadProgressCallback | None = None,
    label: str = "asset",
) -> None:
    last_size = _download_activity_size(target_path)
    last_activity_at = time.monotonic()
    while process.poll() is None:
        current_size = _download_activity_size(target_path)
        now = time.monotonic()
        if current_size != last_size:
            last_size = current_size
            last_activity_at = now
            if current_size > 0:
                pseudo_ratio = min(0.92, 0.08 + min(current_size / (96 * 1024 * 1024), 0.84))
                _emit_download_progress(
                    progress_callback,
                    pseudo_ratio,
                    f"Dang tai {label}",
                )
        elif now - last_activity_at >= stall_timeout_seconds:
            _terminate_download_process(process)
            raise TimeoutError(
                f"Download {label} khong co du lieu moi trong {int(stall_timeout_seconds)} giay."
            )
        time.sleep(poll_interval_seconds)


def _run_gdown_attempt(
    url: str,
    target_path: Path,
    *,
    stall_timeout_seconds: float,
    progress_callback: DownloadProgressCallback | None = None,
    label: str = "asset",
) -> Path:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "gdown",
            "--fuzzy",
            "--quiet",
            url,
            "-O",
            str(target_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_download_process(
            process,
            target_path,
            stall_timeout_seconds=stall_timeout_seconds,
            progress_callback=progress_callback,
            label=label,
        )
    except BaseException:
        _terminate_download_process(process)
        raise

    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(f"gdown thoat voi ma loi {return_code}.")
    if not target_path.exists() or target_path.stat().st_size <= 0:
        raise RuntimeError("gdown khong tao duoc file media hop le.")
    return target_path


def _cleanup_download_attempt_files(target_path: Path) -> None:
    try:
        candidates = list(target_path.parent.iterdir())
    except OSError:
        candidates = [target_path]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            continue


def _download_google_drive_with_retries(
    url: str,
    target_path: Path,
    *,
    attempts: int,
    retry_delay_seconds: float,
    stall_timeout_seconds: float,
    progress_callback: DownloadProgressCallback | None = None,
    label: str = "asset",
) -> Path:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        _cleanup_download_attempt_files(target_path)
        _emit_download_progress(
            progress_callback,
            0.02,
            f"Dang tai {label} lan {attempt}/{attempts}",
        )
        try:
            return _run_gdown_attempt(
                url,
                target_path,
                stall_timeout_seconds=stall_timeout_seconds,
                progress_callback=progress_callback,
                label=label,
            )
        except Exception as exc:
            last_error = exc
            _cleanup_download_attempt_files(target_path)
            if attempt < attempts:
                time.sleep(retry_delay_seconds)

    raise RuntimeError(f"Khong the tai Google Drive asset sau {attempts} lan: {last_error}") from last_error


def download_local_asset(
    client: httpx.Client,
    config: WorkerConfig,
    job_id: str,
    slot: str,
    destination_dir: Path,
    progress_callback: DownloadProgressCallback | None = None,
) -> Path:
    slot_dir = destination_dir / slot
    slot_dir.mkdir(parents=True, exist_ok=True)
    fallback_path = slot_dir / f"{slot}.bin"
    with client.stream(
        "GET",
        f"/api/workers/jobs/{job_id}/assets/{slot}",
        headers=worker_auth_headers(config),
    ) as response:
        response.raise_for_status()
        file_name = _filename_from_content_disposition(response.headers.get("content-disposition")) or fallback_path.name
        target_path = slot_dir / file_name
        total_bytes = int(response.headers.get("content-length") or "0") or 0
        received_bytes = 0
        _emit_download_progress(progress_callback, 0.0, f"Dang tai {slot}")
        with target_path.open("wb") as file_obj:
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                file_obj.write(chunk)
                if total_bytes <= 0:
                    continue
                received_bytes += len(chunk)
                ratio = min(received_bytes / total_bytes, 0.999)
                _emit_download_progress(
                    progress_callback,
                    ratio,
                    f"Dang tai {slot} {int(ratio * 100)}%",
                )
        _emit_download_progress(progress_callback, 1.0, f"Da tai xong {slot}")
    return target_path


def download_remote_asset(
    url: str,
    slot: str,
    destination_dir: Path,
    *,
    progress_callback: DownloadProgressCallback | None = None,
) -> Path:
    slot_dir = destination_dir / slot
    slot_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    file_name = Path(parsed.path).name or slot
    target_path = slot_dir / _sanitize_filename(file_name)

    if _is_google_drive_url(url):
        file_id = _google_drive_file_id(url)
        label = f"{slot} tu Google Drive"
        gdown_error: Exception | None = None
        try:
            downloaded_path = _download_google_drive_with_retries(
                url,
                target_path,
                attempts=_remote_download_attempts(),
                retry_delay_seconds=_remote_download_retry_delay_seconds(),
                stall_timeout_seconds=_remote_download_stall_timeout_seconds(),
                progress_callback=progress_callback,
                label=label,
            )
        except Exception as exc:
            gdown_error = exc
            _cleanup_download_attempt_files(target_path)
            if not file_id:
                raise
            try:
                downloaded_path = _download_google_drive_direct(
                    file_id,
                    target_path,
                    progress_callback=progress_callback,
                    label=label,
                )
            except Exception as direct_error:
                raise RuntimeError(
                    f"Khong the tai Google Drive asset bang gdown hoac direct URL: "
                    f"gdown={gdown_error}; direct={direct_error}"
                ) from direct_error
        _emit_download_progress(progress_callback, 1.0, f"Da tai xong {slot}")
        return downloaded_path

    return _download_via_stream(url, target_path, progress_callback=progress_callback, label=slot)
