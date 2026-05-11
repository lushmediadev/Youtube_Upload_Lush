from __future__ import annotations

import hashlib
import hmac
import html
import logging
from copy import deepcopy
from datetime import datetime, timedelta, timezone, tzinfo
import json
import mimetypes
import os
import re
import secrets
import unicodedata
from pathlib import Path
import shutil
import sqlite3
from threading import Event, RLock, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .browser_runtime import BrowserRuntimeManager
from .schemas import (
    AdminDashboardResponse,
    AdminSummary,
    BrowserSessionRecord,
    BrowserSessionResponse,
    ChannelRecord,
    JobAsset,
    JobCreatePayload,
    LiveWorkerControlResponse,
    LiveWorkerHeartbeatPayload,
    LiveWorkerRegisterPayload,
    LiveStreamRecord,
    OAuthStartResponse,
    OAuthSummary,
    RenderJobRecord,
    UploadCapabilities,
    UploadSessionCreateRequest,
    UploadSessionRecord,
    UploadSessionResponse,
    UserBootstrapResponse,
    UserJobsResponse,
    UserSummary,
    WorkerBrowserSessionSyncPayload,
    WorkerControlResponse,
    WorkerHeartbeatPayload,
    WorkerRegisterPayload,
    WorkerRecord,
    WorkerYouTubeUploadTarget,
)
from .worker_bootstrap import ensure_worker_operation_threads, suggest_next_worker_id

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)


class AppStore:
    TELEGRAM_MESSAGE_CHUNK_LIMIT = 3800

    KNOWN_WORKER_DISPLAY_NAMES = {
        "worker-01": "109.123.233.131",
        "worker-02": "62.72.46.42",
        "live-worker-01": "109.123.233.131",
        "live-worker-02": "62.72.46.42",
    }

    @staticmethod
    def _default_bot_group_label() -> str:
        return "new"

    def _normalized_bot_group(self, group: str | None) -> str:
        return str(group or "").strip() or self._default_bot_group_label()

    @staticmethod
    def _env_flag(name: str, *, default: bool) -> bool:
        raw_value = str(os.getenv(name, "")).strip().lower()
        if raw_value in {"1", "true", "yes", "on"}:
            return True
        if raw_value in {"0", "false", "no", "off"}:
            return False
        return default

    @staticmethod
    def _url_points_to_localhost(value: str | None) -> bool:
        normalized = str(value or "").strip()
        if not normalized:
            return False
        parsed = urlparse(normalized if "://" in normalized else f"http://{normalized}")
        hostname = str(parsed.hostname or "").strip().lower()
        return hostname in {"127.0.0.1", "localhost", "::1"}

    def _resume_worker_operation_queue(self) -> None:
        try:
            ensure_worker_operation_threads(self)
        except Exception as exc:
            print(f"[worker_ops] scheduler resume failed: {exc}", flush=True)

    @classmethod
    def _restore_worker_operation_workspace_mode(cls, task: dict[str, Any]) -> str:
        explicit_workspace = str(task.get("workspace_mode") or "").strip()
        if explicit_workspace:
            return cls._normalize_workspace_mode(explicit_workspace)
        worker_id = str(task.get("worker_id") or "").strip().lower()
        runtime_dir = str(task.get("runtime_dir") or "").strip().lower()
        app_dir = str(task.get("app_dir") or "").strip().lower()
        if worker_id.startswith("live-worker-") or "live-worker" in runtime_dir or "live-worker" in app_dir:
            return "live"
        return "upload"

    @classmethod
    def _live_demo_seed_enabled(cls) -> bool:
        default_enabled = any(
            cls._url_points_to_localhost(candidate)
            for candidate in (
                os.getenv("WORKER_BOOTSTRAP_CONTROL_PLANE_URL"),
                os.getenv("GOOGLE_REDIRECT_URI"),
            )
        )
        return cls._env_flag("APP_ENABLE_LIVE_DEMO_SEED", default=default_enabled)

    def _seed_live_workers(self, *, now: datetime) -> list[WorkerRecord]:
        return [
            WorkerRecord(
                id="live-worker-01",
                name=self.KNOWN_WORKER_DISPLAY_NAMES["live-worker-01"],
                manager_id="manager-1",
                manager_name="manager-alpha",
                group="S - Việt 3",
                created_at=now - timedelta(days=18),
                status="offline",
                capacity=1,
                load_percent=0,
                ram_percent=0,
                ram_used_gb=0.0,
                ram_total_gb=8.0,
                bandwidth_kbps=0,
                disk_used_gb=130.2,
                disk_total_gb=512.0,
                threads=1,
                last_seen_at=now - timedelta(minutes=18),
            ),
            WorkerRecord(
                id="live-worker-02",
                name=self.KNOWN_WORKER_DISPLAY_NAMES["live-worker-02"],
                manager_id="manager-1",
                manager_name="manager-alpha",
                group="S - Việt 3",
                created_at=now - timedelta(days=16),
                status="offline",
                capacity=1,
                load_percent=0,
                ram_percent=0,
                ram_used_gb=0.0,
                ram_total_gb=8.0,
                bandwidth_kbps=0,
                disk_used_gb=90.1,
                disk_total_gb=512.0,
                threads=1,
                last_seen_at=now - timedelta(minutes=12),
            ),
        ]

    @staticmethod
    def _seed_live_user_worker_links() -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "user_id": "user-1",
                "worker_id": "live-worker-01",
                "threads": 1,
                "live_role": "primary",
                "note": "BOT live chính được cấp",
            },
            {
                "id": 2,
                "user_id": "user-1",
                "worker_id": "live-worker-02",
                "threads": 1,
                "live_role": "backup",
                "note": "BOT backup được cấp",
            },
        ]

    def _seed_live_streams(self, *, now: datetime) -> list[LiveStreamRecord]:
        return [
            LiveStreamRecord(
                id="live-stream-001",
                owner_user_id="user-1",
                owner_username="demo-user",
                owner_display_name="demo-user",
                manager_id="manager-1",
                manager_name="manager-alpha",
                primary_worker_id="live-worker-01",
                primary_worker_name=self._resolve_live_worker_display_name("live-worker-01"),
                primary_group="S - Việt 3",
                backup_worker_id="live-worker-02",
                backup_worker_name=self._live_backup_worker_display_name("live-worker-02"),
                backup_group="S - Việt 3",
                stream_name="11hkodi luồng live test tiêu đề khá dài để kiểm tra việc tự xuống dòng trong bảng render livestream",
                stream_key="bz2k-vegp-54v4-v2pc-25sr-demo-key-cuc-ky-dai-de-test-wrap",
                video_url="https://drive.google.com/file/d/live-video-demo",
                audio_url="https://drive.google.com/file/d/live-audio-demo",
                live_label="Live 11h",
                start_time_live=datetime(2025, 11, 26, 20, 0, 0),
                end_time_live=datetime(2025, 11, 27, 13, 0, 0),
                backup_delay_minutes=3,
                status="stopped",
                log_label="Kết thúc",
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=1, hours=3),
            ),
        ]

    @staticmethod
    def _path_has_content(path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.exists() and path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    @staticmethod
    def _app_timezone_name() -> str:
        configured = os.getenv("APP_TIMEZONE", "Asia/Ho_Chi_Minh").strip()
        if configured.casefold() == "asia/saigon":
            return "Asia/Ho_Chi_Minh"
        return configured

    @classmethod
    def _app_timezone(cls) -> tzinfo:
        preferred = cls._app_timezone_name()
        for candidate in (preferred, "Asia/Ho_Chi_Minh", "Etc/GMT-7"):
            try:
                return ZoneInfo(candidate)
            except Exception:
                continue
        return timezone(timedelta(hours=7))

    @classmethod
    def _normalize_datetime(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value
        return value.astimezone(cls._app_timezone()).replace(tzinfo=None)

    @classmethod
    def _now(cls, *, trim: bool = True) -> datetime:
        current = datetime.now(cls._app_timezone())
        if trim:
            current = current.replace(second=0, microsecond=0)
        return current.replace(tzinfo=None)

    @staticmethod
    def _worker_offline_alert_seconds() -> int:
        raw_value = str(os.getenv("WORKER_OFFLINE_ALERT_SECONDS", "180")).strip()
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 180
        return max(60, value)

    @staticmethod
    def _worker_monitor_interval_seconds() -> int:
        raw_value = str(os.getenv("WORKER_MONITOR_INTERVAL_SECONDS", "30")).strip()
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 30
        return max(10, value)

    @staticmethod
    def _worker_job_lease_seconds() -> int:
        raw_value = str(os.getenv("WORKER_JOB_LEASE_SECONDS", "90")).strip()
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 90
        return max(30, value)

    @classmethod
    def _inactive_bot_alert_enabled(cls) -> bool:
        return cls._env_flag("INACTIVE_BOT_ALERT_ENABLED", default=True)

    @staticmethod
    def _inactive_bot_alert_days() -> int:
        raw_value = str(os.getenv("INACTIVE_BOT_ALERT_DAYS", "10")).strip()
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 10
        return max(1, value)

    @staticmethod
    def _inactive_bot_alert_hour() -> int:
        raw_value = str(os.getenv("INACTIVE_BOT_ALERT_HOUR", "8")).strip()
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 8
        return max(0, min(23, value))

    @staticmethod
    def _inactive_bot_alert_minute() -> int:
        raw_value = str(os.getenv("INACTIVE_BOT_ALERT_MINUTE", "0")).strip()
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 0
        return max(0, min(59, value))

    @staticmethod
    def _telegram_alert_bot_token() -> str:
        return str(os.getenv("TELEGRAM_ALERT_BOT_TOKEN", "")).strip()

    @staticmethod
    def _telegram_alert_chat_id() -> str:
        return str(os.getenv("TELEGRAM_ALERT_CHAT_ID", "")).strip()

    @staticmethod
    def _telegram_live_bot_token() -> str:
        return str(os.getenv("TELEGRAM_LIVE_BOT_TOKEN", "")).strip()

    @classmethod
    def _bot_operation_telegram_enabled(cls) -> bool:
        return cls._env_flag("TELEGRAM_BOT_OPERATION_NOTIFICATIONS_ENABLED", default=True)

    @classmethod
    def _telegram_live_notifications_enabled(cls) -> bool:
        return cls._env_flag("TELEGRAM_LIVE_NOTIFICATIONS_ENABLED", default=True)

    @staticmethod
    def _telegram_link_ttl_seconds() -> int:
        raw_value = str(os.getenv("TELEGRAM_LINK_TTL_SECONDS", "600")).strip()
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 600
        return max(120, value)

    @staticmethod
    def _telegram_live_link_ttl_seconds() -> int:
        raw_value = str(
            os.getenv("TELEGRAM_LIVE_LINK_TTL_SECONDS", os.getenv("TELEGRAM_LINK_TTL_SECONDS", "600"))
        ).strip()
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 600
        return max(120, value)

    @staticmethod
    def _normalize_telegram_chat_id(value: str | None) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            return ""
        if not re.fullmatch(r"-?\d+", normalized):
            raise ValueError("Telegram ID phải là chat_id dạng số.")
        return normalized

    def __init__(self) -> None:
        now = self._now()
        live_demo_seed_enabled = self._live_demo_seed_enabled()
        self.data_dir = Path(__file__).resolve().parents[1] / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir = self.data_dir / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.upload_tmp_dir = self.upload_dir / "tmp"
        self.upload_tmp_dir.mkdir(parents=True, exist_ok=True)
        self.upload_asset_dir = self.upload_dir / "assets"
        self.upload_asset_dir.mkdir(parents=True, exist_ok=True)
        self.preview_dir = self.data_dir / "previews"
        self.preview_dir.mkdir(parents=True, exist_ok=True)
        self.state_db_path = self.data_dir / "app_state.db"
        self.upload_sessions: list[UploadSessionRecord] = []
        self.browser_runtime = BrowserRuntimeManager(self.data_dir)
        self.browser_sessions: list[BrowserSessionRecord] = []
        self.browser_profile_cleanup_tasks: list[dict[str, Any]] = []
        self.worker_round_robin_cursor: dict[str, str] = {}
        self.worker_connection_profiles: dict[str, dict[str, Any]] = {}
        self.worker_registration_meta: dict[str, dict[str, Any]] = {}
        self.worker_operation_tasks: list[dict[str, Any]] = []
        self.deleted_workers: dict[str, dict[str, Any]] = {}
        self.telegram_link_requests: dict[str, dict[str, Any]] = {}
        self.live_telegram_link_requests: dict[str, dict[str, Any]] = {}
        self.admin_notifications: list[dict[str, Any]] = []
        self._worker_state_lock = RLock()
        self._monitor_stop_event = Event()
        self._monitor_thread: Thread | None = None
        self.inactive_bot_alert_last_sent_on: str | None = None
        self.inactive_bot_alert_last_attempted_on: str | None = None

        self.users = [
            UserSummary(id="admin-1", username="admin", display_name="Admin", role="admin"),
            UserSummary(id="manager-1", username="manager-alpha", display_name="Manager Alpha", role="manager"),
            UserSummary(id="user-1", username="demo-user", display_name="demo-user", role="user", manager_name="manager-alpha"),
        ]
        self.user_meta: dict[str, dict[str, Any]] = {
            "admin-1": {
                "password_hash": self._hash_password("admin123"),
                "password_algo": "pbkdf2_sha256",
                "telegram": "@admin-control",
                "telegram_live": "",
                "updated_by": "system",
                "updated_at": now - timedelta(days=3),
                "created_at": now - timedelta(days=30),
            },
            "manager-1": {
                "password_hash": self._hash_password("manager123"),
                "password_algo": "pbkdf2_sha256",
                "telegram": "@manager-alpha",
                "telegram_live": "",
                "updated_by": "admin",
                "updated_at": now - timedelta(days=2),
                "created_at": now - timedelta(days=20),
            },
            "user-1": {
                "password_hash": self._hash_password("demo123"),
                "password_algo": "pbkdf2_sha256",
                "telegram": "@demo-user",
                "telegram_live": "",
                "updated_by": "manager-alpha",
                "updated_at": now - timedelta(hours=5),
                "created_at": now - timedelta(days=10),
            },
        }

        self.workers = [
            WorkerRecord(
                id="worker-01",
                name=self.KNOWN_WORKER_DISPLAY_NAMES["worker-01"],
                manager_id="manager-1",
                manager_name="manager-alpha",
                group="manager-alpha",
                created_at=now - timedelta(days=12),
                status="online",
                capacity=4,
                load_percent=45,
                ram_percent=73,
                ram_used_gb=5.84,
                ram_total_gb=8.0,
                bandwidth_kbps=1024,
                disk_used_gb=124.5,
                disk_total_gb=512.0,
                threads=2,
                last_seen_at=now,
            ),
            WorkerRecord(
                id="worker-02",
                name=self.KNOWN_WORKER_DISPLAY_NAMES["worker-02"],
                manager_id="manager-1",
                manager_name="manager-alpha",
                group="manager-alpha",
                created_at=now - timedelta(days=10),
                status="online",
                capacity=4,
                load_percent=28,
                ram_percent=41,
                ram_used_gb=3.28,
                ram_total_gb=8.0,
                bandwidth_kbps=832,
                disk_used_gb=88.2,
                disk_total_gb=512.0,
                threads=2,
                last_seen_at=now,
            ),
        ]
        self.user_worker_links: list[dict[str, Any]] = [
            {"id": 1, "user_id": "user-1", "worker_id": "worker-01", "threads": 2, "note": "BOT chính"},
            {"id": 2, "user_id": "user-1", "worker_id": "worker-02", "threads": 1, "note": "BOT phụ"},
        ]
        self.live_workers = self._seed_live_workers(now=now) if live_demo_seed_enabled else []
        self.live_user_worker_links: list[dict[str, Any]] = (
            self._seed_live_user_worker_links() if live_demo_seed_enabled else []
        )

        self.channels = [
            ChannelRecord(
                id="channel-1",
                name="Kodi Lofi",
                channel_id="UCNaAEEC2W6ISnzTmmKjMCMg",
                avatar_url="https://images.unsplash.com/photo-1516280440614-37939bbacd81?auto=format&fit=crop&w=80&q=80",
                worker_id="worker-01",
                worker_name="worker-01",
                manager_name="manager-alpha",
                status="connected",
                oauth_email="kodi-lofi@example.com",
                oauth_connected_at=now - timedelta(days=2),
            ),
            ChannelRecord(
                id="channel-2",
                name="Daily Beats",
                channel_id="UCdemo00000000000000001",
                avatar_url="https://images.unsplash.com/photo-1493225457124-a3eb161ffa5f?auto=format&fit=crop&w=80&q=80",
                worker_id="worker-02",
                worker_name="worker-02",
                manager_name="manager-alpha",
                status="connected",
                oauth_email="daily-beats@example.com",
                oauth_connected_at=now - timedelta(days=1),
            ),
            ChannelRecord(
                id="channel-3",
                name="demo-user-youtube-channel",
                channel_id="UCuserdemo",
                avatar_url=None,
                worker_id="worker-01",
                worker_name="worker-01",
                manager_name="manager-alpha",
                status="connected",
                oauth_email="demo-user@example.com",
                oauth_connected_at=now - timedelta(hours=8),
            ),
        ]
        self.channel_user_links: list[dict[str, Any]] = [
            {"id": 1, "channel_id": "channel-1", "user_id": "user-1"},
            {"id": 2, "channel_id": "channel-2", "user_id": "user-1"},
        ]

        self.jobs = [
            RenderJobRecord(
                id="job-379e626e",
                title="Drive Job Local Check",
                source_mode="drive",
                channel_id="channel-1",
                channel_name="Kodi Lofi",
                channel_avatar_url=self.channels[0].avatar_url,
                worker_name="worker-01",
                manager_name="manager-alpha",
                status="queueing",
                progress=0,
                queue_order=1,
                created_at=now - timedelta(minutes=43),
                source_label="Google Drive/cloud",
                thumbnail_url=None,
                assets=[
                    JobAsset(slot="video_loop", label="Video loop", source_mode="drive", url="https://drive.google.com/video-loop"),
                    JobAsset(slot="audio_loop", label="Audio loop", source_mode="drive", url="https://drive.google.com/audio-loop"),
                ],
            ),
            RenderJobRecord(
                id="job-3787e1cd",
                title="Local Upload Test",
                source_mode="local",
                channel_id="channel-2",
                channel_name="Daily Beats",
                channel_avatar_url=self.channels[1].avatar_url,
                worker_name="worker-02",
                manager_name="manager-alpha",
                status="pending",
                progress=0,
                queue_order=2,
                created_at=now - timedelta(minutes=44),
                source_label="Local Upload",
                source_file_name="local-upload-test.mp4",
                thumbnail_url=None,
                assets=[
                    JobAsset(slot="video_loop", label="Video loop", source_mode="local", file_name="local-upload-test.mp4"),
                    JobAsset(slot="audio_loop", label="Audio loop", source_mode="local", file_name="ambient-loop.mp3"),
                ],
            ),
            RenderJobRecord(
                id="job-1002",
                title="Loop Mix Episode 2",
                source_mode="local",
                channel_id="channel-2",
                channel_name="Daily Beats",
                channel_avatar_url=self.channels[1].avatar_url,
                worker_name="worker-02",
                manager_name="manager-alpha",
                status="rendering",
                progress=42,
                queue_order=8,
                created_at=now - timedelta(hours=2),
                source_label="Local Upload",
                source_file_name="loop-mix-episode-2.mp4",
                thumbnail_url=None,
                assets=[
                    JobAsset(slot="video_loop", label="Video loop", source_mode="local", file_name="loop-mix-episode-2.mp4"),
                    JobAsset(slot="audio_loop", label="Audio loop", source_mode="local", file_name="loop-mix-episode-2.mp3"),
                ],
            ),
        ]
        self.live_streams = self._seed_live_streams(now=now) if live_demo_seed_enabled else []
        self.render_delete_meta = {
            "user": "admin",
            "deleted_at": now - timedelta(minutes=5),
        }
        self._ensure_state_db()
        self._ensure_auth_tables()
        self._load_or_seed_state()
        normalized_worker_names = self._normalize_known_worker_names()
        normalized_worker_created_at = self._normalize_worker_created_at()
        normalized_user_worker_assignments = self._normalize_user_worker_assignments()
        normalized_live_worker_names = self._normalize_live_worker_names()
        normalized_live_worker_created_at = self._normalize_live_worker_created_at()
        normalized_live_user_worker_assignments = self._normalize_live_user_worker_assignments()
        migrated_credentials = self._migrate_legacy_user_meta_passwords()
        self._bootstrap_auth_tables_from_memory_if_empty()
        self._load_auth_state_from_tables()
        normalized_visible_relationships = self._normalize_visible_admin_relationships()
        normalized_visible_live_relationships = self._normalize_visible_live_relationships()
        normalized_live_stream_assignments = self._normalize_live_stream_assignments()
        normalized_job_runtime_progress = self._normalize_job_runtime_progress()
        if (
            migrated_credentials
            or normalized_worker_names
            or normalized_worker_created_at
            or normalized_user_worker_assignments
            or normalized_live_worker_names
            or normalized_live_worker_created_at
            or normalized_live_user_worker_assignments
            or normalized_visible_relationships
            or normalized_visible_live_relationships
            or normalized_live_stream_assignments
            or normalized_job_runtime_progress
        ):
            self._save_state()

    def get_session_secret(self) -> str:
        return os.getenv("SESSION_SECRET", "dev-control-plane-session-secret")

    def get_worker_shared_secret(self) -> str:
        return os.getenv("WORKER_SHARED_SECRET", "dev-worker-shared-secret")

    @staticmethod
    def _max_active_jobs_per_worker() -> int:
        raw_value = str(os.getenv("WORKER_MAX_ACTIVE_JOBS_PER_VPS", "1")).strip()
        try:
            limit = int(raw_value)
        except (TypeError, ValueError):
            limit = 1
        return max(1, limit)

    @staticmethod
    def _fixed_assignment_threads() -> int:
        return 1

    def _normalize_requested_worker_threads(self, requested_threads: int | None) -> int:
        requested = max(1, int(requested_threads or 1))
        return min(requested, self._max_active_jobs_per_worker())

    def _effective_worker_thread_limit(self, worker: WorkerRecord) -> int:
        configured = max(1, int(worker.threads or worker.capacity or 1))
        return min(configured, self._max_active_jobs_per_worker())

    def get_upload_capabilities(self) -> UploadCapabilities:
        max_local_upload_bytes = int(os.getenv("MAX_LOCAL_UPLOAD_BYTES", "8589934592"))
        resumable_chunk_bytes = int(os.getenv("UPLOAD_CHUNK_BYTES", "8388608"))
        return UploadCapabilities(
            allow_local_upload=True,
            allow_resumable_upload=True,
            resumable_chunk_bytes=resumable_chunk_bytes,
            max_local_upload_bytes=max_local_upload_bytes,
        )

    @staticmethod
    def _google_oauth_scope() -> str:
        return "https://www.googleapis.com/auth/youtube.readonly https://www.googleapis.com/auth/youtube.upload openid email profile"

    @staticmethod
    def _oauth_callback_path() -> str:
        return "/auth/google/callback"

    def _ensure_state_db(self) -> None:
        with sqlite3.connect(self.state_db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    state_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _ensure_auth_tables(self) -> None:
        with sqlite3.connect(self.state_db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    manager_id TEXT,
                    manager_name TEXT,
                    telegram TEXT,
                    telegram_live TEXT,
                    created_at TEXT,
                    updated_at TEXT,
                    updated_by TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_credentials (
                    user_id TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    password_algo TEXT NOT NULL,
                    password_plain TEXT,
                    updated_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
                )
                """
            )
            try:
                connection.execute("ALTER TABLE auth_credentials ADD COLUMN password_plain TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                connection.execute("ALTER TABLE auth_users ADD COLUMN telegram_live TEXT")
            except sqlite3.OperationalError:
                pass
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_channel_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    UNIQUE(channel_id, user_id),
                    FOREIGN KEY(user_id) REFERENCES auth_users(id) ON DELETE CASCADE
                )
                """
            )
            connection.commit()

    @staticmethod
    def _password_iterations() -> int:
        return 390_000

    @classmethod
    def _hash_password(cls, password: str) -> str:
        normalized = password.strip()
        if not normalized:
            raise ValueError("Password không được để trống.")
        salt = secrets.token_hex(16)
        iterations = cls._password_iterations()
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            normalized.encode("utf-8"),
            bytes.fromhex(salt),
            iterations,
        ).hex()
        return f"pbkdf2_sha256${iterations}${salt}${digest}"

    @staticmethod
    def _verify_password_hash(password: str, password_hash: str) -> bool:
        try:
            algorithm, iteration_raw, salt, expected_digest = password_hash.split("$", 3)
            if algorithm != "pbkdf2_sha256":
                return False
            iterations = int(iteration_raw)
        except (TypeError, ValueError):
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.strip().encode("utf-8"),
            bytes.fromhex(salt),
            iterations,
        ).hex()
        return hmac.compare_digest(expected_digest, derived)

    def _set_user_password(self, user_id: str, password: str, *, updated_at: datetime | None = None) -> None:
        meta = self._user_meta_record(user_id)
        normalized_password = password.strip()
        meta["password_hash"] = self._hash_password(normalized_password)
        meta["password_algo"] = "pbkdf2_sha256"
        meta.pop("password", None)
        if updated_at is not None:
            meta["updated_at"] = updated_at

    def _migrate_legacy_user_meta_passwords(self) -> bool:
        changed = False
        for user_id in [user.id for user in self.users]:
            meta = self._user_meta_record(user_id)
            password_hash = str(meta.get("password_hash") or "").strip()
            legacy_password = str(meta.get("password") or "").strip()
            if password_hash:
                meta["password_algo"] = str(meta.get("password_algo") or "pbkdf2_sha256")
                continue
            if not legacy_password:
                continue
            self._set_user_password(user_id, legacy_password, updated_at=meta.get("updated_at"))
            changed = True
        return changed

    def _save_auth_state(self) -> None:
        with sqlite3.connect(self.state_db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM auth_channel_grants")
            connection.execute("DELETE FROM auth_credentials")
            connection.execute("DELETE FROM auth_users")

            manager_ids_by_username = {user.username: user.id for user in self.users if user.role == "manager"}
            for user in self.users:
                meta = self._user_meta_record(user.id)
                password_hash = str(meta.get("password_hash") or "").strip()
                if not password_hash:
                    raise ValueError(f"User {user.username} chưa có password_hash.")
                connection.execute(
                    """
                    INSERT INTO auth_users (
                        id, username, display_name, role, manager_id, manager_name, telegram, telegram_live, created_at, updated_at, updated_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user.id,
                        user.username,
                        user.display_name,
                        user.role,
                        manager_ids_by_username.get(user.manager_name or ""),
                        user.manager_name,
                        meta.get("telegram") or None,
                        meta.get("telegram_live") or None,
                        self._serialize_value(meta.get("created_at")),
                        self._serialize_value(meta.get("updated_at")),
                        meta.get("updated_by") or None,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO auth_credentials (user_id, password_hash, password_algo, password_plain, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user.id,
                        password_hash,
                        meta.get("password_algo") or "pbkdf2_sha256",
                        None,
                        self._serialize_value(meta.get("updated_at")),
                    ),
                )

            for link in self.channel_user_links:
                connection.execute(
                    """
                    INSERT INTO auth_channel_grants (id, channel_id, user_id)
                    VALUES (?, ?, ?)
                    """,
                    (link["id"], link["channel_id"], link["user_id"]),
                )

            connection.commit()

    def _bootstrap_auth_tables_from_memory_if_empty(self) -> None:
        with sqlite3.connect(self.state_db_path) as connection:
            row = connection.execute("SELECT COUNT(*) FROM auth_users").fetchone()
        if row and int(row[0] or 0) > 0:
            return
        self._save_auth_state()

    def _load_auth_state_from_tables(self) -> None:
        with sqlite3.connect(self.state_db_path) as connection:
            connection.row_factory = sqlite3.Row
            user_rows = connection.execute(
                """
                SELECT
                    users.id,
                    users.username,
                    users.display_name,
                    users.role,
                    users.manager_id,
                    users.manager_name,
                    users.telegram,
                    users.telegram_live,
                    users.created_at,
                    users.updated_at,
                    users.updated_by,
                    credentials.password_hash,
                    credentials.password_algo,
                    credentials.password_plain
                FROM auth_users AS users
                JOIN auth_credentials AS credentials ON credentials.user_id = users.id
                ORDER BY users.created_at ASC, users.username ASC
                """
            ).fetchall()
            grant_rows = connection.execute(
                "SELECT id, channel_id, user_id FROM auth_channel_grants ORDER BY id ASC"
            ).fetchall()

        if not user_rows:
            return

        self.users = []
        self.user_meta = {}
        for row in user_rows:
            self.users.append(
                UserSummary(
                    id=row["id"],
                    username=row["username"],
                    display_name=row["display_name"],
                    role=row["role"],
                    manager_id=row["manager_id"],
                    manager_name=row["manager_name"],
                    created_at=self._parse_datetime(row["created_at"]),
                    updated_at=self._parse_datetime(row["updated_at"]),
                    updated_by=row["updated_by"],
                    link_telegram=row["telegram"] or None,
                    link_telegram_live=row["telegram_live"] or None,
                )
            )
            self.user_meta[row["id"]] = {
                "password_hash": row["password_hash"],
                "password_algo": row["password_algo"] or "pbkdf2_sha256",
                "password": "",
                "telegram": row["telegram"] or "",
                "telegram_live": row["telegram_live"] or "",
                "updated_by": row["updated_by"] or "system",
                "updated_at": self._parse_datetime(row["updated_at"]),
                "created_at": self._parse_datetime(row["created_at"]) or self._now(),
            }

        self.channel_user_links = [
            {"id": int(row["id"]), "channel_id": row["channel_id"], "user_id": row["user_id"]}
            for row in grant_rows
        ]

    @staticmethod
    def _serialize_value(value: Any) -> Any:
        if isinstance(value, datetime):
            return AppStore._normalize_datetime(value).isoformat()
        if isinstance(value, dict):
            return {key: AppStore._serialize_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [AppStore._serialize_value(item) for item in value]
        return value

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value in (None, "", "-"):
            return None
        if isinstance(value, datetime):
            return AppStore._normalize_datetime(value)
        if isinstance(value, str):
            return AppStore._normalize_datetime(datetime.fromisoformat(value))
        raise ValueError(f"Unsupported datetime payload: {value!r}")

    def _mapping_numeric_id(self, value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            match = re.search(r"(\d+)$", str(value or "").strip())
            if match:
                return int(match.group(1))
            return 0

    def _restore_worker_connection_profiles(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        restored: dict[str, dict[str, Any]] = {}
        for worker_id, raw_profile in payload.items():
            normalized_worker_id = str(worker_id or "").strip()
            if not normalized_worker_id or not isinstance(raw_profile, dict):
                continue
            profile = dict(raw_profile)
            normalized_ssh_private_key = str(profile.get("ssh_private_key") or "").strip() or None
            restored[normalized_worker_id] = {
                "worker_id": normalized_worker_id,
                "vps_ip": str(profile.get("vps_ip") or "").strip(),
                "ssh_user": str(profile.get("ssh_user") or "").strip() or "root",
                "auth_mode": str(profile.get("auth_mode") or "").strip().lower() or ("ssh_key" if normalized_ssh_private_key else "password"),
                "password": str(profile.get("password") or "").strip() or None,
                "ssh_private_key": normalized_ssh_private_key,
                "updated_at": self._parse_datetime(profile.get("updated_at")),
            }
        return restored

    def _restore_worker_registration_meta(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        restored: dict[str, dict[str, Any]] = {}
        for worker_id, raw_meta in payload.items():
            normalized_worker_id = str(worker_id or "").strip()
            if not normalized_worker_id or not isinstance(raw_meta, dict):
                continue
            workspace_mode = self._normalize_workspace_mode(raw_meta.get("workspace_mode"))
            restored[normalized_worker_id] = {
                "worker_id": normalized_worker_id,
                "workspace_mode": workspace_mode,
                "source": str(raw_meta.get("source") or "").strip() or "register",
                "registered_at": self._parse_datetime(raw_meta.get("registered_at")) or self._now(trim=False),
                "task_id": str(raw_meta.get("task_id") or "").strip() or None,
                "operation_reason": str(raw_meta.get("operation_reason") or "").strip() or None,
                "manager_id": str(raw_meta.get("manager_id") or "").strip() or None,
                "manager_name": str(raw_meta.get("manager_name") or "").strip() or None,
            }
        return restored

    def _restore_worker_operation_tasks(self, payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for raw_task in payload:
            if not isinstance(raw_task, dict):
                continue
            task = dict(raw_task)
            task_id = str(task.get("id") or "").strip()
            worker_id = str(task.get("worker_id") or "").strip()
            if not task_id or not worker_id:
                continue
            workspace_mode = self._restore_worker_operation_workspace_mode(task)
            tasks.append(
                {
                    "id": task_id,
                    "worker_id": worker_id,
                    "worker_name": str(task.get("worker_name") or "").strip(),
                    "vps_ip": str(task.get("vps_ip") or "").strip(),
                    "control_plane_url": str(task.get("control_plane_url") or "").strip() or None,
                    "manager_id": str(task.get("manager_id") or "").strip() or None,
                    "manager_name": str(task.get("manager_name") or "").strip(),
                    "group": str(task.get("group") or "").strip(),
                    "workspace_mode": workspace_mode,
                    "kind": str(task.get("kind") or "install").strip(),
                    "transport": str(task.get("transport") or "").strip() or ("ssh" if str(task.get("kind") or "").strip() == "decommission" else ""),
                    "status": str(task.get("status") or "queued").strip(),
                    "message": str(task.get("message") or "").strip(),
                    "ssh_user": str(task.get("ssh_user") or "").strip() or "root",
                    "app_dir": str(task.get("app_dir") or "").strip() or "/opt/youtube-upload-lush",
                    "runtime_dir": str(task.get("runtime_dir") or "").strip() or "/opt/youtube-upload-lush-runtime",
                    "requested_by": str(task.get("requested_by") or "").strip() or "system",
                    "requested_role": str(task.get("requested_role") or "").strip() or "system",
                    "dispatched_at": self._parse_datetime(task.get("dispatched_at")),
                    "created_at": self._parse_datetime(task.get("created_at")) or self._now(trim=False),
                    "updated_at": self._parse_datetime(task.get("updated_at")) or self._now(trim=False),
                    "completed_at": self._parse_datetime(task.get("completed_at")),
                }
            )
        return tasks

    def _reconcile_registered_worker_operation_tasks(self) -> bool:
        changed = False
        remaining_tasks: list[dict[str, Any]] = []
        for raw_task in self.worker_operation_tasks:
            task = dict(raw_task)
            workspace_mode = self._restore_worker_operation_workspace_mode(task)
            if str(task.get("workspace_mode") or "").strip() != workspace_mode:
                task["workspace_mode"] = workspace_mode
                changed = True
            normalized_control_plane_url = str(task.get("control_plane_url") or "").strip() or None
            if task.get("control_plane_url") != normalized_control_plane_url:
                task["control_plane_url"] = normalized_control_plane_url
                changed = True

            worker_id = str(task.get("worker_id") or "").strip()
            if (
                str(task.get("kind") or "").strip() == "install"
                and str(task.get("status") or "").strip() == "awaiting_registration"
                and worker_id
            ):
                worker = next(
                    (
                        item
                        for item in self._workspace_worker_pool(workspace_mode)
                        if str(item.id or "").strip() == worker_id and str(item.status or "").strip() in {"online", "busy"}
                    ),
                    None,
                )
                if worker is not None:
                    changed = True
                    continue
            remaining_tasks.append(task)
        if changed:
            self.worker_operation_tasks = remaining_tasks
            self._save_state()
        return changed

    def _restore_deleted_workers(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        restored: dict[str, dict[str, Any]] = {}
        for worker_id, raw_meta in payload.items():
            normalized_worker_id = str(worker_id or "").strip()
            if not normalized_worker_id or not isinstance(raw_meta, dict):
                continue
            restored[normalized_worker_id] = {
                "worker_id": normalized_worker_id,
                "worker_name": str(raw_meta.get("worker_name") or "").strip() or normalized_worker_id,
                "vps_ip": str(raw_meta.get("vps_ip") or "").strip() or None,
                "deleted_by": str(raw_meta.get("deleted_by") or "").strip() or "system",
                "reason": str(raw_meta.get("reason") or "").strip() or "manual_delete",
                "deleted_at": self._parse_datetime(raw_meta.get("deleted_at")) or self._now(trim=False),
            }
        return restored

    def _serialize_state(self) -> dict[str, Any]:
        return {
            "users": [user.model_dump(mode="json") for user in self.users],
            "user_meta": self._serialize_value(self.user_meta),
            "workers": [worker.model_dump(mode="json") for worker in self.workers],
            "user_worker_links": self._serialize_value(self.user_worker_links),
            "live_workers": [worker.model_dump(mode="json") for worker in self.live_workers],
            "live_user_worker_links": self._serialize_value(self.live_user_worker_links),
            "channels": [channel.model_dump(mode="json") for channel in self.channels],
            "channel_user_links": self._serialize_value(self.channel_user_links),
            "jobs": [job.model_dump(mode="json") for job in self.jobs],
            "live_streams": [stream.model_dump(mode="json") for stream in self.live_streams],
            "upload_sessions": [session.model_dump(mode="json") for session in self.upload_sessions],
            "browser_sessions": [session.model_dump(mode="json") for session in self.browser_sessions],
            "browser_profile_cleanup_tasks": self._serialize_value(self.browser_profile_cleanup_tasks),
            "worker_round_robin_cursor": self._serialize_value(self.worker_round_robin_cursor),
            "worker_connection_profiles": self._serialize_value(self.worker_connection_profiles),
            "worker_registration_meta": self._serialize_value(self.worker_registration_meta),
            "worker_operation_tasks": self._serialize_value(self.worker_operation_tasks),
            "deleted_workers": self._serialize_value(self.deleted_workers),
            "inactive_bot_alert_last_sent_on": self.inactive_bot_alert_last_sent_on,
            "inactive_bot_alert_last_attempted_on": self.inactive_bot_alert_last_attempted_on,
            "render_delete_meta": self._serialize_value(self.render_delete_meta),
        }

    def _apply_state(self, payload: dict[str, Any]) -> None:
        self.users = [UserSummary.model_validate(item) for item in payload.get("users", [])]
        self.user_meta = {}
        for user_id, meta in (payload.get("user_meta") or {}).items():
            restored = dict(meta)
            restored["created_at"] = self._parse_datetime(restored.get("created_at"))
            restored["updated_at"] = self._parse_datetime(restored.get("updated_at"))
            self.user_meta[user_id] = restored

        self.workers = [WorkerRecord.model_validate(item) for item in payload.get("workers", [])]
        self.user_worker_links = list(payload.get("user_worker_links") or [])
        self.live_workers = [WorkerRecord.model_validate(item) for item in payload.get("live_workers", [])]
        self.live_user_worker_links = list(payload.get("live_user_worker_links") or [])
        self.channels = [ChannelRecord.model_validate(item) for item in payload.get("channels", [])]
        self.channel_user_links = list(payload.get("channel_user_links") or [])
        self.jobs = [RenderJobRecord.model_validate(item) for item in payload.get("jobs", [])]
        self.live_streams = [LiveStreamRecord.model_validate(item) for item in payload.get("live_streams", [])]
        self.upload_sessions = [UploadSessionRecord.model_validate(item) for item in payload.get("upload_sessions", [])]
        self.browser_sessions = [BrowserSessionRecord.model_validate(item) for item in payload.get("browser_sessions", [])]
        self.browser_profile_cleanup_tasks = list(payload.get("browser_profile_cleanup_tasks") or [])
        self.worker_round_robin_cursor = {
            str(worker_id or "").strip(): str(owner_key or "").strip()
            for worker_id, owner_key in (payload.get("worker_round_robin_cursor") or {}).items()
            if str(worker_id or "").strip() and str(owner_key or "").strip()
        }
        self.worker_connection_profiles = self._restore_worker_connection_profiles(
            payload.get("worker_connection_profiles") or {}
        )
        self.worker_registration_meta = self._restore_worker_registration_meta(
            payload.get("worker_registration_meta") or {}
        )
        self.worker_operation_tasks = self._restore_worker_operation_tasks(
            payload.get("worker_operation_tasks") or []
        )
        self.deleted_workers = self._restore_deleted_workers(payload.get("deleted_workers") or {})
        self.inactive_bot_alert_last_sent_on = (
            str(payload.get("inactive_bot_alert_last_sent_on") or "").strip() or None
        )
        self.inactive_bot_alert_last_attempted_on = (
            str(payload.get("inactive_bot_alert_last_attempted_on") or "").strip() or None
        )

        render_meta = payload.get("render_delete_meta") or {}
        self.render_delete_meta = {
            "user": render_meta.get("user") or "admin",
            "deleted_at": self._parse_datetime(render_meta.get("deleted_at"))
            or self._now(),
        }

    def _save_state(self) -> None:
        self._save_auth_state()
        payload = json.dumps(self._serialize_state(), ensure_ascii=False)
        with sqlite3.connect(self.state_db_path) as connection:
            connection.execute(
                """
                INSERT INTO app_state (state_key, payload, updated_at)
                VALUES ('main', ?, ?)
                ON CONFLICT(state_key) DO UPDATE
                SET payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (payload, self._now(trim=False).isoformat()),
            )
            connection.commit()

    def _load_or_seed_state(self) -> None:
        live_demo_seed_enabled = self._live_demo_seed_enabled()
        with sqlite3.connect(self.state_db_path) as connection:
            row = connection.execute("SELECT payload FROM app_state WHERE state_key = 'main'").fetchone()
        if row and row[0]:
            payload = json.loads(row[0])
            self._apply_state(payload)
            if "live_workers" not in payload:
                self.live_workers = self._seed_live_workers(now=self._now(trim=False)) if live_demo_seed_enabled else []
            if "live_user_worker_links" not in payload:
                self.live_user_worker_links = self._seed_live_user_worker_links() if live_demo_seed_enabled else []
            if "live_streams" not in payload:
                self.live_streams = self._seed_live_streams(now=self._now(trim=False)) if live_demo_seed_enabled else []
            if (
                "live_workers" not in payload
                or "live_user_worker_links" not in payload
                or "live_streams" not in payload
            ):
                self._save_state()
            return
        self._save_state()

    @staticmethod
    def _looks_like_ipv4(value: str | None) -> bool:
        normalized = str(value or "").strip()
        if not normalized:
            return False
        return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", normalized))

    def suggest_next_worker_bootstrap_id(self) -> str:
        return self._suggest_next_workspace_worker_bootstrap_id(
            [worker.id for worker in self.workers],
            prefix="worker",
        )

    def _suggest_next_workspace_worker_bootstrap_id(
        self,
        current_worker_ids: list[str],
        *,
        prefix: str,
    ) -> str:
        reserved_ids = [str(worker_id or "").strip() for worker_id in current_worker_ids if str(worker_id or "").strip()]
        reserved_ids.extend(
            str(task.get("worker_id") or "").strip()
            for task in self.worker_operation_tasks
            if str(task.get("worker_id") or "").strip()
            and str(task.get("status") or "").strip() not in {"completed", "failed"}
        )
        return suggest_next_worker_id(reserved_ids, prefix=prefix)

    def _remember_worker_connection_profile(
        self,
        worker_id: str,
        *,
        vps_ip: str,
        ssh_user: str,
        auth_mode: str | None = None,
        password: str | None = None,
        ssh_private_key: str | None = None,
    ) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        normalized_ip = str(vps_ip or "").strip()
        if not normalized_worker_id or not normalized_ip:
            return
        existing = dict(self.worker_connection_profiles.get(normalized_worker_id) or {})
        normalized_auth_mode = str(auth_mode or existing.get("auth_mode") or "").strip().lower()
        normalized_password = password if password is not None else existing.get("password")
        normalized_ssh_private_key = ssh_private_key if ssh_private_key is not None else existing.get("ssh_private_key")
        self.worker_connection_profiles[normalized_worker_id] = {
            "worker_id": normalized_worker_id,
            "vps_ip": normalized_ip,
            "ssh_user": str(ssh_user or "").strip() or "root",
            "auth_mode": normalized_auth_mode or ("ssh_key" if str(normalized_ssh_private_key or "").strip() else "password"),
            "password": str(normalized_password or "").strip() or None,
            "ssh_private_key": str(normalized_ssh_private_key or "").strip() or None,
            "updated_at": self._now(trim=False),
        }

    def _worker_registration_source_for_task(self, task: dict[str, Any] | None) -> str:
        if not isinstance(task, dict):
            return ""
        if str(task.get("operation_reason") or "").strip() == "workspace_conversion":
            return "workspace_conversion"
        if str(task.get("kind") or "").strip() == "install":
            return "create"
        return ""

    def _remember_worker_registration_meta(
        self,
        worker_id: str,
        *,
        workspace_mode: str,
        source: str,
        task: dict[str, Any] | None = None,
        manager_id: str | None = None,
        manager_name: str | None = None,
    ) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return
        normalized_source = str(source or "").strip() or "register"
        task_id = str((task or {}).get("id") or "").strip() or None
        operation_reason = str((task or {}).get("operation_reason") or "").strip() or None
        resolved_manager_id = str(manager_id or (task or {}).get("manager_id") or "").strip() or None
        resolved_manager_name = str(manager_name or (task or {}).get("manager_name") or "").strip() or None
        self.worker_registration_meta[normalized_worker_id] = {
            "worker_id": normalized_worker_id,
            "workspace_mode": self._normalize_workspace_mode(workspace_mode),
            "source": normalized_source,
            "registered_at": self._now(trim=False),
            "task_id": task_id,
            "operation_reason": operation_reason,
            "manager_id": resolved_manager_id,
            "manager_name": resolved_manager_name,
        }

    def _clear_worker_registration_meta(self, worker_id: str) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return
        self.worker_registration_meta.pop(normalized_worker_id, None)

    def _worker_registration_source_label(self, worker: WorkerRecord, *, live: bool) -> str:
        meta = dict(self.worker_registration_meta.get(str(worker.id or "").strip()) or {})
        source = str(meta.get("source") or "").strip()
        if not source:
            normalized_id = str(worker.id or "").strip().lower()
            expected_prefix = "live-worker-" if live else "worker-"
            source = "register" if normalized_id.startswith(expected_prefix) else "manual_register"
        return {
            "create": "create",
            "register": "register",
            "manual_register": "manual register",
            "workspace_conversion": "workspace conversion",
        }.get(source, source.replace("_", " ") or "register")

    def _remember_deleted_worker(
        self,
        worker_id: str,
        *,
        worker_name: str | None = None,
        vps_ip: str | None = None,
        deleted_by: str | None = None,
        reason: str = "manual_delete",
    ) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return
        normalized_vps_ip = str(vps_ip or "").strip() or None
        self.deleted_workers[normalized_worker_id] = {
            "worker_id": normalized_worker_id,
            "worker_name": str(worker_name or "").strip() or normalized_worker_id,
            "vps_ip": normalized_vps_ip,
            "deleted_by": str(deleted_by or "").strip() or "system",
            "reason": str(reason or "").strip() or "manual_delete",
            "deleted_at": self._now(trim=False),
        }

    def _clear_deleted_worker(self, worker_id: str) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return
        self.deleted_workers.pop(normalized_worker_id, None)

    def _ensure_worker_not_deleted(self, worker_id: str) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return
        deleted_meta = self.deleted_workers.get(normalized_worker_id)
        if not deleted_meta:
            return
        raise ValueError("BOT này đã bị xoá khỏi hệ thống. Hãy thêm BOT lại nếu muốn kết nối lại VPS này.")

    def get_worker_connection_profile(self, worker_id: str, *, workspace_mode: str = "upload") -> dict[str, Any]:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            raise KeyError(worker_id)
        resolved_workspace_mode = self._normalize_workspace_mode(workspace_mode)
        profile = dict(self.worker_connection_profiles.get(normalized_worker_id) or {})
        if not profile:
            worker = self._find_workspace_worker(normalized_worker_id, workspace_mode=resolved_workspace_mode)
            worker_name = str(
                self._resolve_workspace_worker_display_name(worker.id, workspace_mode=resolved_workspace_mode) or ""
            ).strip()
            if self._looks_like_ipv4(worker_name):
                profile = {
                    "worker_id": normalized_worker_id,
                    "vps_ip": worker_name,
                    "ssh_user": "root",
                    "updated_at": self._now(trim=False),
                }
        normalized_ip = str(profile.get("vps_ip") or "").strip()
        if not normalized_ip:
            raise ValueError("BOT này chưa có thông tin VPS IP để gỡ worker từ xa.")
        return {
            "worker_id": normalized_worker_id,
            "vps_ip": normalized_ip,
            "ssh_user": str(profile.get("ssh_user") or "").strip() or "root",
            "auth_mode": str(profile.get("auth_mode") or "").strip().lower() or ("ssh_key" if str(profile.get("ssh_private_key") or "").strip() else "password"),
            "password": str(profile.get("password") or "").strip() or None,
            "ssh_private_key": str(profile.get("ssh_private_key") or "").strip() or None,
            "updated_at": self._parse_datetime(profile.get("updated_at")),
        }

    def update_worker_connection_password(
        self,
        worker_id: str,
        password: str | None,
        *,
        workspace_mode: str = "upload",
        persist: bool = False,
    ) -> None:
        normalized_password = str(password or "").strip()
        if not normalized_password:
            return
        profile = self.get_worker_connection_profile(worker_id, workspace_mode=workspace_mode)
        self._remember_worker_connection_profile(
            worker_id,
            vps_ip=str(profile.get("vps_ip") or "").strip(),
            ssh_user=str(profile.get("ssh_user") or "").strip() or "root",
            auth_mode="password",
            password=normalized_password,
            ssh_private_key=str(profile.get("ssh_private_key") or "").strip() or None,
        )
        if persist:
            self._save_state()

    @staticmethod
    def _worker_operation_is_finished(task: dict[str, Any]) -> bool:
        return str(task.get("status") or "").strip() == "completed"

    def _has_pending_install_for_other_workspace(self, worker_id: str, *, workspace_mode: str) -> bool:
        normalized_worker_id = str(worker_id or "").strip()
        resolved_workspace_mode = self._normalize_workspace_mode(workspace_mode)
        if not normalized_worker_id:
            return False
        for task in self.worker_operation_tasks:
            if str(task.get("kind") or "").strip() != "install":
                continue
            if str(task.get("worker_id") or "").strip() != normalized_worker_id:
                continue
            if str(task.get("status") or "").strip() in {"completed", "failed"}:
                continue
            if self._restore_worker_operation_workspace_mode(task) == resolved_workspace_mode:
                continue
            return True
        return False

    def _finalize_workspace_conversion_source(self, task: dict[str, Any]) -> None:
        if str(task.get("operation_reason") or "").strip() != "workspace_conversion":
            return
        # Chỉ retire source BOT sau khi target worker đã register thành công để tránh conversion nửa vời.
        source_worker_id = str(task.get("conversion_source_worker_id") or "").strip()
        source_workspace_mode = self._normalize_workspace_mode(task.get("conversion_source_workspace_mode"))
        if not source_worker_id:
            return
        try:
            source_worker = self._find_workspace_worker(source_worker_id, workspace_mode=source_workspace_mode)
        except KeyError:
            self.worker_connection_profiles.pop(source_worker_id, None)
            return
        source_worker_name = self._resolve_workspace_worker_display_name(
            source_worker.id,
            workspace_mode=source_workspace_mode,
        )
        source_profile = dict(self.worker_connection_profiles.get(source_worker.id) or {})
        source_vps_ip = str(source_profile.get("vps_ip") or "").strip()
        if not source_vps_ip and self._looks_like_ipv4(source_worker_name):
            source_vps_ip = source_worker_name
        self._remember_deleted_worker(
            source_worker.id,
            worker_name=source_worker_name,
            vps_ip=source_vps_ip or None,
            deleted_by=str(task.get("requested_by") or "").strip() or "system",
            reason="workspace_conversion",
        )
        if source_workspace_mode == "live":
            self._delete_live_bot_state(source_worker.id)
        else:
            self._delete_bot_state(source_worker.id)
        self.worker_connection_profiles.pop(source_worker.id, None)

    def _find_worker_operation(self, operation_id: str) -> dict[str, Any]:
        normalized_id = str(operation_id or "").strip()
        for task in self.worker_operation_tasks:
            if str(task.get("id") or "").strip() == normalized_id:
                return task
        raise KeyError(operation_id)

    def _worker_operation_badge(self, task: dict[str, Any]) -> tuple[str, str]:
        kind = str(task.get("kind") or "").strip()
        status = str(task.get("status") or "").strip()
        mapping = {
            ("install", "queued"): (
                "Đang xếp hàng cài đặt...",
                "inline-flex items-center rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[10px] font-semibold text-indigo-700",
            ),
            ("install", "running"): (
                "Đang cài đặt...",
                "inline-flex items-center rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[10px] font-semibold text-indigo-700",
            ),
            ("install", "awaiting_registration"): (
                "Chờ BOT kết nối",
                "inline-flex items-center rounded-full border border-brand-100 bg-brand-50 px-2.5 py-1 text-[10px] font-semibold text-brand-700",
            ),
            ("install", "failed"): (
                "Cài đặt lỗi",
                "inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-2.5 py-1 text-[10px] font-semibold text-rose-700",
            ),
            ("decommission", "queued"): (
                "Đang xếp hàng gỡ BOT...",
                "inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-2.5 py-1 text-[10px] font-semibold text-rose-700",
            ),
            ("decommission", "running"): (
                "Đang gỡ BOT...",
                "inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-2.5 py-1 text-[10px] font-semibold text-rose-700",
            ),
            ("decommission", "failed"): (
                "Gỡ BOT lỗi",
                "inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-2.5 py-1 text-[10px] font-semibold text-rose-700",
            ),
        }
        return mapping.get(
            (kind, status),
            (
                "Đang xử lý",
                "inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700",
            ),
        )

    def _push_admin_notification(
        self,
        *,
        message: str,
        level: str = "info",
        manager_id: str | None = None,
        scope: str = "bot-ops",
    ) -> dict[str, Any]:
        notification = {
            "id": f"notice-{uuid4().hex[:12]}",
            "message": str(message or "").strip(),
            "level": str(level or "info").strip() or "info",
            "manager_id": str(manager_id or "").strip() or None,
            "scope": str(scope or "bot-ops").strip() or "bot-ops",
            "created_at": self._now(trim=False),
        }
        self.admin_notifications.append(notification)
        self.admin_notifications = self.admin_notifications[-100:]
        return deepcopy(notification)

    def _latest_admin_notification_id(
        self,
        *,
        manager_ids: list[str] | None = None,
        scope: str = "bot-ops",
    ) -> str:
        selected_ids = set(self._selected_manager_ids(manager_ids))
        latest_id = ""
        for item in self.admin_notifications:
            if str(item.get("scope") or "").strip() != scope:
                continue
            manager_id = str(item.get("manager_id") or "").strip()
            if selected_ids and manager_id not in selected_ids:
                continue
            latest_id = str(item.get("id") or "").strip()
        return latest_id

    def get_admin_notifications(
        self,
        *,
        manager_ids: list[str] | None = None,
        after_id: str | None = None,
        after_created_at: str | None = None,
        scope: str = "bot-ops",
    ) -> dict[str, Any]:
        selected_ids = set(self._selected_manager_ids(manager_ids))
        normalized_after_id = str(after_id or "").strip()
        normalized_after_created_at = str(after_created_at or "").strip()
        after_created_at_value = self._parse_datetime(normalized_after_created_at) if normalized_after_created_at else None
        latest_id = self._latest_admin_notification_id(manager_ids=manager_ids, scope=scope)
        if not normalized_after_id and after_created_at_value is None:
            return {"items": [], "cursor": latest_id}
        if normalized_after_id == latest_id:
            return {"items": [], "cursor": latest_id}

        def serialize_item(item: dict[str, Any]) -> dict[str, Any]:
            created_at_value = self._parse_datetime(item.get("created_at"))
            return {
                "id": str(item.get("id") or "").strip(),
                "message": str(item.get("message") or "").strip(),
                "level": str(item.get("level") or "info").strip() or "info",
                "created_at": self._format_full_datetime(created_at_value),
                "created_at_iso": created_at_value.isoformat() if created_at_value else "",
            }

        def collect_since_created_at() -> list[dict[str, Any]]:
            if after_created_at_value is None:
                return []
            items: list[dict[str, Any]] = []
            for item in self.admin_notifications:
                if str(item.get("scope") or "").strip() != scope:
                    continue
                manager_id = str(item.get("manager_id") or "").strip()
                if selected_ids and manager_id not in selected_ids:
                    continue
                created_at_value = self._parse_datetime(item.get("created_at"))
                if created_at_value is None or created_at_value <= after_created_at_value:
                    continue
                items.append(serialize_item(item))
            return items

        seen_after = False
        items: list[dict[str, Any]] = []
        for item in self.admin_notifications:
            if str(item.get("scope") or "").strip() != scope:
                continue
            item_id = str(item.get("id") or "").strip()
            if not seen_after:
                if item_id == normalized_after_id:
                    seen_after = True
                continue
            manager_id = str(item.get("manager_id") or "").strip()
            if selected_ids and manager_id not in selected_ids:
                continue
            items.append(serialize_item(item))
        if normalized_after_id and not seen_after and after_created_at_value is not None:
            return {"items": collect_since_created_at(), "cursor": latest_id}
        if not normalized_after_id:
            return {"items": collect_since_created_at(), "cursor": latest_id}
        return {"items": items, "cursor": latest_id}

    def fail_worker_operation(
        self,
        operation_id: str,
        *,
        message: str,
        level: str = "error",
    ) -> None:
        with self._worker_state_lock:
            task = self._find_worker_operation(operation_id)
            task_message = str(message or "").strip() or "Không thể hoàn tất thao tác BOT."
            kind = str(task.get("kind") or "").strip()
            worker_name = str(task.get("worker_name") or task.get("vps_ip") or task.get("worker_id") or "").strip()
            manager_id = str(task.get("manager_id") or "").strip() or None
            notice_prefix = "Cài đặt BOT" if kind == "install" else "Gỡ BOT"
            notice_message = f"{notice_prefix} {worker_name} thất bại: {task_message}"
            duplicated_notice = next(
                (
                    item
                    for item in reversed(self.admin_notifications)
                    if str(item.get("message") or "").strip() == notice_message
                    and str(item.get("level") or "").strip() == level
                    and str(item.get("manager_id") or "").strip() == str(manager_id or "").strip()
                ),
                None,
            )
            if duplicated_notice is None:
                self._push_admin_notification(
                    message=notice_message,
                    level=level,
                    manager_id=manager_id,
                )
            self.worker_operation_tasks = [
                existing
                for existing in self.worker_operation_tasks
                if str(existing.get("id") or "").strip() != str(operation_id or "").strip()
            ]
            self._save_state()
        self._resume_worker_operation_queue()

    def enqueue_worker_install_operation(
        self,
        *,
        worker_id: str,
        worker_name: str,
        vps_ip: str,
        threads: int = 1,
        ssh_user: str,
        control_plane_url: str | None = None,
        auth_mode: str = "password",
        password: str | None = None,
        ssh_private_key: str | None = None,
        manager_id: str | None,
        manager_name: str,
        group: str = "",
        workspace_mode: str = "upload",
        requested_by: str = "system",
        requested_role: str = "system",
        requested_user_id: str | None = None,
        post_install_config: dict[str, Any] | None = None,
        replace_other_workspace_mode: str | None = None,
        workspace_conversion: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._worker_state_lock:
            normalized_worker_id = str(worker_id or "").strip()
            normalized_ip = str(vps_ip or "").strip()
            resolved_workspace_mode = self._normalize_workspace_mode(workspace_mode)
            workspace_workers = self._workspace_worker_pool(resolved_workspace_mode)
            other_workspace_mode = "upload" if resolved_workspace_mode == "live" else "live"
            other_workspace_workers = self._workspace_worker_pool(other_workspace_mode)
            normalized_replace_other_workspace_mode = (
                self._normalize_workspace_mode(replace_other_workspace_mode)
                if str(replace_other_workspace_mode or "").strip()
                else None
            )
            normalized_workspace_conversion = workspace_conversion if isinstance(workspace_conversion, dict) else None
            source_worker_id = (
                str((normalized_workspace_conversion or {}).get("source_worker_id") or "").strip() or None
            )
            source_workspace_mode = (
                self._normalize_workspace_mode((normalized_workspace_conversion or {}).get("source_workspace_mode"))
                if str((normalized_workspace_conversion or {}).get("source_workspace_mode") or "").strip()
                else None
            )
            if not normalized_worker_id or not normalized_ip:
                raise ValueError("Worker bootstrap task thiếu worker_id hoặc VPS IP.")
            self._clear_deleted_worker(normalized_worker_id)
            if any(str(worker.id or "").strip() == normalized_worker_id for worker in workspace_workers):
                raise ValueError("Worker ID này đã tồn tại trong control-plane.")
            if any(
                str(
                    self._resolve_workspace_worker_display_name(
                        worker.id,
                        workspace_mode=resolved_workspace_mode,
                    )
                    or ""
                ).strip()
                == normalized_ip
                for worker in workspace_workers
            ):
                raise ValueError("VPS này đã tồn tại trong danh sách BOT.")
            conflicting_other_workspace_worker = next(
                (
                    worker
                    for worker in other_workspace_workers
                    if str(worker.id or "").strip() == normalized_worker_id
                    or str(
                        self._resolve_workspace_worker_display_name(
                            worker.id,
                            workspace_mode=other_workspace_mode,
                        )
                        or ""
                    ).strip()
                    == normalized_ip
                ),
                None,
            )
            if conflicting_other_workspace_worker is not None:
                if normalized_replace_other_workspace_mode != other_workspace_mode:
                    raise ValueError(
                        "VPS này đang được dùng ở workspace còn lại. Hãy gỡ BOT ở workspace kia trước khi cài lại."
                    )
                if source_worker_id:
                    if (
                        str(conflicting_other_workspace_worker.id or "").strip() != source_worker_id
                        or source_workspace_mode != other_workspace_mode
                    ):
                        raise ValueError("Không thể chuyển workspace khi VPS đang thuộc BOT khác.")
                elif str(conflicting_other_workspace_worker.id or "").strip() != normalized_worker_id:
                    raise ValueError("Không thể chuyển workspace khi VPS đang thuộc BOT khác.")
            removed_failed_worker_ids: set[str] = set()
            remaining_tasks: list[dict[str, Any]] = []
            for task in self.worker_operation_tasks:
                is_replaceable_failed_install = (
                    str(task.get("kind") or "").strip() == "install"
                    and str(task.get("status") or "").strip() == "failed"
                    and self._normalize_workspace_mode(task.get("workspace_mode")) == resolved_workspace_mode
                    and (
                        str(task.get("worker_id") or "").strip() == normalized_worker_id
                        or str(task.get("vps_ip") or "").strip() == normalized_ip
                    )
                )
                if is_replaceable_failed_install:
                    failed_worker_id = str(task.get("worker_id") or "").strip()
                    if failed_worker_id:
                        removed_failed_worker_ids.add(failed_worker_id)
                    continue
                remaining_tasks.append(task)
            self.worker_operation_tasks = remaining_tasks
            for failed_worker_id in removed_failed_worker_ids:
                if not any(str(worker.id or "").strip() == failed_worker_id for worker in workspace_workers):
                    self.worker_connection_profiles.pop(failed_worker_id, None)
            for task in self.worker_operation_tasks:
                if str(task.get("status") or "").strip() in {"completed", "failed"}:
                    continue
                if (
                    str(task.get("worker_id") or "").strip() == normalized_worker_id
                    or str(task.get("vps_ip") or "").strip() == normalized_ip
                ):
                    raise ValueError("BOT này đang có một tiến trình cài đặt hoặc gỡ BOT khác chưa xong.")
            now = self._now(trim=False)
            normalized_threads = (
                self._normalize_live_worker_threads(threads)
                if resolved_workspace_mode == "live"
                else self._normalize_requested_worker_threads(threads)
            )
            task = {
                "id": f"worker-op-{uuid4().hex[:10]}",
                "worker_id": normalized_worker_id,
                "worker_name": str(worker_name or normalized_ip).strip() or normalized_ip,
                "vps_ip": normalized_ip,
                "threads": normalized_threads,
                "control_plane_url": str(control_plane_url or "").strip() or None,
                "manager_id": str(manager_id or "").strip() or None,
                "manager_name": str(manager_name or "").strip() or "system",
                "group": str(group or "").strip(),
                "workspace_mode": resolved_workspace_mode,
                "kind": "install",
                "status": "queued",
                "message": "Đang chờ control-plane kết nối SSH vào VPS...",
                "requested_by": str(requested_by or "").strip() or "system",
                "requested_role": str(requested_role or "").strip() or "system",
                "requested_user_id": str(requested_user_id or "").strip() or None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            if normalized_workspace_conversion:
                task["operation_reason"] = "workspace_conversion"
                task["conversion_source_worker_id"] = source_worker_id
                task["conversion_source_workspace_mode"] = source_workspace_mode
                task["conversion_source_label"] = (
                    str(normalized_workspace_conversion.get("source_label") or "").strip() or None
                )
                task["conversion_target_label"] = (
                    str(normalized_workspace_conversion.get("target_label") or "").strip() or None
                )
            if isinstance(post_install_config, dict):
                normalized_post_install_threads = None
                if post_install_config.get("threads") is not None:
                    normalized_post_install_threads = (
                        self._normalize_live_worker_threads(post_install_config.get("threads"))
                        if resolved_workspace_mode == "live"
                        else self._normalize_requested_worker_threads(post_install_config.get("threads"))
                    )
                normalized_post_install_config = {
                    "name": str(post_install_config.get("name") or "").strip() or None,
                    "local": str(post_install_config.get("local") or "").strip() or None,
                    "group": str(post_install_config.get("group") or "").strip() or None,
                    "manager_id": str(post_install_config.get("manager_id") or "").strip() or None,
                    "live_role": str(post_install_config.get("live_role") or "").strip() or None,
                    "threads": normalized_post_install_threads,
                    "assigned_user_ids": [
                        str(value).strip()
                        for value in (post_install_config.get("assigned_user_ids") or [])
                        if str(value).strip()
                    ],
                }
                if any(
                    normalized_post_install_config.get(key)
                    for key in ("name", "local", "group", "manager_id", "live_role")
                ) or normalized_post_install_config["assigned_user_ids"]:
                    task["post_install_config"] = normalized_post_install_config
                    if resolved_workspace_mode == "live":
                        task["live_role"] = normalized_post_install_config.get("live_role")
            self.worker_operation_tasks.append(task)
            self._remember_worker_connection_profile(
                normalized_worker_id,
                vps_ip=normalized_ip,
                ssh_user=ssh_user,
                auth_mode=auth_mode,
                password=password,
                ssh_private_key=ssh_private_key,
            )
            self._save_state()
            return deepcopy(task)

    def enqueue_worker_decommission_operation(
        self,
        *,
        worker_id: str,
        vps_ip: str,
        ssh_user: str,
        transport: str = "ssh",
        app_dir: str = "/opt/youtube-upload-lush",
        runtime_dir: str = "/opt/youtube-upload-lush-runtime",
        workspace_mode: str = "upload",
        requested_by: str = "system",
        requested_role: str = "system",
    ) -> dict[str, Any]:
        with self._worker_state_lock:
            resolved_workspace_mode = self._normalize_workspace_mode(workspace_mode)
            worker = self._find_workspace_worker(worker_id, workspace_mode=resolved_workspace_mode)
            normalized_worker_id = str(worker.id or "").strip()
            normalized_ip = str(vps_ip or "").strip()
            if not normalized_ip:
                raise ValueError("BOT này chưa có VPS IP để gỡ worker từ xa.")
            for task in self.worker_operation_tasks:
                if self._worker_operation_is_finished(task):
                    continue
                if self._normalize_workspace_mode(task.get("workspace_mode")) != resolved_workspace_mode:
                    continue
                if str(task.get("worker_id") or "").strip() == normalized_worker_id:
                    raise ValueError("BOT này đang có một tiến trình cài đặt hoặc gỡ BOT khác chưa xong.")
            now = self._now(trim=False)
            task = {
                "id": f"worker-op-{uuid4().hex[:10]}",
                "worker_id": normalized_worker_id,
                "worker_name": self._resolve_workspace_worker_display_name(worker.id, workspace_mode=resolved_workspace_mode),
                "vps_ip": normalized_ip,
                "manager_id": worker.manager_id,
                "manager_name": worker.manager_name,
                "group": str(worker.group or "").strip(),
                "workspace_mode": resolved_workspace_mode,
                "kind": "decommission",
                "transport": str(transport or "ssh").strip() or "ssh",
                "status": "queued",
                "message": (
                    "Đang chờ control-plane kết nối SSH để gỡ worker khỏi VPS..."
                    if str(transport or "ssh").strip() != "self"
                    else "Đang chờ BOT tự gỡ khỏi VPS..."
                ),
                "ssh_user": str(ssh_user or "").strip() or "root",
                "app_dir": str(app_dir or "").strip() or "/opt/youtube-upload-lush",
                "runtime_dir": str(runtime_dir or "").strip() or "/opt/youtube-upload-lush-runtime",
                "requested_by": str(requested_by or "").strip() or "system",
                "requested_role": str(requested_role or "").strip() or "system",
                "dispatched_at": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
            }
            self.worker_operation_tasks.append(task)
            self._remember_worker_connection_profile(normalized_worker_id, vps_ip=normalized_ip, ssh_user=ssh_user)
            self._save_state()
            return deepcopy(task)

    def get_worker_decommission_task(self, worker_id: str, shared_secret: str) -> dict[str, Any] | None:
        with self._worker_state_lock:
            worker = self._authenticate_worker(worker_id, shared_secret)
            now = self._now(trim=False)
            task = next(
                (
                    item
                    for item in self.worker_operation_tasks
                    if str(item.get("kind") or "").strip() == "decommission"
                    and str(item.get("transport") or "").strip() == "self"
                    and str(item.get("worker_id") or "").strip() == worker.id
                    and str(item.get("status") or "").strip() in {"queued", "running"}
                ),
                None,
            )
            if task is None:
                return None
            dispatched_at = self._parse_datetime(task.get("dispatched_at"))
            if dispatched_at and now - dispatched_at < timedelta(seconds=20):
                return None
            task["status"] = "running"
            task["message"] = "BOT đang tự gỡ service và dữ liệu khỏi VPS..."
            task["updated_at"] = now
            task["dispatched_at"] = now
            self._save_state()
            return deepcopy(task)

    def complete_worker_decommission_task(
        self,
        *,
        operation_id: str,
        worker_id: str,
        shared_secret: str,
        status: str,
        message: str | None = None,
    ) -> None:
        cleaned_status = str(status or "").strip().lower() or "completed"
        with self._worker_state_lock:
            worker = self._authenticate_worker(worker_id, shared_secret)
            task = self._find_worker_operation(operation_id)
            if str(task.get("kind") or "").strip() != "decommission":
                raise ValueError("Task gỡ BOT không hợp lệ.")
            if str(task.get("worker_id") or "").strip() != worker.id:
                raise ValueError("Task gỡ BOT không thuộc worker hiện tại.")
            task_message = str(message or "").strip()
            if cleaned_status == "completed":
                worker_name = self._resolve_worker_display_name(worker.id)
                self._remember_deleted_worker(
                    worker.id,
                    worker_name=worker_name,
                    vps_ip=str(task.get("vps_ip") or "").strip() or None,
                    deleted_by=str(task.get("requested_by") or "").strip() or "system",
                    reason="self_decommission",
                )
                if task_message:
                    task["message"] = task_message
                self.finalize_decommissioned_bot(worker.id, operation_id)
                return
        if cleaned_status == "completed":
            return
        self.fail_worker_operation(operation_id, message=task_message or "BOT tự gỡ khỏi VPS thất bại.")

    def update_worker_operation(
        self,
        operation_id: str,
        *,
        status: str | None = None,
        message: str | None = None,
        completed: bool = False,
    ) -> dict[str, Any]:
        with self._worker_state_lock:
            task = self._find_worker_operation(operation_id)
            now = self._now(trim=False)
            if status:
                task["status"] = str(status).strip()
            if message is not None:
                task["message"] = str(message).strip()
            task["updated_at"] = now
            task["completed_at"] = now if completed else None
            self._save_state()
            return deepcopy(task)

    def get_worker_operation_snapshots(self) -> list[dict[str, Any]]:
        with self._worker_state_lock:
            return deepcopy(self.worker_operation_tasks)

    def clear_install_operation_after_register(self, worker_id: str, *, workspace_mode: str = "upload") -> bool:
        completed_task: dict[str, Any] | None = None
        changed = False
        with self._worker_state_lock:
            normalized_worker_id = str(worker_id or "").strip()
            resolved_workspace_mode = self._normalize_workspace_mode(workspace_mode)
            completed_task = next(
                (
                    deepcopy(task)
                    for task in self.worker_operation_tasks
                    if str(task.get("kind") or "").strip() == "install"
                    and str(task.get("worker_id") or "").strip() == normalized_worker_id
                    and self._restore_worker_operation_workspace_mode(task) == resolved_workspace_mode
                ),
                None,
            )
            before = len(self.worker_operation_tasks)
            self.worker_operation_tasks = [
                task
                for task in self.worker_operation_tasks
                if not (
                    str(task.get("kind") or "").strip() == "install"
                    and str(task.get("worker_id") or "").strip() == normalized_worker_id
                    and self._restore_worker_operation_workspace_mode(task) == resolved_workspace_mode
                )
            ]
            changed = len(self.worker_operation_tasks) != before
            if changed:
                self._save_state()
        if changed:
            self._resume_worker_operation_queue()
        if changed and completed_task is not None:
            worker = self._find_workspace_worker(normalized_worker_id, workspace_mode=resolved_workspace_mode)
            worker_name = self._resolve_workspace_worker_display_name(worker.id, workspace_mode=resolved_workspace_mode)
            vps_ip = str(completed_task.get("vps_ip") or "").strip() or worker_name
            self._notify_telegram_chat_ids(
                self._bot_operation_recipient_chat_ids(completed_task),
                self._bot_install_result_message(
                    completed_task,
                    worker_name=worker_name,
                    worker_id=worker.id,
                    vps_ip=vps_ip,
                ),
            )
        return changed

    def finalize_decommissioned_bot(self, worker_id: str, operation_id: str) -> None:
        completed_task: dict[str, Any] | None = None
        worker_name = ""
        vps_ip = ""
        with self._worker_state_lock:
            task = next(
                (
                    item
                    for item in self.worker_operation_tasks
                    if str(item.get("id") or "").strip() == str(operation_id or "").strip()
                ),
                {},
            )
            resolved_workspace_mode = self._normalize_workspace_mode(task.get("workspace_mode"))
            worker = self._find_workspace_worker(worker_id, workspace_mode=resolved_workspace_mode)
            completed_task = deepcopy(task) if task else None
            worker_name = self._resolve_workspace_worker_display_name(worker.id, workspace_mode=resolved_workspace_mode)
            vps_ip = str(task.get("vps_ip") or "").strip() or worker_name
            self._remember_deleted_worker(
                worker.id,
                worker_name=worker_name,
                vps_ip=str(task.get("vps_ip") or "").strip() or None,
                deleted_by=str(task.get("requested_by") or "").strip() or "system",
                reason=str(task.get("transport") or "").strip() or "decommission",
            )
            if resolved_workspace_mode == "live":
                self._delete_live_bot_state(worker_id)
            else:
                self._delete_bot_state(worker_id)
            self.worker_operation_tasks = [
                task
                for task in self.worker_operation_tasks
                if str(task.get("id") or "").strip() != str(operation_id or "").strip()
            ]
            self.worker_connection_profiles.pop(str(worker_id or "").strip(), None)
            self._save_state()
        if completed_task is not None:
            self._notify_telegram_chat_ids(
                self._bot_operation_recipient_chat_ids(completed_task),
                self._bot_decommission_completed_message(
                    completed_task,
                    worker_name=worker_name,
                    worker_id=str(worker_id or "").strip(),
                    vps_ip=vps_ip,
                ),
            )

    def start_background_services(self) -> None:
        with self._worker_state_lock:
            if self._monitor_thread and self._monitor_thread.is_alive():
                return
            self._reconcile_worker_connectivity(now=self._now(trim=False))
            self._reconcile_registered_worker_operation_tasks()
            self._monitor_stop_event.clear()
            self._monitor_thread = Thread(
                target=self._worker_monitor_loop,
                name="control-plane-worker-monitor",
                daemon=True,
            )
            self._monitor_thread.start()
        self._resume_worker_operation_queue()

    def stop_background_services(self) -> None:
        self._monitor_stop_event.set()
        monitor_thread = self._monitor_thread
        if monitor_thread and monitor_thread.is_alive():
            monitor_thread.join(timeout=2.0)
        self._monitor_thread = None

    def _worker_monitor_loop(self) -> None:
        interval_seconds = self._worker_monitor_interval_seconds()
        while not self._monitor_stop_event.wait(interval_seconds):
            try:
                now = self._now(trim=False)
                upload_interruption_notifications: list[tuple[list[str], str]] = []
                with self._worker_state_lock:
                    self._reconcile_worker_connectivity(now=now)
                    _, upload_interruption_notifications = self._reconcile_expired_worker_jobs(now=now)
                    self._reconcile_expired_live_streams(now=now)
                    self._reconcile_live_worker_connectivity(now=now)
                for chat_ids, message in upload_interruption_notifications:
                    self._notify_live_telegram_chat_ids(chat_ids, message)
                self._reconcile_inactive_bot_daily_alert(now=now)
            except Exception as exc:
                print(f"[worker_monitor] reconcile failed: {exc}", flush=True)

    def _send_telegram_message(self, message: str, *, bot_token: str, chat_id: str | None) -> bool:
        try:
            resolved_chat_id = self._normalize_telegram_chat_id(chat_id)
        except ValueError:
            return False
        if not bot_token or not resolved_chat_id:
            return False

        encoded_message = urlencode(
            {
                "chat_id": resolved_chat_id,
                "text": message,
            }
        ).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=encoded_message,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return bool(payload.get("ok"))
        except Exception as exc:
            print(f"[telegram_alert] failed: {exc}", flush=True)
            return False

    def _send_telegram_alert(self, message: str, *, chat_id: str | None = None) -> bool:
        return self._send_telegram_message(
            message,
            bot_token=self._telegram_alert_bot_token(),
            chat_id=chat_id or self._telegram_alert_chat_id(),
        )

    def _send_telegram_live_alert(self, message: str, *, chat_id: str | None = None) -> bool:
        return self._send_telegram_message(
            message,
            bot_token=self._telegram_live_bot_token(),
            chat_id=chat_id,
        )

    def _telegram_api_payload_with_bot(
        self,
        method: str,
        *,
        bot_token: str,
        bot_label: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not bot_token:
            raise ValueError(f"{bot_label} chưa được cấu hình.")

        normalized_method = str(method or "").strip().strip("/")
        if not normalized_method:
            raise ValueError("Telegram API method không hợp lệ.")

        request_url = f"https://api.telegram.org/bot{bot_token}/{normalized_method}"
        if params:
            request_url = f"{request_url}?{urlencode(params)}"

        request = Request(
            request_url,
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise ValueError("Không thể kết nối Telegram Bot API.") from exc

        if not payload.get("ok"):
            description = str(payload.get("description") or "").strip() or "Telegram Bot API trả về lỗi."
            raise ValueError(description)
        return payload

    def _telegram_api_payload(self, method: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self._telegram_api_payload_with_bot(
            method,
            bot_token=self._telegram_alert_bot_token(),
            bot_label="Bot Telegram thông báo",
            params=params,
        )

    def _telegram_live_api_payload(self, method: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        return self._telegram_api_payload_with_bot(
            method,
            bot_token=self._telegram_live_bot_token(),
            bot_label="Bot Telegram livestream",
            params=params,
        )

    def _telegram_bot_identity_with_bot(self, *, bot_token: str, bot_label: str) -> dict[str, str]:
        payload = self._telegram_api_payload_with_bot("getMe", bot_token=bot_token, bot_label=bot_label)
        result = payload.get("result") or {}
        username = str(result.get("username") or "").strip()
        first_name = str(result.get("first_name") or "").strip()
        if not username:
            raise ValueError("Bot Telegram chưa có username công khai.")
        return {
            "username": username,
            "display_name": first_name or username,
            "deep_link_base": f"https://t.me/{username}",
        }

    def _telegram_bot_identity(self) -> dict[str, str]:
        return self._telegram_bot_identity_with_bot(
            bot_token=self._telegram_alert_bot_token(),
            bot_label="Bot Telegram thông báo",
        )

    def _telegram_live_bot_identity(self) -> dict[str, str]:
        return self._telegram_bot_identity_with_bot(
            bot_token=self._telegram_live_bot_token(),
            bot_label="Bot Telegram livestream",
        )

    @staticmethod
    def _extract_telegram_link_code(text: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            return ""
        for pattern in (
            r"^/start(?:@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]+)$",
            r"^/link(?:@[A-Za-z0-9_]+)?\s+([A-Za-z0-9_-]+)$",
        ):
            match = re.fullmatch(pattern, normalized)
            if match:
                return str(match.group(1) or "").strip()
        return ""

    def _cleanup_expired_telegram_link_requests(self, *, now: datetime | None = None) -> None:
        current_time = now or self._now(trim=False)
        ttl = timedelta(seconds=self._telegram_link_ttl_seconds())
        expired_codes = [
            code
            for code, request in self.telegram_link_requests.items()
            if current_time - request.get("created_at", current_time) > ttl
        ]
        for code in expired_codes:
            self.telegram_link_requests.pop(code, None)

    def _cleanup_expired_live_telegram_link_requests(self, *, now: datetime | None = None) -> None:
        current_time = now or self._now(trim=False)
        ttl = timedelta(seconds=self._telegram_live_link_ttl_seconds())
        expired_codes = [
            code
            for code, request in self.live_telegram_link_requests.items()
            if current_time - request.get("created_at", current_time) > ttl
        ]
        for code in expired_codes:
            self.live_telegram_link_requests.pop(code, None)

    def create_telegram_link_request(self, user_id: str) -> dict[str, Any]:
        self._find_user(user_id)
        now = self._now(trim=False)
        self._cleanup_expired_telegram_link_requests(now=now)
        bot_identity = self._telegram_bot_identity()
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")
        expires_at = now + timedelta(seconds=self._telegram_link_ttl_seconds())
        self.telegram_link_requests[code] = {
            "user_id": user_id,
            "code": code,
            "created_at": now,
            "expires_at": expires_at,
            "chat_id": "",
            "telegram_username": "",
            "telegram_name": "",
        }
        return {
            "code": code,
            "bot_username": bot_identity["username"],
            "bot_display_name": bot_identity["display_name"],
            "deep_link_url": f"{bot_identity['deep_link_base']}?start={quote(code)}",
            "expires_in_seconds": self._telegram_link_ttl_seconds(),
        }

    def create_live_telegram_link_request(self, user_id: str) -> dict[str, Any]:
        self._require_workspace_user(user_id)
        now = self._now(trim=False)
        self._cleanup_expired_live_telegram_link_requests(now=now)
        bot_identity = self._telegram_live_bot_identity()
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "")
        expires_at = now + timedelta(seconds=self._telegram_live_link_ttl_seconds())
        self.live_telegram_link_requests[code] = {
            "user_id": user_id,
            "code": code,
            "created_at": now,
            "expires_at": expires_at,
            "chat_id": "",
            "telegram_username": "",
            "telegram_name": "",
        }
        return {
            "code": code,
            "bot_username": bot_identity["username"],
            "bot_display_name": bot_identity["display_name"],
            "deep_link_url": f"{bot_identity['deep_link_base']}?start={quote(code)}",
            "expires_in_seconds": self._telegram_live_link_ttl_seconds(),
        }

    def get_telegram_link_request_status(self, user_id: str, code: str) -> dict[str, Any]:
        cleaned_code = str(code or "").strip()
        if not cleaned_code:
            raise ValueError("Thiếu mã liên kết Telegram.")

        now = self._now(trim=False)
        self._cleanup_expired_telegram_link_requests(now=now)
        request_state = self.telegram_link_requests.get(cleaned_code)
        if request_state is None or str(request_state.get("user_id") or "").strip() != str(user_id or "").strip():
            raise KeyError("Mã liên kết Telegram đã hết hạn hoặc không tồn tại.")

        expires_at = request_state.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at <= now:
            self.telegram_link_requests.pop(cleaned_code, None)
            raise ValueError("Mã liên kết Telegram đã hết hạn. Hãy tạo lại mã mới.")

        if not request_state.get("chat_id"):
            payload = self._telegram_api_payload("getUpdates", params={"limit": "100"})
            updates = payload.get("result") or []
            for update in reversed(updates):
                message = update.get("message") or update.get("edited_message") or {}
                matched_code = self._extract_telegram_link_code(message.get("text") or "")
                if matched_code != cleaned_code:
                    continue
                chat = message.get("chat") or {}
                chat_id = self._normalize_telegram_chat_id(str(chat.get("id") or "").strip())
                if not chat_id:
                    continue
                sender = message.get("from") or {}
                display_name = " ".join(
                    [
                        str(sender.get("first_name") or "").strip(),
                        str(sender.get("last_name") or "").strip(),
                    ]
                ).strip()
                request_state["chat_id"] = chat_id
                request_state["telegram_username"] = str(sender.get("username") or "").strip()
                request_state["telegram_name"] = display_name
                break

        chat_id = str(request_state.get("chat_id") or "").strip()
        if chat_id:
            return {
                "status": "linked",
                "chat_id": chat_id,
                "telegram_username": str(request_state.get("telegram_username") or "").strip(),
                "telegram_name": str(request_state.get("telegram_name") or "").strip(),
            }

        expires_in_seconds = 0
        if isinstance(expires_at, datetime):
            expires_in_seconds = max(0, int((expires_at - now).total_seconds()))
        return {
            "status": "pending",
            "expires_in_seconds": expires_in_seconds,
        }

    def get_live_telegram_link_request_status(self, user_id: str, code: str) -> dict[str, Any]:
        cleaned_code = str(code or "").strip()
        if not cleaned_code:
            raise ValueError("Thiếu mã liên kết Telegram live.")

        now = self._now(trim=False)
        self._cleanup_expired_live_telegram_link_requests(now=now)
        request_state = self.live_telegram_link_requests.get(cleaned_code)
        if request_state is None or str(request_state.get("user_id") or "").strip() != str(user_id or "").strip():
            raise KeyError("Mã liên kết Telegram live đã hết hạn hoặc không tồn tại.")

        expires_at = request_state.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at <= now:
            self.live_telegram_link_requests.pop(cleaned_code, None)
            raise ValueError("Mã liên kết Telegram live đã hết hạn. Hãy tạo lại mã mới.")

        if not request_state.get("chat_id"):
            payload = self._telegram_live_api_payload("getUpdates", params={"limit": "100"})
            updates = payload.get("result") or []
            for update in reversed(updates):
                message = update.get("message") or update.get("edited_message") or {}
                matched_code = self._extract_telegram_link_code(message.get("text") or "")
                if matched_code != cleaned_code:
                    continue
                chat = message.get("chat") or {}
                chat_id = self._normalize_telegram_chat_id(str(chat.get("id") or "").strip())
                if not chat_id:
                    continue
                sender = message.get("from") or {}
                display_name = " ".join(
                    [
                        str(sender.get("first_name") or "").strip(),
                        str(sender.get("last_name") or "").strip(),
                    ]
                ).strip()
                request_state["chat_id"] = chat_id
                request_state["telegram_username"] = str(sender.get("username") or "").strip()
                request_state["telegram_name"] = display_name
                break

        chat_id = str(request_state.get("chat_id") or "").strip()
        if chat_id:
            current_chat_id = self._user_telegram_live_chat_id(user_id)
            if current_chat_id != chat_id:
                self.set_live_telegram_chat_id(
                    user_id,
                    chat_id,
                    updated_by="telegram_live_link",
                )
            return {
                "status": "linked",
                "chat_id": chat_id,
                "telegram_username": str(request_state.get("telegram_username") or "").strip(),
                "telegram_name": str(request_state.get("telegram_name") or "").strip(),
            }

        expires_in_seconds = 0
        if isinstance(expires_at, datetime):
            expires_in_seconds = max(0, int((expires_at - now).total_seconds()))
        return {
            "status": "pending",
            "expires_in_seconds": expires_in_seconds,
        }

    def _telegram_linked_confirmation_message(self, user: UserSummary) -> str:
        return (
            "Tài khoản Telegram này đã được thêm để nhận thông báo từ app Youtube Lush.\n"
            f"Tài khoản app: {user.username}\n"
            "Nếu muốn ngắt thông báo, hãy xóa Telegram ID trong mục Cập nhật user rồi lưu lại."
        )

    def _telegram_unlinked_confirmation_message(self, user: UserSummary) -> str:
        return (
            "Tài khoản Telegram này đã được ngắt khỏi hệ thống thông báo của app Youtube Lush.\n"
            f"Tài khoản app: {user.username}\n"
            "Từ bây giờ tài khoản này sẽ không nhận thông báo mới từ app nữa."
        )

    def _telegram_live_linked_confirmation_message(self, user: UserSummary) -> str:
        return (
            "Tài khoản Telegram này đã được thêm để nhận thông báo livestream từ app Youtube Lush.\n"
            f"Tài khoản app: {user.username}\n"
            "Từ bây giờ bạn sẽ nhận thông báo tạo lịch, bắt đầu live, lỗi, dừng/kết thúc, backup và mất kết nối."
        )

    def _telegram_live_unlinked_confirmation_message(self, user: UserSummary) -> str:
        return (
            "Tài khoản Telegram này đã được ngắt khỏi hệ thống thông báo livestream của app Youtube Lush.\n"
            f"Tài khoản app: {user.username}\n"
            "Từ bây giờ tài khoản này sẽ không nhận thông báo livestream mới nữa."
        )

    def _live_telegram_binding_snapshot(self, user_id: str) -> dict[str, Any]:
        linked_chat_id = self._user_telegram_live_chat_id(user_id)
        return {
            "linked": bool(linked_chat_id),
            "chat_id": linked_chat_id,
            "notifications_enabled": self._telegram_live_notifications_enabled(),
            "bot_configured": bool(self._telegram_live_bot_token()),
        }

    def get_live_telegram_binding(self, user_id: str) -> dict[str, Any]:
        user = self._require_workspace_user(user_id)
        payload = self._live_telegram_binding_snapshot(user.id)
        try:
            bot_identity = self._telegram_live_bot_identity()
        except ValueError as exc:
            payload.update(
                {
                    "bot_username": "",
                    "bot_display_name": "",
                    "deep_link_base": "",
                    "bot_error": str(exc),
                }
            )
            return payload

        payload.update(
            {
                "bot_username": bot_identity["username"],
                "bot_display_name": bot_identity["display_name"],
                "deep_link_base": bot_identity["deep_link_base"],
                "bot_error": "",
            }
        )
        return payload

    def set_live_telegram_chat_id(
        self,
        user_id: str,
        chat_id: str | None,
        *,
        updated_by: str | None = None,
    ) -> dict[str, Any]:
        user = self._require_workspace_user(user_id)
        meta = self._user_meta_record(user.id)
        previous_chat_id = self._user_telegram_live_chat_id(user.id)
        next_chat_id = self._normalize_telegram_chat_id(chat_id) if str(chat_id or "").strip() else ""
        meta["telegram_live"] = next_chat_id
        meta["updated_by"] = updated_by or user.username
        meta["updated_at"] = self._now(trim=False)
        self._save_state()

        if next_chat_id and next_chat_id != previous_chat_id:
            self._send_telegram_live_alert(
                self._telegram_live_linked_confirmation_message(user),
                chat_id=next_chat_id,
            )
        elif previous_chat_id and not next_chat_id:
            self._send_telegram_live_alert(
                self._telegram_live_unlinked_confirmation_message(user),
                chat_id=previous_chat_id,
            )
        return self.get_live_telegram_binding(user.id)

    @staticmethod
    def _live_notification_mode_label(stream: LiveStreamRecord) -> str:
        return "24/7" if stream.is_forever else (stream.live_label or "Theo lịch")

    def _live_notification_start_label(self, stream: LiveStreamRecord) -> str:
        if stream.start_time_live is None:
            return "Ngay bây giờ"
        return self._format_full_datetime(stream.start_time_live)

    def _live_notification_end_label(self, stream: LiveStreamRecord) -> str:
        if stream.is_forever or stream.end_time_live is None:
            return "24/7"
        return self._format_full_datetime(stream.end_time_live)

    def _visible_live_stream_for_notification(self, stream: LiveStreamRecord) -> LiveStreamRecord:
        if not self._is_runtime_backup_clone(stream):
            return stream
        parent_stream_id = str(stream.parent_stream_id or "").strip()
        if not parent_stream_id:
            return stream
        parent_stream = self._find_live_stream_optional(parent_stream_id)
        if parent_stream is None:
            return stream
        return parent_stream

    @staticmethod
    def _strip_backup_worker_suffix(name: str | None) -> str:
        normalized = str(name or "").strip()
        lowered = normalized.lower()
        if lowered.endswith(" - t"):
            return normalized[:-4].rstrip()
        if lowered.endswith("-t"):
            return normalized[:-2].rstrip()
        return normalized

    def _live_backup_worker_display_name(self, worker_id: str | None, stored_name: str | None = None) -> str:
        cleaned_name = self._strip_backup_worker_suffix(stored_name)
        if cleaned_name:
            return cleaned_name
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return ""
        return self._resolve_live_worker_display_name(normalized_worker_id)

    def _live_notification_base_lines(self, stream: LiveStreamRecord) -> list[str]:
        visible_stream = self._visible_live_stream_for_notification(stream)
        lines = [
            f"Tài khoản: {visible_stream.owner_username}",
            f"Tên luồng: {visible_stream.stream_name}",
            f"Kiểu live: {self._live_notification_mode_label(visible_stream)}",
            f"BOT chính: {visible_stream.primary_worker_name or self._resolve_live_worker_display_name(visible_stream.primary_worker_id)}",
        ]
        if visible_stream.backup_worker_id:
            lines.append(
                f"BOT backup: {self._live_backup_worker_display_name(visible_stream.backup_worker_id, visible_stream.backup_worker_name)}"
            )
        lines.append(f"Bắt đầu: {self._live_notification_start_label(visible_stream)}")
        lines.append(f"Kết thúc: {self._live_notification_end_label(visible_stream)}")
        return lines

    def _live_stream_created_message(self, stream: LiveStreamRecord) -> str:
        visible_stream = self._visible_live_stream_for_notification(stream)
        title = "[LIVE] Đã tạo lịch live" if visible_stream.start_time_live else "[LIVE] Đã tạo luồng live"
        return "\n".join([title, *self._live_notification_base_lines(visible_stream)])

    def _live_stream_started_message(self, stream: LiveStreamRecord, *, now: datetime) -> str:
        visible_stream = self._visible_live_stream_for_notification(stream)
        return "\n".join(
            [
                "[LIVE] Bắt đầu live",
                *self._live_notification_base_lines(visible_stream),
                f"Bắt đầu lúc: {self._format_full_datetime(now)}",
            ]
        )

    def _summarize_live_error_message(self, error_message: str | None) -> str | None:
        normalized = self._normalize_live_optional_text(error_message)
        if not normalized:
            return None

        lowered = normalized.casefold()
        if "stream map '0:v:0' matches no streams" in lowered:
            if "input #0, mp3" in lowered or "audio: mp3" in lowered:
                return "Link video đang nhập là link file MP3, vui lòng sửa lại link file thành video."
            return "Link video không có video stream hợp lệ, vui lòng đổi sang file video."
        if "stream map '1:a:0' matches no streams" in lowered:
            return "Link audio đang nhập không có audio stream hợp lệ."
        if (
            ("could not write header for output file" in lowered or "incorrect codec parameters" in lowered)
            and any(codec in lowered for codec in {"opus", "vorbis", "webm"})
        ):
            return "Link audio đang nhập chưa đúng định dạng, vui lòng dùng MP3 hoặc AAC."
        if "invalid data found when processing input" in lowered:
            return "File nguồn không hợp lệ hoặc đã bị hỏng."
        if "permission denied" in lowered or "403 forbidden" in lowered:
            return "Không tải được file nguồn do link đang bị chặn truy cập."
        if "404 not found" in lowered:
            return "Không tìm thấy file nguồn từ link đã nhập."
        if "timed out" in lowered or "timeout" in lowered:
            return "Tải hoặc xử lý file nguồn bị timeout."

        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        skip_prefixes = (
            "ffmpeg version",
            "built with",
            "configuration:",
            "libav",
            "input #",
            "metadata:",
            "duration:",
            "stream #",
            "encoder         :",
            "date            :",
            "id3v2_priv.",
            "ffmpeg stats and -progress period set to",
        )
        for line in reversed(lines):
            if line.casefold().startswith(skip_prefixes):
                continue
            return line[:240]
        return normalized.splitlines()[-1][:240]

    def _live_stream_error_log_label(self, error_message: str | None) -> str:
        normalized = self._normalize_live_optional_text(error_message)
        if not normalized:
            return "Lỗi"

        lowered = normalized.casefold()
        if "stream map '0:v:0' matches no streams" in lowered:
            return "Lỗi sai định dạng video"
        if "stream map '1:a:0' matches no streams" in lowered:
            return "Lỗi audio đầu vào"
        if (
            ("could not write header for output file" in lowered or "incorrect codec parameters" in lowered)
            and any(codec in lowered for codec in {"opus", "vorbis", "webm"})
        ):
            return "Lỗi sai định dạng audio"
        if "invalid data found when processing input" in lowered:
            return "Lỗi file nguồn"
        if "permission denied" in lowered or "403 forbidden" in lowered:
            return "Lỗi quyền truy cập"
        if "404 not found" in lowered:
            return "Lỗi không tìm thấy file"
        if "timed out" in lowered or "timeout" in lowered:
            return "Lỗi timeout"
        return "Lỗi xử lý media"

    def _live_stream_error_message(self, stream: LiveStreamRecord, *, error_message: str) -> str:
        visible_stream = self._visible_live_stream_for_notification(stream)
        summary = self._summarize_live_error_message(error_message) or "Không rõ nguyên nhân."
        return "\n".join(
            [
                "[LIVE] Luồng live gặp lỗi",
                *self._live_notification_base_lines(visible_stream),
                f"Lỗi: {summary}",
            ]
        )

    def _live_stream_stopped_message(self, stream: LiveStreamRecord, *, now: datetime) -> str:
        visible_stream = self._visible_live_stream_for_notification(stream)
        return "\n".join(
            [
                "[LIVE] Đã dừng luồng live",
                *self._live_notification_base_lines(visible_stream),
                f"Dừng lúc: {self._format_full_datetime(now)}",
            ]
        )

    def _live_stream_ended_message(self, stream: LiveStreamRecord, *, now: datetime) -> str:
        visible_stream = self._visible_live_stream_for_notification(stream)
        return "\n".join(
            [
                "[LIVE] Luồng live đã kết thúc",
                *self._live_notification_base_lines(visible_stream),
                f"Kết thúc lúc: {self._format_full_datetime(now)}",
            ]
        )

    def _live_backup_activated_message(self, stream: LiveStreamRecord, *, now: datetime) -> str:
        visible_stream = self._visible_live_stream_for_notification(stream)
        backup_label = stream.primary_worker_name or self._resolve_live_worker_display_name(stream.primary_worker_id)
        return "\n".join(
            [
                "[LIVE] Backup đã kích hoạt",
                *self._live_notification_base_lines(visible_stream),
                f"BOT backup đang chạy: {backup_label}",
                f"Kích hoạt lúc: {self._format_full_datetime(now)}",
                "Trạng thái: Luồng backup đang tiếp quản luồng live trên backup ingest.",
            ]
        )

    def _live_stream_disconnected_message(
        self,
        stream: LiveStreamRecord,
        *,
        now: datetime,
        reason: str | None = None,
    ) -> str:
        visible_stream = self._visible_live_stream_for_notification(stream)
        if visible_stream.backup_worker_id:
            status_line = "Trạng thái: BOT chính đã mất kết nối, backup đang được kích hoạt để giữ luồng."
        else:
            status_line = "Trạng thái: Chưa có BOT backup, cần kiểm tra lại BOT chính."
        lines = [
            "[LIVE] Luồng live bị mất kết nối",
            *self._live_notification_base_lines(visible_stream),
            f"Mất kết nối lúc: {self._format_full_datetime(now)}",
            status_line,
        ]
        normalized_reason = self._summarize_live_error_message(reason)
        if normalized_reason:
            lines.append(f"Chi tiết: {normalized_reason}")
        return "\n".join(lines)

    def _job_upload_interrupted_message(
        self,
        job: RenderJobRecord,
        *,
        now: datetime,
        reason: str,
    ) -> str:
        upload_progress = max(int(job.upload_progress or 0), int(job.progress or 0))
        worker_label = str(job.worker_name or job.claimed_by_worker_id or "-").strip() or "-"
        lines = [
            "[UPLOAD] Job upload bị lỗi",
            f"Job: {job.title}",
            f"Kênh: {job.channel_name or job.channel_id}",
            f"BOT: {worker_label}",
            f"Upload đã ghi nhận: {upload_progress}%",
            f"Thời điểm: {self._format_full_datetime(now)}",
            "Trạng thái: Lỗi do bot bị restart mất hoặc mất mạng mất kết nối",
            "Vui lòng xoá Job lỗi và tạo lại Job upload mới",
        ]
        return "\n".join(lines)

    def _job_upload_completed_message(
        self,
        job: RenderJobRecord,
        *,
        now: datetime,
    ) -> str:
        worker_label = str(job.worker_name or job.claimed_by_worker_id or "-").strip() or "-"
        lines = [
            "[UPLOAD] Job upload hoàn thành",
            f"Job: {job.title}",
            f"Kênh: {job.channel_name or job.channel_id}",
            f"BOT: {worker_label}",
            f"Hoàn thành lúc: {self._format_full_datetime(now)}",
        ]
        return "\n".join(lines)

    def _summarize_job_upload_error_message(self, error_message: str | None) -> str | None:
        normalized = self._normalize_live_optional_text(error_message)
        if not normalized:
            return None

        lowered = normalized.casefold()
        video_slot_markers = (
            "asset video_loop",
            "asset intro",
            "asset outro",
            "link video",
            "video_loop",
        )
        if (
            "không có video stream" in lowered
            or "khong co video stream" in lowered
            or "stream map '0:v:0' matches no streams" in lowered
        ) and any(marker in lowered for marker in video_slot_markers):
            if "mp3" in lowered or "audio" in lowered:
                return "Link video đang nhập là link file MP3, vui lòng sửa lại link file thành video."
            return "Link video không có video stream hợp lệ, vui lòng đổi sang file video."
        if (
            "verify it's you" in lowered
            or "verify its you" in lowered
            or "modal verify" in lowered
            or "bot khong the tu xac minh" in lowered
        ):
            return (
                "YouTube/Google yêu cầu Verify it's you. "
                "Vui lòng xoá kênh và thêm lại kênh."
            )
        return None

    def _job_upload_error_message(
        self,
        job: RenderJobRecord,
        *,
        now: datetime,
        error_message: str,
    ) -> str | None:
        summary = self._summarize_job_upload_error_message(error_message)
        if not summary:
            return None
        worker_label = str(job.worker_name or job.claimed_by_worker_id or "-").strip() or "-"
        return "\n".join(
            [
                "[UPLOAD] Job upload gặp lỗi",
                f"Tài khoản: {self._job_username(job)}",
                f"Job: {job.title}",
                f"Kênh: {job.channel_name or job.channel_id}",
                f"BOT: {worker_label}",
                f"Thời điểm: {self._format_full_datetime(now)}",
                f"Lỗi: {summary}",
            ]
        )

    @staticmethod
    def _bot_operation_actor_label(role: str | None, username: str | None) -> str:
        normalized_username = str(username or "").strip() or "system"
        return normalized_username

    def _bot_install_completed_message(self, task: dict[str, Any], *, worker_name: str, worker_id: str, vps_ip: str) -> str:
        actor_label = self._bot_operation_actor_label(task.get("requested_role"), task.get("requested_by"))
        manager_name = str(task.get("manager_name") or "").strip() or "system"
        return (
            "[BOT] Thêm BOT thành công\n"
            f"Người thao tác: {actor_label}\n"
            f"BOT: {worker_name}\n"
            f"BOT ID: {worker_id}\n"
            f"VPS: {vps_ip}\n"
            f"Manager sở hữu: {manager_name}"
        )

    def _bot_workspace_conversion_completed_message(
        self,
        task: dict[str, Any],
        *,
        worker_name: str,
        worker_id: str,
        vps_ip: str,
    ) -> str:
        actor_label = self._bot_operation_actor_label(task.get("requested_role"), task.get("requested_by"))
        manager_name = str(task.get("manager_name") or "").strip() or "system"
        source_worker_id = str(task.get("conversion_source_worker_id") or "").strip() or "--"
        source_label = str(task.get("conversion_source_label") or "").strip() or "BOT cũ"
        target_label = str(task.get("conversion_target_label") or "").strip() or "BOT mới"
        return (
            "[BOT] Cập nhật BOT thành công\n"
            f"Người thao tác: {actor_label}\n"
            f"BOT: {worker_name}\n"
            f"VPS: {vps_ip}\n"
            f"Chuyển loại: {source_label} -> {target_label}\n"
            f"BOT ID cũ: {source_worker_id}\n"
            f"BOT ID mới: {worker_id}\n"
            f"Manager sở hữu: {manager_name}"
        )

    def _bot_install_result_message(self, task: dict[str, Any], *, worker_name: str, worker_id: str, vps_ip: str) -> str:
        if str(task.get("operation_reason") or "").strip() == "workspace_conversion":
            return self._bot_workspace_conversion_completed_message(
                task,
                worker_name=worker_name,
                worker_id=worker_id,
                vps_ip=vps_ip,
            )
        return self._bot_install_completed_message(
            task,
            worker_name=worker_name,
            worker_id=worker_id,
            vps_ip=vps_ip,
        )

    def _bot_decommission_completed_message(self, task: dict[str, Any], *, worker_name: str, worker_id: str, vps_ip: str) -> str:
        actor_label = self._bot_operation_actor_label(task.get("requested_role"), task.get("requested_by"))
        manager_name = str(task.get("manager_name") or "").strip() or "system"
        return (
            "[BOT] Xóa BOT thành công\n"
            f"Người thao tác: {actor_label}\n"
            f"BOT: {worker_name}\n"
            f"BOT ID: {worker_id}\n"
            f"VPS: {vps_ip}\n"
            f"Manager trước khi xóa: {manager_name}"
        )

    def _user_telegram_chat_id(self, user_id: str) -> str:
        meta = self._user_meta_record(user_id)
        try:
            return self._normalize_telegram_chat_id(meta.get("telegram"))
        except ValueError:
            return ""

    def _user_telegram_live_chat_id(self, user_id: str) -> str:
        meta = self._user_meta_record(user_id)
        try:
            return self._normalize_telegram_chat_id(meta.get("telegram_live"))
        except ValueError:
            return ""

    def _live_stream_recipient_chat_ids(self, stream: LiveStreamRecord) -> list[str]:
        visible_stream = self._visible_live_stream_for_notification(stream)
        chat_id = self._user_telegram_live_chat_id(visible_stream.owner_user_id)
        return [chat_id] if chat_id else []

    def _job_upload_interruption_recipient_chat_ids(self, job: RenderJobRecord) -> list[str]:
        user_id = self._job_user_id(job)
        if not user_id:
            return []
        chat_id = self._user_telegram_live_chat_id(user_id)
        return [chat_id] if chat_id else []

    def _live_stream_fallback_alert_chat_ids(self, stream: LiveStreamRecord) -> list[str]:
        visible_stream = self._visible_live_stream_for_notification(stream)
        chat_id = self._user_telegram_chat_id(visible_stream.owner_user_id)
        return [chat_id] if chat_id else []

    def _telegram_recipient_chat_ids_for_users(self, users: list[UserSummary]) -> list[str]:
        chat_ids: list[str] = []
        seen_ids: set[str] = set()
        for user in users:
            chat_id = self._user_telegram_chat_id(user.id)
            if not chat_id or chat_id in seen_ids:
                continue
            chat_ids.append(chat_id)
            seen_ids.add(chat_id)
        return chat_ids

    def _all_admin_telegram_recipient_chat_ids(self) -> list[str]:
        admin_users = [user for user in self.users if user.role == "admin"]
        chat_ids = self._telegram_recipient_chat_ids_for_users(admin_users)
        if chat_ids:
            return chat_ids
        try:
            fallback_chat_id = self._normalize_telegram_chat_id(self._telegram_alert_chat_id())
        except ValueError:
            fallback_chat_id = ""
        return [fallback_chat_id] if fallback_chat_id else []

    def _manager_telegram_recipient_chat_ids(self, manager_usernames: set[str]) -> list[str]:
        if not manager_usernames:
            return []
        manager_users = [
            user
            for user in self.users
            if user.role == "manager" and user.username in manager_usernames
        ]
        return self._telegram_recipient_chat_ids_for_users(manager_users)

    def _manager_telegram_recipient_chat_ids_by_ids(self, manager_ids: set[str]) -> list[str]:
        normalized_ids = {str(manager_id or "").strip() for manager_id in manager_ids}
        normalized_ids.discard("")
        if not normalized_ids:
            return []
        manager_users = [
            user
            for user in self.users
            if user.role == "manager" and user.id in normalized_ids
        ]
        return self._telegram_recipient_chat_ids_for_users(manager_users)

    def _bot_owner_manager_telegram_recipient_chat_ids(self, task: dict[str, Any]) -> list[str]:
        manager_usernames: set[str] = set()
        manager_name = str(task.get("manager_name") or "").strip()
        if manager_name and manager_name != "system":
            manager_usernames.add(manager_name)
        manager_id = str(task.get("manager_id") or "").strip()
        if manager_id:
            manager_user = next(
                (user for user in self.users if user.id == manager_id and user.role == "manager"),
                None,
            )
            if manager_user and manager_user.username:
                manager_usernames.add(manager_user.username)
        return self._manager_telegram_recipient_chat_ids(manager_usernames)

    def _bot_operation_recipient_chat_ids(self, task: dict[str, Any]) -> list[str]:
        if not self._bot_operation_telegram_enabled():
            return []
        recipient_chat_ids = self._all_admin_telegram_recipient_chat_ids()
        seen_ids = set(recipient_chat_ids)
        for chat_id in self._bot_owner_manager_telegram_recipient_chat_ids(task):
            if chat_id in seen_ids:
                continue
            recipient_chat_ids.append(chat_id)
            seen_ids.add(chat_id)
        return recipient_chat_ids

    def _worker_alert_recipient_chat_ids(self, worker: WorkerRecord) -> list[str]:
        recipient_chat_ids = self._all_admin_telegram_recipient_chat_ids()
        seen_ids = set(recipient_chat_ids)
        manager_name = str(worker.manager_name or "").strip()
        if manager_name and manager_name != "system":
            for chat_id in self._manager_telegram_recipient_chat_ids({manager_name}):
                if chat_id in seen_ids:
                    continue
                recipient_chat_ids.append(chat_id)
                seen_ids.add(chat_id)
        return recipient_chat_ids

    def _notify_telegram_chat_ids(self, chat_ids: list[str], message: str) -> bool:
        recipient_chat_ids = [chat_id for chat_id in chat_ids if str(chat_id or "").strip()]
        if not recipient_chat_ids:
            return False
        delivered = False
        message_chunks = self._telegram_message_chunks(message)
        for chat_id in recipient_chat_ids:
            for message_chunk in message_chunks:
                if self._send_telegram_alert(message_chunk, chat_id=chat_id):
                    delivered = True
        return delivered

    @classmethod
    def _telegram_message_chunks(cls, message: str) -> list[str]:
        normalized_message = str(message or "")
        if len(normalized_message) <= cls.TELEGRAM_MESSAGE_CHUNK_LIMIT:
            return [normalized_message]
        chunks: list[str] = []
        current = ""
        for line in normalized_message.splitlines():
            candidate = line if not current else f"{current}\n{line}"
            if len(candidate) <= cls.TELEGRAM_MESSAGE_CHUNK_LIMIT:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            while len(line) > cls.TELEGRAM_MESSAGE_CHUNK_LIMIT:
                chunks.append(line[: cls.TELEGRAM_MESSAGE_CHUNK_LIMIT])
                line = line[cls.TELEGRAM_MESSAGE_CHUNK_LIMIT :]
            current = line
        if current or not chunks:
            chunks.append(current)
        return chunks

    def _notify_live_telegram_chat_ids(self, chat_ids: list[str], message: str) -> bool:
        if not self._telegram_live_notifications_enabled():
            return False
        recipient_chat_ids = [chat_id for chat_id in chat_ids if str(chat_id or "").strip()]
        if not recipient_chat_ids:
            return False
        delivered = False
        for chat_id in recipient_chat_ids:
            if self._send_telegram_live_alert(message, chat_id=chat_id):
                delivered = True
        return delivered

    @staticmethod
    def _worker_aliases(worker: WorkerRecord) -> set[str]:
        return {
            alias
            for alias in {str(worker.id or "").strip(), str(worker.name or "").strip()}
            if alias
        }

    def _safe_parse_datetime(self, value: Any) -> datetime | None:
        try:
            return self._parse_datetime(value)
        except (TypeError, ValueError):
            return None

    def _assignment_created_at(self, link: dict[str, Any], worker: WorkerRecord) -> datetime | None:
        return self._safe_parse_datetime(link.get("created_at")) or worker.created_at or worker.last_seen_at

    def _upload_assignment_last_job_created_at(self, *, user_id: str, worker: WorkerRecord) -> datetime | None:
        worker_aliases = self._worker_aliases(worker)
        linked_channel_ids = {
            str(link.get("channel_id") or "").strip()
            for link in self.channel_user_links
            if str(link.get("user_id") or "").strip() == user_id
        }
        worker_channel_ids = {
            channel.id
            for channel in self.channels
            if channel.id in linked_channel_ids and str(channel.worker_id or "").strip() in worker_aliases
        }
        created_values = [
            job.created_at
            for job in self.jobs
            if job.channel_id in worker_channel_ids and str(job.worker_name or "").strip() in worker_aliases
        ]
        return max(created_values, default=None)

    def _live_assignment_last_stream_created_at(
        self,
        *,
        user_id: str,
        worker_id: str,
        live_role: str,
    ) -> datetime | None:
        normalized_worker_id = str(worker_id or "").strip()
        normalized_role = self._normalize_live_assignment_role(live_role)
        created_values: list[datetime] = []
        for stream in self.live_streams:
            if bool(stream.is_runtime_clone):
                continue
            if str(stream.owner_user_id or "").strip() != user_id:
                continue
            if normalized_role == "backup":
                if str(stream.backup_worker_id or "").strip() != normalized_worker_id:
                    continue
            elif str(stream.primary_worker_id or "").strip() != normalized_worker_id:
                continue
            created_values.append(stream.created_at)
        return max(created_values, default=None)

    def _inactive_bot_manager_scope(self, worker: WorkerRecord, user: UserSummary) -> tuple[str | None, str]:
        manager_name = str(worker.manager_name or "").strip()
        manager_id = str(worker.manager_id or "").strip() or None
        if not manager_id and manager_name:
            manager_id = self._manager_username_to_id_map().get(manager_name)
        if not manager_name and manager_id:
            manager_name = self._manager_username_from_id(manager_id) or ""
        if not manager_name and user.role == "user":
            manager_id = manager_id or self._resolved_user_manager_id(user)
            manager_name = self._manager_username_from_id(manager_id) or str(user.manager_name or "").strip()
        return manager_id, manager_name or "system"

    def _inactive_bot_record(
        self,
        *,
        user: UserSummary,
        worker: WorkerRecord,
        workspace: str,
        bot_type: str,
        bot_type_label: str,
        assignment_created_at: datetime | None,
        last_job_created_at: datetime | None,
        now: datetime,
    ) -> dict[str, Any]:
        manager_id, manager_name = self._inactive_bot_manager_scope(worker, user)
        activity_values = [value for value in (last_job_created_at, assignment_created_at) if value is not None]
        activity_at = max(activity_values, default=None)
        inactive_days = max(0, (now - activity_at).days) if activity_at else 0
        return {
            "workspace": workspace,
            "bot_type": bot_type,
            "bot_type_label": bot_type_label,
            "worker_id": worker.id,
            "worker_name": self._resolve_live_worker_display_name(worker.id)
            if workspace == "live"
            else self._resolve_worker_display_name(worker.id),
            "user_id": user.id,
            "username": user.username,
            "user_display_name": user.display_name or user.username,
            "manager_id": manager_id,
            "manager_name": manager_name,
            "assignment_created_at": assignment_created_at,
            "last_job_created_at": last_job_created_at,
            "last_activity_at": activity_at,
            "inactive_days": inactive_days,
        }

    def get_inactive_bot_allocations(
        self,
        *,
        now: datetime | None = None,
        days: int | None = None,
    ) -> list[dict[str, Any]]:
        normalized_now = self._normalize_datetime(now) if now is not None else self._now(trim=False)
        threshold_days = max(1, int(days if days is not None else self._inactive_bot_alert_days()))
        cutoff = normalized_now - timedelta(days=threshold_days)
        inactive_records: list[dict[str, Any]] = []

        for link in self.user_worker_links:
            user_id = str(link.get("user_id") or "").strip()
            worker_id = str(link.get("worker_id") or "").strip()
            if not user_id or not worker_id:
                continue
            try:
                user = self._find_user(user_id)
                worker = self._find_worker(worker_id)
            except KeyError:
                continue
            assignment_created_at = self._assignment_created_at(link, worker)
            last_job_created_at = self._upload_assignment_last_job_created_at(user_id=user.id, worker=worker)
            last_activity_at = max(
                [value for value in (last_job_created_at, assignment_created_at) if value is not None],
                default=None,
            )
            if last_activity_at is not None and last_activity_at >= cutoff:
                continue
            inactive_records.append(
                self._inactive_bot_record(
                    user=user,
                    worker=worker,
                    workspace="upload",
                    bot_type="upload",
                    bot_type_label="BOT upload",
                    assignment_created_at=assignment_created_at,
                    last_job_created_at=last_job_created_at,
                    now=normalized_now,
                )
            )

        for link in self.live_user_worker_links:
            user_id = str(link.get("user_id") or "").strip()
            worker_id = str(link.get("worker_id") or "").strip()
            if not user_id or not worker_id:
                continue
            try:
                user = self._find_user(user_id)
                worker = self._find_live_worker(worker_id)
            except KeyError:
                continue
            live_role = self._normalize_live_assignment_role(link.get("live_role"), fallback_note=link.get("note"))
            assignment_created_at = self._assignment_created_at(link, worker)
            last_job_created_at = self._live_assignment_last_stream_created_at(
                user_id=user.id,
                worker_id=worker.id,
                live_role=live_role,
            )
            last_activity_at = max(
                [value for value in (last_job_created_at, assignment_created_at) if value is not None],
                default=None,
            )
            if last_activity_at is not None and last_activity_at >= cutoff:
                continue
            inactive_records.append(
                self._inactive_bot_record(
                    user=user,
                    worker=worker,
                    workspace="live",
                    bot_type="live_backup" if live_role == "backup" else "live_primary",
                    bot_type_label="BOT backup" if live_role == "backup" else "BOT live chính",
                    assignment_created_at=assignment_created_at,
                    last_job_created_at=last_job_created_at,
                    now=normalized_now,
                )
            )

        return sorted(
            inactive_records,
            key=lambda item: (
                str(item.get("manager_name") or ""),
                str(item.get("username") or ""),
                str(item.get("workspace") or ""),
                str(item.get("bot_type") or ""),
                str(item.get("worker_name") or ""),
            ),
        )

    def _inactive_bot_alert_due(self, *, now: datetime) -> bool:
        if not self._inactive_bot_alert_enabled():
            return False
        today_key = now.date().isoformat()
        sent_on = str(self.inactive_bot_alert_last_sent_on or "").strip()
        if sent_on == today_key:
            return False
        attempted_on = str(self.inactive_bot_alert_last_attempted_on or "").strip()
        if attempted_on == today_key:
            return False
        scheduled_at = now.replace(
            hour=self._inactive_bot_alert_hour(),
            minute=self._inactive_bot_alert_minute(),
            second=0,
            microsecond=0,
        )
        return now >= scheduled_at

    def _inactive_bot_alert_message(
        self,
        records: list[dict[str, Any]],
        *,
        now: datetime,
        days: int,
        scope_label: str,
    ) -> str:
        lines = [
            f"[CẢNH BÁO] BOT không hoạt động quá {days} ngày",
            f"Tổng BOT: {len(records)}",
            f"Phạm vi: {scope_label}",
            f"Thời điểm kiểm tra: {self._format_full_datetime(now)}",
            "",
            "Danh sách BOT:",
        ]
        max_rows = 20
        for item in records[:max_rows]:
            last_job_label = (
                self._format_full_datetime(item.get("last_job_created_at"))
                if item.get("last_job_created_at")
                else "chưa có job"
            )
            assigned_label = self._format_full_datetime(item.get("assignment_created_at"))
            lines.extend(
                [
                    f"- {item.get('bot_type_label')}: {item.get('worker_name')} ({item.get('worker_id')})",
                    f"  User: {item.get('username')} | Manager: {item.get('manager_name')}",
                    f"  Job gần nhất: {last_job_label} | Ngày cấp: {assigned_label}",
                    f"  Không hoạt động: {item.get('inactive_days')} ngày",
                ]
            )
        remaining = len(records) - max_rows
        if remaining > 0:
            lines.append(f"... còn {remaining} BOT khác.")
        return "\n".join(lines)

    def _reconcile_inactive_bot_daily_alert(self, *, now: datetime) -> bool:
        normalized_now = self._normalize_datetime(now) or self._now(trim=False)
        today_key = normalized_now.date().isoformat()

        with self._worker_state_lock:
            if not self._inactive_bot_alert_due(now=normalized_now):
                return False

            threshold_days = self._inactive_bot_alert_days()
            inactive_records = self.get_inactive_bot_allocations(now=normalized_now, days=threshold_days)
            self.inactive_bot_alert_last_attempted_on = today_key
            if not inactive_records:
                self.inactive_bot_alert_last_sent_on = today_key
                self._save_state()
                return True

            notification_batches: list[tuple[list[str], str]] = [
                (
                    self._all_admin_telegram_recipient_chat_ids(),
                    self._inactive_bot_alert_message(
                        inactive_records,
                        now=normalized_now,
                        days=threshold_days,
                        scope_label="toàn bộ BOT",
                    ),
                )
            ]

            records_by_manager_id: dict[str, list[dict[str, Any]]] = {}
            records_by_manager_name: dict[str, list[dict[str, Any]]] = {}
            for item in inactive_records:
                manager_id = str(item.get("manager_id") or "").strip()
                manager_name = str(item.get("manager_name") or "").strip()
                if manager_id:
                    records_by_manager_id.setdefault(manager_id, []).append(item)
                    continue
                if manager_name and manager_name != "system":
                    records_by_manager_name.setdefault(manager_name, []).append(item)

            for manager_id in sorted(records_by_manager_id):
                manager_records = records_by_manager_id[manager_id]
                manager_name = str(manager_records[0].get("manager_name") or manager_id).strip()
                notification_batches.append(
                    (
                        self._manager_telegram_recipient_chat_ids_by_ids({manager_id}),
                        self._inactive_bot_alert_message(
                            manager_records,
                            now=normalized_now,
                            days=threshold_days,
                            scope_label=f"manager {manager_name}",
                        ),
                    )
                )

            for manager_name in sorted(records_by_manager_name):
                manager_records = records_by_manager_name[manager_name]
                notification_batches.append(
                    (
                        self._manager_telegram_recipient_chat_ids({manager_name}),
                        self._inactive_bot_alert_message(
                            manager_records,
                            now=normalized_now,
                            days=threshold_days,
                            scope_label=f"manager {manager_name}",
                        ),
                    )
                )

            self._save_state()

        delivered = False
        for chat_ids, message in notification_batches:
            if self._notify_telegram_chat_ids(chat_ids, message):
                delivered = True

        if delivered:
            with self._worker_state_lock:
                self.inactive_bot_alert_last_sent_on = today_key
                self._save_state()
            return True
        return False

    def _worker_offline_message(self, worker: WorkerRecord, *, now: datetime) -> str:
        worker_name = worker.name or worker.id
        manager_name = worker.manager_name or "không rõ"
        return (
            "[CẢNH BÁO] BOT mất kết nối quá 3 phút\n"
            f"BOT: {worker_name}\n"
            f"BOT ID: {worker.id}\n"
            f"Manager: {manager_name}\n"
            "Workspace: upload\n"
            f"Source: {self._worker_registration_source_label(worker, live=False)}\n"
            "Trạng thái: BOT chưa tự kết nối lại với control-plane"
        )

    def _live_worker_offline_message(self, worker: WorkerRecord, *, now: datetime) -> str:
        worker_name = worker.name or worker.id
        manager_name = worker.manager_name or "không rõ"
        return (
            "[CẢNH BÁO] BOT live mất kết nối quá 3 phút\n"
            f"BOT: {worker_name}\n"
            f"BOT ID: {worker.id}\n"
            f"Manager: {manager_name}\n"
            "Workspace: live\n"
            f"Source: {self._worker_registration_source_label(worker, live=True)}\n"
            "Trạng thái: BOT live chưa tự kết nối lại với control-plane"
        )

    def _worker_reconnected_message(
        self,
        worker: WorkerRecord,
        *,
        now: datetime,
        offline_since_at: datetime | None = None,
    ) -> str:
        worker_name = worker.name or worker.id
        manager_name = worker.manager_name or "không rõ"
        lines = [
            "[THÔNG BÁO] BOT đã kết nối lại",
            f"BOT: {worker_name}",
            f"BOT ID: {worker.id}",
            f"Manager: {manager_name}",
            "Workspace: upload",
            f"Source: {self._worker_registration_source_label(worker, live=False)}",
        ]
        if offline_since_at is not None:
            lines.append(f"Mất kết nối từ: {self._format_full_datetime(offline_since_at)}")
        lines.append(f"Kết nối lại lúc: {self._format_full_datetime(now)}")
        lines.append("Trạng thái: BOT đã kết nối lại với control-plane")
        return "\n".join(lines)

    def _live_worker_reconnected_message(
        self,
        worker: WorkerRecord,
        *,
        now: datetime,
        offline_since_at: datetime | None = None,
    ) -> str:
        worker_name = worker.name or worker.id
        manager_name = worker.manager_name or "không rõ"
        lines = [
            "[THÔNG BÁO] BOT live đã kết nối lại",
            f"BOT: {worker_name}",
            f"BOT ID: {worker.id}",
            f"Manager: {manager_name}",
            "Workspace: live",
            f"Source: {self._worker_registration_source_label(worker, live=True)}",
        ]
        if offline_since_at is not None:
            lines.append(f"Mất kết nối từ: {self._format_full_datetime(offline_since_at)}")
        lines.append(f"Kết nối lại lúc: {self._format_full_datetime(now)}")
        lines.append("Trạng thái: BOT live đã kết nối lại với control-plane")
        return "\n".join(lines)

    def _worker_reconnect_notification(
        self,
        worker: WorkerRecord,
        *,
        now: datetime,
        live: bool,
    ) -> tuple[list[str], str] | None:
        if worker.offline_alert_sent_at is None:
            return None
        offline_since_at = worker.offline_since_at or worker.last_seen_at
        message = (
            self._live_worker_reconnected_message(worker, now=now, offline_since_at=offline_since_at)
            if live
            else self._worker_reconnected_message(worker, now=now, offline_since_at=offline_since_at)
        )
        return self._worker_alert_recipient_chat_ids(worker), message

    def _reconcile_worker_connectivity(self, *, now: datetime) -> bool:
        changed = False
        alert_seconds = self._worker_offline_alert_seconds()
        stale_cutoff = timedelta(seconds=alert_seconds)
        for worker in self.workers:
            last_seen = worker.last_seen_at
            running_threads = self._count_worker_running_jobs(worker)
            is_online = last_seen is not None and (now - last_seen) < stale_cutoff
            if is_online:
                desired_status = "busy" if running_threads > 0 else "online"
                if worker.status != desired_status:
                    worker.status = desired_status
                    changed = True
                if worker.offline_since_at is not None:
                    worker.offline_since_at = None
                    changed = True
                if worker.offline_alert_sent_at is not None:
                    worker.offline_alert_sent_at = None
                    changed = True
                continue

            if worker.status != "offline":
                worker.status = "offline"
                changed = True
            offline_since_at = last_seen or now
            if worker.offline_since_at != offline_since_at:
                worker.offline_since_at = offline_since_at
                changed = True
            if worker.offline_alert_sent_at is not None:
                continue
            message = self._worker_offline_message(worker, now=now)
            if self._notify_telegram_chat_ids(self._worker_alert_recipient_chat_ids(worker), message):
                worker.offline_alert_sent_at = now
                changed = True
        if changed:
            self._save_state()
        return changed

    def _reconcile_live_worker_connectivity(self, *, now: datetime) -> bool:
        changed = False
        alert_seconds = self._worker_offline_alert_seconds()
        stale_cutoff = timedelta(seconds=alert_seconds)
        for worker in self.live_workers:
            last_seen = worker.last_seen_at
            running_threads = self._count_live_worker_running_streams(worker)
            is_online = last_seen is not None and (now - last_seen) < stale_cutoff
            if is_online:
                desired_status = "busy" if running_threads > 0 else "online"
                if worker.status != desired_status:
                    worker.status = desired_status
                    changed = True
                if worker.offline_since_at is not None:
                    worker.offline_since_at = None
                    changed = True
                if worker.offline_alert_sent_at is not None:
                    worker.offline_alert_sent_at = None
                    changed = True
                continue

            if worker.status != "offline":
                worker.status = "offline"
                changed = True
            offline_since_at = last_seen or now
            if worker.offline_since_at != offline_since_at:
                worker.offline_since_at = offline_since_at
                changed = True
            if worker.offline_alert_sent_at is not None:
                continue
            message = self._live_worker_offline_message(worker, now=now)
            if self._notify_telegram_chat_ids(self._worker_alert_recipient_chat_ids(worker), message):
                worker.offline_alert_sent_at = now
                changed = True
        if changed:
            self._save_state()
        return changed

    def _normalize_user_worker_assignments(self) -> bool:
        changed = False
        normalized_links: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()

        def sort_key(mapping: dict[str, Any]) -> tuple[int, str, str]:
            try:
                mapping_id = int(mapping.get("id") or 0)
            except (TypeError, ValueError):
                mapping_id = 0
            return (mapping_id, str(mapping.get("user_id") or ""), str(mapping.get("worker_id") or ""))

        for raw_mapping in sorted(self.user_worker_links, key=sort_key):
            user_id = str(raw_mapping.get("user_id") or "").strip()
            worker_id = str(raw_mapping.get("worker_id") or "").strip()
            if not user_id or not worker_id:
                changed = True
                continue
            pair = (user_id, worker_id)
            if pair in seen_pairs:
                changed = True
                continue

            mapping = dict(raw_mapping)
            mapping["threads"] = self._fixed_assignment_threads()
            mapping["note"] = str(mapping.get("note") or "").strip() or "VPS được cấp"
            normalized_links.append(mapping)
            seen_pairs.add(pair)
            if mapping != raw_mapping:
                changed = True

        if normalized_links != self.user_worker_links:
            self.user_worker_links = normalized_links
            changed = True

        for job in self.jobs:
            channel = next((item for item in self.channels if item.id == job.channel_id), None)
            if channel is None:
                continue
            if job.worker_name != channel.worker_id:
                job.worker_name = channel.worker_id
                changed = True

        return changed

    def _normalize_live_worker_names(self) -> bool:
        changed = False
        for worker in self.live_workers:
            display_name = self._default_worker_display_name(worker.id, worker.name)
            if worker.name != display_name:
                worker.name = display_name
                changed = True
        return changed

    @staticmethod
    def _normalize_live_assignment_role(
        value: str | None,
        *,
        fallback_note: str | None = None,
    ) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"primary", "backup"}:
            return normalized
        normalized_note = str(fallback_note or "").strip().lower()
        if "backup" in normalized_note:
            return "backup"
        return "primary"

    @staticmethod
    def _live_assignment_role_label(role: str | None) -> str:
        return "BOT backup" if str(role or "").strip().lower() == "backup" else "BOT live chính"

    def _live_assignment_note(self, role: str | None) -> str:
        return "BOT backup được cấp" if self._normalize_live_assignment_role(role) == "backup" else "BOT live chính được cấp"

    def _assert_live_assignment_runtime_guard(
        self,
        *,
        worker_id: str,
        user_id: str,
        current_role: str | None,
        next_role: str | None,
        proposed_threads: int | None,
        removing_assignment: bool = False,
    ) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        normalized_current_role = self._normalize_live_assignment_role(current_role)
        normalized_next_role = (
            self._normalize_live_assignment_role(next_role)
            if str(next_role or "").strip()
            else normalized_current_role
        )
        now = self._now(trim=False)
        active_current_role_count = self._live_runtime_user_active_stream_count(
            owner_user_id=normalized_user_id,
            worker_id=normalized_worker_id,
            role=normalized_current_role,
            now=now,
        )
        worker_name = self._resolve_live_worker_display_name(normalized_worker_id)
        try:
            user_name = self._find_user(normalized_user_id).username
        except KeyError:
            user_name = normalized_user_id

        if removing_assignment and active_current_role_count > 0:
            raise ValueError(
                f"Không thể gỡ user {user_name} khỏi {self._live_assignment_role_label(normalized_current_role)} "
                f"{worker_name} vì hiện còn {active_current_role_count} luồng active trên BOT này."
            )

        if normalized_current_role != normalized_next_role and active_current_role_count > 0:
            raise ValueError(
                f"Không thể đổi {self._live_assignment_role_label(normalized_current_role)} của user {user_name} "
                f"sang {self._live_assignment_role_label(normalized_next_role)} trên BOT {worker_name} vì hiện còn "
                f"{active_current_role_count} luồng active theo role cũ."
            )

        if proposed_threads is not None and proposed_threads < active_current_role_count:
            raise ValueError(
                f"Không thể hạ quota của user {user_name} trên BOT {worker_name} xuống {proposed_threads} "
                f"vì hiện đang có {active_current_role_count} luồng active."
            )

    def _normalize_live_user_worker_assignments(self) -> bool:
        changed = False
        normalized_links: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, str]] = set()

        def sort_key(mapping: dict[str, Any]) -> tuple[int, str, str]:
            try:
                mapping_id = int(mapping.get("id") or 0)
            except (TypeError, ValueError):
                mapping_id = 0
            return (mapping_id, str(mapping.get("user_id") or ""), str(mapping.get("worker_id") or ""))

        for raw_mapping in sorted(self.live_user_worker_links, key=sort_key):
            user_id = str(raw_mapping.get("user_id") or "").strip()
            worker_id = str(raw_mapping.get("worker_id") or "").strip()
            if not user_id or not worker_id:
                changed = True
                continue
            pair = (user_id, worker_id)
            if pair in seen_pairs:
                changed = True
                continue

            mapping = dict(raw_mapping)
            allocated_threads = self._normalize_live_worker_threads(
                mapping.get("allocated_threads") if mapping.get("allocated_threads") is not None else mapping.get("threads")
            )
            mapping["allocated_threads"] = allocated_threads
            mapping["threads"] = allocated_threads
            mapping["live_role"] = self._normalize_live_assignment_role(
                mapping.get("live_role"),
                fallback_note=mapping.get("note"),
            )
            mapping["note"] = str(mapping.get("note") or "").strip() or self._live_assignment_note(mapping["live_role"])
            normalized_links.append(mapping)
            seen_pairs.add(pair)
            if mapping != raw_mapping:
                changed = True

        if normalized_links != self.live_user_worker_links:
            self.live_user_worker_links = normalized_links
            changed = True
        return changed

    def _normalize_worker_created_at(self) -> bool:
        changed = False
        fallback_now = self._now(trim=False)
        for worker in self.workers:
            if worker.created_at is not None:
                continue
            worker.created_at = worker.last_seen_at or fallback_now
            changed = True
        return changed

    def _normalize_live_worker_created_at(self) -> bool:
        changed = False
        fallback_now = self._now(trim=False)
        for worker in self.live_workers:
            if worker.created_at is not None:
                continue
            worker.created_at = worker.last_seen_at or fallback_now
            changed = True
        return changed

    def _normalize_visible_admin_relationships(self) -> bool:
        changed = False
        valid_user_ids = {user.id for user in self.users if user.role in {"user", "manager", "admin"}}
        valid_worker_ids = {worker.id for worker in self.workers}
        valid_channel_ids = {channel.id for channel in self.channels}

        normalized_worker_links: list[dict[str, Any]] = []
        seen_worker_link_pairs: set[tuple[str, str]] = set()
        for raw_link in self.user_worker_links:
            user_id = str(raw_link.get("user_id") or "").strip()
            worker_id = str(raw_link.get("worker_id") or "").strip()
            if user_id not in valid_user_ids or worker_id not in valid_worker_ids:
                changed = True
                continue
            pair = (user_id, worker_id)
            if pair in seen_worker_link_pairs:
                changed = True
                continue
            normalized_worker_links.append(raw_link)
            seen_worker_link_pairs.add(pair)
        if normalized_worker_links != self.user_worker_links:
            self.user_worker_links = normalized_worker_links
            changed = True

        normalized_channel_links: list[dict[str, Any]] = []
        seen_channel_link_pairs: set[tuple[str, str]] = set()
        for raw_link in self.channel_user_links:
            channel_id = str(raw_link.get("channel_id") or "").strip()
            user_id = str(raw_link.get("user_id") or "").strip()
            if channel_id not in valid_channel_ids or user_id not in valid_user_ids:
                changed = True
                continue
            pair = (channel_id, user_id)
            if pair in seen_channel_link_pairs:
                changed = True
                continue
            normalized_channel_links.append(raw_link)
            seen_channel_link_pairs.add(pair)
        if normalized_channel_links != self.channel_user_links:
            self.channel_user_links = normalized_channel_links
            changed = True

        worker_by_id = {worker.id: worker for worker in self.workers}
        channel_by_id = {channel.id: channel for channel in self.channels}

        for worker in self.workers:
            assigned_user = self._assigned_user_for_worker(worker.id)
            if assigned_user is None:
                continue
            manager_id, manager_name = self._resolve_owner_scope_from_assigned_users([assigned_user])
            if manager_id and worker.manager_id != manager_id:
                worker.manager_id = manager_id
                changed = True
            if manager_name and worker.manager_name != manager_name:
                worker.manager_name = manager_name
                changed = True
            if not str(worker.group or "").strip():
                worker.group = self._default_bot_group_label()
                changed = True

        for channel in self.channels:
            linked_user_ids = [
                str(link.get("user_id") or "").strip()
                for link in self.channel_user_links
                if str(link.get("channel_id") or "").strip() == channel.id
            ]
            if len(linked_user_ids) != 1:
                continue
            try:
                linked_user = self._find_user(linked_user_ids[0])
            except KeyError:
                continue
            assigned_workers = self._assigned_workers_for_user(linked_user)
            assigned_worker_ids = {worker.id for worker in assigned_workers}
            assigned_worker = next((worker for worker in assigned_workers if worker.id == channel.worker_id), None)
            if assigned_worker is None and len(assigned_workers) == 1:
                assigned_worker = assigned_workers[0]
                if channel.worker_id != assigned_worker.id:
                    channel.worker_id = assigned_worker.id
                    changed = True
            if assigned_worker and channel.worker_name != assigned_worker.id:
                channel.worker_name = assigned_worker.id
                changed = True
            elif channel.worker_name != channel.worker_id and channel.worker_id in assigned_worker_ids:
                channel.worker_name = channel.worker_id
                changed = True
            resolved_manager_name = linked_user.manager_name or (assigned_worker.manager_name if assigned_worker else "")
            if resolved_manager_name and channel.manager_name != resolved_manager_name:
                channel.manager_name = resolved_manager_name
                changed = True

        for job in self.jobs:
            channel = channel_by_id.get(job.channel_id)
            if channel is None:
                continue
            if job.worker_name != channel.worker_id:
                job.worker_name = channel.worker_id
                changed = True
            resolved_manager_name = self._resolve_channel_manager_name(channel)
            if resolved_manager_name and job.manager_name != resolved_manager_name:
                job.manager_name = resolved_manager_name
                changed = True

        return changed

    def _normalize_visible_live_relationships(self) -> bool:
        changed = False
        valid_user_ids = {user.id for user in self.users if user.role in {"user", "manager", "admin"}}
        valid_worker_ids = {worker.id for worker in self.live_workers}

        normalized_worker_links: list[dict[str, Any]] = []
        seen_worker_link_pairs: set[tuple[str, str]] = set()
        for raw_link in self.live_user_worker_links:
            user_id = str(raw_link.get("user_id") or "").strip()
            worker_id = str(raw_link.get("worker_id") or "").strip()
            if user_id not in valid_user_ids or worker_id not in valid_worker_ids:
                changed = True
                continue
            pair = (user_id, worker_id)
            if pair in seen_worker_link_pairs:
                changed = True
                continue
            normalized_worker_links.append(raw_link)
            seen_worker_link_pairs.add(pair)
        if normalized_worker_links != self.live_user_worker_links:
            self.live_user_worker_links = normalized_worker_links
            changed = True

        for worker in self.live_workers:
            assigned_user = self._assigned_live_user_for_worker(worker.id, role="primary") or self._assigned_live_user_for_worker(worker.id)
            if assigned_user is None:
                continue
            manager_id, manager_name = self._resolve_owner_scope_from_assigned_users([assigned_user])
            if manager_id and worker.manager_id != manager_id:
                worker.manager_id = manager_id
                changed = True
            if manager_name and worker.manager_name != manager_name:
                worker.manager_name = manager_name
                changed = True
            if not str(worker.group or "").strip():
                worker.group = self._default_bot_group_label()
                changed = True
        return changed

    def _normalize_live_stream_assignments(self) -> bool:
        changed = False
        normalized_streams: list[LiveStreamRecord] = []
        seen_ids: set[str] = set()
        current_stream_map = {
            str(item.id or "").strip(): item
            for item in self.live_streams
            if str(item.id or "").strip()
        }

        for stream in self.live_streams:
            stream_changed = False
            if not str(stream.id or "").strip() or stream.id in seen_ids:
                stream.id = f"live-{uuid4().hex[:10]}"
                stream_changed = True
            seen_ids.add(stream.id)

            stream.runtime_role = "backup" if str(stream.runtime_role or "").strip().lower() == "backup" else "primary"
            stream.is_runtime_clone = bool(stream.is_runtime_clone)
            is_runtime_backup_clone = stream.is_runtime_clone and stream.runtime_role == "backup"
            parent_stream: LiveStreamRecord | None = None
            if is_runtime_backup_clone:
                parent_stream_id = str(stream.parent_stream_id or "").strip()
                parent_stream = current_stream_map.get(parent_stream_id)
                if parent_stream is None or bool(parent_stream.is_runtime_clone):
                    changed = True
                    continue
                if stream.owner_user_id != parent_stream.owner_user_id:
                    stream.owner_user_id = parent_stream.owner_user_id
                    stream_changed = True
                if stream.owner_username != parent_stream.owner_username:
                    stream.owner_username = parent_stream.owner_username
                    stream_changed = True
                if stream.owner_display_name != parent_stream.owner_display_name:
                    stream.owner_display_name = parent_stream.owner_display_name
                    stream_changed = True
                if stream.backup_stream_id is not None:
                    stream.backup_stream_id = None
                    stream_changed = True
            else:
                if stream.is_runtime_clone:
                    stream.is_runtime_clone = False
                    stream_changed = True
                if stream.runtime_role != "primary":
                    stream.runtime_role = "primary"
                    stream_changed = True
                if stream.parent_stream_id is not None:
                    stream.parent_stream_id = None
                    stream_changed = True

            try:
                owner = self._require_workspace_user(stream.owner_user_id)
            except (KeyError, ValueError):
                changed = True
                continue

            if stream.owner_username != owner.username:
                stream.owner_username = owner.username
                stream_changed = True
            if stream.owner_display_name != owner.display_name:
                stream.owner_display_name = owner.display_name
                stream_changed = True

            available_workers = self._workspace_live_workers_for_user(owner)
            available_primary_workers = self._assigned_live_workers_for_user(owner, role="primary")
            available_backup_workers = self._assigned_live_workers_for_user(owner, role="backup")
            available_worker_map = {worker.id: worker for worker in available_workers}
            primary_worker_map = {
                worker.id: worker
                for worker in (available_backup_workers if is_runtime_backup_clone else available_primary_workers)
            }
            backup_worker_map = {worker.id: worker for worker in available_backup_workers}
            primary_worker = primary_worker_map.get(stream.primary_worker_id)
            if primary_worker is None:
                desired_primary_name = str(stream.primary_worker_name or self._resolve_worker_display_name(stream.primary_worker_id)).strip()
                primary_worker = next(
                    (
                        worker
                        for worker in (available_backup_workers if is_runtime_backup_clone else available_primary_workers)
                        if desired_primary_name
                        and desired_primary_name in {
                            str(worker.id or "").strip(),
                            self._resolve_live_worker_display_name(worker.id),
                            str(worker.name or "").strip(),
                        }
                    ),
                    None,
                )
            if primary_worker is None and (available_backup_workers if is_runtime_backup_clone else available_primary_workers):
                primary_candidates = available_backup_workers if is_runtime_backup_clone else available_primary_workers
                primary_worker = primary_candidates[0]
                if stream.primary_worker_id != primary_worker.id:
                    stream.primary_worker_id = primary_worker.id
                    stream_changed = True
            elif primary_worker is None:
                primary_worker = self._find_live_worker_optional(stream.primary_worker_id)

            if primary_worker is None:
                changed = True
                continue

            primary_name = self._resolve_live_worker_display_name(primary_worker.id)
            primary_group = primary_worker.group or primary_worker.manager_name
            if stream.primary_worker_name != primary_name:
                stream.primary_worker_name = primary_name
                stream_changed = True
            if stream.primary_group != primary_group:
                stream.primary_group = primary_group
                stream_changed = True

            backup_worker: WorkerRecord | None = None
            if not is_runtime_backup_clone and stream.backup_worker_id:
                candidate_backup = backup_worker_map.get(stream.backup_worker_id)
                if candidate_backup is None:
                    desired_backup_name = self._live_backup_worker_display_name(
                        stream.backup_worker_id,
                        stream.backup_worker_name or self._resolve_worker_display_name(stream.backup_worker_id),
                    )
                    candidate_backup = next(
                        (
                            worker
                            for worker in available_backup_workers
                            if desired_backup_name
                            and desired_backup_name in {
                                str(worker.id or "").strip(),
                                self._resolve_live_worker_display_name(worker.id),
                                f"{self._resolve_live_worker_display_name(worker.id)} - T",
                                str(worker.name or "").strip(),
                            }
                        ),
                        None,
                    )
                if candidate_backup and candidate_backup.id != primary_worker.id:
                    backup_worker = candidate_backup
                else:
                    stream_changed = True

            resolved_backup_id = backup_worker.id if backup_worker else None
            resolved_backup_name = (
                self._live_backup_worker_display_name(backup_worker.id)
                if backup_worker
                else None
            )
            resolved_backup_group = backup_worker.group if backup_worker else None
            if stream.backup_worker_id != resolved_backup_id:
                stream.backup_worker_id = resolved_backup_id
                stream_changed = True
            if stream.backup_worker_name != resolved_backup_name:
                stream.backup_worker_name = resolved_backup_name
                stream_changed = True
            if stream.backup_group != resolved_backup_group:
                stream.backup_group = resolved_backup_group
                stream_changed = True

            manager_id, manager_name = self._resolve_live_owner_manager_scope(owner, primary_worker=primary_worker)
            if stream.manager_id != manager_id:
                stream.manager_id = manager_id
                stream_changed = True
            if stream.manager_name != manager_name:
                stream.manager_name = manager_name
                stream_changed = True

            if is_runtime_backup_clone and parent_stream is not None:
                if stream.stream_key != parent_stream.stream_key:
                    stream.stream_key = parent_stream.stream_key
                    stream_changed = True
                if stream.video_url != parent_stream.video_url:
                    stream.video_url = parent_stream.video_url
                    stream_changed = True
                if stream.audio_url != parent_stream.audio_url:
                    stream.audio_url = parent_stream.audio_url
                    stream_changed = True
                if stream.start_time_live != parent_stream.start_time_live:
                    stream.start_time_live = parent_stream.start_time_live
                    stream_changed = True
                if stream.end_time_live != parent_stream.end_time_live:
                    stream.end_time_live = parent_stream.end_time_live
                    stream_changed = True
                if stream.is_forever != parent_stream.is_forever:
                    stream.is_forever = parent_stream.is_forever
                    stream_changed = True
                if stream.live_label != parent_stream.live_label:
                    stream.live_label = parent_stream.live_label
                    stream_changed = True
                if stream.backup_delay_minutes != parent_stream.backup_delay_minutes:
                    stream.backup_delay_minutes = parent_stream.backup_delay_minutes
                    stream_changed = True
                backup_stream_name = parent_stream.stream_name
                if parent_stream.end_time_live is not None:
                    backup_stream_name = f"{backup_stream_name}_backup11h"
                elif parent_stream.is_forever:
                    backup_stream_name = f"{backup_stream_name}_backup247"
                if stream.stream_name != backup_stream_name:
                    stream.stream_name = backup_stream_name
                    stream_changed = True
                backup_rtmp_url = self._backup_rtmp_url()
                if stream.rtmp_url != backup_rtmp_url:
                    stream.rtmp_url = backup_rtmp_url
                    stream_changed = True

            normalized_platform = str(stream.platform or "youtube_rtmp").strip().lower() or "youtube_rtmp"
            if normalized_platform != "youtube_rtmp":
                normalized_platform = "youtube_rtmp"
            if stream.platform != normalized_platform:
                stream.platform = normalized_platform
                stream_changed = True

            try:
                normalized_delay = self._normalize_backup_delay_minutes(stream.backup_delay_minutes)
            except ValueError:
                normalized_delay = 3
            if stream.backup_delay_minutes != normalized_delay:
                stream.backup_delay_minutes = normalized_delay
                stream_changed = True

            if stream.is_forever and stream.end_time_live is not None:
                stream.end_time_live = None
                stream_changed = True
            elif (
                stream.start_time_live is not None
                and stream.end_time_live is not None
                and stream.end_time_live < stream.start_time_live
            ):
                stream.end_time_live = None
                stream_changed = True

            derived_live_label = self._derive_live_label(
                start_time_live=stream.start_time_live,
                end_time_live=stream.end_time_live,
                is_forever=stream.is_forever,
            )
            if stream.live_label != derived_live_label:
                stream.live_label = derived_live_label
                stream_changed = True

            derived_status, derived_log_label = self._derive_live_status_defaults(
                start_time_live=stream.start_time_live,
                is_live_now=stream.is_live_now,
                is_forever=stream.is_forever,
                status_override=stream.status,
                log_override=stream.log_label,
            )
            if stream.status != derived_status:
                stream.status = derived_status
                stream_changed = True
            if stream.log_label != derived_log_label:
                stream.log_label = derived_log_label
                stream_changed = True

            normalized_streams.append(stream)
            if stream_changed:
                changed = True

        if normalized_streams != self.live_streams:
            self.live_streams = normalized_streams
            changed = True
        normalized_stream_map = {
            str(item.id or "").strip(): item
            for item in self.live_streams
            if str(item.id or "").strip()
        }
        for stream in self.live_streams:
            if stream.is_runtime_clone:
                continue
            backup_stream_id = str(stream.backup_stream_id or "").strip()
            backup_stream = normalized_stream_map.get(backup_stream_id) if backup_stream_id else None
            if backup_stream is None or not backup_stream.is_runtime_clone or backup_stream.parent_stream_id != stream.id:
                if stream.backup_stream_id is not None:
                    stream.backup_stream_id = None
                    changed = True
        return changed

    def _build_session_payload(self, user: UserSummary) -> dict[str, Any]:
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "manager_name": user.manager_name,
        }

    def _find_user_by_username(self, username: str) -> UserSummary | None:
        normalized_username = username.strip().lower()
        return next((item for item in self.users if item.username.lower() == normalized_username), None)

    def _verify_user_password(self, user_id: str, password: str) -> bool:
        meta = self._user_meta_record(user_id)
        password_hash = str(meta.get("password_hash") or "").strip()
        if password_hash:
            return self._verify_password_hash(password, password_hash)
        legacy_password = str(meta.get("password") or "").strip()
        if legacy_password and legacy_password == password.strip():
            self._set_user_password(user_id, legacy_password, updated_at=meta.get("updated_at"))
            self._save_state()
            return True
        return False

    def authenticate_admin_user(self, username: str, password: str) -> dict[str, Any]:
        normalized_username = username.strip().lower()
        normalized_password = password.strip()
        if not normalized_username or not normalized_password:
            raise ValueError("Tên đăng nhập và mật khẩu là bắt buộc.")

        user = self._find_user_by_username(normalized_username)
        if not user:
            raise ValueError("Thông tin đăng nhập không hợp lệ.")
        if user.role not in {"admin", "manager"}:
            raise ValueError("Tài khoản này không được phép vào trang quản trị.")

        if not self._verify_user_password(user.id, normalized_password):
            raise ValueError("Thông tin đăng nhập không hợp lệ.")

        return self._build_session_payload(user)

    def authenticate_app_user(self, username: str, password: str) -> dict[str, Any]:
        normalized_username = username.strip().lower()
        normalized_password = password.strip()
        if not normalized_username or not normalized_password:
            raise ValueError("Tên đăng nhập và mật khẩu là bắt buộc.")

        user = self._find_user_by_username(normalized_username)
        if not user:
            raise ValueError("Thông tin đăng nhập không hợp lệ.")
        if user.role != "user":
            raise ValueError("Tài khoản này không được phép vào workspace user.")
        if not self._verify_user_password(user.id, normalized_password):
            raise ValueError("Thông tin đăng nhập không hợp lệ.")

        return self._build_session_payload(user)

    def authenticate_login_user(self, username: str, password: str) -> dict[str, Any]:
        normalized_username = username.strip().lower()
        normalized_password = password.strip()
        if not normalized_username or not normalized_password:
            raise ValueError("Tên đăng nhập và mật khẩu là bắt buộc.")

        user = self._find_user_by_username(normalized_username)
        if not user:
            raise ValueError("Thông tin đăng nhập không hợp lệ.")
        if not self._verify_user_password(user.id, normalized_password):
            raise ValueError("Thông tin đăng nhập không hợp lệ.")
        return self._build_session_payload(user)

    def assert_admin_session_user(self, user_id: str, role: str) -> None:
        user = self._find_user(user_id)
        if user.role not in {"admin", "manager"}:
            raise ValueError("Tài khoản không được phép vào trang quản trị.")
        if user.role != role:
            raise ValueError("Role session không còn hợp lệ.")

    def assert_app_session_user(self, user_id: str, role: str) -> None:
        user = self._find_user(user_id)
        if user.role not in {"user", "manager", "admin"}:
            raise ValueError("Tài khoản không được phép vào workspace.")
        if user.role != role:
            raise ValueError("Role session không còn hợp lệ.")

    @staticmethod
    def _format_datetime(value: datetime | None, fmt: str = "%H:%M %d/%m/%y") -> str:
        if not value:
            return "-"
        return value.strftime(fmt)

    @staticmethod
    def _format_full_datetime(value: datetime | None) -> str:
        if not value:
            return "-"
        return value.strftime("%d/%m/%Y %H:%M:%S")

    @staticmethod
    def _format_date(value: datetime | None) -> str:
        if not value:
            return "-"
        return value.strftime("%d/%m/%Y")

    @staticmethod
    def _format_clock(value: datetime | None) -> str:
        if not value:
            return ""
        return value.strftime("%H:%M:%S")

    @staticmethod
    def _format_form_datetime(value: datetime | None) -> str:
        if not value:
            return ""
        return value.strftime("%d/%m/%Y %H:%M")

    @staticmethod
    def _format_compact_datetime(value: datetime | None) -> str:
        if not value:
            return "-"
        return value.strftime("%d/%m/%Y %H:%M")

    @staticmethod
    def _initials(label: str) -> str:
        value = (label or "?").strip()
        return value[:1].upper() if value else "?"

    @staticmethod
    def _avatar_palette(label: str) -> str:
        palettes = [
            "bg-rose-500 text-white",
            "bg-emerald-500 text-white",
            "bg-violet-500 text-white",
            "bg-sky-500 text-white",
            "bg-amber-500 text-white",
            "bg-fuchsia-500 text-white",
            "bg-cyan-500 text-white",
            "bg-orange-500 text-white",
            "bg-indigo-500 text-white",
            "bg-teal-500 text-white",
        ]
        normalized = (label or "?").strip()
        index = sum(ord(char) for char in normalized) % len(palettes)
        return palettes[index]

    @staticmethod
    def _guess_preview_kind(value: str | None) -> str | None:
        if not value:
            return None
        normalized = value.lower().split("?", 1)[0]
        extension = Path(normalized).suffix
        if extension in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return "image"
        if extension in {".mp4", ".mov", ".webm", ".m4v"}:
            return "video"
        return None

    @staticmethod
    def _format_render_duration(value: str | None) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            return "00:30:00"

        parts = cleaned.split(":")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            return cleaned

        hours, minutes, seconds = (int(part) for part in parts)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _extract_google_drive_file_id(url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host not in {"drive.google.com", "docs.google.com"}:
            return None

        query_id = parse_qs(parsed.query).get("id", [])
        if query_id and query_id[0]:
            return query_id[0]

        match = re.search(r"/file/d/([^/]+)", parsed.path)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _extract_youtube_video_id(url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        if host in {"www.youtube.com", "youtube.com"}:
            video_ids = parse_qs(parsed.query).get("v", [])
            return video_ids[0] if video_ids and video_ids[0] else None
        if host == "youtu.be":
            video_id = parsed.path.strip("/")
            return video_id or None
        return None

    @staticmethod
    def _extract_browser_session_channel_identity(url: str | None) -> str | None:
        if not url:
            return None
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        query = parse_qs(parsed.query)
        if host == "studio.youtube.com":
            if "/channel/" in path:
                channel_id = path.split("/channel/", 1)[1].split("/", 1)[0].strip()
                return channel_id or None
            query_channel_ids = query.get("channel_id", [])
            return query_channel_ids[0].strip() if query_channel_ids and query_channel_ids[0].strip() else None
        if host in {"www.youtube.com", "youtube.com", "m.youtube.com"} and path.startswith("/channel/"):
            channel_id = path.split("/channel/", 1)[1].split("/", 1)[0].strip()
            return channel_id or None
        return None

    def _job_uploaded_asset_ids(self, job: RenderJobRecord) -> set[str]:
        return {
            str(asset.asset_id).strip()
            for asset in job.assets
            if asset.source_mode == "local" and str(asset.asset_id or "").strip()
        }

    def _job_local_asset_file_names(self, job: RenderJobRecord) -> set[str]:
        return {
            str(asset.file_name).strip()
            for asset in job.assets
            if asset.source_mode == "local" and str(asset.file_name or "").strip()
        }

    def _is_asset_referenced_by_other_job(self, asset_id: str, *, exclude_job_id: str | None = None) -> bool:
        normalized = asset_id.strip()
        if not normalized:
            return False
        for job in self.jobs:
            if exclude_job_id and job.id == exclude_job_id:
                continue
            if normalized in self._job_uploaded_asset_ids(job):
                return True
        return False

    def _is_local_file_referenced_by_other_job(self, file_name: str, *, exclude_job_id: str | None = None) -> bool:
        normalized = file_name.strip()
        if not normalized:
            return False
        for job in self.jobs:
            if exclude_job_id and job.id == exclude_job_id:
                continue
            if normalized in self._job_local_asset_file_names(job):
                return True
        return False

    def _remove_upload_session_file(self, session: UploadSessionRecord) -> None:
        relative_path = str(session.temp_path or "").strip()
        if not relative_path:
            return
        file_path = self._absolute_upload_path(relative_path)
        if file_path.exists():
            file_path.unlink(missing_ok=True)

    def _cleanup_uploaded_assets_for_job(self, job: RenderJobRecord, *, exclude_job_id: str | None = None) -> bool:
        removable_asset_ids = {
            asset_id
            for asset_id in self._job_uploaded_asset_ids(job)
            if not self._is_asset_referenced_by_other_job(asset_id, exclude_job_id=exclude_job_id)
        }
        removable_file_names = {
            file_name
            for file_name in self._job_local_asset_file_names(job)
            if not self._is_local_file_referenced_by_other_job(file_name, exclude_job_id=exclude_job_id)
        }
        if not removable_asset_ids and not removable_file_names:
            return False

        changed = False
        keep_sessions: list[UploadSessionRecord] = []
        removed_file_names: set[str] = set()
        for session in self.upload_sessions:
            asset_id = str(session.asset_id or "").strip()
            stored_file_name = str(session.stored_file_name or "").strip()
            temp_path_name = Path(str(session.temp_path or "").strip()).name
            if (
                (asset_id and asset_id in removable_asset_ids)
                or (stored_file_name and stored_file_name in removable_file_names)
                or (temp_path_name and temp_path_name in removable_file_names)
            ):
                self._remove_upload_session_file(session)
                if stored_file_name:
                    removed_file_names.add(stored_file_name)
                if temp_path_name:
                    removed_file_names.add(temp_path_name)
                changed = True
                continue
            keep_sessions.append(session)

        if changed:
            self.upload_sessions = keep_sessions
        for file_name in removable_file_names:
            if file_name in removed_file_names:
                continue
            file_path = self.upload_asset_dir / file_name
            if file_path.exists():
                file_path.unlink(missing_ok=True)
                changed = True
        return changed

    def _purge_job_artifacts(self, job: RenderJobRecord, *, exclude_job_id: str | None = None) -> None:
        self._cleanup_uploaded_assets_for_job(job, exclude_job_id=exclude_job_id)
        self._delete_job_preview_file(job)

    @staticmethod
    def _derive_legacy_job_phase_progress(job: RenderJobRecord) -> tuple[int, int, int]:
        download_progress = 0
        render_progress = 0
        upload_progress = 0

        if job.status == "downloading":
            download_progress = min(max(job.progress, 0), 100)
        elif job.status == "rendering":
            download_progress = 100
            render_progress = min(max(job.progress, 0), 100)
        elif job.status in {"uploading", "completed"}:
            download_progress = 100
            render_progress = 100
            upload_progress = 100 if job.status == "completed" and job.upload_started_at else min(max(job.progress, 0), 100)
        elif job.status in {"error", "cancelled"}:
            if job.upload_started_at:
                download_progress = 100
                render_progress = 100
                upload_progress = min(max(job.progress, 0), 100)
            elif job.started_at or job.claimed_at or job.completed_at:
                download_progress = 100
                render_progress = min(max(job.progress, 0), 100)

        return (
            min(max(download_progress, 0), 100),
            min(max(render_progress, 0), 100),
            min(max(upload_progress, 0), 100),
        )

    def _normalize_job_runtime_progress(self) -> bool:
        changed = False
        for job in self.jobs:
            if any(
                (
                    int(getattr(job, "download_progress", 0) or 0),
                    int(getattr(job, "render_progress", 0) or 0),
                    int(getattr(job, "upload_progress", 0) or 0),
                )
            ):
                continue
            derived_download, derived_render, derived_upload = self._derive_legacy_job_phase_progress(job)
            if job.download_progress != derived_download:
                job.download_progress = derived_download
                changed = True
            if job.render_progress != derived_render:
                job.render_progress = derived_render
                changed = True
            if job.upload_progress != derived_upload:
                job.upload_progress = derived_upload
                changed = True
        return changed

    @staticmethod
    def _job_status_label(status: str) -> str:
        mapping = {
            "pending": "Chờ tạo hàng đợi",
            "queueing": "Đang xếp hàng",
            "downloading": "Đang tải nguồn",
            "rendering": "Đang render",
            "uploading": "Đang upload",
            "completed": "Hoàn tất",
            "cancelled": "Đã hủy",
            "error": "Lỗi",
        }
        return mapping.get(status, status)

    @staticmethod
    def _job_status_class(status: str) -> str:
        mapping = {
            "pending": "inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] font-semibold text-amber-700",
            "queueing": "inline-flex items-center rounded-full border border-orange-200 bg-orange-50 px-2.5 py-1 text-[10px] font-semibold text-orange-700",
            "downloading": "inline-flex items-center rounded-full border border-sky-200 bg-sky-50 px-2.5 py-1 text-[10px] font-semibold text-sky-700",
            "rendering": "inline-flex items-center rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[10px] font-semibold text-indigo-700",
            "uploading": "inline-flex items-center rounded-full border border-cyan-200 bg-cyan-50 px-2.5 py-1 text-[10px] font-semibold text-cyan-700",
            "completed": "inline-flex items-center rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[10px] font-semibold text-emerald-700",
            "cancelled": "inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700",
            "error": "inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-2.5 py-1 text-[10px] font-semibold text-rose-700",
        }
        return mapping.get(status, "inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700")

    def _job_user_status_view(self, job: RenderJobRecord) -> dict[str, Any]:
        if job.status == "completed":
            if job.upload_started_at:
                return {
                    "label": "Đã upload YouTube",
                    "class": "inline-flex items-center rounded-full border border-sky-200 bg-sky-50 px-2.5 py-1 text-[10px] font-semibold text-sky-700",
                    "progress_text_class": "text-sky-600",
                    "progress_bar_class": "bg-sky-500",
                    "render_at": self._format_datetime(job.upload_started_at or job.completed_at),
                    "upload_at": self._format_datetime(job.completed_at),
                }
            return {
                "label": "Render hoàn tất",
                "class": "inline-flex items-center rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[10px] font-semibold text-indigo-700",
                "progress_text_class": "text-indigo-600",
                "progress_bar_class": "bg-indigo-500",
                "render_at": self._format_datetime(job.completed_at),
                "upload_at": "-",
            }

        progress_text_class = "text-amber-600"
        progress_bar_class = "bg-slate-300"
        if job.status == "downloading":
            progress_text_class = "text-sky-600"
            progress_bar_class = "bg-sky-400"
        elif job.status == "rendering":
            progress_text_class = "text-indigo-600"
            progress_bar_class = "bg-indigo-500"
        elif job.status == "uploading":
            progress_text_class = "text-cyan-600"
            progress_bar_class = "bg-cyan-500"
        elif job.status == "error":
            progress_text_class = "text-rose-600"
            progress_bar_class = "bg-rose-400"
        elif job.status == "cancelled":
            progress_text_class = "text-slate-500"
            progress_bar_class = "bg-slate-300"

        return {
            "label": self._job_status_label(job.status),
            "class": self._job_status_class(job.status),
            "progress_text_class": progress_text_class,
            "progress_bar_class": progress_bar_class,
            "render_at": self._format_datetime(job.upload_started_at),
            "upload_at": "-",
        }

    @staticmethod
    def _job_user_progress_view(job: RenderJobRecord) -> dict[str, int | str]:
        return {
            "mode": "download" if job.status == "downloading" else "pipeline",
            "download": min(max(int(job.download_progress or 0), 0), 100),
            "render": min(max(int(job.render_progress or 0), 0), 100),
            "upload": min(max(int(job.upload_progress or 0), 0), 100),
        }

    def _find_user(self, user_id: str) -> UserSummary:
        for user in self.users:
            if user.id == user_id:
                return user
        raise KeyError(user_id)

    def _find_worker(self, worker_id: str) -> WorkerRecord:
        for worker in self.workers:
            if worker.id == worker_id:
                return worker
        raise KeyError(worker_id)

    def _find_live_worker(self, worker_id: str) -> WorkerRecord:
        for worker in self.live_workers:
            if worker.id == worker_id:
                return worker
        raise KeyError(worker_id)

    @staticmethod
    def _normalize_workspace_mode(value: str | None = None) -> str:
        return "live" if str(value or "").strip().lower() == "live" else "upload"

    def _workspace_worker_pool(self, workspace_mode: str = "upload") -> list[WorkerRecord]:
        return self.live_workers if self._normalize_workspace_mode(workspace_mode) == "live" else self.workers

    def _find_workspace_worker(self, worker_id: str, *, workspace_mode: str = "upload") -> WorkerRecord:
        if self._normalize_workspace_mode(workspace_mode) == "live":
            return self._find_live_worker(worker_id)
        return self._find_worker(worker_id)

    @classmethod
    def _default_worker_display_name(cls, worker_id: str | None, fallback: str | None = None) -> str:
        normalized_id = str(worker_id or "").strip()
        normalized_fallback = str(fallback or "").strip()
        if normalized_fallback:
            return normalized_fallback
        if normalized_id and normalized_id in cls.KNOWN_WORKER_DISPLAY_NAMES:
            return cls.KNOWN_WORKER_DISPLAY_NAMES[normalized_id]
        return normalized_id or "-"

    def _normalize_known_worker_names(self) -> bool:
        changed = False
        for worker in self.workers:
            display_name = self._default_worker_display_name(worker.id, worker.name)
            if worker.name != display_name:
                worker.name = display_name
                changed = True
        return changed

    def _resolve_worker_display_name(self, worker_ref: str | None) -> str:
        if not worker_ref:
            return "-"
        normalized = worker_ref.strip()
        if not normalized:
            return "-"
        worker = next(
            (
                item
                for item in self.workers
                if normalized in {str(item.id or "").strip(), str(item.name or "").strip()}
            ),
            None,
        )
        if worker is None:
            return normalized
        display_name = str(worker.name or "").strip()
        return display_name or str(worker.id or normalized).strip() or normalized

    def _resolve_live_worker_display_name(self, worker_ref: str | None) -> str:
        if not worker_ref:
            return "-"
        normalized = worker_ref.strip()
        if not normalized:
            return "-"
        worker = next(
            (
                item
                for item in self.live_workers
                if normalized in {str(item.id or "").strip(), str(item.name or "").strip()}
            ),
            None,
        )
        if worker is None:
            return normalized
        display_name = str(worker.name or "").strip()
        return display_name or str(worker.id or normalized).strip() or normalized

    def _resolve_workspace_worker_display_name(self, worker_ref: str | None, *, workspace_mode: str = "upload") -> str:
        if self._normalize_workspace_mode(workspace_mode) == "live":
            return self._resolve_live_worker_display_name(worker_ref)
        return self._resolve_worker_display_name(worker_ref)

    def _resolve_channel_worker_display_name(self, channel: ChannelRecord) -> str:
        return self._resolve_worker_display_name(channel.worker_id or channel.worker_name)

    def _resolve_job_worker_display_name(self, job: RenderJobRecord) -> str:
        return self._resolve_worker_display_name(job.worker_name)

    def _authenticate_worker(self, worker_id: str, shared_secret: str) -> WorkerRecord:
        if shared_secret != self.get_worker_shared_secret():
            raise ValueError("Worker shared secret không hợp lệ.")
        self._ensure_worker_not_deleted(worker_id)
        return self._find_worker(worker_id)

    def _refresh_queue_positions(self) -> None:
        queued_jobs = sorted(
            [
                job
                for job in self.jobs
                if job.status in {"pending", "queueing"} and (job.scheduled_at is None or job.scheduled_at <= self._now())
            ],
            key=lambda item: (item.queue_order or 10_000, item.created_at),
        )
        for index, job in enumerate(queued_jobs, start=1):
            job.queue_order = index
        for job in self.jobs:
            if job.status in {"completed", "cancelled", "error"}:
                job.queue_order = None

    @staticmethod
    def _worker_active_job_statuses() -> set[str]:
        return {"queueing", "downloading", "rendering", "uploading"}

    def _job_user_id(self, job: RenderJobRecord) -> str | None:
        for link in self.channel_user_links:
            if str(link.get("channel_id") or "").strip() != job.channel_id:
                continue
            user_id = str(link.get("user_id") or "").strip()
            if not user_id:
                continue
            try:
                self._find_user(user_id)
            except KeyError:
                continue
            return user_id
        return None

    def _job_dispatch_owner_key(self, job: RenderJobRecord) -> str:
        user_id = self._job_user_id(job)
        if user_id:
            return f"user:{user_id}"
        username = str(self._job_username(job) or "").strip().casefold()
        if username:
            return f"username:{username}"
        return f"job:{job.id}"

    def _pick_worker_round_robin_job(
        self,
        worker: WorkerRecord,
        candidates: list[RenderJobRecord],
        *,
        last_owner_key: str | None = None,
    ) -> tuple[RenderJobRecord | None, str | None]:
        if not candidates:
            return None, None

        ordered_candidates = sorted(candidates, key=lambda item: (item.queue_order or 10_000, item.created_at))
        owner_order: list[str] = []
        jobs_by_owner: dict[str, list[RenderJobRecord]] = {}
        for job in ordered_candidates:
            owner_key = self._job_dispatch_owner_key(job)
            if owner_key not in jobs_by_owner:
                jobs_by_owner[owner_key] = []
                owner_order.append(owner_key)
            jobs_by_owner[owner_key].append(job)

        if not owner_order:
            return ordered_candidates[0], self._job_dispatch_owner_key(ordered_candidates[0])
        if len(owner_order) == 1:
            selected_job = ordered_candidates[0]
            return selected_job, owner_order[0]

        effective_last_owner_key = (
            str(last_owner_key or "").strip()
            if last_owner_key is not None
            else str(self.worker_round_robin_cursor.get(worker.id) or "").strip()
        )
        if effective_last_owner_key and effective_last_owner_key in owner_order:
            start_index = (owner_order.index(effective_last_owner_key) + 1) % len(owner_order)
        else:
            start_index = 0

        for offset in range(len(owner_order)):
            owner_key = owner_order[(start_index + offset) % len(owner_order)]
            owner_jobs = jobs_by_owner.get(owner_key) or []
            if owner_jobs:
                return owner_jobs[0], owner_key

        return ordered_candidates[0], self._job_dispatch_owner_key(ordered_candidates[0])

    def _upload_claim_position_by_job_id(self, *, now: datetime | None = None) -> dict[str, int]:
        current = now or self._now()
        positions: dict[str, int] = {}
        for worker in self.workers:
            worker_aliases = {str(worker.id or "").strip(), str(worker.name or "").strip()}
            worker_aliases.discard("")
            candidates = [
                job
                for job in self.jobs
                if str(job.worker_name or "").strip() in worker_aliases
                and job.status in {"pending", "queueing"}
                and (job.scheduled_at is None or job.scheduled_at <= current)
                and (job.claimed_by_worker_id is None or (job.lease_expires_at and job.lease_expires_at <= current))
            ]
            last_owner_key = str(self.worker_round_robin_cursor.get(worker.id) or "").strip()
            position = 1
            while candidates:
                job, owner_key = self._pick_worker_round_robin_job(
                    worker,
                    candidates,
                    last_owner_key=last_owner_key,
                )
                if job is None:
                    break
                positions[job.id] = position
                candidates = [candidate for candidate in candidates if candidate.id != job.id]
                if owner_key:
                    last_owner_key = owner_key
                position += 1
        return positions

    def _count_worker_running_jobs(self, worker: WorkerRecord) -> int:
        worker_aliases = {worker.id, worker.name, self._resolve_worker_display_name(worker.id)}
        active_statuses = self._worker_active_job_statuses()
        return sum(
            1
            for job in self.jobs
            if job.status in active_statuses
            and (
                job.claimed_by_worker_id == worker.id
                or (not job.claimed_by_worker_id and (job.worker_name in worker_aliases))
            )
        )

    def _worker_thread_summary(self, worker: WorkerRecord) -> dict[str, int | str]:
        running_threads = self._count_worker_running_jobs(worker)
        max_threads = self._effective_worker_thread_limit(worker)
        return {
            "running_threads": running_threads,
            "max_threads": max_threads,
            "thread_text": f"{running_threads}/{max_threads}",
            "note_text": "Chạy / Tổng",
        }

    def _sync_worker_runtime_status(self, worker: WorkerRecord) -> None:
        running_threads = self._count_worker_running_jobs(worker)
        worker.status = "busy" if running_threads > 0 else "online"

    def _refresh_worker_job_leases(
        self,
        worker_id: str,
        now: datetime | None = None,
        *,
        job_ids: list[str] | None = None,
    ) -> None:
        lease_base = now or self._now()
        active_statuses = self._worker_active_job_statuses()
        allowed_job_ids = {str(job_id).strip() for job_id in (job_ids or []) if str(job_id).strip()}
        lease_duration = timedelta(seconds=self._worker_job_lease_seconds())
        for job in self.jobs:
            if job.claimed_by_worker_id == worker_id and job.status in active_statuses:
                if allowed_job_ids and job.id not in allowed_job_ids:
                    continue
                job.lease_expires_at = lease_base + lease_duration

    def _pending_install_config(self, task: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(task, dict):
            return None
        config = task.get("post_install_config")
        return config if isinstance(config, dict) else None

    def _apply_pending_install_config(self, task: dict[str, Any], *, worker_id: str, workspace_mode: str) -> None:
        config = self._pending_install_config(task)
        if not config:
            return
        requested_by = str(task.get("requested_by") or "").strip() or "system"
        requested_role = str(task.get("requested_role") or "").strip() or "system"
        requested_user_id = str(task.get("requested_user_id") or "").strip() or None
        if workspace_mode == "live":
            worker_name = self._resolve_live_worker_display_name(worker_id)
        else:
            worker_name = self._resolve_worker_display_name(worker_id)
        desired_name = str(config.get("name") or "").strip() or worker_name
        desired_local = str(config.get("local") or "").strip()
        desired_group = str(config.get("group") or "").strip() or None
        desired_manager_id = str(config.get("manager_id") or "").strip() or None
        desired_live_role = str(config.get("live_role") or "").strip() or None
        desired_threads = config.get("threads")
        desired_user_ids = [
            str(value).strip()
            for value in (config.get("assigned_user_ids") or [])
            if str(value).strip()
        ]
        if workspace_mode == "live" and desired_user_ids and not desired_live_role:
            raise ValueError("BOT live cài xong nhưng chưa có chức năng BOT để gán user.")
        self.update_bot(
            worker_id,
            desired_name,
            desired_local,
            desired_group,
            desired_manager_id,
            workspace_mode=workspace_mode,
            live_role=desired_live_role,
            threads=desired_threads,
            assigned_user_ids=desired_user_ids if desired_user_ids else None,
            viewer_role=requested_role,
            viewer_id=requested_user_id,
            updated_by=requested_by,
        )

    def register_worker(self, payload: WorkerRegisterPayload) -> WorkerControlResponse:
        completed_task: dict[str, Any] | None = None
        worker_snapshot: WorkerRecord | None = None
        reconnect_notification: tuple[list[str], str] | None = None
        vps_ip = ""
        with self._worker_state_lock:
            if payload.shared_secret != self.get_worker_shared_secret():
                raise ValueError("Worker shared secret không hợp lệ.")
            self._ensure_worker_not_deleted(payload.worker_id)
            if self._has_pending_install_for_other_workspace(payload.worker_id, workspace_mode="upload"):
                raise ValueError("BOT này đang được chuyển sang workspace live. Worker upload hiện tại sẽ tự khởi động lại.")

            now = self._now(trim=False)
            worker_display_name = self._default_worker_display_name(payload.worker_id, payload.name)
            public_base_url = str(payload.public_base_url or "").strip() or None
            existing = next((worker for worker in self.workers if worker.id == payload.worker_id), None)
            install_task = next(
                (
                    task
                    for task in self.worker_operation_tasks
                    if str(task.get("kind") or "").strip() == "install"
                    and str(task.get("worker_id") or "").strip() == payload.worker_id
                    and self._restore_worker_operation_workspace_mode(task) == "upload"
                ),
                None,
            )
            install_task_manager_id = str(install_task.get("manager_id") or "").strip() if install_task else ""
            install_task_manager_name = str(install_task.get("manager_name") or "").strip() if install_task else ""
            install_task_group = str(install_task.get("group") or "").strip() if install_task else ""
            install_task_config = self._pending_install_config(install_task)
            install_task_local = str((install_task_config or {}).get("local") or "").strip()
            manager = None
            if install_task_manager_id:
                manager = next(
                    (
                        user
                        for user in self.users
                        if user.id == install_task_manager_id and user.role == "manager"
                    ),
                    None,
                )
            if manager is None:
                manager = next((user for user in self.users if user.username == payload.manager_name and user.role == "manager"), None)
            resolved_manager_name = (
                manager.username if manager else (install_task_manager_name or str(payload.manager_name or "").strip() or "system")
            )
            resolved_group = self._normalized_bot_group(
                install_task_group or str(payload.group or "").strip()
            )
            normalized_threads = self._normalize_requested_worker_threads(payload.threads)
            if existing is None:
                worker = WorkerRecord(
                    id=payload.worker_id,
                    name=worker_display_name,
                    local=install_task_local or None,
                    manager_id=manager.id if manager else None,
                    manager_name=resolved_manager_name,
                    group=resolved_group,
                    created_at=now,
                    status="online",
                    capacity=payload.capacity,
                    load_percent=0,
                    ram_percent=0,
                    ram_used_gb=0,
                    ram_total_gb=0,
                    bandwidth_kbps=0,
                    disk_used_gb=0,
                    disk_total_gb=payload.disk_total_gb,
                    threads=normalized_threads,
                    last_seen_at=now,
                    public_base_url=public_base_url,
                    browser_session_enabled=payload.browser_session_enabled,
                    browser_display_base=payload.browser_display_base,
                    browser_vnc_port_base=payload.browser_vnc_port_base,
                    browser_web_port_base=payload.browser_web_port_base,
                    browser_debug_port_base=payload.browser_debug_port_base,
                )
                worker.offline_since_at = None
                worker.offline_alert_sent_at = None
                self.workers.append(worker)
            else:
                reconnect_notification = self._worker_reconnect_notification(existing, now=now, live=False)
                existing.name = worker_display_name
                if install_task_local:
                    existing.local = install_task_local
                existing.manager_id = manager.id if manager else existing.manager_id
                existing.manager_name = resolved_manager_name
                payload_group = resolved_group
                current_group = str(existing.group or "").strip()
                if payload_group:
                    if not current_group or current_group in {
                        str(existing.manager_name or "").strip(),
                        resolved_manager_name,
                    }:
                        existing.group = payload_group
                elif not current_group:
                    existing.group = self._default_bot_group_label()
                existing.created_at = existing.created_at or now
                existing.capacity = max(int(existing.capacity or 1), int(payload.capacity or 1))
                existing.threads = self._normalize_requested_worker_threads(
                    payload.threads or existing.threads or 1,
                )
                existing.disk_total_gb = payload.disk_total_gb or existing.disk_total_gb
                existing.status = "online"
                existing.last_seen_at = now
                existing.offline_since_at = None
                existing.offline_alert_sent_at = None
                existing.public_base_url = public_base_url or existing.public_base_url
                existing.browser_session_enabled = payload.browser_session_enabled
                existing.browser_display_base = payload.browser_display_base
                existing.browser_vnc_port_base = payload.browser_vnc_port_base
                existing.browser_web_port_base = payload.browser_web_port_base
                existing.browser_debug_port_base = payload.browser_debug_port_base
                worker = existing

            if self._looks_like_ipv4(worker_display_name):
                existing_profile = self.worker_connection_profiles.get(payload.worker_id) or {}
                self._remember_worker_connection_profile(
                    payload.worker_id,
                    vps_ip=worker_display_name,
                    ssh_user=str(existing_profile.get("ssh_user") or "root"),
                )
            existing_registration_meta = dict(self.worker_registration_meta.get(str(payload.worker_id or "").strip()) or {})
            registration_source = self._worker_registration_source_for_task(install_task)
            if not registration_source:
                registration_source = str(existing_registration_meta.get("source") or "").strip()
            if not registration_source:
                registration_source = "manual_register" if existing is None else "register"
            self._remember_worker_registration_meta(
                payload.worker_id,
                workspace_mode="upload",
                source=registration_source,
                task=install_task,
                manager_id=manager.id if manager else None,
                manager_name=resolved_manager_name,
            )
            if install_task is None and existing is None:
                logger.warning(
                    "worker_registered_without_install_task workspace=upload worker_id=%s worker_name=%s manager_name=%s",
                    str(payload.worker_id or "").strip(),
                    worker_display_name,
                    resolved_manager_name,
                )
            completed_task = deepcopy(install_task) if install_task is not None else None
            if install_task is not None:
                self._finalize_workspace_conversion_source(install_task)
            vps_ip = str(payload.name or "").strip() or worker_display_name
            self.worker_operation_tasks = [
                task
                for task in self.worker_operation_tasks
                if not (
                    str(task.get("kind") or "").strip() == "install"
                    and str(task.get("worker_id") or "").strip() == payload.worker_id
                    and self._restore_worker_operation_workspace_mode(task) == "upload"
                )
            ]

            self._save_state()
            worker_snapshot = deepcopy(worker)
        if completed_task is not None and worker_snapshot is not None:
            self._resume_worker_operation_queue()
            config_error: str | None = None
            try:
                self._apply_pending_install_config(completed_task, worker_id=worker_snapshot.id, workspace_mode="upload")
                with self._worker_state_lock:
                    worker_snapshot = deepcopy(self._find_worker(worker_snapshot.id))
            except (KeyError, ValueError) as exc:
                config_error = str(exc)
                self._push_admin_notification(
                    message=f"BOT {worker_snapshot.name} đã cài xong nhưng không thể áp cấu hình ban đầu: {config_error}",
                    level="warning",
                    manager_id=str(completed_task.get("manager_id") or "").strip() or None,
                )
            self._notify_telegram_chat_ids(
                self._bot_operation_recipient_chat_ids(completed_task),
                self._bot_install_result_message(
                    completed_task,
                    worker_name=self._resolve_worker_display_name(worker_snapshot.id),
                    worker_id=worker_snapshot.id,
                    vps_ip=vps_ip or self._resolve_worker_display_name(worker_snapshot.id),
                ) + (
                    f"\nLưu ý cấu hình ban đầu: {config_error}"
                    if config_error
                    else ""
                ),
            )
        if reconnect_notification is not None:
            self._notify_telegram_chat_ids(reconnect_notification[0], reconnect_notification[1])
        return WorkerControlResponse(ok=True, worker=worker_snapshot)

    def heartbeat_worker(self, payload: WorkerHeartbeatPayload) -> WorkerControlResponse:
        reconnect_notification: tuple[list[str], str] | None = None
        upload_interruption_notifications: list[tuple[list[str], str]] = []
        with self._worker_state_lock:
            worker = self._authenticate_worker(payload.worker_id, payload.shared_secret)
            now = self._now(trim=False)
            reconnect_notification = self._worker_reconnect_notification(worker, now=now, live=False)
            worker.load_percent = payload.load_percent
            worker.ram_percent = payload.ram_percent
            worker.ram_used_gb = payload.ram_used_gb
            worker.ram_total_gb = payload.ram_total_gb or worker.ram_total_gb
            worker.bandwidth_kbps = payload.bandwidth_kbps
            worker.disk_used_gb = payload.disk_used_gb
            worker.disk_total_gb = payload.disk_total_gb or worker.disk_total_gb
            worker.threads = self._normalize_requested_worker_threads(payload.threads)
            worker.last_seen_at = now
            worker.offline_since_at = None
            worker.offline_alert_sent_at = None
            if payload.public_base_url is not None:
                worker.public_base_url = str(payload.public_base_url).strip() or None
            if payload.browser_session_enabled is not None:
                worker.browser_session_enabled = bool(payload.browser_session_enabled)
            if payload.browser_display_base is not None:
                worker.browser_display_base = payload.browser_display_base
            if payload.browser_vnc_port_base is not None:
                worker.browser_vnc_port_base = payload.browser_vnc_port_base
            if payload.browser_web_port_base is not None:
                worker.browser_web_port_base = payload.browser_web_port_base
            if payload.browser_debug_port_base is not None:
                worker.browser_debug_port_base = payload.browser_debug_port_base
            if payload.active_job_ids is None:
                upload_interruption_notifications.extend(
                    self._reconcile_worker_jobs_from_heartbeat(
                        worker,
                        active_job_ids=[],
                        now=now,
                    )
                )
            else:
                upload_interruption_notifications.extend(
                    self._reconcile_worker_jobs_from_heartbeat(
                        worker,
                        active_job_ids=payload.active_job_ids,
                        now=now,
                    )
                )
            if self._count_worker_running_jobs(worker) > 0:
                worker.status = "busy"
            else:
                worker.status = payload.status
            self._save_state()
            snapshot = deepcopy(worker)
        if reconnect_notification is not None:
            self._notify_telegram_chat_ids(reconnect_notification[0], reconnect_notification[1])
        for chat_ids, message in upload_interruption_notifications:
            self._notify_live_telegram_chat_ids(chat_ids, message)
        return WorkerControlResponse(ok=True, worker=snapshot)

    def claim_next_job(self, worker_id: str, shared_secret: str) -> tuple[WorkerRecord, RenderJobRecord | None]:
        with self._worker_state_lock:
            worker = self._authenticate_worker(worker_id, shared_secret)
            now = self._now()
            max_threads = self._effective_worker_thread_limit(worker)
            active_jobs = self._count_worker_running_jobs(worker)
            if active_jobs >= max_threads:
                self._sync_worker_runtime_status(worker)
                self._save_state()
                return deepcopy(worker), None

            candidates = [
                job
                for job in self.jobs
                if job.worker_name in {worker.id, worker.name}
                and job.status in {"pending", "queueing"}
                and (job.scheduled_at is None or job.scheduled_at <= now)
                and (job.claimed_by_worker_id is None or (job.lease_expires_at and job.lease_expires_at <= now))
            ]

            if not candidates:
                self._sync_worker_runtime_status(worker)
                self._save_state()
                return deepcopy(worker), None

            job, owner_key = self._pick_worker_round_robin_job(worker, candidates)
            if job is None:
                self._sync_worker_runtime_status(worker)
                self._save_state()
                return deepcopy(worker), None
            job.status = "queueing"
            job.claimed_by_worker_id = worker_id
            job.claimed_at = now
            if owner_key:
                self.worker_round_robin_cursor[worker.id] = owner_key
            self._refresh_worker_job_leases(worker_id, now, job_ids=[job.id])
            job.started_at = job.started_at or now
            self._sync_worker_runtime_status(worker)
            self._refresh_queue_positions()
            self._save_state()
            return deepcopy(worker), deepcopy(job)

    def _find_claimed_job(self, job_id: str, worker_id: str) -> RenderJobRecord:
        for job in self.jobs:
            if job.id == job_id:
                if job.claimed_by_worker_id not in {None, worker_id} and job.status not in {"completed", "cancelled", "error"}:
                    raise ValueError("Job đang thuộc worker khác.")
                return job
        raise KeyError(job_id)

    @staticmethod
    def _ensure_worker_job_can_continue(job: RenderJobRecord) -> None:
        if job.status == "cancelled":
            raise ValueError("Job đã bị hủy trên control plane.")
        if job.status == "completed":
            raise ValueError("Job đã hoàn tất trên control plane.")
        if job.status == "error":
            raise ValueError("Job đã dừng với trạng thái lỗi trên control plane.")

    def update_worker_job_progress(
        self,
        *,
        job_id: str,
        worker_id: str,
        shared_secret: str,
        status: str,
        progress: int,
        message: str | None = None,
    ) -> RenderJobRecord:
        with self._worker_state_lock:
            worker = self._authenticate_worker(worker_id, shared_secret)
            job = self._find_claimed_job(job_id, worker_id)
            self._ensure_worker_job_can_continue(job)
            now = self._now()

            job.claimed_by_worker_id = worker_id
            job.claimed_at = job.claimed_at or now
            self._refresh_worker_job_leases(worker_id, now, job_ids=[job.id])
            job.started_at = job.started_at or now
            job.status = status  # type: ignore[assignment]
            bounded_progress = max(0, min(100, progress))
            job.progress = bounded_progress
            job.status_message = (message or "").strip() or None
            job.error_message = None
            if status == "downloading" and job.download_started_at is None:
                job.download_started_at = now
            if status == "downloading":
                job.download_progress = max(int(job.download_progress or 0), bounded_progress)
                job.render_progress = 0
                job.upload_progress = 0
            if status == "uploading" and job.upload_started_at is None:
                job.upload_started_at = now
            if status == "rendering":
                job.download_progress = max(int(job.download_progress or 0), 100)
                job.render_progress = max(int(job.render_progress or 0), bounded_progress)
                job.upload_progress = 0
            elif status == "uploading":
                job.download_progress = max(int(job.download_progress or 0), 100)
                job.render_progress = max(int(job.render_progress or 0), 100)
                job.upload_progress = max(int(job.upload_progress or 0), bounded_progress)
            self._sync_worker_runtime_status(worker)

            self._save_state()
            return deepcopy(job)

    def complete_worker_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        shared_secret: str,
        output_url: str | None = None,
        message: str | None = None,
    ) -> RenderJobRecord:
        notification_payload: tuple[list[str], str] | None = None
        with self._worker_state_lock:
            worker = self._authenticate_worker(worker_id, shared_secret)
            job = self._find_claimed_job(job_id, worker_id)
            self._ensure_worker_job_can_continue(job)
            now = self._now()

            job.status = "completed"
            job.progress = 100
            job.completed_at = now
            job.can_cancel = False
            job.output_url = output_url
            job.status_message = (message or "").strip() or None
            job.error_message = None
            job.lease_expires_at = None
            job.download_progress = max(int(job.download_progress or 0), 100 if job.started_at or job.download_started_at else 0)
            job.render_progress = max(int(job.render_progress or 0), 100 if job.started_at else 0)
            if job.upload_started_at:
                job.upload_progress = 100
            self._sync_worker_runtime_status(worker)
            if job.upload_started_at:
                self._cleanup_uploaded_assets_for_job(job, exclude_job_id=job.id)
                chat_ids = self._job_upload_interruption_recipient_chat_ids(job)
                if chat_ids:
                    notification_payload = (chat_ids, self._job_upload_completed_message(job, now=now))
            self._refresh_queue_positions()
            self._save_state()
            snapshot = deepcopy(job)
        if notification_payload is not None:
            self._notify_live_telegram_chat_ids(notification_payload[0], notification_payload[1])
        return snapshot

    def fail_worker_job(self, *, job_id: str, worker_id: str, shared_secret: str, message: str) -> RenderJobRecord:
        notification_payload: tuple[list[str], str] | None = None
        with self._worker_state_lock:
            worker = self._authenticate_worker(worker_id, shared_secret)
            job = self._find_claimed_job(job_id, worker_id)
            now = self._now()

            if job.status == "cancelled":
                job.can_cancel = False
                job.lease_expires_at = None
                self._sync_worker_runtime_status(worker)
                self._refresh_queue_positions()
                self._save_state()
                return deepcopy(job)

            job.status = "error"
            job.completed_at = now
            job.can_cancel = False
            job.error_message = message
            job.status_message = None
            job.lease_expires_at = None
            notification_message = self._job_upload_error_message(job, now=now, error_message=message)
            if notification_message:
                chat_ids = self._job_upload_interruption_recipient_chat_ids(job)
                if chat_ids:
                    notification_payload = (chat_ids, notification_message)
            if job.upload_started_at:
                job.download_progress = max(int(job.download_progress or 0), 100)
                job.render_progress = max(int(job.render_progress or 0), 100)
                job.upload_progress = max(int(job.upload_progress or 0), min(max(job.progress, 0), 100))
            elif job.started_at or job.download_started_at:
                job.download_progress = max(int(job.download_progress or 0), 100 if job.download_started_at else 0)
                job.render_progress = max(int(job.render_progress or 0), min(max(job.progress, 0), 100))
            self._sync_worker_runtime_status(worker)
            self._refresh_queue_positions()
            self._save_state()
            snapshot = deepcopy(job)
        if notification_payload is not None:
            self._notify_live_telegram_chat_ids(notification_payload[0], notification_payload[1])
        return snapshot

    def _authenticate_live_worker(self, worker_id: str, shared_secret: str) -> WorkerRecord:
        if shared_secret != self.get_worker_shared_secret():
            raise ValueError("Worker shared secret không hợp lệ.")
        return self._find_live_worker(worker_id)

    @staticmethod
    def _is_runtime_backup_clone(stream: LiveStreamRecord) -> bool:
        return bool(stream.is_runtime_clone and stream.runtime_role == "backup")

    @classmethod
    def _is_visible_live_stream(cls, stream: LiveStreamRecord) -> bool:
        return not cls._is_runtime_backup_clone(stream)

    @staticmethod
    def _backup_rtmp_url() -> str:
        return "rtmp://y.rtmp.youtube.com/live2?backup=1"

    @staticmethod
    def _live_worker_active_statuses() -> set[str]:
        return {"downloading", "preparing", "waiting", "streaming"}

    @classmethod
    def _live_worker_lease_refresh_statuses(cls) -> set[str]:
        return {*cls._live_worker_active_statuses(), "disconnected"}

    @staticmethod
    def _live_fast_failover_lease_seconds() -> int:
        raw_value = str(os.getenv("LIVE_FAST_FAILOVER_LEASE_SECONDS", "20")).strip()
        try:
            return max(5, int(raw_value))
        except ValueError:
            return 20

    @staticmethod
    def _live_retry_cooldown_seconds() -> int:
        raw_value = str(os.getenv("LIVE_RETRY_COOLDOWN_SECONDS", "180")).strip()
        try:
            return max(30, int(raw_value))
        except ValueError:
            return 180

    def _live_runtime_retry_anchor(self, stream: LiveStreamRecord) -> datetime | None:
        return (
            stream.disconnected_at
            or stream.updated_at
            or stream.claimed_at
            or stream.streaming_started_at
            or stream.waiting_started_at
            or stream.prepared_at
            or stream.download_started_at
            or stream.created_at
        )

    def _live_runtime_retry_ready(self, stream: LiveStreamRecord, *, now: datetime) -> bool:
        retry_anchor = self._live_runtime_retry_anchor(stream)
        if retry_anchor is None:
            return True
        retry_after = retry_anchor + timedelta(seconds=self._live_retry_cooldown_seconds())
        return retry_after <= now

    def _should_notify_live_disconnect(self, stream: LiveStreamRecord, *, now: datetime) -> bool:
        last_disconnected_at = stream.disconnected_at
        if last_disconnected_at is None:
            return True
        retry_after = last_disconnected_at + timedelta(seconds=self._live_retry_cooldown_seconds())
        return retry_after <= now

    @classmethod
    def _live_worker_scheduled_statuses(cls) -> set[str]:
        return {"scheduled", "disconnected", *cls._live_worker_active_statuses()}

    @classmethod
    def _live_worker_claimable_statuses(cls) -> set[str]:
        return {"scheduled", "disconnected", "downloading", "preparing", "waiting"}

    @classmethod
    def _live_pre_stream_statuses(cls) -> set[str]:
        return {"scheduled", "downloading", "preparing", "waiting"}

    def _count_live_worker_running_streams(self, worker: WorkerRecord, *, role: str | None = None) -> int:
        active_statuses = self._live_worker_active_statuses()
        normalized_role = (
            self._normalize_live_assignment_role(role)
            if str(role or "").strip()
            else None
        )
        return sum(
            1
            for stream in self.live_streams
            if stream.claimed_by_worker_id == worker.id and stream.status in active_statuses
            and (normalized_role is None or self._live_runtime_role(stream) == normalized_role)
        )

    def _count_live_worker_streaming_streams(self, worker: WorkerRecord, *, role: str | None = None) -> int:
        normalized_role = (
            self._normalize_live_assignment_role(role)
            if str(role or "").strip()
            else None
        )
        return sum(
            1
            for stream in self.live_streams
            if stream.claimed_by_worker_id == worker.id
            and str(stream.status or "").strip().lower() == "streaming"
            and (normalized_role is None or self._live_runtime_role(stream) == normalized_role)
        )

    def _sync_live_worker_runtime_status(self, worker: WorkerRecord) -> None:
        worker.status = "busy" if self._count_live_worker_running_streams(worker) > 0 else "online"

    def _find_live_backup_clone_optional(self, stream: LiveStreamRecord) -> LiveStreamRecord | None:
        if self._is_runtime_backup_clone(stream):
            return None
        backup_stream_id = str(stream.backup_stream_id or "").strip()
        if backup_stream_id:
            candidate = self._find_live_stream_optional(backup_stream_id)
            if candidate and self._is_runtime_backup_clone(candidate) and candidate.parent_stream_id == stream.id:
                return candidate
        return next(
            (
                candidate
                for candidate in self.live_streams
                if self._is_runtime_backup_clone(candidate) and candidate.parent_stream_id == stream.id
            ),
            None,
        )

    def _parent_live_stream_optional(self, stream: LiveStreamRecord) -> LiveStreamRecord | None:
        parent_stream_id = str(stream.parent_stream_id or "").strip()
        if not parent_stream_id:
            return None
        return self._find_live_stream_optional(parent_stream_id)

    def _visible_live_stream_for_runtime(self, stream: LiveStreamRecord) -> LiveStreamRecord:
        if not self._is_runtime_backup_clone(stream):
            return stream
        parent_stream = self._parent_live_stream_optional(stream)
        return parent_stream or stream

    def _is_live_hot_standby_backup_clone(self, stream: LiveStreamRecord) -> bool:
        return bool(self._is_runtime_backup_clone(stream) and stream.is_forever and stream.end_time_live is None)

    def _live_stream_requires_fast_failover(self, stream: LiveStreamRecord) -> bool:
        visible_stream = self._visible_live_stream_for_runtime(stream)
        return bool(visible_stream.is_forever and visible_stream.backup_worker_id)

    def _live_stream_lease_seconds(self, stream: LiveStreamRecord) -> int:
        default_seconds = self._worker_job_lease_seconds()
        if not self._live_stream_requires_fast_failover(stream):
            return default_seconds
        return min(default_seconds, self._live_fast_failover_lease_seconds())

    def _can_reclaim_started_live_stream(self, stream: LiveStreamRecord, *, now: datetime) -> bool:
        normalized_status = str(stream.status or "").strip().lower() or "scheduled"
        if normalized_status != "disconnected":
            return False
        if not self._live_stream_requires_fast_failover(stream):
            return self._live_runtime_retry_ready(stream, now=now)
        if self._is_live_hot_standby_backup_clone(stream):
            return normalized_status in self._live_worker_claimable_statuses() and self._live_runtime_retry_ready(stream, now=now)
        visible_stream = self._visible_live_stream_for_runtime(stream)
        if (
            visible_stream.is_forever
            and visible_stream.backup_worker_id
            and self._has_live_stream_started(stream)
        ):
            return True
        return self._live_runtime_retry_ready(stream, now=now)

    @staticmethod
    def _reset_live_stream_attempt_timeline(stream: LiveStreamRecord) -> None:
        stream.download_started_at = None
        stream.prepared_at = None
        stream.waiting_started_at = None
        stream.streaming_started_at = None

    @staticmethod
    def _live_stream_has_reached_schedule_end(stream: LiveStreamRecord, *, now: datetime) -> bool:
        if stream.is_forever:
            return False
        if stream.end_time_live is None:
            return False
        return stream.end_time_live <= now

    def _finalize_live_stream_ended_state(
        self,
        stream: LiveStreamRecord,
        *,
        now: datetime,
        message: str | None = None,
    ) -> None:
        stream.status = "ended"
        stream.log_label = "Kết thúc"
        stream.status_message = (message or "").strip() or None
        stream.progress = 100
        stream.error_message = None
        stream.is_live_now = False
        stream.ended_at = now
        stream.updated_at = now
        stream.lease_expires_at = None
        stream.claimed_by_worker_id = None
        stream.claimed_by_role = None
        stream.claimed_at = None
        stream.disconnected_at = None

    @staticmethod
    def _live_stream_has_runtime_claim(stream: LiveStreamRecord) -> bool:
        return bool(str(stream.claimed_by_worker_id or "").strip())

    @staticmethod
    def _request_live_stream_stop(
        stream: LiveStreamRecord,
        *,
        now: datetime,
        message: str | None = None,
    ) -> None:
        if stream.stop_requested_at is None:
            stream.stop_requested_at = now
        stream.updated_at = now
        normalized_message = str(message or "").strip()
        if normalized_message and not str(stream.status_message or "").strip():
            stream.status_message = normalized_message

    def _mark_visible_live_stream_ended(
        self,
        stream: LiveStreamRecord,
        *,
        now: datetime,
        message: str | None = None,
    ) -> tuple[list[str], str]:
        self._finalize_live_stream_ended_state(stream, now=now, message=message)
        clone = self._find_live_backup_clone_optional(stream)
        if clone is not None:
            if self._live_stream_has_runtime_claim(clone):
                self._request_live_stream_stop(clone, now=now, message="Luồng chính đã kết thúc.")
            else:
                self._retire_live_backup_clone(clone, now=now, reason="Luồng chính đã kết thúc.")
        stream.backup_stream_id = None
        return self._live_stream_recipient_chat_ids(stream), self._live_stream_ended_message(stream, now=now)

    def _desired_live_playback_mode(self, stream: LiveStreamRecord, *, now: datetime) -> str:
        normalized_status = str(stream.status or "").strip().lower() or "scheduled"
        if normalized_status in {"stopped", "ended", "error"}:
            return "stop"
        if not self._is_live_hot_standby_backup_clone(stream):
            return "stream"
        parent_stream = self._parent_live_stream_optional(stream)
        if parent_stream is None:
            return "stop"
        parent_status = str(parent_stream.status or "").strip().lower() or "scheduled"
        if parent_status in {"stopped", "ended", "error"}:
            return "stop"
        start_time_live = parent_stream.start_time_live
        if start_time_live is not None and now < start_time_live:
            return "standby"
        if parent_status == "streaming":
            return "standby"
        if parent_status == "waiting":
            return "standby"
        return "stream"

    def _is_live_stream_runtime_locked(self, stream: LiveStreamRecord) -> bool:
        effective_runtime = self._effective_live_runtime_stream(stream)
        effective_status = str(effective_runtime.status or "").strip().lower() or "scheduled"
        if effective_status in {"streaming", "disconnected"}:
            return True
        primary_status = str(stream.status or "").strip().lower() or "scheduled"
        if primary_status in {"streaming", "disconnected"}:
            return True
        return False

    def _assert_live_stream_editable(self, stream: LiveStreamRecord) -> None:
        if self._is_live_stream_runtime_locked(stream):
            raise ValueError("Luồng đang được worker xử lý hoặc đang live. Hãy dùng Dừng trước khi chỉnh sửa.")

    def _is_live_stream_prestream_active(self, stream: LiveStreamRecord) -> bool:
        effective_runtime = self._effective_live_runtime_stream(stream)
        effective_status = str(effective_runtime.status or "").strip().lower() or "scheduled"
        if effective_status in self._live_pre_stream_statuses():
            return bool(str(effective_runtime.claimed_by_worker_id or "").strip()) or effective_status != "scheduled"
        primary_status = str(stream.status or "").strip().lower() or "scheduled"
        if primary_status in self._live_pre_stream_statuses():
            return bool(str(stream.claimed_by_worker_id or "").strip()) or primary_status != "scheduled"
        return False

    @staticmethod
    def _has_live_stream_started(stream: LiveStreamRecord) -> bool:
        return stream.first_streaming_started_at is not None or stream.streaming_started_at is not None

    def _live_runtime_role(self, stream: LiveStreamRecord) -> str:
        if self._is_runtime_backup_clone(stream):
            return "backup"
        return "backup" if str(stream.runtime_role or "").strip().lower() == "backup" else "primary"

    def _live_runtime_user_active_stream_count(
        self,
        *,
        owner_user_id: str,
        worker_id: str,
        role: str,
        now: datetime,
        excluding_stream_id: str | None = None,
    ) -> int:
        normalized_role = self._normalize_live_assignment_role(role)
        total = 0
        for stream in self.live_streams:
            if excluding_stream_id and stream.id == excluding_stream_id:
                continue
            if str(stream.owner_user_id or "").strip() != str(owner_user_id or "").strip():
                continue
            if str(stream.claimed_by_worker_id or "").strip() != str(worker_id or "").strip():
                continue
            if not self._live_runtime_stream_counts_toward_quota(stream, now=now):
                continue
            if self._live_runtime_role(stream) != normalized_role:
                continue
            total += 1
        return total

    def _live_runtime_stream_counts_toward_quota(self, stream: LiveStreamRecord, *, now: datetime) -> bool:
        normalized_status = str(stream.status or "").strip().lower()
        if normalized_status in self._live_worker_active_statuses():
            return True
        claimed_by_worker_id = str(stream.claimed_by_worker_id or "").strip()
        if not claimed_by_worker_id:
            return False
        return stream.lease_expires_at is not None and stream.lease_expires_at > now

    def _count_live_worker_reserved_streams(
        self,
        worker: WorkerRecord,
        *,
        now: datetime,
        role: str | None = None,
    ) -> int:
        normalized_role = (
            self._normalize_live_assignment_role(role)
            if str(role or "").strip()
            else None
        )
        return sum(
            1
            for stream in self.live_streams
            if str(stream.claimed_by_worker_id or "").strip() == worker.id
            and self._live_runtime_stream_counts_toward_quota(stream, now=now)
            and (normalized_role is None or self._live_runtime_role(stream) == normalized_role)
        )

    def _can_claim_live_stream(self, stream: LiveStreamRecord, *, worker_id: str, now: datetime) -> bool:
        normalized_status = str(stream.status or "").strip().lower() or "scheduled"
        if normalized_status not in self._live_worker_claimable_statuses():
            return False
        visible_stream = self._visible_live_stream_for_runtime(stream)
        if self._live_stream_has_reached_schedule_end(visible_stream, now=now):
            return False
        if normalized_status == "disconnected" and not self._live_runtime_retry_ready(stream, now=now):
            return False
        if self._has_live_stream_started(stream) and not self._can_reclaim_started_live_stream(stream, now=now):
            return False
        runtime_role = self._live_runtime_role(stream)
        allocated_threads = self._live_user_worker_allocated_threads(
            visible_stream.owner_user_id,
            worker_id,
            role=runtime_role,
        )
        if allocated_threads < 1:
            return False
        current_user_active_streams = self._live_runtime_user_active_stream_count(
            owner_user_id=visible_stream.owner_user_id,
            worker_id=worker_id,
            role=runtime_role,
            now=now,
            excluding_stream_id=stream.id,
        )
        if current_user_active_streams >= allocated_threads:
            return False
        claimed_by_worker_id = str(stream.claimed_by_worker_id or "").strip()
        if not claimed_by_worker_id:
            return True
        lease_is_active = stream.lease_expires_at is not None and stream.lease_expires_at > now
        if claimed_by_worker_id == worker_id:
            return not lease_is_active
        return stream.lease_expires_at is not None and stream.lease_expires_at <= now

    def _reset_live_backup_clone_runtime(self, stream: LiveStreamRecord, *, now: datetime) -> None:
        stream.status = "scheduled"
        stream.log_label = "Lên lịch"
        stream.status_message = None
        stream.progress = 0
        stream.error_message = None
        stream.is_live_now = False
        stream.claimed_by_worker_id = None
        stream.claimed_by_role = None
        stream.claimed_at = None
        stream.lease_expires_at = None
        self._reset_live_stream_attempt_timeline(stream)
        stream.first_streaming_started_at = None
        stream.disconnected_at = None
        stream.stop_requested_at = None
        stream.ended_at = None
        stream.updated_at = now

    def _upsert_live_backup_clone(self, stream: LiveStreamRecord, *, now: datetime) -> LiveStreamRecord | None:
        if self._is_runtime_backup_clone(stream):
            return None
        backup_worker_id = str(stream.backup_worker_id or "").strip()
        if not backup_worker_id:
            return None
        backup_worker = self._find_live_worker_optional(backup_worker_id)
        if backup_worker is None or backup_worker.id == stream.primary_worker_id:
            return None

        clone = self._find_live_backup_clone_optional(stream)
        clone_name = f"{stream.stream_name}_{'backup11h' if stream.end_time_live is not None else 'backup247'}"
        if clone is None:
            clone = LiveStreamRecord(
                id=f"live-{uuid4().hex[:10]}",
                owner_user_id=stream.owner_user_id,
                owner_username=stream.owner_username,
                owner_display_name=stream.owner_display_name,
                manager_id=stream.manager_id,
                manager_name=stream.manager_name,
                primary_worker_id=backup_worker.id,
                primary_worker_name=self._resolve_live_worker_display_name(backup_worker.id),
                primary_group=backup_worker.group or backup_worker.manager_name,
                backup_worker_id=None,
                backup_worker_name=None,
                backup_group=None,
                runtime_role="backup",
                is_runtime_clone=True,
                parent_stream_id=stream.id,
                backup_stream_id=None,
                stream_name=clone_name,
                stream_key=stream.stream_key,
                rtmp_url=self._backup_rtmp_url(),
                platform=stream.platform,
                video_url=stream.video_url,
                audio_url=stream.audio_url,
                live_label=stream.live_label,
                is_forever=stream.is_forever,
                is_live_now=False,
                start_time_live=stream.start_time_live,
                end_time_live=stream.end_time_live,
                backup_delay_minutes=stream.backup_delay_minutes,
                status="scheduled",
                log_label="Lên lịch",
                created_at=now,
                updated_at=now,
            )
            self.live_streams.append(clone)
        else:
            clone.owner_user_id = stream.owner_user_id
            clone.owner_username = stream.owner_username
            clone.owner_display_name = stream.owner_display_name
            clone.manager_id = stream.manager_id
            clone.manager_name = stream.manager_name
            clone.primary_worker_id = backup_worker.id
            clone.primary_worker_name = self._resolve_live_worker_display_name(backup_worker.id)
            clone.primary_group = backup_worker.group or backup_worker.manager_name
            clone.backup_worker_id = None
            clone.backup_worker_name = None
            clone.backup_group = None
            clone.runtime_role = "backup"
            clone.is_runtime_clone = True
            clone.parent_stream_id = stream.id
            clone.backup_stream_id = None
            clone.stream_name = clone_name
            clone.stream_key = stream.stream_key
            clone.rtmp_url = self._backup_rtmp_url()
            clone.platform = stream.platform
            clone.video_url = stream.video_url
            clone.audio_url = stream.audio_url
            clone.live_label = stream.live_label
            clone.is_forever = stream.is_forever
            clone.start_time_live = stream.start_time_live
            clone.end_time_live = stream.end_time_live
            clone.backup_delay_minutes = stream.backup_delay_minutes
            clone_status = str(clone.status or "").strip().lower()
            if clone.claimed_by_worker_id is None and clone_status in {"draft", "scheduled", "disconnected", "ended", "stopped"}:
                self._reset_live_backup_clone_runtime(clone, now=now)
            elif clone.claimed_by_worker_id is None and clone_status == "error":
                if self._live_runtime_retry_ready(clone, now=now):
                    self._reset_live_backup_clone_runtime(clone, now=now)
            else:
                clone.updated_at = now

        stream.backup_stream_id = clone.id
        stream.updated_at = now
        return clone

    def _retire_live_backup_clone(
        self,
        clone: LiveStreamRecord,
        *,
        now: datetime,
        reason: str | None = None,
    ) -> None:
        if clone.status == "stopped" and clone.claimed_by_worker_id is None:
            return
        clone.status = "stopped"
        clone.log_label = "Đã dừng"
        clone.status_message = None
        clone.error_message = (reason or "").strip() or None
        clone.is_live_now = False
        clone.progress = min(int(clone.progress or 0), 100)
        clone.stop_requested_at = now
        clone.lease_expires_at = None
        clone.claimed_by_worker_id = None
        clone.claimed_by_role = None
        clone.updated_at = now

    def _sync_live_backup_policy(self, *, now: datetime) -> None:
        visible_streams = [stream for stream in self.live_streams if self._is_visible_live_stream(stream)]
        for stream in visible_streams:
            clone = self._find_live_backup_clone_optional(stream)
            if self._live_stream_has_reached_schedule_end(stream, now=now):
                stream_has_claim = self._live_stream_has_runtime_claim(stream)
                clone_has_claim = clone is not None and self._live_stream_has_runtime_claim(clone)
                if stream_has_claim or clone_has_claim:
                    if stream_has_claim:
                        self._request_live_stream_stop(stream, now=now, message="Luồng live đã kết thúc theo lịch.")
                    if clone is not None:
                        if clone_has_claim:
                            self._request_live_stream_stop(clone, now=now, message="Luồng live đã kết thúc theo lịch.")
                        else:
                            self._retire_live_backup_clone(clone, now=now, reason="Luồng chính đã kết thúc.")
                            if stream.backup_stream_id is not None:
                                stream.backup_stream_id = None
                    continue
                self._finalize_live_stream_ended_state(
                    stream,
                    now=now,
                    message=stream.status_message or "Luồng live đã kết thúc theo lịch.",
                )
                if clone is not None:
                    self._retire_live_backup_clone(clone, now=now, reason="Luồng chính đã kết thúc.")
                if stream.backup_stream_id is not None:
                    stream.backup_stream_id = None
                continue
            should_have_backup = bool(stream.backup_worker_id) and stream.status not in {"stopped", "ended", "error"}
            if not should_have_backup:
                if clone is not None:
                    if self._live_stream_has_runtime_claim(clone):
                        self._request_live_stream_stop(clone, now=now, message="Luồng chính không còn dùng BOT backup.")
                    else:
                        self._retire_live_backup_clone(clone, now=now, reason="Luồng chính không còn dùng BOT backup.")
                if stream.backup_stream_id is not None and (clone is None or not self._live_stream_has_runtime_claim(clone)):
                    stream.backup_stream_id = None
                    stream.updated_at = now
                continue

            clone = self._upsert_live_backup_clone(stream, now=now)
            if clone is not None and stream.backup_stream_id != clone.id:
                stream.backup_stream_id = clone.id
                stream.updated_at = now

    def _refresh_live_stream_leases(
        self,
        worker_id: str,
        now: datetime | None = None,
        *,
        stream_ids: list[str] | None = None,
    ) -> None:
        lease_base = now or self._now(trim=False)
        allowed_stream_ids = {str(stream_id).strip() for stream_id in (stream_ids or []) if str(stream_id).strip()}
        lease_refresh_statuses = self._live_worker_lease_refresh_statuses()
        for stream in self.live_streams:
            if stream.claimed_by_worker_id != worker_id or stream.status not in lease_refresh_statuses:
                continue
            if allowed_stream_ids and stream.id not in allowed_stream_ids:
                continue
            lease_duration = timedelta(seconds=self._live_stream_lease_seconds(stream))
            stream.lease_expires_at = lease_base + lease_duration

    def _handle_interrupted_live_stream(
        self,
        stream: LiveStreamRecord,
        *,
        now: datetime,
        reason: str,
    ) -> tuple[list[str], str] | None:
        should_notify_disconnect = self._should_notify_live_disconnect(stream, now=now)
        if self._is_runtime_backup_clone(stream):
            stream.status = "error"
            stream.log_label = "Lỗi"
            stream.error_message = reason
            notification_payload = None
        else:
            stream.status = "disconnected"
            stream.log_label = "Mất kết nối"
            stream.error_message = None
            stream.disconnected_at = now
            notification_payload = (
                (
                    self._live_stream_recipient_chat_ids(stream),
                    self._live_stream_disconnected_message(stream, now=now, reason=reason),
                )
                if should_notify_disconnect
                else None
            )
        stream.status_message = None
        stream.is_live_now = False
        stream.claimed_by_worker_id = None
        stream.claimed_by_role = None
        stream.claimed_at = None
        stream.lease_expires_at = None
        stream.updated_at = now
        return notification_payload

    def _reconcile_live_streams_from_heartbeat(
        self,
        worker: WorkerRecord,
        *,
        active_stream_ids: list[str],
        now: datetime,
    ) -> None:
        active_stream_id_set = {str(stream_id).strip() for stream_id in active_stream_ids if str(stream_id).strip()}
        for stream in self.live_streams:
            if stream.claimed_by_worker_id != worker.id:
                continue
            if stream.id in active_stream_id_set:
                lease_duration = timedelta(seconds=self._live_stream_lease_seconds(stream))
                stream.lease_expires_at = now + lease_duration
                continue
            if str(stream.status or "").strip().lower() not in self._live_worker_active_statuses():
                continue
            if stream.lease_expires_at is not None and stream.lease_expires_at > now:
                continue
            notification_payload = self._handle_interrupted_live_stream(
                stream,
                now=now,
                reason="Worker không còn báo luồng live này trong heartbeat sau khi đã qua grace window.",
            )
            if notification_payload is not None:
                self._notify_live_telegram_chat_ids(notification_payload[0], notification_payload[1])

    def register_live_worker(self, payload: LiveWorkerRegisterPayload) -> LiveWorkerControlResponse:
        completed_task: dict[str, Any] | None = None
        worker_snapshot: WorkerRecord | None = None
        reconnect_notification: tuple[list[str], str] | None = None
        vps_ip = ""
        with self._worker_state_lock:
            if payload.shared_secret != self.get_worker_shared_secret():
                raise ValueError("Worker shared secret không hợp lệ.")
            self._ensure_worker_not_deleted(payload.worker_id)
            if self._has_pending_install_for_other_workspace(payload.worker_id, workspace_mode="live"):
                raise ValueError("BOT này đang được chuyển sang workspace upload. Worker live hiện tại sẽ tự khởi động lại.")
            now = self._now(trim=False)
            existing = next((worker for worker in self.live_workers if worker.id == payload.worker_id), None)
            worker_display_name = self._default_worker_display_name(payload.worker_id, payload.name)
            install_task = next(
                (
                    task
                    for task in self.worker_operation_tasks
                    if str(task.get("kind") or "").strip() == "install"
                    and str(task.get("worker_id") or "").strip() == payload.worker_id
                    and self._restore_worker_operation_workspace_mode(task) == "live"
                ),
                None,
            )
            install_task_manager_id = str(install_task.get("manager_id") or "").strip() if install_task else ""
            install_task_manager_name = str(install_task.get("manager_name") or "").strip() if install_task else ""
            install_task_group = str(install_task.get("group") or "").strip() if install_task else ""
            install_task_config = self._pending_install_config(install_task)
            install_task_local = str((install_task_config or {}).get("local") or "").strip()
            manager = next(
                (
                    user
                    for user in self.users
                    if user.role == "manager" and user.id == install_task_manager_id
                ),
                None,
            )
            if manager is None:
                manager = next(
                    (
                        user
                        for user in self.users
                        if user.role == "manager" and user.username == str(payload.manager_name or "").strip()
                    ),
                    None,
                )
            resolved_manager_name = (
                manager.username if manager else (install_task_manager_name or str(payload.manager_name or "").strip() or "system")
            )
            resolved_group = self._normalized_bot_group(
                install_task_group or str(payload.group or "").strip()
            )
            normalized_capacity = self._normalize_live_worker_threads(
                (
                    existing.capacity
                    if existing is not None and int(existing.capacity or 0) > 0
                    else payload.capacity
                )
                or self._fixed_live_worker_thread_limit()
            )
            if existing is None:
                worker = WorkerRecord(
                    id=payload.worker_id,
                    name=worker_display_name,
                    local=install_task_local or None,
                    manager_id=manager.id if manager else None,
                    manager_name=resolved_manager_name,
                    group=resolved_group,
                    created_at=now,
                    status="online",
                    capacity=normalized_capacity,
                    load_percent=0,
                    ram_percent=0,
                    ram_used_gb=0.0,
                    ram_total_gb=0.0,
                    bandwidth_kbps=0.0,
                    disk_used_gb=0.0,
                    disk_total_gb=float(payload.disk_total_gb or 0),
                    threads=normalized_capacity,
                    last_seen_at=now,
                )
                self.live_workers.append(worker)
            else:
                reconnect_notification = self._worker_reconnect_notification(existing, now=now, live=True)
                existing.name = worker_display_name
                if install_task_local:
                    existing.local = install_task_local
                existing.manager_id = manager.id if manager else existing.manager_id
                existing.manager_name = resolved_manager_name
                existing.group = resolved_group
                existing.created_at = existing.created_at or now
                existing.capacity = normalized_capacity
                existing.threads = normalized_capacity
                existing.disk_total_gb = float(payload.disk_total_gb or existing.disk_total_gb or 0)
                existing.status = "online"
                existing.last_seen_at = now
                existing.offline_since_at = None
                existing.offline_alert_sent_at = None
                worker = existing
            if self._looks_like_ipv4(worker_display_name):
                existing_profile = self.worker_connection_profiles.get(payload.worker_id) or {}
                self._remember_worker_connection_profile(
                    payload.worker_id,
                    vps_ip=worker_display_name,
                    ssh_user=str(existing_profile.get("ssh_user") or "root"),
                )
            existing_registration_meta = dict(self.worker_registration_meta.get(str(payload.worker_id or "").strip()) or {})
            registration_source = self._worker_registration_source_for_task(install_task)
            if not registration_source:
                registration_source = str(existing_registration_meta.get("source") or "").strip()
            if not registration_source:
                registration_source = "manual_register" if existing is None else "register"
            self._remember_worker_registration_meta(
                payload.worker_id,
                workspace_mode="live",
                source=registration_source,
                task=install_task,
                manager_id=manager.id if manager else None,
                manager_name=resolved_manager_name,
            )
            if install_task is None and existing is None:
                logger.warning(
                    "worker_registered_without_install_task workspace=live worker_id=%s worker_name=%s manager_name=%s",
                    str(payload.worker_id or "").strip(),
                    worker_display_name,
                    resolved_manager_name,
                )
            completed_task = deepcopy(install_task) if install_task is not None else None
            if install_task is not None:
                self._finalize_workspace_conversion_source(install_task)
            vps_ip = str(payload.name or "").strip() or worker_display_name
            self.worker_operation_tasks = [
                task
                for task in self.worker_operation_tasks
                if not (
                    str(task.get("kind") or "").strip() == "install"
                    and str(task.get("worker_id") or "").strip() == payload.worker_id
                    and self._restore_worker_operation_workspace_mode(task) == "live"
                )
            ]
            self._save_state()
            worker_snapshot = deepcopy(worker)
        if completed_task is not None and worker_snapshot is not None:
            self._resume_worker_operation_queue()
            config_error: str | None = None
            try:
                self._apply_pending_install_config(completed_task, worker_id=worker_snapshot.id, workspace_mode="live")
                with self._worker_state_lock:
                    worker_snapshot = deepcopy(self._find_live_worker(worker_snapshot.id))
            except (KeyError, ValueError) as exc:
                config_error = str(exc)
                self._push_admin_notification(
                    message=f"BOT live {worker_snapshot.name} đã cài xong nhưng không thể áp cấu hình ban đầu: {config_error}",
                    level="warning",
                    manager_id=str(completed_task.get("manager_id") or "").strip() or None,
                )
            self._notify_telegram_chat_ids(
                self._bot_operation_recipient_chat_ids(completed_task),
                self._bot_install_result_message(
                    completed_task,
                    worker_name=self._resolve_live_worker_display_name(worker_snapshot.id),
                    worker_id=worker_snapshot.id,
                    vps_ip=vps_ip or self._resolve_live_worker_display_name(worker_snapshot.id),
                ) + (
                    f"\nLưu ý cấu hình ban đầu: {config_error}"
                    if config_error
                    else ""
                ),
            )
        if reconnect_notification is not None:
            self._notify_telegram_chat_ids(reconnect_notification[0], reconnect_notification[1])
        return LiveWorkerControlResponse(ok=True, worker=worker_snapshot)

    def heartbeat_live_worker(self, payload: LiveWorkerHeartbeatPayload) -> LiveWorkerControlResponse:
        reconnect_notification: tuple[list[str], str] | None = None
        with self._worker_state_lock:
            worker = self._authenticate_live_worker(payload.worker_id, payload.shared_secret)
            now = self._now(trim=False)
            reconnect_notification = self._worker_reconnect_notification(worker, now=now, live=True)
            worker.load_percent = payload.load_percent
            worker.ram_percent = payload.ram_percent
            worker.ram_used_gb = payload.ram_used_gb
            worker.ram_total_gb = payload.ram_total_gb or worker.ram_total_gb
            worker.bandwidth_kbps = payload.bandwidth_kbps
            worker.disk_used_gb = payload.disk_used_gb
            worker.disk_total_gb = payload.disk_total_gb or worker.disk_total_gb
            normalized_capacity = self._normalize_live_worker_threads(
                worker.capacity or payload.capacity or self._fixed_live_worker_thread_limit()
            )
            worker.threads = normalized_capacity
            worker.capacity = normalized_capacity
            worker.last_seen_at = now
            worker.offline_since_at = None
            worker.offline_alert_sent_at = None
            self._reconcile_live_streams_from_heartbeat(
                worker,
                active_stream_ids=payload.active_stream_ids or [],
                now=now,
            )
            self._sync_live_backup_policy(now=now)
            if self._count_live_worker_running_streams(worker) > 0:
                worker.status = "busy"
            else:
                worker.status = payload.status
            self._save_state()
            snapshot = deepcopy(worker)
        if reconnect_notification is not None:
            self._notify_telegram_chat_ids(reconnect_notification[0], reconnect_notification[1])
        return LiveWorkerControlResponse(ok=True, worker=snapshot)

    @staticmethod
    def _live_stream_claim_sort_key(stream: LiveStreamRecord) -> tuple[int, datetime, datetime, str]:
        priority_map = {
            "streaming": 0,
            "waiting": 0,
            "preparing": 0,
            "downloading": 0,
            "disconnected": 1,
            "scheduled": 2,
            "draft": 3,
        }
        active_priority = priority_map.get(str(stream.status or "").strip().lower(), 4)
        start_at = stream.start_time_live or datetime.max
        created_at = stream.created_at or datetime.min
        return (active_priority, start_at, created_at, stream.id)

    def claim_next_live_stream(self, worker_id: str, shared_secret: str) -> tuple[WorkerRecord, LiveStreamRecord | None]:
        with self._worker_state_lock:
            worker = self._authenticate_live_worker(worker_id, shared_secret)
            now = self._now(trim=False)
            self._sync_live_backup_policy(now=now)
            max_threads = self._effective_live_worker_thread_limit(worker)
            if self._count_live_worker_reserved_streams(worker, now=now) >= max_threads:
                self._sync_live_worker_runtime_status(worker)
                self._save_state()
                return deepcopy(worker), None

            candidates = [
                stream
                for stream in self.live_streams
                if stream.primary_worker_id == worker.id
                and self._can_claim_live_stream(stream, worker_id=worker_id, now=now)
            ]
            if not candidates:
                self._sync_live_worker_runtime_status(worker)
                self._save_state()
                return deepcopy(worker), None

            stream = sorted(candidates, key=self._live_stream_claim_sort_key)[0]
            if self._has_live_stream_started(stream):
                self._reset_live_stream_attempt_timeline(stream)
            stream.claimed_by_worker_id = worker.id
            stream.claimed_by_role = stream.runtime_role or "primary"
            stream.claimed_at = now
            stream.lease_expires_at = now + timedelta(seconds=self._live_stream_lease_seconds(stream))
            stream.updated_at = now
            self._sync_live_worker_runtime_status(worker)
            self._save_state()
            return deepcopy(worker), deepcopy(stream)

    def _find_claimed_live_stream(self, stream_id: str, worker_id: str) -> LiveStreamRecord:
        stream = self._find_live_stream(stream_id)
        if stream.claimed_by_worker_id != worker_id:
            raise ValueError("Luồng live không còn thuộc worker hiện tại.")
        return stream

    @staticmethod
    def _ensure_live_stream_can_continue(stream: LiveStreamRecord) -> None:
        if stream.status == "stopped":
            raise ValueError("Luồng live đã bị dừng trên control plane.")
        if stream.status == "ended":
            raise ValueError("Luồng live đã kết thúc trên control plane.")
        if stream.status == "error":
            raise ValueError("Luồng live đã dừng với trạng thái lỗi trên control plane.")

    @staticmethod
    def _live_runtime_log_label(status: str) -> str:
        normalized = str(status or "").strip().lower()
        if normalized == "downloading":
            return "Đang tải media"
        if normalized == "preparing":
            return "Chuẩn bị luồng"
        if normalized == "waiting":
            return "Chờ đến giờ"
        if normalized == "streaming":
            return "Đang live"
        if normalized == "ended":
            return "Kết thúc"
        if normalized == "stopped":
            return "Đã dừng"
        if normalized == "error":
            return "Lỗi"
        if normalized == "scheduled":
            return "Lên lịch"
        if normalized == "disconnected":
            return "Mất kết nối"
        return "Khởi tạo"

    def update_live_stream_progress(
        self,
        *,
        stream_id: str,
        worker_id: str,
        shared_secret: str,
        status: str,
        progress: int,
        message: str | None = None,
    ) -> LiveStreamRecord:
        notification_chat_ids: list[str] = []
        notification_message = ""
        with self._worker_state_lock:
            worker = self._authenticate_live_worker(worker_id, shared_secret)
            stream = self._find_claimed_live_stream(stream_id, worker_id)
            self._ensure_live_stream_can_continue(stream)
            now = self._now(trim=False)
            previous_status = str(stream.status or "").strip().lower() or "scheduled"

            normalized_status = str(status or "").strip().lower() or "scheduled"
            bounded_progress = max(0, min(100, int(progress)))
            stream.claimed_by_worker_id = worker_id
            stream.claimed_by_role = stream.claimed_by_role or stream.runtime_role or "primary"
            stream.claimed_at = stream.claimed_at or now
            stream.status = normalized_status
            stream.progress = bounded_progress
            stream.status_message = (message or "").strip() or None
            stream.log_label = self._live_runtime_log_label(normalized_status)
            stream.error_message = None
            stream.updated_at = now

            if normalized_status == "downloading" and stream.download_started_at is None:
                stream.download_started_at = now
                stream.is_live_now = False
            elif normalized_status == "preparing":
                if stream.download_started_at is None:
                    stream.download_started_at = now
                stream.is_live_now = False
            elif normalized_status == "waiting":
                stream.prepared_at = stream.prepared_at or now
                if previous_status == "streaming":
                    stream.waiting_started_at = now
                    stream.streaming_started_at = None
                else:
                    stream.waiting_started_at = stream.waiting_started_at or now
                stream.is_live_now = False
            elif normalized_status == "streaming":
                stream.prepared_at = stream.prepared_at or now
                first_streaming_transition = not self._has_live_stream_started(stream)
                was_streaming = previous_status == "streaming"
                stream.first_streaming_started_at = stream.first_streaming_started_at or now
                stream.streaming_started_at = now if not was_streaming else (stream.streaming_started_at or now)
                stream.is_live_now = True
                stream.disconnected_at = None
                if not was_streaming:
                    notification_chat_ids = self._live_stream_recipient_chat_ids(stream)
                    if self._is_runtime_backup_clone(stream):
                        notification_message = self._live_backup_activated_message(stream, now=now)
                    elif self._is_visible_live_stream(stream) and first_streaming_transition:
                        notification_message = self._live_stream_started_message(stream, now=now)
            else:
                stream.is_live_now = normalized_status == "streaming"

            if self._is_visible_live_stream(stream) and normalized_status == "streaming":
                stream.disconnected_at = None

            self._refresh_live_stream_leases(worker_id, now, stream_ids=[stream.id])
            self._sync_live_backup_policy(now=now)
            self._sync_live_worker_runtime_status(worker)
            self._save_state()
            snapshot = deepcopy(stream)
        if notification_message and notification_chat_ids:
            self._notify_live_telegram_chat_ids(notification_chat_ids, notification_message)
        return snapshot

    def get_live_stream_runtime_state(
        self,
        *,
        stream_id: str,
        worker_id: str,
        shared_secret: str,
    ) -> dict[str, Any]:
        with self._worker_state_lock:
            self._authenticate_live_worker(worker_id, shared_secret)
            stream = self._find_claimed_live_stream(stream_id, worker_id)
            normalized_status = str(stream.status or "").strip().lower() or "scheduled"
            now = self._now(trim=False)
            self._refresh_live_stream_leases(worker_id, now, stream_ids=[stream.id])
            playback_mode = self._desired_live_playback_mode(stream, now=now)
            explicit_stop_requested = stream.stop_requested_at is not None and normalized_status not in {"stopped", "ended", "error"}
            visible_stream = self._visible_live_stream_for_runtime(stream)
            schedule_end_reached = self._live_stream_has_reached_schedule_end(visible_stream, now=now)
            should_stop = (
                normalized_status in {"stopped", "ended", "error"}
                or playback_mode == "stop"
                or explicit_stop_requested
                or schedule_end_reached
            )
            return {
                "stream_id": stream.id,
                "status": normalized_status,
                "should_stop": should_stop,
                "playback_mode": playback_mode,
                "stop_requested_at": stream.stop_requested_at.isoformat() if stream.stop_requested_at else None,
                "ended_at": stream.ended_at.isoformat() if stream.ended_at else None,
                "updated_at": stream.updated_at.isoformat() if stream.updated_at else None,
            }

    def complete_live_stream_runtime(
        self,
        *,
        stream_id: str,
        worker_id: str,
        shared_secret: str,
        message: str | None = None,
    ) -> LiveStreamRecord:
        notification_chat_ids: list[str] = []
        notification_message = ""
        with self._worker_state_lock:
            worker = self._authenticate_live_worker(worker_id, shared_secret)
            stream = self._find_claimed_live_stream(stream_id, worker_id)
            self._ensure_live_stream_can_continue(stream)
            now = self._now(trim=False)
            if self._is_visible_live_stream(stream):
                notification_chat_ids, notification_message = self._mark_visible_live_stream_ended(
                    stream,
                    now=now,
                    message=message,
                )
            else:
                self._finalize_live_stream_ended_state(stream, now=now, message=message)
                parent_stream = self._parent_live_stream_optional(stream)
                if (
                    parent_stream is not None
                    and str(parent_stream.status or "").strip().lower() not in {"stopped", "ended", "error"}
                    and self._live_stream_has_reached_schedule_end(parent_stream, now=now)
                ):
                    if self._live_stream_has_runtime_claim(parent_stream):
                        self._request_live_stream_stop(parent_stream, now=now, message=message or "Luồng live đã kết thúc theo lịch.")
                    else:
                        notification_chat_ids, notification_message = self._mark_visible_live_stream_ended(
                            parent_stream,
                            now=now,
                            message=message,
                        )
            self._sync_live_worker_runtime_status(worker)
            self._save_state()
            snapshot = deepcopy(stream)
        if notification_message and notification_chat_ids:
            self._notify_live_telegram_chat_ids(notification_chat_ids, notification_message)
        return snapshot

    def fail_live_stream_runtime(
        self,
        *,
        stream_id: str,
        worker_id: str,
        shared_secret: str,
        message: str,
    ) -> LiveStreamRecord:
        notification_chat_ids: list[str] = []
        notification_message = ""
        fallback_notification_chat_ids: list[str] = []
        with self._worker_state_lock:
            worker = self._authenticate_live_worker(worker_id, shared_secret)
            stream = self._find_claimed_live_stream(stream_id, worker_id)
            now = self._now(trim=False)
            should_notify_disconnect = self._should_notify_live_disconnect(stream, now=now)
            if stream.status == "stopped":
                stream.status_message = None
                stream.lease_expires_at = None
                stream.claimed_by_worker_id = None
                stream.claimed_by_role = None
                stream.updated_at = now
                self._sync_live_worker_runtime_status(worker)
                self._save_state()
                return deepcopy(stream)

            if self._is_visible_live_stream(stream) and stream.backup_worker_id:
                stream.status = "disconnected"
                stream.log_label = "Mất kết nối"
                stream.disconnected_at = now
                if should_notify_disconnect:
                    notification_chat_ids = self._live_stream_recipient_chat_ids(stream)
                    fallback_notification_chat_ids = self._live_stream_fallback_alert_chat_ids(stream)
                    notification_message = self._live_stream_disconnected_message(stream, now=now, reason=message)
            else:
                stream.status = "error"
                stream.log_label = self._live_stream_error_log_label(message)
                if self._is_visible_live_stream(stream):
                    notification_chat_ids = self._live_stream_recipient_chat_ids(stream)
                    fallback_notification_chat_ids = self._live_stream_fallback_alert_chat_ids(stream)
                    notification_message = self._live_stream_error_message(stream, error_message=message)
            stream.status_message = None
            stream.error_message = message
            stream.is_live_now = False
            stream.updated_at = now
            stream.lease_expires_at = None
            stream.claimed_by_worker_id = None
            stream.claimed_by_role = None
            stream.claimed_at = None
            if self._is_visible_live_stream(stream):
                if stream.status == "error":
                    clone = self._find_live_backup_clone_optional(stream)
                    if clone is not None:
                        self._retire_live_backup_clone(clone, now=now, reason="Luồng chính gặp lỗi.")
                        stream.backup_stream_id = None
                else:
                    self._sync_live_backup_policy(now=now)
            self._sync_live_worker_runtime_status(worker)
            self._save_state()
            snapshot = deepcopy(stream)
        if notification_message:
            delivered = self._notify_live_telegram_chat_ids(notification_chat_ids, notification_message)
            if not delivered and fallback_notification_chat_ids:
                self._notify_telegram_chat_ids(fallback_notification_chat_ids, notification_message)
        return snapshot

    def _find_channel(self, channel_id: str) -> ChannelRecord:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        raise KeyError(channel_id)

    def _require_workspace_user(self, user_id: str) -> UserSummary:
        user = self._find_user(user_id)
        if user.role not in {"user", "manager", "admin"}:
            raise ValueError("Tài khoản không được phép vào workspace user.")
        return user

    def _workspace_workers_for_user(self, user: UserSummary | str) -> list[WorkerRecord]:
        current_user = self._require_workspace_user(user if isinstance(user, str) else user.id)
        workers = self._assigned_workers_for_user(current_user.id)
        return sorted(
            workers,
            key=lambda worker: (
                str(self._resolve_worker_display_name(worker.id) or "").casefold(),
                str(worker.id or "").casefold(),
            ),
        )

    def _workspace_worker_ids_for_user(self, user: UserSummary | str) -> set[str]:
        return {worker.id for worker in self._workspace_workers_for_user(user)}

    def _workspace_live_workers_for_user(self, user: UserSummary | str) -> list[WorkerRecord]:
        current_user = self._require_workspace_user(user if isinstance(user, str) else user.id)
        workers = self._assigned_live_workers_for_user(current_user.id)
        return sorted(
            workers,
            key=lambda worker: (
                str(self._resolve_live_worker_display_name(worker.id) or "").casefold(),
                str(worker.id or "").casefold(),
            ),
        )

    def _workspace_live_worker_ids_for_user(self, user: UserSummary | str) -> set[str]:
        return {worker.id for worker in self._workspace_live_workers_for_user(user)}

    def _workspace_channels_for_user(self, user: UserSummary | str) -> list[ChannelRecord]:
        current_user = self._require_workspace_user(user if isinstance(user, str) else user.id)
        worker_ids = self._workspace_worker_ids_for_user(current_user)
        linked_channel_ids = self._user_channel_ids(current_user.id)
        channels = [channel for channel in self.channels if channel.id in linked_channel_ids]
        if worker_ids:
            channels = [channel for channel in channels if channel.worker_id in worker_ids]
        return channels

    def _user_channel_ids(self, user_id: str) -> set[str]:
        return {link["channel_id"] for link in self.channel_user_links if link["user_id"] == user_id}

    def _user_has_channel_access(self, user_id: str, channel_id: str) -> bool:
        user = self._require_workspace_user(user_id)
        return any(channel.id == channel_id for channel in self._workspace_channels_for_user(user))

    def _user_jobs_for_workspace(self, user_id: str) -> list[RenderJobRecord]:
        user = self._require_workspace_user(user_id)
        channel_ids = {channel.id for channel in self._workspace_channels_for_user(user)}
        jobs = [job for job in self.jobs if job.channel_id in channel_ids]
        assigned_workers = self._workspace_workers_for_user(user)
        if assigned_workers:
            allowed_worker_aliases = {
                alias
                for worker in assigned_workers
                for alias in {str(worker.id or "").strip(), str(worker.name or "").strip()}
                if alias
            }
            jobs = [job for job in jobs if (job.worker_name or "") in allowed_worker_aliases]
        return sorted(deepcopy(jobs), key=lambda item: item.created_at, reverse=True)

    def _find_user_visible_job(self, user_id: str, job_id: str) -> RenderJobRecord:
        job = self._find_job(job_id)
        if not self._user_has_channel_access(user_id, job.channel_id):
            raise KeyError(job_id)
        return job

    def _assigned_worker_link_for_user(self, user_id: str) -> dict[str, Any] | None:
        links = self._assigned_worker_links_for_user(user_id)
        if not links:
            return None
        return links[0]

    def _assigned_worker_links_for_user(self, user_id: str) -> list[dict[str, Any]]:
        links = [link for link in self.user_worker_links if str(link.get("user_id") or "").strip() == str(user_id or "").strip()]
        links.sort(key=lambda item: self._mapping_numeric_id(item.get("id")))
        return links

    def _assigned_live_worker_links_for_user(
        self,
        user_id: str,
        *,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_role = self._normalize_live_assignment_role(role) if role is not None else None
        links = [
            link
            for link in self.live_user_worker_links
            if str(link.get("user_id") or "").strip() == str(user_id or "").strip()
            and (
                normalized_role is None
                or self._normalize_live_assignment_role(link.get("live_role"), fallback_note=link.get("note")) == normalized_role
            )
        ]
        links.sort(key=lambda item: self._mapping_numeric_id(item.get("id")))
        return links

    def _live_user_worker_allocated_threads(self, user_id: str, worker_id: str, *, role: str | None = None) -> int:
        normalized_worker_id = str(worker_id or "").strip()
        normalized_role = self._normalize_live_assignment_role(role) if role is not None else None
        total = 0
        for link in self.live_user_worker_links:
            if str(link.get("user_id") or "").strip() != str(user_id or "").strip():
                continue
            if str(link.get("worker_id") or "").strip() != normalized_worker_id:
                continue
            if normalized_role is not None and self._normalize_live_assignment_role(
                link.get("live_role"),
                fallback_note=link.get("note"),
            ) != normalized_role:
                continue
            total += self._live_link_allocated_threads(link)
        return total

    def _assigned_workers_for_user(self, user: UserSummary | str) -> list[WorkerRecord]:
        user_id = user.id if isinstance(user, UserSummary) else user
        workers: list[WorkerRecord] = []
        seen_worker_ids: set[str] = set()
        for link in self._assigned_worker_links_for_user(user_id):
            worker_id = str(link.get("worker_id") or "").strip()
            if not worker_id or worker_id in seen_worker_ids:
                continue
            try:
                worker = self._find_worker(worker_id)
            except KeyError:
                continue
            workers.append(worker)
            seen_worker_ids.add(worker_id)
        return workers

    def _assigned_live_workers_for_user(
        self,
        user: UserSummary | str,
        *,
        role: str | None = None,
    ) -> list[WorkerRecord]:
        user_id = user.id if isinstance(user, UserSummary) else user
        workers: list[WorkerRecord] = []
        seen_worker_ids: set[str] = set()
        for link in self._assigned_live_worker_links_for_user(user_id, role=role):
            worker_id = str(link.get("worker_id") or "").strip()
            if not worker_id or worker_id in seen_worker_ids:
                continue
            try:
                worker = self._find_live_worker(worker_id)
            except KeyError:
                continue
            workers.append(worker)
            seen_worker_ids.add(worker_id)
        return workers

    def _assigned_worker_for_user(self, user: UserSummary | str) -> WorkerRecord | None:
        workers = self._assigned_workers_for_user(user)
        if not workers:
            return None
        return workers[0]

    def _assigned_live_worker_for_user(self, user: UserSummary | str, *, role: str | None = None) -> WorkerRecord | None:
        workers = self._assigned_live_workers_for_user(user, role=role)
        if not workers:
            return None
        return workers[0]

    def _pick_worker_for_user(self, user: UserSummary, worker_id: str | None = None) -> WorkerRecord:
        workers = self._workspace_workers_for_user(user)
        if worker_id:
            normalized_worker_id = str(worker_id).strip()
            worker = next((item for item in workers if item.id == normalized_worker_id), None)
            if worker is not None:
                return worker
            raise ValueError("VPS được chọn không nằm trong danh sách đã cấp cho user.")
        worker = workers[0] if workers else None
        if worker is not None:
            return worker
        raise ValueError("User này chưa được cấp VPS render/upload.")

    def _channel_matches_user_worker(self, user: UserSummary, channel: ChannelRecord) -> bool:
        assigned_worker_ids = {worker.id for worker in self._workspace_workers_for_user(user)}
        if not assigned_worker_ids:
            return False
        return channel.worker_id in assigned_worker_ids

    def _assert_channel_matches_user_worker(self, user: UserSummary, channel: ChannelRecord) -> WorkerRecord:
        assigned_workers = self._workspace_workers_for_user(user)
        if not assigned_workers:
            raise ValueError("User này chưa được cấp VPS render/upload.")
        worker = next((item for item in assigned_workers if item.id == channel.worker_id), None)
        if worker is not None:
            return worker
        assigned_label = ", ".join(self._resolve_worker_display_name(worker.id) for worker in assigned_workers)
        channel_label = self._resolve_channel_worker_display_name(channel)
        raise ValueError(
            f"Kênh này đang gắn với VPS {channel_label}. User {user.username} hiện được cấp các VPS: {assigned_label}. "
            "Hãy reconnect kênh trên một VPS đã được cấp trước khi render/upload."
        )

    def _can_assign_channel_to_user(
        self,
        user: UserSummary,
        channel: ChannelRecord,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> bool:
        if user.role != "user":
            return False
        if viewer_role == "manager" and viewer_id and self._resolved_user_manager_id(user) != viewer_id:
            return False
        return self._channel_matches_user_worker(user, channel)

    @staticmethod
    def _worker_browser_base(worker: WorkerRecord) -> tuple[int, int, int, int]:
        return (
            int(worker.browser_display_base or 90),
            int(worker.browser_vnc_port_base or 15900),
            int(worker.browser_web_port_base or 16080),
            int(worker.browser_debug_port_base or 19220),
        )

    def _assert_worker_browser_ready(self, worker: WorkerRecord) -> None:
        worker_label = self._resolve_worker_display_name(worker.id)
        if not worker.browser_session_enabled:
            raise ValueError(f"VPS {worker_label} chưa bật browser session.")
        if not str(worker.public_base_url or "").strip():
            raise ValueError(f"VPS {worker_label} chưa khai báo public_base_url cho noVNC.")

    def _ensure_channel_user_link(self, *, channel_id: str, user_id: str) -> None:
        exists = next(
            (
                link
                for link in self.channel_user_links
                if link["channel_id"] == channel_id and link["user_id"] == user_id
            ),
            None,
        )
        if exists:
            return

        next_id = max([item["id"] for item in self.channel_user_links], default=0) + 1
        self.channel_user_links.append({"id": next_id, "channel_id": channel_id, "user_id": user_id})

    def _find_browser_session(self, session_id: str) -> BrowserSessionRecord:
        for session in self.browser_sessions:
            if session.session_id == session_id:
                return session
        raise KeyError(session_id)

    def _find_user_browser_session(self, user_id: str, session_id: str) -> BrowserSessionRecord:
        session = self._find_browser_session(session_id)
        if session.owner_user_id != user_id:
            raise KeyError(session_id)
        return session

    def _browser_session_response(self, session: BrowserSessionRecord) -> BrowserSessionResponse:
        return BrowserSessionResponse(
            session_id=session.session_id,
            status=session.status,
            target_worker_id=session.target_worker_id,
            target_worker_name=session.target_worker_name,
            target_worker_public_base_url=session.target_worker_public_base_url,
            novnc_url=session.novnc_url,
            access_password=session.access_password,
            expires_at=session.expires_at,
            current_url=session.current_url,
            current_title=session.current_title,
            detected_channel_id=session.detected_channel_id,
            detected_channel_name=session.detected_channel_name,
            channel_record_id=session.channel_record_id,
            last_error=session.last_error,
        )

    def _cleanup_stale_browser_sessions(self) -> None:
        now = self._now(trim=False)
        changed = False
        keep_sessions: list[BrowserSessionRecord] = []
        for session in self.browser_sessions:
            if session.status in {"closed", "expired"}:
                if session.expires_at < now - timedelta(hours=12):
                    changed = True
                    continue
                keep_sessions.append(session)
                continue

            if session.expires_at <= now:
                session.status = "expired"
                changed = True
                keep_sessions.append(session)
                continue
            keep_sessions.append(session)

        if changed:
            self.browser_sessions = keep_sessions
            self._save_state()

    def _active_browser_session_for_user(self, user_id: str) -> BrowserSessionRecord | None:
        self._cleanup_stale_browser_sessions()
        candidates = [
            session
            for session in self.browser_sessions
            if session.owner_user_id == user_id
            and session.status in {"launching", "awaiting_confirmation", "confirmed"}
            and session.expires_at > self._now(trim=False)
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: item.created_at, reverse=True)[0]

    def _allocate_browser_ports_for_worker(self, worker: WorkerRecord) -> tuple[int, int, int, int]:
        display_base, vnc_base, web_base, debug_base = self._worker_browser_base(worker)
        now = self._now(trim=False)
        taken = {
            (session.display_number, session.vnc_port, session.web_port, session.debug_port)
            for session in self.browser_sessions
            if session.target_worker_id == worker.id
            if (
                session.status in {"launching", "awaiting_confirmation", "confirmed"}
                or session.expires_at >= now - timedelta(minutes=5)
            )
        }
        for offset in range(0, 200):
            candidate = (
                display_base + offset,
                vnc_base + offset,
                web_base + offset,
                debug_base + offset,
            )
            if candidate not in taken:
                return candidate
        raise ValueError(
            f"Đã hết pool port browser session trên VPS {self._resolve_worker_display_name(worker.id)}."
        )

    def _sync_browser_session_details(self, session: BrowserSessionRecord) -> BrowserSessionRecord:
        if not session.novnc_url and session.target_worker_public_base_url:
            session.novnc_url = self._build_browser_session_novnc_url(
                session.target_worker_public_base_url,
                session.web_port,
                session.access_password,
            )
        return session

    @staticmethod
    def _build_browser_session_novnc_url(base_url: str, web_port: int, access_password: str) -> str:
        encoded_password = quote(access_password, safe="")
        return (
            f"{base_url.rstrip('/')}:{web_port}/vnc.html?autoconnect=1&resize=scale&view_only=0"
            f"#password={encoded_password}"
        )

    @staticmethod
    def _extract_html_meta_content(document: str, *patterns: str) -> str | None:
        for pattern in patterns:
            match = re.search(pattern, document, re.IGNORECASE)
            if not match:
                continue
            value = html.unescape(str(match.group(1) or "").strip())
            if value:
                return value
        return None

    def _fetch_public_youtube_channel_metadata(self, channel_id: str) -> dict[str, str | None]:
        cleaned_channel_id = str(channel_id or "").strip()
        if not cleaned_channel_id:
            return {"channel_name": None, "avatar_url": None}

        request = Request(
            f"https://www.youtube.com/channel/{cleaned_channel_id}",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                )
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                document = response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError):
            return {"channel_name": None, "avatar_url": None}

        channel_name = self._extract_html_meta_content(
            document,
            r'<meta\s+property="og:title"\s+content="([^"]+)"',
            r'<meta\s+name="title"\s+content="([^"]+)"',
            r"<title>([^<]+)</title>",
        )
        if channel_name:
            channel_name = re.sub(r"\s*-\s*YouTube\s*$", "", channel_name, flags=re.IGNORECASE).strip()
            if channel_name.lower() in {"youtube", "youtube studio", "studio"}:
                channel_name = None

        avatar_url = self._extract_html_meta_content(
            document,
            r'<meta\s+property="og:image"\s+content="([^"]+)"',
            r'<link\s+rel="image_src"\s+href="([^"]+)"',
        )
        return {
            "channel_name": channel_name,
            "avatar_url": avatar_url,
        }

    @staticmethod
    def _browser_channel_metadata_is_generic(channel: ChannelRecord) -> bool:
        if channel.connection_mode != "browser_profile":
            return False
        normalized_name = str(channel.name or "").strip().lower()
        if not normalized_name:
            return True
        if normalized_name in {
            "trang tổng quan của kênh",
            "trang tong quan cua kenh",
            "youtube studio",
            "dashboard",
            "studio",
        }:
            return True
        return not str(channel.avatar_url or "").strip()

    def _refresh_browser_channel_metadata(self, channel: ChannelRecord) -> bool:
        if not self._browser_channel_metadata_is_generic(channel):
            return False

        public_metadata = self._fetch_public_youtube_channel_metadata(channel.channel_id)
        resolved_name = str(public_metadata.get("channel_name") or "").strip()
        resolved_avatar = str(public_metadata.get("avatar_url") or "").strip() or None
        changed = False

        if resolved_name and channel.name != resolved_name:
            channel.name = resolved_name
            changed = True
        if resolved_avatar and channel.avatar_url != resolved_avatar:
            channel.avatar_url = resolved_avatar
            changed = True

        if not changed:
            return False

        for job in self.jobs:
            if job.channel_id != channel.id:
                continue
            job.channel_name = channel.name
            job.channel_avatar_url = channel.avatar_url
        return True

    def _refresh_browser_channels_metadata(self, channels: list[ChannelRecord]) -> bool:
        changed = False
        for channel in channels:
            if self._refresh_browser_channel_metadata(channel):
                changed = True
        if changed:
            self._save_state()
        return changed

    def _upsert_browser_connected_channel(
        self,
        *,
        user: UserSummary,
        session: BrowserSessionRecord,
        channel_id: str,
        channel_name: str,
        avatar_url: str | None = None,
    ) -> ChannelRecord:
        worker = self._pick_worker_for_user(user, session.target_worker_id)
        existing_channel = next((item for item in self.channels if item.channel_id == channel_id), None)
        now = self._now()
        if existing_channel:
            linked_user_ids = {
                str(link.get("user_id") or "").strip()
                for link in self.channel_user_links
                if str(link.get("channel_id") or "").strip() == existing_channel.id
            }
            if linked_user_ids and str(user.id or "").strip() not in linked_user_ids:
                raise ValueError(
                    "Kênh này đang gắn với tài khoản khác trên hệ thống. "
                    "Hãy đăng xuất hoặc chọn đúng kênh trước khi xác nhận."
                )
            channel = existing_channel
        else:
            channel = ChannelRecord(
                id=f"channel-{uuid4().hex[:8]}",
                name=channel_name,
                channel_id=channel_id,
                avatar_url=avatar_url,
                worker_id=worker.id,
                worker_name=worker.id,
                manager_name=user.manager_name or worker.manager_name,
                status="connected",
                connection_mode="browser_profile",
            )
            self.channels.append(channel)

        previous_browser_profile_key = channel.browser_profile_key
        previous_connection_mode = channel.connection_mode
        channel.name = channel_name
        channel.status = "connected"
        channel.worker_id = worker.id
        channel.worker_name = worker.id
        channel.manager_name = user.manager_name or worker.manager_name
        channel.connection_mode = "browser_profile"
        channel.browser_profile_key = session.profile_key
        channel.browser_profile_path = session.profile_path
        channel.browser_connected_at = now
        if avatar_url:
            channel.avatar_url = avatar_url
        if (
            previous_connection_mode == "browser_profile"
            and previous_browser_profile_key
            and previous_browser_profile_key != session.profile_key
        ):
            self._queue_browser_profile_cleanup(
                worker_id=worker.id,
                profile_key=previous_browser_profile_key,
            )
        for job in self.jobs:
            if job.channel_id != channel.id:
                continue
            job.channel_name = channel.name
            job.channel_avatar_url = channel.avatar_url
        self._ensure_channel_user_link(channel_id=channel.id, user_id=user.id)
        return channel

    def create_browser_session(self, user_id: str, worker_id: str | None = None) -> BrowserSessionResponse:
        user = self._require_workspace_user(user_id)
        worker = self._pick_worker_for_user(user, worker_id)
        existing = self._active_browser_session_for_user(user.id)
        if existing:
            self._sync_browser_session_details(existing)
            existing.status = "closed"
            existing.expires_at = self._now(trim=False)
            if existing.target_worker_id != worker.id:
                existing.last_error = "Session cũ đã đóng do VPS được cấp đã thay đổi."
            else:
                existing.last_error = "Session cũ đã đóng để mở phiên thêm kênh mới."
            self._queue_browser_profile_cleanup_if_orphan(
                worker_id=existing.target_worker_id,
                profile_key=existing.profile_key,
            )
            self._save_state()

        self._close_orphan_browser_sessions_for_user(user.id)
        self._assert_worker_browser_ready(worker)
        display_number, vnc_port, web_port, debug_port = self._allocate_browser_ports_for_worker(worker)
        now = self._now(trim=False)
        lifetime_minutes = self.browser_runtime.load_config().lifetime_minutes
        session = BrowserSessionRecord(
            session_id=f"browser-{uuid4().hex[:12]}",
            owner_user_id=user.id,
            owner_username=user.username,
            target_worker_id=worker.id,
            target_worker_name=self._resolve_worker_display_name(worker.id),
            target_worker_public_base_url=str(worker.public_base_url or "").rstrip("/") or None,
            profile_key=f"{user.username}-{uuid4().hex[:10]}",
            display_number=display_number,
            vnc_port=vnc_port,
            web_port=web_port,
            debug_port=debug_port,
            access_password=secrets.token_urlsafe(9),
            status="launching",
            created_at=now,
            expires_at=now + timedelta(minutes=lifetime_minutes),
        )
        self.browser_sessions.append(session)
        self._sync_browser_session_details(session)
        self._save_state()
        return self._browser_session_response(session)

    def get_browser_session(self, user_id: str, session_id: str) -> BrowserSessionResponse:
        self._require_workspace_user(user_id)
        self._cleanup_stale_browser_sessions()
        session = self._find_user_browser_session(user_id, session_id)
        self._sync_browser_session_details(session)
        self._save_state()
        return self._browser_session_response(session)

    def confirm_browser_session(self, user_id: str, session_id: str) -> dict[str, Any]:
        user = self._require_workspace_user(user_id)
        self._cleanup_stale_browser_sessions()
        session = self._find_user_browser_session(user.id, session_id)
        if session.status not in {"awaiting_confirmation", "confirmed"}:
            raise ValueError("Browser session không còn ở trạng thái xác nhận hợp lệ.")

        self._sync_browser_session_details(session)
        session_channel_id = self._extract_browser_session_channel_identity(session.current_url)
        channel_id = str(session_channel_id or session.detected_channel_id or "").strip()
        channel_name = str(session.detected_channel_name or "").strip() or channel_id
        if not channel_id:
            raise ValueError("Chưa nhận diện được channel. Hãy mở YouTube Studio đúng kênh rồi thử lại.")
        if not session_channel_id:
            raise ValueError(
                "Phiên đăng nhập chưa đứng tại YouTube Studio của kênh cần thêm. "
                "Hãy mở đúng YouTube Studio rồi thử xác nhận lại."
            )

        session.detected_channel_id = channel_id
        public_metadata = self._fetch_public_youtube_channel_metadata(channel_id)
        channel_name = str(public_metadata.get("channel_name") or "").strip() or channel_name
        avatar_url = str(public_metadata.get("avatar_url") or "").strip() or None

        channel = self._upsert_browser_connected_channel(
            user=user,
            session=session,
            channel_id=channel_id,
            channel_name=channel_name or channel_id,
            avatar_url=avatar_url,
        )
        confirmed_at = self._now(trim=False)
        session.status = "closed"
        session.confirmed_at = confirmed_at
        session.channel_record_id = channel.id
        session.expires_at = confirmed_at
        self._save_state()
        return {
            "session": self._browser_session_response(session).model_dump(mode="json"),
            "channel": {
                "channel_record_id": channel.id,
                "channel_id": channel.channel_id,
                "channel_name": channel.name,
                "connection_mode": channel.connection_mode,
            },
        }

    def close_browser_session(self, user_id: str, session_id: str) -> BrowserSessionResponse:
        self._require_workspace_user(user_id)
        session = self._find_user_browser_session(user_id, session_id)
        session.status = "closed"
        session.expires_at = self._now(trim=False)
        self._queue_browser_profile_cleanup_if_orphan(
            worker_id=session.target_worker_id,
            profile_key=session.profile_key,
        )
        self._save_state()
        return self._browser_session_response(session)

    def get_worker_browser_sessions(self, worker_id: str, shared_secret: str) -> list[BrowserSessionRecord]:
        worker = self._authenticate_worker(worker_id, shared_secret)
        self._cleanup_stale_browser_sessions()
        now = self._now(trim=False)
        sessions = [
            session
            for session in self.browser_sessions
            if session.target_worker_id == worker.id
            and (
                session.status in {"launching", "awaiting_confirmation", "confirmed"}
                or session.expires_at >= now - timedelta(minutes=30)
            )
        ]
        for session in sessions:
            session.target_worker_public_base_url = str(worker.public_base_url or "").rstrip("/") or None
            self._sync_browser_session_details(session)
        return [deepcopy(session) for session in sessions]

    def get_worker_browser_profile_cleanup_tasks(
        self,
        worker_id: str,
        shared_secret: str,
    ) -> list[dict[str, str]]:
        worker = self._authenticate_worker(worker_id, shared_secret)
        tasks: list[dict[str, str]] = []
        for item in self.browser_profile_cleanup_tasks:
            if str(item.get("worker_id") or "").strip() != worker.id:
                continue
            profile_key = str(item.get("profile_key") or "").strip()
            if not profile_key:
                continue
            tasks.append({"profile_key": profile_key})
        return tasks

    def ack_worker_browser_profile_cleanup_tasks(
        self,
        worker_id: str,
        shared_secret: str,
        profile_keys: list[str],
    ) -> list[str]:
        worker = self._authenticate_worker(worker_id, shared_secret)
        normalized = {
            str(profile_key or "").strip()
            for profile_key in profile_keys
            if str(profile_key or "").strip()
        }
        if not normalized:
            return []

        cleared: list[str] = []
        remaining: list[dict[str, Any]] = []
        for item in self.browser_profile_cleanup_tasks:
            item_worker_id = str(item.get("worker_id") or "").strip()
            item_profile_key = str(item.get("profile_key") or "").strip()
            if item_worker_id == worker.id and item_profile_key in normalized:
                cleared.append(item_profile_key)
                continue
            remaining.append(item)

        if len(remaining) != len(self.browser_profile_cleanup_tasks):
            self.browser_profile_cleanup_tasks = remaining
            self._save_state()
        return cleared

    def sync_worker_browser_session(
        self,
        session_id: str,
        payload: WorkerBrowserSessionSyncPayload,
    ) -> BrowserSessionRecord:
        worker = self._authenticate_worker(payload.worker_id, payload.shared_secret)
        session = self._find_browser_session(session_id)
        if session.target_worker_id != worker.id:
            raise ValueError("Browser session đang thuộc VPS khác.")

        if not (
            session.status in {"closed", "expired"}
            and payload.status in {"launching", "awaiting_confirmation", "confirmed"}
        ):
            session.status = payload.status

        session.novnc_url = payload.novnc_url or session.novnc_url
        session.current_url = payload.current_url
        session.current_title = payload.current_title
        session.detected_channel_id = payload.detected_channel_id
        session.detected_channel_name = payload.detected_channel_name
        session.last_error = payload.last_error
        session.profile_path = payload.profile_path or session.profile_path
        session.session_path = payload.session_path or session.session_path
        session.password_file = payload.password_file or session.password_file
        session.xvfb_pid = payload.xvfb_pid
        session.openbox_pid = payload.openbox_pid
        session.chromium_pid = payload.chromium_pid
        session.x11vnc_pid = payload.x11vnc_pid
        session.websockify_pid = payload.websockify_pid
        if payload.status in {"awaiting_confirmation", "confirmed"} and session.launched_at is None:
            session.launched_at = self._now(trim=False)
        self._save_state()
        return deepcopy(session)

    def _user_meta_record(self, user_id: str) -> dict[str, Any]:
        return self.user_meta.setdefault(
            user_id,
            {
                "password_hash": "",
                "password_algo": "pbkdf2_sha256",
                "telegram": "",
                "telegram_live": "",
                "updated_by": "system",
                "updated_at": None,
                "created_at": self._now(),
            },
        )

    def _admin_workspace_href(self, href: str, workspace_mode: str = "upload") -> str:
        if workspace_mode != "live":
            return href
        separator = "&" if "?" in href else "?"
        return f"{href}{separator}workspace=live"

    def _admin_workspace_target_href(
        self,
        *,
        target_workspace: str,
        active_page: str,
        user_section: str | None = None,
    ) -> str:
        if active_page == "workers":
            return "/admin/ManagerBOT/index"
        if target_workspace == "live":
            if active_page == "users":
                return "/admin/user/index?workspace=live"
            if active_page == "channels":
                return "/app/live"
            if active_page in {"renders", "upload_jobs", "live_stream_jobs"}:
                return "/admin/livestream/index"
            return "/app/live"

        if active_page == "users":
            return "/admin/user/index"
        if active_page == "workers":
            return "/admin/ManagerBOT/index"
        if active_page == "channels":
            return "/admin/channel/index"
        if active_page in {"renders", "upload_jobs", "live_stream_jobs"}:
            return "/admin/upload/index"
        if active_page in {"live_workspace", "render_workspace"}:
            return "/app"
        return "/admin/user/index"

    def _admin_workspace_tabs(
        self,
        workspace_mode: str = "upload",
        *,
        active_page: str = "users",
        user_section: str | None = None,
    ) -> list[dict[str, str | bool]]:
        if active_page in {"channels", "workers", "users"}:
            return []
        workspace_tab_active = not (active_page == "users" and user_section in {"create", "managers", "admins"})
        return [
            {
                "label": "Upload",
                "href": self._admin_workspace_target_href(
                    target_workspace="upload",
                    active_page=active_page,
                    user_section=user_section,
                ),
                "active": workspace_tab_active and workspace_mode != "live",
            },
            {
                "label": "Live Stream",
                "href": self._admin_workspace_target_href(
                    target_workspace="live",
                    active_page=active_page,
                    user_section=user_section,
                ),
                "active": workspace_tab_active and workspace_mode == "live",
            },
        ]

    def _admin_nav_items(self, workspace_mode: str = "upload") -> list[dict[str, str]]:
        items = [
            {"key": "users", "label": "Người dùng", "href": "/admin/user/index", "icon": "users"},
            {"key": "workers", "label": "Danh sách BOT", "href": "/admin/ManagerBOT/index", "icon": "server"},
            {"key": "upload_jobs", "label": "Danh sách Upload", "href": "/admin/upload/index", "icon": "video"},
            {"key": "live_stream_jobs", "label": "Danh sách Live Stream", "href": "/admin/livestream/index", "icon": "radio"},
            {"key": "render_workspace", "label": "Điều phối Upload", "href": "/app", "icon": "clapperboard"},
            {"key": "live_workspace", "label": "Điều phối Live Stream", "href": "/app/live", "icon": "radio-tower"},
        ]
        items.insert(
            2,
            {
                "key": "channels",
                "label": "Danh sách Kênh",
                "href": "/admin/channel/index",
                "icon": "link",
            },
        )
        return items

    def _user_section_tabs(
        self,
        active_key: str,
        viewer_role: str = "admin",
    ) -> list[dict[str, str | bool]]:
        tabs = [
            {"key": "users", "label": "Danh sách user", "href": "/admin/user/index"},
            {"key": "create", "label": "Tạo user", "href": "/admin/user/create"},
        ]
        if viewer_role != "manager":
            tabs.extend(
                [
                    {"key": "managers", "label": "Manager", "href": "/admin/user/manager"},
                    {"key": "admins", "label": "Admin", "href": "/admin/user/admins"},
                ]
            )
        for item in tabs:
            item["active"] = item["key"] == active_key
        return tabs

    def _selected_manager_ids(self, manager_ids: list[str] | None) -> list[str]:
        available_ids = [user.id for user in self.users if user.role == "manager"]
        if not manager_ids:
            return []
        return [manager_id for manager_id in manager_ids if manager_id in available_ids]

    def _manager_options(self, selected_ids: list[str] | None = None) -> list[dict[str, str | bool]]:
        selected_set = set(selected_ids or [])
        return [
            {
                "id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "initials": self._initials(user.display_name or user.username),
                "avatar_class": self._avatar_palette(user.display_name or user.username),
                "selected": user.id in selected_set,
            }
            for user in self.users
            if user.role == "manager"
        ]

    def _bot_manager_options(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> list[dict[str, str]]:
        options: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        for user in self.users:
            include = user.role == "manager"
            if viewer_role == "manager" and viewer_id:
                include = user.id == viewer_id
            if not include or user.id in seen_ids:
                continue
            options.append(
                {
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.display_name,
                    "role": user.role,
                }
            )
            seen_ids.add(user.id)
        options.sort(
            key=lambda item: (
                0 if (viewer_id and item["id"] == viewer_id) else 1,
                item["username"].casefold(),
            )
        )
        return options

    def _filtered_workers(self, manager_ids: list[str] | None = None) -> list[WorkerRecord]:
        selected_ids = set(self._selected_manager_ids(manager_ids))
        if not selected_ids:
            return list(self.workers)
        return [
            worker
            for worker in self.workers
            if self._resolved_bot_manager_scope(
                manager_id=worker.manager_id,
                manager_name=worker.manager_name,
                assigned_users=self._assigned_users_for_worker(worker.id),
            )[0]
            in selected_ids
        ]

    def _filtered_live_workers(self, manager_ids: list[str] | None = None) -> list[WorkerRecord]:
        selected_ids = set(self._selected_manager_ids(manager_ids))
        if not selected_ids:
            return list(self.live_workers)
        return [
            worker
            for worker in self.live_workers
            if self._resolved_bot_manager_scope(
                manager_id=worker.manager_id,
                manager_name=worker.manager_name,
                assigned_users=self._assigned_live_users_for_worker(worker.id),
            )[0]
            in selected_ids
        ]

    def _effective_manager_scope_ids(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        manager_ids: list[str] | None = None,
    ) -> set[str]:
        selected_manager_ids = set(self._selected_manager_ids(manager_ids))
        if viewer_role == "manager" and viewer_id:
            return {viewer_id}
        return selected_manager_ids

    def _live_workspace_scope_query(
        self,
        *,
        manager_ids: list[str] | set[str] | None = None,
        user_id: str | None = None,
    ) -> tuple[str, str]:
        query: list[tuple[str, str]] = []
        for manager_id in manager_ids or []:
            normalized_manager_id = str(manager_id or "").strip()
            if normalized_manager_id:
                query.append(("manager_ids", normalized_manager_id))
        normalized_user_id = str(user_id or "").strip()
        if normalized_user_id:
            query.append(("userId", normalized_user_id))
        if not query:
            return "", ""
        encoded = urlencode(query, doseq=True)
        return f"?{encoded}", f"&{encoded}"

    def _scoped_admin_summary(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        manager_ids: list[str] | None = None,
    ) -> AdminSummary:
        selected_manager_ids = self._effective_manager_scope_ids(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        if not selected_manager_ids:
            return self.get_admin_dashboard().summary

        scoped_workers = [worker for worker in self.workers if worker.manager_id in selected_manager_ids]
        scoped_worker_ids = {worker.id for worker in scoped_workers}
        scoped_channels = [
            channel
            for channel in self.channels
            if self._resolve_channel_manager_id(channel) in selected_manager_ids
        ]
        scoped_jobs = [job for job in self.jobs if self._resolve_job_manager_id(job) in selected_manager_ids]
        scoped_user_ids = {
            str(link.get("user_id") or "").strip()
            for link in self.user_worker_links
            if str(link.get("user_id") or "").strip()
            and str(link.get("worker_id") or "").strip() in scoped_worker_ids
        }
        scoped_users = [
            user
            for user in self.users
            if user.id in scoped_user_ids and user.role in {"user", "manager", "admin"}
        ]
        return AdminSummary(
            total_managers=len(selected_manager_ids),
            total_users=len(scoped_users),
            total_workers=len(scoped_workers),
            workers_online=len([worker for worker in scoped_workers if worker.status in {"online", "busy"}]),
            channels_connected=len([channel for channel in scoped_channels if channel.status == "connected"]),
            queued_jobs=len([job for job in scoped_jobs if job.status in {"pending", "queueing"}]),
            running_jobs=len([job for job in scoped_jobs if job.status in {"downloading", "rendering", "uploading"}]),
            failed_jobs=len([job for job in scoped_jobs if job.status == "error"]),
        )

    def _admin_shell_context(
        self,
        *,
        page_title: str,
        active_page: str,
        user_section: str | None = None,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        manager_ids: list[str] | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        effective_manager_ids = self._effective_manager_scope_ids(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        selected_manager_ids = list(effective_manager_ids)
        scoped_summary = self._scoped_admin_summary(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=selected_manager_ids,
        )
        context = {
            "page_title": page_title,
            "app_name": "Youtube Lush",
            "active_page": active_page,
            "workspace_mode": workspace_mode,
            "workspace_heading": "Live Stream" if workspace_mode == "live" else "Upload",
            "workspace_tabs": self._admin_workspace_tabs(
                workspace_mode,
                active_page=active_page,
                user_section=user_section,
            ),
            "nav_items": self._admin_nav_items(workspace_mode),
            "summary_strip": (
                self._live_summary_strip(
                    viewer_role=viewer_role,
                    viewer_id=viewer_id,
                    manager_ids=selected_manager_ids,
                )
                if workspace_mode == "live"
                else self._summary_strip(
                    viewer_role=viewer_role,
                    viewer_id=viewer_id,
                    manager_ids=selected_manager_ids,
                )
            ),
            "summary": scoped_summary,
            "current_user": {"name": "Admin", "avatar": "/static/admin-themes/assets/img/avatar/avatar-1.png"},
            "manager_options": self._manager_options(selected_manager_ids),
            "selected_manager_ids": selected_manager_ids,
            "manager_filter_locked": viewer_role == "manager",
            "notice": notice,
            "notice_level": notice_level,
        }
        if user_section:
            context["user_tabs"] = self._user_section_tabs(
                user_section,
                viewer_role=viewer_role,
            )
            context["user_section"] = user_section
        return context

    def _role_badge(self, role: str) -> tuple[str, str]:
        mapping = {
            "admin": ("Admin", "inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-2.5 py-1 text-[10px] font-semibold text-rose-700"),
            "manager": ("Manager", "inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] font-semibold text-amber-700"),
            "user": ("User", "inline-flex items-center rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[10px] font-semibold text-emerald-700"),
        }
        return mapping.get(role, (role, "inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700"))

    def _worker_status_badge(self, status: str) -> tuple[str, str]:
        mapping = {
            "online": ("Connected", "inline-flex items-center rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[10px] font-semibold text-emerald-700"),
            "busy": ("Busy", "inline-flex items-center rounded-full border border-sky-200 bg-sky-50 px-2.5 py-1 text-[10px] font-semibold text-sky-700"),
            "offline": ("Disconnected", "inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-2.5 py-1 text-[10px] font-semibold text-rose-700"),
        }
        return mapping.get(status, (status, "inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700"))

    @staticmethod
    def _worker_status_dot_class(status: str) -> str:
        normalized = str(status or "").strip().lower()
        mapping = {
            "online": "bg-emerald-500",
            "busy": "bg-amber-400",
            "offline": "bg-rose-500",
        }
        return mapping.get(normalized, "bg-slate-300")

    def _channel_status_badge(self, status: str) -> tuple[str, str]:
        mapping = {
            "connected": ("Connected", "inline-flex items-center rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[10px] font-semibold text-emerald-700"),
            "pending_reconnect": ("Pending reconnect", "inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] font-semibold text-amber-700"),
            "disconnected": ("Disconnected", "inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-2.5 py-1 text-[10px] font-semibold text-rose-700"),
        }
        return mapping.get(status, (status, "inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700"))

    def _render_status_badge(self, job: RenderJobRecord) -> tuple[str, str]:
        mapping = {
            "pending": ("Pending", "inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2.5 py-1 text-[10px] font-semibold text-amber-700"),
            "queueing": ("Queueing", "inline-flex items-center rounded-full border border-orange-200 bg-orange-50 px-2.5 py-1 text-[10px] font-semibold text-orange-700"),
            "downloading": (f"Downloading ({job.progress}%)", "inline-flex items-center rounded-full border border-sky-200 bg-sky-50 px-2.5 py-1 text-[10px] font-semibold text-sky-700"),
            "rendering": (f"Rendering ({job.progress}%)", "inline-flex items-center rounded-full border border-indigo-200 bg-indigo-50 px-2.5 py-1 text-[10px] font-semibold text-indigo-700"),
            "uploading": (f"Uploading ({job.progress}%)", "inline-flex items-center rounded-full border border-cyan-200 bg-cyan-50 px-2.5 py-1 text-[10px] font-semibold text-cyan-700"),
            "completed": ("Completed", "inline-flex items-center rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-[10px] font-semibold text-emerald-700"),
            "cancelled": ("Cancelled", "inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700"),
            "error": ("Error", "inline-flex items-center rounded-full border border-rose-200 bg-rose-50 px-2.5 py-1 text-[10px] font-semibold text-rose-700"),
        }
        return mapping.get(job.status, (job.status, "inline-flex items-center rounded-full border border-slate-200 bg-slate-100 px-2.5 py-1 text-[10px] font-semibold text-slate-700"))

    def _summary_strip(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        manager_ids: list[str] | None = None,
    ) -> list[dict[str, str]]:
        summary = self._scoped_admin_summary(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        selected_manager_ids = self._effective_manager_scope_ids(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        upload_jobs = self.jobs
        if selected_manager_ids:
            upload_jobs = [job for job in upload_jobs if self._resolve_job_manager_id(job) in selected_manager_ids]
        uploading_count = len([job for job in upload_jobs if job.status == "uploading"])
        return [
            {
                "value": str(summary.channels_connected),
                "label": "Tổng Số Kênh",
                "icon": "radio",
                "icon_class": "text-emerald-500",
                "accent": "Online",
                "accent_class": "text-emerald-600",
                "accent_badge_class": "border-emerald-200 bg-emerald-50 text-emerald-700",
                "value_class": "text-emerald-600",
                "bar_color": "#10b981",
            },
            {
                "value": f"{summary.workers_online}/{summary.total_workers}",
                "label": "BOT Đang Chạy",
                "icon": "server",
                "icon_class": "text-brand-500",
                "accent": "Active",
                "accent_class": "text-brand-600",
                "accent_badge_class": "border-brand-100 bg-brand-50 text-brand-700",
                "value_class": "text-brand-600",
                "bar_color": "#6366f1",
            },
            {
                "value": str(summary.total_users),
                "label": "Tổng User",
                "icon": "users",
                "icon_class": "text-rose-500",
                "accent": "Accounts",
                "accent_class": "text-rose-500",
                "accent_badge_class": "border-rose-200 bg-rose-50 text-rose-700",
                "value_class": "text-slate-900",
                "bar_color": "#f43f5e",
            },
            {
                "value": str(summary.running_jobs),
                "label": "Đang Render",
                "icon": "loader",
                "icon_class": "text-brand-500",
                "accent": "Render",
                "accent_class": "text-brand-600",
                "accent_badge_class": "border-indigo-200 bg-indigo-50 text-indigo-700",
                "value_class": "text-brand-600",
                "bar_color": "#5d67df",
            },
            {
                "value": str(uploading_count),
                "label": "Đang Upload",
                "icon": "cloud-upload",
                "icon_class": "text-sky-500",
                "accent": "Upload",
                "accent_class": "text-sky-500",
                "accent_badge_class": "border-sky-200 bg-sky-50 text-sky-700",
                "value_class": "text-slate-900" if uploading_count == 0 else "text-sky-500",
                "bar_color": "#0284c7",
            },
            {
                "value": str(summary.queued_jobs),
                "label": "Job Đang Chờ",
                "icon": "layers",
                "icon_class": "text-amber-500",
                "accent": "Queue",
                "accent_class": "text-amber-600",
                "accent_badge_class": "border-amber-200 bg-amber-50 text-amber-700",
                "value_class": "text-amber-600",
                "bar_color": "#f59e0b",
            },
        ]

    def _combined_bot_summary_strip(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        manager_ids: list[str] | None = None,
    ) -> list[dict[str, str]]:
        selected_manager_ids = self._effective_manager_scope_ids(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        scoped_manager_ids = list(selected_manager_ids) if selected_manager_ids else None
        scoped_upload_workers = self._filtered_workers(scoped_manager_ids)
        scoped_live_workers = self._filtered_live_workers(scoped_manager_ids)
        combined_workers = [*scoped_upload_workers, *scoped_live_workers]
        combined_workers_online = len([worker for worker in combined_workers if worker.status in {"online", "busy"}])
        scoped_summary = self._scoped_admin_summary(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        scoped_streams = self._scoped_live_streams(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        upload_jobs = self.jobs
        if selected_manager_ids:
            upload_jobs = [job for job in upload_jobs if self._resolve_job_manager_id(job) in selected_manager_ids]
        uploading_count = len([job for job in upload_jobs if job.status == "uploading"])
        active_live_count = len(
            [stream for stream in scoped_streams if self._effective_live_runtime_stream(stream).status == "streaming"]
        )
        scoped_live_worker_ids = {worker.id for worker in scoped_live_workers}
        backup_worker_count = len(
            {
                worker_id
                for worker_id in scoped_live_worker_ids
                if self._live_worker_assigned_role(worker_id) == "backup"
            }
        )
        return [
            {
                "value": str(scoped_summary.channels_connected),
                "label": "Tổng Số Kênh",
                "icon": "radio",
                "icon_class": "text-emerald-500",
                "accent": "Online",
                "accent_badge_class": "border-emerald-200 bg-emerald-50 text-emerald-700",
                "value_class": "text-emerald-600",
                "bar_color": "#10b981",
            },
            {
                "value": f"{combined_workers_online}/{len(combined_workers)}",
                "label": "BOT Đang Chạy",
                "icon": "server",
                "icon_class": "text-brand-500",
                "accent": "Active",
                "accent_badge_class": "border-brand-100 bg-brand-50 text-brand-700",
                "value_class": "text-brand-600",
                "bar_color": "#6366f1",
            },
            {
                "value": str(scoped_summary.total_users),
                "label": "Tổng User",
                "icon": "users",
                "icon_class": "text-rose-500",
                "accent": "Accounts",
                "accent_badge_class": "border-rose-200 bg-rose-50 text-rose-700",
                "value_class": "text-slate-900",
                "bar_color": "#f43f5e",
            },
            {
                "value": str(active_live_count),
                "label": "Đang Live",
                "icon": "radio",
                "icon_class": "text-brand-500",
                "accent": "Live",
                "accent_badge_class": "border-indigo-200 bg-indigo-50 text-indigo-700",
                "value_class": "text-brand-600" if active_live_count else "text-slate-900",
                "bar_color": "#5d67df",
            },
            {
                "value": str(uploading_count),
                "label": "Đang Upload",
                "icon": "upload-cloud",
                "icon_class": "text-sky-500",
                "accent": "Upload",
                "accent_badge_class": "border-sky-200 bg-sky-50 text-sky-700",
                "value_class": "text-sky-500" if uploading_count else "text-slate-900",
                "bar_color": "#0284c7",
            },
            {
                "value": str(backup_worker_count),
                "label": "BOT Backup",
                "icon": "shield-check",
                "icon_class": "text-amber-500",
                "accent": "Fallback",
                "accent_badge_class": "border-amber-200 bg-amber-50 text-amber-700",
                "value_class": "text-amber-600" if backup_worker_count else "text-slate-900",
                "bar_color": "#f59e0b",
            },
        ]

    def _user_worker_count(self, user: UserSummary) -> int:
        return len(self._assigned_worker_links_for_user(user.id))

    def _user_live_worker_count(self, user: UserSummary) -> int:
        return len(self._assigned_live_worker_links_for_user(user.id))

    def _user_channel_count(self, user: UserSummary) -> int:
        return len(self._user_channel_ids(user.id))

    def _user_bot_triplet_metrics(self, user: UserSummary) -> dict[str, int]:
        return {
            "upload_bots_total": len(self._assigned_workers_for_user(user)),
            "live_primary_bots_total": len(self._assigned_live_workers_for_user(user, role="primary")),
            "live_backup_bots_total": len(self._assigned_live_workers_for_user(user, role="backup")),
        }

    def _admin_user_summary_strip(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        manager_ids: list[str] | None = None,
    ) -> list[dict[str, str]]:
        summary = self._scoped_admin_summary(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        selected_manager_ids = self._effective_manager_scope_ids(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        upload_jobs = self.jobs
        if selected_manager_ids:
            upload_jobs = [job for job in upload_jobs if self._resolve_job_manager_id(job) in selected_manager_ids]
        uploading_count = len([job for job in upload_jobs if job.status == "uploading"])
        scoped_streams = self._scoped_live_streams(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        active_live_count = len(
            [stream for stream in scoped_streams if self._effective_live_runtime_stream(stream).status == "streaming"]
        )
        backup_live_count = len({stream.backup_worker_id for stream in scoped_streams if stream.backup_worker_id})
        return [
            {
                "value": str(summary.channels_connected),
                "label": "Tổng Kênh",
                "icon": "radio",
                "icon_class": "text-emerald-500",
                "accent": "Upload",
                "accent_badge_class": "border-emerald-200 bg-emerald-50 text-emerald-700",
                "value_class": "text-emerald-600",
                "bar_color": "#10b981",
            },
            {
                "value": str(summary.total_users),
                "label": "Tổng User",
                "icon": "users",
                "icon_class": "text-rose-500",
                "accent": "Accounts",
                "accent_badge_class": "border-rose-200 bg-rose-50 text-rose-700",
                "value_class": "text-slate-900",
                "bar_color": "#f43f5e",
            },
            {
                "value": str(uploading_count),
                "label": "Đang Upload",
                "icon": "cloud-upload",
                "icon_class": "text-sky-500",
                "accent": "Upload",
                "accent_badge_class": "border-sky-200 bg-sky-50 text-sky-700",
                "value_class": "text-slate-900" if uploading_count == 0 else "text-sky-600",
                "bar_color": "#0ea5e9",
            },
            {
                "value": str(active_live_count),
                "label": "Đang Live",
                "icon": "radio",
                "icon_class": "text-brand-500",
                "accent": "Live",
                "accent_badge_class": "border-brand-100 bg-brand-50 text-brand-700",
                "value_class": "text-slate-900" if active_live_count == 0 else "text-brand-600",
                "bar_color": "#6b74f0",
            },
            {
                "value": str(backup_live_count),
                "label": "BOT Backup",
                "icon": "shield-check",
                "icon_class": "text-amber-500",
                "accent": "Fallback",
                "accent_badge_class": "border-amber-200 bg-amber-50 text-amber-700",
                "value_class": "text-slate-900" if backup_live_count == 0 else "text-amber-600",
                "bar_color": "#f59e0b",
            },
        ]

    def _channel_users(self, channel: ChannelRecord) -> list[str]:
        assigned_ids = [link["user_id"] for link in self.channel_user_links if link["channel_id"] == channel.id]
        assigned = []
        for user_id in assigned_ids:
            try:
                assigned.append(self._find_user(user_id).display_name)
            except KeyError:
                continue
        return assigned

    def _natural_sort_text(self, value: str | None) -> str:
        normalized = unicodedata.normalize("NFKD", str(value or "").strip())
        ascii_folded = "".join(char for char in normalized if not unicodedata.combining(char))
        return ascii_folded.casefold()

    def _channel_user_count(self, channel_id: str) -> int:
        return len([link for link in self.channel_user_links if link["channel_id"] == channel_id])

    def _channel_link(self, channel: ChannelRecord) -> str:
        return f"https://www.youtube.com/channel/{channel.channel_id}"

    def _find_job(self, job_id: str) -> RenderJobRecord:
        for job in self.jobs:
            if job.id == job_id:
                return job
        raise KeyError(job_id)

    def _find_channel_optional(self, channel_id: str | None) -> ChannelRecord | None:
        if not channel_id:
            return None
        try:
            return self._find_channel(channel_id)
        except KeyError:
            return None

    def _find_worker_optional(self, worker_id: str | None) -> WorkerRecord | None:
        if not worker_id:
            return None
        try:
            return self._find_worker(worker_id)
        except KeyError:
            return None

    def _find_live_worker_optional(self, worker_id: str | None) -> WorkerRecord | None:
        if not worker_id:
            return None
        try:
            return self._find_live_worker(worker_id)
        except KeyError:
            return None

    def _manager_username_to_id_map(self) -> dict[str, str]:
        return {
            user.username: user.id
            for user in self.users
            if user.role == "manager"
        }

    def _manager_username_from_id(self, manager_id: str | None) -> str | None:
        if not manager_id:
            return None
        try:
            user = self._find_user(manager_id)
        except KeyError:
            return None
        return user.username if user.role == "manager" else None

    def _resolve_owner_scope_from_assigned_users(
        self,
        assigned_users: list[UserSummary] | None = None,
    ) -> tuple[str | None, str | None]:
        for assigned_user in assigned_users or []:
            if assigned_user.role == "manager":
                return assigned_user.id, assigned_user.username
            if assigned_user.role == "user":
                manager_id = self._resolved_user_manager_id(assigned_user)
                manager_name = (
                    self._manager_username_from_id(manager_id)
                    or str(assigned_user.manager_name or "").strip()
                    or None
                )
                if manager_id or manager_name:
                    return manager_id, manager_name
            if assigned_user.role == "admin":
                return None, assigned_user.username
        return None, None

    def _resolved_bot_manager_scope(
        self,
        *,
        manager_id: str | None = None,
        manager_name: str | None = None,
        assigned_users: list[UserSummary] | None = None,
    ) -> tuple[str | None, str]:
        normalized_manager_id = str(manager_id or "").strip() or None
        normalized_manager_name = str(manager_name or "").strip()
        if normalized_manager_id:
            resolved_manager_name = self._manager_username_from_id(normalized_manager_id)
            if resolved_manager_name:
                return normalized_manager_id, resolved_manager_name
        if normalized_manager_name and normalized_manager_name not in {"system", "-"}:
            resolved_manager = self._find_user_by_username(normalized_manager_name)
            if resolved_manager and resolved_manager.role == "manager":
                return resolved_manager.id, resolved_manager.username
        fallback_manager_id, fallback_manager_name = self._resolve_owner_scope_from_assigned_users(assigned_users)
        if fallback_manager_id or fallback_manager_name:
            return fallback_manager_id, fallback_manager_name or normalized_manager_name or "system"
        if normalized_manager_id:
            return normalized_manager_id, normalized_manager_name or "system"
        return None, normalized_manager_name or "system"

    def _resolve_channel_manager_id(self, channel: ChannelRecord) -> str | None:
        worker = self._find_worker_optional(channel.worker_id)
        if worker and worker.manager_id:
            return worker.manager_id
        manager_name = str(channel.manager_name or "").strip()
        if not manager_name:
            return None
        return self._manager_username_to_id_map().get(manager_name)

    def _resolve_channel_manager_name(self, channel: ChannelRecord) -> str:
        manager_name = self._manager_username_from_id(self._resolve_channel_manager_id(channel))
        if manager_name:
            return manager_name
        worker = self._find_worker_optional(channel.worker_id)
        if worker and worker.manager_name:
            return worker.manager_name
        return str(channel.manager_name or "-")

    @staticmethod
    def _channel_connected_at(channel: ChannelRecord) -> datetime | None:
        return channel.browser_connected_at or channel.oauth_connected_at

    def _resolve_job_manager_id(self, job: RenderJobRecord) -> str | None:
        username_to_id = self._manager_username_to_id_map()
        channel = self._find_channel_optional(job.channel_id)
        if channel:
            manager_id = self._resolve_channel_manager_id(channel)
            if manager_id:
                return manager_id

        worker = self._find_worker_optional(job.claimed_by_worker_id or job.worker_name)
        if worker and worker.manager_id:
            return worker.manager_id

        return username_to_id.get(str(job.manager_name or "").strip() or "")

    def _resolve_job_manager_name(self, job: RenderJobRecord) -> str:
        manager_name = self._manager_username_from_id(self._resolve_job_manager_id(job))
        if manager_name:
            return manager_name

        channel = self._find_channel_optional(job.channel_id)
        if channel:
            return self._resolve_channel_manager_name(channel)

        worker = self._find_worker_optional(job.claimed_by_worker_id or job.worker_name)
        if worker and worker.manager_name:
            return worker.manager_name

        return str(job.manager_name or "-")

    def _release_worker_job_claim(self, job: RenderJobRecord, *, reset_status: str = "pending", message: str | None = None) -> None:
        job.status = reset_status  # type: ignore[assignment]
        job.progress = 0 if reset_status == "pending" else job.progress
        if reset_status == "pending":
            job.download_progress = 0
            job.render_progress = 0
            job.upload_progress = 0
        job.can_cancel = reset_status not in {"completed", "error"}
        job.claimed_by_worker_id = None
        job.claimed_at = None
        job.lease_expires_at = None
        job.download_started_at = None if reset_status == "pending" else job.download_started_at
        job.upload_started_at = None if reset_status == "pending" else job.upload_started_at
        job.status_message = None
        if message:
            job.error_message = message

    @staticmethod
    def _upload_interruption_looks_requeueable(job: RenderJobRecord) -> bool:
        upload_progress = max(int(job.upload_progress or 0), int(job.progress or 0))
        if upload_progress > 10:
            return False
        if job.output_url:
            return False
        status_message = str(job.status_message or "").strip().casefold()
        committed_markers = (
            "processing",
            "dang xu ly",
            "đang xử lý",
            "da luu",
            "đã lưu",
            "saved as",
            "uploaded",
            "đã tải",
        )
        return not any(marker in status_message for marker in committed_markers)

    def _handle_interrupted_worker_job(
        self,
        job: RenderJobRecord,
        *,
        now: datetime,
        reason: str,
    ) -> tuple[list[str], str] | None:
        if job.status == "uploading":
            if self._upload_interruption_looks_requeueable(job):
                self._release_worker_job_claim(
                    job,
                    reset_status="pending",
                    message=f"{reason} Job được đưa lại hàng chờ vì upload mới bắt đầu.",
                )
                return None
            job.status = "error"
            job.completed_at = now
            job.can_cancel = False
            job.lease_expires_at = None
            job.status_message = None
            job.download_progress = max(int(job.download_progress or 0), 100)
            job.render_progress = max(int(job.render_progress or 0), 100)
            job.upload_progress = max(int(job.upload_progress or 0), min(max(job.progress, 0), 99))
            job.error_message = (
                f"{reason} Job đang ở pha upload đã tiến xa, "
                "nên được đánh dấu lỗi để tránh upload trùng video."
            )
            chat_ids = self._job_upload_interruption_recipient_chat_ids(job)
            if not chat_ids:
                return None
            return (chat_ids, self._job_upload_interrupted_message(job, now=now, reason=reason))
        self._release_worker_job_claim(
            job,
            reset_status="pending",
            message=f"{reason} Job được đưa lại hàng chờ để worker nhận lại sau khi kết nối ổn định.",
        )
        return None

    def _reconcile_expired_worker_jobs(self, *, now: datetime) -> tuple[bool, list[tuple[list[str], str]]]:
        changed = False
        notifications: list[tuple[list[str], str]] = []
        active_statuses = self._worker_active_job_statuses()
        for job in self.jobs:
            if job.status not in active_statuses:
                continue
            if not job.claimed_by_worker_id:
                continue
            if job.lease_expires_at is None or job.lease_expires_at > now:
                continue
            notification_payload = self._handle_interrupted_worker_job(
                job,
                now=now,
                reason="Control-plane không nhận được heartbeat/progress của worker trong thời gian grace.",
            )
            if notification_payload is not None:
                notifications.append(notification_payload)
            changed = True
        if changed:
            self._refresh_queue_positions()
            self._save_state()
        return changed, notifications

    def _reconcile_expired_live_streams(self, *, now: datetime) -> bool:
        changed = False
        active_statuses = self._live_worker_active_statuses()
        for stream in self.live_streams:
            if stream.status not in active_statuses:
                continue
            if not stream.claimed_by_worker_id:
                continue
            if stream.lease_expires_at is None or stream.lease_expires_at > now:
                continue
            notification_payload = self._handle_interrupted_live_stream(
                stream,
                now=now,
                reason="Control-plane không nhận được heartbeat/progress của live worker trong thời gian grace.",
            )
            if notification_payload is not None:
                self._notify_live_telegram_chat_ids(notification_payload[0], notification_payload[1])
            changed = True
        if changed:
            self._sync_live_backup_policy(now=now)
            self._save_state()
        return changed

    def _reconcile_worker_jobs_from_heartbeat(
        self,
        worker: WorkerRecord,
        *,
        active_job_ids: list[str],
        now: datetime,
    ) -> list[tuple[list[str], str]]:
        notifications: list[tuple[list[str], str]] = []
        active_job_id_set = {str(job_id).strip() for job_id in active_job_ids if str(job_id).strip()}
        for job in self.jobs:
            if job.claimed_by_worker_id != worker.id or job.status not in self._worker_active_job_statuses():
                continue
            if job.id in active_job_id_set:
                job.lease_expires_at = now + timedelta(seconds=self._worker_job_lease_seconds())
                continue
            if job.lease_expires_at is not None and job.lease_expires_at > now:
                continue
            notification_payload = self._handle_interrupted_worker_job(
                job,
                now=now,
                reason="Worker không còn báo job này trong heartbeat sau khi đã qua grace window.",
            )
            if notification_payload is not None:
                notifications.append(notification_payload)
        self._refresh_queue_positions()
        return notifications

    def _job_username(self, job: RenderJobRecord) -> str:
        user_id = self._job_user_id(job)
        if user_id:
            try:
                return self._find_user(user_id).username
            except KeyError:
                pass
        return "demo-user"

    def get_user_dashboard_view(
        self,
        *,
        user_id: str,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        bootstrap = self.get_user_bootstrap(user_id)
        jobs_response = self.get_user_jobs(user_id)
        now = self._now()
        browser_workers = self._workspace_workers_for_user(bootstrap.user)
        has_assigned_worker = bool(browser_workers)
        active_browser_session = self._active_browser_session_for_user(user_id)
        active_browser_worker_id = active_browser_session.target_worker_id if active_browser_session else ""
        browser_worker = next((worker for worker in browser_workers if worker.id == active_browser_worker_id), None)
        if browser_worker is None and len(browser_workers) == 1:
            browser_worker = browser_workers[0]
        browser_session_enabled = any(
            worker.browser_session_enabled and str(worker.public_base_url or "").strip()
            for worker in browser_workers
        )
        browser_session_payload = None
        if active_browser_session:
            self._sync_browser_session_details(active_browser_session)
            browser_session_payload = self._browser_session_response(active_browser_session).model_dump(mode="json")

        visible_channel_ids = {channel.id for channel in bootstrap.channels}
        browser_worker_cards: list[dict[str, Any]] = []
        for index, worker in enumerate(browser_workers, start=1):
            worker_id = str(worker.id or "").strip()
            channel_count = len(
                [
                    channel
                    for channel in self.channels
                    if channel.id in visible_channel_ids and channel.worker_id == worker_id
                ]
            )
            is_browser_ready = bool(worker.browser_session_enabled and str(worker.public_base_url or "").strip())
            worker_status_key = str(worker.status or "").strip().lower()
            is_online = worker_status_key == "online"
            is_busy = worker_status_key == "busy"
            browser_worker_cards.append(
                {
                    "id": worker_id,
                    "name": self._resolve_worker_display_name(worker_id),
                    "label": f"VPS-{index:03d}",
                    "channel_count": channel_count,
                    "channel_text": f"{channel_count} kênh",
                    "status_key": "busy" if is_busy else ("online" if is_online else str(worker.status or "offline")),
                    "status_text": "Đang xử lý" if is_busy else ("Sẵn sàng" if is_browser_ready else ("Offline" if not is_online else "Chưa sẵn sàng")),
                    "status_dot_class": self._worker_status_dot_class("busy" if is_busy else ("online" if is_browser_ready else worker.status)),
                    "disabled": not is_browser_ready,
                }
            )

        queued_jobs = [job for job in jobs_response.jobs if job.status in {"pending", "queueing"}]
        rendering_jobs = [job for job in jobs_response.jobs if job.status == "rendering"]
        uploading_jobs = [job for job in jobs_response.jobs if job.status == "uploading"]
        failed_jobs = [job for job in jobs_response.jobs if job.status == "error"]
        claim_positions = self._upload_claim_position_by_job_id(now=now)

        channels = [
            {"value": "", "label": "-- Chọn kênh --", "meta": "", "avatar": "", "avatar_class": "", "is_active": True, "is_placeholder": True}
        ]
        for channel in bootstrap.channels:
            channels.append(
                {
                    "value": channel.id,
                    "label": channel.name,
                    "meta": channel.channel_id,
                    "avatar": self._initials(channel.name),
                    "avatar_url": channel.avatar_url,
                    "avatar_class": "bg-emerald-800 text-white",
                    "is_active": False,
                    "is_placeholder": False,
                }
            )

        connected_channels: list[dict[str, Any]] = []
        worker_status_by_id: dict[str, str] = {
            str(worker.id or "").strip(): str(worker.status or "").strip().lower()
            for worker in browser_workers
        }
        worker_channel_count_by_id: dict[str, int] = {}
        for channel in bootstrap.channels:
            worker_key = str(channel.worker_id or channel.worker_name or "").strip()
            if not worker_key:
                continue
            worker_channel_count_by_id[worker_key] = worker_channel_count_by_id.get(worker_key, 0) + 1
        connected_channel_source = sorted(
            bootstrap.channels,
            key=lambda channel: (
                self._natural_sort_text(self._resolve_channel_worker_display_name(channel)),
                self._natural_sort_text(channel.worker_id or channel.worker_name or ""),
                self._natural_sort_text(channel.name),
                self._natural_sort_text(channel.channel_id),
            ),
        )
        for channel in connected_channel_source:
            worker_label = self._resolve_channel_worker_display_name(channel)
            worker_key = str(channel.worker_id or channel.worker_name or "").strip()
            worker_status = worker_status_by_id.get(worker_key, "offline")
            worker_channel_count = worker_channel_count_by_id.get(worker_key, 0)
            connected_channels.append(
                {
                    "id": channel.id,
                    "title": channel.name,
                    "channel_id": channel.channel_id,
                    "worker_label": worker_label,
                    "worker_status_dot_class": self._worker_status_dot_class(worker_status),
                    "worker_channel_count": worker_channel_count,
                    "search_text": " ".join(
                        part
                        for part in [
                            str(channel.name or "").strip(),
                            str(channel.channel_id or "").strip(),
                            str(worker_label or "").strip(),
                            f"{worker_channel_count} kênh" if worker_channel_count else "",
                        ]
                        if part
                    ).lower(),
                    "public_url": f"https://www.youtube.com/channel/{channel.channel_id}",
                    "avatar": self._initials(channel.name),
                    "avatar_url": channel.avatar_url,
                    "avatar_class": "bg-emerald-800 text-white",
                    "avatar_small": False,
                }
            )

        render_jobs: list[dict[str, Any]] = []
        for index, job in enumerate(jobs_response.jobs, start=1):
            is_drive = job.source_mode == "drive"
            preview = self._resolve_job_preview(job)
            status_view = self._job_user_status_view(job)
            progress_view = self._job_user_progress_view(job)
            scheduled_waiting = bool(job.scheduled_at and job.status in {"pending", "queueing"} and job.scheduled_at > now)
            scheduled_wait_at = self._format_compact_datetime(job.scheduled_at) if scheduled_waiting else ""
            worker_display_name = self._resolve_job_worker_display_name(job)
            bot_meta = job.worker_name if worker_display_name != (job.worker_name or "-") else (job.manager_name or "-")
            render_jobs.append(
                {
                    "id": job.id,
                    "index": index,
                    "kind": "Drive" if is_drive else "Upload",
                    "kind_class": "text-sky-600" if is_drive else "text-violet-600",
                    "title": job.title,
                    "meta": f"{job.source_label} • render {self._format_render_duration(job.time_render_string)} • ",
                    "job_id": job.id,
                    "description": (job.description or "").strip(),
                    "channel_avatar": self._initials(job.channel_name),
                    "channel_avatar_url": job.channel_avatar_url,
                    "channel_name": job.channel_name,
                    "queue_label": (
                        f"Queue #{claim_positions[job.id]}"
                        if job.id in claim_positions
                        else ("Hẹn giờ" if scheduled_waiting else f"Job #{index}")
                    ),
                    "bot": worker_display_name,
                    "bot_meta": bot_meta,
                    "owner": job.manager_name or "-",
                    "progress": f"{job.progress}%",
                    "progress_mode": progress_view["mode"],
                    "download_progress": progress_view["download"],
                    "render_progress": progress_view["render"],
                    "upload_progress": progress_view["upload"],
                    "created_at": self._format_datetime(job.created_at),
                    "render_at": status_view["render_at"],
                    "uploaded_at": status_view["upload_at"],
                    "scheduled_waiting": scheduled_waiting,
                    "scheduled_wait_at": scheduled_wait_at,
                    "status": status_view["label"],
                    "status_key": job.status,
                    "status_class": status_view["class"],
                    "progress_text_class": status_view["progress_text_class"],
                    "progress_bar_class": status_view["progress_bar_class"],
                    "error_message": (job.error_message or "").strip(),
                    "icon": "hard-drive-download" if is_drive else None,
                    "icon_class": "text-sky-600",
                    "preview_kind": preview["kind"] if preview else None,
                    "preview_url": preview["url"] if preview else None,
                    "preview_text": (job.source_file_name or "Preview")[:16],
                    "can_cancel": job.can_cancel,
                }
            )

        is_admin_shell = bootstrap.user.role in {"admin", "manager"}

        return {
            "page_title": "Youtube Lush",
            "app_name": "Youtube Lush",
            "admin_shell": is_admin_shell,
            "active_page": "render_workspace",
            "nav_items": (
                self._admin_nav_items()
                if is_admin_shell
                else [
                    {
                        "key": "render_workspace",
                        "label": "Điều phối Upload",
                        "href": "/app",
                        "icon": "layers",
                    },
                    {
                        "key": "live_workspace",
                        "label": "Điều phối Live Stream",
                        "href": "/app/live",
                        "icon": "radio-tower",
                    },
                ]
            ),
            "workspace_label": {
                "admin": "Admin workspace",
                "manager": "Manager workspace",
            }.get(bootstrap.user.role, "User workspace"),
            "user_name": bootstrap.user.display_name,
            "user_role": {
                "admin": "control plane",
                "manager": "manager scope",
            }.get(bootstrap.user.role, bootstrap.user.manager_name or bootstrap.user.role),
            "logout_path": "/admin/logout" if is_admin_shell else "/logout",
            "upload_capabilities": bootstrap.upload_capabilities.model_dump(mode="json"),
            "channel_connect_mode": "browser_session",
            "channel_connect_blocked_message": (
                "Bạn chưa được manager cấp VPS. Hãy liên hệ manager trước khi thêm kênh."
                if bootstrap.user.role == "user" and not has_assigned_worker
                else (
                    "Bạn chưa có VPS nào trong scope quản lý để mở phiên thêm kênh."
                    if bootstrap.user.role == "manager" and not has_assigned_worker
                    else (
                        "Bạn chưa tự cấp VPS cho tài khoản admin này. Hãy vào Danh sách BOT rồi gán VPS cho chính mình."
                        if bootstrap.user.role == "admin" and not has_assigned_worker
                        else None
                    )
                )
            ),
            "browser_session_enabled": browser_session_enabled,
            "browser_worker_count": len(browser_workers),
            "browser_workers": browser_worker_cards,
            "browser_worker": {
                "id": browser_worker.id,
                "name": self._resolve_worker_display_name(browser_worker.id),
                "public_base_url": browser_worker.public_base_url,
            }
            if browser_worker
            else None,
            "browser_session": browser_session_payload,
            "notice": notice,
            "notice_level": notice_level,
            "kpis": [
                {"label": "Kênh đã thêm", "icon": "radio", "icon_class": "text-emerald-500", "value": bootstrap.oauth.connected_count, "accent": "Online", "accent_class": "text-emerald-600", "accent_badge_class": "border-emerald-200 bg-emerald-50 text-emerald-700", "value_class": "text-emerald-600", "bar_class": "bg-emerald-500"},
                {"label": "Đang chờ xử lý", "icon": "layers", "icon_class": "text-amber-500", "value": len(queued_jobs), "accent": "Chờ", "accent_class": "text-amber-600", "accent_badge_class": "border-amber-200 bg-amber-50 text-amber-700", "value_class": "text-amber-600", "bar_class": "bg-amber-500"},
                {"label": "Đang render", "icon": "loader", "icon_class": "text-brand-500", "value": len(rendering_jobs), "accent": "Render", "accent_class": "text-brand-600", "accent_badge_class": "border-indigo-200 bg-indigo-50 text-indigo-700", "value_class": "text-brand-600", "bar_class": "bg-brand-500"},
                {"label": "Đang upload", "icon": "cloud-upload", "icon_class": "text-sky-500", "value": len(uploading_jobs), "accent": "Upload", "accent_class": "text-sky-500", "accent_badge_class": "border-sky-200 bg-sky-50 text-sky-700", "value_class": "text-slate-900" if len(uploading_jobs) == 0 else "text-sky-500", "bar_class": "bg-sky-400"},
                {"label": "Xử lý lỗi", "icon": "shield-alert", "icon_class": "text-rose-500", "value": len(failed_jobs), "accent": "Lỗi", "accent_class": "text-rose-500", "accent_badge_class": "border-rose-200 bg-rose-50 text-rose-700", "value_class": "text-slate-900" if len(failed_jobs) == 0 else "text-rose-500", "bar_class": "bg-rose-400"},
            ],
            "render_config": {"title": "Cấu hình render", "description": "Thiết lập nguồn đầu vào, file loop và kênh xuất bản cho job mới."},
            "quick_settings": {"video_title": "", "description_text": "", "duration": "03:30:00", "schedule_at": (now + timedelta(hours=1)).strftime("%d/%m/%Y %H:%M")},
            "channels": channels,
            "connected_channels": connected_channels,
            "render_tabs": [
                {"label": "Danh sách render", "count": len(render_jobs), "active": True},
                {"label": "Đang xử lý", "count": len([job for job in render_jobs if job["status"] in {"Đang render", "Đang upload", "Đang tải nguồn"}]), "active": False},
            ],
            "render_jobs": render_jobs,
            "render_summary": f"Hiển thị 1 đến {len(render_jobs)} trong số {len(render_jobs)} kết quả" if render_jobs else "Chưa có job nào trong hàng đợi",
        }

    def _build_live_worker_picker_cards(self, workers: list[WorkerRecord]) -> list[dict[str, Any]]:
        return [
            {
                "id": worker.id,
                "name": self._resolve_live_worker_display_name(worker.id),
                "manager_name": worker.manager_name or "-",
                "role_label": "",
            }
            for worker in workers
        ]

    def _build_live_form_values_from_stream(self, stream: LiveStreamRecord) -> dict[str, Any]:
        return {
            "stream_id": stream.id,
            "stream_name": stream.stream_name,
            "video_url": stream.video_url,
            "audio_url": stream.audio_url or "",
            "stream_key": stream.stream_key,
            "backup_delay_minutes": str(stream.backup_delay_minutes),
            "primary_worker_id": stream.primary_worker_id,
            "backup_worker_id": stream.backup_worker_id or "",
            "start_at": self._format_form_datetime(stream.start_time_live) if stream.start_time_live else "",
            "end_at": self._format_form_datetime(stream.end_time_live) if stream.end_time_live else "",
        }

    def _live_stream_detail_payload(self, stream: LiveStreamRecord) -> dict[str, Any]:
        return {
            "id": stream.id,
            "title": stream.stream_name,
            "owner_username": stream.owner_username,
            "manager_name": stream.manager_name or "-",
            "primary_worker_name": stream.primary_worker_name or self._resolve_live_worker_display_name(stream.primary_worker_id),
            "backup_worker_name": self._live_backup_worker_display_name(stream.backup_worker_id, stream.backup_worker_name) or "Không dùng backup",
            "stream_key": stream.stream_key,
            "video_url": stream.video_url,
            "audio_url": stream.audio_url or "Không có",
            "live_label": stream.live_label,
            "status_label": self._live_status_presentation(stream.status)[0],
            "status_class": self._live_status_presentation(stream.status)[1],
            "log_label": stream.log_label,
            "error_message": self._summarize_live_error_message(stream.error_message),
            "start_at": self._format_full_datetime(stream.start_time_live) if stream.start_time_live else "-",
            "end_at": self._format_full_datetime(stream.end_time_live) if stream.end_time_live else ("Live 24/7" if stream.is_forever else "-"),
            "backup_delay_minutes": stream.backup_delay_minutes,
            "updated_at": self._format_full_datetime(stream.updated_at),
        }

    def get_user_live_workspace_view(
        self,
        *,
        user_id: str,
        notice: str | None = None,
        notice_level: str = "success",
        live_form_values: dict[str, Any] | None = None,
        editing_stream_id: str | None = None,
        detail_stream_id: str | None = None,
    ) -> dict[str, Any]:
        bootstrap = self.get_user_bootstrap(user_id)
        user = bootstrap.user
        live_context_query, live_context_suffix = self._live_workspace_scope_query()
        resolved_notice = notice
        resolved_notice_level = notice_level
        assigned_workers = self._workspace_live_workers_for_user(user)
        primary_workers = self._assigned_live_workers_for_user(user, role="primary")
        backup_workers = self._assigned_live_workers_for_user(user, role="backup")
        live_ready = len([worker for worker in assigned_workers if worker.status in {"online", "busy"}])
        total_workers = len(assigned_workers)
        live_records = self._live_streams_for_user(user)
        active_live_count = len(
            [record for record in live_records if self._effective_live_runtime_stream(record).status == "streaming"]
        )
        scheduled_live_count = len(
            [
                record
                for record in live_records
                if self._effective_live_runtime_stream(record).status in self._live_pre_stream_statuses()
            ]
        )
        backup_live_count = len([record for record in live_records if record.backup_worker_id])
        editing_stream = None
        if editing_stream_id:
            candidate_stream = self.get_live_stream(editing_stream_id, viewer_role=user.role, viewer_id=user.id)
            if self._is_live_stream_runtime_locked(candidate_stream):
                if not resolved_notice:
                    resolved_notice = "Luồng đang được worker xử lý hoặc đang live. Hãy dùng Dừng trước khi chỉnh sửa."
                    resolved_notice_level = "error"
            else:
                editing_stream = candidate_stream
        detail_stream = None
        if detail_stream_id:
            detail_stream = self.get_live_stream(detail_stream_id, viewer_role=user.role, viewer_id=user.id)
        effective_form_values = live_form_values or (self._build_live_form_values_from_stream(editing_stream) if editing_stream else None)
        live_form_state = self._build_live_form_state(effective_form_values)
        is_admin_shell = user.role in {"admin", "manager"}

        return {
            "page_title": "Điều phối live",
            "app_name": "Youtube Lush",
            "admin_shell": is_admin_shell,
            "active_page": "live_workspace",
            "nav_items": (
                self._admin_nav_items("live")
                if is_admin_shell
                else [
                    {
                        "key": "render_workspace",
                        "label": "Điều phối Upload",
                        "href": "/app",
                        "icon": "layers",
                    },
                    {
                        "key": "live_workspace",
                        "label": "Điều phối Live Stream",
                        "href": "/app/live",
                        "icon": "radio-tower",
                    },
                ]
            ),
            "workspace_label": {
                "admin": "Admin workspace",
                "manager": "Manager workspace",
            }.get(user.role, "User workspace"),
            "user_name": user.display_name,
            "user_role": {
                "admin": "control plane",
                "manager": "manager scope",
            }.get(user.role, user.manager_name or user.role),
            "logout_path": "/admin/logout" if is_admin_shell else "/logout",
            "notice": resolved_notice,
            "notice_level": resolved_notice_level,
            "kpis": [
                {"label": "BOT được cấp", "icon": "server", "icon_class": "text-emerald-500", "value": total_workers, "accent": "Live + Backup", "accent_class": "text-emerald-600", "value_class": "text-emerald-600", "bar_class": "bg-emerald-400"},
                {"label": "BOT live sẵn sàng", "icon": "server-cog", "icon_class": "text-brand-500", "value": live_ready, "accent": f"{live_ready}/{total_workers} BOT", "accent_class": "text-brand-600", "value_class": "text-brand-600", "bar_class": "bg-brand-500"},
                {"label": "Đang live", "icon": "radio", "icon_class": "text-rose-500", "value": active_live_count, "accent": "Live", "accent_class": "text-rose-500", "value_class": "text-slate-900" if active_live_count == 0 else "text-rose-500", "bar_class": "bg-rose-400"},
                {"label": "Chờ lên lịch", "icon": "clock-3", "icon_class": "text-amber-500", "value": scheduled_live_count, "accent": "Lịch", "accent_class": "text-amber-600", "value_class": "text-slate-900" if scheduled_live_count == 0 else "text-amber-600", "bar_class": "bg-amber-400"},
                {"label": "BOT backup", "icon": "shield-check", "icon_class": "text-sky-500", "value": backup_live_count, "accent": "Fallback", "accent_class": "text-sky-500", "value_class": "text-slate-900" if backup_live_count == 0 else "text-sky-500", "bar_class": "bg-sky-400"},
            ],
            "live_config": {
                "title": "Live Config",
                "description": "Thiết lập BOT chính, BOT backup, media nguồn và lịch live.",
            },
            "live_form_action": "/app/live/update" if editing_stream else "/app/live/create",
            "live_form_user_id": "",
            "live_form_mode": "edit" if editing_stream else "create",
            "live_form_submit_label": "Cập nhật luồng live" if editing_stream else "Tạo luồng live",
            "live_form_reset_label": "Hủy chỉnh sửa" if editing_stream else "Đặt lại",
            "live_form_cancel_href": "/app/live" if editing_stream else "",
            "live_index_href": "/app/live",
            "live_delete_action": "/app/live/delete",
            "live_stop_action": "/app/live/stop",
            "live_context_query": live_context_query,
            "live_context_suffix": live_context_suffix,
            "live_owner_user_id": user.id,
            "live_dashboard_api_href": "/api/user/dashboard/live",
            "live_telegram": self._live_telegram_binding_snapshot(user.id),
            "live_form": live_form_state,
            "primary_live_workers": self._build_live_worker_picker_cards(primary_workers),
            "backup_live_workers": self._build_live_worker_picker_cards(backup_workers),
            "live_workers": self._build_live_worker_picker_cards(assigned_workers),
            "editing_stream_id": editing_stream.id if editing_stream else "",
            "detail_stream": self._live_stream_detail_payload(detail_stream) if detail_stream else None,
            "live_tabs": [
                {"label": "Danh sách live", "count": len(live_records), "active": True},
                {"label": "Chờ lên lịch", "count": scheduled_live_count, "active": False},
            ],
            "live_stream_rows": self._build_live_stream_rows(live_records),
            "live_summary": (
                f"Hiển thị 1 đến {len(live_records)} trong {len(live_records)} kết quả"
                if live_records
                else "Chưa có luồng live nào trong danh sách"
            ),
        }

    @staticmethod
    def _live_workspace_payload(dashboard: dict[str, Any]) -> dict[str, Any]:
        return {
            "kpis": dashboard["kpis"],
            "live_tabs": dashboard["live_tabs"],
            "live_stream_rows": dashboard["live_stream_rows"],
            "live_summary": dashboard["live_summary"],
        }

    def _live_streams_for_user(self, user: UserSummary) -> list[LiveStreamRecord]:
        return sorted(
            [
                stream
                for stream in self.live_streams
                if stream.owner_user_id == user.id
                and self._is_visible_live_stream(stream)
            ],
            key=lambda item: (item.created_at or datetime.min, item.id),
            reverse=True,
        )

    def _build_live_form_state(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        state: dict[str, Any] = {
            "stream_name": "",
            "video_url": "",
            "audio_url": "",
            "stream_key": "",
            "backup_delay_minutes": 0,
            "primary_worker_id": "",
            "backup_worker_id": "",
            "start_at": "",
            "end_at": "",
        }
        if not values:
            return state
        state.update(
            {
                "stream_name": str(values.get("stream_name") or "").strip(),
                "video_url": str(values.get("video_url") or "").strip(),
                "audio_url": str(values.get("audio_url") or "").strip(),
                "stream_key": str(values.get("stream_key") or "").strip(),
                "backup_delay_minutes": str(values.get("backup_delay_minutes") or "0").strip() or "0",
                "primary_worker_id": str(values.get("primary_worker_id") or "").strip(),
                "backup_worker_id": str(values.get("backup_worker_id") or "").strip(),
                "start_at": str(values.get("start_at") or "").strip(),
                "end_at": str(values.get("end_at") or "").strip(),
            }
        )
        return state

    def resolve_live_owner_from_primary_worker(
        self,
        primary_worker_id: str,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> UserSummary:
        normalized_worker_id = str(primary_worker_id or "").strip()
        if not normalized_worker_id:
            raise ValueError("BOT chính là bắt buộc.")
        assigned_users = self._assigned_live_users_for_worker(normalized_worker_id, role="primary")
        if not assigned_users:
            raise ValueError("BOT chính chưa được cấp cho tài khoản nào để lưu dữ liệu live stream.")
        scoped_users = []
        for owner in assigned_users:
            try:
                self._assert_live_stream_owner_scope(owner, viewer_role=viewer_role, viewer_id=viewer_id)
            except ValueError:
                continue
            scoped_users.append(owner)
        if not scoped_users:
            raise ValueError("Không có quyền thao tác luồng live của tài khoản này.")
        if len(scoped_users) > 1:
            raise ValueError("BOT chính đang được cấp cho nhiều user. Form hiện tại chưa xác định được tài khoản sở hữu luồng live.")
        return scoped_users[0]

    def list_live_streams(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        manager_ids: list[str] | None = None,
        owner_user_id: str | None = None,
    ) -> list[LiveStreamRecord]:
        normalized_owner_user_id = str(owner_user_id or "").strip()
        if normalized_owner_user_id:
            owner = self._require_workspace_user(normalized_owner_user_id)
            self._assert_live_stream_owner_scope(owner, viewer_role=viewer_role, viewer_id=viewer_id)
            return [
                deepcopy(stream)
                for stream in self._live_streams_for_user(owner)
            ]
        return [
            deepcopy(stream)
            for stream in self._scoped_live_streams(
                viewer_role=viewer_role,
                viewer_id=viewer_id,
                manager_ids=manager_ids,
            )
        ]

    def _find_live_stream_optional(self, stream_id: str) -> LiveStreamRecord | None:
        normalized_stream_id = str(stream_id or "").strip()
        if not normalized_stream_id:
            return None
        return next((stream for stream in self.live_streams if stream.id == normalized_stream_id), None)

    def _find_live_stream(self, stream_id: str) -> LiveStreamRecord:
        normalized_stream_id = str(stream_id or "").strip()
        for stream in self.live_streams:
            if stream.id == normalized_stream_id:
                return stream
        raise KeyError(stream_id)

    def _find_visible_live_stream(self, stream_id: str) -> LiveStreamRecord:
        stream = self._find_live_stream(stream_id)
        if self._is_runtime_backup_clone(stream):
            raise KeyError(stream_id)
        return stream

    def _assert_live_stream_owner_scope(
        self,
        owner: UserSummary,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> None:
        normalized_viewer_id = str(viewer_id or "").strip()
        if viewer_role == "admin":
            return
        if viewer_role == "manager":
            if owner.role == "manager" and owner.id == normalized_viewer_id:
                return
            if owner.role == "user" and self._resolved_user_manager_id(owner) == normalized_viewer_id:
                return
            raise ValueError("Không có quyền thao tác luồng live của tài khoản này.")
        if owner.id != normalized_viewer_id:
            raise ValueError("Không có quyền thao tác luồng live của tài khoản này.")

    def _normalize_live_text(self, value: str | None, *, field_label: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{field_label} là bắt buộc.")
        return normalized

    def _normalize_live_source_url(
        self,
        value: str | None,
        *,
        field_label: str,
        required: bool,
    ) -> str | None:
        normalized = str(value or "").strip()
        if not normalized:
            if required:
                raise ValueError(f"{field_label} là bắt buộc.")
            return None

        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"{field_label} phải là link HTTP hoặc HTTPS hợp lệ.")

        host = (parsed.hostname or "").lower()
        if host in {"drive.google.com", "docs.google.com"} and self._extract_google_drive_file_id(normalized) is None:
            raise ValueError(f"{field_label} Google Drive chưa đúng định dạng chia sẻ file.")
        return normalized

    def _normalize_live_optional_text(self, value: str | None) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def _normalize_backup_delay_minutes(self, value: int | str | None) -> int:
        try:
            delay_minutes = int(value if value is not None else 0)
        except (TypeError, ValueError):
            raise ValueError("Delay backup phải là số nguyên hợp lệ.")
        if delay_minutes < 0:
            raise ValueError("Delay backup không được âm.")
        return delay_minutes

    @staticmethod
    def _fixed_live_worker_thread_limit() -> int:
        return 5

    def _normalize_live_worker_threads(self, requested_threads: int | str | None) -> int:
        try:
            requested = int(requested_threads if requested_threads is not None else 1)
        except (TypeError, ValueError):
            requested = 1
        return max(1, min(self._fixed_live_worker_thread_limit(), requested))

    def _live_link_allocated_threads(self, link: dict[str, Any]) -> int:
        return self._normalize_live_worker_threads(
            link.get("allocated_threads") if link.get("allocated_threads") is not None else link.get("threads")
        )

    def _live_worker_allocated_threads(self, worker_id: str, *, role: str | None = None) -> int:
        normalized_worker_id = str(worker_id or "").strip()
        normalized_role = self._normalize_live_assignment_role(role) if role is not None else None
        total = 0
        for link in self.live_user_worker_links:
            if str(link.get("worker_id") or "").strip() != normalized_worker_id:
                continue
            if normalized_role is not None and self._normalize_live_assignment_role(
                link.get("live_role"),
                fallback_note=link.get("note"),
            ) != normalized_role:
                continue
            total += self._live_link_allocated_threads(link)
        return total

    def _live_worker_total_granted_threads(self, worker_id: str, *, role: str | None = None) -> int:
        """
        Canonical total for BOT table live/backup rows.

        "Tổng" means the sum of stream slots granted to the users assigned to this VPS,
        not the VPS capacity/thread ceiling itself.
        """
        return self._live_worker_allocated_threads(worker_id, role=role)

    def _live_user_allocated_threads(self, user_id: str, *, role: str | None = None) -> int:
        return sum(
            self._live_link_allocated_threads(link)
            for link in self._assigned_live_worker_links_for_user(user_id, role=role)
        )

    def _live_worker_default_assignment_threads(self, worker_id: str) -> int:
        allocations = {
            self._live_link_allocated_threads(link)
            for link in self.live_user_worker_links
            if str(link.get("worker_id") or "").strip() == str(worker_id or "").strip()
        }
        if not allocations:
            worker = self._find_live_worker_optional(worker_id)
            if worker is not None:
                return self._effective_live_worker_thread_limit(worker)
            return self._fixed_live_worker_thread_limit()
        if len(allocations) == 1:
            return next(iter(allocations))
        return max(allocations)

    def _effective_live_worker_thread_limit(self, worker: WorkerRecord) -> int:
        configured = int(worker.capacity or self._fixed_live_worker_thread_limit())
        return self._normalize_live_worker_threads(configured)

    def _overlapping_visible_live_streams(
        self,
        *,
        worker_id: str | None,
        start_time_live: datetime | None,
        end_time_live: datetime | None,
        is_forever: bool,
        excluding_stream_id: str | None = None,
        owner_user_id: str | None = None,
        role: str | None = None,
    ) -> list[LiveStreamRecord]:
        normalized_worker_id = str(worker_id or "").strip()
        normalized_owner_user_id = str(owner_user_id or "").strip()
        normalized_role = self._normalize_live_assignment_role(role) if role is not None else None
        matches: list[LiveStreamRecord] = []
        for stream in self.live_streams:
            if not self._is_visible_live_stream(stream):
                continue
            if excluding_stream_id and stream.id == excluding_stream_id:
                continue
            normalized_status = str(stream.status or "").strip().lower()
            if normalized_status in {"stopped", "ended", "error"}:
                continue
            if normalized_owner_user_id and stream.owner_user_id != normalized_owner_user_id:
                continue
            if normalized_role == "primary":
                if stream.primary_worker_id != normalized_worker_id:
                    continue
            elif normalized_role == "backup":
                if str(stream.backup_worker_id or "").strip() != normalized_worker_id:
                    continue
            elif normalized_worker_id:
                if (
                    stream.primary_worker_id != normalized_worker_id
                    and str(stream.backup_worker_id or "").strip() != normalized_worker_id
                ):
                    continue
            if not self._live_schedule_windows_overlap(
                start_time_live=start_time_live,
                end_time_live=end_time_live,
                is_forever=is_forever,
                other_start_time_live=stream.start_time_live,
                other_end_time_live=stream.end_time_live,
                other_is_forever=stream.is_forever,
            ):
                continue
            matches.append(stream)
        return matches

    def _assert_live_worker_quota_available(
        self,
        *,
        owner: UserSummary,
        worker_id: str | None,
        role: str,
        field_label: str,
        start_time_live: datetime | None,
        end_time_live: datetime | None,
        is_forever: bool,
        excluding_stream_id: str | None = None,
    ) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return
        worker = self._find_live_worker(normalized_worker_id)
        worker_name = self._resolve_live_worker_display_name(normalized_worker_id)
        allocated_threads = self._live_user_worker_allocated_threads(owner.id, normalized_worker_id, role=role)
        if allocated_threads < 1:
            raise ValueError(f"{field_label} {worker_name} chưa có quota được cấp cho tài khoản này.")

        overlapping_user_streams = self._overlapping_visible_live_streams(
            worker_id=normalized_worker_id,
            owner_user_id=owner.id,
            role=role,
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_forever=is_forever,
            excluding_stream_id=excluding_stream_id,
        )
        if len(overlapping_user_streams) >= allocated_threads:
            raise ValueError(
                f"{field_label} {worker_name} chỉ còn {allocated_threads} luồng được cấp cho tài khoản này, "
                f"nhưng đã có {len(overlapping_user_streams)}/{allocated_threads} luồng trùng lịch."
            )

        worker_capacity = self._effective_live_worker_thread_limit(worker)
        overlapping_worker_streams = self._overlapping_visible_live_streams(
            worker_id=normalized_worker_id,
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_forever=is_forever,
            excluding_stream_id=excluding_stream_id,
        )
        if len(overlapping_worker_streams) >= worker_capacity:
            raise ValueError(
                f"{field_label} {worker_name} đã kín {len(overlapping_worker_streams)}/{worker_capacity} luồng "
                "trong khoảng thời gian trùng nhau."
            )

    @staticmethod
    def _resolve_live_schedule_window(
        *,
        start_time_live: datetime | None,
        end_time_live: datetime | None,
        is_forever: bool,
    ) -> tuple[datetime, datetime]:
        start_at = start_time_live or datetime.min
        end_at = datetime.max if is_forever or end_time_live is None else end_time_live
        return start_at, end_at

    @classmethod
    def _live_schedule_windows_overlap(
        cls,
        *,
        start_time_live: datetime | None,
        end_time_live: datetime | None,
        is_forever: bool,
        other_start_time_live: datetime | None,
        other_end_time_live: datetime | None,
        other_is_forever: bool,
    ) -> bool:
        start_a, end_a = cls._resolve_live_schedule_window(
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_forever=is_forever,
        )
        start_b, end_b = cls._resolve_live_schedule_window(
            start_time_live=other_start_time_live,
            end_time_live=other_end_time_live,
            is_forever=other_is_forever,
        )
        return start_a < end_b and start_b < end_a

    def _assert_live_worker_schedule_available(
        self,
        *,
        worker_id: str | None,
        field_label: str,
        start_time_live: datetime | None,
        end_time_live: datetime | None,
        is_forever: bool,
        excluding_stream_id: str | None = None,
    ) -> None:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return
        for stream in self.live_streams:
            if not self._is_visible_live_stream(stream):
                continue
            if excluding_stream_id and stream.id == excluding_stream_id:
                continue
            normalized_status = str(stream.status or "").strip().lower()
            if normalized_status in {"stopped", "ended", "error"}:
                continue
            worker_roles: list[str] = []
            if stream.primary_worker_id == normalized_worker_id:
                worker_roles.append("BOT chính")
            if str(stream.backup_worker_id or "").strip() == normalized_worker_id:
                worker_roles.append("BOT backup")
            if not worker_roles:
                continue
            if not self._live_schedule_windows_overlap(
                start_time_live=start_time_live,
                end_time_live=end_time_live,
                is_forever=is_forever,
                other_start_time_live=stream.start_time_live,
                other_end_time_live=stream.end_time_live,
                other_is_forever=stream.is_forever,
            ):
                continue
            worker_name = self._resolve_live_worker_display_name(normalized_worker_id)
            conflict_roles = ", ".join(worker_roles)
            raise ValueError(
                f"{field_label} {worker_name} đang được dùng làm {conflict_roles} cho luồng "
                f"'{stream.stream_name}' trong khoảng thời gian trùng nhau."
            )

    def _resolve_live_owner_manager_scope(
        self,
        owner: UserSummary,
        *,
        primary_worker: WorkerRecord,
    ) -> tuple[str | None, str | None]:
        if owner.role == "user":
            return (
                self._resolved_user_manager_id(owner) or primary_worker.manager_id,
                owner.manager_name or primary_worker.manager_name or None,
            )
        if owner.role == "manager":
            return owner.id, owner.username
        return primary_worker.manager_id, primary_worker.manager_name or owner.username

    def _validate_live_worker_for_owner(
        self,
        owner: UserSummary,
        worker_id: str,
        *,
        field_label: str,
        role: str | None = None,
    ) -> WorkerRecord:
        available_workers = self._assigned_live_workers_for_user(owner, role=role) if role else self._workspace_live_workers_for_user(owner)
        normalized_worker_id = str(worker_id or "").strip()
        selected_worker = next((worker for worker in available_workers if worker.id == normalized_worker_id), None)
        if selected_worker is not None:
            return selected_worker
        raise ValueError(f"{field_label} không nằm trong danh sách BOT đã cấp cho tài khoản này.")

    def _derive_live_label(
        self,
        *,
        start_time_live: datetime | None,
        end_time_live: datetime | None,
        is_forever: bool,
    ) -> str:
        if is_forever:
            return "Live 24/7"
        if start_time_live and end_time_live and end_time_live >= start_time_live:
            duration = end_time_live - start_time_live
            total_seconds = int(duration.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            if remainder > 0:
                hours += 1
            return f"Live {max(hours, 1)}h"
        return "Live thường"

    def _derive_live_status_defaults(
        self,
        *,
        start_time_live: datetime | None,
        is_live_now: bool,
        is_forever: bool,
        status_override: str | None = None,
        log_override: str | None = None,
    ) -> tuple[str, str]:
        if status_override:
            normalized_status = str(status_override or "").strip().lower()
        elif is_live_now:
            normalized_status = "streaming"
        elif start_time_live or is_forever:
            normalized_status = "scheduled"
        else:
            normalized_status = "draft"

        if log_override:
            return normalized_status, str(log_override).strip()
        if normalized_status == "streaming":
            return normalized_status, "Đang live"
        if normalized_status == "downloading":
            return normalized_status, "Đang tải media"
        if normalized_status == "preparing":
            return normalized_status, "Chuẩn bị luồng"
        if normalized_status == "waiting":
            return normalized_status, "Chờ đến giờ"
        if normalized_status == "scheduled":
            return normalized_status, "Lên lịch"
        if normalized_status == "disconnected":
            return normalized_status, "Mất kết nối"
        if normalized_status == "error":
            return normalized_status, "Lỗi"
        if normalized_status == "ended":
            return normalized_status, "Kết thúc"
        if normalized_status == "stopped":
            return normalized_status, "Đã dừng"
        return normalized_status, "Khởi tạo"

    def _validate_live_schedule(
        self,
        *,
        start_time_live: datetime | None,
        end_time_live: datetime | None,
        is_live_now: bool,
        is_forever: bool,
    ) -> tuple[datetime | None, datetime | None]:
        if not is_live_now and start_time_live is None:
            raise ValueError("Thời gian bắt đầu là bắt buộc nếu không chọn live ngay.")
        if is_forever:
            return start_time_live, None
        if start_time_live and end_time_live and end_time_live < start_time_live:
            raise ValueError("Thời gian kết thúc phải lớn hơn hoặc bằng thời gian bắt đầu.")
        return start_time_live, end_time_live

    def create_live_stream(
        self,
        *,
        owner_user_id: str,
        stream_name: str,
        primary_worker_id: str,
        stream_key: str,
        video_url: str,
        audio_url: str | None = None,
        backup_worker_id: str | None = None,
        backup_delay_minutes: int | str | None = 0,
        start_time_live: datetime | None = None,
        end_time_live: datetime | None = None,
        is_live_now: bool = False,
        is_forever: bool = False,
        platform: str = "youtube_rtmp",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        status: str | None = None,
        log_label: str | None = None,
    ) -> LiveStreamRecord:
        owner = self._require_workspace_user(owner_user_id)
        self._assert_live_stream_owner_scope(owner, viewer_role=viewer_role, viewer_id=viewer_id)
        normalized_name = self._normalize_live_text(stream_name, field_label="Tên luồng live")
        normalized_stream_key = self._normalize_live_text(stream_key, field_label="Stream key")
        normalized_video_url = self._normalize_live_source_url(
            video_url,
            field_label="Link video nguồn",
            required=True,
        )
        normalized_audio_url = self._normalize_live_source_url(
            audio_url,
            field_label="Link audio",
            required=False,
        )
        primary_worker = self._validate_live_worker_for_owner(owner, primary_worker_id, field_label="BOT chính", role="primary")
        backup_worker: WorkerRecord | None = None
        normalized_backup_worker_id = str(backup_worker_id or "").strip() or None
        if normalized_backup_worker_id:
            if normalized_backup_worker_id == primary_worker.id:
                raise ValueError("BOT backup phải khác BOT chính.")
            backup_worker = self._validate_live_worker_for_owner(owner, normalized_backup_worker_id, field_label="BOT backup", role="backup")
        normalized_delay = self._normalize_backup_delay_minutes(backup_delay_minutes)
        start_time_live, end_time_live = self._validate_live_schedule(
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_live_now=is_live_now,
            is_forever=is_forever,
        )
        self._assert_live_worker_quota_available(
            owner=owner,
            worker_id=primary_worker.id,
            role="primary",
            field_label="BOT chính",
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_forever=is_forever,
        )
        self._assert_live_worker_quota_available(
            owner=owner,
            worker_id=backup_worker.id if backup_worker else None,
            role="backup",
            field_label="BOT backup",
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_forever=is_forever,
        )
        manager_id, manager_name = self._resolve_live_owner_manager_scope(owner, primary_worker=primary_worker)
        live_status, live_log_label = self._derive_live_status_defaults(
            start_time_live=start_time_live,
            is_live_now=is_live_now,
            is_forever=is_forever,
            status_override=status,
            log_override=log_label,
        )
        now = self._now(trim=False)
        stream = LiveStreamRecord(
            id=f"live-{uuid4().hex[:10]}",
            owner_user_id=owner.id,
            owner_username=owner.username,
            owner_display_name=owner.display_name,
            manager_id=manager_id,
            manager_name=manager_name,
            primary_worker_id=primary_worker.id,
            primary_worker_name=self._resolve_live_worker_display_name(primary_worker.id),
            primary_group=primary_worker.group or primary_worker.manager_name,
            backup_worker_id=backup_worker.id if backup_worker else None,
            backup_worker_name=(
                self._live_backup_worker_display_name(backup_worker.id)
                if backup_worker
                else None
            ),
            backup_group=backup_worker.group if backup_worker else None,
            stream_name=normalized_name,
            stream_key=normalized_stream_key,
            platform=str(platform or "youtube_rtmp").strip().lower() or "youtube_rtmp",
            video_url=normalized_video_url,
            audio_url=normalized_audio_url,
            live_label=self._derive_live_label(
                start_time_live=start_time_live,
                end_time_live=end_time_live,
                is_forever=is_forever,
            ),
            is_forever=is_forever,
            is_live_now=is_live_now,
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            backup_delay_minutes=normalized_delay,
            status=live_status,
            log_label=live_log_label,
            created_at=now,
            updated_at=now,
        )
        self.live_streams.append(stream)
        self._sync_live_backup_policy(now=now)
        self._save_state()
        self._notify_live_telegram_chat_ids(
            self._live_stream_recipient_chat_ids(stream),
            self._live_stream_created_message(stream),
        )
        return deepcopy(stream)

    def get_live_stream(
        self,
        stream_id: str,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> LiveStreamRecord:
        stream = self._find_visible_live_stream(stream_id)
        owner = self._require_workspace_user(stream.owner_user_id)
        self._assert_live_stream_owner_scope(owner, viewer_role=viewer_role, viewer_id=viewer_id)
        return deepcopy(stream)

    def update_live_stream(
        self,
        *,
        stream_id: str,
        stream_name: str,
        primary_worker_id: str,
        stream_key: str,
        video_url: str,
        audio_url: str | None = None,
        backup_worker_id: str | None = None,
        backup_delay_minutes: int | str | None = 0,
        start_time_live: datetime | None = None,
        end_time_live: datetime | None = None,
        is_live_now: bool = False,
        is_forever: bool = False,
        platform: str = "youtube_rtmp",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        status: str | None = None,
        log_label: str | None = None,
    ) -> LiveStreamRecord:
        stream = self._find_visible_live_stream(stream_id)
        owner = self._require_workspace_user(stream.owner_user_id)
        self._assert_live_stream_owner_scope(owner, viewer_role=viewer_role, viewer_id=viewer_id)
        self._assert_live_stream_editable(stream)
        restart_prestream_runtime = self._is_live_stream_prestream_active(stream)
        normalized_name = self._normalize_live_text(stream_name, field_label="Tên luồng live")
        normalized_stream_key = self._normalize_live_text(stream_key, field_label="Stream key")
        normalized_video_url = self._normalize_live_source_url(
            video_url,
            field_label="Link video nguồn",
            required=True,
        )
        normalized_audio_url = self._normalize_live_source_url(
            audio_url,
            field_label="Link audio",
            required=False,
        )
        primary_worker = self._validate_live_worker_for_owner(owner, primary_worker_id, field_label="BOT chính", role="primary")
        backup_worker: WorkerRecord | None = None
        normalized_backup_worker_id = str(backup_worker_id or "").strip() or None
        if normalized_backup_worker_id:
            if normalized_backup_worker_id == primary_worker.id:
                raise ValueError("BOT backup phải khác BOT chính.")
            backup_worker = self._validate_live_worker_for_owner(owner, normalized_backup_worker_id, field_label="BOT backup", role="backup")
        normalized_delay = self._normalize_backup_delay_minutes(backup_delay_minutes)
        start_time_live, end_time_live = self._validate_live_schedule(
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_live_now=is_live_now,
            is_forever=is_forever,
        )
        self._assert_live_worker_quota_available(
            owner=owner,
            worker_id=primary_worker.id,
            role="primary",
            field_label="BOT chính",
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_forever=is_forever,
            excluding_stream_id=stream.id,
        )
        self._assert_live_worker_quota_available(
            owner=owner,
            worker_id=backup_worker.id if backup_worker else None,
            role="backup",
            field_label="BOT backup",
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_forever=is_forever,
            excluding_stream_id=stream.id,
        )
        manager_id, manager_name = self._resolve_live_owner_manager_scope(owner, primary_worker=primary_worker)
        live_status, live_log_label = self._derive_live_status_defaults(
            start_time_live=start_time_live,
            is_live_now=is_live_now,
            is_forever=is_forever,
            status_override=status,
            log_override=log_label,
        )
        now = self._now(trim=False)

        stream.manager_id = manager_id
        stream.manager_name = manager_name
        stream.primary_worker_id = primary_worker.id
        stream.primary_worker_name = self._resolve_live_worker_display_name(primary_worker.id)
        stream.primary_group = primary_worker.group or primary_worker.manager_name
        stream.backup_worker_id = backup_worker.id if backup_worker else None
        stream.backup_worker_name = (
            self._live_backup_worker_display_name(backup_worker.id)
            if backup_worker
            else None
        )
        stream.backup_group = backup_worker.group if backup_worker else None
        stream.stream_name = normalized_name
        stream.stream_key = normalized_stream_key
        stream.platform = str(platform or "youtube_rtmp").strip().lower() or "youtube_rtmp"
        stream.video_url = normalized_video_url
        stream.audio_url = normalized_audio_url
        stream.live_label = self._derive_live_label(
            start_time_live=start_time_live,
            end_time_live=end_time_live,
            is_forever=is_forever,
        )
        stream.is_forever = is_forever
        stream.is_live_now = is_live_now
        stream.start_time_live = start_time_live
        stream.end_time_live = end_time_live
        stream.backup_delay_minutes = normalized_delay
        stream.status = live_status
        stream.log_label = live_log_label
        stream.status_message = None
        stream.progress = 0
        stream.error_message = None
        stream.claimed_by_worker_id = None
        stream.claimed_by_role = None
        stream.claimed_at = None
        stream.lease_expires_at = None
        self._reset_live_stream_attempt_timeline(stream)
        stream.first_streaming_started_at = None
        stream.disconnected_at = None
        stream.stop_requested_at = now if restart_prestream_runtime else None
        stream.ended_at = None
        stream.updated_at = now
        self._sync_live_backup_policy(now=now)
        self._save_state()
        return deepcopy(stream)

    def delete_live_stream(
        self,
        stream_id: str,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> None:
        stream = self._find_visible_live_stream(stream_id)
        owner = self._require_workspace_user(stream.owner_user_id)
        self._assert_live_stream_owner_scope(owner, viewer_role=viewer_role, viewer_id=viewer_id)
        effective_runtime = self._effective_live_runtime_stream(stream)
        if effective_runtime.status == "streaming":
            raise ValueError("Luồng đang live. Hãy dùng Dừng trước khi xóa.")
        now = self._now(trim=False)
        clone = self._find_live_backup_clone_optional(stream)
        if clone is not None:
            self._retire_live_backup_clone(clone, now=now, reason="Luồng chính đã bị xoá.")
        self.live_streams = [item for item in self.live_streams if item.id != stream.id]
        self._save_state()

    def delete_live_streams(
        self,
        stream_ids: list[str],
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> list[str]:
        normalized_ids: list[str] = []
        for stream_id in stream_ids:
            cleaned = str(stream_id or "").strip()
            if cleaned and cleaned not in normalized_ids:
                normalized_ids.append(cleaned)

        if not normalized_ids:
            return []

        deleted_ids: list[str] = []
        for stream_id in normalized_ids:
            try:
                self.delete_live_stream(
                    stream_id,
                    viewer_role=viewer_role,
                    viewer_id=viewer_id,
                )
            except KeyError:
                continue
            deleted_ids.append(stream_id)

        return deleted_ids

    def update_live_stream_status(
        self,
        stream_id: str,
        *,
        status: str,
        log_label: str | None = None,
        error_message: str | None = None,
    ) -> LiveStreamRecord:
        stream = self._find_visible_live_stream(stream_id)
        now = self._now(trim=False)
        normalized_status, resolved_log = self._derive_live_status_defaults(
            start_time_live=stream.start_time_live,
            is_live_now=stream.is_live_now,
            is_forever=stream.is_forever,
            status_override=status,
            log_override=log_label,
        )
        stream.status = normalized_status
        stream.log_label = resolved_log
        stream.status_message = None
        stream.error_message = self._normalize_live_optional_text(error_message)
        if normalized_status in {"ended", "stopped", "error"}:
            stream.is_live_now = False
            stream.lease_expires_at = None
            stream.claimed_by_worker_id = None
            stream.claimed_by_role = None
            stream.claimed_at = None
            if normalized_status == "ended" and stream.ended_at is None:
                stream.ended_at = now
            clone = self._find_live_backup_clone_optional(stream)
            if clone is not None:
                self._retire_live_backup_clone(clone, now=now, reason="Luồng chính đã chuyển trạng thái dừng.")
                stream.backup_stream_id = None
        stream.updated_at = now
        self._save_state()
        return deepcopy(stream)

    def stop_live_stream(
        self,
        stream_id: str,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> LiveStreamRecord:
        stream = self._find_visible_live_stream(stream_id)
        owner = self._require_workspace_user(stream.owner_user_id)
        self._assert_live_stream_owner_scope(owner, viewer_role=viewer_role, viewer_id=viewer_id)
        now = self._now(trim=False)
        stream.status = "stopped"
        stream.log_label = "Đã dừng"
        stream.status_message = None
        stream.error_message = None
        stream.is_live_now = False
        stream.progress = min(int(stream.progress or 0), 100)
        stream.stop_requested_at = now
        stream.lease_expires_at = None
        stream.claimed_by_worker_id = None
        stream.claimed_by_role = None
        stream.claimed_at = None
        stream.disconnected_at = None
        stream.ended_at = stream.ended_at or now
        stream.updated_at = now
        clone = self._find_live_backup_clone_optional(stream)
        if clone is not None:
            self._retire_live_backup_clone(clone, now=now, reason="Luồng chính đã được dừng thủ công.")
            stream.backup_stream_id = None
        self._save_state()
        self._notify_live_telegram_chat_ids(
            self._live_stream_recipient_chat_ids(stream),
            self._live_stream_stopped_message(stream, now=now),
        )
        return deepcopy(stream)

    def _scoped_live_streams(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        manager_ids: list[str] | None = None,
        owner_user_id: str | None = None,
    ) -> list[LiveStreamRecord]:
        effective_manager_ids = self._effective_manager_scope_ids(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        normalized_owner_user_id = str(owner_user_id or "").strip()
        if viewer_role == "manager" and viewer_id:
            scoped = [
                stream for stream in self.live_streams
                if stream.manager_id == viewer_id
                and self._is_visible_live_stream(stream)
            ]
        elif not effective_manager_ids:
            scoped = [stream for stream in self.live_streams if self._is_visible_live_stream(stream)]
        else:
            scoped = [
                stream
                for stream in self.live_streams
                if stream.manager_id in effective_manager_ids
                and self._is_visible_live_stream(stream)
            ]
        if normalized_owner_user_id:
            scoped = [stream for stream in scoped if str(stream.owner_user_id or "").strip() == normalized_owner_user_id]
        return sorted(scoped, key=lambda item: (item.created_at or datetime.min, item.id), reverse=True)

    def _live_status_presentation(self, status: str) -> tuple[str, str]:
        normalized = str(status or "").strip().lower()
        if normalized == "streaming":
            return ("Đang live", "border-emerald-200 bg-emerald-50 text-emerald-700")
        if normalized == "downloading":
            return ("Đang tải", "border-sky-200 bg-sky-50 text-sky-700")
        if normalized == "preparing":
            return ("Chuẩn bị", "border-brand-200 bg-brand-50 text-brand-700")
        if normalized == "waiting":
            return ("Chờ giờ", "border-amber-200 bg-amber-50 text-amber-700")
        if normalized == "scheduled":
            return ("Chờ lịch", "border-amber-200 bg-amber-50 text-amber-700")
        if normalized == "disconnected":
            return ("Mất kết nối", "border-orange-200 bg-orange-50 text-orange-700")
        if normalized == "ended":
            return ("Kết thúc", "border-slate-200 bg-slate-50 text-slate-700")
        if normalized == "error":
            return ("Lỗi", "border-rose-200 bg-rose-50 text-rose-700")
        return ("Đã dừng", "border-rose-200 bg-rose-50 text-rose-700")

    def _effective_live_runtime_stream(self, stream: LiveStreamRecord) -> LiveStreamRecord:
        if self._is_runtime_backup_clone(stream):
            return stream
        clone = self._find_live_backup_clone_optional(stream)
        primary_status = str(stream.status or "").strip().lower() or "scheduled"
        if primary_status == "streaming":
            return stream
        if clone is not None and str(clone.status or "").strip().lower() == "streaming":
            return clone
        if primary_status in self._live_worker_active_statuses():
            return stream
        if clone is not None and clone.status in self._live_worker_active_statuses():
            return clone
        return stream

    def _live_duration_text(self, stream: LiveStreamRecord) -> str:
        if stream.is_forever:
            return "24/7"
        if stream.start_time_live and stream.end_time_live and stream.end_time_live >= stream.start_time_live:
            duration = stream.end_time_live - stream.start_time_live
            total_seconds = int(duration.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return stream.live_label

    def _live_streams_using_worker(self, worker_id: str) -> list[LiveStreamRecord]:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return []
        return [
            stream
            for stream in self.live_streams
            if stream.primary_worker_id == normalized_worker_id or stream.backup_worker_id == normalized_worker_id
        ]

    def _count_live_worker_pending_backups(self, worker_id: str) -> int:
        normalized_worker_id = str(worker_id or "").strip()
        if not normalized_worker_id:
            return 0
        return sum(
            1
            for stream in self.live_streams
            if self._is_visible_live_stream(stream)
            and str(stream.backup_worker_id or "").strip() == normalized_worker_id
            and self._live_backup_pending_for_stream(stream)
        )

    def _live_worker_thread_summary(self, worker: WorkerRecord, *, live_role: str | None = None) -> dict[str, int | str]:
        normalized_role = (
            self._normalize_live_assignment_role(live_role)
            if str(live_role or "").strip() in {"primary", "backup"}
            else None
        )
        max_threads = self._effective_live_worker_thread_limit(worker)
        if normalized_role == "backup":
            running_threads = self._count_live_worker_streaming_streams(worker, role="backup")
            pending_threads = self._count_live_worker_pending_backups(worker.id)
            granted_threads_total = self._live_worker_total_granted_threads(worker.id, role="backup")
            is_overallocated = granted_threads_total > max_threads
            return {
                "running_threads": running_threads,
                "pending_threads": pending_threads,
                "allocated_threads": granted_threads_total,
                "granted_threads_total": granted_threads_total,
                "max_threads": max_threads,
                "thread_text": f"{running_threads}/{pending_threads}/{granted_threads_total}",
                "note_text": "Chạy / Chờ / Tổng",
                "is_overallocated": is_overallocated,
            }
        running_threads = self._count_live_worker_running_streams(
            worker,
            role="primary" if normalized_role == "primary" else None,
        )
        granted_threads_total = self._live_worker_total_granted_threads(
            worker.id,
            role="primary" if normalized_role == "primary" else None,
        )
        is_overallocated = granted_threads_total > max_threads
        return {
            "running_threads": running_threads,
            "allocated_threads": granted_threads_total,
            "granted_threads_total": granted_threads_total,
            "max_threads": max_threads,
            "thread_text": f"{running_threads}/{granted_threads_total}",
            "note_text": "Chạy / Tổng",
            "is_overallocated": is_overallocated,
        }

    def _live_backup_running_for_stream(self, stream: LiveStreamRecord) -> bool:
        clone = self._find_live_backup_clone_optional(stream)
        return bool(clone and str(clone.status or "").strip().lower() == "streaming")

    def _live_backup_pending_for_stream(self, stream: LiveStreamRecord) -> bool:
        if not stream.is_forever or not stream.backup_worker_id:
            return False
        if str(stream.status or "").strip().lower() in {"stopped", "ended", "error"}:
            return False
        clone = self._find_live_backup_clone_optional(stream)
        if clone is None:
            return True
        return str(clone.status or "").strip().lower() in self._live_pre_stream_statuses()

    def _live_user_metrics(self, user: UserSummary) -> dict[str, Any]:
        live_streams = [stream for stream in self.live_streams if stream.owner_user_id == user.id and self._is_visible_live_stream(stream)]
        live_primary_bots = self._assigned_live_workers_for_user(user, role="primary")
        live_backup_bots = self._assigned_live_workers_for_user(user, role="backup")
        return {
            "live_threads_running": len(
                [stream for stream in live_streams if str(stream.status or "").strip().lower() == "streaming"]
            ),
            "live_threads_total": self._live_user_allocated_threads(user.id, role="primary"),
            "live_bots_total": len(live_primary_bots),
            "live_threads_backup_running": len([stream for stream in live_streams if self._live_backup_running_for_stream(stream)]),
            "live_threads_backup_pending": len([stream for stream in live_streams if self._live_backup_pending_for_stream(stream)]),
            "live_threads_backup_total": self._live_user_allocated_threads(user.id, role="backup"),
            "live_bots_backup_total": len(live_backup_bots),
            "live_total_bots": len(live_primary_bots) + len(live_backup_bots),
        }

    def _live_stream_display_row(self, stream: LiveStreamRecord) -> dict[str, Any]:
        effective_runtime = self._effective_live_runtime_stream(stream)
        status_label, status_class = self._live_status_presentation(effective_runtime.status)
        primary_name = stream.primary_worker_name or self._resolve_live_worker_display_name(stream.primary_worker_id)
        backup_name = self._live_backup_worker_display_name(stream.backup_worker_id, stream.backup_worker_name)
        effective_log_label = effective_runtime.log_label or stream.log_label
        effective_error_message = self._summarize_live_error_message(effective_runtime.error_message or stream.error_message)
        can_delete = effective_runtime.status != "streaming"
        can_edit = not self._is_live_stream_runtime_locked(stream)
        return {
            "id": stream.id,
            "manager_id": stream.manager_id,
            "manager_name": stream.manager_name or "-",
            "username": stream.owner_username,
            "bot_name": primary_name,
            "bot_group": stream.primary_group or "-",
            "group_name": stream.primary_group or "-",
            "group_meta": stream.manager_name or "-",
            "title": stream.stream_name,
            "stream_name": stream.stream_name,
            "stream_key": stream.stream_key,
            "stream_key_masked": stream.stream_key,
            "bot_backup_name": backup_name or "Không dùng backup",
            "bot_backup_group": stream.backup_group or "",
            "live_label": stream.live_label,
            "live_type_label": stream.live_label,
            "start_at": self._format_full_datetime(stream.start_time_live) if stream.start_time_live else "-",
            "end_at": self._format_full_datetime(stream.end_time_live) if stream.end_time_live else "-",
            "start_date_text": self._format_date(stream.start_time_live) if stream.start_time_live else "-",
            "start_clock_text": self._format_clock(stream.start_time_live) if stream.start_time_live else "",
            "end_date_text": self._format_date(stream.end_time_live) if stream.end_time_live else ("24/7" if stream.is_forever else "-"),
            "end_clock_text": self._format_clock(stream.end_time_live) if stream.end_time_live else "",
            "duration_text": self._live_duration_text(stream),
            "duration_meta": "",
            "log_label": effective_log_label,
            "error_message": effective_error_message,
            "log_class": "border-rose-200 bg-rose-50 text-rose-700" if effective_log_label == "Kết thúc" else "border-slate-200 bg-slate-50 text-slate-700",
            "status_label": status_label,
            "status_class": status_class,
            "can_stop": effective_runtime.status in self._live_worker_scheduled_statuses(),
            "can_edit": can_edit,
            "can_delete": can_delete,
            "detail_href": "",
        }

    def _build_live_stream_rows(self, streams: list[LiveStreamRecord]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, stream in enumerate(streams, start=1):
            rows.append({"index": index, **self._live_stream_display_row(stream)})
        return rows

    def _build_live_render_rows(self, streams: list[LiveStreamRecord]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, stream in enumerate(streams, start=1):
            rows.append({"index": index, **self._live_stream_display_row(stream)})
        return rows

    def get_user_bootstrap(self, user_id: str) -> UserBootstrapResponse:
        user = deepcopy(self._require_workspace_user(user_id))
        assigned_workers = self._workspace_workers_for_user(user)
        assigned_worker_ids = {worker.id for worker in assigned_workers}
        channels = self._workspace_channels_for_user(user)
        if user.role == "user" and assigned_worker_ids:
            channels = [channel for channel in channels if channel.worker_id in assigned_worker_ids]
        self._refresh_browser_channels_metadata(channels)
        channels = deepcopy(channels)
        connected_count = len([channel for channel in channels if channel.status == "connected"])
        reconnect_count = len([channel for channel in channels if channel.status != "connected"])
        return UserBootstrapResponse(
            user=user,
            channels=channels,
            upload_capabilities=self.get_upload_capabilities(),
            oauth=OAuthSummary(connected_count=connected_count, needs_reconnect_count=reconnect_count),
        )

    def get_user_jobs(self, user_id: str) -> UserJobsResponse:
        self._require_workspace_user(user_id)
        return UserJobsResponse(jobs=self._user_jobs_for_workspace(user_id))

    def get_user_dashboard_payload(self, user_id: str) -> dict[str, Any]:
        dashboard = self.get_user_dashboard_view(user_id=user_id)
        return {
            "kpis": dashboard["kpis"],
            "connected_channels": dashboard["connected_channels"],
            "browser_session": dashboard["browser_session"],
            "render_tabs": dashboard["render_tabs"],
            "render_jobs": dashboard["render_jobs"],
            "render_summary": dashboard["render_summary"],
        }

    def get_user_dashboard_live_payload(self, user_id: str) -> dict[str, Any]:
        dashboard = self.get_user_live_workspace_view(user_id=user_id)
        return self._live_workspace_payload(dashboard)

    def get_admin_live_workspace_payload(
        self,
        *,
        manager_ids: list[str] | None = None,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        dashboard = self.get_admin_live_workspace_context(
            manager_ids=manager_ids,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            owner_user_id=owner_user_id,
        )
        return self._live_workspace_payload(dashboard)

    def _resolve_job_preview(self, job: RenderJobRecord) -> dict[str, str] | None:
        if job.thumbnail_path and self._path_has_content(self._absolute_preview_path(str(job.thumbnail_path).strip())):
            return {
                "kind": "image",
                "url": self._preview_route(job.id),
            }

        video_asset = next((asset for asset in job.assets if asset.slot == "video_loop"), None)
        if not video_asset:
            youtube_video_id = self._extract_youtube_video_id(job.output_url)
            if youtube_video_id:
                return {
                    "kind": "image",
                    "url": f"https://i.ytimg.com/vi/{youtube_video_id}/hqdefault.jpg",
                }
            return None

        if video_asset.source_mode == "local" and video_asset.file_name:
            local_asset_path = self.upload_asset_dir / video_asset.file_name
            preview_kind = self._guess_preview_kind(video_asset.file_name) or "video"
            if self._path_has_content(local_asset_path):
                return {
                    "kind": preview_kind,
                    "url": f"/api/user/jobs/{job.id}/preview/video_loop",
                }

        if video_asset.url:
            preview_kind = self._guess_preview_kind(video_asset.url)
            if preview_kind:
                return {
                    "kind": preview_kind,
                    "url": video_asset.url,
                }

        if job.thumbnail_url:
            return {
                "kind": "image",
                "url": job.thumbnail_url,
            }

        youtube_video_id = self._extract_youtube_video_id(job.output_url)
        if youtube_video_id:
            return {
                "kind": "image",
                "url": f"https://i.ytimg.com/vi/{youtube_video_id}/hqdefault.jpg",
            }

        return None

    def create_job(self, user_id: str, payload: JobCreatePayload, source_file_names: dict[str, str | None]) -> RenderJobRecord:
        user = self._require_workspace_user(user_id)
        channel = self._find_channel(payload.channel_id)
        if not self._user_has_channel_access(user_id, channel.id):
            raise ValueError("Bạn không có quyền tạo job trên kênh này.")
        worker = self._assert_channel_matches_user_worker(user, channel)
        queue_order = max([job.queue_order or 0 for job in self.jobs], default=0) + 1
        created_at = self._now()
        normalize = lambda value: (value or "").strip() or None
        normalized_title = re.sub(r"\s+", " ", str(payload.title or "").strip())
        if not normalized_title:
            raise ValueError("Tên video là bắt buộc.")

        resolved_asset_ids = {
            "intro": normalize(payload.intro_asset_id),
            "video_loop": normalize(payload.video_loop_asset_id),
            "audio_loop": normalize(payload.audio_loop_asset_id),
            "outro": normalize(payload.outro_asset_id),
        }
        resolved_file_names = {
            "intro": normalize(source_file_names.get("intro")) or self.consume_uploaded_asset(user_id, resolved_asset_ids.get("intro")),
            "video_loop": normalize(source_file_names.get("video_loop")) or self.consume_uploaded_asset(user_id, resolved_asset_ids.get("video_loop")),
            "audio_loop": normalize(source_file_names.get("audio_loop")) or self.consume_uploaded_asset(user_id, resolved_asset_ids.get("audio_loop")),
            "outro": normalize(source_file_names.get("outro")) or self.consume_uploaded_asset(user_id, resolved_asset_ids.get("outro")),
        }

        assets = [
            JobAsset(
                slot="intro",
                label="Intro",
                source_mode="local" if resolved_file_names.get("intro") else "drive",
                asset_id=resolved_asset_ids.get("intro"),
                url=normalize(payload.intro_url),
                file_name=resolved_file_names.get("intro"),
            ),
            JobAsset(
                slot="video_loop",
                label="Video loop",
                source_mode="local" if resolved_file_names.get("video_loop") else "drive",
                asset_id=resolved_asset_ids.get("video_loop"),
                url=normalize(payload.video_loop_url),
                file_name=resolved_file_names.get("video_loop"),
            ),
            JobAsset(
                slot="audio_loop",
                label="Audio loop",
                source_mode="local" if resolved_file_names.get("audio_loop") else "drive",
                asset_id=resolved_asset_ids.get("audio_loop"),
                url=normalize(payload.audio_loop_url),
                file_name=resolved_file_names.get("audio_loop"),
            ),
            JobAsset(
                slot="outro",
                label="Outro",
                source_mode="local" if resolved_file_names.get("outro") else "drive",
                asset_id=resolved_asset_ids.get("outro"),
                url=normalize(payload.outro_url),
                file_name=resolved_file_names.get("outro"),
            ),
        ]

        has_local_asset = any(item["file_name"] for item in [
            {"file_name": resolved_file_names.get("intro")},
            {"file_name": resolved_file_names.get("video_loop")},
            {"file_name": resolved_file_names.get("audio_loop")},
            {"file_name": resolved_file_names.get("outro")},
        ])

        job = RenderJobRecord(
            id=f"job-{uuid4().hex[:8]}",
            title=normalized_title,
            description=payload.description,
            source_mode="local" if has_local_asset else "drive",
            channel_id=channel.id,
            channel_name=channel.name,
            channel_avatar_url=channel.avatar_url,
            worker_name=worker.id,
            manager_name=user.manager_name or worker.manager_name,
            status="pending",
            progress=0,
            queue_order=queue_order,
            visibility="draft",
            time_render_string=payload.time_render_string,
            scheduled_at=payload.schedule_time,
            created_at=created_at,
            source_label="Local Upload" if has_local_asset else "Google Drive/cloud",
            source_file_name=resolved_file_names.get("video_loop"),
            thumbnail_url=None,
            assets=[asset for asset in assets if asset.url or asset.file_name],
        )
        self.jobs.append(job)
        self._refresh_queue_positions()
        self._save_state()
        return deepcopy(job)

    def cancel_job(self, job_id: str, *, user_id: str | None = None) -> RenderJobRecord:
        job = self._find_user_visible_job(user_id, job_id) if user_id else self._find_job(job_id)
        if job.status not in {"completed", "cancelled", "error"}:
            job.status = "cancelled"
            job.can_cancel = False
            job.completed_at = self._now()
            job.lease_expires_at = None
            self._refresh_queue_positions()
            self._save_state()
        return deepcopy(job)

    def delete_job(self, job_id: str, *, user_id: str | None = None) -> None:
        target_job = self._find_user_visible_job(user_id, job_id) if user_id else self._find_job(job_id)
        for index, job in enumerate(self.jobs):
            if job.id == target_job.id:
                self._purge_job_artifacts(job, exclude_job_id=job.id)
                self.jobs.pop(index)
                self._refresh_queue_positions()
                self._save_state()
                return
        raise KeyError(job_id)

    def delete_jobs(self, job_ids: list[str], *, user_id: str | None = None) -> list[str]:
        normalized_ids: list[str] = []
        for job_id in job_ids:
            cleaned = str(job_id or "").strip()
            if cleaned and cleaned not in normalized_ids:
                normalized_ids.append(cleaned)

        if not normalized_ids:
            return []

        visible_job_ids: set[str] = set()
        for job_id in normalized_ids:
            try:
                target_job = self._find_user_visible_job(user_id, job_id) if user_id else self._find_job(job_id)
            except KeyError:
                continue
            visible_job_ids.add(target_job.id)

        if not visible_job_ids:
            return []

        deleted_ids: list[str] = []
        remaining_jobs: list[RenderJobRecord] = []
        for job in self.jobs:
            if job.id in visible_job_ids:
                self._purge_job_artifacts(job, exclude_job_id=job.id)
                deleted_ids.append(job.id)
                continue
            remaining_jobs.append(job)

        if not deleted_ids:
            return []

        self.jobs = remaining_jobs
        self._refresh_queue_positions()
        self._save_state()
        return deleted_ids

    def delete_all_jobs(self, deleted_by: str = "admin") -> None:
        jobs_to_delete = list(self.jobs)
        self.jobs = []
        for job in jobs_to_delete:
            self._purge_job_artifacts(job)
        self.render_delete_meta = {
            "user": deleted_by,
            "deleted_at": self._now(),
        }
        self._save_state()

    def get_admin_dashboard(self) -> AdminDashboardResponse:
        queued = len([job for job in self.jobs if job.status in {"pending", "queueing"}])
        running = len([job for job in self.jobs if job.status in {"downloading", "rendering", "uploading"}])
        failed = len([job for job in self.jobs if job.status == "error"])
        workers_online = len([worker for worker in self.workers if worker.status in {"online", "busy"}])
        summary = AdminSummary(
            total_managers=len([user for user in self.users if user.role == "manager"]),
            total_users=len([user for user in self.users if user.role == "user"]),
            total_workers=len(self.workers),
            workers_online=workers_online,
            channels_connected=len([channel for channel in self.channels if channel.status == "connected"]),
            queued_jobs=queued,
            running_jobs=running,
            failed_jobs=failed,
        )
        return AdminDashboardResponse(
            summary=summary,
            users=deepcopy(self.users),
            workers=deepcopy(self.workers),
            channels=deepcopy(self.channels),
            jobs=deepcopy(self.jobs),
        )

    def _build_user_rows(
        self,
        manager_ids: list[str] | None = None,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> list[dict[str, Any]]:
        selected_manager_ids = set(self._selected_manager_ids(manager_ids))
        rows: list[dict[str, Any]] = []
        managers_by_username = {user.username: user for user in self.users if user.role == "manager"}

        for user in self.users:
            manager = managers_by_username.get(user.manager_name or "")
            resolved_manager_id = user.manager_id or (manager.id if manager else "")

            if viewer_role == "manager":
                if not viewer_id:
                    continue
                if user.role == "manager":
                    if user.id != viewer_id:
                        continue
                elif user.role == "user":
                    if resolved_manager_id != viewer_id:
                        continue
                else:
                    continue

            include_row = True
            if selected_manager_ids:
                if user.role == "manager":
                    include_row = user.id in selected_manager_ids
                elif user.role == "user":
                    include_row = resolved_manager_id in selected_manager_ids

            if not include_row:
                continue

            role_label, role_class = self._role_badge(user.role)
            meta = self._user_meta_record(user.id)
            row_manager_name = user.manager_name or (user.username if user.role == "manager" else "-")
            row_manager_id = manager.id if manager else (user.id if user.role == "manager" else "")
            row_telegram = (
                (meta.get("telegram_live") or meta.get("telegram") or "")
                if user.role == "user"
                else (meta.get("telegram") or "")
            )
            row = {
                "index": len(rows) + 1,
                "id": user.id,
                "username": user.username,
                "display_name": user.username,
                "avatar_initials": self._initials(user.username),
                "avatar_class": self._avatar_palette(user.username),
                "role": user.role,
                "role_label": role_label,
                "role_class": role_class,
                "manager_name": row_manager_name,
                "manager_id": row_manager_id,
                "telegram": row_telegram,
                "telegram_live": meta.get("telegram_live") or "",
                "updated_meta": f"{meta.get('updated_by') or '-'} • {self._format_compact_datetime(meta.get('updated_at'))}",
                "total_channels": self._user_channel_count(user),
                "total_workers": self._user_worker_count(user),
                "live_list_href": f"/admin/livestream/index?userId={quote(user.id)}",
                "can_delete": not (
                    (viewer_role == "manager" and viewer_id and user.role == "manager" and user.id == viewer_id)
                    or (user.role == "admin" and len([item for item in self.users if item.role == "admin"]) <= 1)
                ),
            }
            row.update(self._live_user_metrics(user))
            row.update(self._user_bot_triplet_metrics(user))
            rows.append(row)
        return rows

    @staticmethod
    def _credential_status_label(meta: dict[str, Any]) -> str:
        password_hash = str(meta.get("password_hash") or "").strip()
        if password_hash:
            return "Đã đặt mật khẩu"
        if str(meta.get("password") or "").strip():
            return "Cần đổi lại mật khẩu"
        return "Chưa đặt mật khẩu"

    def _build_role_page_context(
        self,
        *,
        role_kind: str,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        workspace_mode: str = "upload",
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        is_manager = role_kind == "manager"
        current_role = "manager" if is_manager else "admin"
        rows = []
        for user in self.users:
            if user.role != current_role:
                continue
            meta = self._user_meta_record(user.id)
            rows.append(
                {
                    "index": len(rows) + 1,
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.display_name,
                    "credential_status": self._credential_status_label(meta),
                    "updated_meta": f"{meta.get('updated_by') or '-'} • {self._format_compact_datetime(meta.get('updated_at'))}",
                }
            )

        context = self._admin_shell_context(
            page_title="Danh sách manager" if is_manager else "Danh sách admin",
            active_page="users",
            user_section="managers" if is_manager else "admins",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/user_role_list.html",
                "page_kind": role_kind,
                "heading": "Danh sách manager" if is_manager else "Danh sách admin",
                "role_label": "manager" if is_manager else "admin",
                "title": "Danh sách manager" if is_manager else "Danh sách admin",
                "description": "Bổ nhiệm hoặc gỡ quyền manager cho tài khoản." if is_manager else "Bổ nhiệm hoặc gỡ quyền admin cho tài khoản.",
                "action_route": "/admin/user/updaterolemanager" if is_manager else "/admin/user/updateroleadmin",
                "rows": rows,
                "assignable_user_options": [
                    {
                        "id": user.id,
                        "username": user.username,
                        "search_text": " ".join(
                            part
                            for part in [
                                user.username,
                                user.display_name,
                                user.role,
                                user.manager_name,
                            ]
                            if part
                        ).lower(),
                        "description": " • ".join(
                            part
                            for part in [
                                f"Role: {user.role}",
                                user.display_name if user.display_name and user.display_name != user.username else "",
                            ]
                            if part
                        ),
                    }
                    for user in sorted(self.users, key=lambda item: (item.username or "").lower())
                ],
                "hide_workspace_tabs": True,
                "workspace_heading": "Admin",
            }
        )
        return context

    def _build_user_bot_links(self, user_id: str, *, workspace_mode: str = "upload") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        is_live_workspace = workspace_mode == "live"
        link_source = self.live_user_worker_links if is_live_workspace else self.user_worker_links
        for mapping in link_source:
            if mapping["user_id"] != user_id:
                continue
            worker = self._find_live_worker(mapping["worker_id"]) if is_live_workspace else self._find_worker(mapping["worker_id"])
            status_label, status_class = self._worker_status_badge(worker.status)
            live_role = (
                self._normalize_live_assignment_role(mapping.get("live_role"), fallback_note=mapping.get("note"))
                if is_live_workspace
                else None
            )
            rows.append(
                {
                    "index": len(rows) + 1,
                    "id": mapping["id"],
                    "worker_id": worker.id,
                    "worker_name": self._resolve_live_worker_display_name(worker.id) if is_live_workspace else self._resolve_worker_display_name(worker.id),
                    "manager_name": worker.manager_name,
                    "status_label": status_label,
                    "status_class": status_class,
                    "bot_type": self._live_assignment_role_label(live_role) if is_live_workspace else mapping.get("bot_type", "live"),
                    "number_of_threads": mapping["threads"],
                    "live_role": live_role or "",
                    "live_role_label": self._live_assignment_role_label(live_role) if is_live_workspace else "",
                    "note": mapping.get("note") or (self._live_assignment_note(live_role) if is_live_workspace else "VPS được cấp"),
                    "disk_text": f"{worker.disk_used_gb:.1f}/{worker.disk_total_gb:.1f}GB",
                    "bandwidth_text": f"{worker.bandwidth_kbps:.2f}KB/s",
                }
            )
        return rows

    def _worker_assigned_user_link(self, worker_id: str, *, exclude_user_id: str | None = None) -> dict[str, Any] | None:
        for mapping in self.user_worker_links:
            if mapping.get("worker_id") != worker_id:
                continue
            if exclude_user_id and mapping.get("user_id") == exclude_user_id:
                continue
            return mapping
        return None

    def _live_worker_assigned_user_link(self, worker_id: str, *, exclude_user_id: str | None = None) -> dict[str, Any] | None:
        for mapping in self.live_user_worker_links:
            if mapping.get("worker_id") != worker_id:
                continue
            if exclude_user_id and mapping.get("user_id") == exclude_user_id:
                continue
            return mapping
        return None

    def _assigned_users_for_worker(self, worker_id: str) -> list[UserSummary]:
        users: list[UserSummary] = []
        seen_user_ids: set[str] = set()
        for mapping in sorted(self.user_worker_links, key=lambda item: self._mapping_numeric_id(item.get("id"))):
            if str(mapping.get("worker_id") or "").strip() != str(worker_id or "").strip():
                continue
            user_id = str(mapping.get("user_id") or "").strip()
            if not user_id or user_id in seen_user_ids:
                continue
            try:
                user = self._find_user(user_id)
            except KeyError:
                continue
            users.append(user)
            seen_user_ids.add(user_id)
        return users

    def _assigned_live_users_for_worker(
        self,
        worker_id: str,
        *,
        role: str | None = None,
    ) -> list[UserSummary]:
        normalized_role = self._normalize_live_assignment_role(role) if role is not None else None
        users: list[UserSummary] = []
        seen_user_ids: set[str] = set()
        for mapping in sorted(self.live_user_worker_links, key=lambda item: self._mapping_numeric_id(item.get("id"))):
            if str(mapping.get("worker_id") or "").strip() != str(worker_id or "").strip():
                continue
            if normalized_role is not None and self._normalize_live_assignment_role(mapping.get("live_role"), fallback_note=mapping.get("note")) != normalized_role:
                continue
            user_id = str(mapping.get("user_id") or "").strip()
            if not user_id or user_id in seen_user_ids:
                continue
            try:
                user = self._find_user(user_id)
            except KeyError:
                continue
            users.append(user)
            seen_user_ids.add(user_id)
        return users

    def _assigned_live_user_for_worker(self, worker_id: str, *, role: str | None = None) -> UserSummary | None:
        users = self._assigned_live_users_for_worker(worker_id, role=role)
        if not users:
            return None
        return users[0]

    def _worker_candidates_for_user(self, user: UserSummary) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        current_worker_ids = {
            str(link.get("worker_id") or "").strip()
            for link in self._assigned_worker_links_for_user(user.id)
            if str(link.get("worker_id") or "").strip()
        }
        for worker in self.workers:
            if user.manager_name and worker.manager_name != user.manager_name:
                continue
            candidates.append({"id": worker.id, "name": self._resolve_worker_display_name(worker.id)})
        candidate_ids = {item["id"] for item in candidates}
        for worker_id in sorted(current_worker_ids):
            if worker_id in candidate_ids:
                continue
            worker = self._find_worker(worker_id)
            candidates.insert(0, {"id": worker.id, "name": self._resolve_worker_display_name(worker.id)})
            candidate_ids.add(worker.id)
        return candidates

    def _live_worker_candidates_for_user(self, user: UserSummary) -> list[dict[str, str]]:
        candidates: list[dict[str, str]] = []
        current_worker_ids = {
            str(link.get("worker_id") or "").strip()
            for link in self._assigned_live_worker_links_for_user(user.id)
            if str(link.get("worker_id") or "").strip()
        }
        for worker in self.live_workers:
            if user.manager_name and worker.manager_name != user.manager_name:
                continue
            candidates.append({"id": worker.id, "name": self._resolve_live_worker_display_name(worker.id)})
        candidate_ids = {item["id"] for item in candidates}
        for worker_id in sorted(current_worker_ids):
            if worker_id in candidate_ids:
                continue
            worker = self._find_live_worker(worker_id)
            candidates.insert(0, {"id": worker.id, "name": self._resolve_live_worker_display_name(worker.id)})
            candidate_ids.add(worker.id)
        return candidates

    def _resolved_user_manager_id(self, user: UserSummary) -> str | None:
        if user.role != "user":
            return None
        if user.manager_id:
            return user.manager_id
        manager = next(
            (
                item
                for item in self.users
                if item.role == "manager" and item.username == (user.manager_name or "")
            ),
            None,
        )
        return manager.id if manager else None

    def _assigned_user_for_worker(self, worker_id: str) -> UserSummary | None:
        users = self._assigned_users_for_worker(worker_id)
        if not users:
            return None
        return users[0]

    def _bot_user_options(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        workspace_mode: str = "upload",
    ) -> list[dict[str, Any]]:
        if workspace_mode == "live":
            options: list[dict[str, Any]] = []
            normalized_viewer_id = str(viewer_id or "").strip()
            for user in self.users:
                if viewer_role == "manager":
                    if user.role == "manager" and user.id != normalized_viewer_id:
                        continue
                    if user.role == "admin":
                        continue
                    if user.role == "user" and self._resolved_user_manager_id(user) != normalized_viewer_id:
                        continue
                elif viewer_role == "admin" and user.role == "admin" and user.id != normalized_viewer_id:
                    continue

                if user.role == "user":
                    manager_id = self._resolved_user_manager_id(user) or ""
                    role_label = "User"
                    scope_locked = False
                    self_assignable = False
                elif user.role == "manager":
                    manager_id = user.id
                    role_label = "Manager"
                    scope_locked = viewer_role == "admin"
                    self_assignable = viewer_role == "manager" and user.id == normalized_viewer_id
                elif user.role == "admin":
                    manager_id = ""
                    role_label = "Admin"
                    scope_locked = False
                    self_assignable = viewer_role == "admin" and user.id == normalized_viewer_id
                else:
                    continue

                assigned_workers = self._workspace_live_workers_for_user(user.id)
                assigned_worker = assigned_workers[0] if assigned_workers else None
                assigned_worker_ids = [worker.id for worker in assigned_workers]
                assigned_worker_names = [self._resolve_live_worker_display_name(worker.id) for worker in assigned_workers]
                assigned_links = self._assigned_live_worker_links_for_user(user.id)
                assigned_role_labels = [
                    self._live_assignment_role_label(
                        self._normalize_live_assignment_role(link.get("live_role"), fallback_note=link.get("note"))
                    )
                    for link in assigned_links
                ]
                options.append(
                    {
                        "id": user.id,
                        "username": user.username,
                        "display_name": user.display_name,
                        "role": user.role,
                        "role_label": role_label,
                        "manager_id": manager_id,
                        "manager_name": user.manager_name or "",
                        "assigned_worker_id": assigned_worker.id if assigned_worker else "",
                        "assigned_worker_name": self._resolve_live_worker_display_name(assigned_worker.id) if assigned_worker else "",
                        "assigned_worker_ids": assigned_worker_ids,
                        "assigned_worker_names": assigned_worker_names,
                        "assigned_worker_count": len(assigned_workers),
                        "assigned_worker_names_text": ", ".join(assigned_worker_names),
                        "assigned_role_labels": assigned_role_labels,
                        "assigned_role_labels_text": ", ".join(assigned_role_labels),
                        "scope_locked": scope_locked,
                        "self_assignable": self_assignable,
                    }
                )
            return options

        options: list[dict[str, Any]] = []
        viewer_scope_manager_id = str(viewer_id or "").strip()
        for user in self.users:
            scope_locked = False
            self_assignable = False
            if user.role == "user":
                manager_id = self._resolved_user_manager_id(user)
                assigned_workers = self._workspace_workers_for_user(user.id)
                if viewer_role == "manager" and viewer_scope_manager_id and manager_id != viewer_scope_manager_id:
                    assigned_workers = [
                        worker
                        for worker in assigned_workers
                        if str(worker.manager_id or "").strip() == viewer_scope_manager_id
                    ]
                    if not assigned_workers:
                        continue
                    scope_locked = True
                role_label = "User"
            elif user.role == "manager" and viewer_role == "manager" and viewer_id and user.id == viewer_id:
                manager_id = user.id
                assigned_workers = self._workspace_workers_for_user(user.id)
                role_label = "Manager"
                self_assignable = True
            elif user.role == "manager" and viewer_role == "manager" and viewer_scope_manager_id:
                manager_id = user.id
                assigned_workers = [
                    worker
                    for worker in self._workspace_workers_for_user(user.id)
                    if str(worker.manager_id or "").strip() == viewer_scope_manager_id
                ]
                if not assigned_workers:
                    continue
                role_label = "Manager"
                scope_locked = True
            elif user.role == "manager" and viewer_role == "admin":
                manager_id = user.id
                assigned_workers = self._workspace_workers_for_user(user.id)
                if not assigned_workers:
                    continue
                role_label = "Manager"
                scope_locked = True
            elif user.role == "admin" and viewer_role == "admin" and viewer_id and user.id == viewer_id:
                manager_id = ""
                assigned_workers = self._workspace_workers_for_user(user.id)
                role_label = "Admin"
                self_assignable = True
            elif user.role == "admin" and viewer_role == "manager" and viewer_scope_manager_id:
                assigned_workers = [
                    worker
                    for worker in self._workspace_workers_for_user(user.id)
                    if str(worker.manager_id or "").strip() == viewer_scope_manager_id
                ]
                if not assigned_workers:
                    continue
                manager_id = viewer_scope_manager_id
                role_label = "Admin"
                scope_locked = True
            else:
                continue
            assigned_worker = assigned_workers[0] if assigned_workers else None
            assigned_worker_ids = [worker.id for worker in assigned_workers]
            assigned_worker_names = [self._resolve_worker_display_name(worker.id) for worker in assigned_workers]
            options.append(
                {
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.display_name,
                    "role": user.role,
                    "role_label": role_label,
                    "manager_id": manager_id or "",
                    "manager_name": user.manager_name or "",
                    "assigned_worker_id": assigned_worker.id if assigned_worker else "",
                    "assigned_worker_name": self._resolve_worker_display_name(assigned_worker.id) if assigned_worker else "",
                    "assigned_worker_ids": assigned_worker_ids,
                    "assigned_worker_names": assigned_worker_names,
                    "assigned_worker_count": len(assigned_workers),
                    "assigned_worker_names_text": ", ".join(assigned_worker_names),
                    "scope_locked": scope_locked,
                    "self_assignable": self_assignable,
                }
            )
        options.sort(
            key=lambda item: (
                0 if item.get("role") == "admin" else 1,
                (item["manager_name"] or "").casefold(),
                item["username"].casefold(),
            )
        )
        return options

    def _combined_bot_user_options(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> list[dict[str, Any]]:
        merged_by_user_id: dict[str, dict[str, Any]] = {}
        for workspace_mode in ("upload", "live"):
            for raw_option in self._bot_user_options(
                viewer_role=viewer_role,
                viewer_id=viewer_id,
                workspace_mode=workspace_mode,
            ):
                user_id = str(raw_option.get("id") or "").strip()
                if not user_id:
                    continue
                option = dict(raw_option)
                option["assigned_worker_ids"] = list(raw_option.get("assigned_worker_ids") or [])
                option["assigned_worker_names"] = list(raw_option.get("assigned_worker_names") or [])
                option["assigned_role_labels"] = list(raw_option.get("assigned_role_labels") or [])
                existing = merged_by_user_id.get(user_id)
                if existing is None:
                    merged_by_user_id[user_id] = option
                    continue
                if not str(existing.get("manager_id") or "").strip() and str(option.get("manager_id") or "").strip():
                    existing["manager_id"] = option.get("manager_id") or ""
                if not str(existing.get("manager_name") or "").strip() and str(option.get("manager_name") or "").strip():
                    existing["manager_name"] = option.get("manager_name") or ""
                if not str(existing.get("assigned_worker_id") or "").strip() and str(option.get("assigned_worker_id") or "").strip():
                    existing["assigned_worker_id"] = option.get("assigned_worker_id") or ""
                    existing["assigned_worker_name"] = option.get("assigned_worker_name") or ""
                existing["scope_locked"] = bool(existing.get("scope_locked")) or bool(option.get("scope_locked"))
                existing["self_assignable"] = bool(existing.get("self_assignable")) or bool(option.get("self_assignable"))
                combined_worker_ids = list(existing.get("assigned_worker_ids") or [])
                combined_worker_names = list(existing.get("assigned_worker_names") or [])
                combined_role_labels = list(existing.get("assigned_role_labels") or [])
                for worker_id in option["assigned_worker_ids"]:
                    if worker_id not in combined_worker_ids:
                        combined_worker_ids.append(worker_id)
                for worker_name in option["assigned_worker_names"]:
                    if worker_name not in combined_worker_names:
                        combined_worker_names.append(worker_name)
                for role_label in option["assigned_role_labels"]:
                    if role_label not in combined_role_labels:
                        combined_role_labels.append(role_label)
                existing["assigned_worker_ids"] = combined_worker_ids
                existing["assigned_worker_names"] = combined_worker_names
                existing["assigned_worker_count"] = len(combined_worker_ids)
                existing["assigned_worker_names_text"] = ", ".join(combined_worker_names)
                existing["assigned_role_labels"] = combined_role_labels
                existing["assigned_role_labels_text"] = ", ".join(combined_role_labels)
        options = list(merged_by_user_id.values())
        options.sort(
            key=lambda item: (
                0 if item.get("role") == "admin" else 1,
                (item.get("manager_name") or "").casefold(),
                item["username"].casefold(),
            )
        )
        return options

    def _apply_worker_manager(self, worker: WorkerRecord, manager: UserSummary | None) -> None:
        worker.manager_id = manager.id if manager else None
        worker.manager_name = manager.username if manager else "system"
        if not str(worker.group or "").strip():
            worker.group = self._default_bot_group_label()
        updated_manager_name = worker.manager_name
        for channel in self.channels:
            if channel.worker_id == worker.id:
                channel.manager_name = updated_manager_name
        for job in self.jobs:
            if job.worker_name == worker.id:
                job.manager_name = updated_manager_name

    def _clear_worker_channels(self, worker_id: str) -> int:
        cleaned_worker_id = str(worker_id or "").strip()
        if not cleaned_worker_id:
            return 0

        worker_channel_ids = [
            channel.id
            for channel in self.channels
            if str(channel.worker_id or "").strip() == cleaned_worker_id
        ]
        if not worker_channel_ids:
            return 0

        channel_id_set = set(worker_channel_ids)
        linked_user_ids: set[str] = set()
        removed_jobs = [job for job in self.jobs if job.channel_id in channel_id_set]

        for channel in list(self.channels):
            if channel.id not in channel_id_set:
                continue
            linked_user_ids.update(
                str(link.get("user_id") or "").strip()
                for link in self.channel_user_links
                if link.get("channel_id") == channel.id
            )
            self._schedule_channel_browser_profile_cleanup(channel)

        for job in removed_jobs:
            self._purge_job_artifacts(job, exclude_job_id=job.id)

        self.channels = [channel for channel in self.channels if channel.id not in channel_id_set]
        self.channel_user_links = [
            link for link in self.channel_user_links if str(link.get("channel_id") or "").strip() not in channel_id_set
        ]
        self.jobs = [job for job in self.jobs if job.channel_id not in channel_id_set]

        for user_id in linked_user_ids:
            if user_id:
                self._close_orphan_browser_sessions_for_user(user_id)

        return len(worker_channel_ids)

    def _close_user_worker_browser_sessions(self, user_id: str, worker_id: str, *, reason: str) -> bool:
        cleaned_user_id = str(user_id or "").strip()
        cleaned_worker_id = str(worker_id or "").strip()
        if not cleaned_user_id or not cleaned_worker_id:
            return False
        closed_at = self._now(trim=False)
        changed = False
        for session in self.browser_sessions:
            if session.owner_user_id != cleaned_user_id:
                continue
            if str(session.target_worker_id or "").strip() != cleaned_worker_id:
                continue
            if session.status not in {"launching", "awaiting_confirmation", "confirmed"}:
                continue
            session.status = "closed"
            session.expires_at = closed_at
            session.last_error = reason
            changed = True
        return changed

    def _clear_user_worker_channels(self, user_id: str, worker_id: str) -> int:
        cleaned_user_id = str(user_id or "").strip()
        cleaned_worker_id = str(worker_id or "").strip()
        if not cleaned_user_id or not cleaned_worker_id:
            return 0

        affected_channels = [
            channel
            for channel in self.channels
            if str(channel.worker_id or "").strip() == cleaned_worker_id
            and any(
                str(link.get("channel_id") or "").strip() == channel.id
                and str(link.get("user_id") or "").strip() == cleaned_user_id
                for link in self.channel_user_links
            )
        ]
        if not affected_channels:
            self._close_user_worker_browser_sessions(
                cleaned_user_id,
                cleaned_worker_id,
                reason="Session đã đóng do user không còn được cấp VPS.",
            )
            return 0

        affected_channel_ids = {channel.id for channel in affected_channels}
        self.channel_user_links = [
            link
            for link in self.channel_user_links
            if not (
                str(link.get("user_id") or "").strip() == cleaned_user_id
                and str(link.get("channel_id") or "").strip() in affected_channel_ids
            )
        ]

        removable_channel_ids: set[str] = set()
        removed_jobs: list[RenderJobRecord] = []
        for channel in affected_channels:
            still_linked = any(
                str(link.get("channel_id") or "").strip() == channel.id
                for link in self.channel_user_links
            )
            if still_linked:
                continue
            removable_channel_ids.add(channel.id)
            self._schedule_channel_browser_profile_cleanup(channel)
            removed_jobs.extend([job for job in self.jobs if job.channel_id == channel.id])

        for job in removed_jobs:
            self._purge_job_artifacts(job, exclude_job_id=job.id)

        if removable_channel_ids:
            self.channels = [channel for channel in self.channels if channel.id not in removable_channel_ids]
            self.jobs = [job for job in self.jobs if job.channel_id not in removable_channel_ids]

        self._close_user_worker_browser_sessions(
            cleaned_user_id,
            cleaned_worker_id,
            reason="Session đã đóng do user không còn được cấp VPS.",
        )
        self._close_orphan_browser_sessions_for_user(cleaned_user_id)
        return len(affected_channels)

    def _purge_worker_assignment_scope(self, worker_id: str, *, session_reason: str) -> None:
        cleaned_worker_id = str(worker_id or "").strip()
        if not cleaned_worker_id:
            return

        linked_user_ids = {
            str(link.get("user_id") or "").strip()
            for link in self.user_worker_links
            if str(link.get("worker_id") or "").strip() == cleaned_worker_id and str(link.get("user_id") or "").strip()
        }
        for user_id in linked_user_ids:
            self._clear_user_worker_channels(user_id, cleaned_worker_id)

        self._clear_worker_channels(cleaned_worker_id)

        removed_jobs = [
            job for job in self.jobs if str(job.worker_name or "").strip() == cleaned_worker_id
        ]
        for job in removed_jobs:
            self._purge_job_artifacts(job, exclude_job_id=job.id)
        self.jobs = [job for job in self.jobs if str(job.worker_name or "").strip() != cleaned_worker_id]

        self.user_worker_links = [
            link for link in self.user_worker_links if str(link.get("worker_id") or "").strip() != cleaned_worker_id
        ]

        for user_id in linked_user_ids:
            self._close_user_worker_browser_sessions(user_id, cleaned_worker_id, reason=session_reason)
            self._close_orphan_browser_sessions_for_user(user_id)

    def get_admin_user_index_context(
        self,
        *,
        manager_ids: list[str] | None = None,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        context = self._admin_shell_context(
            page_title="Danh sách người dùng",
            active_page="users",
            user_section="users",
            workspace_mode="upload",
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/user_index.html",
                "users": self._build_user_rows(
                    manager_ids,
                    viewer_role=viewer_role,
                    viewer_id=viewer_id,
                ),
                "manager_form_options": [
                    {"id": user.id, "label": user.username}
                    for user in self.users
                    if user.role == "manager" and (viewer_role != "manager" or user.id == viewer_id)
                ],
                "hide_workspace_tabs": True,
                "workspace_heading": "Admin",
                "summary_strip": self._admin_user_summary_strip(
                    viewer_role=viewer_role,
                    viewer_id=viewer_id,
                    manager_ids=manager_ids,
                ),
            }
        )
        if viewer_role == "manager" and viewer_id:
            context["manager_options"] = [item for item in context["manager_options"] if item["id"] == viewer_id]
        return context

    def get_admin_user_create_context(
        self,
        *,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
        form_data: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        context = self._admin_shell_context(
            page_title="Tạo user",
            active_page="users",
            user_section="create",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/user_create.html",
                "form": form_data
                or {
                    "username": "",
                    "manager_id": "",
                },
                "error": error,
                "manager_candidates": [{"id": user.id, "username": user.username} for user in self.users if user.role == "manager"],
                "hide_workspace_tabs": True,
                "workspace_heading": "Admin",
            }
        )
        return context

    def get_admin_user_edit_context(
        self,
        *,
        user_id: str,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
        error: str | None = None,
    ) -> dict[str, Any]:
        user = self._find_user(user_id)
        meta = self._user_meta_record(user.id)
        manager = next((item for item in self.users if item.username == user.manager_name), None)
        context = self._admin_shell_context(
            page_title="Cập nhật user",
            active_page="users",
            user_section="users",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/user_edit.html",
                "error": error,
                "manager_candidates": [{"id": item.id, "username": item.username} for item in self.users if item.role == "manager"],
                "user_record": {
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.username,
                    "role": user.role,
                    "manager_id": manager.id if manager else "",
                    "telegram": (meta.get("telegram_live") or "") if user.role == "user" else (meta.get("telegram") or ""),
                    "telegram_live": meta.get("telegram_live") or "",
                    "password": "",
                },
                "hide_workspace_tabs": True,
                "workspace_heading": "Admin",
            }
        )
        return context

    def get_admin_user_binding_payload(self, user_id: str) -> dict[str, str]:
        user = self._find_user(user_id)
        meta = self._user_meta_record(user.id)
        manager = next((item for item in self.users if item.username == user.manager_name), None)
        return {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "manager_id": manager.id if manager else "",
            "telegram": (meta.get("telegram_live") or "") if user.role == "user" else (meta.get("telegram") or ""),
            "telegram_live": meta.get("telegram_live") or "",
        }

    def get_admin_manager_page_context(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        workspace_mode: str = "upload",
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        return self._build_role_page_context(
            role_kind="manager",
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            workspace_mode=workspace_mode,
            notice=notice,
            notice_level=notice_level,
        )

    def get_admin_admin_page_context(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        workspace_mode: str = "upload",
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        return self._build_role_page_context(
            role_kind="admin",
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            workspace_mode=workspace_mode,
            notice=notice,
            notice_level=notice_level,
        )

    def get_admin_user_bot_context(
        self,
        *,
        user_id: str,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        user = self._find_user(user_id)
        meta = self._user_meta_record(user.id)
        is_live_workspace = workspace_mode == "live"
        assigned_workers = self._assigned_live_workers_for_user(user) if is_live_workspace else self._assigned_workers_for_user(user)
        assigned_worker = assigned_workers[0] if assigned_workers else None
        context = self._admin_shell_context(
            page_title=f"BOT của {user.username}",
            active_page="users",
            user_section="users",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/user_manager_bot.html",
                "target_user": {
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.display_name,
                    "manager_name": user.manager_name or "-",
                    "telegram": meta.get("telegram") or "-",
                },
                "user_record": {
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.display_name,
                    "manager_name": user.manager_name or "-",
                    "link_telegram": meta.get("telegram") or "-",
                },
                "assigned_workers": [
                    {
                        "id": worker.id,
                        "name": self._resolve_live_worker_display_name(worker.id) if is_live_workspace else self._resolve_worker_display_name(worker.id),
                    }
                    for worker in assigned_workers
                ],
                "assigned_worker": {
                    "id": assigned_worker.id,
                    "name": self._resolve_live_worker_display_name(assigned_worker.id) if is_live_workspace else self._resolve_worker_display_name(assigned_worker.id),
                }
                if assigned_worker
                else None,
                "assigned_worker_count": len(assigned_workers),
                "rows": self._build_user_bot_links(user.id, workspace_mode=workspace_mode),
                "worker_candidates": self._live_worker_candidates_for_user(user) if is_live_workspace else self._worker_candidates_for_user(user),
            }
        )
        return context

    def _filtered_worker_operation_tasks(
        self,
        manager_ids: list[str] | None = None,
        *,
        workspace_mode: str = "upload",
    ) -> list[dict[str, Any]]:
        selected_ids = set(self._selected_manager_ids(manager_ids))
        resolved_workspace_mode = self._normalize_workspace_mode(workspace_mode)
        tasks: list[dict[str, Any]] = []
        for task in self.worker_operation_tasks:
            if self._worker_operation_is_finished(task):
                continue
            if self._normalize_workspace_mode(task.get("workspace_mode")) != resolved_workspace_mode:
                continue
            manager_id = str(task.get("manager_id") or "").strip()
            if selected_ids and manager_id not in selected_ids:
                continue
            tasks.append(task)
        tasks.sort(
            key=lambda item: (
                self._parse_datetime(item.get("created_at")) or self._now(trim=False),
                str(item.get("worker_id") or "").strip(),
            )
        )
        return tasks

    def _build_operation_placeholder_row(self, task: dict[str, Any]) -> dict[str, Any]:
        status_label, status_class = self._worker_operation_badge(task)
        placeholder_name = str(task.get("worker_name") or task.get("vps_ip") or task.get("worker_id") or "").strip()
        workspace_kind = self._normalize_workspace_mode(task.get("workspace_mode"))
        task_live_role = self._normalize_live_assignment_role(task.get("live_role")) if str(task.get("live_role") or "").strip() else ""
        bot_type = self._bot_type_badge_data(workspace_kind, task_live_role)
        bot_function_key = "upload" if workspace_kind != "live" else "backup" if task_live_role == "backup" else "primary"
        pending_config = self._pending_install_config(task)
        placeholder_local = str((pending_config or {}).get("local") or task.get("local") or "").strip()
        requested_threads = (
            self._normalize_live_worker_threads(
                (pending_config or {}).get("threads") if (pending_config or {}).get("threads") is not None else task.get("threads") or 1
            )
            if workspace_kind == "live"
            else 0
        )
        assigned_user_ids = [
            str(value).strip()
            for value in ((pending_config or {}).get("assigned_user_ids") or [])
            if str(value).strip()
        ]
        granted_threads_total = requested_threads * len(assigned_user_ids) if workspace_kind == "live" else 0
        max_threads = self._fixed_live_worker_thread_limit() if workspace_kind == "live" else requested_threads
        is_backup_live_bot = workspace_kind == "live" and bot_function_key == "backup"
        workload_text = (
            f"0/0/{granted_threads_total}"
            if is_backup_live_bot
            else f"0/{granted_threads_total}"
            if workspace_kind == "live"
            else "--"
        )
        workload_note = (
            "Chạy / Chờ / Tổng"
            if is_backup_live_bot
            else "Chạy / Tổng"
        )
        is_overallocated = workspace_kind == "live" and granted_threads_total > max_threads
        bot_function_label = {
            "upload": "Upload",
            "backup": "Backup",
            "primary": "Live chính",
        }.get(bot_function_key, "Live chính")
        resolved_manager_id, resolved_manager_name = self._resolved_bot_manager_scope(
            manager_id=task.get("manager_id"),
            manager_name=task.get("manager_name"),
        )
        return {
            "index": 0,
            "id": str(task.get("worker_id") or "").strip(),
            "bot_id": str(task.get("worker_id") or "").strip(),
            "workspace_kind": workspace_kind,
            "manager_id": str(resolved_manager_id or "").strip(),
            "manager_name": resolved_manager_name,
            "name": placeholder_name,
            "raw_name": placeholder_name,
            "local": placeholder_local,
            "group": str(task.get("group") or "").strip() or "-",
            "bot_type_key": bot_type["key"],
            "bot_type_label": bot_type["label"],
            "bot_type_badge_class": bot_type["badge_class"],
            "bot_function_key": bot_function_key,
            "bot_function_label": bot_function_label,
            "assigned_user_id": "",
            "assigned_user_ids": [],
            "assigned_user_name": "",
            "assigned_user_names_text": "",
            "status_key": f"{task.get('kind')}-{task.get('status')}",
            "status_label": status_label,
            "status_class": status_class,
            "created_at": self._format_full_datetime(self._parse_datetime(task.get("created_at"))),
            "total_channels": 0,
            "total_users": 0,
            "thread_text": workload_text,
            "live_thread_text": workload_text,
            "workload_text": workload_text,
            "workload_note": workload_note,
            "workload_badge_class": (
                "border-rose-100 bg-rose-50 text-rose-700"
                if is_overallocated
                else bot_type["badge_class"]
            ),
            "running_threads": 0,
            "allocated_threads": granted_threads_total,
            "granted_threads_total": granted_threads_total,
            "max_threads": max_threads,
            "pending_threads": 0,
            "threads": requested_threads,
            "ram_text": "--",
            "ram_percent": 0,
            "disk_text": "--",
            "space_text": "--",
            "bandwidth_text": "--",
            "load_percent": 0,
            "cpu_percent": 0,
            "meta_text": str(task.get("message") or "").strip() or "Control-plane đang xử lý trên VPS.",
            "row_dimmed": True,
            "actions_disabled": True,
            "is_operation_placeholder": True,
            "operation_kind": str(task.get("kind") or "").strip(),
            "operation_status": str(task.get("status") or "").strip(),
        }

    def _apply_operation_state_to_worker_row(self, row: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
        status_label, status_class = self._worker_operation_badge(task)
        kind = str(task.get("kind") or "").strip()
        status = str(task.get("status") or "").strip()
        operation_failed = status == "failed"
        updated_row = dict(row)
        updated_row.update(
            {
                "status_key": f"{kind}-{status}",
                "status_label": status_label,
                "status_class": status_class,
                "meta_text": str(task.get("message") or "").strip() or updated_row.get("meta_text") or updated_row.get("manager_name") or "-",
                "row_dimmed": True,
                "actions_disabled": not operation_failed,
                "operation_kind": kind,
                "operation_status": status,
            }
        )
        return updated_row

    def _bot_type_badge_data(self, workspace_kind: str, live_role: str | None = None) -> dict[str, str]:
        resolved_workspace_kind = self._normalize_workspace_mode(workspace_kind)
        if resolved_workspace_kind != "live":
            return {
                "key": "upload",
                "label": "Upload",
                "badge_class": "border-sky-100 bg-sky-50 text-sky-700",
            }
        normalized_live_role = self._normalize_live_assignment_role(live_role) if str(live_role or "").strip() else ""
        if normalized_live_role == "backup":
            return {
                "key": "backup",
                "label": "Backup",
                "badge_class": "border-amber-100 bg-amber-50 text-amber-700",
            }
        return {
            "key": "live",
            "label": "Live",
                "badge_class": "border-emerald-100 bg-emerald-50 text-emerald-700",
            }

    def _live_worker_assigned_role(self, worker_id: str) -> str:
        role_values = {
            self._normalize_live_assignment_role(link.get("live_role"), fallback_note=link.get("note"))
            for link in self.live_user_worker_links
            if str(link.get("worker_id") or "").strip() == str(worker_id or "").strip()
        }
        if len(role_values) == 1:
            return next(iter(role_values))
        if role_values:
            return "mixed"
        return ""

    def _build_bot_rows(self, manager_ids: list[str] | None = None, *, workspace_mode: str = "upload") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        workspace_order = ["upload", "live"]
        operation_by_workspace: dict[str, dict[str, dict[str, Any]]] = {}
        operation_tasks_by_workspace: dict[str, list[dict[str, Any]]] = {}
        for current_workspace_kind in workspace_order:
            workspace_tasks = self._filtered_worker_operation_tasks(manager_ids, workspace_mode=current_workspace_kind)
            operation_tasks_by_workspace[current_workspace_kind] = workspace_tasks
            operation_by_workspace[current_workspace_kind] = {
                str(task.get("worker_id") or "").strip(): task
                for task in workspace_tasks
                if str(task.get("worker_id") or "").strip()
            }
        visible_worker_ids: set[str] = set()
        for current_workspace_kind in workspace_order:
            is_live_workspace = current_workspace_kind == "live"
            worker_source = self._filtered_live_workers(manager_ids) if is_live_workspace else self._filtered_workers(manager_ids)
            operation_by_worker_id = operation_by_workspace.get(current_workspace_kind, {})
            for worker in worker_source:
                assigned_users = self._assigned_live_users_for_worker(worker.id) if is_live_workspace else self._assigned_users_for_worker(worker.id)
                resolved_manager_id, resolved_manager_name = self._resolved_bot_manager_scope(
                    manager_id=worker.manager_id,
                    manager_name=worker.manager_name,
                    assigned_users=assigned_users,
                )
                assigned_live_role = ""
                assigned_live_role_label = ""
                if is_live_workspace:
                    assigned_live_role = self._live_worker_assigned_role(worker.id)
                    assigned_live_role_label = (
                        "Hỗn hợp"
                        if assigned_live_role == "mixed"
                        else self._live_assignment_role_label(assigned_live_role)
                        if assigned_live_role
                        else ""
                    )
                status_label, status_class = self._worker_status_badge(worker.status)
                thread_summary = (
                    self._live_worker_thread_summary(worker, live_role=assigned_live_role)
                    if is_live_workspace
                    else self._worker_thread_summary(worker)
                )
                bot_type = self._bot_type_badge_data(current_workspace_kind, assigned_live_role)
                bot_function_key = (
                    "upload"
                    if current_workspace_kind != "live"
                    else "backup" if assigned_live_role == "backup" else "primary"
                )
                bot_function_label = {
                    "upload": "Upload",
                    "backup": "Backup",
                    "primary": "Live chính",
                }.get(bot_function_key, "Live chính")
                worker_display_name = (
                    self._resolve_live_worker_display_name(worker.id)
                    if is_live_workspace
                    else self._resolve_worker_display_name(worker.id)
                )
                connection_profile = dict(self.worker_connection_profiles.get(worker.id) or {})
                worker_local = str(getattr(worker, "local", None) or "").strip()
                assigned_user_ids = [user.id for user in assigned_users]
                assigned_user_names = [user.username for user in assigned_users]
                if len(assigned_users) == 1:
                    assigned_user_id = assigned_users[0].id
                    assigned_user_name = assigned_users[0].username
                elif len(assigned_users) > 1:
                    assigned_user_id = ""
                    assigned_user_name = f"{len(assigned_users)} user"
                else:
                    assigned_user_id = ""
                    assigned_user_name = "BOT trống"
                row = {
                    "index": 0,
                    "id": worker.id,
                    "bot_id": worker.id,
                    "workspace_kind": current_workspace_kind,
                    "manager_id": resolved_manager_id,
                    "manager_name": resolved_manager_name,
                    "name": worker_display_name,
                    "raw_name": worker_display_name,
                    "local": worker_local,
                    "group": self._normalized_bot_group(worker.group),
                    "bot_type_key": bot_type["key"],
                    "bot_type_label": bot_type["label"],
                    "bot_type_badge_class": bot_type["badge_class"],
                    "bot_function_key": bot_function_key,
                    "bot_function_label": bot_function_label,
                    "assigned_user_id": assigned_user_id,
                    "assigned_user_ids": assigned_user_ids,
                    "connection_password": str(connection_profile.get("password") or "").strip(),
                    "assigned_user_name": assigned_user_name,
                    "assigned_user_names_text": ", ".join(assigned_user_names),
                    "assigned_live_role": assigned_live_role,
                    "assigned_live_role_label": assigned_live_role_label,
                    "live_type_key": assigned_live_role,
                    "live_type_label": assigned_live_role_label,
                    "status_key": worker.status,
                    "status_label": status_label,
                    "status_class": status_class,
                    "created_at": self._format_full_datetime(worker.created_at or worker.last_seen_at),
                    "total_channels": 0 if is_live_workspace else len([channel for channel in self.channels if channel.worker_id == worker.id]),
                    "total_users": len(
                        [
                            link
                            for link in (self.live_user_worker_links if is_live_workspace else self.user_worker_links)
                            if link["worker_id"] == worker.id
                        ]
                    ),
                    "thread_text": thread_summary["thread_text"],
                    "live_thread_text": thread_summary["thread_text"],
                    "workload_text": thread_summary["thread_text"],
                    "workload_note": thread_summary.get("note_text", "Chạy / Tổng"),
                    "workload_badge_class": (
                        "border-rose-100 bg-rose-50 text-rose-700"
                        if is_live_workspace and bool(thread_summary.get("is_overallocated"))
                        else bot_type["badge_class"]
                    ),
                    "running_threads": thread_summary["running_threads"],
                    "allocated_threads": thread_summary.get("allocated_threads", 0),
                    "granted_threads_total": thread_summary.get(
                        "granted_threads_total",
                        thread_summary.get("allocated_threads", 0),
                    ),
                    "max_threads": thread_summary["max_threads"],
                    "threads": (
                        self._live_worker_default_assignment_threads(worker.id)
                        if is_live_workspace
                        else worker.threads
                    ),
                    "ram_text": f"{worker.ram_used_gb:.1f}GB / {worker.ram_total_gb:.1f}GB"
                    if worker.ram_total_gb > 0
                    else "--",
                    "ram_percent": worker.ram_percent,
                    "disk_text": f"{worker.disk_used_gb:.1f}/{worker.disk_total_gb:.1f}GB",
                    "space_text": f"{worker.disk_used_gb:.1f}/{worker.disk_total_gb:.1f}GB",
                    "bandwidth_text": f"{worker.bandwidth_kbps:.2f}KB/s",
                    "load_percent": worker.load_percent,
                    "cpu_percent": worker.load_percent,
                    "meta_text": resolved_manager_name,
                    "row_dimmed": False,
                    "actions_disabled": False,
                    "is_operation_placeholder": False,
                    "operation_kind": "",
                    "operation_status": "",
                }
                worker_task = operation_by_worker_id.get(worker.id)
                if worker_task is not None:
                    row = self._apply_operation_state_to_worker_row(row, worker_task)
                rows.append(row)
                visible_worker_ids.add(f"{current_workspace_kind}:{worker.id}")
            for task in operation_tasks_by_workspace.get(current_workspace_kind, []):
                if str(task.get("kind") or "").strip() != "install":
                    continue
                task_worker_id = str(task.get("worker_id") or "").strip()
                visible_key = f"{current_workspace_kind}:{task_worker_id}"
                if visible_key in visible_worker_ids:
                    continue
                rows.append(self._build_operation_placeholder_row(task))
        for index, row in enumerate(rows, start=1):
            row["index"] = index
        return rows

    def get_admin_bot_index_context(
        self,
        *,
        manager_ids: list[str] | None = None,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        focus_user_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        worker_rows = self._build_bot_rows(manager_ids, workspace_mode=workspace_mode)
        context = self._admin_shell_context(
            page_title="Danh sách BOT",
            active_page="workers",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
            notice=notice,
            notice_level=notice_level,
        )
        focus_user: dict[str, Any] | None = None
        if focus_user_id:
            user = self._find_user(focus_user_id)
            resolved_manager_id = user.id if user.role == "manager" else (self._resolved_user_manager_id(user) or "")
            assigned_workers = (
                self._workspace_live_workers_for_user(user.id)
                if workspace_mode == "live" and user.role in {"user", "manager", "admin"}
                else self._workspace_workers_for_user(user.id) if user.role in {"user", "manager", "admin"} else []
            )
            focus_user = {
                "id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "role": user.role,
                "manager_id": resolved_manager_id,
                "manager_name": user.manager_name or (user.username if user.role == "manager" else "-"),
                "assigned_worker_id": assigned_workers[0].id if assigned_workers else "",
                "assigned_worker_name": (
                    self._resolve_live_worker_display_name(assigned_workers[0].id)
                    if workspace_mode == "live" and assigned_workers
                    else self._resolve_worker_display_name(assigned_workers[0].id) if assigned_workers else ""
                ),
                "assigned_worker_ids": [worker.id for worker in assigned_workers],
                "assigned_worker_names": [
                    self._resolve_live_worker_display_name(worker.id) if workspace_mode == "live" else self._resolve_worker_display_name(worker.id)
                    for worker in assigned_workers
                ],
                "assigned_worker_count": len(assigned_workers),
                "total_channels": self._user_channel_count(user),
            }
        context.update(
            {
                "template": "admin/worker_index.html",
                "workspace_heading": "Admin",
                "hide_workspace_tabs": True,
                "workers": worker_rows,
                "summary_strip": self._combined_bot_summary_strip(
                    viewer_role=viewer_role,
                    viewer_id=viewer_id,
                    manager_ids=manager_ids,
                ),
                "worker_event_cursor": self._latest_admin_notification_id(manager_ids=manager_ids),
                "worker_event_started_at": self._now(trim=False).isoformat(),
                "focus_user": focus_user,
                "manager_binding_locked": viewer_role == "manager",
                "bot_manager_options": self._bot_manager_options(viewer_role=viewer_role, viewer_id=viewer_id),
                "bot_user_options": self._combined_bot_user_options(
                    viewer_role=viewer_role,
                    viewer_id=viewer_id,
                ),
                "selected_manager_labels": [
                    item["username"] for item in context.get("manager_options", []) if item.get("selected")
                ],
            }
        )
        if viewer_role == "manager" and viewer_id:
            context["manager_options"] = [item for item in context["manager_options"] if item["id"] == viewer_id]
        return context

    def get_admin_bots_of_user_context(
        self,
        *,
        user_id: str,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        user = self._find_user(user_id)
        is_live_workspace = workspace_mode == "live"
        worker_pool = self.live_workers if is_live_workspace else self.workers
        if user.role == "user":
            worker_source = self._assigned_live_workers_for_user(user) if is_live_workspace else self._assigned_workers_for_user(user)
        elif user.role == "manager":
            worker_source = [worker for worker in worker_pool if worker.manager_name == user.username]
        else:
            worker_source = list(worker_pool)

        rows = []
        for index, worker in enumerate(worker_source, start=1):
            status_label, status_class = self._worker_status_badge(worker.status)
            rows.append(
                {
                    "index": index,
                    "id": worker.id,
                    "bot_id": worker.id,
                    "name": self._resolve_live_worker_display_name(worker.id) if is_live_workspace else self._resolve_worker_display_name(worker.id),
                    "group": self._normalized_bot_group(worker.group),
                    "status_label": status_label,
                    "status_class": status_class,
                    "created_at": self._format_full_datetime(worker.last_seen_at),
                    "total_channels": 0 if is_live_workspace else len([channel for channel in self.channels if channel.worker_id == worker.id]),
                    "total_users": len(
                        [
                            link
                            for link in (self.live_user_worker_links if is_live_workspace else self.user_worker_links)
                            if link["worker_id"] == worker.id
                        ]
                    ),
                    "threads": worker.threads,
                    "disk_text": f"{worker.disk_used_gb:.1f}/{worker.disk_total_gb:.1f}GB",
                    "bandwidth_text": f"{worker.bandwidth_kbps:.2f}KB/s",
                }
            )

        context = self._admin_shell_context(
            page_title=f"Danh sách BOT của {user.username}",
            active_page="workers",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/bot_of_user.html",
                "target_user": {
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.display_name,
                },
                "workers": rows,
            }
        )
        return context

    def get_admin_users_of_bot_context(
        self,
        *,
        worker_id: str,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        is_live_workspace = workspace_mode == "live"
        worker = self._find_live_worker(worker_id) if is_live_workspace else self._find_worker(worker_id)
        assigned_links = [
            link
            for link in (self.live_user_worker_links if is_live_workspace else self.user_worker_links)
            if link["worker_id"] == worker.id
        ]
        rows = []
        for index, mapping in enumerate(assigned_links, start=1):
            user_id = mapping["user_id"]
            user = self._find_user(user_id)
            rows.append(
                {
                    "index": index,
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.display_name,
                    "total_channels": self._user_channel_count(user),
                    "total_bots": self._user_live_worker_count(user) if is_live_workspace else self._user_worker_count(user),
                    "live_role": (
                        self._normalize_live_assignment_role(mapping.get("live_role"), fallback_note=mapping.get("note"))
                        if is_live_workspace
                        else ""
                    ),
                    "live_role_label": (
                        self._live_assignment_role_label(mapping.get("live_role"))
                        if is_live_workspace
                        else ""
                    ),
                }
            )

        context = self._admin_shell_context(
            page_title=(
                f"Danh sách user của {self._resolve_live_worker_display_name(worker.id)}"
                if is_live_workspace
                else f"Danh sách user của {self._resolve_worker_display_name(worker.id)}"
            ),
            active_page="workers",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/user_of_bot.html",
                "target_bot": {
                    "id": worker.id,
                    "name": self._resolve_live_worker_display_name(worker.id) if is_live_workspace else self._resolve_worker_display_name(worker.id),
                },
                "users": rows,
            }
        )
        return context

    def _live_summary_strip(
        self,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        manager_ids: list[str] | None = None,
        user_id: str | None = None,
    ) -> list[dict[str, str]]:
        selected_manager_ids = self._effective_manager_scope_ids(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        normalized_user_id = str(user_id or "").strip()
        scoped_workers = [
            worker for worker in self.live_workers if not selected_manager_ids or worker.manager_id in selected_manager_ids
        ]
        scoped_worker_ids = {worker.id for worker in scoped_workers}
        scoped_user_ids = {
            str(link.get("user_id") or "").strip()
            for link in self.live_user_worker_links
            if str(link.get("user_id") or "").strip()
            and (
                not selected_manager_ids
                or str(link.get("worker_id") or "").strip() in scoped_worker_ids
            )
        }
        scoped_users = [
            user
            for user in self.users
            if user.id in scoped_user_ids
            and user.role in {"user", "manager", "admin"}
        ]
        if normalized_user_id:
            owner_scoped_worker_ids = {
                str(link.get("worker_id") or "").strip()
                for link in self.live_user_worker_links
                if str(link.get("user_id") or "").strip() == normalized_user_id
            }
            scoped_workers = [worker for worker in scoped_workers if worker.id in owner_scoped_worker_ids]
            scoped_users = [user for user in scoped_users if user.id == normalized_user_id]
            if not scoped_users:
                try:
                    scoped_owner = self._find_user(normalized_user_id)
                except KeyError:
                    scoped_owner = None
                if scoped_owner is not None and scoped_owner.role in {"user", "manager", "admin"}:
                    owner_visible_in_scope = not selected_manager_ids or bool(
                        owner_scoped_worker_ids & scoped_worker_ids
                    )
                    if not owner_visible_in_scope and scoped_owner.role == "manager":
                        owner_visible_in_scope = scoped_owner.id in selected_manager_ids
                    if not owner_visible_in_scope and scoped_owner.role == "user":
                        owner_visible_in_scope = self._resolved_user_manager_id(scoped_owner) in selected_manager_ids
                    if owner_visible_in_scope:
                        scoped_users = [scoped_owner]
        scoped_streams = self._scoped_live_streams(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
            owner_user_id=normalized_user_id or None,
        )
        live_ready = len([worker for worker in scoped_workers if worker.status in {"online", "busy"}])
        active_live_count = len(
            [stream for stream in scoped_streams if self._effective_live_runtime_stream(stream).status == "streaming"]
        )
        scheduled_live_count = len(
            [
                stream
                for stream in scoped_streams
                if self._effective_live_runtime_stream(stream).status in self._live_pre_stream_statuses()
            ]
        )
        backup_live_count = len({stream.backup_worker_id for stream in scoped_streams if stream.backup_worker_id})
        return [
            {
                "value": f"{live_ready}/{len(scoped_workers)}",
                "label": "BOT Đang Chạy",
                "icon": "server",
                "icon_class": "text-brand-500",
                "accent": "Active",
                "accent_badge_class": "border-brand-100 bg-brand-50 text-brand-700",
                "value_class": "text-brand-600",
                "bar_color": "#6366f1",
            },
            {
                "value": str(len(scoped_users)),
                "label": "Tổng User",
                "icon": "users",
                "icon_class": "text-rose-500",
                "accent": "Accounts",
                "accent_badge_class": "border-rose-200 bg-rose-50 text-rose-700",
                "value_class": "text-slate-900",
                "bar_color": "#f43f5e",
            },
            {
                "value": str(active_live_count),
                "label": "Đang Live",
                "icon": "radio",
                "icon_class": "text-brand-500",
                "accent": "Live",
                "accent_badge_class": "border-indigo-200 bg-indigo-50 text-indigo-700",
                "value_class": "text-brand-600" if active_live_count else "text-slate-900",
                "bar_color": "#5d67df",
            },
            {
                "value": str(scheduled_live_count),
                "label": "Chờ Lên Lịch",
                "icon": "clock-3",
                "icon_class": "text-sky-500",
                "accent": "Queue",
                "accent_badge_class": "border-sky-200 bg-sky-50 text-sky-700",
                "value_class": "text-sky-500" if scheduled_live_count else "text-slate-900",
                "bar_color": "#0284c7",
            },
            {
                "value": str(backup_live_count),
                "label": "BOT Backup",
                "icon": "shield-check",
                "icon_class": "text-amber-500",
                "accent": "Fallback",
                "accent_badge_class": "border-amber-200 bg-amber-50 text-amber-700",
                "value_class": "text-amber-600" if backup_live_count else "text-slate-900",
                "bar_color": "#f59e0b",
            },
        ]

    def get_admin_live_workspace_context(
        self,
        *,
        manager_ids: list[str] | None = None,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
        live_form_values: dict[str, Any] | None = None,
        editing_stream_id: str | None = None,
        detail_stream_id: str | None = None,
        owner_user_id: str | None = None,
    ) -> dict[str, Any]:
        effective_manager_ids = list(
            self._effective_manager_scope_ids(
                viewer_role=viewer_role,
                viewer_id=viewer_id,
                manager_ids=manager_ids,
            )
        )
        normalized_owner_user_id = str(owner_user_id or "").strip()
        if normalized_owner_user_id:
            owner = self._require_workspace_user(normalized_owner_user_id)
            self._assert_live_stream_owner_scope(owner, viewer_role=viewer_role, viewer_id=viewer_id)
            context = self.get_user_live_workspace_view(
                user_id=owner.id,
                notice=notice,
                notice_level=notice_level,
                live_form_values=live_form_values,
                editing_stream_id=editing_stream_id,
                detail_stream_id=detail_stream_id,
            )
            live_context_query, live_context_suffix = self._live_workspace_scope_query(
                manager_ids=effective_manager_ids,
                user_id=owner.id,
            )
            context.update(
                {
                    "template": "user_live_dashboard.html",
                    "admin_shell": True,
                    "workspace_label": "Manager workspace" if viewer_role == "manager" else "Admin workspace",
                    "active_page": "live_workspace",
                    "nav_items": self._admin_nav_items("live"),
                    "user_name": "Manager" if viewer_role == "manager" else "Admin",
                    "user_role": "Manager scope" if viewer_role == "manager" else "Control plane",
                    "logout_path": "/admin/logout",
                    "live_form_action": "/admin/live/update" if context.get("editing_stream_id") else "/admin/live/create",
                    "live_form_manager_ids": effective_manager_ids,
                    "live_form_user_id": owner.id,
                    "live_form_cancel_href": "/admin/live" if context.get("editing_stream_id") else "",
                    "live_index_href": "/admin/live",
                    "live_delete_action": "/admin/live/delete",
                    "live_stop_action": "/admin/live/stop",
                    "live_context_query": live_context_query,
                    "live_context_suffix": live_context_suffix,
                    "live_dashboard_api_href": f"/api/admin/live/dashboard{live_context_query}",
                }
            )
            return context

        effective_manager_ids = set(effective_manager_ids)
        scoped_workers = [
            worker for worker in self.live_workers if not effective_manager_ids or worker.manager_id in effective_manager_ids
        ]
        scoped_streams = self._scoped_live_streams(
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
        )
        resolved_notice = notice
        resolved_notice_level = notice_level
        editing_stream = None
        if editing_stream_id:
            candidate_stream = self.get_live_stream(editing_stream_id, viewer_role=viewer_role, viewer_id=viewer_id)
            if self._is_live_stream_runtime_locked(candidate_stream):
                if not resolved_notice:
                    resolved_notice = "Luồng đang được worker xử lý hoặc đang live. Hãy dùng Dừng trước khi chỉnh sửa."
                    resolved_notice_level = "error"
            else:
                editing_stream = candidate_stream
        detail_stream = None
        if detail_stream_id:
            detail_stream = self.get_live_stream(detail_stream_id, viewer_role=viewer_role, viewer_id=viewer_id)
        primary_worker_ids = {
            str(link.get("worker_id") or "").strip()
            for link in self.live_user_worker_links
            if self._normalize_live_assignment_role(link.get("live_role"), fallback_note=link.get("note")) == "primary"
        }
        backup_worker_ids = {
            str(link.get("worker_id") or "").strip()
            for link in self.live_user_worker_links
            if self._normalize_live_assignment_role(link.get("live_role"), fallback_note=link.get("note")) == "backup"
        }
        primary_workers = [worker for worker in scoped_workers if worker.id in primary_worker_ids]
        backup_workers = [worker for worker in scoped_workers if worker.id in backup_worker_ids]
        effective_form_values = live_form_values or (self._build_live_form_values_from_stream(editing_stream) if editing_stream else None)
        live_form_state = self._build_live_form_state(effective_form_values)
        live_ready = len([worker for worker in scoped_workers if worker.status in {"online", "busy"}])
        total_workers = len(scoped_workers)
        assigned_bot_count = total_workers
        active_live_count = len(
            [stream for stream in scoped_streams if self._effective_live_runtime_stream(stream).status == "streaming"]
        )
        scheduled_live_count = len(
            [
                stream
                for stream in scoped_streams
                if self._effective_live_runtime_stream(stream).status in self._live_pre_stream_statuses()
            ]
        )
        backup_live_count = len({stream.backup_worker_id for stream in scoped_streams if stream.backup_worker_id})
        kpis = [
            {
                "label": "BOT được cấp",
                "icon": "server",
                "icon_class": "text-emerald-500",
                "value": assigned_bot_count,
                "accent": "Live + Backup",
                "accent_class": "text-emerald-600",
                "value_class": "text-emerald-600",
                "bar_class": "bg-emerald-400",
            },
            {
                "label": "BOT live sẵn sàng",
                "icon": "server-cog",
                "icon_class": "text-brand-500",
                "value": live_ready,
                "accent": f"{live_ready}/{total_workers} BOT",
                "accent_class": "text-brand-600",
                "value_class": "text-brand-600",
                "bar_class": "bg-brand-500",
            },
            {
                "label": "Đang live",
                "icon": "radio",
                "icon_class": "text-rose-500",
                "value": active_live_count,
                "accent": "Live",
                "accent_class": "text-rose-500",
                "value_class": "text-slate-900" if active_live_count == 0 else "text-rose-500",
                "bar_class": "bg-rose-400",
            },
            {
                "label": "Chờ lên lịch",
                "icon": "clock-3",
                "icon_class": "text-amber-500",
                "value": scheduled_live_count,
                "accent": "Lịch",
                "accent_class": "text-amber-600",
                "value_class": "text-slate-900" if scheduled_live_count == 0 else "text-amber-600",
                "bar_class": "bg-amber-400",
            },
            {
                "label": "BOT backup",
                "icon": "shield-check",
                "icon_class": "text-sky-500",
                "value": backup_live_count,
                "accent": "Fallback",
                "accent_class": "text-sky-500",
                "value_class": "text-slate-900" if backup_live_count == 0 else "text-sky-500",
                "bar_class": "bg-sky-400",
            },
        ]
        return {
            "template": "user_live_dashboard.html",
            "page_title": "Điều phối live",
            "admin_shell": True,
            "app_name": "Youtube Lush",
            "workspace_label": "Admin workspace",
            "active_page": "live_workspace",
            "nav_items": self._admin_nav_items("live"),
            "user_name": "Admin" if viewer_role == "admin" else "Manager",
            "user_role": "Control plane",
            "logout_path": "/admin/logout",
            "notice": resolved_notice,
            "notice_level": resolved_notice_level,
            "kpis": kpis,
            "live_config": {
                "title": "Live Config",
                "description": "Thiết lập BOT chính, BOT backup, media nguồn và lịch live.",
            },
            "live_form_action": "/admin/live/update" if editing_stream else "/admin/live/create",
            "live_form_manager_ids": list(effective_manager_ids),
            "live_form_user_id": "",
            "live_form_mode": "edit" if editing_stream else "create",
            "live_form_submit_label": "Cập nhật luồng live" if editing_stream else "Tạo luồng live",
            "live_form_reset_label": "Hủy chỉnh sửa" if editing_stream else "Đặt lại",
            "live_form_cancel_href": "/admin/live" if editing_stream else "",
            "live_index_href": "/admin/live",
            "live_delete_action": "/admin/live/delete",
            "live_stop_action": "/admin/live/stop",
            "live_context_query": "",
            "live_context_suffix": "",
            "live_owner_user_id": str(viewer_id or "").strip(),
            "live_dashboard_api_href": "/api/admin/live/dashboard",
            "live_form": live_form_state,
            "primary_live_workers": self._build_live_worker_picker_cards(primary_workers),
            "backup_live_workers": self._build_live_worker_picker_cards(backup_workers),
            "live_workers": self._build_live_worker_picker_cards(scoped_workers),
            "editing_stream_id": editing_stream.id if editing_stream else "",
            "detail_stream": self._live_stream_detail_payload(detail_stream) if detail_stream else None,
            "live_tabs": [
                {"label": "Danh sách live", "count": len(scoped_streams), "active": True},
                {"label": "Chờ lên lịch", "count": scheduled_live_count, "active": False},
            ],
            "live_stream_rows": self._build_live_stream_rows(scoped_streams),
            "live_summary": (
                f"Hiển thị 1 đến {len(scoped_streams)} trong {len(scoped_streams)} kết quả"
                if scoped_streams
                else "Chưa có luồng live nào trong danh sách"
            ),
        }

    def create_admin_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role: str,
        manager_id: str | None,
        telegram: str | None,
        updated_by: str = "admin",
    ) -> dict[str, str]:
        username = username.strip()
        display_name = display_name.strip()
        role = role.strip().lower()
        if not username or not display_name or not password.strip():
            raise ValueError("UserName, DisplayName và Password là bắt buộc.")
        if any(user.username.lower() == username.lower() for user in self.users):
            raise ValueError("UserName đã tồn tại.")
        if role not in {"user", "manager", "admin"}:
            raise ValueError("Role không hợp lệ.")

        manager_name: str | None = None
        if role == "user":
            if not manager_id:
                manager_name = None
            else:
                manager = self._find_user(manager_id)
                if manager.role != "manager":
                    raise ValueError("Manager được chọn không hợp lệ.")
                manager_name = manager.username

        user_id = f"user-{uuid4().hex[:8]}"
        self.users.append(UserSummary(id=user_id, username=username, display_name=display_name, role=role, manager_name=manager_name))  # type: ignore[arg-type]
        self.user_meta[user_id] = {
            "password_hash": self._hash_password(password.strip()),
            "password_algo": "pbkdf2_sha256",
            "password": password.strip(),
            "telegram": (telegram or "").strip(),
            "telegram_live": "",
            "updated_by": updated_by,
            "updated_at": self._now(),
            "created_at": self._now(),
        }
        self._save_state()
        return {"user_id": user_id, "username": username}

    def _validate_admin_username(self, username: str, *, exclude_user_id: str | None = None) -> str:
        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("Tên đăng nhập là bắt buộc.")
        if any(
            user.id != exclude_user_id and user.username.lower() == normalized_username.lower()
            for user in self.users
        ):
            raise ValueError("Tên đăng nhập đã tồn tại.")
        return normalized_username

    def _cascade_manager_username_change(self, old_username: str, new_username: str) -> None:
        if old_username == new_username:
            return
        for user in self.users:
            if user.role == "user" and user.manager_name == old_username:
                user.manager_name = new_username
        for worker in self.workers:
            if worker.manager_name == old_username:
                worker.manager_name = new_username
        for channel in self.channels:
            if channel.manager_name == old_username:
                channel.manager_name = new_username
        for job in self.jobs:
            if job.manager_name == old_username:
                job.manager_name = new_username

    def delete_admin_user(self, user_id: str) -> None:
        user = self._find_user(user_id)
        if user.role == "admin" and len([item for item in self.users if item.role == "admin"]) <= 1:
            raise ValueError("Không thể xóa admin cuối cùng.")
        if user.role == "manager" and any(item.role == "user" and item.manager_name == user.username for item in self.users):
            raise ValueError("Manager này đang quản lý user khác, chưa thể xóa.")

        self.users = [item for item in self.users if item.id != user_id]
        self.user_meta.pop(user_id, None)
        self.user_worker_links = [item for item in self.user_worker_links if item["user_id"] != user_id]
        self.channel_user_links = [item for item in self.channel_user_links if item["user_id"] != user_id]
        for session in [item for item in self.upload_sessions if item.owner_user_id == user_id]:
            self._remove_upload_session_file(session)
        self.upload_sessions = [item for item in self.upload_sessions if item.owner_user_id != user_id]
        self.browser_sessions = [item for item in self.browser_sessions if item.owner_user_id != user_id]
        self._save_state()

    def reset_admin_user_password(self, user_id: str, password: str, updated_by: str = "admin") -> None:
        if not password.strip():
            raise ValueError("Password mới là bắt buộc.")
        self._find_user(user_id)
        meta = self._user_meta_record(user_id)
        self._set_user_password(user_id, password.strip())
        meta["updated_by"] = updated_by
        meta["updated_at"] = self._now()
        self._save_state()

    def update_admin_user(
        self,
        *,
        user_id: str,
        username: str,
        password: str | None,
        manager_id: str | None,
        telegram: str | None = None,
        telegram_live: str | None = None,
        actor_role: str = "admin",
        updated_by: str = "admin",
    ) -> UserSummary:
        user = self._find_user(user_id)
        previous_telegram = self._user_telegram_chat_id(user_id)
        next_telegram = previous_telegram
        previous_telegram_live = self._user_telegram_live_chat_id(user_id)
        next_telegram_live = previous_telegram_live

        old_username = user.username
        can_edit_username = actor_role == "admin" or (actor_role == "manager" and user.role == "user")
        if can_edit_username:
            normalized_username = self._validate_admin_username(username, exclude_user_id=user.id)
            user.username = normalized_username
            if user.role == "manager":
                self._cascade_manager_username_change(old_username, user.username)

        user.display_name = user.username

        if user.role == "user":
            if not manager_id:
                user.manager_name = None
            else:
                manager = self._find_user(manager_id)
                if manager.role != "manager":
                    raise ValueError("Manager được chọn không hợp lệ.")
                user.manager_name = manager.username
        else:
            user.manager_name = None

        meta = self._user_meta_record(user_id)
        if telegram is not None:
            normalized_telegram = self._normalize_telegram_chat_id(telegram)
            if user.role == "user":
                next_telegram_live = normalized_telegram
                meta["telegram_live"] = next_telegram_live
            else:
                next_telegram = normalized_telegram
                meta["telegram"] = next_telegram
        if telegram_live is not None:
            next_telegram_live = self._normalize_telegram_chat_id(telegram_live)
            meta["telegram_live"] = next_telegram_live
        if password and password.strip():
            self._set_user_password(user_id, password.strip(), updated_at=self._now())
        meta["updated_by"] = updated_by
        meta["updated_at"] = self._now()
        self._save_state()
        if next_telegram and next_telegram != previous_telegram:
            self._send_telegram_alert(
                self._telegram_linked_confirmation_message(user),
                chat_id=next_telegram,
            )
        elif previous_telegram and not next_telegram:
            self._send_telegram_alert(
                self._telegram_unlinked_confirmation_message(user),
                chat_id=previous_telegram,
            )
        if next_telegram_live and next_telegram_live != previous_telegram_live:
            self._send_telegram_live_alert(
                self._telegram_live_linked_confirmation_message(user),
                chat_id=next_telegram_live,
            )
        elif previous_telegram_live and not next_telegram_live:
            self._send_telegram_live_alert(
                self._telegram_live_unlinked_confirmation_message(user),
                chat_id=previous_telegram_live,
            )
        return user

    def update_role_manager(self, user_id: str, promote: bool, updated_by: str = "admin") -> None:
        user = self._find_user(user_id)
        if promote:
            if user.role == "admin":
                raise ValueError("Admin hiện tại không cần gán thêm quyền manager.")
            user.role = "manager"
            user.manager_name = None
        else:
            if user.role != "manager":
                raise ValueError("User này không phải manager.")
            if any(item.role == "user" and item.manager_name == user.username for item in self.users):
                raise ValueError("Manager này đang quản lý user khác, chưa thể gỡ quyền.")
            user.role = "user"
            user.manager_name = None

        meta = self._user_meta_record(user_id)
        meta["updated_by"] = updated_by
        meta["updated_at"] = self._now()
        self._save_state()

    def update_role_admin(self, user_id: str, promote: bool, updated_by: str = "admin") -> None:
        user = self._find_user(user_id)
        if promote:
            user.role = "admin"
            user.manager_name = None
        else:
            if user.role != "admin":
                raise ValueError("User này không phải admin.")
            if len([item for item in self.users if item.role == "admin"]) <= 1:
                raise ValueError("Không thể gỡ quyền admin cuối cùng.")
            user.role = "user"
            user.manager_name = None

        meta = self._user_meta_record(user_id)
        meta["updated_by"] = updated_by
        meta["updated_at"] = self._now()
        self._save_state()

    def suggest_next_live_worker_bootstrap_id(self) -> str:
        return self._suggest_next_workspace_worker_bootstrap_id(
            [worker.id for worker in self.live_workers],
            prefix="live-worker",
        )

    def create_live_bot(
        self,
        *,
        name: str,
        manager_id: str | None,
        threads: int | None = None,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> WorkerRecord:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Tên BOT là bắt buộc.")
        normalized_capacity = self._fixed_live_worker_thread_limit()
        manager: UserSummary | None = None
        if manager_id:
            manager = self._find_user(manager_id)
            if manager.role != "manager":
                raise ValueError("Manager được chọn không hợp lệ.")
        if viewer_role == "manager":
            if manager is None or (viewer_id and manager.id != viewer_id):
                raise ValueError("Manager không được tạo BOT ngoài scope của mình.")
        worker = WorkerRecord(
            id=self.suggest_next_live_worker_bootstrap_id(),
            name=normalized_name,
            manager_id=manager.id if manager else None,
            manager_name=manager.username if manager else "system",
            group=self._default_bot_group_label(),
            created_at=self._now(trim=False),
            status="offline",
            capacity=normalized_capacity,
            load_percent=0,
            ram_percent=0,
            ram_used_gb=0.0,
            ram_total_gb=8.0,
            bandwidth_kbps=0,
            disk_used_gb=0.0,
            disk_total_gb=512.0,
            threads=normalized_capacity,
            last_seen_at=self._now(trim=False),
        )
        self.live_workers.append(worker)
        self._save_state()
        return deepcopy(worker)

    def _apply_live_worker_manager(self, worker: WorkerRecord, manager: UserSummary | None) -> None:
        worker.manager_id = manager.id if manager else None
        worker.manager_name = manager.username if manager else "system"
        if not str(worker.group or "").strip():
            worker.group = self._default_bot_group_label()

    def _purge_live_worker_assignment_scope_from_state(
        self,
        worker_id: str,
        *,
        live_user_worker_links: list[dict[str, Any]],
        live_streams: list[LiveStreamRecord],
        now: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], list[LiveStreamRecord]]:
        cleaned_worker_id = str(worker_id or "").strip()
        if not cleaned_worker_id:
            return live_user_worker_links, live_streams
        normalized_now = now or self._now(trim=False)
        next_live_user_worker_links = [
            link
            for link in live_user_worker_links
            if str(link.get("worker_id") or "").strip() != cleaned_worker_id
        ]
        remaining_streams: list[LiveStreamRecord] = []
        for stream in live_streams:
            if stream.primary_worker_id == cleaned_worker_id:
                continue
            if stream.backup_worker_id == cleaned_worker_id:
                stream.backup_worker_id = None
                stream.backup_worker_name = None
                stream.backup_group = None
                stream.updated_at = normalized_now
            remaining_streams.append(stream)
        return next_live_user_worker_links, remaining_streams

    def _purge_live_worker_assignment_scope(self, worker_id: str) -> None:
        self.live_user_worker_links, self.live_streams = self._purge_live_worker_assignment_scope_from_state(
            worker_id,
            live_user_worker_links=self.live_user_worker_links,
            live_streams=self.live_streams,
        )

    def update_live_bot(
        self,
        worker_id: str,
        name: str,
        local: str | None,
        group: str | None,
        manager_id: str | None,
        *,
        password: str | None = None,
        live_role: str | None = None,
        threads: int | None = None,
        assigned_user_id: str | None = None,
        assigned_user_ids: list[str] | None = None,
        confirm_manager_transfer_cleanup: bool = False,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> None:
        worker = self._find_live_worker(worker_id)
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Tên BOT là bắt buộc.")
        normalized_local = str(local or "").strip() or None
        normalized_group = self._normalized_bot_group(group)
        normalized_password = str(password or "").strip()
        existing_connection_password = str(
            (self.worker_connection_profiles.get(worker.id) or {}).get("password") or ""
        ).strip()
        password_changed = bool(normalized_password) and normalized_password != existing_connection_password
        normalized_user_id = str(assigned_user_id or "").strip()
        normalized_user_ids: list[str] = []
        seen_user_ids: set[str] = set()
        if assigned_user_ids is not None:
            for raw_user_id in assigned_user_ids:
                selected_user_id = str(raw_user_id or "").strip()
                if not selected_user_id or selected_user_id in seen_user_ids:
                    continue
                normalized_user_ids.append(selected_user_id)
                seen_user_ids.add(selected_user_id)
        manager: UserSummary | None = None
        normalized_manager_id = str(manager_id or "").strip()
        normalized_live_role = (
            self._normalize_live_assignment_role(live_role)
            if str(live_role or "").strip()
            else None
        )
        normalized_threads = self._normalize_live_worker_threads(
            threads if threads is not None else self._live_worker_default_assignment_threads(worker.id)
        )
        if normalized_manager_id:
            manager = self._find_user(normalized_manager_id)
            if manager.role != "manager":
                raise ValueError("Manager được chọn không hợp lệ.")
        if viewer_role == "manager":
            if manager is None or (viewer_id and manager.id != viewer_id):
                raise ValueError("Manager không được đổi phạm vi BOT sang manager khác.")
            if worker.manager_id and worker.manager_id != manager.id:
                raise ValueError("BOT này không nằm trong phạm vi manager hiện tại.")

        current_user_ids = {
            str(link.get("user_id") or "").strip()
            for link in self.live_user_worker_links
            if str(link.get("worker_id") or "").strip() == worker.id and str(link.get("user_id") or "").strip()
        }
        manager_changed = str(worker.manager_id or "").strip() != str(manager.id if manager else "").strip()
        has_live_scope_to_purge = bool(current_user_ids or any(stream.primary_worker_id == worker.id for stream in self.live_streams))
        should_purge_live_scope = manager_changed and has_live_scope_to_purge
        if should_purge_live_scope:
            if not confirm_manager_transfer_cleanup:
                raise ValueError(
                    "Đổi manager BOT live này sẽ làm sạch gán user và luồng live hiện tại. "
                    "Hãy xác nhận cảnh báo rồi thử lại."
                )
        current_user_ids_for_validation = set() if should_purge_live_scope else current_user_ids

        selected_users: list[UserSummary] = []
        target_user_ids = normalized_user_ids if assigned_user_ids is not None else ([normalized_user_id] if normalized_user_id else [])
        for selected_user_id in target_user_ids:
            assigned_user = self._find_user(selected_user_id)
            already_assigned = assigned_user.id in current_user_ids_for_validation
            if assigned_user.role == "user":
                assigned_user_manager_id = self._resolved_user_manager_id(assigned_user)
                if manager and assigned_user_manager_id != manager.id and not already_assigned:
                    raise ValueError("User phải thuộc manager đã chọn.")
            elif assigned_user.role == "manager":
                if manager and assigned_user.id != manager.id and not already_assigned:
                    raise ValueError("Manager chỉ được chọn chính mình trong BOT này.")
            elif assigned_user.role == "admin":
                if not already_assigned and (not viewer_id or assigned_user.id != viewer_id):
                    raise ValueError("Admin chỉ được tự gán BOT cho chính tài khoản admin đang đăng nhập.")
            else:
                raise ValueError("User được chọn không hợp lệ.")
            selected_users.append(assigned_user)

        next_live_user_worker_links = [dict(link) for link in self.live_user_worker_links]
        next_live_streams = self.live_streams
        if should_purge_live_scope:
            next_live_streams = deepcopy(self.live_streams)
            next_live_user_worker_links, next_live_streams = self._purge_live_worker_assignment_scope_from_state(
                worker.id,
                live_user_worker_links=next_live_user_worker_links,
                live_streams=next_live_streams,
            )

        if assigned_user_ids is not None or normalized_user_id:
            selected_user_id_set = {item.id for item in selected_users}
            current_links_by_user_id = {
                str(link.get("user_id") or "").strip(): link
                for link in next_live_user_worker_links
                if str(link.get("worker_id") or "").strip() == worker.id
            }
            deselected_user_ids = set(current_links_by_user_id.keys()) - selected_user_id_set
            for deselected_user_id in deselected_user_ids:
                deselected_link = current_links_by_user_id.get(deselected_user_id)
                if deselected_link is None:
                    continue
                self._assert_live_assignment_runtime_guard(
                    worker_id=worker.id,
                    user_id=deselected_user_id,
                    current_role=self._normalize_live_assignment_role(
                        deselected_link.get("live_role"),
                        fallback_note=deselected_link.get("note"),
                    ),
                    next_role=None,
                    proposed_threads=None,
                    removing_assignment=True,
                )

            next_live_user_worker_links = [
                link
                for link in next_live_user_worker_links
                if not (
                    str(link.get("worker_id") or "").strip() == worker.id
                    and str(link.get("user_id") or "").strip() not in selected_user_id_set
                )
            ]
            current_links_by_user_id = {
                str(link.get("user_id") or "").strip(): link
                for link in next_live_user_worker_links
                if str(link.get("worker_id") or "").strip() == worker.id
            }
            new_selected_user_ids = selected_user_id_set - set(current_links_by_user_id.keys())
            should_apply_threads_to_all_selected = not new_selected_user_ids
            assignment_changed = set(current_links_by_user_id.keys()) != selected_user_id_set
            if should_apply_threads_to_all_selected and not assignment_changed:
                for selected_user in selected_users:
                    existing_link = current_links_by_user_id.get(selected_user.id)
                    if existing_link is None:
                        assignment_changed = True
                        break
                    current_role = self._normalize_live_assignment_role(
                        existing_link.get("live_role"),
                        fallback_note=existing_link.get("note"),
                    )
                    selected_role = normalized_live_role or current_role
                    if current_role != selected_role:
                        assignment_changed = True
                        break
                    if self._live_link_allocated_threads(existing_link) != normalized_threads:
                        assignment_changed = True
                        break
            if should_apply_threads_to_all_selected:
                projected_total_allocated_threads = normalized_threads * len(selected_user_id_set)
            else:
                projected_total_allocated_threads = (
                    sum(
                        self._live_link_allocated_threads(link)
                        for user_id, link in current_links_by_user_id.items()
                        if user_id in selected_user_id_set
                    )
                    + (normalized_threads * len(new_selected_user_ids))
                )
            worker_capacity = self._effective_live_worker_thread_limit(worker)
            if assignment_changed and projected_total_allocated_threads > worker_capacity:
                raise ValueError(
                    f"BOT {self._resolve_live_worker_display_name(worker.id)} chỉ có trần {worker_capacity} luồng, "
                    f"nhưng cấu hình hiện tại đang cấp phát tổng cộng {projected_total_allocated_threads} luồng "
                    f"cho {len(selected_user_id_set)} user."
                )
            next_id = max([self._mapping_numeric_id(item.get("id")) for item in next_live_user_worker_links], default=0) + 1
            for assigned_user in selected_users:
                existing_link = current_links_by_user_id.get(assigned_user.id)
                selected_role = (
                    normalized_live_role
                    or (
                        self._normalize_live_assignment_role(existing_link.get("live_role"), fallback_note=existing_link.get("note"))
                        if existing_link is not None
                        else "primary"
                    )
                )
                if existing_link is None:
                    next_live_user_worker_links.append(
                        {
                            "id": next_id,
                            "user_id": assigned_user.id,
                            "worker_id": worker.id,
                            "allocated_threads": normalized_threads,
                            "threads": normalized_threads,
                            "live_role": selected_role,
                            "note": self._live_assignment_note(selected_role),
                            "created_at": self._now(trim=False),
                        }
                    )
                    next_id += 1
                else:
                    current_role = self._normalize_live_assignment_role(
                        existing_link.get("live_role"),
                        fallback_note=existing_link.get("note"),
                    )
                    next_threads = normalized_threads if should_apply_threads_to_all_selected else None
                    self._assert_live_assignment_runtime_guard(
                        worker_id=worker.id,
                        user_id=assigned_user.id,
                        current_role=current_role,
                        next_role=selected_role,
                        proposed_threads=next_threads,
                    )
                    if should_apply_threads_to_all_selected:
                        existing_link["allocated_threads"] = normalized_threads
                        existing_link["threads"] = normalized_threads
                    existing_link["live_role"] = selected_role
                    existing_link["note"] = self._live_assignment_note(selected_role)

        worker.name = normalized_name
        worker.local = normalized_local
        self._apply_live_worker_manager(worker, manager)
        worker.group = normalized_group
        worker.capacity = self._effective_live_worker_thread_limit(worker)
        worker.threads = worker.capacity
        self.live_user_worker_links = next_live_user_worker_links
        if next_live_streams is not self.live_streams:
            self.live_streams = next_live_streams
        if password_changed:
            self.update_worker_connection_password(worker.id, normalized_password, workspace_mode="live")
        self._save_state()

    def _delete_live_bot_state(self, worker_id: str) -> None:
        self._find_live_worker(worker_id)
        self._purge_live_worker_assignment_scope(worker_id)
        self.live_workers = [worker for worker in self.live_workers if worker.id != worker_id]
        self._clear_worker_registration_meta(worker_id)

    def delete_live_bot(self, worker_id: str) -> None:
        self._delete_live_bot_state(worker_id)
        self._save_state()

    def delete_live_bot_without_ssh(self, worker_id: str, *, deleted_by: str = "system") -> None:
        completed_task: dict[str, Any] | None = None
        worker_name = ""
        vps_ip = ""
        with self._worker_state_lock:
            worker = self._find_live_worker(worker_id)
            profile = dict(self.worker_connection_profiles.get(worker.id) or {})
            worker_name = self._resolve_live_worker_display_name(worker.id)
            vps_ip = str(profile.get("vps_ip") or "").strip()
            if not vps_ip and self._looks_like_ipv4(worker_name):
                vps_ip = worker_name
            actor = self._find_user_by_username(deleted_by)
            completed_task = {
                "worker_id": worker.id,
                "worker_name": worker_name,
                "vps_ip": vps_ip,
                "manager_name": str(worker.manager_name or "").strip() or "system",
                "workspace_mode": "live",
                "requested_by": str(deleted_by or "").strip() or "system",
                "requested_role": actor.role if actor else "system",
            }
            self._remember_deleted_worker(
                worker.id,
                worker_name=worker_name,
                vps_ip=vps_ip or None,
                deleted_by=deleted_by,
                reason="delete_without_ssh",
            )
            self._delete_live_bot_state(worker.id)
            self.worker_connection_profiles.pop(str(worker.id or "").strip(), None)
            self.worker_operation_tasks = [
                task
                for task in self.worker_operation_tasks
                if str(task.get("worker_id") or "").strip() != str(worker.id or "").strip()
            ]
            self._save_state()
        if completed_task is not None:
            self._notify_telegram_chat_ids(
                self._bot_operation_recipient_chat_ids(completed_task),
                self._bot_decommission_completed_message(
                    completed_task,
                    worker_name=worker_name,
                    worker_id=str(worker_id or "").strip(),
                    vps_ip=vps_ip or worker_name,
                ),
            )

    def update_live_bot_thread(self, worker_id: str, thread: int) -> None:
        worker = self._find_live_worker(worker_id)
        if thread < 1:
            raise ValueError("Số luồng phải lớn hơn hoặc bằng 1.")
        normalized_threads = self._normalize_live_worker_threads(thread)
        worker_capacity = self._effective_live_worker_thread_limit(worker)
        worker_link_count = len(
            [
                link
                for link in self.live_user_worker_links
                if str(link.get("worker_id") or "").strip() == worker.id
            ]
        )
        if normalized_threads * worker_link_count > worker_capacity:
            raise ValueError(
                f"BOT {self._resolve_live_worker_display_name(worker.id)} đang vượt trần "
                f"{normalized_threads * worker_link_count}/{worker_capacity} luồng."
            )
        next_live_user_worker_links = [dict(link) for link in self.live_user_worker_links]
        for link in next_live_user_worker_links:
            if str(link.get("worker_id") or "").strip() != worker.id:
                continue
            current_role = self._normalize_live_assignment_role(
                link.get("live_role"),
                fallback_note=link.get("note"),
            )
            self._assert_live_assignment_runtime_guard(
                worker_id=worker.id,
                user_id=str(link.get("user_id") or "").strip(),
                current_role=current_role,
                next_role=current_role,
                proposed_threads=normalized_threads,
            )
            link["allocated_threads"] = normalized_threads
            link["threads"] = normalized_threads
        worker.threads = worker_capacity
        worker.capacity = worker_capacity
        self.live_user_worker_links = next_live_user_worker_links
        self._save_state()

    def update_bot(
        self,
        worker_id: str,
        name: str,
        local: str | None,
        group: str | None,
        manager_id: str | None,
        *,
        workspace_mode: str = "upload",
        password: str | None = None,
        live_role: str | None = None,
        threads: int | None = None,
        assigned_user_id: str | None = None,
        assigned_user_ids: list[str] | None = None,
        confirm_manager_transfer_cleanup: bool = False,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        updated_by: str = "admin",
    ) -> None:
        if workspace_mode == "live":
            self.update_live_bot(
                worker_id,
                name,
                local,
                group,
                manager_id,
                password=password,
                live_role=live_role,
                threads=threads,
                assigned_user_id=assigned_user_id,
                assigned_user_ids=assigned_user_ids,
                confirm_manager_transfer_cleanup=confirm_manager_transfer_cleanup,
                viewer_role=viewer_role,
                viewer_id=viewer_id,
            )
            return
        worker = self._find_worker(worker_id)
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Tên BOT là bắt buộc.")
        normalized_local = str(local or "").strip() or None
        normalized_group = str(group or "").strip()
        normalized_password = str(password or "").strip()
        existing_connection_password = str(
            (self.worker_connection_profiles.get(worker.id) or {}).get("password") or ""
        ).strip()
        password_changed = bool(normalized_password) and normalized_password != existing_connection_password
        normalized_user_id = str(assigned_user_id or "").strip()
        normalized_user_ids: list[str] = []
        seen_user_ids: set[str] = set()
        if assigned_user_ids is not None:
            for raw_user_id in assigned_user_ids:
                user_id = str(raw_user_id or "").strip()
                if not user_id or user_id in seen_user_ids:
                    continue
                normalized_user_ids.append(user_id)
                seen_user_ids.add(user_id)
        manager: UserSummary | None = None
        normalized_manager_id = str(manager_id or "").strip()
        if normalized_manager_id:
            manager = self._find_user(normalized_manager_id)
            if manager.role != "manager":
                raise ValueError("Manager được chọn không hợp lệ.")

        if viewer_role == "manager":
            if manager is None:
                raise ValueError("Manager được chọn không hợp lệ.")
            if viewer_id and manager.id != viewer_id:
                raise ValueError("Manager không được đổi phạm vi BOT sang manager khác.")
            if worker.manager_id and worker.manager_id != manager.id:
                raise ValueError("BOT này không nằm trong phạm vi manager hiện tại.")

            current_worker_links = [
                link for link in self.user_worker_links if str(link.get("worker_id") or "").strip() == worker.id
            ]
            current_links_by_user_id = {
                str(link.get("user_id") or "").strip(): link
                for link in current_worker_links
                if str(link.get("user_id") or "").strip()
            }
            current_user_ids = set(current_links_by_user_id.keys())

            if assigned_user_ids is not None:
                selected_users: list[UserSummary] = []
                for selected_user_id in normalized_user_ids:
                    assigned_user = self._find_user(selected_user_id)
                    already_assigned = assigned_user.id in current_user_ids
                    if assigned_user.role == "user":
                        assigned_user_manager_id = self._resolved_user_manager_id(assigned_user)
                        if assigned_user_manager_id != manager.id and not already_assigned:
                            raise ValueError("User phải thuộc manager hiện tại.")
                    elif assigned_user.role == "manager":
                        if assigned_user.id != manager.id and not already_assigned:
                            raise ValueError("Manager chỉ được chọn chính mình trong BOT này.")
                    elif assigned_user.role == "admin":
                        if not already_assigned:
                            raise ValueError("User được chọn không hợp lệ.")
                    else:
                        raise ValueError("User được chọn không hợp lệ.")
                    selected_users.append(assigned_user)

                current_links_by_user_id = {
                    str(link.get("user_id") or "").strip(): link
                    for link in current_worker_links
                    if str(link.get("user_id") or "").strip()
                }
                selected_user_ids = {user.id for user in selected_users}

                for removed_user_id in current_user_ids - selected_user_ids:
                    self._clear_user_worker_channels(removed_user_id, worker.id)

                self.user_worker_links = [
                    link
                    for link in self.user_worker_links
                    if not (
                        str(link.get("worker_id") or "").strip() == worker.id
                        and str(link.get("user_id") or "").strip() in (current_user_ids - selected_user_ids)
                    )
                ]

                next_id = max([self._mapping_numeric_id(item.get("id")) for item in self.user_worker_links], default=0) + 1
                for assigned_user in selected_users:
                    existing_link = current_links_by_user_id.get(assigned_user.id)
                    if existing_link is None:
                        self.user_worker_links.append(
                            {
                                "id": next_id,
                                "user_id": assigned_user.id,
                                "worker_id": worker.id,
                                "threads": self._fixed_assignment_threads(),
                                "bot_type": "1080p",
                                "note": "VPS được cấp",
                                "created_at": self._now(trim=False),
                            }
                        )
                        next_id += 1
                    else:
                        existing_link["threads"] = self._fixed_assignment_threads()
                        existing_link["note"] = str(existing_link.get("note") or "").strip() or "VPS được cấp"
            elif normalized_user_id:
                assigned_user = self._find_user(normalized_user_id)
                if assigned_user.role == "user":
                    assigned_user_manager_id = self._resolved_user_manager_id(assigned_user)
                    if assigned_user_manager_id != manager.id:
                        raise ValueError("User phải thuộc manager hiện tại.")
                elif assigned_user.role == "manager":
                    if assigned_user.id != manager.id:
                        raise ValueError("Manager chỉ được chọn chính mình trong BOT này.")
                elif assigned_user.role == "admin":
                    if assigned_user.id not in current_user_ids:
                        raise ValueError("User được chọn không hợp lệ.")
                else:
                    raise ValueError("User được chọn không hợp lệ.")
                existing_link = next(
                    (
                        link
                        for link in self.user_worker_links
                        if str(link.get("worker_id") or "").strip() == worker.id
                        and str(link.get("user_id") or "").strip() == assigned_user.id
                    ),
                    None,
                )
                if existing_link is None:
                    next_id = max([self._mapping_numeric_id(item.get("id")) for item in self.user_worker_links], default=0) + 1
                    self.user_worker_links.append(
                        {
                            "id": next_id,
                            "user_id": assigned_user.id,
                            "worker_id": worker.id,
                            "threads": self._fixed_assignment_threads(),
                            "bot_type": "1080p",
                            "note": "VPS được cấp",
                            "created_at": self._now(trim=False),
                        }
                    )
                else:
                    existing_link["threads"] = self._fixed_assignment_threads()
                    existing_link["note"] = str(existing_link.get("note") or "").strip() or "VPS được cấp"

            worker.name = normalized_name
            worker.local = normalized_local
            self._apply_worker_manager(worker, manager)
            if normalized_group:
                worker.group = normalized_group
            if password_changed:
                self.update_worker_connection_password(worker.id, normalized_password, workspace_mode="upload")
            self._save_state()
            return

        current_manager_id = str(worker.manager_id or "").strip()
        next_manager_id = str(manager.id if manager else "").strip()
        manager_changed = current_manager_id != next_manager_id
        if manager_changed:
            worker_links = [link for link in self.user_worker_links if str(link.get("worker_id") or "").strip() == worker.id]
            worker_channels = [channel for channel in self.channels if str(channel.worker_id or "").strip() == worker.id]
            worker_jobs = [job for job in self.jobs if str(job.worker_name or "").strip() == worker.id]
            worker_link_user_ids = {
                str(link.get("user_id") or "").strip()
                for link in worker_links
                if str(link.get("user_id") or "").strip()
            }
            can_drop_self_workspace_link = (
                not worker_channels
                and not worker_jobs
                and viewer_id is not None
                and worker_link_user_ids == {str(viewer_id).strip()}
            )
            if can_drop_self_workspace_link:
                self.user_worker_links = [
                    link
                    for link in self.user_worker_links
                    if not (
                        str(link.get("worker_id") or "").strip() == worker.id
                        and str(link.get("user_id") or "").strip() == str(viewer_id).strip()
                    )
                ]
            elif worker_links or worker_channels or worker_jobs:
                if not confirm_manager_transfer_cleanup:
                    raise ValueError(
                        "Đổi manager BOT này sẽ xóa sạch dữ liệu user/kênh/job của manager hiện tại. "
                        "Hãy xác nhận cảnh báo rồi thử lại."
                    )
                self._purge_worker_assignment_scope(
                    worker.id,
                    session_reason="Session đã đóng do BOT đã được chuyển sang manager khác.",
                )
        worker.name = normalized_name
        worker.local = normalized_local
        self._apply_worker_manager(worker, manager)
        if normalized_group:
            worker.group = normalized_group
        if manager is None:
            if password_changed:
                self.update_worker_connection_password(worker.id, normalized_password, workspace_mode="upload")
            self._save_state()
            return
        current_worker_links = [
            link for link in self.user_worker_links if str(link.get("worker_id") or "").strip() == worker.id
        ]
        current_links_by_user_id = {
            str(link.get("user_id") or "").strip(): link
            for link in current_worker_links
            if str(link.get("user_id") or "").strip()
        }
        current_user_ids = set(current_links_by_user_id.keys())

        if assigned_user_ids is not None:
            selected_users: list[UserSummary] = []
            for selected_user_id in normalized_user_ids:
                assigned_user = self._find_user(selected_user_id)
                already_assigned = assigned_user.id in current_user_ids
                if assigned_user.role == "user":
                    assigned_user_manager_id = self._resolved_user_manager_id(assigned_user)
                    if assigned_user_manager_id != manager.id and not already_assigned:
                        raise ValueError("User phải thuộc manager đã chọn.")
                elif assigned_user.role == "manager":
                    if assigned_user.id != manager.id and not already_assigned:
                        raise ValueError("Manager chỉ được chọn chính mình trong BOT này.")
                elif assigned_user.role == "admin":
                    if not already_assigned and (not viewer_id or assigned_user.id != viewer_id):
                        raise ValueError("Admin chỉ được tự gán BOT cho chính tài khoản admin đang đăng nhập.")
                else:
                    raise ValueError("User được chọn không hợp lệ.")
                selected_users.append(assigned_user)

            current_links_by_user_id = {
                str(link.get("user_id") or "").strip(): link
                for link in current_worker_links
                if str(link.get("user_id") or "").strip()
            }
            selected_user_ids = {user.id for user in selected_users}

            for removed_user_id in current_user_ids - selected_user_ids:
                self._clear_user_worker_channels(removed_user_id, worker.id)

            self.user_worker_links = [
                link
                for link in self.user_worker_links
                if not (
                    str(link.get("worker_id") or "").strip() == worker.id
                    and str(link.get("user_id") or "").strip() in (current_user_ids - selected_user_ids)
                )
            ]

            next_id = max([self._mapping_numeric_id(item.get("id")) for item in self.user_worker_links], default=0) + 1
            for assigned_user in selected_users:
                existing_link = current_links_by_user_id.get(assigned_user.id)
                if existing_link is None:
                    self.user_worker_links.append(
                        {
                            "id": next_id,
                            "user_id": assigned_user.id,
                            "worker_id": worker.id,
                            "threads": self._fixed_assignment_threads(),
                            "bot_type": "1080p",
                            "note": "VPS được cấp",
                            "created_at": self._now(trim=False),
                        }
                    )
                    next_id += 1
                else:
                    existing_link["threads"] = self._fixed_assignment_threads()
                    existing_link["note"] = str(existing_link.get("note") or "").strip() or "VPS được cấp"
        elif normalized_user_id:
            assigned_user = self._find_user(normalized_user_id)
            if assigned_user.role == "user":
                assigned_user_manager_id = self._resolved_user_manager_id(assigned_user)
                if assigned_user_manager_id != manager.id:
                    raise ValueError("User phải thuộc manager đã chọn.")
            elif assigned_user.role == "manager":
                if assigned_user.id != manager.id:
                    raise ValueError("Manager chỉ được chọn chính mình trong BOT này.")
            elif assigned_user.role == "admin":
                if not viewer_id or assigned_user.id != viewer_id:
                    raise ValueError("Admin chỉ được tự gán BOT cho chính tài khoản admin đang đăng nhập.")
            else:
                raise ValueError("User được chọn không hợp lệ.")
            existing_link = next(
                (
                    link
                    for link in self.user_worker_links
                    if str(link.get("worker_id") or "").strip() == worker.id
                    and str(link.get("user_id") or "").strip() == assigned_user.id
                ),
                None,
            )
            if existing_link is None:
                next_id = max([self._mapping_numeric_id(item.get("id")) for item in self.user_worker_links], default=0) + 1
                self.user_worker_links.append(
                    {
                        "id": next_id,
                        "user_id": assigned_user.id,
                        "worker_id": worker.id,
                        "threads": self._fixed_assignment_threads(),
                        "bot_type": "1080p",
                        "note": "VPS được cấp",
                        "created_at": self._now(trim=False),
                    }
                )
            else:
                existing_link["threads"] = self._fixed_assignment_threads()
                existing_link["note"] = str(existing_link.get("note") or "").strip() or "VPS được cấp"
        if password_changed:
            self.update_worker_connection_password(worker.id, normalized_password, workspace_mode="upload")
        self._save_state()

    def reconcile_assignment_target_bots(
        self,
        worker_ids: list[str],
        *,
        manager_id: str | None,
        assigned_user_id: str | None = None,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        updated_by: str = "admin",
    ) -> int:
        normalized_worker_ids: list[str] = []
        seen_worker_ids: set[str] = set()
        for raw_worker_id in worker_ids:
            worker_id = str(raw_worker_id or "").strip()
            if not worker_id or worker_id in seen_worker_ids:
                continue
            normalized_worker_ids.append(worker_id)
            seen_worker_ids.add(worker_id)

        if not manager_id:
            raise ValueError("Vui lòng chọn manager.")

        manager = self._find_user(manager_id)
        allowed_manager_roles = {"manager"}
        if viewer_role == "admin" and viewer_id and manager.id == viewer_id:
            allowed_manager_roles.add("admin")
        if viewer_role == "manager" and viewer_id and manager.id == viewer_id:
            allowed_manager_roles.add("manager")
        if manager.role not in allowed_manager_roles:
            raise ValueError("Manager được chọn không hợp lệ.")
        if viewer_role == "manager" and viewer_id and manager.id != viewer_id:
            raise ValueError("Manager không được đổi phạm vi BOT sang manager khác.")

        assigned_user: UserSummary | None = None
        if assigned_user_id:
            assigned_user = self._find_user(assigned_user_id)
            if assigned_user.role not in {"user", "admin", "manager"}:
                raise ValueError("Người nhận được chọn không hợp lệ.")
            if assigned_user.role == "user":
                assigned_user_manager_id = self._resolved_user_manager_id(assigned_user)
                if assigned_user_manager_id != manager.id:
                    raise ValueError("User phải thuộc manager đã chọn.")
            elif assigned_user.role == "manager":
                if assigned_user.id != manager.id:
                    raise ValueError("Manager chỉ được chọn chính mình trong BOT này.")

        selected_workers: list[WorkerRecord] = []
        for worker_id in normalized_worker_ids:
            worker = self._find_worker(worker_id)
            selected_workers.append(worker)

        if assigned_user is not None:
            previous_links = self._assigned_worker_links_for_user(assigned_user.id)
            previous_links_by_worker_id = {
                str(link.get("worker_id") or ""): link
                for link in previous_links
                if str(link.get("worker_id") or "")
            }
            previous_worker_ids = set(previous_links_by_worker_id.keys())
            selected_worker_ids = {worker.id for worker in selected_workers}

            for worker_id in previous_worker_ids - selected_worker_ids:
                self._clear_user_worker_channels(assigned_user.id, worker_id)

            for worker in selected_workers:
                self._apply_worker_manager(worker, manager)

            self.user_worker_links = [
                link for link in self.user_worker_links if str(link.get("user_id") or "") != assigned_user.id
            ]

            next_id = max([self._mapping_numeric_id(item.get("id")) for item in self.user_worker_links], default=0) + 1
            for worker in selected_workers:
                previous_link = previous_links_by_worker_id.get(worker.id)
                self.user_worker_links.append(
                    {
                        "id": next_id,
                        "user_id": assigned_user.id,
                        "worker_id": worker.id,
                        "threads": max(
                            1,
                            int((previous_link or {}).get("threads") or worker.threads or 1),
                        ),
                        "bot_type": str((previous_link or {}).get("bot_type") or "1080p").strip() or "1080p",
                        "note": str((previous_link or {}).get("note") or "").strip() or "VPS được cấp",
                        "created_at": (previous_link or {}).get("created_at") or self._now(trim=False),
                    }
                )
                next_id += 1
        else:
            current_manager_pool_ids = set(self._manager_inventory_worker_ids(manager.id))
            selected_worker_ids = {worker.id for worker in selected_workers}
            removed_worker_ids = current_manager_pool_ids - selected_worker_ids

            for worker in selected_workers:
                self._apply_worker_manager(worker, manager)

            for worker_id in removed_worker_ids:
                worker = self._find_worker(worker_id)
                if self._assigned_user_for_worker(worker.id) is not None:
                    continue
                self._clear_worker_channels(worker.id)
                self._apply_worker_manager(worker, None)

        self._save_state()
        return len(normalized_worker_ids)

    def _delete_bot_state(self, worker_id: str) -> None:
        self._find_worker(worker_id)
        self._clear_worker_channels(worker_id)
        removed_jobs = [job for job in self.jobs if job.worker_name == worker_id]
        for job in removed_jobs:
            self._purge_job_artifacts(job, exclude_job_id=job.id)
        self.workers = [worker for worker in self.workers if worker.id != worker_id]
        self.user_worker_links = [link for link in self.user_worker_links if link["worker_id"] != worker_id]
        self.jobs = [job for job in self.jobs if job.worker_name != worker_id]
        self._clear_worker_registration_meta(worker_id)

    def delete_bot(self, worker_id: str) -> None:
        self._delete_bot_state(worker_id)
        self.worker_connection_profiles.pop(str(worker_id or "").strip(), None)
        self.worker_operation_tasks = [
            task
            for task in self.worker_operation_tasks
            if str(task.get("worker_id") or "").strip() != str(worker_id or "").strip()
        ]
        self._save_state()

    def delete_bot_without_ssh(self, worker_id: str, *, deleted_by: str = "system") -> None:
        completed_task: dict[str, Any] | None = None
        worker_name = ""
        vps_ip = ""
        with self._worker_state_lock:
            worker = self._find_worker(worker_id)
            profile = dict(self.worker_connection_profiles.get(worker.id) or {})
            worker_name = self._resolve_worker_display_name(worker.id)
            vps_ip = str(profile.get("vps_ip") or "").strip()
            if not vps_ip and self._looks_like_ipv4(worker_name):
                vps_ip = worker_name
            actor = self._find_user_by_username(deleted_by)
            completed_task = {
                "worker_id": worker.id,
                "worker_name": worker_name,
                "vps_ip": vps_ip,
                "manager_name": str(worker.manager_name or "").strip() or "system",
                "requested_by": str(deleted_by or "").strip() or "system",
                "requested_role": actor.role if actor else "system",
            }
            self._remember_deleted_worker(
                worker.id,
                worker_name=worker_name,
                vps_ip=vps_ip or None,
                deleted_by=deleted_by,
                reason="delete_without_ssh",
            )
            self._delete_bot_state(worker.id)
            self.worker_connection_profiles.pop(str(worker.id or "").strip(), None)
            self.worker_operation_tasks = [
                task
                for task in self.worker_operation_tasks
                if str(task.get("worker_id") or "").strip() != str(worker.id or "").strip()
            ]
            self._save_state()
        if completed_task is not None:
            self._notify_telegram_chat_ids(
                self._bot_operation_recipient_chat_ids(completed_task),
                self._bot_decommission_completed_message(
                    completed_task,
                    worker_name=worker_name,
                    worker_id=str(worker_id or "").strip(),
                    vps_ip=vps_ip or worker_name,
                ),
            )

    def update_bot_thread(self, worker_id: str, thread: int) -> None:
        worker = self._find_worker(worker_id)
        if thread < 1:
            raise ValueError("Số luồng phải lớn hơn hoặc bằng 1.")
        worker.threads = self._fixed_assignment_threads()
        worker.capacity = self._fixed_assignment_threads()
        self._save_state()

    def add_user_bot(self, user_id: str, worker_id: str, threads: int, bot_type: str = "1080p", note: str | None = None) -> None:
        user = self._find_user(user_id)
        worker = self._find_worker(worker_id)
        if user.role != "user":
            raise ValueError("Chỉ user thường mới được cấp VPS trực tiếp.")
        if user.manager_name and worker.manager_name != user.manager_name:
            raise ValueError("User chỉ được cấp VPS thuộc manager của mình.")

        normalized_note = (note or "").strip() or "VPS được cấp"
        existing = next(
            (
                item
                for item in self.user_worker_links
                if str(item.get("user_id") or "") == user_id and str(item.get("worker_id") or "") == worker_id
            ),
            None,
        )
        if existing:
            existing["threads"] = self._fixed_assignment_threads()
            existing["bot_type"] = bot_type.lower()
            existing["note"] = normalized_note
        else:
            next_id = max([self._mapping_numeric_id(item.get("id")) for item in self.user_worker_links], default=0) + 1
            self.user_worker_links.append(
                {
                    "id": next_id,
                    "user_id": user_id,
                    "worker_id": worker_id,
                    "threads": self._fixed_assignment_threads(),
                    "bot_type": bot_type.lower(),
                    "note": normalized_note,
                    "created_at": self._now(trim=False),
                }
            )

        self._save_state()

    def delete_user_bot(self, mapping_id: int) -> None:
        mapping = next((item for item in self.user_worker_links if item["id"] == mapping_id), None)
        if mapping is None:
            raise KeyError(mapping_id)
        removed_worker_id = str(mapping.get("worker_id") or "").strip()
        removed_user_id = str(mapping.get("user_id") or "").strip()
        self._clear_user_worker_channels(removed_user_id, removed_worker_id)
        self.user_worker_links = [item for item in self.user_worker_links if item["id"] != mapping_id]
        for session in self.browser_sessions:
            if session.owner_user_id != removed_user_id:
                continue
            if removed_worker_id and session.target_worker_id != removed_worker_id:
                continue
            if session.status in {"launching", "awaiting_confirmation", "confirmed"}:
                session.status = "closed"
                session.expires_at = self._now(trim=False)
                session.last_error = "Session đã đóng do user không còn được cấp VPS."
        self._save_state()

    def update_user_bot(self, mapping_id: int, threads: int, bot_type: str | None = None, note: str | None = None) -> None:
        for mapping in self.user_worker_links:
            if mapping["id"] != mapping_id:
                continue
            mapping["threads"] = self._fixed_assignment_threads()
            if bot_type:
                mapping["bot_type"] = bot_type.lower()
            if note is not None:
                mapping["note"] = (note or "").strip() or mapping.get("note") or "VPS được cấp"
            self._save_state()
            return
        raise KeyError(mapping_id)

    def _filtered_channels_v2(
        self,
        *,
        manager_ids: list[str] | None = None,
        user_id: str | None = None,
        bot_id: str | None = None,
    ) -> tuple[list[ChannelRecord], dict[str, Any] | None, dict[str, Any] | None]:
        selected_manager_ids = set(self._selected_manager_ids(manager_ids))

        channel_source = list(self.channels)
        if selected_manager_ids:
            channel_source = [
                channel
                for channel in channel_source
                if self._resolve_channel_manager_id(channel) in selected_manager_ids
            ]

        filtered_user: dict[str, Any] | None = None
        filtered_bot: dict[str, Any] | None = None

        if user_id:
            user = self._find_user(user_id)
            filtered_user = {"id": user.id, "username": user.username, "display_name": user.display_name}
            assigned_channel_ids = {link["channel_id"] for link in self.channel_user_links if link["user_id"] == user.id}
            channel_source = [channel for channel in channel_source if channel.id in assigned_channel_ids]

        if bot_id:
            worker = self._find_worker(bot_id)
            filtered_bot = {"id": worker.id, "name": self._resolve_worker_display_name(worker.id)}
            channel_source = [channel for channel in channel_source if channel.worker_id == worker.id]

        return channel_source, filtered_user, filtered_bot

    def _build_channel_rows_v2(self, channel_source: list[ChannelRecord]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for index, channel in enumerate(channel_source, start=1):
            status_label, status_class = self._channel_status_badge(channel.status)
            worker = next((item for item in self.workers if item.id == channel.worker_id), None)
            manager_name = self._resolve_channel_manager_name(channel)
            rows.append(
                {
                    "index": index,
                    "id": channel.id,
                    "avatar_url": channel.avatar_url or "/legacy/admin-themes/assets/img/avatar/avatar-1.png",
                    "name": channel.name,
                    "channel_id": channel.channel_id,
                    "channel_link": self._channel_link(channel),
                    "gmail": channel.oauth_email or "-",
                    "manager_name": manager_name,
                    "users": self._channel_users(channel),
                    "worker_id": channel.worker_id,
                    "worker_name": self._resolve_channel_worker_display_name(channel),
                    "group": (worker.group if worker and worker.group else manager_name),
                    "status_label": status_label,
                    "status_class": status_class,
                    "created_at": self._format_full_datetime(self._channel_connected_at(channel)),
                    "total_users": self._channel_user_count(channel.id),
                    "total_renders": len([job for job in self.jobs if job.channel_id == channel.id]),
                }
            )
        return rows

    def get_admin_channel_index_context(
        self,
        *,
        manager_ids: list[str] | None = None,
        user_id: str | None = None,
        bot_id: str | None = None,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        if workspace_mode == "live":
            context = self._admin_shell_context(
                page_title="Danh sách Kênh",
                active_page="channels",
                workspace_mode=workspace_mode,
                viewer_role=viewer_role,
                viewer_id=viewer_id,
                manager_ids=manager_ids,
                notice=notice,
                notice_level=notice_level,
            )
            context.update(
                {
                    "template": "admin/channel_index.html",
                    "channels": [],
                    "filtered_user": None,
                    "filtered_bot": None,
                }
            )
            return context
        channels, filtered_user, filtered_bot = self._filtered_channels_v2(
            manager_ids=manager_ids,
            user_id=user_id,
            bot_id=bot_id,
        )
        context = self._admin_shell_context(
            page_title="Danh sách Kênh",
            active_page="channels",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/channel_index.html",
                "channels": self._build_channel_rows_v2(channels),
                "filtered_user": filtered_user,
                "filtered_bot": filtered_bot,
            }
        )
        return context

    def get_admin_channels_of_user_context(
        self,
        *,
        user_id: str,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        user = self._find_user(user_id)
        if user.role == "user":
            channel_source = [
                channel
                for channel in self.channels
                if self._resolve_channel_manager_id(channel) == user.manager_id
            ]
        elif user.role == "manager":
            channel_source = [
                channel
                for channel in self.channels
                if self._resolve_channel_manager_id(channel) == user.id
            ]
        else:
            channel_source = list(self.channels)

        granted_channel_ids = {link["channel_id"] for link in self.channel_user_links if link["user_id"] == user.id}
        rows = []
        for row in self._build_channel_rows_v2(channel_source):
            row["is_used"] = row["id"] in granted_channel_ids
            rows.append(row)

        context = self._admin_shell_context(
            page_title=f"Thêm kênh cho người dùng {user.username}",
            active_page="channels",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/channel_user.html",
                "target_user": {
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.display_name,
                },
                "channels": rows,
            }
        )
        return context

    def get_admin_users_of_channel_context(
        self,
        *,
        channel_id: str,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        channel = self._find_channel(channel_id)
        assigned_ids = [link["user_id"] for link in self.channel_user_links if link["channel_id"] == channel.id]
        assigned_id_set = set(assigned_ids)
        rows = []
        for index, user_id in enumerate(assigned_ids, start=1):
            user = self._find_user(user_id)
            rows.append(
                {
                    "index": index,
                    "id": user.id,
                    "username": user.username,
                    "display_name": user.display_name,
                    "total_channels": self._user_channel_count(user),
                    "total_bots": self._user_worker_count(user),
                }
            )

        available_users = []
        for user in self.users:
            if user.id in assigned_id_set:
                continue
            if not self._can_assign_channel_to_user(
                user,
                channel,
                viewer_role=viewer_role,
                viewer_id=viewer_id,
            ):
                continue
            manager_name = self._manager_username_from_id(self._resolved_user_manager_id(user)) or user.manager_name or "-"
            display_name = user.display_name or user.username
            available_users.append(
                {
                    "id": user.id,
                    "username": user.username,
                    "display_name": display_name,
                    "manager_name": manager_name,
                    "search_text": " ".join(
                        part
                        for part in [
                            display_name,
                            user.username,
                            manager_name,
                        ]
                        if str(part or "").strip()
                    ),
                }
            )
        available_users.sort(
            key=lambda item: (
                item["manager_name"].casefold(),
                item["display_name"].casefold(),
                item["username"].casefold(),
            )
        )

        if viewer_role == "manager":
            assignment_scope_label = "User do bạn quản lý"
            assignment_scope_note = (
                "Manager chỉ thấy user thuộc scope của mình, chưa được gán trước đó, "
                "và đã được cấp đúng VPS của kênh."
            )
        else:
            assignment_scope_label = "Toàn bộ user đủ điều kiện"
            assignment_scope_note = (
                "Admin thấy tất cả user chưa được gán trước đó và đã được cấp đúng VPS của kênh, "
                "kể cả khác manager."
            )

        context = self._admin_shell_context(
            page_title=f"Danh sách người dùng của kênh {channel.name}",
            active_page="channels",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/channel_users.html",
                "target_channel": {
                    "id": channel.id,
                    "name": channel.name,
                    "channel_id": channel.channel_id,
                    "channel_link": self._channel_link(channel),
                    "avatar_url": channel.avatar_url or "/static/admin-themes/assets/img/avatar/avatar-1.png",
                    "manager_name": self._resolve_channel_manager_name(channel),
                    "assigned_user_count": len(rows),
                    "available_user_count": len(available_users),
                    "assignment_scope_label": assignment_scope_label,
                    "assignment_scope_note": assignment_scope_note,
                },
                "users": rows,
                "available_users": available_users,
            }
        )
        return context

    def update_user_channel(
        self,
        user_id: str,
        channel_id: str,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> str:
        user = self._find_user(user_id)
        channel = self._find_channel(channel_id)
        existing = next(
            (link for link in self.channel_user_links if link["user_id"] == user.id and link["channel_id"] == channel.id),
            None,
        )
        if existing:
            self.channel_user_links = [link for link in self.channel_user_links if link["id"] != existing["id"]]
            self._save_state()
            return "removed"

        if user.role != "user":
            raise ValueError("Chỉ user thường mới được gán vào kênh.")
        if viewer_role == "manager" and viewer_id and self._resolved_user_manager_id(user) != viewer_id:
            raise ValueError("Manager chỉ được gán kênh cho user mình quản lý.")

        self._assert_channel_matches_user_worker(user, channel)

        next_id = max([item["id"] for item in self.channel_user_links], default=0) + 1
        self.channel_user_links.append({"id": next_id, "user_id": user.id, "channel_id": channel.id})
        self._save_state()
        return "added"

    def add_user_to_channel(
        self,
        user_id: str,
        channel_id: str,
        *,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
    ) -> str:
        return self.update_user_channel(
            user_id,
            channel_id,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
        )

    def update_channel_profile(self, channel_id: str) -> None:
        channel = self._find_channel(channel_id)
        channel.status = "connected"
        channel.oauth_connected_at = self._now()
        self._save_state()

    def _queue_browser_profile_cleanup(self, *, worker_id: str, profile_key: str | None) -> None:
        cleaned_worker_id = str(worker_id or "").strip()
        cleaned_profile_key = str(profile_key or "").strip()
        if not cleaned_worker_id or not cleaned_profile_key:
            return
        for item in self.browser_profile_cleanup_tasks:
            if (
                str(item.get("worker_id") or "").strip() == cleaned_worker_id
                and str(item.get("profile_key") or "").strip() == cleaned_profile_key
            ):
                return
        self.browser_profile_cleanup_tasks.append(
            {
                "worker_id": cleaned_worker_id,
                "profile_key": cleaned_profile_key,
                "queued_at": self._now(trim=False).isoformat(),
            }
        )

    def _browser_profile_bound_channel(self, profile_key: str | None) -> ChannelRecord | None:
        cleaned_profile_key = str(profile_key or "").strip()
        if not cleaned_profile_key:
            return None
        return next(
            (
                channel
                for channel in self.channels
                if channel.connection_mode == "browser_profile"
                and str(channel.browser_profile_key or "").strip() == cleaned_profile_key
            ),
            None,
        )

    def _queue_browser_profile_cleanup_if_orphan(self, *, worker_id: str, profile_key: str | None) -> None:
        if self._browser_profile_bound_channel(profile_key) is not None:
            return
        self._queue_browser_profile_cleanup(worker_id=worker_id, profile_key=profile_key)

    def _close_orphan_browser_sessions_for_user(self, user_id: str) -> None:
        closed_at = self._now(trim=False)
        changed = False
        for session in self.browser_sessions:
            if session.owner_user_id != user_id:
                continue
            if session.status not in {"launching", "awaiting_confirmation", "confirmed"}:
                continue
            if self._browser_profile_bound_channel(session.profile_key) is not None:
                continue
            session.status = "closed"
            session.expires_at = closed_at
            session.last_error = "Session đã đóng vì profile tạm không còn gắn với kênh nào."
            self._queue_browser_profile_cleanup_if_orphan(
                worker_id=session.target_worker_id,
                profile_key=session.profile_key,
            )
            changed = True
        if changed:
            self._save_state()

    def _close_browser_sessions_for_profile(self, profile_key: str | None) -> None:
        cleaned_profile_key = str(profile_key or "").strip()
        if not cleaned_profile_key:
            return
        closed_at = self._now(trim=False)
        for session in self.browser_sessions:
            if str(session.profile_key or "").strip() != cleaned_profile_key:
                continue
            if session.status not in {"launching", "awaiting_confirmation", "confirmed"}:
                continue
            session.status = "closed"
            session.expires_at = closed_at
            session.last_error = "Session đã đóng vì kênh/profile đã bị xóa."

    def _schedule_channel_browser_profile_cleanup(self, channel: ChannelRecord) -> None:
        if channel.connection_mode != "browser_profile":
            return
        self._close_browser_sessions_for_profile(channel.browser_profile_key)
        self._queue_browser_profile_cleanup(
            worker_id=channel.worker_id,
            profile_key=channel.browser_profile_key,
        )

    def delete_channel(self, channel_id: str) -> None:
        channel = self._find_channel(channel_id)
        linked_user_ids = [
            str(link.get("user_id") or "").strip()
            for link in self.channel_user_links
            if link.get("channel_id") == channel.id
        ]
        self._schedule_channel_browser_profile_cleanup(channel)
        removed_jobs = [job for job in self.jobs if job.channel_id == channel_id]
        for job in removed_jobs:
            self._purge_job_artifacts(job, exclude_job_id=job.id)
        self.channels = [channel for channel in self.channels if channel.id != channel_id]
        self.channel_user_links = [link for link in self.channel_user_links if link["channel_id"] != channel_id]
        self.jobs = [job for job in self.jobs if job.channel_id != channel_id]
        for user_id in linked_user_ids:
            if user_id:
                self._close_orphan_browser_sessions_for_user(user_id)
        self._save_state()

    def delete_user_connected_channel(self, channel_id: str, *, user_id: str) -> None:
        channel = self._find_channel(channel_id)
        has_access = self._user_has_channel_access(user_id, channel.id)
        if not has_access:
            raise ValueError("Bạn không có quyền xóa kênh này.")
        self.delete_channel(channel.id)

    def get_channel_export_rows(self) -> list[dict[str, Any]]:
        return self.get_channel_export_rows_filtered()

    def get_channel_export_rows_filtered(self, *, manager_ids: list[str] | None = None) -> list[dict[str, Any]]:
        rows = []
        selected_manager_ids = set(self._selected_manager_ids(manager_ids))
        channel_source = list(self.channels)
        if selected_manager_ids:
            channel_source = [
                channel
                for channel in channel_source
                if self._resolve_channel_manager_id(channel) in selected_manager_ids
            ]
        for channel in channel_source:
            rows.append(
                {
                    "Avatar": channel.avatar_url or "",
                    "ChannelName": channel.name,
                    "ChannelYTId": channel.channel_id,
                    "ChannelLink": self._channel_link(channel),
                    "BotName": self._resolve_channel_worker_display_name(channel),
                    "ChannelGmail": channel.oauth_email or "",
                    "Group": next((worker.group for worker in self.workers if worker.id == channel.worker_id), channel.manager_name),
                    "CreatedTime": self._format_full_datetime(self._channel_connected_at(channel)),
                    "TotalUser": self._channel_user_count(channel.id),
                    "Status": channel.status,
                    "UserUpload": ",".join(self._channel_users(channel)),
                    "Manager": channel.manager_name,
                }
            )
        return rows

    def get_admin_render_index_context(
        self,
        *,
        manager_ids: list[str] | None = None,
        user_id: str | None = None,
        channel_id: str | None = None,
        workspace_mode: str = "upload",
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        is_live_workspace = workspace_mode == "live"
        context = self._admin_shell_context(
            page_title="Danh sách Live Stream" if is_live_workspace else "Danh sách Upload",
            active_page="live_stream_jobs" if is_live_workspace else "upload_jobs",
            workspace_mode=workspace_mode,
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            manager_ids=manager_ids,
            notice=notice,
            notice_level=notice_level,
        )
        context["hide_workspace_tabs"] = True

        if is_live_workspace:
            normalized_user_id = str(user_id or "").strip() or None
            selected_user_name = ""
            if normalized_user_id:
                try:
                    selected_user_name = self._find_user(normalized_user_id).username
                except KeyError:
                    selected_user_name = ""
            scoped_streams = self._scoped_live_streams(
                viewer_role=viewer_role,
                viewer_id=viewer_id,
                manager_ids=manager_ids,
                owner_user_id=normalized_user_id,
            )
            render_rows = self._build_live_render_rows(scoped_streams)

            context.update(
                {
                    "template": "admin/live_render_index.html",
                    "summary_strip": self._live_summary_strip(
                        viewer_role=viewer_role,
                        viewer_id=viewer_id,
                        manager_ids=manager_ids,
                        user_id=normalized_user_id,
                    ),
                    "renders": render_rows,
                    "render_list_href": "/admin/livestream/index",
                    "render_heading": f"DS Live của {selected_user_name}" if selected_user_name else "Danh sách live stream",
                    "render_note": (
                        f"Đang lọc theo user {selected_user_name}. "
                        "Theo dõi luồng live stream theo bộ cột quản trị hiện tại, gộp timeline và cấu hình để bảng gọn và dễ rà soát hơn."
                        if selected_user_name
                        else "Theo dõi luồng live stream theo bộ cột quản trị hiện tại, gộp timeline và cấu hình để bảng gọn và dễ rà soát hơn."
                    ),
                    "selected_user_id": normalized_user_id or "",
                    "selected_user_name": selected_user_name,
                    "render_summary": (
                        f"Hiển thị 1 đến {len(render_rows)} trong {len(render_rows)} kết quả"
                        if render_rows
                        else "Chưa có live stream nào trong danh sách"
                    ),
                }
            )
            return context

        selected_manager_ids = set(self._selected_manager_ids(manager_ids))
        job_source = list(self.jobs)
        claim_positions = self._upload_claim_position_by_job_id()
        if selected_manager_ids:
            job_source = [
                job for job in job_source
                if self._resolve_job_manager_id(job) in selected_manager_ids
            ]

        filtered_channel: dict[str, Any] | None = None
        if channel_id:
            channel = self._find_channel(channel_id)
            filtered_channel = {"id": channel.id, "name": channel.name, "channel_id": channel.channel_id}
            job_source = [job for job in job_source if job.channel_id == channel.id]

        render_rows: list[dict[str, Any]] = []
        for index, job in enumerate(sorted(job_source, key=lambda item: item.created_at, reverse=True), start=1):
            status_label, status_class = self._render_status_badge(job)
            primary_asset = next((asset for asset in job.assets if asset.url or asset.file_name), None)
            channel = self._find_channel_optional(job.channel_id)
            manager_name = self._resolve_job_manager_name(job)
            render_rows.append(
                {
                    "index": index,
                    "id": job.id,
                    "manager_name": manager_name,
                    "username": self._job_username(job),
                    "created_at": self._format_full_datetime(job.created_at),
                    "title": job.title,
                    "worker_name": self._resolve_job_worker_display_name(job),
                    "group": manager_name,
                    "avatar_url": job.channel_avatar_url or "/legacy/admin-themes/assets/img/avatar/avatar-1.png",
                    "channel_name": job.channel_name,
                    "channel_id": channel.channel_id if channel else job.channel_id,
                    "video_link": primary_asset.url if primary_asset and primary_asset.url else (primary_asset.file_name if primary_asset else "-"),
                    "time_render_string": job.time_render_string,
                    "download_started_at": self._format_full_datetime(job.download_started_at),
                    "upload_started_at": self._format_full_datetime(job.upload_started_at),
                    "completed_at": self._format_full_datetime(job.completed_at),
                    "status_label": status_label,
                    "status_class": status_class,
                    "queue_order": claim_positions.get(job.id, "-"),
                }
            )

        context.update(
            {
                "template": "admin/render_index.html",
                "renders": render_rows,
                "filtered_channel": filtered_channel,
                "render_list_href": "/admin/upload/index",
                "render_channel_href": "/admin/upload/channel",
                "render_detail_href": "/admin/upload/renderinfo",
                "render_heading": "Danh sách upload",
                "render_note": "Theo dõi queue upload, timeline download/upload và thao tác nhanh trên từng job upload.",
                "render_table_heading": "Danh sách job upload",
                "render_table_note": "Theo dõi tiến trình xử lý, mốc thời gian và thao tác nhanh trên từng job upload.",
                "delete_render_meta": {
                    "user": self.render_delete_meta["user"],
                    "deleted_at": self._format_compact_datetime(self.render_delete_meta["deleted_at"]),
                },
            }
        )
        return context

    def get_admin_render_detail_context(
        self,
        *,
        job_id: str,
        viewer_role: str = "admin",
        viewer_id: str | None = None,
        notice: str | None = None,
        notice_level: str = "success",
    ) -> dict[str, Any]:
        job = self._find_job(job_id)
        channel = self._find_channel(job.channel_id)
        asset_map = {asset.slot: asset for asset in job.assets}

        def asset_value(slot: str) -> str:
            asset = asset_map.get(slot)
            if not asset:
                return ""
            return asset.url or asset.file_name or ""

        context = self._admin_shell_context(
            page_title="Thông tin Upload",
            active_page="upload_jobs",
            viewer_role=viewer_role,
            viewer_id=viewer_id,
            notice=notice,
            notice_level=notice_level,
        )
        context.update(
            {
                "template": "admin/render_detail.html",
                "hide_workspace_tabs": True,
                "render_list_href": "/admin/upload/index",
                "render_detail": {
                    "id": job.id,
                    "title": job.title,
                    "intro": asset_value("intro"),
                    "video_loop": asset_value("video_loop"),
                    "audio_loop": asset_value("audio_loop"),
                    "outro": asset_value("outro"),
                    "channel_name": channel.name,
                    "channel_id": channel.channel_id,
                    "time_render_string": job.time_render_string,
                    "scheduled_at": self._format_full_datetime(job.scheduled_at),
                    "source_mode": job.source_mode,
                },
            }
        )
        return context

    def get_admin_template_context(self, active_page: str, user_id: str | None = None, bot_id: str | None = None) -> dict[str, Any]:
        if active_page == "users":
            return self.get_admin_user_index_context()
        if active_page == "channels":
            return self.get_admin_channel_index_context(user_id=user_id, bot_id=bot_id)
        if active_page in {"renders", "upload_jobs", "live_stream_jobs"}:
            return self.get_admin_render_index_context()
        raise KeyError(active_page)

    def _resolve_google_redirect_uri(self, base_url: str | None = None) -> str | None:
        return os.getenv("GOOGLE_REDIRECT_URI") or (
            f"{base_url}{self._oauth_callback_path()}" if base_url else None
        )

    def _google_oauth_config(self, base_url: str | None = None) -> dict[str, str | None]:
        return {
            "client_id": os.getenv("GOOGLE_CLIENT_ID"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
            "redirect_uri": self._resolve_google_redirect_uri(base_url),
        }

    @staticmethod
    def _parse_google_error(body: str) -> str:
        if not body:
            return ""
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return body.strip()

        if isinstance(payload, dict):
            error = payload.get("error")
            description = payload.get("error_description")
            nested = payload.get("error")
            if isinstance(nested, dict):
                message = nested.get("message")
                status = nested.get("status")
                return " - ".join([part for part in [status, message] if part])
            return " - ".join([part for part in [str(error) if error else "", str(description) if description else ""] if part])
        return body.strip()

    def _post_form_json(self, url: str, payload: dict[str, str]) -> dict[str, Any]:
        encoded_payload = urlencode(payload).encode("utf-8")
        request = Request(
            url,
            data=encoded_payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = self._parse_google_error(exc.read().decode("utf-8", errors="ignore"))
            raise ValueError(detail or "Google token endpoint trả về lỗi.") from exc
        except URLError as exc:
            raise ValueError("Không thể kết nối tới Google OAuth endpoint.") from exc

    def _get_json(self, url: str, access_token: str) -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = self._parse_google_error(exc.read().decode("utf-8", errors="ignore"))
            raise ValueError(detail or "Google API trả về lỗi.") from exc
        except URLError as exc:
            raise ValueError("Không thể kết nối tới Google API.") from exc

    def start_oauth(self, base_url: str | None = None, *, state: str) -> OAuthStartResponse:
        config = self._google_oauth_config(base_url)
        client_id = config["client_id"]
        redirect_uri = config["redirect_uri"]

        if not client_id or not redirect_uri:
            return OAuthStartResponse(
                auth_url=None,
                message="Đã sẵn sàng cho flow Google OAuth. Cần bổ sung GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET và GOOGLE_REDIRECT_URI để bật kết nối thật.",
            )

        query = urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "access_type": "offline",
                "include_granted_scopes": "true",
                "scope": self._google_oauth_scope(),
                "state": state,
                "prompt": "consent",
            }
        )
        return OAuthStartResponse(
            auth_url=f"https://accounts.google.com/o/oauth2/v2/auth?{query}",
            message="Đã tạo URL kết nối Google OAuth.",
        )

    def complete_google_oauth(self, *, user_id: str, code: str, base_url: str | None = None) -> dict[str, str]:
        config = self._google_oauth_config(base_url)
        client_id = config["client_id"]
        client_secret = config["client_secret"]
        redirect_uri = config["redirect_uri"]
        if not client_id or not client_secret or not redirect_uri:
            raise ValueError("Thiếu GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET hoặc GOOGLE_REDIRECT_URI.")

        token_payload = self._post_form_json(
            "https://oauth2.googleapis.com/token",
            {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        access_token = str(token_payload.get("access_token") or "").strip()
        refresh_token = str(token_payload.get("refresh_token") or "").strip() or None
        scope = str(token_payload.get("scope") or self._google_oauth_scope()).strip()
        token_type = str(token_payload.get("token_type") or "").strip() or None
        if not access_token:
            raise ValueError("Google không trả về access_token.")

        userinfo = self._get_json("https://openidconnect.googleapis.com/v1/userinfo", access_token)
        youtube_payload = self._get_json(
            "https://www.googleapis.com/youtube/v3/channels?part=id,snippet&mine=true",
            access_token,
        )
        items = youtube_payload.get("items") or []
        if not items:
            raise ValueError("Tài khoản Google này chưa có YouTube channel hoặc chưa chọn đúng channel.")

        channel_payload = items[0] or {}
        channel_id = str(channel_payload.get("id") or "").strip()
        snippet = channel_payload.get("snippet") or {}
        channel_name = str(snippet.get("title") or "").strip() or "Untitled YouTube Channel"
        avatar_url = (
            ((snippet.get("thumbnails") or {}).get("default") or {}).get("url")
            or ((snippet.get("thumbnails") or {}).get("medium") or {}).get("url")
            or ((snippet.get("thumbnails") or {}).get("high") or {}).get("url")
        )
        oauth_email = str(userinfo.get("email") or "").strip() or None
        oauth_google_subject = str(userinfo.get("sub") or "").strip() or None
        now = self._now()

        if not channel_id:
            raise ValueError("Google trả về dữ liệu channel không hợp lệ.")

        current_user = self._require_workspace_user(user_id)
        worker = self._pick_worker_for_user(current_user)
        existing_channel = next((channel for channel in self.channels if channel.channel_id == channel_id), None)
        existing_refresh_token = existing_channel.oauth_refresh_token if existing_channel else None
        if not refresh_token and not existing_refresh_token:
            raise ValueError("Google không trả về refresh_token. Hãy xóa quyền app cũ và kết nối lại.")

        if existing_channel:
            channel = existing_channel
        else:
            channel = ChannelRecord(
                id=f"channel-{uuid4().hex[:8]}",
                name=channel_name,
                channel_id=channel_id,
                avatar_url=avatar_url,
                worker_id=worker.id,
                worker_name=worker.id,
                manager_name=current_user.manager_name or worker.manager_name,
                status="connected",
            )
            self.channels.append(channel)

        previous_browser_profile_key = channel.browser_profile_key
        previous_connection_mode = channel.connection_mode
        channel.name = channel_name
        channel.avatar_url = avatar_url or channel.avatar_url
        channel.status = "connected"
        channel.worker_id = worker.id
        channel.worker_name = worker.id
        channel.manager_name = current_user.manager_name or worker.manager_name
        channel.oauth_email = oauth_email or channel.oauth_email
        channel.oauth_connected_at = now
        channel.oauth_google_subject = oauth_google_subject or channel.oauth_google_subject
        channel.oauth_refresh_token = refresh_token or existing_refresh_token
        channel.oauth_scope = scope or channel.oauth_scope
        channel.oauth_token_type = token_type or channel.oauth_token_type
        channel.connection_mode = "oauth"
        channel.browser_profile_key = None
        channel.browser_profile_path = None
        channel.browser_connected_at = None
        if previous_connection_mode == "browser_profile" and previous_browser_profile_key:
            self._queue_browser_profile_cleanup(
                worker_id=worker.id,
                profile_key=previous_browser_profile_key,
            )

        self._ensure_channel_user_link(channel_id=channel.id, user_id=current_user.id)
        self._save_state()
        return {
            "channel_name": channel.name,
            "youtube_channel_id": channel.channel_id,
            "channel_record_id": channel.id,
        }

    @staticmethod
    def _sanitize_filename(file_name: str) -> str:
        value = re.sub(r"[^A-Za-z0-9._-]+", "-", (file_name or "").strip())
        value = value.strip(".-")
        return value or f"upload-{uuid4().hex[:8]}"

    @staticmethod
    def _path_for_session(session_id: str) -> str:
        return f"tmp/{session_id}.part"

    def _absolute_upload_path(self, relative_path: str) -> Path:
        return self.upload_dir / relative_path

    def _absolute_preview_path(self, relative_path: str) -> Path:
        return self.preview_dir / relative_path

    @staticmethod
    def _preview_route(job_id: str) -> str:
        return f"/api/user/jobs/{job_id}/preview-thumbnail"

    def _delete_job_preview_file(self, job: RenderJobRecord) -> bool:
        relative_path = str(job.thumbnail_path or "").strip()
        if not relative_path:
            return False
        preview_path = self._absolute_preview_path(relative_path)
        if preview_path.exists():
            preview_path.unlink(missing_ok=True)
        job.thumbnail_path = None
        job.thumbnail_url = None
        return True

    def _find_upload_session(self, session_id: str) -> UploadSessionRecord:
        for session in self.upload_sessions:
            if session.session_id == session_id:
                return session
        raise KeyError(session_id)

    def _find_user_upload_session(self, user_id: str, session_id: str) -> UploadSessionRecord:
        session = self._find_upload_session(session_id)
        if session.owner_user_id != user_id:
            raise KeyError(session_id)
        return session

    def _cleanup_stale_uploads(self) -> None:
        now = self._now(trim=False)
        changed = False
        keep_sessions: list[UploadSessionRecord] = []
        for session in self.upload_sessions:
            if session.status == "completed":
                keep_sessions.append(session)
                continue
            if session.expires_at > now:
                keep_sessions.append(session)
                continue
            session.status = "expired"
            temp_path = self._absolute_upload_path(session.temp_path)
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            changed = True
        if changed:
            self.upload_sessions = keep_sessions
            self._save_state()

    @staticmethod
    def _upload_response(session: UploadSessionRecord) -> UploadSessionResponse:
        return UploadSessionResponse(
            session_id=session.session_id,
            slot=session.slot,
            file_name=session.file_name,
            size_bytes=session.size_bytes,
            received_bytes=session.received_bytes,
            chunk_size=session.chunk_size,
            status=session.status,
            expires_at=session.expires_at,
            asset_id=session.asset_id,
            stored_file_name=session.stored_file_name,
        )

    def create_upload_session(self, user_id: str, payload: UploadSessionCreateRequest) -> UploadSessionResponse:
        self._require_workspace_user(user_id)
        self._cleanup_stale_uploads()
        capabilities = self.get_upload_capabilities()
        if payload.size_bytes <= 0:
            raise ValueError("Kích thước file phải lớn hơn 0.")
        if payload.size_bytes > capabilities.max_local_upload_bytes:
            raise ValueError("File vượt quá giới hạn local upload hiện tại.")

        extension = Path(payload.file_name).suffix.lower()
        if extension not in capabilities.allowed_extensions:
            raise ValueError(f"Định dạng file không hỗ trợ: {extension or '(không có extension)'}")

        sanitized_name = self._sanitize_filename(payload.file_name)
        now = self._now(trim=False)
        session = UploadSessionRecord(
            session_id=f"upl-{uuid4().hex}",
            owner_user_id=user_id,
            slot=payload.slot,
            file_name=sanitized_name,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            received_bytes=0,
            chunk_size=capabilities.resumable_chunk_bytes,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=24),
            temp_path="",
        )
        session.temp_path = self._path_for_session(session.session_id)
        temp_path = self._absolute_upload_path(session.temp_path)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(b"")
        self.upload_sessions.append(session)
        self._save_state()
        return self._upload_response(session)

    def get_upload_session(self, user_id: str, session_id: str) -> UploadSessionResponse:
        self._require_workspace_user(user_id)
        self._cleanup_stale_uploads()
        session = self._find_user_upload_session(user_id, session_id)
        return self._upload_response(session)

    def append_upload_chunk(self, user_id: str, session_id: str, offset: int, chunk: bytes) -> UploadSessionResponse:
        self._require_workspace_user(user_id)
        session = self._find_user_upload_session(user_id, session_id)
        if session.status not in {"active", "completed"}:
            raise ValueError("Upload session không còn hoạt động.")
        if session.status == "completed":
            return self._upload_response(session)
        if offset < 0:
            raise ValueError("Offset không hợp lệ.")

        temp_path = self._absolute_upload_path(session.temp_path)
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        current_size = temp_path.stat().st_size if temp_path.exists() else 0
        if offset != current_size:
            raise ValueError(f"Offset không khớp. Server đang có {current_size} bytes.")
        if offset + len(chunk) > session.size_bytes:
            raise ValueError("Chunk vượt quá kích thước đã khai báo.")

        with temp_path.open("ab") as file_obj:
            file_obj.write(chunk)

        session.received_bytes = temp_path.stat().st_size
        session.updated_at = self._now(trim=False)
        session.expires_at = session.updated_at + timedelta(hours=24)
        if session.received_bytes > session.size_bytes:
            raise ValueError("Upload vượt quá kích thước đã khai báo.")

        if session.received_bytes == session.size_bytes:
            session.status = "completed"
            session.asset_id = f"asset-{uuid4().hex[:12]}"
            stored_name = f"{session.asset_id}-{session.file_name}"
            asset_relative_path = f"assets/{stored_name}"
            final_path = self._absolute_upload_path(asset_relative_path)
            final_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(temp_path), str(final_path))
            session.stored_file_name = stored_name
            session.temp_path = asset_relative_path

        self._save_state()
        return self._upload_response(session)

    def consume_uploaded_asset(self, user_id: str, asset_id: str | None) -> str | None:
        if not asset_id:
            return None
        session = next(
            (
                item
                for item in self.upload_sessions
                if item.asset_id == asset_id and item.owner_user_id == user_id
            ),
            None,
        )
        if session is None or session.status != "completed" or not session.stored_file_name:
            raise ValueError("Uploaded asset không hợp lệ hoặc chưa hoàn tất.")
        return session.stored_file_name

    def _find_job_asset(self, job: RenderJobRecord, slot: str) -> JobAsset:
        for asset in job.assets:
            if asset.slot == slot:
                return asset
        raise KeyError(slot)

    def get_worker_job_asset_file(
        self,
        *,
        job_id: str,
        slot: str,
        worker_id: str,
        shared_secret: str,
    ) -> dict[str, Any]:
        self._authenticate_worker(worker_id, shared_secret)
        job = self._find_claimed_job(job_id, worker_id)
        self._ensure_worker_job_can_continue(job)
        asset = self._find_job_asset(job, slot)
        if asset.source_mode != "local" or not asset.file_name:
            raise ValueError("Asset này không phải local upload.")

        file_path = self.upload_asset_dir / asset.file_name
        if not file_path.exists():
            raise FileNotFoundError(asset.file_name)

        return {
            "path": file_path,
            "file_name": asset.file_name,
        }

    def get_user_job_asset_file(self, *, user_id: str, job_id: str, slot: str) -> dict[str, Any]:
        self._require_workspace_user(user_id)
        job = self._find_user_visible_job(user_id, job_id)
        asset = self._find_job_asset(job, slot)
        if asset.source_mode != "local" or not asset.file_name:
            raise ValueError("Asset này không phải local upload.")

        file_path = self.upload_asset_dir / asset.file_name
        if not self._path_has_content(file_path):
            raise FileNotFoundError(asset.file_name)

        content_type, _ = mimetypes.guess_type(str(file_path))
        return {
            "path": file_path,
            "file_name": asset.file_name,
            "content_type": content_type or "application/octet-stream",
        }

    def get_user_job_preview_thumbnail_file(self, *, user_id: str, job_id: str) -> dict[str, Any]:
        self._require_workspace_user(user_id)
        job = self._find_user_visible_job(user_id, job_id)
        relative_path = str(job.thumbnail_path or "").strip()
        if not relative_path:
            raise FileNotFoundError(job_id)

        file_path = self._absolute_preview_path(relative_path)
        if not self._path_has_content(file_path):
            raise FileNotFoundError(relative_path)

        content_type, _ = mimetypes.guess_type(str(file_path))
        return {
            "path": file_path,
            "file_name": file_path.name,
            "content_type": content_type or "image/jpeg",
        }

    def store_worker_job_preview_thumbnail(
        self,
        *,
        job_id: str,
        worker_id: str,
        shared_secret: str,
        file_name: str,
        content_type: str | None,
        payload: bytes,
    ) -> RenderJobRecord:
        self._authenticate_worker(worker_id, shared_secret)
        job = self._find_claimed_job(job_id, worker_id)
        self._ensure_worker_job_can_continue(job)
        if not payload:
            raise ValueError("Preview payload rong.")

        suffix = Path(file_name or "preview.jpg").suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".jpg"

        self._delete_job_preview_file(job)
        relative_path = f"{job.id}{suffix}"
        preview_path = self._absolute_preview_path(relative_path)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_bytes(payload)
        job.thumbnail_path = relative_path
        job.thumbnail_url = self._preview_route(job.id)
        self._save_state()
        return deepcopy(job)

    def get_worker_job_youtube_target(
        self,
        *,
        job_id: str,
        worker_id: str,
        shared_secret: str,
    ) -> WorkerYouTubeUploadTarget:
        self._authenticate_worker(worker_id, shared_secret)
        job = self._find_claimed_job(job_id, worker_id)
        self._ensure_worker_job_can_continue(job)
        channel = self._find_channel(job.channel_id)
        self._refresh_browser_channel_metadata(channel)
        if channel.connection_mode != "browser_profile":
            raise ValueError(
                "Luồng OAuth/API upload đã được tắt trên nhánh hiện tại. Hãy reconnect kênh bằng Ubuntu Browser trước khi upload."
            )
        return WorkerYouTubeUploadTarget(
            job_id=job.id,
            channel_id=channel.channel_id,
            channel_name=channel.name,
            title=job.title,
            description=job.description,
            connection_mode="browser_profile",
            browser_profile_key=channel.browser_profile_key,
            browser_profile_path=channel.browser_profile_path,
        )

    def abort_upload_session(self, user_id: str, session_id: str) -> None:
        self._require_workspace_user(user_id)
        session = self._find_user_upload_session(user_id, session_id)
        temp_path = self._absolute_upload_path(session.temp_path)
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
        self.upload_sessions = [item for item in self.upload_sessions if item.session_id != session_id]
        self._save_state()
        return None


store = AppStore()

