import os
import unittest
from datetime import datetime

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import UserSummary, WorkerRecord
from backend.app.store import AppStore


class LocalStore(AppStore):
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


def make_worker(worker_id: str = "worker-eu") -> WorkerRecord:
    now = datetime(2026, 5, 11, 8, 0)
    return WorkerRecord(
        id=worker_id,
        name="109.123.233.131",
        manager_id=None,
        manager_name="system",
        group="H-Upload",
        created_at=now,
        status="online",
        capacity=1,
        load_percent=0,
        bandwidth_kbps=0,
        disk_used_gb=0,
        disk_total_gb=100,
        threads=1,
        last_seen_at=now,
    )


class BotLocalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = LocalStore()
        self.store.workers = [make_worker()]
        self.store.live_workers = []
        self.store.channels = []
        self.store.jobs = []
        self.store.user_worker_links = []
        self.store.live_user_worker_links = []

    def test_update_bot_persists_local_and_exposes_it_in_rows(self) -> None:
        self.store.update_bot("worker-eu", "109.123.233.131", "EU", "H-Upload", None)

        self.assertEqual(self.store.workers[0].name, "109.123.233.131")
        self.assertEqual(self.store.workers[0].local, "EU")
        rows = self.store._build_bot_rows()
        self.assertEqual(rows[0]["local"], "EU")

    def test_pending_install_placeholder_exposes_requested_local(self) -> None:
        row = self.store._build_operation_placeholder_row(
            {
                "kind": "install",
                "status": "queued",
                "worker_id": "worker-pending",
                "worker_name": "82.197.71.52",
                "vps_ip": "82.197.71.52",
                "workspace_mode": "upload",
                "manager_name": "system",
                "post_install_config": {"local": "UK", "threads": 1},
            }
        )

        self.assertEqual(row["name"], "82.197.71.52")
        self.assertEqual(row["local"], "UK")

    def test_empty_live_bot_update_persists_backup_role(self) -> None:
        self.store.workers = []
        self.store.live_workers = [make_worker("live-worker-01")]

        self.store.update_bot(
            "live-worker-01",
            "62.146.169.168",
            "US-west",
            "workers",
            None,
            workspace_mode="live",
            live_role="backup",
            threads=5,
            assigned_user_ids=[],
        )

        self.assertEqual(self.store.live_workers[0].live_role, "backup")
        rows = self.store._build_bot_rows(workspace_mode="live")
        self.assertEqual(rows[0]["bot_function_key"], "backup")
        self.assertEqual(rows[0]["bot_type_label"], "Backup")
        self.assertEqual(rows[0]["assigned_live_role"], "backup")

    def test_pending_live_conversion_without_users_persists_backup_role(self) -> None:
        self.store.workers = []
        self.store.live_workers = [make_worker("live-worker-08")]

        self.store._apply_pending_install_config(
            {
                "requested_by": "admin",
                "requested_role": "admin",
                "requested_user_id": "admin-1",
                "post_install_config": {
                    "name": "62.72.46.42",
                    "local": "US-west",
                    "group": "workers",
                    "live_role": "backup",
                    "threads": 5,
                    "assigned_user_ids": [],
                },
            },
            worker_id="live-worker-08",
            workspace_mode="live",
        )

        self.assertEqual(self.store.live_workers[0].live_role, "backup")
        rows = self.store._build_bot_rows(workspace_mode="live")
        self.assertEqual(rows[0]["bot_function_key"], "backup")
        self.assertEqual(rows[0]["bot_type_label"], "Backup")

    def test_update_user_manager_refreshes_bot_picker_scope_without_restart(self) -> None:
        self.store.users = [
            UserSummary(id="admin-1", username="admin", display_name="admin", role="admin"),
            UserSummary(id="manager-old", username="manager", display_name="manager", role="manager"),
            UserSummary(id="manager-new", username="thanh", display_name="thanh", role="manager"),
            UserSummary(
                id="user-1",
                username="user1",
                display_name="user1",
                role="user",
                manager_id="manager-old",
                manager_name="manager",
            ),
        ]

        updated = self.store.update_admin_user(
            user_id="user-1",
            username="user1",
            password=None,
            manager_id="manager-new",
            actor_role="admin",
            updated_by="admin",
        )

        self.assertEqual(updated.manager_name, "thanh")
        self.assertEqual(updated.manager_id, "manager-new")
        option = next(
            item for item in self.store._combined_bot_user_options(viewer_role="admin", viewer_id="admin-1")
            if item["id"] == "user-1"
        )
        self.assertEqual(option["manager_id"], "manager-new")

    def test_create_user_manager_scope_is_available_to_bot_picker_without_restart(self) -> None:
        self.store.users = [
            UserSummary(id="admin-1", username="admin", display_name="admin", role="admin"),
            UserSummary(id="manager-new", username="thanh", display_name="thanh", role="manager"),
        ]
        self.store.user_meta = {"admin-1": {}}

        created = self.store.create_admin_user(
            username="user1",
            display_name="user1",
            password="secret123",
            role="user",
            manager_id="manager-new",
            telegram=None,
            updated_by="admin",
        )
        user = self.store._find_user(created["user_id"])

        self.assertEqual(user.manager_name, "thanh")
        self.assertEqual(user.manager_id, "manager-new")
        option = next(
            item for item in self.store._combined_bot_user_options(viewer_role="admin", viewer_id="admin-1")
            if item["id"] == created["user_id"]
        )
        self.assertEqual(option["manager_id"], "manager-new")


if __name__ == "__main__":
    unittest.main()
