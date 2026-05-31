from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Lock, Semaphore
from zoneinfo import ZoneInfo

import httpx

from .config import WorkerConfig
from .control_plane import (
    LiveStreamStoppedError,
    complete_live_stream,
    get_live_stream_runtime_state,
    update_live_stream_progress,
    upload_live_thumbnail,
)
from .downloader import download_remote_asset
from .ffmpeg_pipeline import MediaInfo, probe_media
from .live_supervisor import LiveSupervisor
from .preview import capture_video_preview


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw_value = str(os.getenv(name, str(default))).strip()
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    return max(minimum, parsed)
LIVE_RUNTIME_GUARD_INTERVAL_SECONDS = _env_float("WORKER_LIVE_RUNTIME_GUARD_INTERVAL_SECONDS", 1.0, minimum=0.2)
LIVE_FFMPEG_PROGRESS_INTERVAL_SECONDS = _env_float("WORKER_LIVE_FFMPEG_PROGRESS_INTERVAL_SECONDS", 0.5, minimum=0.2)
LIVE_RTMP_RETRY_DELAY_SECONDS = _env_float("WORKER_LIVE_RTMP_RETRY_DELAY_SECONDS", 0.0, minimum=0.0)
LIVE_COPY_SUPPORTED_VIDEO_CODECS = {"h264"}
LIVE_COPY_SUPPORTED_AUDIO_CODECS = {"aac", "mp3"}
LIVE_NORMALIZE_TARGET_GOP_SECONDS = 2.0
LIVE_NORMALIZE_DEFAULT_FRAME_RATE = 25.0
YOUTUBE_RTMPS_PRIMARY_URL = "rtmps://a.rtmps.youtube.com/live2"
YOUTUBE_RTMPS_BACKUP_URL = "rtmps://b.rtmps.youtube.com/live2?backup=1"
_LIVE_NORMALIZE_SLOT_LOCK = Lock()
_LIVE_NORMALIZE_SLOT: Semaphore | None = None
_LIVE_NORMALIZE_SLOT_LIMIT = 0


@dataclass(slots=True)
class LiveVideoNormalizePlan:
    normalize_required: bool
    profile_height: int
    scale_height: int | None
    frame_rate: float
    gop_frames: int
    maxrate_kbps: int
    crf: int
    reason: str
    source_bitrate_kbps: int | None


def _live_normalize_slot(limit: int) -> Semaphore:
    global _LIVE_NORMALIZE_SLOT, _LIVE_NORMALIZE_SLOT_LIMIT
    normalized_limit = max(1, int(limit or 1))
    with _LIVE_NORMALIZE_SLOT_LOCK:
        if _LIVE_NORMALIZE_SLOT is None or _LIVE_NORMALIZE_SLOT_LIMIT != normalized_limit:
            _LIVE_NORMALIZE_SLOT = Semaphore(normalized_limit)
            _LIVE_NORMALIZE_SLOT_LIMIT = normalized_limit
        return _LIVE_NORMALIZE_SLOT


def _acquire_live_normalize_slot(
    config: WorkerConfig,
    *,
    stream_id: str,
    report_progress,
    lifecycle_guard=None,
) -> Semaphore:
    slot = _live_normalize_slot(config.live_normalize_concurrency)
    wait_started_at: float | None = None
    last_report_second = -1
    while True:
        if slot.acquire(blocking=False):
            if wait_started_at is not None:
                waited_seconds = time.monotonic() - wait_started_at
                print(
                    (
                        "[live] normalize slot acquired "
                        f"stream_id={stream_id} "
                        f"limit={config.live_normalize_concurrency} "
                        f"waited_seconds={waited_seconds:.1f}"
                    ),
                    flush=True,
                )
            return slot
        if lifecycle_guard:
            lifecycle_guard(force=True)
        if wait_started_at is None:
            wait_started_at = time.monotonic()
            print(
                (
                    "[live] normalize slot busy "
                    f"stream_id={stream_id} "
                    f"limit={config.live_normalize_concurrency}"
                ),
                flush=True,
            )
        waited_seconds = int(time.monotonic() - wait_started_at)
        should_report = waited_seconds == 0 or waited_seconds - last_report_second >= 5
        if should_report:
            report_progress(
                "preparing",
                0,
                f"Đang chờ lượt tối ưu nguồn trên VPS ({waited_seconds}s)",
                force=True,
            )
            last_report_second = waited_seconds
        time.sleep(0.5 if waited_seconds < 10 else 1.0)


def _app_timezone() -> ZoneInfo:
    configured = os.getenv("APP_TIMEZONE", "Asia/Ho_Chi_Minh").strip()
    if configured.casefold() == "asia/saigon":
        configured = "Asia/Ho_Chi_Minh"
    for candidate in (configured, "Asia/Ho_Chi_Minh", "Etc/GMT-7"):
        try:
            return ZoneInfo(candidate)
        except Exception:
            continue
    raise RuntimeError("No valid timezone found for live worker")


def _now_local() -> datetime:
    return datetime.now(_app_timezone()).replace(tzinfo=None)


def _parse_control_plane_datetime(value: str | None) -> datetime | None:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    normalized = cleaned.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        return parsed.astimezone(_app_timezone()).replace(tzinfo=None)
    return parsed


def _normalize_youtube_ingest_url(base_url: str | None) -> str:
    cleaned = str(base_url or "").strip()
    if not cleaned:
        return YOUTUBE_RTMPS_PRIMARY_URL
    parsed = urlsplit(cleaned)
    host = parsed.hostname or ""
    if parsed.scheme == "rtmp" and host.endswith(".rtmp.youtube.com"):
        host_prefix = host.split(".", 1)[0].lower()
        target = YOUTUBE_RTMPS_BACKUP_URL if host_prefix in {"b", "y"} else YOUTUBE_RTMPS_PRIMARY_URL
        if parsed.query and "backup=1" in parsed.query:
            target = YOUTUBE_RTMPS_BACKUP_URL
        return target
    if cleaned == "rtmp://x.rtmp.youtube.com/live2":
        return YOUTUBE_RTMPS_PRIMARY_URL
    if cleaned == "rtmp://y.rtmp.youtube.com/live2?backup=1":
        return YOUTUBE_RTMPS_BACKUP_URL
    return cleaned


def _append_stream_key_to_ingest_url(base_url: str, stream_key: str) -> str:
    parsed = urlsplit(str(base_url or "").strip())
    path = f"{parsed.path.rstrip('/')}/{stream_key}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


def _rtmp_target(stream: dict) -> str:
    base_url = _normalize_youtube_ingest_url(stream.get("rtmp_url"))
    stream_key = str(stream.get("stream_key") or "").strip()
    if not stream_key:
        raise ValueError("Thiếu stream_key cho live runtime.")
    return _append_stream_key_to_ingest_url(base_url, stream_key)


def _is_hot_standby_backup_stream(stream: dict) -> bool:
    runtime_role = str(stream.get("runtime_role") or "").strip().lower()
    is_runtime_clone = bool(stream.get("is_runtime_clone"))
    is_forever = bool(stream.get("is_forever"))
    end_time_live = _parse_control_plane_datetime(stream.get("end_time_live"))
    return runtime_role == "backup" and is_runtime_clone and is_forever and end_time_live is None


def _has_live_failover_backup(stream: dict) -> bool:
    runtime_role = str(stream.get("runtime_role") or "").strip().lower()
    is_runtime_clone = bool(stream.get("is_runtime_clone"))
    backup_worker_id = str(stream.get("backup_worker_id") or "").strip()
    return runtime_role != "backup" and not is_runtime_clone and bool(backup_worker_id)


def _is_timed_primary_with_parallel_backup(stream: dict) -> bool:
    if not _has_live_failover_backup(stream):
        return False
    return _parse_control_plane_datetime(stream.get("end_time_live")) is not None


def _is_retriable_rtmp_output_error(exc: BaseException) -> bool:
    message = str(exc or "").casefold()
    if "ffmpeg live runtime failed" not in message:
        return False
    retriable_tokens = (
        "rtmp output disconnected",
        "connection reset by peer",
        "broken pipe",
        "end of file",
        "error muxing a packet",
        "error submitting a packet to the muxer",
        "error writing trailer",
        "error closing file",
        "av_interleaved_write_frame",
        "i/o error",
        "input/output error",
    )
    return any(token in message for token in retriable_tokens)


def _should_retry_rtmp_output_error(stream: dict, exc: BaseException) -> bool:
    if _has_live_failover_backup(stream) and not _is_timed_primary_with_parallel_backup(stream):
        return False
    return _is_retriable_rtmp_output_error(exc)


def _sleep_before_rtmp_retry(
    *,
    delay_seconds: float,
    end_time_live: datetime | None,
    lifecycle_guard=None,
) -> None:
    deadline = time.monotonic() + max(0.0, delay_seconds)
    while time.monotonic() < deadline:
        if end_time_live and _now_local() >= end_time_live:
            return
        if lifecycle_guard:
            lifecycle_guard(force=True)
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _touch_activity(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        return


def _is_terminal_live_stream_conflict(exc: Exception) -> bool:
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    response = exc.response
    request = exc.request
    if response is None or request is None:
        return False
    request_url = str(request.url)
    if "/api/live-workers/streams/" not in request_url:
        return False
    return response.status_code in {404, 409}


def _stop_process(process: subprocess.Popen[str], *, timeout_seconds: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is not None:
        return
    process.kill()
    try:
        process.wait(timeout=max(0.5, timeout_seconds))
    except subprocess.TimeoutExpired:
        return


def _make_progress_reporter(
    client: httpx.Client,
    config: WorkerConfig,
    stream_id: str,
    *,
    activity_path: Path | None = None,
    supervisor: LiveSupervisor | None = None,
):
    state = {
        "status": None,
        "progress": -1,
        "message": None,
        "sent_at": 0.0,
    }

    def _report(status: str, progress: int, message: str | None = None, *, force: bool = False) -> None:
        _touch_activity(activity_path)
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
        should_send = should_send or progress_jump >= 25
        should_send = should_send or (cleaned_message != last_message and now - last_sent_at >= 60.0)
        should_send = should_send or (progress_jump > 0 and now - last_sent_at >= 30.0)
        should_send = should_send or now - last_sent_at >= 60.0

        if not should_send:
            state["status"] = status
            state["progress"] = max(last_progress, bounded)
            state["message"] = cleaned_message
            return

        if supervisor is not None:
            supervisor.record_state(status, bounded, cleaned_message, event="progress")
        try:
            update_live_stream_progress(
                client,
                config,
                stream_id,
                status=status,
                progress=bounded,
                message=cleaned_message,
            )
        except Exception as exc:
            if isinstance(exc, LiveStreamStoppedError) or _is_terminal_live_stream_conflict(exc):
                raise
            if supervisor is not None:
                supervisor.record_event(
                    "progress_report_failed",
                    status=status,
                    progress=bounded,
                    message=cleaned_message,
                    error=str(exc),
                )
            print(f"[live] progress report failed for {stream_id}: {exc}", flush=True)
        state["status"] = status
        state["progress"] = max(last_progress, bounded)
        state["message"] = cleaned_message
        state["sent_at"] = now

    return _report


def _make_live_runtime_guard(
    client: httpx.Client,
    config: WorkerConfig,
    stream_id: str,
):
    state = {"last_checked_at": 0.0, "runtime_state": None}

    def _guard(*, force: bool = False) -> None:
        now = time.monotonic()
        last_checked_at = float(state["last_checked_at"])
        if not force and now - last_checked_at < LIVE_RUNTIME_GUARD_INTERVAL_SECONDS:
            return state["runtime_state"]
        state["last_checked_at"] = now
        try:
            runtime_state = get_live_stream_runtime_state(client, config, stream_id)
        except Exception as exc:
            if isinstance(exc, LiveStreamStoppedError):
                raise
            if _is_terminal_live_stream_conflict(exc):
                raise LiveStreamStoppedError(stream_id, "stopped") from exc
            print(f"[live] runtime guard check failed for {stream_id}: {exc}", flush=True)
            return state["runtime_state"]
        state["runtime_state"] = runtime_state
        if runtime_state.should_stop:
            raise LiveStreamStoppedError(stream_id, runtime_state.status)
        return runtime_state

    return _guard


def _download_live_assets(
    stream: dict,
    downloads_dir: Path,
    report_progress,
    *,
    lifecycle_guard=None,
) -> tuple[Path, Path | None]:
    downloads_dir.mkdir(parents=True, exist_ok=True)
    video_url = str(stream.get("video_url") or "").strip()
    if not video_url:
        raise ValueError("Thiếu video_url cho live runtime.")
    audio_url = str(stream.get("audio_url") or "").strip()

    ordered_assets = [("video", video_url)]
    if audio_url:
        ordered_assets.append(("audio", audio_url))
    total_assets = len(ordered_assets)

    def _progress_callback_for(asset_index: int, slot_name: str):
        def _callback(ratio: float, _message: str | None) -> None:
            if lifecycle_guard:
                lifecycle_guard()
            clamped_ratio = max(0.0, min(1.0, ratio))
            overall_ratio = (asset_index + clamped_ratio) / total_assets
            report_progress(
                "downloading",
                max(0, min(100, int(overall_ratio * 100))),
                f"Đang tải {slot_name} {int(clamped_ratio * 100)}%",
            )

        return _callback

    if lifecycle_guard:
        lifecycle_guard(force=True)
    video_path = download_remote_asset(
        video_url,
        "video",
        downloads_dir,
        progress_callback=_progress_callback_for(0, "video"),
    )
    report_progress("downloading", max(1, int((1 / total_assets) * 100)), "Đã tải xong video")

    audio_path: Path | None = None
    if audio_url:
        if lifecycle_guard:
            lifecycle_guard(force=True)
        audio_path = download_remote_asset(
            audio_url,
            "audio",
            downloads_dir,
            progress_callback=_progress_callback_for(1, "audio"),
        )
        report_progress("downloading", 100, "Đã tải xong audio", force=True)
    else:
        report_progress("downloading", 100, "Đã tải xong media", force=True)
    return video_path, audio_path


def _try_upload_live_preview_thumbnail(
    client: httpx.Client,
    config: WorkerConfig,
    *,
    stream: dict,
    stream_id: str,
    video_source_path: Path | None,
    work_dir: Path,
) -> None:
    if not video_source_path or not video_source_path.exists():
        return
    visible_stream_id = str(stream.get("parent_stream_id") or stream_id or "").strip()
    if not visible_stream_id:
        return

    preview_dir = work_dir / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / "live-preview.jpg"
    try:
        snapshot_path = capture_video_preview(
            config,
            source_path=video_source_path,
            destination_path=preview_path,
        )
        upload_live_thumbnail(
            client,
            config,
            visible_stream_id,
            file_name=snapshot_path.name,
            content_type="image/jpeg",
            payload=snapshot_path.read_bytes(),
        )
    except Exception as exc:
        print(f"[live-preview] stream={stream_id} thumbnail upload skipped: {exc}", flush=True)


def _resolve_live_video_normalize_plan(
    config: WorkerConfig,
    media_info: MediaInfo,
    *,
    has_external_audio: bool,
) -> LiveVideoNormalizePlan:
    source_height = int(media_info.height or 0)
    source_width = int(media_info.width or 0)
    if source_height:
        inferred_height = source_height
    elif source_width >= 3840:
        inferred_height = 2160
    elif source_width >= 2560:
        inferred_height = 1440
    else:
        inferred_height = 1080
    scale_height = config.live_normalize_max_height if source_height > config.live_normalize_max_height else None
    effective_height = scale_height or inferred_height
    if effective_height > 1440:
        profile_height = 2160
    elif effective_height > 1080:
        profile_height = 1440
    else:
        profile_height = 1080
    frame_rate = float(media_info.frame_rate or LIVE_NORMALIZE_DEFAULT_FRAME_RATE)
    if frame_rate <= 0:
        frame_rate = LIVE_NORMALIZE_DEFAULT_FRAME_RATE
    gop_frames = max(1, int(round(frame_rate * LIVE_NORMALIZE_TARGET_GOP_SECONDS)))
    if profile_height > 1440:
        maxrate_kbps = config.live_normalize_2160_maxrate_kbps
        crf = config.live_normalize_2160_crf
    elif profile_height > 1080:
        maxrate_kbps = config.live_normalize_1440_maxrate_kbps
        crf = config.live_normalize_1440_crf
    else:
        maxrate_kbps = config.live_normalize_1080_maxrate_kbps
        crf = config.live_normalize_1080_crf
    source_bitrate_kbps = (
        int(round(float(media_info.bit_rate_bps or 0) / 1000.0))
        if media_info.bit_rate_bps
        else None
    )
    video_codec = str(media_info.video_codec or "").strip().lower()
    audio_codec = str(media_info.audio_codec or "").strip().lower()
    audio_compatible = (
        has_external_audio
        or not media_info.has_audio
        or audio_codec in LIVE_COPY_SUPPORTED_AUDIO_CODECS
    )

    reasons: list[str] = []
    if scale_height is not None:
        reasons.append(f"nguon {source_height}p vuot tran {config.live_normalize_max_height}p")
    if source_bitrate_kbps and source_bitrate_kbps > maxrate_kbps:
        reasons.append(f"bitrate {source_bitrate_kbps} kbps vuot muc {maxrate_kbps} kbps")
    if video_codec not in LIVE_COPY_SUPPORTED_VIDEO_CODECS:
        reasons.append(f"codec video {video_codec or 'unknown'} khong hop fast-path")
    if not audio_compatible:
        reasons.append(f"codec audio {audio_codec or 'unknown'} khong hop fast-path")

    normalize_required = bool(config.live_normalize_enabled and reasons)
    if not config.live_normalize_enabled:
        reasons = []
    reason = "; ".join(reasons) if reasons else "giu nguyen media goc"
    return LiveVideoNormalizePlan(
        normalize_required=normalize_required,
        profile_height=profile_height,
        scale_height=scale_height,
        frame_rate=frame_rate,
        gop_frames=gop_frames,
        maxrate_kbps=maxrate_kbps,
        crf=crf,
        reason=reason,
        source_bitrate_kbps=source_bitrate_kbps,
    )


def _maybe_normalize_live_video(
    config: WorkerConfig,
    *,
    stream_id: str,
    video_path: Path,
    audio_path: Path | None,
    normalized_dir: Path,
    report_progress,
    lifecycle_guard=None,
    supervisor: LiveSupervisor | None = None,
) -> tuple[Path, bool]:
    media_info = probe_media(config.ffprobe_bin, video_path)
    plan = _resolve_live_video_normalize_plan(
        config,
        media_info,
        has_external_audio=audio_path is not None,
    )
    source_height = int(media_info.height or 0)

    if not plan.normalize_required:
        print(
            (
                "[live] skip normalize "
                f"stream_id={stream_id} "
                f"path={video_path.name} "
                f"height={source_height or 'unknown'} "
                f"bitrate_kbps={plan.source_bitrate_kbps or 'unknown'} "
                f"reason={plan.reason}"
            ),
            flush=True,
        )
        return video_path, False

    normalize_slot = _acquire_live_normalize_slot(
        config,
        stream_id=stream_id,
        report_progress=report_progress,
        lifecycle_guard=lifecycle_guard,
    )
    try:
        normalized_dir.mkdir(parents=True, exist_ok=True)
        normalized_path = normalized_dir / "video-normalized.mp4"
        filter_chain: list[str] = []
        if plan.scale_height is not None:
            filter_chain.append(f"scale=-2:{plan.scale_height}:flags=lanczos")

        arguments = ["-y", "-i", str(video_path), "-map", "0:v:0"]
        if audio_path is None:
            arguments.extend(["-map", "0:a:0?"])
        else:
            arguments.append("-an")
        if filter_chain:
            arguments.extend(["-vf", ",".join(filter_chain)])
        arguments.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                config.live_normalize_preset,
                "-crf",
                str(plan.crf),
                "-g",
                str(plan.gop_frames),
                "-keyint_min",
                str(plan.gop_frames),
                "-sc_threshold",
                "0",
                "-maxrate",
                f"{plan.maxrate_kbps}k",
                "-bufsize",
                f"{plan.maxrate_kbps * 2}k",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                str(config.live_normalize_threads),
            ]
        )
        if audio_path is None:
            arguments.extend(
                [
                    "-c:a",
                    "aac",
                    "-b:a",
                    f"{config.live_normalize_audio_bitrate_kbps}k",
                ]
            )
        arguments.extend(["-movflags", "+faststart", str(normalized_path)])

        def _on_progress(ratio: float, _current_seconds: float) -> None:
            report_progress(
                "preparing",
                max(0, min(95, int(ratio * 95))),
                f"Đang tối ưu nguồn live {plan.profile_height}p ({int(ratio * 100)}%)",
            )

        print(
            (
                "[live] normalize video "
                f"stream_id={stream_id} "
                f"path={video_path.name} "
                f"target={plan.profile_height}p "
                f"scale_height={plan.scale_height or 'copy'} "
                f"fps={plan.frame_rate:.3f} "
                f"gop_frames={plan.gop_frames} "
                f"gop_seconds={LIVE_NORMALIZE_TARGET_GOP_SECONDS:.1f} "
                f"maxrate_kbps={plan.maxrate_kbps} "
                f"crf={plan.crf} "
                f"threads={config.live_normalize_threads} "
                f"concurrency={config.live_normalize_concurrency} "
                f"reason={plan.reason}"
            ),
            flush=True,
        )
        report_progress(
            "preparing",
            0,
            f"Đang tối ưu nguồn live: {plan.reason}",
            force=True,
        )
        _run_ffmpeg_with_progress(
            config,
            arguments,
            working_dir=normalized_dir,
            total_duration_seconds=max(1.0, float(media_info.duration_seconds or 1.0)),
            progress_callback=_on_progress,
            lifecycle_guard=lifecycle_guard,
            supervisor=supervisor,
        )
        return normalized_path, True
    finally:
        normalize_slot.release()


def _run_ffmpeg_with_progress(
    config: WorkerConfig,
    arguments: list[str],
    *,
    working_dir: Path,
    total_duration_seconds: float | None = None,
    progress_callback=None,
    end_time_live: datetime | None = None,
    lifecycle_guard=None,
    expected_playback_mode: str | None = None,
    supervisor: LiveSupervisor | None = None,
) -> str:
    command = [
        config.ffmpeg_bin,
        "-stats_period",
        f"{LIVE_FFMPEG_PROGRESS_INTERVAL_SECONDS:.2f}",
        "-progress",
        "pipe:1",
        "-nostats",
        *arguments,
    ]
    process = subprocess.Popen(
        command,
        cwd=str(working_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output_lines: list[str] = []
    terminated_for_end_time = False
    terminated_for_mode_change = False
    callback_error: Exception | None = None
    return_code: int | None = None
    if supervisor is not None:
        supervisor.record_event("ffmpeg_start", pid=process.pid, cwd=str(working_dir))

    def _check_runtime(*, force: bool = False) -> bool:
        nonlocal callback_error, terminated_for_mode_change
        try:
            runtime_state = lifecycle_guard(force=force) if lifecycle_guard else None
            if (
                expected_playback_mode
                and runtime_state is not None
                and str(getattr(runtime_state, "playback_mode", "") or "stream").strip().lower() != expected_playback_mode
                and process.poll() is None
            ):
                terminated_for_mode_change = True
                _stop_process(process)
            return True
        except Exception as exc:
            if isinstance(exc, LiveStreamStoppedError) or _is_terminal_live_stream_conflict(exc):
                callback_error = exc
                _stop_process(process)
                return False
            raise

    try:
        assert process.stdout is not None
        while True:
            if not _check_runtime():
                break
            if end_time_live and _now_local() >= end_time_live and process.poll() is None:
                terminated_for_end_time = True
                _stop_process(process)
            raw_line = process.stdout.readline()
            if not raw_line:
                if process.poll() is not None:
                    break
                if not _check_runtime(force=True):
                    break
                time.sleep(0.1)
                continue
            line = raw_line.strip()
            if line:
                output_lines.append(line)
                if supervisor is not None:
                    supervisor.append_ffmpeg_line(line)
            if not progress_callback or not total_duration_seconds:
                continue
            if line.startswith("out_time_ms="):
                try:
                    current_seconds = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                try:
                    progress_callback(max(0.0, min(1.0, current_seconds / total_duration_seconds)), current_seconds)
                except Exception as exc:
                    if isinstance(exc, LiveStreamStoppedError) or _is_terminal_live_stream_conflict(exc):
                        callback_error = exc
                        _stop_process(process)
                        break
                    print(f"[live] progress callback failed: {exc}", flush=True)
    finally:
        if process.poll() is None:
            try:
                return_code = process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                _stop_process(process)
                return_code = process.wait(timeout=5.0)
        else:
            return_code = process.wait()
    if supervisor is not None:
        supervisor.record_event(
            "ffmpeg_exit",
            return_code=return_code,
            ended_by_schedule=terminated_for_end_time,
            paused_by_playback_mode=terminated_for_mode_change,
        )
    if callback_error is not None:
        raise callback_error
    if terminated_for_mode_change:
        return "paused"
    if terminated_for_end_time:
        return "ended"
    if return_code != 0:
        tail = "\n".join(output_lines[-40:])
        raise RuntimeError(f"FFmpeg live runtime failed ({return_code}).\n{tail}".strip())
    return "completed"


def _prepare_rendered_media(
    config: WorkerConfig,
    *,
    video_path: Path,
    audio_path: Path | None,
    render_dir: Path,
    report_progress,
    lifecycle_guard=None,
    progress_floor: int = 0,
    supervisor: LiveSupervisor | None = None,
) -> Path:
    render_dir.mkdir(parents=True, exist_ok=True)
    rendered_path = render_dir / "rendered.flv"
    video_info = probe_media(config.ffprobe_bin, video_path)
    video_duration = max(1.0, float(video_info.duration_seconds or 1.0))
    total_duration = video_duration

    def _on_progress(ratio: float, _current_seconds: float) -> None:
        report_progress(
            "preparing",
            max(progress_floor, min(100, progress_floor + int(ratio * (100 - progress_floor)))),
            "Đang chuẩn bị luồng RTMP",
        )

    if audio_path is not None:
        audio_info = probe_media(config.ffprobe_bin, audio_path)
        total_duration = max(1.0, float(audio_info.duration_seconds or video_duration))
        arguments = [
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-shortest",
            "-f",
            "flv",
            str(rendered_path),
        ]
    else:
        arguments = [
            "-y",
            "-i",
            str(video_path),
            "-c",
            "copy",
            "-shortest",
            "-f",
            "flv",
            str(rendered_path),
        ]

    report_progress("preparing", progress_floor, "Bắt đầu ghép media", force=True)
    _run_ffmpeg_with_progress(
        config,
        arguments,
        working_dir=render_dir,
        total_duration_seconds=total_duration,
        progress_callback=_on_progress,
        lifecycle_guard=lifecycle_guard,
        supervisor=supervisor,
    )
    report_progress("preparing", 100, "Đã chuẩn bị xong luồng", force=True)
    return rendered_path


def _wait_until_start(stream: dict, report_progress, *, lifecycle_guard=None) -> None:
    start_time_live = _parse_control_plane_datetime(stream.get("start_time_live"))
    if start_time_live is None:
        return

    waiting_message = f"Chờ tới giờ live {start_time_live:%d/%m/%Y %H:%M}"
    report_progress("waiting", 100, waiting_message, force=True)

    while True:
        if lifecycle_guard:
            lifecycle_guard(force=True)
        now = _now_local()
        diff = start_time_live - now
        remaining_seconds = diff.total_seconds()
        if remaining_seconds <= 0:
            break

        report_progress("waiting", 100, waiting_message)
        if remaining_seconds > 300:
            sleep_seconds = 15.0
        elif remaining_seconds > 60:
            sleep_seconds = 5.0
        elif remaining_seconds > 15:
            sleep_seconds = 1.0
        elif remaining_seconds > 5:
            sleep_seconds = 0.5
        elif remaining_seconds > 1:
            sleep_seconds = 0.2
        else:
            sleep_seconds = 0.05
        time.sleep(min(sleep_seconds, remaining_seconds))


def _stream_once(
    client: httpx.Client,
    config: WorkerConfig,
    *,
    stream_id: str,
    rendered_path: Path,
    rendered_duration: float,
    rtmp_target: str,
    report_progress,
    end_time_live: datetime | None,
    lifecycle_guard=None,
    expected_playback_mode: str | None = None,
    supervisor: LiveSupervisor | None = None,
) -> str:
    def _on_progress(ratio: float, current_seconds: float) -> None:
        report_progress(
            "streaming",
            max(0, min(100, int(ratio * 100))),
            f"\u0110ang live {time.strftime('%H:%M:%S', time.gmtime(max(0, int(current_seconds))))}",
        )

    ffmpeg_arguments = [
        "-re",
        "-f",
        "flv",
        "-stream_loop",
        "-1",
        "-i",
        str(rendered_path),
        "-c",
        "copy",
        "-f",
        "flv",
        "-flvflags",
        "no_duration_filesize",
        rtmp_target,
    ]
    completed_message = "\u0110\u00e3 ph\u00e1t xong 1 v\u00f2ng media (-c copy)"

    stream_result = _run_ffmpeg_with_progress(
        config,
        ffmpeg_arguments,
        working_dir=rendered_path.parent,
        total_duration_seconds=rendered_duration,
        progress_callback=_on_progress,
        end_time_live=end_time_live,
        lifecycle_guard=lifecycle_guard,
        expected_playback_mode=expected_playback_mode,
        supervisor=supervisor,
    )
    if stream_result == "completed":
        report_progress("streaming", 100, completed_message, force=True)
    return stream_result


def _run_hot_standby_backup_loop(
    client: httpx.Client,
    config: WorkerConfig,
    *,
    stream: dict,
    stream_id: str,
    rendered_path: Path,
    rendered_duration: float,
    rtmp_target: str,
    report_progress,
    lifecycle_guard=None,
    supervisor: LiveSupervisor | None = None,
) -> None:
    standby_message = "Backup đã sẵn sàng, đang chờ tiếp quản luồng"
    report_progress("waiting", 100, standby_message, force=True)

    rtmp_retry_count = 0
    while True:
        runtime_state = lifecycle_guard(force=True) if lifecycle_guard else None
        playback_mode = str(getattr(runtime_state, "playback_mode", "") or "standby").strip().lower() if runtime_state is not None else "standby"
        if playback_mode != "stream":
            report_progress("waiting", 100, standby_message)
            time.sleep(0.5)
            continue

        try:
            stream_result = _stream_once(
                client,
                config,
                stream_id=stream_id,
                rendered_path=rendered_path,
                rendered_duration=rendered_duration,
                rtmp_target=rtmp_target,
                report_progress=report_progress,
                end_time_live=None,
                lifecycle_guard=lifecycle_guard,
                expected_playback_mode="stream",
                supervisor=supervisor,
            )
        except RuntimeError as exc:
            if not _should_retry_rtmp_output_error(stream, exc):
                raise
            rtmp_retry_count += 1
            retry_delay = LIVE_RTMP_RETRY_DELAY_SECONDS
            if supervisor is not None:
                supervisor.record_event(
                    "rtmp_retry",
                    role="backup",
                    attempt=rtmp_retry_count,
                    delay_seconds=retry_delay,
                    error=str(exc),
                )
            print(
                (
                    "[live] backup RTMP output disconnected; retrying "
                    f"stream_id={stream_id} "
                    f"attempt={rtmp_retry_count} "
                    f"delay_seconds={retry_delay:.1f}: {exc}"
                ),
                flush=True,
            )
            report_progress(
                "disconnected",
                0,
                f"Backup m\u1ea5t k\u1ebft n\u1ed1i RTMP, th\u1eed n\u1ed1i l\u1ea1i l\u1ea7n {rtmp_retry_count} sau {int(retry_delay)} gi\u00e2y",
                force=True,
            )
            _sleep_before_rtmp_retry(
                delay_seconds=retry_delay,
                end_time_live=None,
                lifecycle_guard=lifecycle_guard,
            )
            report_progress(
                "streaming",
                0,
                f"Backup \u0111ang n\u1ed1i l\u1ea1i RTMP l\u1ea7n {rtmp_retry_count}",
                force=True,
            )
            continue
        if stream_result == "paused":
            rtmp_retry_count = 0
            report_progress("waiting", 100, standby_message, force=True)
            continue
        if stream_result == "ended":
            break
        rtmp_retry_count = 0


def run_live_stream(client: httpx.Client, config: WorkerConfig, stream: dict) -> None:
    stream_id = str(stream["id"])
    live_root = config.work_root / "live-streams"
    supervisor = LiveSupervisor(
        root=config.work_root / "live-state" / stream_id,
        worker_id=config.worker_id,
        stream_id=stream_id,
    )
    work_dir = live_root / stream_id
    downloads_dir = work_dir / "downloads"
    normalized_dir = work_dir / "normalized"
    render_dir = work_dir / "render"
    activity_path = work_dir / "activity.touch"
    rendered_path: Path | None = None
    live_root.mkdir(parents=True, exist_ok=True)
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    report_progress = _make_progress_reporter(
        client,
        config,
        stream_id,
        activity_path=activity_path,
        supervisor=supervisor,
    )
    lifecycle_guard = _make_live_runtime_guard(client, config, stream_id)
    _touch_activity(activity_path)
    supervisor.record_event("run_started", stream_name=str(stream.get("stream_name") or ""))

    try:
        lifecycle_guard(force=True)
        video_path, audio_path = _download_live_assets(
            stream,
            downloads_dir,
            report_progress,
            lifecycle_guard=lifecycle_guard,
        )
        _try_upload_live_preview_thumbnail(
            client,
            config,
            stream=stream,
            stream_id=stream_id,
            video_source_path=video_path,
            work_dir=work_dir,
        )
        video_path, normalized_video = _maybe_normalize_live_video(
            config,
            stream_id=stream_id,
            video_path=video_path,
            audio_path=audio_path,
            normalized_dir=normalized_dir,
            report_progress=report_progress,
            lifecycle_guard=lifecycle_guard,
            supervisor=supervisor,
        )
        rendered_path = _prepare_rendered_media(
            config,
            video_path=video_path,
            audio_path=audio_path,
            render_dir=render_dir,
            report_progress=report_progress,
            lifecycle_guard=lifecycle_guard,
            progress_floor=96 if normalized_video else 0,
            supervisor=supervisor,
        )
        shutil.rmtree(downloads_dir, ignore_errors=True)

        _wait_until_start(stream, report_progress, lifecycle_guard=lifecycle_guard)

        lifecycle_guard(force=True)
        rendered_info = probe_media(config.ffprobe_bin, rendered_path)
        rendered_duration = max(1.0, float(rendered_info.duration_seconds or 1.0))
        end_time_live = _parse_control_plane_datetime(stream.get("end_time_live"))
        target = _rtmp_target(stream)

        if _is_hot_standby_backup_stream(stream):
            _run_hot_standby_backup_loop(
                client,
                config,
                stream=stream,
                stream_id=stream_id,
                rendered_path=rendered_path,
                rendered_duration=rendered_duration,
                rtmp_target=target,
                report_progress=report_progress,
                lifecycle_guard=lifecycle_guard,
                supervisor=supervisor,
            )
            supervisor.record_event("run_finished", result="backup_loop_returned")
            return

        report_progress("streaming", 0, "Bắt đầu đẩy RTMP", force=True)
        rtmp_retry_count = 0
        while True:
            if end_time_live and _now_local() >= end_time_live:
                break
            try:
                stream_result = _stream_once(
                    client,
                    config,
                    stream_id=stream_id,
                    rendered_path=rendered_path,
                    rendered_duration=rendered_duration,
                    rtmp_target=target,
                    report_progress=report_progress,
                    end_time_live=end_time_live,
                    lifecycle_guard=lifecycle_guard,
                    supervisor=supervisor,
                )
            except RuntimeError as exc:
                if not _should_retry_rtmp_output_error(stream, exc):
                    raise
                if end_time_live and _now_local() >= end_time_live:
                    break
                rtmp_retry_count += 1
                retry_delay = LIVE_RTMP_RETRY_DELAY_SECONDS
                supervisor.record_event(
                    "rtmp_retry",
                    role="primary",
                    attempt=rtmp_retry_count,
                    delay_seconds=retry_delay,
                    error=str(exc),
                )
                print(
                    (
                        "[live] RTMP output disconnected; retrying "
                        f"stream_id={stream_id} "
                        f"attempt={rtmp_retry_count} "
                        f"delay_seconds={retry_delay:.1f}: {exc}"
                    ),
                    flush=True,
                )
                report_progress(
                    "disconnected",
                    0,
                    f"Mất kết nối RTMP, thử nối lại lần {rtmp_retry_count} sau {int(retry_delay)} giây",
                    force=True,
                )
                _sleep_before_rtmp_retry(
                    delay_seconds=retry_delay,
                    end_time_live=end_time_live,
                    lifecycle_guard=lifecycle_guard,
                )
                if end_time_live and _now_local() >= end_time_live:
                    break
                report_progress(
                    "streaming",
                    0,
                    f"Đang nối lại RTMP lần {rtmp_retry_count}",
                    force=True,
                )
                continue
            if stream_result == "ended":
                break
            rtmp_retry_count = 0
            if end_time_live and _now_local() >= end_time_live:
                break
        complete_live_stream(client, config, stream_id, message="Luồng live đã kết thúc theo lịch.")
        supervisor.record_event("run_finished", result="completed")
    except Exception as exc:
        supervisor.record_event("run_failed", error=str(exc))
        raise
    finally:
        if not config.keep_job_dirs and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


def simulate_live_stream(client: httpx.Client, config: WorkerConfig, stream: dict) -> None:
    stream_id = str(stream["id"])
    phases = [
        ("downloading", [10, 40, 70, 100], "Đang tải media"),
        ("preparing", [10, 45, 80, 100], "Đang chuẩn bị luồng"),
        ("waiting", [100], "Chờ đến giờ live"),
        ("streaming", [10, 45, 80, 100], "Đang live"),
    ]
    for status, values, message in phases:
        for value in values:
            update_live_stream_progress(
                client,
                config,
                stream_id,
                status=status,
                progress=value,
                message=message,
            )
            time.sleep(config.simulate_step_seconds)
    complete_live_stream(client, config, stream_id, message="Luồng live demo đã kết thúc.")
