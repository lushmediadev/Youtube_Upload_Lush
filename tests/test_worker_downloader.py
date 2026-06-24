from __future__ import annotations

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

from workers.agent import downloader


class FakeProcess:
    def __init__(self, return_code: int | None = None) -> None:
        self.return_code = return_code
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = -15

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9

    def wait(self, timeout: float | None = None) -> int:
        return int(self.return_code or 0)


class WorkerDownloaderTests(unittest.TestCase):
    def test_wait_for_download_process_terminates_after_stall(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mp4"
            process = FakeProcess()

            with (
                patch.object(downloader.time, "monotonic", side_effect=[0.0, 0.4, 1.1]),
                patch.object(downloader.time, "sleep"),
            ):
                with self.assertRaisesRegex(TimeoutError, "khong co du lieu moi"):
                    downloader._wait_for_download_process(
                        process,
                        target,
                        stall_timeout_seconds=1.0,
                        poll_interval_seconds=0.1,
                    )

            self.assertTrue(process.terminated)
            self.assertFalse(process.killed)

    def test_gdown_attempt_terminates_when_progress_callback_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mp4"
            process = FakeProcess()

            def cancel_callback(ratio: float, message: str | None) -> None:
                raise RuntimeError("job cancelled")

            with (
                patch.object(downloader.subprocess, "Popen", return_value=process),
                patch.object(
                    downloader,
                    "_wait_for_download_process",
                    side_effect=RuntimeError("job cancelled"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "job cancelled"):
                    downloader._run_gdown_attempt(
                        "https://drive.google.com/file/d/example/view",
                        target,
                        stall_timeout_seconds=1,
                        progress_callback=cancel_callback,
                    )

            self.assertTrue(process.terminated)

    def test_google_drive_retry_removes_partial_file_before_next_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mp4"
            calls: list[bool] = []

            def fake_attempt(url: str, attempt_target: Path, **_: object) -> Path:
                calls.append(attempt_target.exists())
                if len(calls) == 1:
                    attempt_target.write_bytes(b"partial")
                    raise TimeoutError("stalled")
                attempt_target.write_bytes(b"complete")
                return attempt_target

            with (
                patch.object(downloader, "_run_gdown_attempt", side_effect=fake_attempt),
                patch.object(downloader.time, "sleep"),
            ):
                result = downloader._download_google_drive_with_retries(
                    "https://drive.google.com/file/d/example/view",
                    target,
                    attempts=3,
                    retry_delay_seconds=0,
                    stall_timeout_seconds=1,
                )

            self.assertEqual(calls, [False, False])
            self.assertEqual(result.read_bytes(), b"complete")

    def test_google_drive_retry_raises_after_attempt_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "video.mp4"

            with (
                patch.object(
                    downloader,
                    "_run_gdown_attempt",
                    side_effect=TimeoutError("stalled"),
                ) as run_attempt,
                patch.object(downloader.time, "sleep"),
            ):
                with self.assertRaisesRegex(RuntimeError, "sau 3 lan"):
                    downloader._download_google_drive_with_retries(
                        "https://drive.google.com/file/d/example/view",
                        target,
                        attempts=3,
                        retry_delay_seconds=0,
                        stall_timeout_seconds=1,
                    )

            self.assertEqual(run_attempt.call_count, 3)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
