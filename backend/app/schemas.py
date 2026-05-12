from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


Role = Literal["admin", "manager", "user"]
WorkerStatus = Literal["online", "busy", "offline"]
ChannelStatus = Literal["connected", "pending_reconnect", "disconnected"]
JobStatus = Literal["pending", "queueing", "downloading", "rendering", "uploading", "completed", "cancelled", "error"]
SourceMode = Literal["drive", "local"]
JobVisibility = Literal["draft", "private", "public", "unlisted"]
UploadSessionStatus = Literal["active", "completed", "expired", "cancelled"]
ChannelConnectionMode = Literal["oauth", "browser_profile"]
BrowserSessionStatus = Literal["launching", "awaiting_confirmation", "confirmed", "closed", "expired", "failed"]
LiveStreamStatus = Literal[
    "draft",
    "scheduled",
    "downloading",
    "preparing",
    "waiting",
    "streaming",
    "disconnected",
    "ended",
    "stopped",
    "error",
]
LivePlatform = Literal["youtube_rtmp"]
LiveRuntimeRole = Literal["primary", "backup"]


class UserSummary(BaseModel):
    id: str
    username: str
    display_name: str
    role: Role
    manager_id: str | None = None
    manager_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None
    link_telegram: str | None = None
    link_telegram_live: str | None = None


class WorkerRecord(BaseModel):
    id: str
    name: str
    local: str | None = None
    live_role: LiveRuntimeRole | None = None
    manager_id: str | None = None
    manager_name: str
    group: str | None = None
    created_at: datetime | None = None
    status: WorkerStatus
    capacity: int
    load_percent: int
    ram_percent: int = 0
    ram_used_gb: float = 0
    ram_total_gb: float = 0
    bandwidth_kbps: float
    disk_used_gb: float
    disk_total_gb: float
    threads: int
    last_seen_at: datetime | None = None
    offline_since_at: datetime | None = None
    offline_alert_sent_at: datetime | None = None
    public_base_url: str | None = None
    browser_session_enabled: bool = False
    browser_display_base: int | None = None
    browser_vnc_port_base: int | None = None
    browser_web_port_base: int | None = None
    browser_debug_port_base: int | None = None


class ChannelRecord(BaseModel):
    id: str
    name: str
    channel_id: str
    avatar_url: str | None = None
    worker_id: str
    worker_name: str
    manager_name: str
    status: ChannelStatus
    oauth_email: str | None = None
    oauth_connected_at: datetime | None = None
    oauth_google_subject: str | None = None
    oauth_refresh_token: str | None = None
    oauth_scope: str | None = None
    oauth_token_type: str | None = None
    connection_mode: ChannelConnectionMode = "oauth"
    browser_profile_key: str | None = None
    browser_profile_path: str | None = None
    browser_connected_at: datetime | None = None


class UserBotAssignment(BaseModel):
    id: str
    user_id: str
    user_name: str
    worker_id: str
    worker_name: str
    bot_type: Literal["1080p", "4k"]
    number_of_threads: int
    created_at: datetime | None = None


class UploadCapabilities(BaseModel):
    allow_local_upload: bool = True
    allow_resumable_upload: bool = True
    resumable_chunk_bytes: int = 8_388_608
    max_local_upload_bytes: int = 8_589_934_592
    allowed_extensions: list[str] = Field(default_factory=lambda: [".mp4", ".mov", ".webm", ".mp3", ".wav", ".jpg", ".png"])


class OAuthSummary(BaseModel):
    connected_count: int
    needs_reconnect_count: int


class UserBootstrapResponse(BaseModel):
    user: UserSummary
    channels: list[ChannelRecord]
    upload_capabilities: UploadCapabilities
    oauth: OAuthSummary


class JobAsset(BaseModel):
    slot: Literal["intro", "video_loop", "audio_loop", "outro"]
    label: str
    source_mode: SourceMode
    asset_id: str | None = None
    url: str | None = None
    file_name: str | None = None


class RenderJobRecord(BaseModel):
    id: str
    title: str
    description: str | None = None
    source_mode: SourceMode
    channel_id: str
    channel_name: str
    channel_avatar_url: str | None = None
    worker_name: str | None = None
    manager_name: str | None = None
    status: JobStatus
    progress: int = 0
    download_progress: int = 0
    render_progress: int = 0
    upload_progress: int = 0
    status_message: str | None = None
    queue_order: int | None = None
    visibility: JobVisibility = "draft"
    time_render_string: str = "00:30:00"
    scheduled_at: datetime | None = None
    created_at: datetime
    download_started_at: datetime | None = None
    upload_started_at: datetime | None = None
    completed_at: datetime | None = None
    started_at: datetime | None = None
    claimed_at: datetime | None = None
    claimed_by_worker_id: str | None = None
    lease_expires_at: datetime | None = None
    error_message: str | None = None
    output_url: str | None = None
    source_label: str
    source_file_name: str | None = None
    thumbnail_url: str | None = None
    thumbnail_path: str | None = None
    can_cancel: bool = True
    assets: list[JobAsset] = Field(default_factory=list)


class LiveStreamRecord(BaseModel):
    id: str
    owner_user_id: str
    owner_username: str
    owner_display_name: str
    manager_id: str | None = None
    manager_name: str | None = None
    primary_worker_id: str
    primary_worker_name: str
    primary_group: str | None = None
    backup_worker_id: str | None = None
    backup_worker_name: str | None = None
    backup_group: str | None = None
    runtime_role: LiveRuntimeRole = "primary"
    is_runtime_clone: bool = False
    parent_stream_id: str | None = None
    backup_stream_id: str | None = None
    stream_name: str
    stream_key: str
    rtmp_url: str = "rtmp://x.rtmp.youtube.com/live2"
    platform: LivePlatform = "youtube_rtmp"
    video_url: str
    audio_url: str | None = None
    live_label: str = "Live 11h"
    is_forever: bool = False
    is_live_now: bool = False
    start_time_live: datetime | None = None
    end_time_live: datetime | None = None
    backup_delay_minutes: int = 0
    status: LiveStreamStatus = "draft"
    status_message: str | None = None
    progress: int = 0
    log_label: str = "Khởi tạo"
    error_message: str | None = None
    claimed_by_worker_id: str | None = None
    claimed_by_role: LiveRuntimeRole | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    download_started_at: datetime | None = None
    prepared_at: datetime | None = None
    waiting_started_at: datetime | None = None
    streaming_started_at: datetime | None = None
    first_streaming_started_at: datetime | None = None
    disconnected_at: datetime | None = None
    stop_requested_at: datetime | None = None
    ended_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class UserJobsResponse(BaseModel):
    jobs: list[RenderJobRecord]


class JobCreatePayload(BaseModel):
    channel_id: str
    title: str
    description: str | None = None
    source_mode: SourceMode
    visibility: JobVisibility = "draft"
    intro_url: str | None = None
    video_loop_url: str | None = None
    audio_loop_url: str | None = None
    outro_url: str | None = None
    intro_asset_id: str | None = None
    video_loop_asset_id: str | None = None
    audio_loop_asset_id: str | None = None
    outro_asset_id: str | None = None
    time_render_string: str = "00:30:00"
    schedule_time: datetime | None = None


class OAuthStartResponse(BaseModel):
    auth_url: str | None = None
    message: str


class BrowserSessionRecord(BaseModel):
    session_id: str
    owner_user_id: str
    owner_username: str
    target_worker_id: str = ""
    target_worker_name: str = ""
    target_worker_public_base_url: str | None = None
    profile_key: str
    display_number: int
    vnc_port: int
    web_port: int
    debug_port: int
    access_password: str
    status: BrowserSessionStatus = "launching"
    created_at: datetime
    expires_at: datetime
    launched_at: datetime | None = None
    confirmed_at: datetime | None = None
    novnc_url: str | None = None
    profile_path: str | None = None
    session_path: str | None = None
    password_file: str | None = None
    current_url: str | None = None
    current_title: str | None = None
    detected_channel_id: str | None = None
    detected_channel_name: str | None = None
    channel_record_id: str | None = None
    last_error: str | None = None
    xvfb_pid: int | None = None
    openbox_pid: int | None = None
    chromium_pid: int | None = None
    x11vnc_pid: int | None = None
    websockify_pid: int | None = None


class BrowserSessionResponse(BaseModel):
    session_id: str
    status: BrowserSessionStatus
    target_worker_id: str
    target_worker_name: str
    target_worker_public_base_url: str | None = None
    novnc_url: str | None = None
    access_password: str
    expires_at: datetime
    current_url: str | None = None
    current_title: str | None = None
    detected_channel_id: str | None = None
    detected_channel_name: str | None = None
    channel_record_id: str | None = None
    last_error: str | None = None


class BrowserSessionCreateRequest(BaseModel):
    worker_id: str | None = None


class BrowserSessionConfirmResponse(BaseModel):
    session: BrowserSessionResponse
    channel: dict[str, str | None]


class AdminSummary(BaseModel):
    total_managers: int
    total_users: int
    total_workers: int
    workers_online: int
    channels_connected: int
    queued_jobs: int
    running_jobs: int
    failed_jobs: int


class AdminDashboardResponse(BaseModel):
    summary: AdminSummary
    users: list[UserSummary]
    workers: list[WorkerRecord]
    channels: list[ChannelRecord]
    jobs: list[RenderJobRecord]


class UploadSessionCreateRequest(BaseModel):
    slot: Literal["intro", "video_loop", "audio_loop", "outro"]
    file_name: str
    size_bytes: int
    content_type: str | None = None


class UploadSessionRecord(BaseModel):
    session_id: str
    owner_user_id: str | None = None
    slot: Literal["intro", "video_loop", "audio_loop", "outro"]
    file_name: str
    content_type: str | None = None
    size_bytes: int
    received_bytes: int = 0
    chunk_size: int
    status: UploadSessionStatus = "active"
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    temp_path: str
    asset_id: str | None = None
    stored_file_name: str | None = None


class UploadSessionResponse(BaseModel):
    session_id: str
    slot: Literal["intro", "video_loop", "audio_loop", "outro"]
    file_name: str
    size_bytes: int
    received_bytes: int
    chunk_size: int
    status: UploadSessionStatus
    expires_at: datetime
    asset_id: str | None = None
    stored_file_name: str | None = None


class WorkerRegisterPayload(BaseModel):
    worker_id: str
    name: str
    shared_secret: str
    manager_name: str = "system"
    group: str | None = None
    capacity: int = 1
    threads: int = 1
    disk_total_gb: float = 0
    public_base_url: str | None = None
    browser_session_enabled: bool = False
    browser_display_base: int | None = None
    browser_vnc_port_base: int | None = None
    browser_web_port_base: int | None = None
    browser_debug_port_base: int | None = None


class WorkerHeartbeatPayload(BaseModel):
    worker_id: str
    shared_secret: str
    load_percent: int = 0
    ram_percent: int = 0
    ram_used_gb: float = 0
    ram_total_gb: float = 0
    bandwidth_kbps: float = 0
    disk_used_gb: float = 0
    disk_total_gb: float = 0
    threads: int = 1
    status: WorkerStatus = "online"
    active_job_ids: list[str] | None = None
    public_base_url: str | None = None
    browser_session_enabled: bool | None = None
    browser_display_base: int | None = None
    browser_vnc_port_base: int | None = None
    browser_web_port_base: int | None = None
    browser_debug_port_base: int | None = None


class WorkerControlResponse(BaseModel):
    ok: bool
    worker: WorkerRecord


class WorkerAuthPayload(BaseModel):
    worker_id: str
    shared_secret: str


class WorkerDecommissionCompletePayload(BaseModel):
    worker_id: str
    shared_secret: str
    status: str = "completed"
    message: str | None = None


class WorkerBrowserProfileCleanupAckPayload(BaseModel):
    worker_id: str
    shared_secret: str
    profile_keys: list[str] = Field(default_factory=list)


class WorkerClaimResponse(BaseModel):
    ok: bool
    worker: WorkerRecord
    job: RenderJobRecord | None = None


class WorkerJobProgressPayload(BaseModel):
    worker_id: str
    shared_secret: str
    status: JobStatus
    progress: int = 0
    message: str | None = None


class WorkerJobCompletePayload(BaseModel):
    worker_id: str
    shared_secret: str
    output_url: str | None = None
    message: str | None = None


class WorkerJobFailPayload(BaseModel):
    worker_id: str
    shared_secret: str
    message: str


class LiveWorkerRegisterPayload(BaseModel):
    worker_id: str
    name: str
    shared_secret: str
    manager_name: str = "system"
    group: str | None = None
    capacity: int = 1
    threads: int = 1
    disk_total_gb: float = 0


class LiveWorkerHeartbeatPayload(BaseModel):
    worker_id: str
    shared_secret: str
    load_percent: int = 0
    ram_percent: int = 0
    ram_used_gb: float = 0
    ram_total_gb: float = 0
    bandwidth_kbps: float = 0
    disk_used_gb: float = 0
    disk_total_gb: float = 0
    threads: int = 1
    status: WorkerStatus = "online"
    active_stream_ids: list[str] | None = None


class LiveWorkerControlResponse(BaseModel):
    ok: bool
    worker: WorkerRecord


class LiveWorkerAuthPayload(BaseModel):
    worker_id: str
    shared_secret: str


class LiveWorkerClaimResponse(BaseModel):
    ok: bool
    worker: WorkerRecord
    stream: LiveStreamRecord | None = None


class LiveWorkerProgressPayload(BaseModel):
    worker_id: str
    shared_secret: str
    status: LiveStreamStatus
    progress: int = 0
    message: str | None = None


class LiveWorkerCompletePayload(BaseModel):
    worker_id: str
    shared_secret: str
    message: str | None = None


class LiveWorkerFailPayload(BaseModel):
    worker_id: str
    shared_secret: str
    message: str


class WorkerBrowserSessionSyncPayload(BaseModel):
    worker_id: str
    shared_secret: str
    status: BrowserSessionStatus
    novnc_url: str | None = None
    current_url: str | None = None
    current_title: str | None = None
    detected_channel_id: str | None = None
    detected_channel_name: str | None = None
    last_error: str | None = None
    profile_path: str | None = None
    session_path: str | None = None
    password_file: str | None = None
    xvfb_pid: int | None = None
    openbox_pid: int | None = None
    chromium_pid: int | None = None
    x11vnc_pid: int | None = None
    websockify_pid: int | None = None


class WorkerYouTubeUploadTarget(BaseModel):
    job_id: str
    channel_id: str
    channel_name: str
    title: str
    description: str | None = None
    connection_mode: ChannelConnectionMode = "browser_profile"
    browser_profile_key: str | None = None
    browser_profile_path: str | None = None
