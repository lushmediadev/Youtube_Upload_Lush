import os
import unittest
from datetime import datetime

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import ChannelRecord, JobCreatePayload, UserSummary, WorkerRecord
from backend.app.store import AppStore


class TestableStore(AppStore):
    def _ensure_state_db(self) -> None:
        return None

    def _ensure_auth_tables(self) -> None:
        return None

    def _load_or_seed_state(self) -> None:
        return None

    def _bootstrap_auth_tables_from_memory_if_empty(self) -> None:
        return None

    def _load_auth_state_from_tables(self) -> None:
        return None

    def _save_auth_state(self) -> None:
        return None

    def _save_state(self) -> None:
        return None


class JobCreationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TestableStore()
        self.store.users = [
            UserSummary(id="user-1", username="hoangmai", display_name="hoangmai", role="user"),
        ]
        self.store.workers = [
            WorkerRecord(
                id="worker-1",
                name="62.169.23.188",
                manager_id="manager-1",
                manager_name="trieudo",
                group="workers",
                created_at=datetime(2026, 5, 8, 9, 0),
                status="online",
                capacity=1,
                load_percent=0,
                bandwidth_kbps=0,
                disk_used_gb=0,
                disk_total_gb=100,
                threads=1,
            )
        ]
        self.store.channels = [
            ChannelRecord(
                id="channel-1",
                name="Coffee Jazz Moments",
                channel_id="UC-channel-1",
                worker_id="worker-1",
                worker_name="62.169.23.188",
                manager_name="trieudo",
                status="connected",
            )
        ]
        self.store.channel_user_links = [{"id": 1, "channel_id": "channel-1", "user_id": "user-1"}]
        self.store.user_worker_links = [{"id": 1, "user_id": "user-1", "worker_id": "worker-1"}]

    def test_create_job_rejects_blank_title_at_store_layer(self) -> None:
        payload = JobCreatePayload(
            channel_id="channel-1",
            title="   ",
            source_mode="drive",
            video_loop_url="https://drive.google.com/file/d/video-id/view",
        )

        with self.assertRaisesRegex(ValueError, "Tên video là bắt buộc"):
            self.store.create_job("user-1", payload, {})


if __name__ == "__main__":
    unittest.main()
