import os
import sys
import types
import unittest
from unittest.mock import patch


if "gdown" not in sys.modules:
    gdown = types.ModuleType("gdown")
    gdown.download = lambda *args, **kwargs: None
    sys.modules["gdown"] = gdown

from backend.app import worker_bootstrap
from workers.agent import live_runner


class LiveTimezoneTests(unittest.TestCase):
    def test_live_runner_defaults_to_ho_chi_minh_timezone(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(live_runner._app_timezone().key, "Asia/Ho_Chi_Minh")

    def test_live_runner_falls_back_when_runtime_timezone_is_legacy_or_missing(self) -> None:
        with patch.dict(os.environ, {"APP_TIMEZONE": "Asia/Saigon"}):
            self.assertEqual(live_runner._app_timezone().key, "Asia/Ho_Chi_Minh")

    def test_worker_bootstrap_env_sets_ho_chi_minh_timezone_for_new_live_bots(self) -> None:
        request = worker_bootstrap.WorkerBootstrapRequest(
            vps_ip="127.0.0.1",
            ssh_user="root",
            control_plane_url="https://example.test",
            shared_secret="secret",
            worker_id="live-worker-test",
            worker_name="live-worker-test",
            runtime_mode="live",
        )

        env_file = worker_bootstrap._build_worker_env_file(request)

        self.assertIn("APP_TIMEZONE=Asia/Ho_Chi_Minh", env_file)
        self.assertNotIn("Asia/Saigon", env_file)


if __name__ == "__main__":
    unittest.main()
