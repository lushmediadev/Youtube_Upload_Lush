import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import LiveStreamRecord, UserSummary, WorkerRecord
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


def make_live_worker(worker_id: str) -> WorkerRecord:
    now = datetime(2026, 5, 11, 21, 0)
    return WorkerRecord(
        id=worker_id,
        name="62.146.168.126",
        manager_id="manager-1",
        manager_name="thanh",
        group="workers",
        created_at=now,
        status="online",
        capacity=1,
        load_percent=0,
        bandwidth_kbps=0,
        disk_used_gb=0,
        disk_total_gb=100,
        threads=1,
    )


def make_stream(*, status: str, lease_expires_at: datetime) -> LiveStreamRecord:
    now = datetime(2026, 5, 11, 21, 0)
    return LiveStreamRecord(
        id="live-test",
        owner_user_id="user-1",
        owner_username="user1",
        owner_display_name="user1",
        manager_id="manager-1",
        manager_name="thanh",
        primary_worker_id="live-worker-01",
        primary_worker_name="62.146.168.126",
        stream_name="test live",
        stream_key="stream-key",
        video_url="https://example.com/video.mp4",
        status=status,
        created_at=now,
        updated_at=now,
        claimed_by_worker_id="live-worker-01",
        claimed_at=now,
        lease_expires_at=lease_expires_at,
    )


class LiveRuntimeLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TestableStore()
        self.store.users = [UserSummary(id="user-1", username="user1", display_name="user1", role="user")]
        self.store.live_workers = [make_live_worker("live-worker-01")]
        self.store.live_user_worker_links = [
            {"id": 1, "user_id": "user-1", "worker_id": "live-worker-01", "threads": 1, "role": "primary"}
        ]
        self.store.live_streams = []

    def test_runtime_state_refreshes_claimed_stream_lease(self) -> None:
        expired_lease = datetime(2000, 1, 1)
        self.store.live_streams = [make_stream(status="preparing", lease_expires_at=expired_lease)]

        state = self.store.get_live_stream_runtime_state(
            stream_id="live-test",
            worker_id="live-worker-01",
            shared_secret=self.store.get_worker_shared_secret(),
        )

        self.assertFalse(state["should_stop"])
        refreshed_lease = self.store.live_streams[0].lease_expires_at
        self.assertIsNotNone(refreshed_lease)
        self.assertGreater(refreshed_lease, datetime.now() + timedelta(seconds=30))

    def test_disconnected_progress_keeps_worker_claim_and_refreshes_lease(self) -> None:
        expired_lease = datetime(2000, 1, 1)
        self.store.live_streams = [make_stream(status="streaming", lease_expires_at=expired_lease)]

        updated = self.store.update_live_stream_progress(
            stream_id="live-test",
            worker_id="live-worker-01",
            shared_secret=self.store.get_worker_shared_secret(),
            status="disconnected",
            progress=0,
            message="Mất kết nối RTMP, đang thử nối lại",
        )

        self.assertEqual(updated.status, "disconnected")
        self.assertEqual(updated.claimed_by_worker_id, "live-worker-01")
        self.assertEqual(updated.log_label, "Mất kết nối")
        refreshed_lease = self.store.live_streams[0].lease_expires_at
        self.assertIsNotNone(refreshed_lease)
        self.assertGreater(refreshed_lease, datetime.now() + timedelta(seconds=30))


if __name__ == "__main__":
    unittest.main()
