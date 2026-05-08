from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .config import WorkerConfig


def _is_truthy(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _read_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _latest_mtime(path: Path) -> float:
    latest = path.stat().st_mtime
    if path.is_dir():
        for child in path.rglob("*"):
            try:
                latest = max(latest, child.stat().st_mtime)
            except OSError:
                continue
    return latest


def _is_older_than(path: Path, *, cutoff_ts: float) -> bool:
    try:
        return _latest_mtime(path) < cutoff_ts
    except OSError:
        return False


def _safe_remove_tree(path: Path) -> bool:
    try:
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()
    except OSError:
        return False


def _safe_remove_file(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return not path.exists()
    except OSError:
        return False


def _kill_stray_xvfb_processes() -> int:
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return 0

    killed = 0
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            pid_text, args = line.split(None, 1)
            pid = int(pid_text)
        except Exception:
            continue
        normalized_args = args.strip()
        if normalized_args != "Xvfb":
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                killed += 1
                continue
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            continue
    return killed


def cleanup_stale_worker_artifacts(config: WorkerConfig) -> dict[str, int]:
    enabled = _is_truthy(os.getenv("WORKER_JANITOR_ENABLED"), default=True)
    if not enabled:
        return {
            "removed_job_dirs": 0,
            "removed_live_stream_dirs": 0,
            "removed_output_files": 0,
            "removed_browser_upload_runtime_dirs": 0,
            "removed_browser_upload_download_dirs": 0,
            "killed_stray_xvfb": 0,
        }

    temp_retention_hours = _read_env_float("WORKER_TEMP_RETENTION_HOURS", 6.0)
    live_stream_retention_hours = _read_env_float(
        "WORKER_LIVE_STREAM_RETENTION_HOURS",
        1.0 if config.runtime_mode == "live" else temp_retention_hours,
    )
    output_retention_hours = _read_env_float(
        "WORKER_OUTPUT_RETENTION_HOURS",
        6.0 if config.youtube_upload_enabled else 48.0,
    )
    now = time.time()
    temp_cutoff = now - (temp_retention_hours * 3600.0)
    live_stream_cutoff = now - (live_stream_retention_hours * 3600.0)
    output_cutoff = now - (output_retention_hours * 3600.0)

    removed_job_dirs = 0
    removed_live_stream_dirs = 0
    removed_output_files = 0
    removed_browser_upload_runtime_dirs = 0
    removed_browser_upload_download_dirs = 0

    work_root = config.work_root
    for job_dir in sorted(work_root.glob("job-*")):
        if not job_dir.is_dir():
            continue
        if not _is_older_than(job_dir, cutoff_ts=temp_cutoff):
            continue
        if _safe_remove_tree(job_dir):
            removed_job_dirs += 1

    live_stream_root = work_root / "live-streams"
    if live_stream_root.exists():
        for stream_dir in sorted(live_stream_root.iterdir()):
            if not stream_dir.is_dir():
                continue
            if not _is_older_than(stream_dir, cutoff_ts=live_stream_cutoff):
                continue
            if _safe_remove_tree(stream_dir):
                removed_live_stream_dirs += 1

    browser_upload_runtime_root = work_root / "browser-upload-runtime"
    if browser_upload_runtime_root.exists():
        for runtime_dir in sorted(browser_upload_runtime_root.iterdir()):
            if not runtime_dir.is_dir():
                continue
            if not _is_older_than(runtime_dir, cutoff_ts=temp_cutoff):
                continue
            if _safe_remove_tree(runtime_dir):
                removed_browser_upload_runtime_dirs += 1

    browser_upload_download_root = Path.home() / "Downloads" / "youtube-upload-lush"
    if browser_upload_download_root.exists():
        for upload_dir in sorted(browser_upload_download_root.iterdir()):
            if not upload_dir.is_dir():
                continue
            if not _is_older_than(upload_dir, cutoff_ts=temp_cutoff):
                continue
            if _safe_remove_tree(upload_dir):
                removed_browser_upload_download_dirs += 1

    outputs_dir = work_root / "outputs"
    if outputs_dir.exists():
        for output_file in sorted(outputs_dir.iterdir()):
            if not output_file.is_file():
                continue
            if not _is_older_than(output_file, cutoff_ts=output_cutoff):
                continue
            if _safe_remove_file(output_file):
                removed_output_files += 1

    killed_stray_xvfb = _kill_stray_xvfb_processes()
    return {
        "removed_job_dirs": removed_job_dirs,
        "removed_live_stream_dirs": removed_live_stream_dirs,
        "removed_output_files": removed_output_files,
        "removed_browser_upload_runtime_dirs": removed_browser_upload_runtime_dirs,
        "removed_browser_upload_download_dirs": removed_browser_upload_download_dirs,
        "killed_stray_xvfb": killed_stray_xvfb,
    }
