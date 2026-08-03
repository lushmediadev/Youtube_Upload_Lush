import os
import unittest
from copy import deepcopy
from datetime import datetime, timedelta

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import LiveStreamRecord, RenderJobRecord, UserSummary
from backend.app.store import AppStore


class AdminHistoryStore(AppStore):
    def __init__(self) -> None:
        self.save_calls = 0
        self.purged_job_ids: list[str] = []
        self.deleted_live_preview_ids: list[str] = []
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

    def _purge_job_artifacts(self, job: RenderJobRecord, *, exclude_job_id: str | None = None) -> None:
        self.purged_job_ids.append(job.id)

    def _delete_live_preview_file(self, stream: LiveStreamRecord) -> bool:
        self.deleted_live_preview_ids.append(stream.id)
        return True


def make_job(index: int, *, status: str = "completed", completed_at: datetime | None = None) -> RenderJobRecord:
    created_at = datetime(2026, 1, 1) + timedelta(minutes=index)
    return RenderJobRecord(
        id=f"job-{index:03d}",
        title=f"Job {index:03d}",
        source_mode="drive",
        channel_id="channel-1",
        channel_name="Kênh thử nghiệm",
        worker_name="worker-01",
        manager_name="manager-one",
        status=status,
        created_at=created_at,
        completed_at=completed_at,
        source_label="URL",
    )


def make_live_stream(
    index: int,
    *,
    status: str = "ended",
    ended_at: datetime | None = None,
    end_time_live: datetime | None = None,
) -> LiveStreamRecord:
    created_at = datetime(2026, 1, 1) + timedelta(minutes=index)
    return LiveStreamRecord(
        id=f"live-{index:03d}",
        owner_user_id="user-1",
        owner_username="nguyen-user",
        owner_display_name="Nguyễn User",
        manager_id="manager-1",
        manager_name="manager-one",
        primary_worker_id="live-worker-01",
        primary_worker_name="live-worker-01",
        stream_name=f"Live {index:03d}",
        stream_key=f"key-{index:03d}",
        video_url="https://example.com/video.mp4",
        is_forever=end_time_live is None,
        end_time_live=end_time_live,
        status=status,
        ended_at=ended_at,
        created_at=created_at,
        updated_at=ended_at or created_at,
    )


class AdminHistoryPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = AdminHistoryStore()
        self.store.users = [
            UserSummary(id="admin-1", username="admin", display_name="Admin", role="admin"),
            UserSummary(
                id="user-1",
                username="nguyen-user",
                display_name="Nguyễn User",
                role="user",
                manager_id="manager-1",
                manager_name="manager-one",
            ),
        ]
        self.store.user_meta = {}
        self.store.workers = []
        self.store.live_workers = []
        self.store.channels = []
        self.store.channel_user_links = []
        self.store.user_worker_links = []
        self.store.live_user_worker_links = []

    def test_upload_history_is_sliced_to_twenty_rows_after_global_sort(self) -> None:
        self.store.jobs = [make_job(index) for index in range(45)]

        context = self.store.get_admin_render_index_context(
            viewer_role="admin",
            viewer_id="admin-1",
            workspace_mode="upload",
            page=2,
            query="",
        )

        self.assertEqual(len(context["renders"]), 20)
        self.assertEqual(context["renders"][0]["index"], 21)
        self.assertEqual(context["renders"][-1]["index"], 40)
        self.assertEqual(context["history_pagination"]["total"], 45)
        self.assertEqual(context["history_pagination"]["total_pages"], 3)
        self.assertEqual(context["history_pagination"]["from"], 21)
        self.assertEqual(context["history_pagination"]["to"], 40)

    def test_upload_search_is_accent_insensitive_and_runs_before_pagination(self) -> None:
        jobs = [make_job(index) for index in range(45)]
        jobs[3].title = "Điện ảnh Việt Nam"
        self.store.jobs = jobs

        context = self.store.get_admin_render_index_context(
            viewer_role="admin",
            viewer_id="admin-1",
            workspace_mode="upload",
            page=1,
            query="dien anh",
        )

        self.assertEqual([row["id"] for row in context["renders"]], ["job-003"])
        self.assertEqual(context["history_pagination"]["total"], 1)

    def test_live_history_only_builds_requested_page_of_visible_streams(self) -> None:
        self.store.live_streams = [make_live_stream(index) for index in range(45)]

        context = self.store.get_admin_render_index_context(
            viewer_role="admin",
            viewer_id="admin-1",
            workspace_mode="live",
            page=3,
            query="",
        )

        self.assertEqual(len(context["renders"]), 5)
        self.assertEqual(context["renders"][0]["index"], 41)
        self.assertEqual(context["history_pagination"]["total"], 45)


class CompletedHistoryRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = AdminHistoryStore()
        self.store.jobs = []
        self.store.live_streams = []

    def test_cleanup_only_removes_upload_jobs_completed_before_cutoff(self) -> None:
        now = datetime(2026, 8, 3, 12, 0)
        old_completed = make_job(1, completed_at=now - timedelta(days=31))
        recent_completed = make_job(2, completed_at=now - timedelta(days=29))
        old_error = make_job(3, status="error", completed_at=now - timedelta(days=60))
        self.store.jobs = [old_completed, recent_completed, old_error]

        result = self.store._cleanup_completed_history(now=now)

        self.assertEqual(result["upload_jobs"], 1)
        self.assertEqual([job.id for job in self.store.jobs], [recent_completed.id, old_error.id])
        self.assertEqual(self.store.purged_job_ids, [old_completed.id])

    def test_cleanup_removes_genuinely_ended_live_primary_and_terminal_clone(self) -> None:
        now = datetime(2026, 8, 3, 12, 0)
        schedule_end = now - timedelta(days=31)
        primary = make_live_stream(1, ended_at=now - timedelta(days=1), end_time_live=schedule_end)
        primary.backup_stream_id = "live-backup"
        clone = deepcopy(primary)
        clone.id = "live-backup"
        clone.is_runtime_clone = True
        clone.runtime_role = "backup"
        clone.parent_stream_id = primary.id
        clone.status = "stopped"
        clone.ended_at = schedule_end
        self.store.live_streams = [primary, clone]

        result = self.store._cleanup_completed_history(now=now)

        self.assertEqual(result["live_streams"], 1)
        self.assertEqual(result["live_clones"], 1)
        self.assertEqual(self.store.live_streams, [])
        self.assertCountEqual(self.store.deleted_live_preview_ids, [primary.id, clone.id])

    def test_cleanup_keeps_ended_primary_when_backup_clone_is_still_active(self) -> None:
        now = datetime(2026, 8, 3, 12, 0)
        schedule_end = now - timedelta(days=31)
        primary = make_live_stream(1, ended_at=schedule_end, end_time_live=schedule_end)
        primary.backup_stream_id = "live-backup"
        clone = deepcopy(primary)
        clone.id = "live-backup"
        clone.is_runtime_clone = True
        clone.runtime_role = "backup"
        clone.parent_stream_id = primary.id
        clone.status = "streaming"
        clone.is_live_now = True
        clone.claimed_by_worker_id = "live-worker-02"
        clone.lease_expires_at = now + timedelta(minutes=1)
        self.store.live_streams = [primary, clone]

        result = self.store._cleanup_completed_history(now=now)

        self.assertEqual(result["live_streams"], 0)
        self.assertEqual([stream.id for stream in self.store.live_streams], [primary.id, clone.id])

    def test_cleanup_keeps_stopped_error_and_claimed_ended_live_records(self) -> None:
        now = datetime(2026, 8, 3, 12, 0)
        old = now - timedelta(days=60)
        stopped = make_live_stream(1, status="stopped", ended_at=old)
        errored = make_live_stream(2, status="error", ended_at=old)
        claimed = make_live_stream(3, status="ended", ended_at=old)
        claimed.claimed_by_worker_id = "live-worker-01"
        claimed.lease_expires_at = now + timedelta(minutes=1)
        self.store.live_streams = [stopped, errored, claimed]

        result = self.store._cleanup_completed_history(now=now)

        self.assertEqual(result["live_streams"], 0)
        self.assertEqual(len(self.store.live_streams), 3)


if __name__ == "__main__":
    unittest.main()
