from __future__ import annotations

from typing import Any

import httpx

from .browser_runtime import BrowserRuntimeManager
from .config import WorkerConfig
from .control_plane import (
    BrowserProfileCleanupAssignment,
    BrowserSessionAssignment,
    ack_browser_profile_cleanup,
    poll_browser_sessions,
    sync_browser_session,
)


class BrowserSessionCoordinator:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.runtime = BrowserRuntimeManager(config.work_root / "browser-runtime")
        self.local_sessions: dict[str, dict[str, Any]] = self.runtime.load_persisted_sessions()

    @staticmethod
    def _base_record(session: BrowserSessionAssignment) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "purpose": session.purpose,
            "profile_key": session.profile_key,
            "display_number": session.display_number,
            "vnc_port": session.vnc_port,
            "web_port": session.web_port,
            "debug_port": session.debug_port,
            "access_password": session.access_password,
            "start_url": session.start_url,
        }

    def _stop_local_session(self, session_id: str) -> None:
        record = self.local_sessions.pop(session_id, None)
        if record:
            self.runtime.stop(record)

    def _cleanup_profiles(
        self,
        client: httpx.Client,
        cleanup_profiles: list[BrowserProfileCleanupAssignment],
    ) -> None:
        if not cleanup_profiles:
            return

        cleared_profile_keys: list[str] = []
        for cleanup in cleanup_profiles:
            profile_key = str(cleanup.profile_key or "").strip()
            if not profile_key:
                continue

            for session_id, record in list(self.local_sessions.items()):
                if str(record.get("profile_key") or "").strip() != profile_key:
                    continue
                self._stop_local_session(session_id)

            self.runtime.delete_profile(profile_key)
            cleared_profile_keys.append(profile_key)

        ack_browser_profile_cleanup(client, self.config, cleared_profile_keys)

    def _sync_running_session(
        self,
        client: httpx.Client,
        session: BrowserSessionAssignment,
        local_record: dict[str, Any],
    ) -> None:
        inspection = self.runtime.inspect(local_record)
        sync_browser_session(
            client,
            self.config,
            session.session_id,
            status="confirmed" if session.status == "confirmed" else "awaiting_confirmation",
            novnc_url=local_record.get("novnc_url"),
            current_url=inspection.get("current_url"),
            current_title=inspection.get("current_title"),
            detected_channel_id=inspection.get("channel_id"),
            detected_channel_name=inspection.get("channel_name"),
            last_error=None,
            profile_path=local_record.get("profile_path"),
            session_path=local_record.get("session_path"),
            password_file=local_record.get("password_file"),
            xvfb_pid=local_record.get("xvfb_pid"),
            openbox_pid=local_record.get("openbox_pid"),
            chromium_pid=local_record.get("chromium_pid"),
            x11vnc_pid=local_record.get("x11vnc_pid"),
            websockify_pid=local_record.get("websockify_pid"),
        )

    def _ensure_local_session(
        self,
        client: httpx.Client,
        session: BrowserSessionAssignment,
    ) -> None:
        local_record = self.local_sessions.get(session.session_id)
        if local_record and self.runtime.is_running(local_record):
            try:
                self._sync_running_session(client, session, local_record)
                return
            except Exception as exc:
                sync_browser_session(
                    client,
                    self.config,
                    session.session_id,
                    status="confirmed" if session.status == "confirmed" else "awaiting_confirmation",
                    novnc_url=local_record.get("novnc_url"),
                    current_url=session.current_url,
                    current_title=session.current_title,
                    detected_channel_id=session.detected_channel_id,
                    detected_channel_name=session.detected_channel_name,
                    last_error=str(exc),
                    profile_path=local_record.get("profile_path"),
                    session_path=local_record.get("session_path"),
                    password_file=local_record.get("password_file"),
                    xvfb_pid=local_record.get("xvfb_pid"),
                    openbox_pid=local_record.get("openbox_pid"),
                    chromium_pid=local_record.get("chromium_pid"),
                    x11vnc_pid=local_record.get("x11vnc_pid"),
                    websockify_pid=local_record.get("websockify_pid"),
                )
                return

        record = self._base_record(session)
        try:
            launch_meta = self.runtime.launch(record)
        except Exception as exc:
            sync_browser_session(
                client,
                self.config,
                session.session_id,
                status="failed",
                last_error=str(exc),
            )
            self._stop_local_session(session.session_id)
            return

        local_record = {**record, **launch_meta}
        self.local_sessions[session.session_id] = local_record
        self.runtime.save_session_record(local_record)
        try:
            self._sync_running_session(client, session, local_record)
        except Exception as exc:
            sync_browser_session(
                client,
                self.config,
                session.session_id,
                status="awaiting_confirmation",
                novnc_url=local_record.get("novnc_url"),
                last_error=str(exc),
                profile_path=local_record.get("profile_path"),
                session_path=local_record.get("session_path"),
                password_file=local_record.get("password_file"),
                xvfb_pid=local_record.get("xvfb_pid"),
                openbox_pid=local_record.get("openbox_pid"),
                chromium_pid=local_record.get("chromium_pid"),
                x11vnc_pid=local_record.get("x11vnc_pid"),
                websockify_pid=local_record.get("websockify_pid"),
            )

    def reconcile(self, client: httpx.Client) -> None:
        assignments, cleanup_profiles = poll_browser_sessions(client, self.config)
        self._cleanup_profiles(client, cleanup_profiles)
        desired_ids = {session.session_id for session in assignments}
        active_statuses = {"launching", "awaiting_confirmation", "confirmed"}
        ordered_assignments = sorted(
            assignments,
            key=lambda session: (1 if session.status in active_statuses else 0, session.session_id),
        )

        for stale_id in [session_id for session_id in self.local_sessions if session_id not in desired_ids]:
            self._stop_local_session(stale_id)

        for session in ordered_assignments:
            if session.status in active_statuses:
                self._ensure_local_session(client, session)
                continue

            self._stop_local_session(session.session_id)
            sync_browser_session(client, self.config, session.session_id, status=session.status)
