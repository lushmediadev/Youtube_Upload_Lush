## Working agreements

- Khi giao tiep, walkthrough, task/checklist va huong dan trien khai: viet tieng Viet.
- Giu nguyen tieng Anh cho ten ham/bien, log loi, lenh terminal, config key va API field.
- Luon phan loai nhiem vu thanh `Quick Task` hoac `Project Task`.

## Quick Task

- Tra loi truc tiep.
- Khong doc project memory mac dinh.
- Chi doc code/file khi cau hoi phu thuoc truc tiep vao chung.
- Khong bat buoc cap nhat `docs/CHANGELOG.md`, `docs/DECISIONS_INDEX.md`, `docs/modules/*`, `docs/tasks/*`.

## Project Task

- Truoc khi sua code, luon doc:
  - `AGENTS.md`
  - `docs/PROJECT_BRIEF.md`
  - `docs/MEMORY_INDEX.md`
- Chi doc them khi task thuc su can:
  - `docs/modules/<module>.md`
  - `docs/DECISIONS_INDEX.md`
  - `docs/tasks/active/<task-id>.md`
  - `docs/UI_SYSTEM.md` neu task dung UI
  - subfolder `AGENTS.md` gan nhat voi vung code dang sua
- Khong doc mac dinh:
  - full `docs/CHANGELOG.md`
  - full `docs/DECISIONS.md`
  - full `docs/WORKLOG.md`
  - full `docs/archive/*`
- Lop tuong thich nguoc:
  - `docs/PROJECT_CONTEXT.md`, `docs/DECISIONS.md`, `docs/WORKLOG.md` van duoc giu lai.
  - Chi mo cac file nay khi can doi chieu lich su, investigate regression cu, hoac khi `PROJECT_BRIEF/MEMORY_INDEX` chua cover du scope.
- Voi repo lon, neu `cgraph` hoac `code-graph` retrieval kha dung, uu tien dung no de thu hep working set truoc khi mo nhieu file bang tay.

## Project Memory Skill

- Voi moi `Project Task`, uu tien dung skill `project-memory-bootstrap` de tao moi hoac refresh project memory chuan.
- Root `AGENTS.md` chi giu policy, trigger va routing; khong giu template chi tiet hay workflow memory dai.
- Cac format chuan cho:
  - `docs/PROJECT_BRIEF.md`
  - `docs/MEMORY_INDEX.md`
  - `docs/DECISIONS_INDEX.md`
  - `docs/modules/<module>.md`
  - `docs/tasks/active/<task-id>.md`
  phai do skill `project-memory-bootstrap` quan ly khi skill kha dung.
- Neu memory chuan bi thieu hoac stale sau thay doi cau truc lon, refresh bang skill nay truoc khi mo rong root rule.

## Rule Bootstrap

- Voi `Project Task`, truoc khi sua code phai quet nhanh cau truc repo va file config chinh.
- Neu thieu, tao toi thieu:
  - `docs/PROJECT_BRIEF.md`
  - `docs/MEMORY_INDEX.md`
  - `docs/DECISIONS_INDEX.md`
  - `docs/CHANGELOG.md`
- Chi tao `docs/modules/<module>.md` cho module thuc su quan trong hoac dang duoc chinh sua.
- Chi tao `docs/tasks/active/<task-id>.md` khi task co nhieu buoc hoac keo dai qua nhieu luot.
- Khong migrate nguyen xi log/history dai sang memory moi.

## Build / Test / Lint

- Local backend: `python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000`
- Backend syntax check: `python -m compileall backend/app`
- Worker syntax check: `python -m compileall workers/agent`
- User dashboard JS check: `node --check backend/app/static/js/user_dashboard.js`
- Admin tables JS check: `node --check backend/app/static/js/admin_tables.js`
- Frontend hien tai uu tien `server-rendered HTML` qua FastAPI templates; khong mac dinh React/Vite neu chua co ly do ro rang.

## Module Boundaries

- `backend/`: FastAPI, templates, static assets, API routers, auth, store/schema va control-plane logic.
- `workers/`: worker agent Python, heartbeat/claim/progress/browser session/upload.
- `infra/`: Docker, reverse proxy, systemd, bootstrap/deploy artifact; khong dat business logic vao day.
- `docs/`: project memory va migration notes; khong tro thanh ban sao cua code.

## Update Policy

- Sau moi `Project Task`, append 1 entry ngan vao `docs/WORKLOG.md` va `docs/CHANGELOG.md`.
- Khi co quyet dinh moi con hieu luc, cap nhat ca `docs/DECISIONS.md` va `docs/DECISIONS_INDEX.md`.
- Neu context cu xung dot voi workflow moi, uu tien `docs/PROJECT_BRIEF.md` + `docs/MEMORY_INDEX.md`, sau do ghi ro xung dot vao `WORKLOG`.

## UI Design Discipline

- Moi task UI phai doc `docs/UI_SYSTEM.md` truoc.
- `final_user_ui.html` van la visual source of truth cho user/admin tru khi co decision moi.
- Man `Cap phat BOT` la screen exception co chu dich; giu exception trong module docs thay vi nhan rong thanh pattern chung.

## Safety Rules

- Khong doi contract lon cua backend/worker neu chua cap nhat `docs/DECISIONS.md`.
- Production control-plane hien hanh cua repo nay la `109.123.227.253` (`vmi3263494`, `ytb.lushmedia.net`). Khong deploy repo nay len `82.197.71.6`; moi reference toi `82.197.71.6` trong docs/WORKLOG/CHANGELOG/PROJECT_CONTEXT chi la lich su cu/stale.
- Khong dung memory file de thay cho viec doc code lien quan.
- Khong dung `code-graph` hoac graph query lam canonical source thay cho code, config va memory chuan; graph chi la lop retrieval.
- Khong duplicate cung mot thong tin o nhieu file memory neu mot canonical source la du.
- Khong nhot workflow chi tiet, template dai hay lich su dai vao root `AGENTS.md`.
