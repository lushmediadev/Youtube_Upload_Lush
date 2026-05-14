# Worker Control Plane

## Responsibility
- Chua worker runtime tren VPS: register/heartbeat/claim/progress, browser session ownership, render/upload flow, cleanup profile.

## Entry Points
- Main loop: `workers/agent/main.py`
- Control plane API client: `workers/agent/control_plane.py`
- Browser session runtime: `workers/agent/browser_runtime.py`, `workers/agent/browser_sessions.py`
- Upload/render: `workers/agent/browser_uploader.py`, `workers/agent/job_runner.py`, `workers/agent/ffmpeg_pipeline.py`
- Live local supervisor: `workers/agent/live_supervisor.py`

## Key Files
- `workers/agent/main.py`
- `workers/agent/control_plane.py`
- `workers/agent/browser_runtime.py`
- `workers/agent/browser_sessions.py`
- `workers/agent/browser_uploader.py`
- `workers/agent/job_runner.py`
- `workers/agent/ffmpeg_pipeline.py`
- `workers/agent/live_runner.py`
- `workers/agent/live_supervisor.py`

## Depends On
- Control-plane APIs trong `backend/app/api_worker.py`
- Runtime env tren tung VPS
- Chromium/noVNC/X stack
- FFmpeg va media dependencies

## Used By
- Tung worker VPS duoc cap cho user/job

## Invariants
- Worker API loop co jitter/backoff de tranh nhieu worker cung heartbeat/claim dung mot nhip. Live worker dang ban chi probe `claim_live_stream` day hon khi vua thay khong co job moi, nhung status/progress/failover event van day len control-plane ngay khi co thay doi.
- Worker la outbound-only; control plane khong push stateful browser runtime vao chinh no.
- Live worker la local supervisor cho ffmpeg/live runtime; control-plane miss heartbeat/lease ban dau chi la telemetry stale, khong phai bang chung runtime da chet.
- Neu primary live co backup va telemetry stale qua `LIVE_TELEMETRY_FAILOVER_SECONDS` (mac dinh 180s), control-plane moi release primary claim de backup takeover va gui ops Telegram.
- Stream live co marker `Mất telemetry` chi duoc self-reclaim boi dung worker dang giu claim; worker khac khong duoc dung marker nay de cuop runtime.
- Browser session va upload browser phai bam theo worker/VPS so huu.
- Cleanup profile/channel stale phai do worker thuc hien tren may cua no.

## Known Pitfalls
- Browser uploader de treo hoac bao sai progress neu chi dua vao dialog footer; can doi chieu draft/background verification.
- Profile stale, Google Sign in redirect, verification challenge co the lam nham la bug upload.
- Runtime deploy drift giua local/GitHub/VPS tung xay ra; worker source can doi chieu production truoc khi sua cac bug kho.
- Live incident can doi chieu local `worker-data/live-state/<stream_id>/current.json`, `events.log`, va `ffmpeg.log` tren worker truoc khi ket luan RTMP hay worker runtime hong.
- `worker-data/live-state/*` la log/state local dai hon runtime `live-streams/*`; janitor xoa theo `WORKER_LIVE_STATE_RETENTION_HOURS` (mac dinh 168h) de tranh phinh disk.

## Related Decisions
- `DEC-001`
- `DEC-003`
- `DEC-004`
- `DEC-005`
- `DEC-052`
- `DEC-053`
