import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import ChannelRecord, RenderJobRecord, UserSummary, WorkerHeartbeatPayload, WorkerRecord
from backend.app.store import AppStore


class TestableStore(AppStore):
    def __init__(self) -> None:
        self.sent_live_alerts: list[tuple[str | None, str]] = []
        self.saved_count = 0
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

    def _send_telegram_live_alert(self, message: str, *, chat_id: str | None = None) -> bool:
        self.sent_live_alerts.append((chat_id, message))
        return True


def make_worker() -> WorkerRecord:
    return WorkerRecord(
        id="worker-1",
        name="62.169.23.112",
        manager_id="manager-1",
        manager_name="manager",
        group="workers",
        created_at=datetime(2026, 5, 8, 9, 0),
        status="busy",
        capacity=1,
        load_percent=0,
        bandwidth_kbps=0,
        disk_used_gb=0,
        disk_total_gb=100,
        threads=1,
        last_seen_at=datetime(2026, 5, 8, 11, 0),
    )


def make_job(*, progress: int) -> RenderJobRecord:
    now = datetime(2026, 5, 8, 10, 55)
    return RenderJobRecord(
        id="job-1",
        title="Aurelian Nocturne",
        source_mode="drive",
        channel_id="channel-1",
        channel_name="Aurelian Nocturne",
        worker_name="62.169.23.112",
        manager_name="manager",
        status="uploading",
        progress=progress,
        download_progress=100,
        render_progress=100,
        upload_progress=progress,
        created_at=now,
        started_at=now,
        download_started_at=now,
        upload_started_at=now + timedelta(minutes=6),
        claimed_at=now,
        claimed_by_worker_id="worker-1",
        lease_expires_at=datetime(2000, 1, 1),
        source_label="Google Drive/cloud",
    )


class WorkerUploadInterruptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 5, 8, 11, 22)
        self.store = TestableStore()
        self.store.users = [
            UserSummary(id="user-1", username="phamphong", display_name="phamphong", role="user"),
        ]
        self.store.user_meta = {
            "user-1": {"telegram": "", "telegram_live": "900"},
        }
        self.store.workers = [make_worker()]
        self.store.channels = [
            ChannelRecord(
                id="channel-1",
                name="Aurelian Nocturne",
                channel_id="UC-channel-1",
                worker_id="worker-1",
                worker_name="62.169.23.112",
                manager_name="manager",
                status="connected",
            )
        ]
        self.store.channel_user_links = [{"id": 1, "channel_id": "channel-1", "user_id": "user-1"}]

    def test_upload_interruption_under_threshold_requeues_without_notify(self) -> None:
        self.store.jobs = [make_job(progress=8)]

        self.store.heartbeat_worker(
            WorkerHeartbeatPayload(
                worker_id="worker-1",
                shared_secret=self.store.get_worker_shared_secret(),
                active_job_ids=[],
            )
        )

        job = self.store.jobs[0]
        self.assertEqual(job.status, "pending")
        self.assertIsNone(job.claimed_by_worker_id)
        self.assertEqual(job.upload_progress, 0)
        self.assertEqual(self.store.sent_live_alerts, [])

    def test_upload_interruption_after_commit_marks_error_and_notifies_user_live_telegram(self) -> None:
        self.store.jobs = [make_job(progress=74)]

        self.store.heartbeat_worker(
            WorkerHeartbeatPayload(
                worker_id="worker-1",
                shared_secret=self.store.get_worker_shared_secret(),
                active_job_ids=[],
            )
        )

        job = self.store.jobs[0]
        self.assertEqual(job.status, "error")
        self.assertEqual(job.upload_progress, 74)
        self.assertFalse(job.can_cancel)
        self.assertEqual(len(self.store.sent_live_alerts), 1)
        chat_id, message = self.store.sent_live_alerts[0]
        self.assertEqual(chat_id, "900")
        self.assertIn("[UPLOAD] Job upload bị lỗi", message)
        self.assertIn("Aurelian Nocturne", message)
        self.assertIn("74%", message)
        self.assertIn("62.169.23.112", message)
        self.assertIn("Trạng thái: Lỗi do bot bị restart mất hoặc mất mạng mất kết nối", message)
        self.assertIn("Vui lòng xoá Job lỗi và tạo lại Job upload mới", message)
        self.assertNotIn("Chi tiết:", message)

    def test_completed_upload_notifies_user_live_telegram(self) -> None:
        self.store.jobs = [make_job(progress=100)]

        completed = self.store.complete_worker_job(
            job_id="job-1",
            worker_id="worker-1",
            shared_secret=self.store.get_worker_shared_secret(),
            output_url="https://www.youtube.com/watch?v=abc123",
            message="YouTube da xac nhan video upload o che do ban nhap",
        )

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.upload_progress, 100)
        self.assertEqual(len(self.store.sent_live_alerts), 1)
        chat_id, message = self.store.sent_live_alerts[0]
        self.assertEqual(chat_id, "900")
        self.assertIn("[UPLOAD] Job upload hoàn thành", message)
        self.assertIn("Aurelian Nocturne", message)
        self.assertIn("62.169.23.112", message)
        self.assertNotIn("Video:", message)
        self.assertNotIn("Trạng thái:", message)

    def test_video_slot_mp3_upload_failure_notifies_user_live_telegram(self) -> None:
        self.store.jobs = [make_job(progress=42)]

        failed = self.store.fail_worker_job(
            job_id="job-1",
            worker_id="worker-1",
            shared_secret=self.store.get_worker_shared_secret(),
            message="Asset video_loop đang là file MP3, không có video stream.",
        )

        self.assertEqual(failed.status, "error")
        self.assertEqual(len(self.store.sent_live_alerts), 1)
        chat_id, message = self.store.sent_live_alerts[0]
        self.assertEqual(chat_id, "900")
        self.assertIn("[UPLOAD] Job upload gặp lỗi", message)
        self.assertIn("Tài khoản: phamphong", message)
        self.assertIn("Aurelian Nocturne", message)
        self.assertIn("62.169.23.112", message)
        self.assertIn("Link video đang nhập là link file MP3, vui lòng sửa lại link file thành video.", message)

    def test_verify_its_you_upload_failure_notifies_user_live_telegram(self) -> None:
        self.store.jobs = [make_job(progress=18)]

        failed = self.store.fail_worker_job(
            job_id="job-1",
            worker_id="worker-1",
            shared_secret=self.store.get_worker_shared_secret(),
            message=(
                "YouTube/Google dang hien modal Verify it's you tren Chrome profile cua kenh. "
                "Bot khong the tu xac minh tai khoan nay. Hay mo noVNC de xu ly thu cong, "
                "hoac xoa kenh va them lai kenh de tao profile sach roi chay lai job."
            ),
        )

        self.assertEqual(failed.status, "error")
        self.assertEqual(len(self.store.sent_live_alerts), 1)
        chat_id, message = self.store.sent_live_alerts[0]
        self.assertEqual(chat_id, "900")
        self.assertIn("[UPLOAD] Job upload", message)
        self.assertIn("Aurelian Nocturne", message)
        self.assertIn(
            "Lỗi: YouTube/Google yêu cầu Verify it's you. Vui lòng xoá kênh và thêm lại kênh.",
            message,
        )

    def test_generic_upload_failure_does_not_send_media_specific_notification(self) -> None:
        self.store.jobs = [make_job(progress=42)]

        failed = self.store.fail_worker_job(
            job_id="job-1",
            worker_id="worker-1",
            shared_secret=self.store.get_worker_shared_secret(),
            message="YouTube Studio timeout while waiting for button.",
        )

        self.assertEqual(failed.status, "error")
        self.assertEqual(self.store.sent_live_alerts, [])

    def test_startup_grace_skips_false_upload_expiry_after_control_plane_restart(self) -> None:
        self.store._process_started_at = self.now - timedelta(seconds=15)
        self.store.jobs = [make_job(progress=74)]

        changed, notifications = self.store._reconcile_expired_worker_jobs(now=self.now)

        job = self.store.jobs[0]
        self.assertFalse(changed)
        self.assertEqual(notifications, [])
        self.assertEqual(job.status, "uploading")
        self.assertEqual(job.claimed_by_worker_id, "worker-1")
        self.assertEqual(self.store.sent_live_alerts, [])


if __name__ == "__main__":
    unittest.main()
