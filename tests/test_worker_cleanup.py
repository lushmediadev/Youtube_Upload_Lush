import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from workers.agent.cleanup import cleanup_stale_worker_artifacts
from workers.agent.config import WorkerConfig


def make_config(work_root: Path) -> WorkerConfig:
    return WorkerConfig(
        runtime_mode="upload",
        control_plane_url="https://control.example",
        shared_secret="secret",
        worker_id="worker-1",
        worker_name="worker-1",
        manager_name="manager",
        group="workers",
        capacity=1,
        threads=1,
        heartbeat_seconds=15,
        poll_seconds=5,
        browser_session_poll_seconds=15,
        decommission_poll_seconds=60,
        simulate_jobs=False,
        execute_jobs=True,
        simulate_step_seconds=1.0,
        work_root=work_root,
        keep_job_dirs=False,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        youtube_upload_enabled=True,
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
        live_normalize_max_height=1440,
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


class WorkerCleanupTests(unittest.TestCase):
    def test_cleanup_removes_stale_youtube_upload_download_staging_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            work_root = root / "worker-data"
            home = root / "home"
            stale_dir = home / "Downloads" / "youtube-upload-lush" / "job-stale"
            fresh_dir = home / "Downloads" / "youtube-upload-lush" / "job-fresh"
            stale_dir.mkdir(parents=True)
            fresh_dir.mkdir(parents=True)
            (stale_dir / "video.mp4").write_text("stale", encoding="utf-8")
            (fresh_dir / "video.mp4").write_text("fresh", encoding="utf-8")
            old = time.time() - (8 * 3600)
            os.utime(stale_dir / "video.mp4", (old, old))
            os.utime(stale_dir, (old, old))

            with patch.dict(os.environ, {"WORKER_TEMP_RETENTION_HOURS": "6"}, clear=False):
                with patch("workers.agent.cleanup.Path.home", return_value=home):
                    result = cleanup_stale_worker_artifacts(make_config(work_root))

            self.assertEqual(result["removed_browser_upload_download_dirs"], 1)
            self.assertFalse(stale_dir.exists())
            self.assertTrue(fresh_dir.exists())

    def test_cleanup_removes_live_state_dirs_after_retention(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work_root = Path(temp_dir) / "worker-data"
            stale_dir = work_root / "live-state" / "live-stale"
            fresh_dir = work_root / "live-state" / "live-fresh"
            stale_dir.mkdir(parents=True)
            fresh_dir.mkdir(parents=True)
            (stale_dir / "events.log").write_text("stale", encoding="utf-8")
            (fresh_dir / "events.log").write_text("fresh", encoding="utf-8")
            old = time.time() - (8 * 24 * 3600)
            os.utime(stale_dir / "events.log", (old, old))
            os.utime(stale_dir, (old, old))

            with patch.dict(os.environ, {"WORKER_LIVE_STATE_RETENTION_HOURS": "168"}, clear=False):
                result = cleanup_stale_worker_artifacts(make_config(work_root))

            self.assertEqual(result["removed_live_state_dirs"], 1)
            self.assertFalse(stale_dir.exists())
            self.assertTrue(fresh_dir.exists())


if __name__ == "__main__":
    unittest.main()
