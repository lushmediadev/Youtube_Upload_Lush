import os
import unittest
from datetime import datetime

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import WorkerRecord
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


if __name__ == "__main__":
    unittest.main()
