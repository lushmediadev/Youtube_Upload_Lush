from __future__ import annotations

import unittest
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("gdown", types.ModuleType("gdown"))
from workers.agent import live_runner
from workers.agent.live_runner import _is_retriable_rtmp_output_error, _should_retry_rtmp_output_error


class LiveRtmpRetryTests(unittest.TestCase):
    def test_treats_ffmpeg_broken_pipe_as_retriable_rtmp_disconnect(self) -> None:
        exc = RuntimeError(
            "FFmpeg live runtime failed (224).\n"
            "[out#0/flv] Error muxing a packet\n"
            "[out#0/flv] Error writing trailer: Broken pipe"
        )

        self.assertTrue(_is_retriable_rtmp_output_error(exc))

    def test_treats_ffmpeg_connection_reset_as_retriable_rtmp_disconnect(self) -> None:
        exc = RuntimeError(
            "FFmpeg live runtime failed (224).\n"
            "RTMP output disconnected\n"
            "Error submitting a packet to the muxer: Connection reset by peer"
        )

        self.assertTrue(_is_retriable_rtmp_output_error(exc))

    def test_does_not_retry_non_ffmpeg_errors(self) -> None:
        self.assertFalse(_is_retriable_rtmp_output_error(RuntimeError("download failed")))

    def test_does_not_retry_primary_stream_when_failover_backup_exists(self) -> None:
        exc = RuntimeError(
            "FFmpeg live runtime failed (224).\n"
            "[out#0/flv] Error writing trailer: Broken pipe"
        )

        self.assertFalse(
            _should_retry_rtmp_output_error(
                {
                    "runtime_role": "primary",
                    "is_runtime_clone": False,
                    "backup_worker_id": "live-worker-backup",
                },
                exc,
            )
        )

    def test_retries_primary_stream_without_failover_backup(self) -> None:
        exc = RuntimeError(
            "FFmpeg live runtime failed (224).\n"
            "[out#0/flv] Error writing trailer: Broken pipe"
        )

        self.assertTrue(
            _should_retry_rtmp_output_error(
                {
                    "runtime_role": "primary",
                    "is_runtime_clone": False,
                    "backup_worker_id": "",
                },
                exc,
            )
        )

    def test_retries_hot_standby_backup_stream_after_rtmp_disconnect(self) -> None:
        calls: list[str] = []
        statuses: list[str] = []

        def fake_stream_once(*args, **kwargs) -> str:
            calls.append("stream")
            if len(calls) == 1:
                raise RuntimeError(
                    "FFmpeg live runtime failed (224).\n"
                    "RTMP output disconnected\n"
                    "Error submitting a packet to the muxer: Connection reset by peer\n"
                    "Error writing trailer: Connection reset by peer"
                )
            return "ended"

        def report_progress(status: str, progress: int, message: str, *, force: bool = False) -> None:
            statuses.append(status)

        def lifecycle_guard(*, force: bool = False):
            return SimpleNamespace(playback_mode="stream")

        with (
            patch.object(live_runner, "_stream_once", side_effect=fake_stream_once),
            patch.object(live_runner, "_sleep_before_rtmp_retry"),
        ):
            live_runner._run_hot_standby_backup_loop(
                client=None,
                config=object(),
                stream={"runtime_role": "backup", "is_runtime_clone": True},
                stream_id="live-backup",
                rendered_path=Path("rendered.flv"),
                rendered_duration=1.0,
                rtmp_target="rtmps://b.rtmps.youtube.com/live2/key",
                report_progress=report_progress,
                lifecycle_guard=lifecycle_guard,
            )

        self.assertEqual(len(calls), 2)
        self.assertIn("disconnected", statuses)


if __name__ == "__main__":
    unittest.main()
