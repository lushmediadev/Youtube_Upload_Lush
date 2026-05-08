import os
import unittest
from datetime import datetime

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import ChannelRecord, RenderJobRecord, UserSummary, WorkerRecord
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


def make_worker() -> WorkerRecord:
    return WorkerRecord(
        id="worker-11",
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


def make_channel(channel_id: str, name: str) -> ChannelRecord:
    return ChannelRecord(
        id=channel_id,
        name=name,
        channel_id=f"UC-{channel_id}",
        worker_id="worker-11",
        worker_name="62.169.23.188",
        manager_name="trieudo",
        status="connected",
    )


def make_job(
    job_id: str,
    title: str,
    channel_id: str,
    channel_name: str,
    *,
    queue_order: int,
    created_at: datetime,
) -> RenderJobRecord:
    return RenderJobRecord(
        id=job_id,
        title=title,
        source_mode="drive",
        channel_id=channel_id,
        channel_name=channel_name,
        worker_name="worker-11",
        manager_name="trieudo",
        status="pending",
        queue_order=queue_order,
        created_at=created_at,
        scheduled_at=created_at,
        source_label="Google Drive/cloud",
    )


class WorkerJobClaimingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TestableStore()
        self.store.workers = [make_worker()]
        self.store.users = [
            UserSummary(id="user-hoangmai", username="hoangmai", display_name="hoangmai", role="user"),
            UserSummary(id="user-other", username="other", display_name="other", role="user"),
        ]
        self.store.channels = [
            make_channel("channel-coffee", "Coffee Jazz Moments"),
            make_channel("channel-forest", "Dreamy Forest Jazz"),
        ]
        self.store.channel_user_links = [
            {"id": 1, "channel_id": "channel-coffee", "user_id": "user-hoangmai"},
            {"id": 2, "channel_id": "channel-forest", "user_id": "user-other"},
        ]

    def test_claim_prefers_queue_order_before_round_robin_owner_rotation(self) -> None:
        older_waiting_job = make_job(
            "job-vd5",
            "vd 5",
            "channel-coffee",
            "Coffee Jazz Moments",
            queue_order=1,
            created_at=datetime(2026, 5, 8, 14, 4),
        )
        newer_same_owner_job = make_job(
            "job-vd6",
            "vd 6",
            "channel-coffee",
            "Coffee Jazz Moments",
            queue_order=2,
            created_at=datetime(2026, 5, 8, 15, 3),
        )
        self.store.jobs = [older_waiting_job, newer_same_owner_job]
        self.store.worker_round_robin_cursor["worker-11"] = "user:user-hoangmai"

        _, claimed = self.store.claim_next_job("worker-11", self.store.get_worker_shared_secret())

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, "job-vd5")
        self.assertEqual(self.store.jobs[0].status, "queueing")
        self.assertEqual(self.store.jobs[1].status, "pending")

    def test_claim_rotates_between_multiple_owners_but_keeps_each_owner_queue_order(self) -> None:
        first_owner_oldest_job = make_job(
            "job-vd5",
            "vd 5",
            "channel-coffee",
            "Coffee Jazz Moments",
            queue_order=1,
            created_at=datetime(2026, 5, 8, 14, 4),
        )
        second_owner_first_job = make_job(
            "job-dreamy-1",
            "DreamyForestJazz 1",
            "channel-forest",
            "Dreamy Forest Jazz",
            queue_order=2,
            created_at=datetime(2026, 5, 8, 15, 3),
        )
        second_owner_second_job = make_job(
            "job-dreamy-2",
            "DreamyForestJazz 2",
            "channel-forest",
            "Dreamy Forest Jazz",
            queue_order=3,
            created_at=datetime(2026, 5, 8, 15, 5),
        )
        self.store.jobs = [first_owner_oldest_job, second_owner_second_job, second_owner_first_job]
        self.store.worker_round_robin_cursor["worker-11"] = "user:user-hoangmai"

        _, claimed = self.store.claim_next_job("worker-11", self.store.get_worker_shared_secret())

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, "job-dreamy-1")
        self.assertEqual(self.store.jobs[0].status, "pending")
        self.assertEqual(self.store.jobs[1].status, "pending")
        self.assertEqual(self.store.jobs[2].status, "queueing")


if __name__ == "__main__":
    unittest.main()
