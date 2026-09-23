import unittest
from types import SimpleNamespace
from unittest.mock import patch

from workers.agent import control_plane


def make_config():
    return SimpleNamespace(
        worker_id="live-worker-test",
        shared_secret="secret",
        live_control_plane_timeout_seconds=3.0,
        progress_retry_attempts=3,
    )


class LiveControlPlaneResilienceTests(unittest.TestCase):
    def test_claim_live_stream_is_single_attempt_with_short_timeout(self) -> None:
        response = SimpleNamespace(json=lambda: {"stream": None})
        with patch.object(control_plane, "_request_with_retry", return_value=response) as request:
            self.assertIsNone(control_plane.claim_live_stream(object(), make_config()))

        kwargs = request.call_args.kwargs
        self.assertFalse(kwargs["retry_forever"])
        self.assertEqual(kwargs["max_attempts"], 1)
        self.assertEqual(kwargs["timeout"], 3.0)

    def test_live_progress_is_not_allowed_to_block_ffmpeg_for_long_retry_window(self) -> None:
        with patch.object(control_plane, "_request_with_retry") as request:
            control_plane.update_live_stream_progress(
                object(),
                make_config(),
                "live-test",
                status="streaming",
                progress=50,
            )

        kwargs = request.call_args.kwargs
        self.assertFalse(kwargs["retry_forever"])
        self.assertEqual(kwargs["max_attempts"], 1)
        self.assertEqual(kwargs["timeout"], 3.0)

    def test_runtime_guard_state_check_is_single_short_attempt(self) -> None:
        response = SimpleNamespace(
            json=lambda: {
                "stream_id": "live-test",
                "status": "streaming",
                "should_stop": False,
                "playback_mode": "stream",
            }
        )
        with patch.object(control_plane, "_request_with_retry", return_value=response) as request:
            state = control_plane.get_live_stream_runtime_state(object(), make_config(), "live-test")

        self.assertEqual(state.stream_id, "live-test")
        kwargs = request.call_args.kwargs
        self.assertFalse(kwargs["retry_forever"])
        self.assertEqual(kwargs["max_attempts"], 1)
        self.assertEqual(kwargs["timeout"], 3.0)


if __name__ == "__main__":
    unittest.main()
