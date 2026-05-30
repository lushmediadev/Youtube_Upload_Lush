import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import LiveStreamRecord, UserSummary, WorkerRecord
from backend.app.store import AppStore


class TestableStore(AppStore):
    def __init__(self) -> None:
        self.live_notifications: list[tuple[list[str], str]] = []
        self.telegram_notifications: list[tuple[list[str], str]] = []
        self.save_calls = 0
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
        self.save_calls += 1

    def _notify_live_telegram_chat_ids(self, chat_ids: list[str], message: str) -> bool:
        self.live_notifications.append((chat_ids, message))
        return True

    def _notify_telegram_chat_ids(self, chat_ids: list[str], message: str) -> bool:
        self.telegram_notifications.append((chat_ids, message))
        return True


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


def make_stream(
    *,
    status: str,
    lease_expires_at: datetime,
    first_streaming_started_at: datetime | None = None,
    end_time_live: datetime | None = None,
) -> LiveStreamRecord:
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
        is_forever=end_time_live is None,
        live_label="Live 24/7" if end_time_live is None else "Live 1h",
        end_time_live=end_time_live,
        status=status,
        created_at=now,
        updated_at=now,
        claimed_by_worker_id="live-worker-01",
        claimed_at=now,
        lease_expires_at=lease_expires_at,
        first_streaming_started_at=first_streaming_started_at,
        streaming_started_at=first_streaming_started_at,
        disconnected_at=now - timedelta(seconds=300) if status == "disconnected" else None,
    )


class LiveRuntimeLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TestableStore()
        self.store.save_calls = 0
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

    def test_streaming_progress_throttles_state_persistence(self) -> None:
        self.store.live_streams = [
            make_stream(
                status="streaming",
                lease_expires_at=datetime.now() + timedelta(minutes=5),
                first_streaming_started_at=datetime(2026, 5, 11, 21, 0),
            )
        ]

        first = self.store.update_live_stream_progress(
            stream_id="live-test",
            worker_id="live-worker-01",
            shared_secret=self.store.get_worker_shared_secret(),
            status="streaming",
            progress=50,
            message="streaming",
        )
        second = self.store.update_live_stream_progress(
            stream_id="live-test",
            worker_id="live-worker-01",
            shared_secret=self.store.get_worker_shared_secret(),
            status="streaming",
            progress=51,
            message="streaming",
        )

        self.assertEqual(first.status, "streaming")
        self.assertEqual(second.progress, 51)
        self.assertEqual(self.store.live_streams[0].progress, 51)
        self.assertEqual(self.store.save_calls, 0)

    def test_live_worker_heartbeat_throttles_state_persistence(self) -> None:
        payload = type(
            "Payload",
            (),
            {
                "worker_id": "live-worker-01",
                "shared_secret": self.store.get_worker_shared_secret(),
                "status": "online",
                "load_percent": 1,
                "ram_percent": 2,
                "ram_used_gb": 0.1,
                "ram_total_gb": 1.0,
                "bandwidth_kbps": 3.0,
                "disk_used_gb": 4.0,
                "disk_total_gb": 100.0,
                "capacity": 1,
                "threads": 1,
                "active_stream_ids": [],
            },
        )()

        self.store.heartbeat_live_worker(payload)
        payload.load_percent = 5
        self.store.heartbeat_live_worker(payload)

        self.assertEqual(self.store.live_workers[0].load_percent, 5)
        self.assertEqual(self.store.save_calls, 0)

    def test_noop_live_claim_does_not_persist_state(self) -> None:
        _worker, claimed = self.store.claim_next_live_stream(
            "live-worker-01",
            self.store.get_worker_shared_secret(),
        )

        self.assertIsNone(claimed)
        self.assertEqual(self.store.save_calls, 0)

    def test_started_disconnected_stream_without_backup_can_be_reclaimed(self) -> None:
        self.store.live_streams = [
            make_stream(
                status="disconnected",
                lease_expires_at=datetime(2000, 1, 1),
                first_streaming_started_at=datetime(2026, 5, 11, 21, 0),
            )
        ]

        self.assertTrue(
            self.store._can_claim_live_stream(
                self.store.live_streams[0],
                worker_id="live-worker-01",
                now=datetime.now(),
            )
        )

    def test_timed_primary_with_active_backup_can_reclaim_after_disconnect(self) -> None:
        end_time = datetime.now() + timedelta(hours=2)
        primary = make_stream(
            status="disconnected",
            lease_expires_at=datetime(2000, 1, 1),
            first_streaming_started_at=datetime(2026, 5, 11, 21, 0),
            end_time_live=end_time,
        )
        primary.backup_worker_id = "live-worker-backup"
        primary.backup_worker_name = "62.146.169.230"
        primary.backup_stream_id = "live-backup"
        backup = make_stream(
            status="streaming",
            lease_expires_at=datetime.now() + timedelta(minutes=5),
            first_streaming_started_at=datetime(2026, 5, 11, 21, 1),
            end_time_live=end_time,
        )
        backup.id = "live-backup"
        backup.primary_worker_id = "live-worker-backup"
        backup.primary_worker_name = "62.146.169.230"
        backup.runtime_role = "backup"
        backup.is_runtime_clone = True
        backup.parent_stream_id = primary.id
        backup.claimed_by_worker_id = "live-worker-backup"
        backup.claimed_by_role = "backup"
        self.store.live_workers.append(make_live_worker("live-worker-backup"))
        self.store.live_user_worker_links.append(
            {"id": 2, "user_id": "user-1", "worker_id": "live-worker-backup", "threads": 1, "live_role": "backup"}
        )
        self.store.live_streams = [primary, backup]

        self.assertTrue(
            self.store._can_claim_live_stream(
                primary,
                worker_id="live-worker-01",
                now=datetime.now(),
            )
        )
        _, claimed = self.store.claim_next_live_stream(
            "live-worker-01",
            self.store.get_worker_shared_secret(),
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, "live-test")

    def test_forever_primary_with_active_backup_keeps_hot_standby_reclaim_policy(self) -> None:
        primary = make_stream(
            status="disconnected",
            lease_expires_at=datetime(2000, 1, 1),
            first_streaming_started_at=datetime(2026, 5, 11, 21, 0),
        )
        primary.backup_worker_id = "live-worker-backup"
        primary.backup_worker_name = "62.146.169.230"
        primary.backup_stream_id = "live-backup"
        backup = make_stream(
            status="streaming",
            lease_expires_at=datetime.now() + timedelta(minutes=5),
            first_streaming_started_at=datetime(2026, 5, 11, 21, 1),
        )
        backup.id = "live-backup"
        backup.primary_worker_id = "live-worker-backup"
        backup.primary_worker_name = "62.146.169.230"
        backup.runtime_role = "backup"
        backup.is_runtime_clone = True
        backup.parent_stream_id = primary.id
        backup.claimed_by_worker_id = "live-worker-backup"
        backup.claimed_by_role = "backup"
        self.store.live_workers.append(make_live_worker("live-worker-backup"))
        self.store.live_user_worker_links.append(
            {"id": 2, "user_id": "user-1", "worker_id": "live-worker-backup", "threads": 1, "live_role": "backup"}
        )
        self.store.live_streams = [primary, backup]

        self.assertTrue(
            self.store._can_claim_live_stream(
                primary,
                worker_id="live-worker-01",
                now=datetime.now(),
            )
        )

    def test_backup_clone_can_reclaim_after_disconnect(self) -> None:
        primary = make_stream(
            status="disconnected",
            lease_expires_at=datetime(2000, 1, 1),
            first_streaming_started_at=datetime(2026, 5, 11, 21, 0),
        )
        primary.backup_worker_id = "live-worker-backup"
        primary.backup_stream_id = "live-backup"
        backup = make_stream(
            status="disconnected",
            lease_expires_at=datetime(2000, 1, 1),
            first_streaming_started_at=datetime(2026, 5, 11, 21, 1),
        )
        backup.id = "live-backup"
        backup.primary_worker_id = "live-worker-backup"
        backup.runtime_role = "backup"
        backup.is_runtime_clone = True
        backup.parent_stream_id = primary.id
        backup.claimed_by_worker_id = None
        backup.claimed_by_role = None
        self.store.live_workers.append(make_live_worker("live-worker-backup"))
        self.store.live_user_worker_links.append(
            {"id": 2, "user_id": "user-1", "worker_id": "live-worker-backup", "threads": 1, "live_role": "backup"}
        )
        self.store.live_streams = [primary, backup]

        self.assertTrue(
            self.store._can_claim_live_stream(
                backup,
                worker_id="live-worker-backup",
                now=datetime.now(),
            )
        )
        _, claimed = self.store.claim_next_live_stream(
            "live-worker-backup",
            self.store.get_worker_shared_secret(),
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, "live-backup")

    def test_timed_primary_can_reclaim_if_backup_clone_is_not_active(self) -> None:
        end_time = datetime.now() + timedelta(hours=2)
        primary = make_stream(
            status="disconnected",
            lease_expires_at=datetime(2000, 1, 1),
            first_streaming_started_at=datetime(2026, 5, 11, 21, 0),
            end_time_live=end_time,
        )
        primary.backup_worker_id = "live-worker-backup"
        primary.backup_stream_id = "live-backup"
        backup = make_stream(
            status="error",
            lease_expires_at=datetime(2000, 1, 1),
            first_streaming_started_at=datetime(2026, 5, 11, 21, 1),
            end_time_live=end_time,
        )
        backup.id = "live-backup"
        backup.primary_worker_id = "live-worker-backup"
        backup.runtime_role = "backup"
        backup.is_runtime_clone = True
        backup.parent_stream_id = primary.id
        backup.claimed_by_worker_id = None
        backup.claimed_by_role = None
        self.store.live_streams = [primary, backup]

        self.assertTrue(
            self.store._can_claim_live_stream(
                primary,
                worker_id="live-worker-01",
                now=datetime.now(),
            )
        )

    def test_missing_heartbeat_after_scheduled_end_notifies_ended_not_disconnected(self) -> None:
        end_time = datetime(2026, 5, 12, 9, 30)
        reconcile_at = datetime(2026, 5, 12, 9, 31, 37)
        self.store.live_streams = [
            make_stream(
                status="streaming",
                lease_expires_at=datetime(2026, 5, 12, 9, 31),
                first_streaming_started_at=datetime(2026, 5, 12, 9, 15, 9),
                end_time_live=end_time,
            )
        ]

        self.store._reconcile_live_streams_from_heartbeat(
            self.store.live_workers[0],
            active_stream_ids=[],
            now=reconcile_at,
        )

        stream = self.store.live_streams[0]
        self.assertEqual(stream.status, "ended")
        self.assertEqual(stream.log_label, "Kết thúc")
        self.assertIsNone(stream.claimed_by_worker_id)
        self.assertEqual(len(self.store.live_notifications), 1)
        self.assertIn("[LIVE] Luồng live đã kết thúc", self.store.live_notifications[0][1])
        self.assertNotIn("mất kết nối", self.store.live_notifications[0][1].casefold())

    def test_startup_grace_skips_false_live_expiry_after_control_plane_restart(self) -> None:
        reconcile_at = datetime(2026, 5, 12, 9, 31, 37)
        self.store._process_started_at = reconcile_at - timedelta(seconds=15)
        self.store.live_streams = [
            make_stream(
                status="streaming",
                lease_expires_at=datetime(2026, 5, 12, 9, 31),
                first_streaming_started_at=datetime(2026, 5, 12, 9, 15, 9),
            )
        ]

        changed = self.store._reconcile_expired_live_streams(now=reconcile_at)

        stream = self.store.live_streams[0]
        self.assertFalse(changed)
        self.assertEqual(stream.status, "streaming")
        self.assertEqual(stream.claimed_by_worker_id, "live-worker-01")
        self.assertEqual(self.store.live_notifications, [])

    def test_missing_heartbeat_marks_live_telemetry_stale_without_releasing_claim(self) -> None:
        reconcile_at = datetime(2026, 5, 13, 16, 55)
        self.store.live_streams = [
            make_stream(
                status="streaming",
                lease_expires_at=reconcile_at - timedelta(seconds=1),
                first_streaming_started_at=datetime(2026, 5, 13, 16, 50),
            )
        ]

        self.store._reconcile_live_streams_from_heartbeat(
            self.store.live_workers[0],
            active_stream_ids=[],
            now=reconcile_at,
        )

        stream = self.store.live_streams[0]
        self.assertEqual(stream.status, "streaming")
        self.assertEqual(stream.claimed_by_worker_id, "live-worker-01")
        self.assertIsNone(stream.disconnected_at)
        self.assertEqual(stream.log_label, "Mất telemetry")
        self.assertIn("Không nhận telemetry", stream.status_message or "")
        self.assertEqual(self.store.live_notifications, [])

    def test_expired_live_lease_marks_telemetry_stale_without_failing_runtime(self) -> None:
        reconcile_at = datetime(2026, 5, 13, 16, 55)
        self.store._process_started_at = reconcile_at - timedelta(hours=1)
        self.store.live_streams = [
            make_stream(
                status="streaming",
                lease_expires_at=reconcile_at - timedelta(seconds=1),
                first_streaming_started_at=datetime(2026, 5, 13, 16, 50),
            )
        ]

        changed = self.store._reconcile_expired_live_streams(now=reconcile_at)

        stream = self.store.live_streams[0]
        self.assertTrue(changed)
        self.assertEqual(stream.status, "streaming")
        self.assertEqual(stream.claimed_by_worker_id, "live-worker-01")
        self.assertEqual(stream.log_label, "Mất telemetry")
        self.assertIn("Control-plane không nhận được telemetry", stream.status_message or "")
        self.assertEqual(self.store.live_notifications, [])

    def test_primary_with_backup_fails_over_after_telemetry_timeout(self) -> None:
        reconcile_at = datetime(2026, 5, 13, 16, 55)
        self.store._process_started_at = reconcile_at - timedelta(hours=1)
        self.store.users.append(UserSummary(id="admin-1", username="admin", display_name="admin", role="admin"))
        self.store.user_meta["admin-1"] = {"telegram": "12345", "telegram_live": ""}
        self.store.live_workers.append(make_live_worker("live-worker-backup"))
        self.store.live_user_worker_links.append(
            {"id": 2, "user_id": "user-1", "worker_id": "live-worker-backup", "threads": 1, "live_role": "backup"}
        )
        stream = make_stream(
            status="streaming",
            lease_expires_at=reconcile_at - timedelta(seconds=1),
            first_streaming_started_at=datetime(2026, 5, 13, 16, 40),
        )
        stream.backup_worker_id = "live-worker-backup"
        stream.backup_worker_name = "62.146.235.216"
        stream.log_label = "Mất telemetry"
        stream.status_message = "Không nhận telemetry từ live worker."
        stream.telemetry_stale_at = reconcile_at - timedelta(seconds=181)
        self.store.live_streams = [stream]

        changed = self.store._reconcile_expired_live_streams(now=reconcile_at)

        self.assertTrue(changed)
        self.assertEqual(stream.status, "disconnected")
        self.assertEqual(stream.log_label, "Mất kết nối")
        self.assertIsNone(stream.claimed_by_worker_id)
        self.assertIsNone(stream.lease_expires_at)
        self.assertIsNone(stream.telemetry_stale_at)
        self.assertEqual(len(self.store.telegram_notifications), 1)
        self.assertIn("[LIVE] BOT live mất telemetry quá lâu", self.store.telegram_notifications[0][1])
        backup_clone = self.store._find_live_backup_clone_optional(stream)
        self.assertIsNotNone(backup_clone)
        _, claimed = self.store.claim_next_live_stream(
            "live-worker-backup",
            self.store.get_worker_shared_secret(),
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, backup_clone.id)

    def test_primary_without_backup_stays_stale_after_telemetry_timeout(self) -> None:
        reconcile_at = datetime(2026, 5, 13, 16, 55)
        stale_at = reconcile_at - timedelta(minutes=10)
        self.store._process_started_at = reconcile_at - timedelta(hours=1)
        stream = make_stream(
            status="streaming",
            lease_expires_at=reconcile_at - timedelta(seconds=1),
            first_streaming_started_at=datetime(2026, 5, 13, 16, 40),
        )
        stream.log_label = "Mất telemetry"
        stream.status_message = "Không nhận telemetry từ live worker."
        stream.telemetry_stale_at = stale_at
        self.store.live_streams = [stream]

        changed = self.store._reconcile_expired_live_streams(now=reconcile_at)

        self.assertTrue(changed)
        self.assertEqual(stream.status, "streaming")
        self.assertEqual(stream.claimed_by_worker_id, "live-worker-01")
        self.assertIsNone(stream.disconnected_at)
        self.assertEqual(stream.telemetry_stale_at, stale_at)
        self.assertEqual(self.store.live_notifications, [])
        self.assertEqual(self.store.telegram_notifications, [])

    def test_expired_backup_clone_telemetry_stale_does_not_turn_clone_error(self) -> None:
        reconcile_at = datetime(2026, 5, 13, 16, 55)
        self.store._process_started_at = reconcile_at - timedelta(hours=1)
        primary = make_stream(
            status="disconnected",
            lease_expires_at=reconcile_at - timedelta(minutes=10),
            first_streaming_started_at=datetime(2026, 5, 13, 16, 45),
        )
        primary.backup_worker_id = "live-worker-backup"
        primary.backup_stream_id = "live-backup"
        backup = make_stream(
            status="streaming",
            lease_expires_at=reconcile_at - timedelta(seconds=1),
            first_streaming_started_at=datetime(2026, 5, 13, 16, 46),
        )
        backup.id = "live-backup"
        backup.primary_worker_id = "live-worker-backup"
        backup.runtime_role = "backup"
        backup.is_runtime_clone = True
        backup.parent_stream_id = primary.id
        backup.claimed_by_worker_id = "live-worker-backup"
        backup.claimed_by_role = "backup"
        self.store.live_workers.append(make_live_worker("live-worker-backup"))
        self.store.live_streams = [primary, backup]

        changed = self.store._reconcile_expired_live_streams(now=reconcile_at)

        self.assertTrue(changed)
        self.assertEqual(backup.status, "streaming")
        self.assertEqual(backup.claimed_by_worker_id, "live-worker-backup")
        self.assertEqual(backup.log_label, "Mất telemetry")
        self.assertIsNone(backup.error_message)
        self.assertEqual(self.store.live_notifications, [])

    def test_heartbeat_active_stream_clears_telemetry_stale_marker(self) -> None:
        heartbeat_at = datetime(2026, 5, 13, 16, 55)
        stream = make_stream(
            status="streaming",
            lease_expires_at=heartbeat_at - timedelta(seconds=1),
            first_streaming_started_at=datetime(2026, 5, 13, 16, 50),
        )
        stream.log_label = "Mất telemetry"
        stream.status_message = "Không nhận telemetry từ live worker."
        self.store.live_streams = [stream]

        self.store._reconcile_live_streams_from_heartbeat(
            self.store.live_workers[0],
            active_stream_ids=[stream.id],
            now=heartbeat_at,
        )

        self.assertEqual(stream.status, "streaming")
        self.assertEqual(stream.log_label, "Đang live")
        self.assertIsNone(stream.status_message)
        self.assertGreater(stream.lease_expires_at, heartbeat_at)

    def test_same_worker_can_reclaim_telemetry_stale_stream_after_restart(self) -> None:
        now = self.store._now(trim=False)
        stream = make_stream(
            status="streaming",
            lease_expires_at=now + timedelta(seconds=120),
            first_streaming_started_at=now - timedelta(minutes=5),
        )
        stream.log_label = "Mất telemetry"
        stream.status_message = "Không nhận telemetry từ live worker."
        self.store.live_streams = [stream]

        _worker, claimed = self.store.claim_next_live_stream(
            "live-worker-01",
            self.store.get_worker_shared_secret(),
        )

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, stream.id)
        refreshed = self.store.live_streams[0]
        self.assertEqual(refreshed.claimed_by_worker_id, "live-worker-01")
        self.assertGreater(refreshed.lease_expires_at, now)

    def test_editing_downloading_or_preparing_live_is_locked(self) -> None:
        for status in ("downloading", "preparing"):
            with self.subTest(status=status):
                self.setUp()
                scheduled_start = datetime.now() + timedelta(hours=1)
                stream = make_stream(
                    status=status,
                    lease_expires_at=datetime.now() + timedelta(minutes=10),
                )
                stream.start_time_live = scheduled_start
                stream.is_forever = True
                stream.end_time_live = None
                self.store.live_streams = [stream]

                with self.assertRaises(ValueError):
                    self.store.update_live_stream(
                        stream_id=stream.id,
                        stream_name="edited live",
                        primary_worker_id="live-worker-01",
                        stream_key="edited-key",
                        video_url="https://example.com/edited.mp4",
                        is_forever=True,
                        start_time_live=scheduled_start,
                    )
                self.assertFalse(self.store._live_stream_display_row(stream)["can_edit"])

    def test_editing_waiting_live_waits_for_old_runtime_to_exit_before_reclaim(self) -> None:
        scheduled_start = datetime.now() + timedelta(hours=1)
        stream = make_stream(
            status="waiting",
            lease_expires_at=datetime.now() + timedelta(minutes=10),
        )
        stream.start_time_live = scheduled_start
        stream.is_forever = True
        stream.end_time_live = None
        self.store.live_streams = [stream]

        updated = self.store.update_live_stream(
            stream_id=stream.id,
            stream_name="edited live",
            primary_worker_id="live-worker-01",
            stream_key="edited-key",
            video_url="https://example.com/edited.mp4",
            is_forever=True,
            start_time_live=scheduled_start,
        )

        self.assertEqual(updated.status, "scheduled")
        self.assertIsNone(updated.claimed_by_worker_id)

        with self.assertRaises(ValueError):
            self.store.update_live_stream_progress(
                stream_id=stream.id,
                worker_id="live-worker-01",
                shared_secret=self.store.get_worker_shared_secret(),
                status="waiting",
                progress=100,
                message="old runtime should not update after edit",
            )

        _, claimed = self.store.claim_next_live_stream(
            "live-worker-01",
            self.store.get_worker_shared_secret(),
        )
        self.assertIsNone(claimed)

        self.store.heartbeat_live_worker(
            type(
                "Payload",
                (),
                {
                    "worker_id": "live-worker-01",
                    "shared_secret": self.store.get_worker_shared_secret(),
                    "status": "busy",
                    "load_percent": 0,
                    "ram_percent": 0,
                    "ram_used_gb": 0.0,
                    "ram_total_gb": 0.0,
                    "bandwidth_kbps": 0.0,
                    "disk_used_gb": 0.0,
                    "disk_total_gb": 100.0,
                    "capacity": 1,
                    "active_stream_ids": [stream.id],
                },
            )()
        )
        self.assertIsNotNone(self.store.live_streams[0].stop_requested_at)

        self.store.heartbeat_live_worker(
            type(
                "Payload",
                (),
                {
                    "worker_id": "live-worker-01",
                    "shared_secret": self.store.get_worker_shared_secret(),
                    "status": "online",
                    "load_percent": 0,
                    "ram_percent": 0,
                    "ram_used_gb": 0.0,
                    "ram_total_gb": 0.0,
                    "bandwidth_kbps": 0.0,
                    "disk_used_gb": 0.0,
                    "disk_total_gb": 100.0,
                    "capacity": 1,
                    "active_stream_ids": [],
                },
            )()
        )

        _, claimed = self.store.claim_next_live_stream(
            "live-worker-01",
            self.store.get_worker_shared_secret(),
        )
        self.assertIsNotNone(claimed)
        self.assertIsNone(claimed.stop_requested_at)
        state = self.store.get_live_stream_runtime_state(
            stream_id=stream.id,
            worker_id="live-worker-01",
            shared_secret=self.store.get_worker_shared_secret(),
        )
        self.assertFalse(state["should_stop"])


if __name__ == "__main__":
    unittest.main()
