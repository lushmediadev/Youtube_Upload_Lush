from __future__ import annotations

import unittest
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("gdown", types.ModuleType("gdown"))
from workers.agent import live_runner
from workers.agent.live_runner import (
    _build_live_ffmpeg_arguments,
    _env_bool,
    _env_float,
    _is_retriable_rtmp_output_error,
    _should_retry_rtmp_output_error,
)


class LiveRtmpRetryTests(unittest.TestCase):
    def test_live_rtmp_retry_delay_allows_zero_delay(self) -> None:
        with patch.dict("os.environ", {"WORKER_LIVE_RTMP_RETRY_DELAY_SECONDS": "0"}):
            self.assertEqual(_env_float("WORKER_LIVE_RTMP_RETRY_DELAY_SECONDS", 20.0, minimum=0.0), 0.0)

    def test_env_bool_parses_fifo_flag(self) -> None:
        with patch.dict("os.environ", {"WORKER_LIVE_RTMP_FIFO_ENABLED": "true"}):
            self.assertTrue(_env_bool("WORKER_LIVE_RTMP_FIFO_ENABLED"))

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

    def test_retries_any_ffmpeg_live_runtime_failure_like_legacy_worker(self) -> None:
        exc = RuntimeError(
            "FFmpeg live runtime failed (152).\n"
            "[tls] The specified session has been invalidated for some reason."
        )

        self.assertTrue(_is_retriable_rtmp_output_error(exc))

    def test_does_not_retry_non_ffmpeg_errors(self) -> None:
        self.assertFalse(_is_retriable_rtmp_output_error(RuntimeError("download failed")))

    def test_retries_timed_primary_stream_when_parallel_backup_exists(self) -> None:
        exc = RuntimeError(
            "FFmpeg live runtime failed (224).\n"
            "[out#0/flv] Error writing trailer: Broken pipe"
        )

        self.assertTrue(
            _should_retry_rtmp_output_error(
                {
                    "runtime_role": "primary",
                    "is_runtime_clone": False,
                    "backup_worker_id": "live-worker-backup",
                    "end_time_live": (datetime.now() + timedelta(hours=2)).isoformat(),
                },
                exc,
            )
        )

    def test_retries_forever_primary_stream_when_failover_backup_exists(self) -> None:
        exc = RuntimeError(
            "FFmpeg live runtime failed (224).\n"
            "[out#0/flv] Error writing trailer: Broken pipe"
        )

        self.assertTrue(
            _should_retry_rtmp_output_error(
                {
                    "runtime_role": "primary",
                    "is_runtime_clone": False,
                    "backup_worker_id": "live-worker-backup",
                    "is_forever": True,
                    "end_time_live": None,
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

    def test_stream_once_uses_ffmpeg_stream_loop_instead_of_python_looping_media(self) -> None:
        statuses: list[str] = []

        def report_progress(status: str, progress: int, message: str, *, force: bool = False) -> None:
            statuses.append(status)

        with patch.object(live_runner, "_run_ffmpeg_with_progress", return_value="paused") as run_ffmpeg:
            result = live_runner._stream_once(
                client=None,
                config=SimpleNamespace(ffmpeg_bin="ffmpeg"),
                stream_id="live-test",
                rendered_path=Path("rendered.flv"),
                rendered_duration=60.0,
                rtmp_target="rtmps://a.rtmps.youtube.com/live2/key",
                report_progress=report_progress,
                end_time_live=None,
            )

        self.assertEqual(result, "paused")
        ffmpeg_args = run_ffmpeg.call_args.args[1]
        self.assertIn("-stream_loop", ffmpeg_args)
        self.assertEqual(ffmpeg_args[ffmpeg_args.index("-stream_loop") + 1], "-1")
        self.assertLess(ffmpeg_args.index("-stream_loop"), ffmpeg_args.index("-i"))
        self.assertIn("-flvflags", ffmpeg_args)
        self.assertIn("no_duration_filesize", ffmpeg_args)

    def test_fifo_command_is_opt_in_and_passes_flv_options_to_fifo_muxer(self) -> None:
        with patch.object(live_runner, "LIVE_RTMP_FIFO_ENABLED", True):
            ffmpeg_args = _build_live_ffmpeg_arguments(
                rendered_path=Path("rendered.flv"),
                rtmp_target="rtmps://a.rtmps.youtube.com/live2/key",
            )

        self.assertIn("-f", ffmpeg_args)
        self.assertEqual(ffmpeg_args[ffmpeg_args.index("-f", ffmpeg_args.index("-c")) + 1], "fifo")
        self.assertIn("-fifo_format", ffmpeg_args)
        self.assertEqual(ffmpeg_args[ffmpeg_args.index("-fifo_format") + 1], "flv")
        self.assertIn("-attempt_recovery", ffmpeg_args)
        self.assertIn("-recover_any_error", ffmpeg_args)
        self.assertIn("-format_opts", ffmpeg_args)
        self.assertEqual(ffmpeg_args[ffmpeg_args.index("-format_opts") + 1], "flvflags=no_duration_filesize")


if __name__ == "__main__":
    unittest.main()
