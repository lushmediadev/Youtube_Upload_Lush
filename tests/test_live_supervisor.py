import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.modules.setdefault("gdown", ModuleType("gdown"))

from workers.agent import live_runner
from workers.agent.live_supervisor import LiveSupervisor
from workers.agent.live_supervisor import DEFAULT_FFMPEG_LOG_MAX_BYTES
from workers.agent.live_supervisor import DEFAULT_SUPERVISOR_LOG_MAX_BYTES


class LiveSupervisorTests(unittest.TestCase):
    def test_records_current_state_events_and_ffmpeg_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = LiveSupervisor(
                root=Path(tmp),
                worker_id="live-worker-01",
                stream_id="stream-1",
            )

            supervisor.record_state("downloading", 15, "Đang tải nguồn", event="phase")
            supervisor.append_ffmpeg_line("frame=1")
            supervisor.record_state("streaming", 80, "Đang live", event="phase")

            current = json.loads((Path(tmp) / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(current["stream_id"], "stream-1")
            self.assertEqual(current["worker_id"], "live-worker-01")
            self.assertEqual(current["status"], "streaming")
            self.assertEqual(current["progress"], 80)

            events = (Path(tmp) / "events.log").read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(events), 2)
            self.assertEqual(json.loads(events[-1])["status"], "streaming")
            self.assertIn("frame=1", (Path(tmp) / "ffmpeg.log").read_text(encoding="utf-8"))

    def test_progress_reporter_keeps_local_state_when_control_plane_progress_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = LiveSupervisor(
                root=Path(tmp),
                worker_id="live-worker-01",
                stream_id="stream-1",
            )
            reporter = live_runner._make_progress_reporter(
                httpx.Client(),
                SimpleNamespace(worker_id="live-worker-01"),
                "stream-1",
                supervisor=supervisor,
            )

            with patch.object(live_runner, "update_live_stream_progress", side_effect=RuntimeError("control-plane down")):
                reporter("streaming", 33, "Đang live", force=True)

            current = json.loads((Path(tmp) / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(current["status"], "streaming")
            self.assertEqual(current["progress"], 33)
            event_text = (Path(tmp) / "events.log").read_text(encoding="utf-8")
            self.assertIn("progress_report_failed", event_text)

    def test_progress_reporter_throttles_routine_progress(self) -> None:
        sent: list[tuple[str, int, str | None]] = []
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = LiveSupervisor(
                root=Path(tmp),
                worker_id="live-worker-01",
                stream_id="stream-1",
            )
            reporter = live_runner._make_progress_reporter(
                httpx.Client(),
                SimpleNamespace(worker_id="live-worker-01"),
                "stream-1",
                supervisor=supervisor,
            )

            with patch.object(
                live_runner,
                "update_live_stream_progress",
                side_effect=lambda _client, _config, _stream_id, *, status, progress, message, **_kwargs: sent.append((status, progress, message)),
            ):
                reporter("streaming", 0, "Đang live", force=True)
                reporter("streaming", 1, "Đang live")
                reporter("streaming", 2, "Đang live")

            self.assertEqual(len(sent), 1)
            events = (Path(tmp) / "events.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(events), 1)

    def test_progress_reporter_forwards_runtime_health_payload(self) -> None:
        sent: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = LiveSupervisor(
                root=Path(tmp),
                worker_id="live-worker-01",
                stream_id="stream-1",
            )
            reporter = live_runner._make_progress_reporter(
                httpx.Client(),
                SimpleNamespace(worker_id="live-worker-01"),
                "stream-1",
                supervisor=supervisor,
            )

            def fake_update(_client, _config, _stream_id, **kwargs) -> None:
                sent.append(kwargs)

            with patch.object(live_runner, "update_live_stream_progress", side_effect=fake_update):
                reporter(
                    "streaming",
                    0,
                    "Primary RTMP loi keo dai",
                    force=True,
                    runtime_health="rtmp_unhealthy",
                    runtime_health_elapsed_seconds=31.5,
                    runtime_health_message="FFmpeg no progress",
                )

            self.assertEqual(sent[0]["runtime_health"], "rtmp_unhealthy")
            self.assertEqual(sent[0]["runtime_health_elapsed_seconds"], 31.5)
            self.assertEqual(sent[0]["runtime_health_message"], "FFmpeg no progress")

    def test_supervisor_log_caps_are_small_for_many_streams(self) -> None:
        self.assertLessEqual(DEFAULT_SUPERVISOR_LOG_MAX_BYTES, 1 * 1024 * 1024)
        self.assertLessEqual(DEFAULT_FFMPEG_LOG_MAX_BYTES, 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
