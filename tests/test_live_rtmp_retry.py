from __future__ import annotations

import unittest
import sys
import types

sys.modules.setdefault("gdown", types.ModuleType("gdown"))
from workers.agent.live_runner import _is_retriable_rtmp_output_error, _should_retry_rtmp_output_error


class LiveRtmpRetryTests(unittest.TestCase):
    def test_treats_ffmpeg_broken_pipe_as_retriable_rtmp_disconnect(self) -> None:
        exc = RuntimeError(
            "FFmpeg live runtime failed (224).\n"
            "[out#0/flv] Error muxing a packet\n"
            "[out#0/flv] Error writing trailer: Broken pipe"
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


if __name__ == "__main__":
    unittest.main()
