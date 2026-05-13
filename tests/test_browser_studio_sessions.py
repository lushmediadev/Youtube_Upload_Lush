import os
import unittest
from datetime import datetime

os.environ.setdefault("APP_ENABLE_LIVE_DEMO_SEED", "0")

from backend.app.schemas import ChannelRecord, UserSummary, WorkerRecord
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
        id="worker-17",
        name="217.216.110.32",
        manager_name="trieudo",
        group="workers",
        created_at=datetime(2026, 5, 13, 10, 0),
        status="online",
        capacity=1,
        load_percent=0,
        bandwidth_kbps=0,
        disk_used_gb=0,
        disk_total_gb=100,
        threads=1,
        browser_session_enabled=True,
        public_base_url="http://217.216.110.32",
        browser_display_base=90,
        browser_vnc_port_base=15900,
        browser_web_port_base=16080,
        browser_debug_port_base=19220,
    )


def make_channel() -> ChannelRecord:
    return ChannelRecord(
        id="channel-warm",
        name="Warm Melody",
        channel_id="UCwarm",
        worker_id="worker-17",
        worker_name="217.216.110.32",
        manager_name="trieudo",
        status="connected",
        connection_mode="browser_profile",
        browser_profile_key="htrang-5eb3e630b7",
        browser_profile_path="/opt/youtube-upload-lush/worker-data/browser-runtime/browser-profiles/htrang-5eb3e630b7",
    )


class BrowserStudioSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TestableStore()
        self.store.users = [
            UserSummary(id="user-1", username="htrang", display_name="htrang", role="user"),
            UserSummary(id="user-2", username="other", display_name="other", role="user"),
        ]
        self.store.workers = [make_worker()]
        self.store.channels = [make_channel()]
        self.store.user_worker_links = [{"id": 1, "user_id": "user-1", "worker_id": "worker-17"}]
        self.store.channel_user_links = [{"id": 1, "channel_id": "channel-warm", "user_id": "user-1"}]

    def test_create_studio_session_reuses_channel_profile_and_expires_in_30_minutes(self) -> None:
        before = self.store._now(trim=False)

        session = self.store.create_channel_studio_session("user-1", "channel-warm")

        self.assertEqual(session.purpose, "studio_access")
        self.assertEqual(session.channel_record_id, "channel-warm")
        self.assertEqual(session.target_worker_id, "worker-17")
        self.assertIn("/channel/UCwarm/videos", session.start_url or "")
        record = self.store.browser_sessions[0]
        self.assertEqual(record.profile_key, "htrang-5eb3e630b7")
        self.assertEqual(record.profile_path, "/opt/youtube-upload-lush/worker-data/browser-runtime/browser-profiles/htrang-5eb3e630b7")
        self.assertAlmostEqual((record.expires_at - before).total_seconds(), 30 * 60, delta=5)

    def test_create_studio_session_reuses_existing_active_session_for_same_channel(self) -> None:
        first = self.store.create_channel_studio_session("user-1", "channel-warm")
        second = self.store.create_channel_studio_session("user-1", "channel-warm")

        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(len(self.store.browser_sessions), 1)

    def test_create_studio_session_rejects_unlinked_channel(self) -> None:
        with self.assertRaises(KeyError):
            self.store.create_channel_studio_session("user-2", "channel-warm")

    def test_confirm_rejects_studio_access_session(self) -> None:
        session = self.store.create_channel_studio_session("user-1", "channel-warm")
        self.store.browser_sessions[0].status = "awaiting_confirmation"

        with self.assertRaises(ValueError):
            self.store.confirm_browser_session("user-1", session.session_id)


if __name__ == "__main__":
    unittest.main()
