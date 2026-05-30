from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

import httpx

from .browser_uploader import upload_video_via_browser
from .config import WorkerConfig
from .control_plane import complete_job, get_job_youtube_target, update_job_progress, upload_job_thumbnail
from .downloader import download_local_asset, download_remote_asset
from .ffmpeg_pipeline import render_job_assets
from .preview import capture_video_preview


def _make_progress_reporter(client: httpx.Client, config: WorkerConfig, job_id: str):
    state = {
        "status": None,
        "progress": -1,
        "message": None,
        "sent_at": 0.0,
    }

    def _report(status: str, progress: int, message: str | None = None, *, force: bool = False) -> None:
        bounded = max(0, min(100, int(progress)))
        cleaned_message = (message or "").strip() or None
        now = time.monotonic()
        last_status = state["status"]
        last_progress = int(state["progress"])
        last_message = state["message"]
        last_sent_at = float(state["sent_at"])

        progress_jump = bounded - last_progress
        should_send = force
        should_send = should_send or last_status != status
        should_send = should_send or bounded in {0, 100}
        should_send = should_send or progress_jump >= 3
        should_send = should_send or (cleaned_message != last_message and now - last_sent_at >= 1.0)
        should_send = should_send or (progress_jump > 0 and now - last_sent_at >= 1.0)
        should_send = should_send or now - last_sent_at >= 10.0

        if not should_send:
            state["status"] = status
            state["progress"] = max(last_progress, bounded)
            state["message"] = cleaned_message
            return

        try:
            update_job_progress(
                client,
                config,
                job_id,
                status=status,
                progress=bounded,
                message=cleaned_message,
            )
        except Exception as exc:
            print(
                f"[job_progress] job={job_id} status={status} progress={bounded} "
                f"update skipped after retries: {exc}",
                flush=True,
            )
            state["status"] = status
            state["progress"] = max(last_progress, bounded)
            state["message"] = cleaned_message
            state["sent_at"] = now
            return
        state["status"] = status
        state["progress"] = bounded
        state["message"] = cleaned_message
        state["sent_at"] = now

    return _report


def _job_asset_map(job: dict) -> dict[str, dict]:
    return {str(asset["slot"]): asset for asset in job.get("assets") or []}


def _safe_upload_title_file_name(title: str | None, suffix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 ]+", " ", str(title or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    stem = cleaned or "video"
    normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ".mp4"
    return f"{stem}{normalized_suffix}"


def _prepare_browser_upload_file(job_id: str, source_path: Path, *, title: str | None) -> Path:
    upload_dir = Path.home() / "Downloads" / "youtube-upload-lush" / job_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    staged_path = upload_dir / _safe_upload_title_file_name(title, source_path.suffix or ".mp4")
    staged_path.unlink(missing_ok=True)
    try:
        os.link(source_path, staged_path)
    except OSError:
        shutil.copy2(source_path, staged_path)
    return staged_path


def _download_assets(
    client: httpx.Client,
    config: WorkerConfig,
    job: dict,
    downloads_dir: Path,
    report_progress,
) -> dict[str, Path]:
    assets = _job_asset_map(job)
    downloaded: dict[str, Path] = {}
    ordered_assets = [
        (slot, assets.get(slot))
        for slot in ["intro", "video_loop", "audio_loop", "outro"]
        if assets.get(slot)
    ]
    total_assets = len(ordered_assets)
    if total_assets <= 0:
        return downloaded

    def _progress_callback_for(asset_index: int, slot_name: str):
        def _callback(ratio: float, _message: str | None) -> None:
            clamped_ratio = max(0.0, min(1.0, ratio))
            overall_ratio = (asset_index + clamped_ratio) / total_assets
            report_progress(
                status="downloading",
                progress=max(0, min(100, int(overall_ratio * 100))),
                message=f"Dang tai {slot_name} {int(clamped_ratio * 100)}%",
            )

        return _callback

    for index, (slot, asset) in enumerate(ordered_assets):
        if asset.get("source_mode") == "local":
            downloaded[slot] = download_local_asset(
                client,
                config,
                str(job["id"]),
                slot,
                downloads_dir,
                progress_callback=_progress_callback_for(index, slot),
            )
        elif asset.get("url"):
            downloaded[slot] = download_remote_asset(
                str(asset["url"]),
                slot,
                downloads_dir,
                progress_callback=_progress_callback_for(index, slot),
            )
        else:
            raise ValueError(f"Asset {slot} khong co nguon tai hop le.")
        report_progress(
            status="downloading",
            progress=max(0, min(100, int(((index + 1) / total_assets) * 100))),
            message=f"Da tai xong {slot}",
            force=index == total_assets - 1,
        )
    return downloaded


def _try_upload_job_preview_thumbnail(
    client: httpx.Client,
    config: WorkerConfig,
    *,
    job_id: str,
    video_source_path: Path | None,
    job_dir: Path,
) -> None:
    if not video_source_path or not video_source_path.exists():
        return

    preview_dir = job_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / "video-loop-preview.jpg"
    try:
        snapshot_path = capture_video_preview(
            config,
            source_path=video_source_path,
            destination_path=preview_path,
        )
        upload_job_thumbnail(
            client,
            config,
            job_id,
            file_name=snapshot_path.name,
            content_type="image/jpeg",
            payload=snapshot_path.read_bytes(),
        )
    except Exception as exc:
        print(f"[preview] job={job_id} thumbnail upload skipped: {exc}")


def run_job(client: httpx.Client, config: WorkerConfig, job: dict) -> None:
    job_id = str(job["id"])
    report_progress = _make_progress_reporter(client, config, job_id)
    job_dir = config.work_root / job_id
    downloads_dir = job_dir / "downloads"
    outputs_dir = config.work_root / "outputs"
    final_output_path: Path | None = None
    browser_upload_path: Path | None = None
    config.work_root.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    if job_dir.exists():
        shutil.rmtree(job_dir)
    downloads_dir.mkdir(parents=True, exist_ok=True)

    try:
        report_progress(status="downloading", progress=0, message="Bat dau tai asset", force=True)
        asset_paths = _download_assets(client, config, job, downloads_dir, report_progress)
        report_progress(status="rendering", progress=1, message="Bat dau render", force=True)

        _try_upload_job_preview_thumbnail(
            client,
            config,
            job_id=job_id,
            video_source_path=asset_paths.get("video_loop"),
            job_dir=job_dir,
        )

        result = render_job_assets(
            config,
            job,
            asset_paths,
            job_dir,
            progress_callback=lambda progress, message: report_progress(
                status="rendering",
                progress=progress,
                message=message,
            ),
        )

        final_output_path = outputs_dir / f"{job_id}-{result.output_path.name}"
        shutil.move(str(result.output_path), str(final_output_path))
        local_output_url = f"worker://{config.worker_name}/{final_output_path.as_posix()}"

        if config.youtube_upload_enabled:
            report_progress(
                status="uploading",
                progress=0,
                message="Dang chuan bi upload YouTube",
                force=True,
            )
            target = get_job_youtube_target(client, config, job_id)
            browser_upload_path = _prepare_browser_upload_file(
                job_id,
                final_output_path,
                title=target.title,
            )
            upload_result = upload_video_via_browser(
                config=config,
                target=target,
                file_path=browser_upload_path,
                progress_callback=lambda ratio, message: report_progress(
                    status="uploading",
                    progress=max(0, min(100, int(max(0.0, min(1.0, ratio)) * 100))),
                    message=message,
                ),
            )
            complete_job(
                client,
                config,
                job_id,
                output_url=upload_result.output_url,
                message=upload_result.completion_message or "YouTube da xac nhan video upload o che do ban nhap",
            )
            if not config.keep_job_dirs and upload_result.cleanup_safe and final_output_path.exists():
                final_output_path.unlink(missing_ok=True)
        else:
            complete_job(client, config, job_id, output_url=local_output_url)
    except Exception:
        if not config.keep_job_dirs and final_output_path is not None and final_output_path.exists():
            final_output_path.unlink(missing_ok=True)
        raise
    finally:
        if browser_upload_path is not None:
            browser_upload_path.unlink(missing_ok=True)
            try:
                browser_upload_path.parent.rmdir()
                browser_upload_path.parent.parent.rmdir()
            except OSError:
                pass
        if not config.keep_job_dirs and job_dir.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
