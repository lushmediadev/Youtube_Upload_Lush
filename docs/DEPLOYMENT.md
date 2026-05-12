# Deployment Flow

## Source Of Truth
- `origin/main` la source of truth duy nhat cho code chay tren local va VPS.
- Khong sua source truc tiep tren VPS neu khong phai hotfix khan cap. Neu co hotfix tren VPS, phai backport len git ngay sau do.

## Current Production Target
- Control-plane hien hanh: `109.123.227.253` (`vmi3263494`), public app `ytb.lushmedia.net`.
- Repo checkout tren host: `/opt/youtube-upload-lush`.
- Runtime dir tren host: `/opt/youtube-upload-lush-runtime`.
- Runtime env tren host phai giu:
  - `WORKER_BOOTSTRAP_CONTROL_PLANE_URL=http://109.123.227.253:8000`
  - `WORKER_BOOTSTRAP_REPO_URL=https://github.com/lushmediadev/Youtube_Upload_Lush.git`
  - `WORKER_BOOTSTRAP_BRANCH=main`
- Khong deploy repo nay len `82.197.71.6`. Host `82.197.71.6` chi xuat hien trong log lich su cu va khong phai target cua `ytb.lushmedia.net`.

## Runtime Layout Tren VPS
- Repo checkout: `/opt/youtube-upload-lush`
- Runtime control-plane: `/opt/youtube-upload-lush-runtime`
  - `.env`
  - `.venv`
  - `.backup`
  - `backend-data`
- Runtime worker: `/opt/youtube-upload-lush-runtime`
  - `.venv`
  - `.backup`
  - `worker-data`

Trong repo checkout, cac duong dan runtime duoc mount lai bang symlink:
- `/opt/youtube-upload-lush/.env -> /opt/youtube-upload-lush-runtime/.env`
- `/opt/youtube-upload-lush/.venv -> /opt/youtube-upload-lush-runtime/.venv`
- `/opt/youtube-upload-lush/.backup -> /opt/youtube-upload-lush-runtime/.backup`
- `/opt/youtube-upload-lush/backend/data -> /opt/youtube-upload-lush-runtime/backend-data`
- `/opt/youtube-upload-lush/worker-data -> /opt/youtube-upload-lush-runtime/worker-data`

## Bootstrap / Update

### Control-plane
```bash
APP_DIR=/opt/youtube-upload-lush \
RUNTIME_DIR=/opt/youtube-upload-lush-runtime \
REPO_URL=https://github.com/lushmediadev/Youtube_Upload_Lush.git \
BRANCH=main \
SYSTEMD_SERVICE_NAME=youtube-upload-web.service \
SERVICE_PORT=8000 \
bash /opt/youtube-upload-lush/scripts/bootstrap_host.sh
```

Preview control-plane `/opt/youtube-upload-lush-live*` da duoc retire tren host production tu `2026-04-13`; live workspace hien chay chung trong env chinh `/opt/youtube-upload-lush` qua `/app/live`.

### Worker
```bash
APP_DIR=/opt/youtube-upload-lush \
RUNTIME_DIR=/opt/youtube-upload-lush-runtime \
REPO_URL=https://github.com/lushmediadev/Youtube_Upload_Lush.git \
BRANCH=main \
bash /opt/youtube-upload-lush/scripts/bootstrap_worker.sh
```

## Local -> GitHub -> VPS
1. Tren may local: `git pull`, sua code, test, `git commit`, `git push origin main`.
2. Tren control-plane: chay `bootstrap_host.sh` de `fetch/reset` ve branch dich, giu nguyen runtime, render lai unit systemd theo env deploy, va restart dung service dich.
3. Tren tung worker: chay `bootstrap_worker.sh` de `fetch/reset` ve `origin/main`, giu nguyen runtime va restart service.

## VPS Migration

Dung `scripts/migrate_control_plane_vps.sh` de chuyen control-plane sang VPS moi theo kieu:

- cai moi code tu GitHub
- copy runtime `.env`
- copy `backend-data`
- render/start lai `youtube-upload-web.service`

Runbook chi tiet nam o `docs/VPS_MIGRATION.md`. Script nay khong tu dong be toan bo BOT worker sang VPS moi; BOT worker nen duoc them/bootstrap lai sau khi control-plane moi da verify.

## Safety Rules
- Khong commit `.env`, `.venv`, `backend/data`, `worker-data`, `.backup`.
- Truoc khi migrate layout, dam bao job dang chay da ve `completed` hoac `idle`.
- Moi lan doi deploy flow, cap nhat `docs/PROJECT_CONTEXT.md`, `docs/DECISIONS.md`, `docs/WORKLOG.md`, `docs/CHANGELOG.md`.
