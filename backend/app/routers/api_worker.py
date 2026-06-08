from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse

from ..schemas import (
    LiveWorkerAuthPayload,
    LiveWorkerCompletePayload,
    LiveWorkerFailPayload,
    LiveWorkerHeartbeatPayload,
    LiveWorkerProgressPayload,
    LiveWorkerRegisterPayload,
    WorkerBrowserProfileCleanupAckPayload,
    WorkerDecommissionCompletePayload,
    WorkerBrowserSessionSyncPayload,
    WorkerAuthPayload,
    WorkerHeartbeatPayload,
    WorkerJobCompletePayload,
    WorkerJobFailPayload,
    WorkerJobProgressPayload,
    WorkerRegisterPayload,
)
from ..store import store

router = APIRouter(tags=["worker"])


@router.post("/workers/register")
async def register_worker(payload: WorkerRegisterPayload):
    try:
        return store.register_worker(payload)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/workers/heartbeat")
async def heartbeat_worker(payload: WorkerHeartbeatPayload):
    try:
        return store.heartbeat_worker(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Worker chưa được đăng ký.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/workers/claim")
async def claim_worker_job(payload: WorkerAuthPayload):
    try:
        worker, job = store.claim_next_job(payload.worker_id, payload.shared_secret)
        return {"ok": True, "worker": worker.model_dump(mode="json"), "job": job.model_dump(mode="json") if job else None}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Worker chưa được đăng ký.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/workers/browser-sessions/poll")
async def poll_worker_browser_sessions(payload: WorkerAuthPayload):
    try:
        sessions = store.get_worker_browser_sessions(payload.worker_id, payload.shared_secret)
        cleanup_profiles = store.get_worker_browser_profile_cleanup_tasks(payload.worker_id, payload.shared_secret)
        return {
            "ok": True,
            "sessions": [session.model_dump(mode="json") for session in sessions],
            "cleanup_profiles": cleanup_profiles,
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Worker chưa được đăng ký.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/live-workers/register")
async def register_live_worker(payload: LiveWorkerRegisterPayload):
    try:
        return store.register_live_worker(payload)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/live-workers/heartbeat")
async def heartbeat_live_worker(payload: LiveWorkerHeartbeatPayload):
    try:
        return store.heartbeat_live_worker(payload)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Live worker chưa được đăng ký.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/live-workers/claim")
async def claim_live_worker_stream(payload: LiveWorkerAuthPayload):
    try:
        worker, stream = store.claim_next_live_stream(payload.worker_id, payload.shared_secret)
        return {"ok": True, "worker": worker.model_dump(mode="json"), "stream": stream.model_dump(mode="json") if stream else None}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Live worker chưa được đăng ký.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/workers/browser-profiles/cleanup-ack")
async def ack_worker_browser_profile_cleanup(payload: WorkerBrowserProfileCleanupAckPayload):
    try:
        cleared = store.ack_worker_browser_profile_cleanup_tasks(
            worker_id=payload.worker_id,
            shared_secret=payload.shared_secret,
            profile_keys=payload.profile_keys,
        )
        return {"ok": True, "cleared": cleared}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Worker chưa được đăng ký.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/workers/decommission/poll")
async def poll_worker_decommission(payload: WorkerAuthPayload):
    try:
        task = store.get_worker_decommission_task(payload.worker_id, payload.shared_secret)
        return {"ok": True, "task": task}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Worker chưa được đăng ký.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/workers/decommission/{operation_id}/complete")
async def complete_worker_decommission(operation_id: str, payload: WorkerDecommissionCompletePayload):
    try:
        store.complete_worker_decommission_task(
            operation_id=operation_id,
            worker_id=payload.worker_id,
            shared_secret=payload.shared_secret,
            status=payload.status,
            message=payload.message,
        )
        return {"ok": True}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Task gỡ BOT không tồn tại.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/workers/browser-sessions/{session_id}/sync")
async def sync_worker_browser_session(session_id: str, payload: WorkerBrowserSessionSyncPayload):
    try:
        session = store.sync_worker_browser_session(session_id, payload)
        return {"ok": True, "session": session.model_dump(mode="json")}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy browser session.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workers/jobs/{job_id}/progress")
async def update_worker_job_progress(job_id: str, payload: WorkerJobProgressPayload):
    try:
        return store.update_worker_job_progress(
            job_id=job_id,
            worker_id=payload.worker_id,
            shared_secret=payload.shared_secret,
            status=payload.status,
            progress=payload.progress,
            message=payload.message,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workers/jobs/{job_id}/complete")
async def complete_worker_job(job_id: str, payload: WorkerJobCompletePayload):
    try:
        return store.complete_worker_job(
            job_id=job_id,
            worker_id=payload.worker_id,
            shared_secret=payload.shared_secret,
            output_url=payload.output_url,
            message=payload.message,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workers/jobs/{job_id}/fail")
async def fail_worker_job(job_id: str, payload: WorkerJobFailPayload):
    try:
        return store.fail_worker_job(
            job_id=job_id,
            worker_id=payload.worker_id,
            shared_secret=payload.shared_secret,
            message=payload.message,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/live-workers/streams/{stream_id}/progress")
async def update_live_stream_progress(stream_id: str, payload: LiveWorkerProgressPayload):
    try:
        return store.update_live_stream_progress(
            stream_id=stream_id,
            worker_id=payload.worker_id,
            shared_secret=payload.shared_secret,
            status=payload.status,
            progress=payload.progress,
            message=payload.message,
            runtime_health=payload.runtime_health,
            runtime_health_elapsed_seconds=payload.runtime_health_elapsed_seconds,
            runtime_health_message=payload.runtime_health_message,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy luồng live.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/live-workers/streams/{stream_id}")
async def get_live_stream_runtime_state(
    stream_id: str,
    x_worker_id: str = Header(...),
    x_worker_secret: str = Header(...),
):
    try:
        return store.get_live_stream_runtime_state(
            stream_id=stream_id,
            worker_id=x_worker_id,
            shared_secret=x_worker_secret,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy luồng live.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/live-workers/streams/{stream_id}/complete")
async def complete_live_stream(stream_id: str, payload: LiveWorkerCompletePayload):
    try:
        return store.complete_live_stream_runtime(
            stream_id=stream_id,
            worker_id=payload.worker_id,
            shared_secret=payload.shared_secret,
            message=payload.message,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy luồng live.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/live-workers/streams/{stream_id}/fail")
async def fail_live_stream(stream_id: str, payload: LiveWorkerFailPayload):
    try:
        return store.fail_live_stream_runtime(
            stream_id=stream_id,
            worker_id=payload.worker_id,
            shared_secret=payload.shared_secret,
            message=payload.message,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy luồng live.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/workers/jobs/{job_id}/assets/{slot}")
async def download_worker_job_asset(
    job_id: str,
    slot: str,
    x_worker_id: str = Header(...),
    x_worker_secret: str = Header(...),
):
    try:
        asset_file = store.get_worker_job_asset_file(
            job_id=job_id,
            slot=slot,
            worker_id=x_worker_id,
            shared_secret=x_worker_secret,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy file asset.") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job hoặc slot asset.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return FileResponse(path=asset_file["path"], filename=asset_file["file_name"])


@router.get("/workers/jobs/{job_id}/youtube-target")
async def get_worker_job_youtube_target(
    job_id: str,
    x_worker_id: str = Header(...),
    x_worker_secret: str = Header(...),
):
    try:
        payload = store.get_worker_job_youtube_target(
            job_id=job_id,
            worker_id=x_worker_id,
            shared_secret=x_worker_secret,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job hoặc channel.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return payload.model_dump(mode="json")


@router.post("/workers/jobs/{job_id}/thumbnail")
async def upload_worker_job_thumbnail(
    job_id: str,
    request: Request,
    x_worker_id: str = Header(...),
    x_worker_secret: str = Header(...),
    x_file_name: str = Header(default="preview.jpg"),
    content_type: str | None = Header(default=None),
):
    payload = await request.body()
    try:
        return store.store_worker_job_preview_thumbnail(
            job_id=job_id,
            worker_id=x_worker_id,
            shared_secret=x_worker_secret,
            file_name=x_file_name,
            content_type=content_type,
            payload=payload,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Không tìm thấy job.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/live-workers/streams/{stream_id}/thumbnail")
async def upload_worker_live_thumbnail(
    stream_id: str,
    request: Request,
    x_worker_id: str = Header(...),
    x_worker_secret: str = Header(...),
    x_file_name: str = Header(default="preview.jpg"),
    content_type: str | None = Header(default=None),
):
    payload = await request.body()
    try:
        return store.store_worker_live_preview_thumbnail(
            stream_id=stream_id,
            worker_id=x_worker_id,
            shared_secret=x_worker_secret,
            file_name=x_file_name,
            content_type=content_type,
            payload=payload,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="KhÃ´ng tÃ¬m tháº¥y luá»“ng live.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
