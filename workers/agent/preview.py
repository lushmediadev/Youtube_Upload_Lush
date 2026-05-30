from __future__ import annotations

import subprocess
from pathlib import Path

from .config import WorkerConfig
from .ffmpeg_pipeline import probe_media


def capture_video_preview(
    config: WorkerConfig,
    *,
    source_path: Path,
    destination_path: Path,
) -> Path:
    media_info = probe_media(config.ffprobe_bin, source_path)
    if not media_info.has_video:
        raise ValueError(f"Asset {source_path.name} khong co video stream.")

    duration_seconds = max(0.0, float(media_info.duration_seconds or 0.0))
    seek_seconds = 0.0
    if duration_seconds > 2:
        seek_seconds = min(max(duration_seconds * 0.15, 1.0), max(duration_seconds - 0.5, 0.0))

    command = [
        config.ffmpeg_bin,
        "-y",
        "-ss",
        f"{seek_seconds:.3f}",
        "-i",
        str(source_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=480:-2:force_original_aspect_ratio=decrease",
        str(destination_path),
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or not destination_path.exists():
        tail = "\n".join((result.stderr or result.stdout or "").splitlines()[-20:])
        raise RuntimeError(f"Khong the tao snapshot preview.\n{tail}".strip())
    return destination_path
