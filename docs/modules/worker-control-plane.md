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
- Worker la outbound-only; control plane khong push stateful browser runtime vao chinh no.
- Live worker la local supervisor cho ffmpeg/live runtime; control-plane miss heartbeat/lease chi la telemetry stale, khong phai bang chung runtime da chet.
- Browser session va upload browser phai bam theo worker/VPS so huu.
- Cleanup profile/channel stale phai do worker thuc hien tren may cua no.

## Known Pitfalls
- Browser uploader de treo hoac bao sai progress neu chi dua vao dialog footer; can doi chieu draft/background verification.
- Profile stale, Google Sign in redirect, verification challenge co the lam nham la bug upload.
- Runtime deploy drift giua local/GitHub/VPS tung xay ra; worker source can doi chieu production truoc khi sua cac bug kho.
- Live incident can doi chieu local `worker-data/live-state/<stream_id>/current.json`, `events.log`, va `ffmpeg.log` tren worker truoc khi ket luan RTMP hay worker runtime hong.

## Related Decisions
- `DEC-001`
- `DEC-003`
- `DEC-004`
- `DEC-005`
- `DEC-052`
