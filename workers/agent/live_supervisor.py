from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SUPERVISOR_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_FFMPEG_LOG_MAX_BYTES = 10 * 1024 * 1024


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_int(value: int | str | float | None) -> int:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, min(100, parsed))


class LiveSupervisor:
    """Small local state/log sink for live runtimes on the worker VPS."""

    def __init__(
        self,
        *,
        root: Path,
        worker_id: str,
        stream_id: str,
        event_log_max_bytes: int = DEFAULT_SUPERVISOR_LOG_MAX_BYTES,
        ffmpeg_log_max_bytes: int = DEFAULT_FFMPEG_LOG_MAX_BYTES,
    ) -> None:
        self.root = root
        self.worker_id = str(worker_id)
        self.stream_id = str(stream_id)
        self.event_log_max_bytes = max(1024, int(event_log_max_bytes))
        self.ffmpeg_log_max_bytes = max(1024, int(ffmpeg_log_max_bytes))
        self.current_path = self.root / "current.json"
        self.events_path = self.root / "events.log"
        self.ffmpeg_path = self.root / "ffmpeg.log"
        self.root.mkdir(parents=True, exist_ok=True)

    def _rotate_if_needed(self, path: Path, max_bytes: int) -> None:
        try:
            if not path.exists() or path.stat().st_size <= max_bytes:
                return
            rotated = path.with_name(f"{path.name}.1")
            if rotated.exists():
                rotated.unlink()
            path.replace(rotated)
        except OSError:
            return

    def _append_jsonl(self, payload: dict[str, Any]) -> None:
        self._rotate_if_needed(self.events_path, self.event_log_max_bytes)
        with self.events_path.open("a", encoding="utf-8") as file_obj:
            file_obj.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    def record_state(
        self,
        status: str,
        progress: int | str | float | None = 0,
        message: str | None = None,
        *,
        event: str = "progress",
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "event": event,
            "worker_id": self.worker_id,
            "stream_id": self.stream_id,
            "status": str(status or "").strip().lower() or "unknown",
            "progress": _bounded_int(progress),
            "message": (message or "").strip() or None,
        }
        payload.update({key: value for key, value in extra.items() if value is not None})
        temp_path = self.current_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temp_path, self.current_path)
        self._append_jsonl(payload)

    def record_event(self, event: str, **fields: Any) -> None:
        payload: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "event": str(event or "event").strip() or "event",
            "worker_id": self.worker_id,
            "stream_id": self.stream_id,
        }
        payload.update({key: value for key, value in fields.items() if value is not None})
        self._append_jsonl(payload)

    def append_ffmpeg_line(self, line: str) -> None:
        cleaned = str(line or "").rstrip()
        if not cleaned:
            return
        self._rotate_if_needed(self.ffmpeg_path, self.ffmpeg_log_max_bytes)
        with self.ffmpeg_path.open("a", encoding="utf-8", errors="replace") as file_obj:
            file_obj.write(f"{_utc_now_iso()} {cleaned}\n")
