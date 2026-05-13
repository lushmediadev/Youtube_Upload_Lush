from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from ..auth import require_app_access
from ..schemas import JobCreatePayload, UploadSessionCreateRequest
from ..store import store

router = APIRouter(tags=["user"])


def _current_app_user_id(request: Request) -> str:
    return require_app_access(request, "user").id


def _clean_optional(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    return cleaned or None


def _parse_schedule_time(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None

    cleaned = value.strip()
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    raise HTTPException(status_code=422, detail="schedule_time khong dung dinh dang ho tro.")


def _is_supported_google_drive_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    if host not in {"drive.google.com", "docs.google.com"}:
        return False
    query_id = parse_qs(parsed.query).get("id", [])
    if query_id and query_id[0]:
        return True
    return bool(re.search(r"/file/d/([^/]+)", parsed.path))


@router.get("/user/bootstrap")
async def get_user_bootstrap(request: Request):
    return store.get_user_bootstrap(_current_app_user_id(request))


@router.get("/user/dashboard/live")
async def get_user_dashboard_live(request: Request):
    return store.get_user_dashboard_live_payload(_current_app_user_id(request))


@router.get("/user/jobs")
async def get_user_jobs(request: Request):
    return store.get_user_jobs(_current_app_user_id(request))


@router.get("/user/jobs/{job_id}/preview/{slot}")
async def get_user_job_preview(request: Request, job_id: str, slot: str):
    try:
        payload = store.get_user_job_asset_file(
            user_id=_current_app_user_id(request),
            job_id=job_id,
            slot=slot,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay asset preview.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="File preview khong con ton tai.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return FileResponse(
        path=payload["path"],
        media_type=payload["content_type"],
        filename=payload["file_name"],
    )


@router.get("/user/jobs/{job_id}/preview-thumbnail")
async def get_user_job_preview_thumbnail(request: Request, job_id: str):
    try:
        payload = store.get_user_job_preview_thumbnail_file(
            user_id=_current_app_user_id(request),
            job_id=job_id,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay job preview.") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Preview image khong con ton tai.") from exc

    return FileResponse(
        path=payload["path"],
        media_type=payload["content_type"],
        filename=payload["file_name"],
    )


@router.post("/user/oauth/connect/start")
async def start_user_oauth(request: Request):
    _current_app_user_id(request)
    raise HTTPException(
        status_code=410,
        detail="Luong OAuth da duoc tat. Hay dung '+ Them Kenh' de dang nhap bang Ubuntu Browser.",
    )


@router.post("/user/browser-sessions")
async def create_user_browser_session(request: Request):
    try:
        return store.create_browser_session(_current_app_user_id(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/user/channels/{channel_id}/studio-session")
async def create_user_channel_studio_session(request: Request, channel_id: str):
    try:
        return store.create_channel_studio_session(_current_app_user_id(request), channel_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy kênh.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/user/browser-sessions/{session_id}")
async def get_user_browser_session(request: Request, session_id: str):
    try:
        return store.get_browser_session(_current_app_user_id(request), session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay browser session.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/user/browser-sessions/{session_id}/confirm")
async def confirm_user_browser_session(request: Request, session_id: str):
    try:
        return store.confirm_browser_session(_current_app_user_id(request), session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay browser session.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/user/browser-sessions/{session_id}")
async def close_user_browser_session(request: Request, session_id: str):
    try:
        return store.close_browser_session(_current_app_user_id(request), session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay browser session.") from exc


@router.post("/user/uploads/sessions")
async def create_upload_session(request: Request, payload: UploadSessionCreateRequest = Body(...)):
    try:
        return store.create_upload_session(_current_app_user_id(request), payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/user/uploads/sessions/{session_id}")
async def get_upload_session(request: Request, session_id: str):
    try:
        return store.get_upload_session(_current_app_user_id(request), session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay upload session.") from exc


@router.patch("/user/uploads/sessions/{session_id}")
async def append_upload_session_chunk(request: Request, session_id: str):
    offset_header = request.headers.get("x-upload-offset", "0")
    try:
        offset = int(offset_header)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="x-upload-offset khong hop le.") from exc
    body = await request.body()
    try:
        return store.append_upload_chunk(_current_app_user_id(request), session_id, offset, body)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay upload session.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/user/uploads/sessions/{session_id}")
async def delete_upload_session(request: Request, session_id: str):
    try:
        store.abort_upload_session(_current_app_user_id(request), session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay upload session.") from exc
    return {"status": "deleted", "session_id": session_id}


@router.post("/user/jobs")
async def create_user_job(
    request: Request,
    channel_id: str = Form(...),
    title: str = Form(...),
    description: str | None = Form(default=None),
    source_mode: str | None = Form(default=None),
    time_render_string: str = Form(default="00:30:00"),
    schedule_time: str | None = Form(default=None),
    intro_url: str | None = Form(default=None),
    video_loop_url: str | None = Form(default=None),
    audio_loop_url: str | None = Form(default=None),
    outro_url: str | None = Form(default=None),
    intro_asset_id: str | None = Form(default=None),
    video_loop_asset_id: str | None = Form(default=None),
    audio_loop_asset_id: str | None = Form(default=None),
    outro_asset_id: str | None = Form(default=None),
    intro_file: UploadFile | None = File(default=None),
    video_loop_file: UploadFile | None = File(default=None),
    audio_loop_file: UploadFile | None = File(default=None),
    outro_file: UploadFile | None = File(default=None),
):
    uploads = {
        "intro": intro_file,
        "video_loop": video_loop_file,
        "audio_loop": audio_loop_file,
        "outro": outro_file,
    }
    saved_files: dict[str, str | None] = {}

    for slot, file in uploads.items():
        if not file or not (getattr(file, "filename", None) or "").strip():
            saved_files[slot] = None
            continue
        safe_name = store._sanitize_filename(file.filename)
        target_path = store.upload_asset_dir / f"asset-{uuid4().hex[:12]}-{safe_name}"
        with target_path.open("wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                buffer.write(chunk)
        saved_files[slot] = Path(target_path).name

    uploaded_asset_ids = {
        "intro": _clean_optional(intro_asset_id),
        "video_loop": _clean_optional(video_loop_asset_id),
        "audio_loop": _clean_optional(audio_loop_asset_id),
        "outro": _clean_optional(outro_asset_id),
    }
    derived_source_mode = (
        "local"
        if any(file and file.filename for file in uploads.values()) or any(uploaded_asset_ids.values())
        else "drive"
    )
    resolved_source_mode = source_mode or derived_source_mode
    parsed_schedule_time = _parse_schedule_time(schedule_time)

    if not channel_id.strip():
        raise HTTPException(status_code=422, detail="channel_id la bat buoc.")
    if not (video_loop_url and video_loop_url.strip()) and not saved_files.get("video_loop") and not uploaded_asset_ids.get("video_loop"):
        raise HTTPException(status_code=422, detail="Can nhap video loop hoac upload file video loop.")
    for label, url_value, saved_file, uploaded_asset_id in (
        ("Link video Intro", intro_url, saved_files.get("intro"), uploaded_asset_ids.get("intro")),
        ("Link video loop", video_loop_url, saved_files.get("video_loop"), uploaded_asset_ids.get("video_loop")),
        ("Link audio loop", audio_loop_url, saved_files.get("audio_loop"), uploaded_asset_ids.get("audio_loop")),
        ("Link Outro", outro_url, saved_files.get("outro"), uploaded_asset_ids.get("outro")),
    ):
        cleaned_url = _clean_optional(url_value)
        if not cleaned_url or saved_file or uploaded_asset_id:
            continue
        if not _is_supported_google_drive_url(cleaned_url):
            raise HTTPException(status_code=422, detail=f"{label} chi nhan link Google Drive hop le.")

    payload = JobCreatePayload(
        channel_id=channel_id,
        title=title,
        description=description,
        source_mode=resolved_source_mode,  # type: ignore[arg-type]
        intro_url=_clean_optional(intro_url),
        video_loop_url=_clean_optional(video_loop_url),
        audio_loop_url=_clean_optional(audio_loop_url),
        outro_url=_clean_optional(outro_url),
        intro_asset_id=uploaded_asset_ids["intro"],
        video_loop_asset_id=uploaded_asset_ids["video_loop"],
        audio_loop_asset_id=uploaded_asset_ids["audio_loop"],
        outro_asset_id=uploaded_asset_ids["outro"],
        time_render_string=time_render_string,
        schedule_time=parsed_schedule_time,
    )
    try:
        return store.create_job(_current_app_user_id(request), payload, saved_files)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.patch("/user/jobs/{job_id}/cancel")
async def cancel_user_job(request: Request, job_id: str):
    try:
        return store.cancel_job(job_id, user_id=_current_app_user_id(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay job.") from exc


@router.delete("/user/jobs/{job_id}")
async def delete_user_job(request: Request, job_id: str):
    try:
        store.delete_job(job_id, user_id=_current_app_user_id(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay job.") from exc
    return {"status": "deleted", "job_id": job_id}


@router.post("/user/jobs/actions/bulk-delete")
async def delete_user_jobs_bulk(request: Request, payload: dict = Body(...)):
    job_ids = payload.get("job_ids")
    if not isinstance(job_ids, list):
        raise HTTPException(status_code=422, detail="job_ids phai la danh sach.")

    deleted_ids = store.delete_jobs(job_ids, user_id=_current_app_user_id(request))
    return {
        "status": "deleted",
        "deleted_count": len(deleted_ids),
        "job_ids": deleted_ids,
    }


@router.delete("/user/channels/{channel_id}")
async def delete_user_channel(request: Request, channel_id: str):
    try:
        store.delete_user_connected_channel(channel_id, user_id=_current_app_user_id(request))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Khong tim thay kenh.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"status": "deleted", "channel_id": channel_id}
