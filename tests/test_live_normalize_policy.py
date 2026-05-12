import os
import tempfile
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

if "gdown" not in sys.modules:
    gdown = types.ModuleType("gdown")
    gdown.download = lambda *args, **kwargs: None
    sys.modules["gdown"] = gdown

from workers.agent.config import WorkerConfig, load_config
from workers.agent.ffmpeg_pipeline import MediaInfo
from workers.agent.live_runner import _resolve_live_video_normalize_plan


def make_config(work_root: Path) -> WorkerConfig:
    return WorkerConfig(
        runtime_mode="live",
        control_plane_url="https://control.example",
        shared_secret="secret",
        worker_id="worker-live-1",
        worker_name="worker-live-1",
        manager_name="manager",
        group="workers",
        capacity=1,
        threads=1,
        heartbeat_seconds=15,
        poll_seconds=5,
        simulate_jobs=False,
        execute_jobs=True,
        simulate_step_seconds=1.0,
        work_root=work_root,
        keep_job_dirs=False,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        youtube_upload_enabled=False,
        youtube_upload_chunk_bytes=8_388_608,
        browser_public_base_url="",
        browser_session_enabled=False,
        browser_display_base=90,
        browser_vnc_port_base=5990,
        browser_web_port_base=6090,
        browser_debug_port_base=9222,
        live_normalize_enabled=True,
        live_normalize_concurrency=1,
        live_normalize_threads=2,
        live_normalize_preset="veryfast",
        live_normalize_max_height=2160,
        live_normalize_1080_maxrate_kbps=6000,
        live_normalize_1440_maxrate_kbps=13000,
        live_normalize_2160_maxrate_kbps=20000,
        live_normalize_1080_crf=23,
        live_normalize_1440_crf=22,
        live_normalize_2160_crf=21,
        live_normalize_audio_bitrate_kbps=128,
        network_retry_base_seconds=3.0,
        network_retry_max_seconds=30.0,
        progress_retry_attempts=3,
    )


def media_info(
    *,
    width: int,
    height: int,
    frame_rate: float = 30.0,
    bit_rate_bps: int = 45_000_000,
    video_codec: str = "h264",
    audio_codec: str = "aac",
) -> MediaInfo:
    return MediaInfo(
        path=Path("source.mp4"),
        duration_seconds=60.0,
        has_video=True,
        has_audio=True,
        width=width,
        height=height,
        frame_rate=frame_rate,
        bit_rate_bps=bit_rate_bps,
        format_name="mov,mp4,m4a,3gp,3g2,mj2",
        video_codec=video_codec,
        audio_codec=audio_codec,
    )


class LiveNormalizePolicyTests(unittest.TestCase):
    def test_live_normalize_is_disabled_by_default_until_reenabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "CONTROL_PLANE_URL": "https://control.example",
                    "WORKER_SHARED_SECRET": "secret",
                    "WORKER_RUNTIME_MODE": "live",
                    "WORKER_DATA_DIR": temp_dir,
                },
                clear=True,
            ):
                config = load_config()

        self.assertFalse(config.live_normalize_enabled)

    def test_4k_source_uses_2160_profile_without_downscale(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = _resolve_live_video_normalize_plan(
                make_config(Path(temp_dir)),
                media_info(width=3840, height=2160, frame_rate=30.0, bit_rate_bps=45_000_000),
                has_external_audio=False,
            )

        self.assertTrue(plan.normalize_required)
        self.assertEqual(plan.profile_height, 2160)
        self.assertIsNone(plan.scale_height)
        self.assertEqual(plan.maxrate_kbps, 20000)
        self.assertEqual(plan.crf, 21)
        self.assertEqual(plan.gop_frames, 60)

    def test_source_above_4k_is_capped_to_2160_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = _resolve_live_video_normalize_plan(
                make_config(Path(temp_dir)),
                media_info(width=7680, height=4320, frame_rate=25.0, bit_rate_bps=80_000_000),
                has_external_audio=False,
            )

        self.assertTrue(plan.normalize_required)
        self.assertEqual(plan.profile_height, 2160)
        self.assertEqual(plan.scale_height, 2160)
        self.assertEqual(plan.maxrate_kbps, 20000)
        self.assertEqual(plan.gop_frames, 50)

    def test_4k_source_within_profile_limits_stays_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plan = _resolve_live_video_normalize_plan(
                make_config(Path(temp_dir)),
                media_info(width=3840, height=2160, frame_rate=30.0, bit_rate_bps=18_000_000),
                has_external_audio=False,
            )

        self.assertFalse(plan.normalize_required)
        self.assertEqual(plan.profile_height, 2160)
        self.assertIsNone(plan.scale_height)
        self.assertEqual(plan.maxrate_kbps, 20000)


if __name__ == "__main__":
    unittest.main()
