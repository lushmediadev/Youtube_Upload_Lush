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


if __name__ == "__main__":
    unittest.main()
