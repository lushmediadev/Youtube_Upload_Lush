import os
import json
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import ChannelRecord, LiveStreamRecord, RenderJobRecord, UserSummary, WorkerRecord
from backend.app.store import AppStore


class TestableStore(AppStore):
    def __init__(self) -> None:
        self.sent_alerts: list[tuple[str | None, str]] = []
        self.saved_count = 0
        self.telegram_send_result = True
        super().__init__()

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
        self.saved_count += 1

    def _send_telegram_alert(self, message: str, *, chat_id: str | None = None) -> bool:
        self.sent_alerts.append((chat_id, message))
        return self.telegram_send_result


def make_worker(worker_id: str, manager_id: str, manager_name: str, created_at: datetime) -> WorkerRecord:
    return WorkerRecord(
        id=worker_id,
        name=worker_id,
        manager_id=manager_id,
        manager_name=manager_name,
        group=manager_name,
        created_at=created_at,
        status="online",
        capacity=4,
        load_percent=0,
        bandwidth_kbps=0,
        disk_used_gb=0,
        disk_total_gb=100,
        threads=1,
        last_seen_at=created_at,
    )


def make_channel(channel_id: str, worker_id: str, manager_name: str) -> ChannelRecord:
    return ChannelRecord(
        id=channel_id,
        name=channel_id,
        channel_id=f"UC-{channel_id}",
        worker_id=worker_id,
        worker_name=worker_id,
        manager_name=manager_name,
        status="connected",
    )


def make_job(job_id: str, channel_id: str, worker_id: str, created_at: datetime) -> RenderJobRecord:
    return RenderJobRecord(
        id=job_id,
        title=job_id,
        source_mode="drive",
        channel_id=channel_id,
        channel_name=channel_id,
        worker_name=worker_id,
        manager_name="manager-alpha",
        status="completed",
        created_at=created_at,
        source_label="Google Drive/cloud",
    )


def make_stream(
    stream_id: str,
    owner_user_id: str,
    primary_worker_id: str,
    backup_worker_id: str | None,
    created_at: datetime,
) -> LiveStreamRecord:
    return LiveStreamRecord(
        id=stream_id,
        owner_user_id=owner_user_id,
        owner_username=owner_user_id,
        owner_display_name=owner_user_id,
        manager_id="manager-1",
        manager_name="manager-alpha",
        primary_worker_id=primary_worker_id,
        primary_worker_name=primary_worker_id,
        backup_worker_id=backup_worker_id,
        backup_worker_name=backup_worker_id,
        stream_name=stream_id,
        stream_key="stream-key",
        video_url="https://example.com/video.mp4",
        status="ended",
        created_at=created_at,
        updated_at=created_at,
    )


class InactiveBotAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 5, 4, 8, 5)
        self.store = TestableStore()
        self.store.users = [
            UserSummary(id="admin-1", username="admin", display_name="Admin", role="admin"),
            UserSummary(id="manager-1", username="manager-alpha", display_name="Manager Alpha", role="manager"),
            UserSummary(
                id="user-1",
                username="demo-user",
                display_name="Demo User",
                role="user",
                manager_id="manager-1",
                manager_name="manager-alpha",
            ),
        ]
        self.store.user_meta = {
            "admin-1": {"telegram": "100"},
            "manager-1": {"telegram": "200"},
            "user-1": {"telegram": ""},
        }
        self.store.jobs = []
        self.store.live_streams = []
        self.store.channels = []
        self.store.channel_user_links = []
        self.store.workers = []
        self.store.live_workers = []
        self.store.user_worker_links = []
        self.store.live_user_worker_links = []

    def test_collects_upload_live_primary_and_live_backup_allocations_without_recent_jobs(self) -> None:
        old = self.now - timedelta(days=20)
        recent = self.now - timedelta(days=2)
        stale_job = self.now - timedelta(days=12)

        self.store.workers = [
            make_worker("worker-upload-old", "manager-1", "manager-alpha", old),
            make_worker("worker-upload-active", "manager-1", "manager-alpha", old),
        ]
        self.store.live_workers = [
            make_worker("live-primary-old", "manager-1", "manager-alpha", old),
            make_worker("live-backup-old", "manager-1", "manager-alpha", old),
            make_worker("live-primary-new", "manager-1", "manager-alpha", recent),
        ]
        self.store.user_worker_links = [
            {"id": 1, "user_id": "user-1", "worker_id": "worker-upload-old", "created_at": old},
            {"id": 2, "user_id": "user-1", "worker_id": "worker-upload-active", "created_at": old},
        ]
        self.store.live_user_worker_links = [
            {"id": 1, "user_id": "user-1", "worker_id": "live-primary-old", "live_role": "primary", "created_at": old},
            {"id": 2, "user_id": "user-1", "worker_id": "live-backup-old", "live_role": "backup", "created_at": old},
            {"id": 3, "user_id": "user-1", "worker_id": "live-primary-new", "live_role": "primary", "created_at": recent},
        ]
        self.store.channels = [
            make_channel("channel-old", "worker-upload-old", "manager-alpha"),
            make_channel("channel-active", "worker-upload-active", "manager-alpha"),
        ]
        self.store.channel_user_links = [
            {"id": 1, "channel_id": "channel-old", "user_id": "user-1"},
            {"id": 2, "channel_id": "channel-active", "user_id": "user-1"},
        ]
        self.store.jobs = [
            make_job("job-old", "channel-old", "worker-upload-old", stale_job),
            make_job("job-recent", "channel-active", "worker-upload-active", recent),
        ]
        self.store.live_streams = [
            make_stream("stream-primary-old", "user-1", "live-primary-old", None, stale_job),
            make_stream("stream-backup-old", "user-1", "live-primary-new", "live-backup-old", stale_job),
            make_stream("stream-primary-new", "user-1", "live-primary-new", None, recent),
        ]

        inactive = self.store.get_inactive_bot_allocations(now=self.now, days=10)

        self.assertEqual(
            {(item["worker_id"], item["bot_type"]) for item in inactive},
            {
                ("worker-upload-old", "upload"),
                ("live-primary-old", "live_primary"),
                ("live-backup-old", "live_backup"),
            },
        )

    def test_bot_rows_only_show_users_inactive_over_threshold(self) -> None:
        old = self.now - timedelta(days=20)
        recent = self.now - timedelta(days=2)
        stale_job = self.now - timedelta(days=12)
        self.store._now = lambda trim=True: self.now
        self.store.users.append(
            UserSummary(
                id="user-2",
                username="active-user",
                display_name="Active User",
                role="user",
                manager_id="manager-1",
                manager_name="manager-alpha",
            )
        )
        self.store.user_meta["user-2"] = {"telegram": ""}
        self.store.workers = [
            make_worker("worker-upload-mixed", "manager-1", "manager-alpha", old),
        ]
        self.store.user_worker_links = [
            {"id": 1, "user_id": "user-1", "worker_id": "worker-upload-mixed", "created_at": old},
            {"id": 2, "user_id": "user-2", "worker_id": "worker-upload-mixed", "created_at": old},
        ]
        self.store.channels = [
            make_channel("channel-stale", "worker-upload-mixed", "manager-alpha"),
            make_channel("channel-active", "worker-upload-mixed", "manager-alpha"),
        ]
        self.store.channel_user_links = [
            {"id": 1, "channel_id": "channel-stale", "user_id": "user-1"},
            {"id": 2, "channel_id": "channel-active", "user_id": "user-2"},
        ]
        self.store.jobs = [
            make_job("job-stale", "channel-stale", "worker-upload-mixed", stale_job),
            make_job("job-active", "channel-active", "worker-upload-mixed", recent),
        ]

        rows = self.store._build_bot_rows()

        self.assertEqual(rows[0]["inactive_days"], 12)
        self.assertTrue(rows[0]["inactive_days_alert"])
        self.assertEqual(rows[0]["inactive_days_label"], "demo-user: 12 ngày")
        self.assertEqual(rows[0]["inactive_users"], [{"username": "demo-user", "days": 12}])

    def test_daily_alert_routes_all_inactive_bots_to_admin_and_only_owned_bots_to_each_manager(self) -> None:
        old = self.now - timedelta(days=20)
        self.store.users.append(
            UserSummary(id="manager-2", username="manager-beta", display_name="Manager Beta", role="manager")
        )
        self.store.user_meta["manager-2"] = {"telegram": "300"}
        self.store.users.append(
            UserSummary(
                id="user-2",
                username="beta-user",
                display_name="Beta User",
                role="user",
                manager_id="manager-2",
                manager_name="manager-beta",
            )
        )
        self.store.user_meta["user-2"] = {"telegram": ""}
        self.store.workers = [
            make_worker("worker-alpha", "manager-1", "manager-alpha", old),
            make_worker("worker-beta", "manager-2", "manager-beta", old),
        ]
        self.store.user_worker_links = [
            {"id": 1, "user_id": "user-1", "worker_id": "worker-alpha", "created_at": old},
            {"id": 2, "user_id": "user-2", "worker_id": "worker-beta", "created_at": old},
        ]

        self.store._reconcile_inactive_bot_daily_alert(now=datetime(2026, 5, 4, 7, 59))
        self.assertEqual(self.store.sent_alerts, [])

        self.store._reconcile_inactive_bot_daily_alert(now=self.now)

        self.assertEqual([chat_id for chat_id, _ in self.store.sent_alerts], ["100", "200", "300"])
        admin_message = self.store.sent_alerts[0][1]
        alpha_message = self.store.sent_alerts[1][1]
        beta_message = self.store.sent_alerts[2][1]
        self.assertIn("Tổng BOT: 2", admin_message)
        self.assertIn("worker-alpha", admin_message)
        self.assertIn("worker-beta", admin_message)
        self.assertIn("worker-alpha", alpha_message)
        self.assertNotIn("worker-beta", alpha_message)
        self.assertIn("worker-beta", beta_message)
        self.assertNotIn("worker-alpha", beta_message)
        self.assertEqual(self.store.inactive_bot_alert_last_sent_on, "2026-05-04")

        self.store._reconcile_inactive_bot_daily_alert(now=datetime(2026, 5, 4, 8, 30))
        self.assertEqual(len(self.store.sent_alerts), 3)

    def test_daily_alert_routes_manager_by_id_when_worker_manager_name_is_display_label(self) -> None:
        old = self.now - timedelta(days=20)
        self.store.users[1] = UserSummary(
            id="manager-1",
            username="manager-alpha",
            display_name="Manager Alpha",
            role="manager",
        )
        self.store.user_meta["manager-1"] = {"telegram": "200"}
        self.store.workers = [
            make_worker("worker-alpha", "manager-1", "Manager Alpha", old),
        ]
        self.store.user_worker_links = [
            {"id": 1, "user_id": "user-1", "worker_id": "worker-alpha", "created_at": old},
        ]

        self.store._reconcile_inactive_bot_daily_alert(now=self.now)

        self.assertEqual([chat_id for chat_id, _ in self.store.sent_alerts], ["100", "200"])

    def test_failed_daily_alert_is_attempted_once_per_day(self) -> None:
        old = self.now - timedelta(days=20)
        self.store.telegram_send_result = False
        self.store.workers = [
            make_worker("worker-alpha", "manager-1", "manager-alpha", old),
        ]
        self.store.user_worker_links = [
            {"id": 1, "user_id": "user-1", "worker_id": "worker-alpha", "created_at": old},
        ]

        self.store._reconcile_inactive_bot_daily_alert(now=self.now)
        self.store._reconcile_inactive_bot_daily_alert(now=datetime(2026, 5, 4, 8, 30))

        self.assertEqual(len(self.store.sent_alerts), 2)
        self.assertEqual(self.store.inactive_bot_alert_last_attempted_on, "2026-05-04")
        self.assertIsNone(self.store.inactive_bot_alert_last_sent_on)

    def test_notify_telegram_chat_ids_splits_long_messages(self) -> None:
        long_message = "\n".join([f"line-{index}-" + ("x" * 500) for index in range(12)])

        delivered = self.store._notify_telegram_chat_ids(["100"], long_message)

        self.assertTrue(delivered)
        self.assertGreater(len(self.store.sent_alerts), 1)
        self.assertTrue(all(len(message) <= self.store.TELEGRAM_MESSAGE_CHUNK_LIMIT for _, message in self.store.sent_alerts))

    def test_state_serializes_datetime_mapping_links_created_by_bot_assignment(self) -> None:
        created_at = datetime(2026, 5, 4, 8, 15)
        self.store.user_worker_links = [
            {
                "id": 1,
                "user_id": "user-1",
                "worker_id": "worker-upload",
                "threads": 1,
                "bot_type": "1080p",
                "note": "VPS được cấp",
                "created_at": created_at,
            }
        ]
        self.store.live_user_worker_links = [
            {
                "id": 2,
                "user_id": "user-1",
                "worker_id": "live-worker",
                "live_role": "primary",
                "created_at": created_at,
            }
        ]

        payload = self.store._serialize_state()

        json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["user_worker_links"][0]["created_at"], "2026-05-04T08:15:00")
        self.assertEqual(payload["live_user_worker_links"][0]["created_at"], "2026-05-04T08:15:00")


if __name__ == "__main__":
    unittest.main()
