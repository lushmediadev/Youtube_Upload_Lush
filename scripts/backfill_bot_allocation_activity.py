#!/usr/bin/env python3
"""Recover allocation activity watermarks from retained SQLite state snapshots."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def activity_key(*, workspace: str, role: str, worker_id: str, user_id: str) -> str:
    return "|".join((workspace, role, worker_id, user_id))


def read_state(path: Path) -> tuple[dict[str, Any], str]:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT payload, updated_at FROM app_state WHERE state_key = 'main'"
        ).fetchone()
    if row is None:
        raise ValueError(f"{path} has no app_state/main row")
    return json.loads(row[0]), str(row[1] or "")


def worker_aliases(state: dict[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for worker in state.get("workers") or []:
        worker_id = str(worker.get("id") or "").strip()
        if not worker_id:
            continue
        for alias in (worker_id, str(worker.get("name") or "").strip()):
            if alias:
                aliases[alias] = worker_id
    return aliases


def merge_activity(
    activity: dict[str, dict[str, str]],
    *,
    key: str,
    field: str,
    occurred_at: datetime | None,
) -> bool:
    if occurred_at is None:
        return False
    entry = activity.setdefault(key, {})
    existing = parse_datetime(entry.get(field))
    if existing is not None and existing >= occurred_at:
        return False
    entry[field] = occurred_at.isoformat()
    return True


def backfill_upload_activity(state: dict[str, Any], activity: dict[str, dict[str, str]]) -> int:
    aliases = worker_aliases(state)
    channels = {str(channel.get("id") or ""): channel for channel in state.get("channels") or []}
    users_by_channel: dict[str, set[str]] = {}
    for link in state.get("channel_user_links") or []:
        channel_id = str(link.get("channel_id") or "").strip()
        user_id = str(link.get("user_id") or "").strip()
        if channel_id and user_id:
            users_by_channel.setdefault(channel_id, set()).add(user_id)

    changes = 0
    for job in state.get("jobs") or []:
        channel_id = str(job.get("channel_id") or "").strip()
        channel = channels.get(channel_id)
        if channel is None:
            continue
        worker_id = aliases.get(str(channel.get("worker_id") or "").strip())
        job_worker_id = aliases.get(str(job.get("worker_name") or "").strip())
        if not worker_id or (job_worker_id and job_worker_id != worker_id):
            continue
        for user_id in users_by_channel.get(channel_id, set()):
            changes += merge_activity(
                activity,
                key=activity_key(workspace="upload", role="upload", worker_id=worker_id, user_id=user_id),
                field="last_job_created_at",
                occurred_at=parse_datetime(job.get("created_at")),
            )
    return changes


def terminal_activity_at(stream: dict[str, Any]) -> datetime | None:
    if str(stream.get("status") or "").strip().lower() not in {"ended", "stopped", "error"}:
        return None
    candidates = (
        parse_datetime(stream.get("ended_at")),
        parse_datetime(stream.get("stop_requested_at")),
        parse_datetime(stream.get("updated_at")),
        parse_datetime(stream.get("created_at")),
    )
    return max((value for value in candidates if value is not None), default=None)


def backfill_live_activity(state: dict[str, Any], activity: dict[str, dict[str, str]]) -> int:
    changes = 0
    for stream in state.get("live_streams") or []:
        if stream.get("is_runtime_clone"):
            continue
        user_id = str(stream.get("owner_user_id") or "").strip()
        if not user_id:
            continue
        created_at = parse_datetime(stream.get("created_at"))
        terminal_at = terminal_activity_at(stream)
        for role, worker_field in (("primary", "primary_worker_id"), ("backup", "backup_worker_id")):
            worker_id = str(stream.get(worker_field) or "").strip()
            if not worker_id:
                continue
            key = activity_key(workspace="live", role=role, worker_id=worker_id, user_id=user_id)
            changes += merge_activity(activity, key=key, field="last_job_created_at", occurred_at=created_at)
            changes += merge_activity(activity, key=key, field="last_terminal_at", occurred_at=terminal_at)
    return changes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path, required=True, help="Runtime app_state.db to update")
    parser.add_argument("--snapshot", type=Path, action="append", required=True, help="Historical SQLite snapshot")
    parser.add_argument("--apply", action="store_true", help="Write recovered watermarks to state-db")
    parser.add_argument("--backup-path", type=Path, help="SQLite backup created before write")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply and args.backup_path is None:
        raise SystemExit("--apply requires --backup-path")

    current_state, current_updated_at = read_state(args.state_db)
    raw_activity = current_state.get("bot_allocation_activity") or {}
    activity = {
        str(key): {str(field): str(value) for field, value in value.items() if value}
        for key, value in raw_activity.items()
        if isinstance(value, dict)
    }
    initial_activity = json.dumps(activity, sort_keys=True)

    for snapshot_path in args.snapshot:
        snapshot_state, snapshot_updated_at = read_state(snapshot_path)
        upload_changes = backfill_upload_activity(snapshot_state, activity)
        live_changes = backfill_live_activity(snapshot_state, activity)
        print(
            f"snapshot={snapshot_path} updated_at={snapshot_updated_at} "
            f"upload_updates={upload_changes} live_updates={live_changes}"
        )

    changed = json.dumps(activity, sort_keys=True) != initial_activity
    print(f"state_updated_at={current_updated_at} allocation_entries={len(activity)} changed={changed}")
    if not args.apply or not changed:
        return 0

    args.backup_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.state_db) as source, sqlite3.connect(args.backup_path) as target:
        source.backup(target)
    current_state["bot_allocation_activity"] = activity
    payload = json.dumps(current_state, ensure_ascii=False)
    with sqlite3.connect(args.state_db) as connection:
        connection.execute(
            "UPDATE app_state SET payload = ?, updated_at = ? WHERE state_key = 'main'",
            (payload, datetime.now().isoformat()),
        )
        connection.commit()
    print(f"applied_backup={args.backup_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
