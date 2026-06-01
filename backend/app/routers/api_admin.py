from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..request_urls import resolve_worker_bootstrap_control_plane_url
from ..auth import normalize_manager_filter_ids, require_admin_access, require_admin_only
from ..store import store
from ..worker_bootstrap import (
    WorkerBootstrapError,
    build_worker_bootstrap_request,
    build_worker_decommission_request,
    normalize_ssh_user,
    start_worker_decommission_operation,
    start_worker_install_operation,
)

router = APIRouter(tags=["admin"])


class AdminUserCreatePayload(BaseModel):
    username: str
    password: str
    manager_id: str | None = None


class AdminUserUpdatePayload(BaseModel):
    username: str
    password: str | None = None
    manager_id: str | None = None
    telegram: str | None = None


class AdminResetPasswordPayload(BaseModel):
    password: str


class AdminUserBotPayload(BaseModel):
    worker_id: str
    threads: int = 1
    bot_type: str = "1080p"


class AdminUserBotUpdatePayload(BaseModel):
    threads: int = 1
    bot_type: str | None = None


class AdminBotUpdatePayload(BaseModel):
    name: str
    local: str | None = None
    group: str | None = None
    password: str | None = None
    manager_id: str | None = None
    live_role: str | None = None
    threads: int | None = None
    assigned_user_id: str | None = None
    assigned_user_ids: list[str] | None = None
    confirm_manager_transfer_cleanup: bool = False
    confirm_workspace_transfer_cleanup: bool = False
    current_workspace: str | None = None
    workspace: str | None = None


class AdminBotThreadPayload(BaseModel):
    thread: int


class AdminBotInstallPayload(BaseModel):
    vps_ip: str
    ssh_user: str = "root"
    auth_mode: str = "password"
    password: str | None = None
    ssh_private_key: str | None = None
    bot_kind: str | None = None
    workspace: str | None = None
    name: str | None = None
    local: str | None = None
    group: str | None = None
    manager_id: str | None = None
    live_role: str | None = None
    threads: int | None = None
    assigned_user_ids: list[str] | None = None


class AdminBotDecommissionPayload(BaseModel):
    ssh_user: str = "root"
    auth_mode: str = "password"
    password: str | None = None
    ssh_private_key: str | None = None
    workspace: str | None = None


class ManagerFilterPayload(BaseModel):
    manager_ids: list[str] = []


def _manager_ids_from_request(request: Request, manager_ids: list[str] | None = None) -> list[str]:
    current_user = require_admin_access(request)
    available_ids = [user.id for user in store.users if user.role == "manager"]
    return normalize_manager_filter_ids(request, manager_ids or [], available_ids, current_user)


def _resolve_workspace_mode(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    return "live" if normalized == "live" else "upload"


def _assert_upload_workspace(workspace: str | None, *, detail: str = "Workspace Live Stream không có Danh sách Kênh.") -> None:
    if _resolve_workspace_mode(workspace) == "live":
        raise HTTPException(status_code=400, detail=detail)


def _current_user_payload(request: Request) -> dict[str, Any]:
    current_user = require_admin_access(request)
    return {
        "id": current_user.id,
        "username": current_user.username,
        "display_name": current_user.display_name,
        "role": current_user.role,
        "manager_name": current_user.manager_name,
        "manager_filter_ids": request.session.get("admin_manager_ids") or [],
    }


def _enforce_user_scope(request: Request, user_id: str) -> None:
    current_user = require_admin_access(request)
    if current_user.role != "manager":
        return
    user = store._find_user(user_id)
    if user.role == "admin":
        raise HTTPException(status_code=403, detail="Manager không được thao tác admin.")
    if user.role == "manager" and user.id != current_user.id:
        raise HTTPException(status_code=403, detail="Không đủ quyền thao tác manager này.")
    if user.role == "user" and user.manager_name != current_user.username:
        raise HTTPException(status_code=403, detail="Không đủ quyền thao tác user khác manager.")


def _enforce_worker_scope(request: Request, worker_id: str, *, workspace_mode: str = "upload") -> None:
    current_user = require_admin_access(request)
    if current_user.role != "manager":
        return
    worker = store._find_live_worker(worker_id) if workspace_mode == "live" else store._find_worker(worker_id)
    if worker.manager_id != current_user.id:
        raise HTTPException(status_code=403, detail="Không đủ quyền thao tác BOT ngoài scope manager.")


@router.get("/admin/live/dashboard")
async def get_admin_live_dashboard(
    request: Request,
    manager_ids: list[str] = Query(default=[]),
    userId: str | None = None,
):
    current_user = require_admin_access(request)
    resolved_user_id = str(userId or "").strip() or current_user.id
    _enforce_user_scope(request, resolved_user_id)
    resolved_manager_ids = _manager_ids_from_request(request, manager_ids)
    if not resolved_manager_ids:
        focus_user = store._find_user(resolved_user_id)
        if focus_user.role == "manager":
            resolved_manager_ids = [focus_user.id]
        else:
            resolved_manager_id = store._resolved_user_manager_id(focus_user)
            resolved_manager_ids = [resolved_manager_id] if resolved_manager_id else []
    return store.get_admin_live_workspace_payload(
        manager_ids=resolved_manager_ids,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
        owner_user_id=resolved_user_id,
    )


@router.get("/admin/live/telegram")
async def get_admin_live_telegram_binding(request: Request, userId: str):
    _enforce_user_scope(request, userId)
    try:
        return store.get_live_telegram_binding(userId)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/admin/live/telegram")
async def delete_admin_live_telegram_binding(request: Request, userId: str):
    current_user = require_admin_access(request)
    _enforce_user_scope(request, userId)
    try:
        return store.set_live_telegram_chat_id(
            userId,
            None,
            updated_by=f"admin_live_workspace:{current_user.username}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/admin/live/telegram-link/request")
async def create_admin_live_telegram_link_request(request: Request, userId: str):
    _enforce_user_scope(request, userId)
    try:
        return store.create_live_telegram_link_request(userId)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/admin/live/telegram-link/status")
async def get_admin_live_telegram_link_status(request: Request, userId: str, code: str):
    _enforce_user_scope(request, userId)
    try:
        return store.get_live_telegram_link_request_status(userId, code)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Mã liên kết Telegram live không tồn tại hoặc đã hết hạn.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _start_bot_workspace_conversion(
    request: Request,
    current_user,
    *,
    worker_id: str,
    current_workspace_mode: str,
    target_workspace_mode: str,
    name: str,
    local: str | None,
    group: str | None,
    manager_id: str | None,
    live_role: str | None,
    threads: int | None,
    password: str | None,
    assigned_user_ids: list[str],
) -> dict[str, Any]:
    resolved_current_workspace_mode = _resolve_workspace_mode(current_workspace_mode)
    resolved_target_workspace_mode = _resolve_workspace_mode(target_workspace_mode)
    if resolved_current_workspace_mode == resolved_target_workspace_mode:
        raise ValueError("BOT này không đổi workspace.")
    if assigned_user_ids and not manager_id:
        raise ValueError("Hãy chọn manager trước khi gán user cho BOT.")

    manager_name = "system"
    if manager_id:
        selected_manager = store._find_user(manager_id)
        if selected_manager.role != "manager":
            raise ValueError("Manager được chọn không hợp lệ.")
        manager_name = selected_manager.username
    elif current_user.role == "manager":
        manager_name = current_user.username

    source_live_role = (
        store._live_worker_assigned_role(worker_id)
        if resolved_current_workspace_mode == "live"
        else "upload"
    )
    desired_live_role = str(live_role or "").strip().lower() or None
    desired_threads = (
        store._normalize_live_worker_threads(threads)
        if resolved_target_workspace_mode == "live"
        else 1
    )
    if resolved_target_workspace_mode == "live":
        if desired_live_role not in {"primary", "backup"}:
            raise ValueError("Chức năng BOT live không hợp lệ.")
        if assigned_user_ids and not desired_live_role:
            raise ValueError("Hãy chọn chức năng BOT live trước khi gán user.")
    else:
        desired_live_role = "upload"

    source_label = (
        "Upload"
        if resolved_current_workspace_mode != "live"
        else "Backup" if source_live_role == "backup" else "Live chính"
    )
    target_label = (
        "Upload"
        if resolved_target_workspace_mode != "live"
        else "Backup" if desired_live_role == "backup" else "Live chính"
    )
    target_worker_id = (
        store.suggest_next_live_worker_bootstrap_id()
        if resolved_target_workspace_mode == "live"
        else store.suggest_next_worker_bootstrap_id()
    )
    normalized_password = str(password or "").strip()
    profile = store.get_worker_connection_profile(worker_id, workspace_mode=resolved_current_workspace_mode)
    effective_auth_mode = "password" if normalized_password else str(profile.get("auth_mode") or "password").strip().lower() or "password"
    effective_password = normalized_password or profile["password"]
    bootstrap_request = build_worker_bootstrap_request(
        vps_ip=profile["vps_ip"],
        ssh_user=profile["ssh_user"],
        password=effective_password if effective_auth_mode != "ssh_key" else None,
        ssh_private_key=profile["ssh_private_key"],
        shared_secret=store.get_worker_shared_secret(),
        control_plane_url=resolve_worker_bootstrap_control_plane_url(request),
        worker_id=target_worker_id,
        manager_name=manager_name,
        runtime_mode=resolved_target_workspace_mode,
    )
    bootstrap_request.capacity = (
        max(desired_threads, desired_threads * max(1, len(assigned_user_ids)))
        if resolved_target_workspace_mode == "live"
        else 1
    )
    bootstrap_request.threads = bootstrap_request.capacity
    return start_worker_install_operation(
        store=store,
        request=bootstrap_request,
        ssh_user=profile["ssh_user"],
        auth_mode=effective_auth_mode,
        password=effective_password if effective_auth_mode != "ssh_key" else None,
        ssh_private_key=profile["ssh_private_key"],
        manager_id=manager_id,
        manager_name=manager_name,
        group=group or "",
        requested_by=current_user.username,
        requested_role=current_user.role,
        requested_user_id=current_user.id,
        post_install_config={
            "name": name,
            "local": local,
            "group": group,
            "manager_id": manager_id,
            "live_role": desired_live_role if resolved_target_workspace_mode == "live" else "upload",
            "threads": desired_threads,
            "assigned_user_ids": assigned_user_ids,
        },
        replace_other_workspace_mode=resolved_current_workspace_mode,
        workspace_conversion={
            "source_worker_id": worker_id,
            "source_workspace_mode": resolved_current_workspace_mode,
            "source_label": source_label,
            "target_label": target_label,
        },
    )


def _enforce_channel_scope(request: Request, channel_id: str) -> None:
    current_user = require_admin_access(request)
    if current_user.role != "manager":
        return
    channel = store._find_channel(channel_id)
    if store._resolve_channel_manager_id(channel) != current_user.id:
        raise HTTPException(status_code=403, detail="Không đủ quyền thao tác kênh ngoài scope manager.")


def _enforce_job_scope(request: Request, job_id: str) -> None:
    current_user = require_admin_access(request)
    if current_user.role != "manager":
        return
    job = store._find_job(job_id)
    if store._resolve_job_manager_id(job) != current_user.id:
        raise HTTPException(status_code=403, detail="Không đủ quyền thao tác job ngoài scope manager.")


def _enforce_user_bot_mapping_scope(request: Request, user_id: str, assignment_id: int) -> None:
    _enforce_user_scope(request, user_id)
    mapping = next((item for item in store.user_worker_links if int(item.get("id") or 0) == assignment_id), None)
    if mapping is None or str(mapping.get("user_id") or "").strip() != user_id:
        raise HTTPException(status_code=403, detail="Không đủ quyền thao tác mapping BOT này.")


def _scoped_job_ids(request: Request, *, channel_id: str | None = None) -> list[str]:
    current_user = require_admin_access(request)
    jobs = list(store.jobs)
    if current_user.role == "manager":
        jobs = [job for job in jobs if store._resolve_job_manager_id(job) == current_user.id]
    if channel_id:
        jobs = [job for job in jobs if job.channel_id == channel_id]
    return [job.id for job in jobs]


@router.get("/admin/session")
async def get_admin_session(request: Request):
    return {"current_user": _current_user_payload(request)}


@router.post("/admin/session/manager-filter")
async def update_admin_manager_filter(request: Request, payload: ManagerFilterPayload):
    manager_ids = _manager_ids_from_request(request, payload.manager_ids)
    return {"manager_ids": manager_ids}


@router.get("/admin/dashboard")
async def get_admin_dashboard(request: Request):
    current_user = require_admin_access(request)
    selected_manager_ids = _manager_ids_from_request(request, [])
    effective_manager_ids = store._effective_manager_scope_ids(
        viewer_role=current_user.role,
        viewer_id=current_user.id,
        manager_ids=selected_manager_ids,
    )
    scoped_users = store._build_user_rows(
        effective_manager_ids,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    )
    workers = [
        row
        for row in store.get_admin_bot_index_context(
            manager_ids=effective_manager_ids,
            viewer_role=current_user.role,
            viewer_id=current_user.id,
        ).get("workers", [])
    ]
    channels = store.get_admin_channel_index_context(
        manager_ids=effective_manager_ids,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    ).get("channels", [])
    renders = store.get_admin_render_index_context(
        manager_ids=effective_manager_ids,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    ).get("renders", [])
    return {
        "summary": store._scoped_admin_summary(
            viewer_role=current_user.role,
            viewer_id=current_user.id,
            manager_ids=effective_manager_ids,
        ),
        "users": scoped_users,
        "workers": workers,
        "channels": channels,
        "jobs": renders,
    }


@router.get("/admin/users")
async def get_admin_users(request: Request, manager_ids: list[str] | None = Query(default=None)):
    selected_manager_ids = _manager_ids_from_request(request, manager_ids)
    current_user = require_admin_access(request)
    return {"items": store._build_user_rows(selected_manager_ids, viewer_role=current_user.role, viewer_id=current_user.id)}


@router.get("/admin/users/{user_id}")
async def get_admin_user_detail(request: Request, user_id: str):
    _enforce_user_scope(request, user_id)
    current_user = require_admin_access(request)
    return store.get_admin_user_edit_context(user_id=user_id, viewer_role=current_user.role, viewer_id=current_user.id)


@router.post("/admin/users")
async def create_admin_user(request: Request, payload: AdminUserCreatePayload):
    current_user = require_admin_access(request)
    manager_id = current_user.id if current_user.role == "manager" else payload.manager_id
    if manager_id == "__bot_empty__":
        manager_id = None
    result = store.create_admin_user(
        username=payload.username,
        display_name=payload.username,
        password=payload.password,
        role="user",
        manager_id=manager_id,
        telegram=None,
        updated_by=current_user.username,
    )
    return result


@router.put("/admin/users/{user_id}")
async def update_admin_user(request: Request, user_id: str, payload: AdminUserUpdatePayload):
    current_user = require_admin_access(request)
    _enforce_user_scope(request, user_id)
    manager_id = current_user.id if current_user.role == "manager" else payload.manager_id
    payload_fields = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    telegram = payload.telegram if "telegram" in payload_fields else None
    store.update_admin_user(
        user_id=user_id,
        username=payload.username,
        password=payload.password,
        manager_id=manager_id,
        telegram=telegram,
        actor_role=current_user.role,
        updated_by=current_user.username,
    )
    return {"ok": True}


@router.post("/admin/users/{user_id}/reset-password")
async def reset_admin_user_password(request: Request, user_id: str, payload: AdminResetPasswordPayload):
    current_user = require_admin_access(request)
    _enforce_user_scope(request, user_id)
    store.reset_admin_user_password(user_id, payload.password, updated_by=current_user.username)
    return {"ok": True}


@router.delete("/admin/users/{user_id}")
async def delete_admin_user(request: Request, user_id: str):
    _enforce_user_scope(request, user_id)
    store.delete_admin_user(user_id)
    return {"ok": True}


@router.get("/admin/users/{user_id}/bots")
async def get_admin_user_bots(request: Request, user_id: str):
    _enforce_user_scope(request, user_id)
    current_user = require_admin_access(request)
    return store.get_admin_user_bot_context(
        user_id=user_id,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    )


@router.post("/admin/users/{user_id}/bots")
async def create_admin_user_bot(request: Request, user_id: str, payload: AdminUserBotPayload):
    require_admin_access(request)
    raise HTTPException(status_code=410, detail="Gán BOT đã được gộp vào Danh sách BOT.")


@router.put("/admin/users/{user_id}/bots/{assignment_id}")
async def update_admin_user_bot(request: Request, user_id: str, assignment_id: int, payload: AdminUserBotUpdatePayload):
    require_admin_access(request)
    raise HTTPException(status_code=410, detail="Sửa mapping BOT đã được gộp vào Danh sách BOT.")


@router.delete("/admin/users/{user_id}/bots/{assignment_id}")
async def delete_admin_user_bot(request: Request, user_id: str, assignment_id: int):
    require_admin_access(request)
    raise HTTPException(status_code=410, detail="Xóa mapping BOT đã được gộp vào Danh sách BOT.")


@router.get("/admin/roles/managers")
async def get_admin_manager_roles(request: Request):
    require_admin_only(request)
    return store.get_admin_manager_page_context()


@router.post("/admin/roles/managers/{user_id}")
async def toggle_manager_role(request: Request, user_id: str):
    current_user = require_admin_only(request)
    user = store._find_user(user_id)
    promote = user.role != "manager"
    store.update_role_manager(user_id, promote=promote, updated_by=current_user.username)
    return {"ok": True, "promote": promote}


@router.get("/admin/roles/admins")
async def get_admin_admin_roles(request: Request):
    require_admin_only(request)
    return store.get_admin_admin_page_context()


@router.post("/admin/roles/admins/{user_id}")
async def toggle_admin_role(request: Request, user_id: str):
    current_user = require_admin_only(request)
    user = store._find_user(user_id)
    promote = user.role != "admin"
    store.update_role_admin(user_id, promote=promote, updated_by=current_user.username)
    return {"ok": True, "promote": promote}


@router.get("/admin/bots")
async def get_admin_bots(
    request: Request,
    manager_ids: list[str] | None = Query(default=None),
    userId: str | None = None,
    after_event_id: str | None = None,
    after_event_created_at: str | None = None,
    workspace: str = "upload",
):
    if userId:
        _enforce_user_scope(request, userId)
    workspace_mode = "upload"
    selected_manager_ids = _manager_ids_from_request(request, manager_ids)
    if not selected_manager_ids and userId:
        focus_user = store._find_user(userId)
        if focus_user.role == "manager":
            selected_manager_ids = [focus_user.id]
        else:
            resolved_manager_id = store._resolved_user_manager_id(focus_user)
            if resolved_manager_id:
                selected_manager_ids = [resolved_manager_id]
    current_user = require_admin_access(request)
    if current_user.role == "manager" and not selected_manager_ids:
        selected_manager_ids = [current_user.id]
    dashboard = store.get_admin_bot_index_context(
        manager_ids=selected_manager_ids,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
        workspace_mode=workspace_mode,
    )
    notifications = store.get_admin_notifications(
        manager_ids=selected_manager_ids,
        after_id=after_event_id,
        after_created_at=after_event_created_at,
    )
    return JSONResponse(
        {
            "items": dashboard.get("workers", []),
            "summary_strip": dashboard.get("summary_strip", []),
            "events": notifications.get("items", []),
            "event_cursor": notifications.get("cursor", ""),
        },
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "Vary": "Cookie",
        },
    )


@router.post("/admin/bots/install")
async def install_admin_bot(request: Request, payload: AdminBotInstallPayload):
    current_user = require_admin_access(request)
    requested_bot_kind = str(payload.bot_kind or "").strip().lower()
    workspace_mode = _resolve_workspace_mode(
        "live" if requested_bot_kind in {"primary", "backup"} else requested_bot_kind or payload.workspace
    )
    manager_name = current_user.username if current_user.role == "manager" else "system"
    manager_id = current_user.id if current_user.role == "manager" else str(payload.manager_id or "").strip() or None
    if manager_id == "__bot_empty__":
        manager_id = None
    worker_id = (
        store.suggest_next_live_worker_bootstrap_id()
        if workspace_mode == "live"
        else store.suggest_next_worker_bootstrap_id()
    )
    ssh_user = normalize_ssh_user(payload.ssh_user)
    auth_mode = str(payload.auth_mode or "password").strip().lower() or "password"
    auth_mode = "ssh_key" if auth_mode == "ssh_key" else "password"
    requested_name = str(payload.name or "").strip() or None
    requested_local = str(payload.local or "").strip() or None
    requested_group = str(payload.group or "").strip() or None
    requested_live_role = str(payload.live_role or "").strip().lower() or None
    requested_threads = store._normalize_live_worker_threads(payload.threads) if workspace_mode == "live" else 1
    if requested_bot_kind in {"upload", "primary", "backup"}:
        requested_live_role = "upload" if requested_bot_kind == "upload" else requested_bot_kind
    requested_user_ids = [str(value).strip() for value in (payload.assigned_user_ids or []) if str(value).strip()]
    if current_user.role == "admin" and manager_id:
        selected_manager = store._find_user(manager_id)
        if selected_manager.role != "manager":
            raise HTTPException(status_code=400, detail="Manager được chọn không hợp lệ.")
        manager_name = selected_manager.username
    if requested_user_ids and not manager_id:
        raise HTTPException(status_code=400, detail="Hãy chọn manager trước khi gán user cho BOT.")
    if workspace_mode == "live":
        if requested_live_role not in {None, "", "primary", "backup"}:
            raise HTTPException(status_code=400, detail="Chức năng BOT live không hợp lệ.")
        if requested_user_ids and not requested_live_role:
            raise HTTPException(status_code=400, detail="Hãy chọn chức năng BOT live trước khi gán user.")
    else:
        requested_live_role = "upload"
    for user_id in requested_user_ids:
        _enforce_user_scope(request, user_id)
        assigned_user = store._find_user(user_id)
        if assigned_user.role == "user":
            resolved_manager_id = store._resolved_user_manager_id(assigned_user)
            if resolved_manager_id != manager_id:
                raise HTTPException(status_code=400, detail="User phải thuộc manager đã chọn.")
        elif assigned_user.role == "manager":
            if assigned_user.id != manager_id:
                raise HTTPException(status_code=400, detail="Manager chỉ được chọn chính mình trong BOT này.")
        elif assigned_user.role == "admin":
            if assigned_user.id != current_user.id:
                raise HTTPException(status_code=400, detail="Admin chỉ được tự gán BOT cho chính tài khoản admin đang đăng nhập.")
        else:
            raise HTTPException(status_code=400, detail="User được chọn không hợp lệ.")
    post_install_config = {
        "name": requested_name,
        "local": requested_local,
        "group": requested_group,
        "manager_id": manager_id,
        "live_role": requested_live_role if workspace_mode == "live" else "upload",
        "threads": requested_threads,
        "assigned_user_ids": requested_user_ids,
    }
    try:
        bootstrap_request = build_worker_bootstrap_request(
            vps_ip=payload.vps_ip,
            ssh_user=ssh_user,
            password=payload.password if auth_mode != "ssh_key" else None,
            ssh_private_key=payload.ssh_private_key if auth_mode == "ssh_key" else None,
            shared_secret=store.get_worker_shared_secret(),
            control_plane_url=resolve_worker_bootstrap_control_plane_url(request),
            worker_id=worker_id,
            manager_name=manager_name,
            runtime_mode=workspace_mode,
        )
        bootstrap_request.capacity = (
            max(requested_threads, requested_threads * max(1, len(requested_user_ids)))
            if workspace_mode == "live"
            else requested_threads
        )
        bootstrap_request.threads = bootstrap_request.capacity
        task = start_worker_install_operation(
            store=store,
            request=bootstrap_request,
            ssh_user=ssh_user,
            auth_mode=auth_mode,
            password=payload.password if auth_mode != "ssh_key" else None,
            ssh_private_key=payload.ssh_private_key if auth_mode == "ssh_key" else None,
            manager_id=manager_id,
            manager_name=manager_name,
            group=requested_group or "",
            requested_by=current_user.username,
            requested_role=current_user.role,
            requested_user_id=current_user.id,
            post_install_config=post_install_config,
        )
        return {
            "ok": True,
            "operation_id": task["id"],
            "worker_id": task["worker_id"],
            "vps_ip": task["vps_ip"],
            "message": (
                f"Đã tạo yêu cầu cài {'BOT live' if workspace_mode == 'live' else 'BOT'} "
                f"{task['worker_id']} trên {task['vps_ip']}."
            ),
        }
    except (ValueError, WorkerBootstrapError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/workers")
async def get_admin_workers_legacy(request: Request, manager_ids: list[str] | None = Query(default=None)):
    return await get_admin_bots(request, manager_ids)


@router.put("/admin/bots/{bot_id}")
async def update_admin_bot(request: Request, bot_id: str, payload: AdminBotUpdatePayload):
    current_user = require_admin_access(request)
    workspace_mode = _resolve_workspace_mode(payload.workspace)
    current_workspace_mode = _resolve_workspace_mode(payload.current_workspace or payload.workspace)
    _enforce_worker_scope(request, bot_id, workspace_mode=current_workspace_mode)
    manager_id = current_user.id if current_user.role == "manager" else payload.manager_id
    assigned_user_id = payload.assigned_user_id
    assigned_user_ids = [str(value).strip() for value in (payload.assigned_user_ids or []) if str(value).strip()]
    existing_assigned_user_ids = set()
    if payload.assigned_user_ids is not None:
        try:
            existing_assigned_user_ids = {
                user.id
                for user in (
                    store._assigned_live_users_for_worker(bot_id)
                    if current_workspace_mode == "live"
                    else store._assigned_users_for_worker(bot_id)
                )
            }
        except KeyError:
            existing_assigned_user_ids = set()
    if current_user.role == "admin" and manager_id:
        try:
            selected_owner = store._find_user(manager_id)
        except KeyError:
            selected_owner = None
        if selected_owner is None or selected_owner.role != "manager":
            assigned_user_id = None
    if assigned_user_id and not assigned_user_ids:
        _enforce_user_scope(request, assigned_user_id)
    for selected_user_id in assigned_user_ids:
        if current_user.role == "manager" and selected_user_id in existing_assigned_user_ids:
            continue
        _enforce_user_scope(request, selected_user_id)
    try:
        if current_workspace_mode != workspace_mode:
            if not payload.confirm_workspace_transfer_cleanup:
                raise ValueError("Hãy xác nhận cảnh báo chuyển loại BOT rồi thử lại.")
            _start_bot_workspace_conversion(
                request,
                current_user,
                worker_id=bot_id,
                current_workspace_mode=current_workspace_mode,
                target_workspace_mode=workspace_mode,
                name=payload.name,
                local=payload.local,
                group=payload.group,
                manager_id=manager_id,
                live_role=payload.live_role,
                threads=payload.threads,
                password=payload.password,
                assigned_user_ids=assigned_user_ids if payload.assigned_user_ids is not None else [],
            )
            return {"ok": True, "conversion_started": True}
        store.update_bot(
            bot_id,
            payload.name,
            payload.local,
            payload.group,
            manager_id,
            workspace_mode=workspace_mode,
            password=payload.password,
            live_role=payload.live_role,
            threads=payload.threads,
            assigned_user_id=assigned_user_id,
            assigned_user_ids=assigned_user_ids if payload.assigned_user_ids is not None else None,
            confirm_manager_transfer_cleanup=bool(payload.confirm_manager_transfer_cleanup),
            viewer_role=current_user.role,
            viewer_id=current_user.id,
            updated_by=current_user.username,
        )
        return {"ok": True}
    except (KeyError, ValueError, WorkerBootstrapError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/bots/assign")
async def assign_admin_bots(request: Request):
    require_admin_access(request)
    raise HTTPException(status_code=410, detail="Cấp phát BOT đã được gộp vào Danh sách BOT.")


@router.post("/admin/bots/{bot_id}/threads")
async def update_admin_bot_threads(request: Request, bot_id: str, payload: AdminBotThreadPayload):
    require_admin_access(request)
    raise HTTPException(status_code=410, detail="Thiết lập luồng BOT cũ đã được bỏ khỏi UI điều phối hiện tại.")


@router.delete("/admin/bots/{bot_id}")
async def delete_admin_bot(request: Request, bot_id: str, payload: AdminBotDecommissionPayload):
    workspace_mode = _resolve_workspace_mode(payload.workspace)
    _enforce_worker_scope(request, bot_id, workspace_mode=workspace_mode)
    try:
        current_user = require_admin_access(request)
        worker_pool = store.live_workers if workspace_mode == "live" else store.workers
        worker = next((item for item in worker_pool if item.id == bot_id), None)
        if worker is None:
            raise KeyError(bot_id)
        connection_profile: dict[str, object] | None = None
        try:
            connection_profile = store.get_worker_connection_profile(bot_id, workspace_mode=workspace_mode)
        except (KeyError, ValueError):
            connection_profile = None
        has_saved_credential = bool(
            connection_profile
            and (
                str(connection_profile.get("password") or "").strip()
                or str(connection_profile.get("ssh_private_key") or "").strip()
            )
        )
        if connection_profile and has_saved_credential:
            resolved_auth_mode = str(connection_profile.get("auth_mode") or payload.auth_mode or "password").strip().lower() or "password"
            decommission_request = build_worker_decommission_request(
                vps_ip=str(connection_profile.get("vps_ip") or "").strip(),
                ssh_user=str(connection_profile.get("ssh_user") or payload.ssh_user or "root").strip() or "root",
                password=(str(connection_profile.get("password") or "").strip() or None) if resolved_auth_mode != "ssh_key" else None,
                ssh_private_key=(str(connection_profile.get("ssh_private_key") or "").strip() or None) if resolved_auth_mode == "ssh_key" else None,
                runtime_mode=workspace_mode,
            )
            task = start_worker_decommission_operation(
                store=store,
                worker_id=bot_id,
                request=decommission_request,
                ssh_user=str(connection_profile.get("ssh_user") or payload.ssh_user or "root").strip() or "root",
                workspace_mode=workspace_mode,
                requested_by=current_user.username,
                requested_role=current_user.role,
            )
            return {
                "ok": True,
                "worker_id": bot_id,
                "deleted": False,
                "operation_id": task["id"],
                "mode": "ssh",
                "message": f"Đã bắt đầu gỡ {'BOT live' if workspace_mode == 'live' else 'BOT'} bằng credential đã lưu.",
            }
        if str(worker.status or "").strip() == "offline":
            if workspace_mode == "live":
                store.delete_live_bot_without_ssh(bot_id, deleted_by=current_user.username)
            else:
                store.delete_bot_without_ssh(bot_id, deleted_by=current_user.username)
            return {
                "ok": True,
                "worker_id": bot_id,
                "deleted": True,
                "mode": "local",
                "message": (
                    f"{'BOT live' if workspace_mode == 'live' else 'BOT'} đang offline và không có credential đã lưu, "
                    "nên hệ thống chỉ xóa BOT khỏi control-plane."
                ),
            }
        raise WorkerBootstrapError(
            f"{'BOT live' if workspace_mode == 'live' else 'BOT'} này chưa có credential đã lưu để gỡ tự động. "
            "Hãy gỡ thủ công trên VPS hoặc thêm lại BOT rồi xóa lại."
        )
    except (KeyError, ValueError, WorkerBootstrapError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/bots/of-user/{user_id}")
async def get_admin_bots_of_user(request: Request, user_id: str, workspace: str = "upload"):
    _enforce_user_scope(request, user_id)
    current_user = require_admin_access(request)
    workspace_mode = _resolve_workspace_mode(workspace)
    return store.get_admin_bots_of_user_context(
        user_id=user_id,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
        workspace_mode=workspace_mode,
    )


@router.get("/admin/bots/{bot_id}/users")
async def get_admin_users_of_bot(request: Request, bot_id: str, workspace: str = "upload"):
    workspace_mode = _resolve_workspace_mode(workspace)
    _enforce_worker_scope(request, bot_id, workspace_mode=workspace_mode)
    current_user = require_admin_access(request)
    return store.get_admin_users_of_bot_context(
        worker_id=bot_id,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
        workspace_mode=workspace_mode,
    )


@router.get("/admin/channels")
async def get_admin_channels(
    request: Request,
    manager_ids: list[str] | None = Query(default=None),
    user_id: str | None = None,
    bot_id: str | None = None,
    workspace: str = "upload",
):
    _assert_upload_workspace(workspace)
    selected_manager_ids = _manager_ids_from_request(request, manager_ids)
    current_user = require_admin_access(request)
    workspace_mode = _resolve_workspace_mode(workspace)
    if user_id:
        _enforce_user_scope(request, user_id)
    if bot_id:
        _enforce_worker_scope(request, bot_id, workspace_mode=workspace_mode)
    return store.get_admin_channel_index_context(
        manager_ids=selected_manager_ids,
        user_id=user_id,
        bot_id=bot_id,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
        workspace_mode=workspace_mode,
    )


@router.get("/admin/channels/of-user/{user_id}")
async def get_admin_channels_of_user(request: Request, user_id: str, workspace: str = "upload"):
    _assert_upload_workspace(workspace)
    _enforce_user_scope(request, user_id)
    current_user = require_admin_access(request)
    return store.get_admin_channels_of_user_context(
        user_id=user_id,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    )


@router.get("/admin/channels/of-bot/{bot_id}")
async def get_admin_channels_of_bot(request: Request, bot_id: str, workspace: str = "upload"):
    _assert_upload_workspace(workspace)
    current_user = require_admin_access(request)
    _enforce_worker_scope(request, bot_id)
    return store.get_admin_channel_index_context(
        bot_id=bot_id,
        manager_ids=[current_user.id] if current_user.role == "manager" else None,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    )


@router.get("/admin/channels/{channel_id}/users")
async def get_admin_channel_users(request: Request, channel_id: str, workspace: str = "upload"):
    _assert_upload_workspace(workspace)
    current_user = require_admin_access(request)
    _enforce_channel_scope(request, channel_id)
    return store.get_admin_users_of_channel_context(
        channel_id=channel_id,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    )


@router.post("/admin/channels/{channel_id}/users")
async def update_admin_channel_user(request: Request, channel_id: str, payload: dict[str, str]):
    _assert_upload_workspace(payload.get("workspace") or request.query_params.get("workspace"))
    user_id = str(payload.get("user_id") or "").strip()
    if not user_id:
        raise HTTPException(status_code=422, detail="user_id là bắt buộc.")
    _enforce_channel_scope(request, channel_id)
    _enforce_user_scope(request, user_id)
    action = store.update_user_channel(user_id, channel_id)
    return {"ok": True, "action": action}


@router.patch("/admin/channels/{channel_id}/profile")
async def update_admin_channel_profile(request: Request, channel_id: str, workspace: str = "upload"):
    _assert_upload_workspace(workspace)
    _enforce_channel_scope(request, channel_id)
    store.update_channel_profile(channel_id)
    return {"ok": True}


@router.delete("/admin/channels/{channel_id}")
async def delete_admin_channel(request: Request, channel_id: str, workspace: str = "upload"):
    _assert_upload_workspace(workspace)
    _enforce_channel_scope(request, channel_id)
    store.delete_channel(channel_id)
    return {"ok": True}


@router.get("/admin/renders")
async def get_admin_renders(
    request: Request,
    manager_ids: list[str] | None = Query(default=None),
    workspace: str = "upload",
):
    selected_manager_ids = _manager_ids_from_request(request, manager_ids)
    current_user = require_admin_access(request)
    workspace_mode = _resolve_workspace_mode(workspace)
    return store.get_admin_render_index_context(
        manager_ids=selected_manager_ids,
        workspace_mode=workspace_mode,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    )


@router.get("/admin/jobs")
async def get_admin_jobs_legacy(
    request: Request,
    manager_ids: list[str] | None = Query(default=None),
    workspace: str = "upload",
):
    return await get_admin_renders(request, manager_ids, workspace)


@router.get("/admin/renders/of-channel/{channel_id}")
async def get_admin_renders_of_channel(
    request: Request,
    channel_id: str,
    manager_ids: list[str] | None = Query(default=None),
    workspace: str = "upload",
):
    _assert_upload_workspace(workspace)
    selected_manager_ids = _manager_ids_from_request(request, manager_ids)
    current_user = require_admin_access(request)
    _enforce_channel_scope(request, channel_id)
    return store.get_admin_render_index_context(
        manager_ids=selected_manager_ids,
        channel_id=channel_id,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    )


@router.get("/admin/renders/{job_id}")
async def get_admin_render_detail(request: Request, job_id: str):
    current_user = require_admin_access(request)
    _enforce_job_scope(request, job_id)
    return store.get_admin_render_detail_context(
        job_id=job_id,
        viewer_role=current_user.role,
        viewer_id=current_user.id,
    )


@router.delete("/admin/renders/{job_id}")
async def delete_admin_render_job(request: Request, job_id: str):
    _enforce_job_scope(request, job_id)
    store.delete_job(job_id)
    return {"ok": True}


@router.delete("/admin/renders")
async def delete_admin_renders(request: Request, channel_id: str | None = None):
    current_user = require_admin_access(request)
    if channel_id:
        _enforce_channel_scope(request, channel_id)
    scoped_job_ids = _scoped_job_ids(request, channel_id=channel_id)
    if current_user.role == "admin" and not channel_id:
        store.delete_all_jobs(deleted_by=current_user.username)
    else:
        store.delete_jobs(scoped_job_ids)
    return {"ok": True}
