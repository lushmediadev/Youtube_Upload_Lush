# Project Brief

## Purpose
- Rebuild va van hanh lai YouTube upload tool theo huong `FastAPI control plane + Python workers`.
- Giu workflow user/admin hien tai, nhung giam drift giua code, docs va deploy.
- Giam token bootstrap cho repo lon bang memory ngan + retrieval layer.

## System Shape
- `backend/app/`: control plane FastAPI, HTML/Jinja UI, API cho user/admin/worker, auth, store/schema.
- `workers/agent/`: worker outbound chay tren VPS, heartbeat/claim/progress, browser session, render/upload pipeline.
- `infra/`: Docker/Caddy/systemd/bootstrap/deploy packaging.
- `docs/`: project memory. `PROJECT_CONTEXT/DECISIONS/WORKLOG/CHANGELOG` giu lai vi tuong thich nguoc; `PROJECT_BRIEF/MEMORY_INDEX/DECISIONS_INDEX` la lop bootstrap moi.

## Main Modules
- `backend/app`: source of truth cho web app va control-plane API.
- `backend/app/templates` + `backend/app/static`: UI user/admin server-rendered.
- `workers/agent`: worker runtime va browser/upload/render flow.
- `infra/docker`, `infra/caddy`, `infra/systemd`: runtime packaging va deploy surface.

## Global Invariants
- Production control-plane hien hanh cua repo nay la `109.123.227.253` (`vmi3263494`, public app `ytb.lushmedia.net`). `82.197.71.6` la host lich su/khong con la deploy target cho repo nay.
- Visual source of truth cho UI la `final_user_ui.html`; `docs/UI_SYSTEM.md` mo ta design system hien tai.
- Worker la outbound-only agent: `register + heartbeat + claim + progress + complete/fail`.
- Browser session/onboarding va upload browser phai chay tren worker/VPS duoc gan, khong chay tren control plane.
- User co the duoc gan nhieu VPS; channel/browser session/render job phai bam theo worker so huu cua no.
- Local upload file lon phai di qua `upload session + resumable chunk upload` truoc khi tao job.
- `PROJECT_BRIEF + MEMORY_INDEX + DECISIONS_INDEX` la bootstrap mac dinh; `PROJECT_CONTEXT + DECISIONS + WORKLOG` la lop lich su/tuong thich.

## Build / Test / Lint
- Run web local: `python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000`
- Backend syntax: `python -m compileall backend/app`
- Worker syntax: `python -m compileall workers/agent`
- User JS: `node --check backend/app/static/js/user_dashboard.js`
- Admin JS: `node --check backend/app/static/js/admin_tables.js`

## Module Boundaries
- `backend/` khong duoc nuot worker runtime vao route handler; worker contract phai qua API/store/schema ro rang.
- `workers/` khong hardcode secret/host URL, khong phu thuoc browser session local tren may control plane.
- `infra/` chi chua packaging/service config, khong chua business logic.
- `docs/` chi giu tri thuc kho suy ra tu code; khong tro thanh worklog narrative mac dinh cho moi task.

## Safety Constraints
- Khong doi contract worker/API lon neu chua co decision va migration note.
- Khong dua UI ve mot design system khac `final_user_ui.html` neu chua co quyet dinh moi.
- Khi doi chieu issue cu, uu tien doc module docs + decision index truoc, chi mo full WORKLOG/DECISIONS khi can.

## Key References
- `docs/MEMORY_INDEX.md`
- `docs/DECISIONS_INDEX.md`
- `docs/UI_SYSTEM.md`
- `docs/modules/backend-app.md`
- `docs/modules/user-workspace.md`
- `docs/modules/admin-workspace.md`
- `docs/modules/worker-control-plane.md`
- `docs/modules/infra-runtime.md`
