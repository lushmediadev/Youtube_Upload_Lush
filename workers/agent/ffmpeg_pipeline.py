from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import WorkerConfig


ProgressCallback = Callable[[int, str | None], None]


@dataclass
class MediaInfo:
    path: Path
    duration_seconds: float
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    frame_rate: float | None
    bit_rate_bps: int | None
    format_name: str | None
    video_codec: str | None
    audio_codec: str | None


@dataclass
class RenderResult:
    output_path: Path


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return value.strip(".-") or "out"


def parse_duration_string(value: str) -> float:
    parts = [segment.strip() for segment in value.split(":")]
    if len(parts) != 3:
        raise ValueError("time_render_string phải theo dạng HH:MM:SS.")
    hours, minutes, seconds = parts
    total_seconds = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    if total_seconds <= 0:
        raise ValueError("time_render_string phải lớn hơn 0.")
    return float(total_seconds)


def _parse_frame_rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    return float(value)


def probe_media(ffprobe_bin: str, path: Path) -> MediaInfo:
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    format_info = payload.get("format") or {}
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    duration = float(format_info.get("duration") or 0.0)
    if duration <= 0:
        raise ValueError(f"Không đọc được duration của file {path.name}.")

    bit_rate_bps: int | None = None
    try:
        parsed_bit_rate = int(float(format_info.get("bit_rate") or 0.0))
        if parsed_bit_rate > 0:
            bit_rate_bps = parsed_bit_rate
    except (TypeError, ValueError):
        bit_rate_bps = None
    if bit_rate_bps is None:
        try:
            bit_rate_bps = int((path.stat().st_size * 8) / duration)
        except OSError:
            bit_rate_bps = None

    return MediaInfo(
        path=path,
        duration_seconds=duration,
        has_video=video_stream is not None,
        has_audio=audio_stream is not None,
        width=int(video_stream.get("width")) if video_stream and video_stream.get("width") else None,
        height=int(video_stream.get("height")) if video_stream and video_stream.get("height") else None,
        frame_rate=_parse_frame_rate((video_stream or {}).get("r_frame_rate")),
        bit_rate_bps=bit_rate_bps,
        format_name=str(format_info.get("format_name") or "").strip() or None,
        video_codec=str((video_stream or {}).get("codec_name") or "").strip() or None,
        audio_codec=str((audio_stream or {}).get("codec_name") or "").strip() or None,
    )


def _run_ffmpeg(
    ffmpeg_bin: str,
    arguments: list[str],
    *,
    working_dir: Path,
    total_duration_seconds: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    command = [ffmpeg_bin, "-progress", "pipe:1", "-nostats", *arguments]
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
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                output_lines.append(line)
            if not on_progress or not total_duration_seconds:
                continue
            if line.startswith("out_time_ms="):
                try:
                    current_seconds = int(line.split("=", 1)[1]) / 1_000_000
                except ValueError:
                    continue
                on_progress(max(0.0, min(1.0, current_seconds / total_duration_seconds)))
    finally:
        return_code = process.wait()
    if return_code != 0:
        tail = "\n".join(output_lines[-40:])
        raise RuntimeError(f"FFmpeg failed ({return_code}).\n{tail}")


def _prepare_video_ts(
    config: WorkerConfig,
    source_path: Path,
    destination_path: Path,
    *,
    total_duration_seconds: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    _run_ffmpeg(
        config.ffmpeg_bin,
        ["-y", "-i", str(source_path), "-c", "copy", str(destination_path)],
        working_dir=destination_path.parent,
        total_duration_seconds=total_duration_seconds,
        on_progress=on_progress,
    )


def _prepare_audio_asset(
    source_path: Path,
    destination_path: Path,
    *,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    shutil.copy2(source_path, destination_path)
    if on_progress:
        on_progress(1.0)


def _cut_media_copy(
    config: WorkerConfig,
    source_path: Path,
    destination_path: Path,
    cut_seconds: float,
) -> None:
    _run_ffmpeg(
        config.ffmpeg_bin,
        ["-y", "-i", str(source_path), "-c", "copy", "-t", f"{cut_seconds:.3f}", str(destination_path)],
        working_dir=destination_path.parent,
        total_duration_seconds=cut_seconds,
    )


def _build_sequence(
    *,
    intro_name: str | None,
    loop_name: str,
    outro_name: str | None,
    target_seconds: float,
    duration_lookup: dict[str, float],
    partial_name_factory: Callable[[int], str],
    partial_builder: Callable[[str, str, float], None],
) -> list[str]:
    sequence: list[str] = []
    accumulated = 0.0
    reserved_outro = duration_lookup.get(outro_name, 0.0) if outro_name else 0.0

    if intro_name:
        intro_duration = duration_lookup[intro_name]
        if target_seconds <= intro_duration:
            raise ValueError("Intro dài hơn hoặc bằng thời lượng output.")
        sequence.append(intro_name)
        accumulated += intro_duration

    if accumulated + reserved_outro >= target_seconds:
        raise ValueError("Intro/Outro dài hơn hoặc bằng thời lượng output.")

    loop_duration = duration_lookup[loop_name]
    if loop_duration <= 0:
        raise ValueError("Loop asset có duration không hợp lệ.")

    partial_index = 0
    while accumulated + reserved_outro < target_seconds:
        remaining = target_seconds - accumulated - reserved_outro
        if loop_duration < remaining:
            sequence.append(loop_name)
            accumulated += loop_duration
            continue

        partial_name = partial_name_factory(partial_index)
        partial_builder(loop_name, partial_name, remaining)
        sequence.append(partial_name)
        accumulated += remaining
        partial_index += 1
        break

    if outro_name:
        sequence.append(outro_name)

    return sequence


def render_job_assets(
    config: WorkerConfig,
    job: dict,
    asset_paths: dict[str, Path],
    working_dir: Path,
    progress_callback: ProgressCallback | None = None,
) -> RenderResult:
    working_dir.mkdir(parents=True, exist_ok=True)
    render_dir = working_dir / "render"
    render_dir.mkdir(parents=True, exist_ok=True)

    target_seconds = parse_duration_string(job["time_render_string"])
    video_sources = {slot: path for slot, path in asset_paths.items() if slot in {"intro", "video_loop", "outro"} and path}
    audio_sources = {slot: path for slot, path in asset_paths.items() if slot == "audio_loop" and path}
    if "video_loop" not in video_sources:
        raise ValueError("Job không có video_loop.")

    probed: dict[str, MediaInfo] = {
        slot: probe_media(config.ffprobe_bin, path)
        for slot, path in asset_paths.items()
        if path.exists()
    }

    for slot, media in probed.items():
        if slot in {"intro", "video_loop", "outro"} and slot in video_sources:
            if not media.has_video:
                audio_marker = " MP3" if str(media.audio_codec or "").strip().lower() == "mp3" else ""
                if audio_marker:
                    raise ValueError(f"Asset {slot} đang là file{audio_marker}, không có video stream.")
                raise ValueError(f"Asset {slot} không có video stream.")
            if media.frame_rate is not None and media.frame_rate != round(media.frame_rate):
                raise ValueError(f"Asset {slot} có FPS không phải số nguyên ({media.frame_rate}).")

    if "audio_loop" not in audio_sources:
        for slot, media in probed.items():
            if slot in video_sources and not media.has_audio:
                raise ValueError(f"Asset video {slot} không có audio stream khi thiếu audio_loop.")

    video_dimensions = {
        (media.width, media.height)
        for slot, media in probed.items()
        if slot in video_sources
    }
    if len(video_dimensions) > 1:
        raise ValueError("Các video không cùng kích thước khung hình.")

    last_progress = 0

    def _emit_render_progress(progress: int, message: str) -> None:
        nonlocal last_progress
        if not progress_callback:
            return
        bounded = max(last_progress, min(progress, 99))
        last_progress = bounded
        progress_callback(bounded, message)

    _emit_render_progress(5, "Dang chuan bi render output")

    prepare_steps: list[tuple[str, str, Path]] = [
        *[("video", slot, path) for slot, path in video_sources.items()],
        *[("audio", slot, path) for slot, path in audio_sources.items()],
    ]
    total_prepare_steps = max(len(prepare_steps), 1)
    prepare_start = 8
    prepare_end = 68

    def _prepare_progress_handler(step_index: int, slot: str) -> tuple[int, int, Callable[[float], None]]:
        step_start = prepare_start + int(((prepare_end - prepare_start) * step_index) / total_prepare_steps)
        step_end = prepare_start + int(((prepare_end - prepare_start) * (step_index + 1)) / total_prepare_steps)

        def _on_progress(ratio: float) -> None:
            local_ratio = max(0.0, min(1.0, ratio))
            _emit_render_progress(
                step_start + int(local_ratio * max(step_end - step_start, 1)),
                f"Dang chuan hoa {slot}",
            )

        return step_start, step_end, _on_progress

    video_ts_names: dict[str, str] = {}
    for step_index, (kind, slot, source_path) in enumerate(prepare_steps):
        if kind != "video":
            continue
        target_name = f"{slot}.ts"
        step_start, step_end, on_progress = _prepare_progress_handler(step_index, slot)
        _emit_render_progress(step_start, f"Dang chuan hoa {slot}")
        _prepare_video_ts(
            config,
            source_path,
            render_dir / target_name,
            total_duration_seconds=probed[slot].duration_seconds,
            on_progress=on_progress,
        )
        _emit_render_progress(step_end, f"Da chuan hoa xong {slot}")
        video_ts_names[slot] = target_name

    audio_asset_names: dict[str, str] = {}
    if "audio_loop" in audio_sources:
        for step_index, (kind, slot, source_path) in enumerate(prepare_steps):
            if kind != "audio":
                continue
            target_name = f"{slot}{source_path.suffix.lower() or '.mp3'}"
            step_start, step_end, on_progress = _prepare_progress_handler(step_index, slot)
            _emit_render_progress(step_start, f"Dang chuan bi {slot}")
            _prepare_audio_asset(
                source_path,
                render_dir / target_name,
                on_progress=on_progress,
            )
            _emit_render_progress(step_end, f"Da san sang {slot}")
            audio_asset_names[slot] = target_name

    _emit_render_progress(70, "Dang lap sequence render")
    video_durations = {name: probe_media(config.ffprobe_bin, render_dir / name).duration_seconds for name in video_ts_names.values()}
    audio_durations = {name: probe_media(config.ffprobe_bin, render_dir / name).duration_seconds for name in audio_asset_names.values()}

    _emit_render_progress(73, "Dang lap sequence video")
    video_sequence = _build_sequence(
        intro_name=video_ts_names.get("intro"),
        loop_name=video_ts_names["video_loop"],
        outro_name=video_ts_names.get("outro"),
        target_seconds=target_seconds,
        duration_lookup=video_durations,
        partial_name_factory=lambda index: f"video-cut-{index}.ts",
        partial_builder=lambda source_name, target_name, duration: _cut_media_copy(
            config,
            render_dir / source_name,
            render_dir / target_name,
            duration,
        ),
    )

    audio_sequence: list[str] = []
    if "audio_loop" in audio_asset_names:
        _emit_render_progress(77, "Dang lap sequence audio")
        audio_loop_suffix = Path(audio_asset_names["audio_loop"]).suffix or ".mp3"
        audio_sequence = _build_sequence(
            intro_name=audio_asset_names.get("intro"),
            loop_name=audio_asset_names["audio_loop"],
            outro_name=audio_asset_names.get("outro"),
            target_seconds=target_seconds,
            duration_lookup=audio_durations,
            partial_name_factory=lambda index: f"audio-cut-{index}{audio_loop_suffix}",
            partial_builder=lambda source_name, target_name, duration: _cut_media_copy(
                config,
                render_dir / source_name,
                render_dir / target_name,
                duration,
            ),
        )

    videos_txt = render_dir / "videos.txt"
    videos_txt.write_text("\n".join(f"file '{name}'" for name in video_sequence), encoding="ascii")

    audios_txt: Path | None = None
    if audio_sequence:
        audios_txt = render_dir / "audios.txt"
        audios_txt.write_text("\n".join(f"file '{name}'" for name in audio_sequence), encoding="ascii")

    output_name = f"{_safe_name(str(job.get('title') or job.get('id') or 'out'))}.mp4"
    output_path = render_dir / output_name

    _emit_render_progress(80, "Dang concat output")

    if audios_txt:
        ffmpeg_args = [
            "-y",
            "-an",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(videos_txt),
            "-vn",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(audios_txt),
            "-c",
            "copy",
            "-t",
            f"{target_seconds:.3f}",
            str(output_path),
        ]
    else:
        ffmpeg_args = [
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(videos_txt),
            "-c",
            "copy",
            "-t",
            f"{target_seconds:.3f}",
            str(output_path),
        ]

    def _on_concat_progress(ratio: float) -> None:
        _emit_render_progress(80 + int(max(0.0, min(1.0, ratio)) * 18), "Dang render output")

    _run_ffmpeg(
        config.ffmpeg_bin,
        ffmpeg_args,
        working_dir=render_dir,
        total_duration_seconds=target_seconds,
        on_progress=_on_concat_progress,
    )

    _emit_render_progress(98, "Da render xong file output")

    return RenderResult(output_path=output_path)
