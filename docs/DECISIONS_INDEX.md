# Decisions Index

| ID | Decision | Status | Scope | Impact |
| --- | --- | --- | --- | --- |
| DEC-001 | Core architecture giu `FastAPI control plane + Python worker`; local bootstrap dung SQLite, data target ve sau la Postgres + Redis | Active | backend + workers | High |
| DEC-002 | `final_user_ui.html` la visual source of truth cho user/admin; `docs/UI_SYSTEM.md` mo ta design system hien tai | Active | UI system | High |
| DEC-003 | Worker la outbound agent theo contract `register + heartbeat + claim + progress + complete/fail` | Active | worker runtime | High |
| DEC-004 | Browser session onboarding va browser upload phai chay tren worker/VPS duoc gan, khong chay tren control plane | Active | worker/browser | High |
| DEC-005 | User co the duoc gan nhieu VPS; channel/browser session/render job phai bam theo worker so huu cua no | Active | user/admin assignment | High |
| DEC-006 | Local upload file lon phai qua `upload session + resumable chunk upload` truoc khi tao job | Active | upload flow | High |
| DEC-007 | Memory bootstrap moi dung `PROJECT_BRIEF + MEMORY_INDEX + DECISIONS_INDEX`; `PROJECT_CONTEXT + DECISIONS + WORKLOG` giu lai cho lich su/tuong thich nguoc | Active | docs/workflow | High |
| DEC-008 | Man `Cap phat BOT` la UI exception theo split-dispatch workspace, nhung van phai giu cung design system chung | Active | admin UI | Medium |
| DEC-009 | Browser upload phai giu progress/complete theo dialog telemetry, nhung visibility contract phai ho tro day du `draft/private/unlisted/public` giong app cu | Active | worker/browser upload | High |
| DEC-010 | `admin` va `manager` duoc vao chung workspace `Điều phối Render` qua `/app`; manager dung kho VPS cua minh, admin dung VPS tu tu cap phat cho chinh tai khoan admin | Active | admin/user workspace | High |
| DEC-011 | Một worker/VPS có thể được gán cho nhiều user; cleanup dữ liệu phải scoped theo `user_id + worker_id`, không xóa kiểu toàn worker khi chỉ gỡ một user hoặc đổi tên BOT | Active | admin assignment + channel data | High |
| DEC-012 | Worker cleanup theo 3 lớp: per-job cleanup, browser-session/profile cleanup, và janitor startup/periodic cho artifact stale | Active | worker runtime | High |

| DEC-013 | Scheduler moi worker giu `1 active job` va claim queue theo round-robin giua cac user tren cung worker; worker chi claim job tiep theo sau khi `run_job()` return, tuc sau cleanup local cua job truoc | Active | worker scheduling + disk safety | High |
| DEC-014 | Worker resilience dung retry/backoff cho HTTP, heartbeat background de tu reconnect sau mat mang/restart, Telegram chi alert khi worker offline qua 180s, va job `uploading` chi `requeue` khi mat ket noi luc upload moi bat dau | Active | worker runtime + control-plane ops | High |
| DEC-015 | Job dang `rendering/uploading` chi duoc chuyen trang thai sau `lease/grace window`; heartbeat hut ngan khong duoc lap tuc `pending/error` | Active | worker runtime + control-plane ops | High |
| DEC-016 | Them worker/BOT moi se di qua mot duong SSH bootstrap duy nhat tren control-plane; UI admin va CLI chi la hai mat goi chung mot helper, khong bootstrap qua Telegram | Active | admin ops + deploy | High |
| DEC-017 | `Xoa BOT` phai la decommission that tren VPS va `Them BOT` phai hien row provisioning live ngay trong Danh sach BOT | Active | admin ops + BOT UI | High |
| DEC-018 | Bootstrap/decommission worker phai chiu duoc Windows line endings va viec tai tao worker tren cung VPS: script upload sang Linux phai duoc normalize LF, venv phai tao o runtime dir, failed install row khong duoc giu slot `worker-XX`, va requirement worker phai du de boot tren may trang | Active | worker bootstrap + admin ops | High |
| DEC-019 | Worker browser/upload live se tam thoi bam sat flow on dinh gan `012e614`: worker service chay duoi `root`, browser canonical la `chromium-browser`/`chromium`, va chi giu lai cleanup stale `Xvfb` + decommission cleanup de tranh state cu sot lai | Superseded | worker runtime + browser profile | High |
| DEC-020 | `admin` va `manager` duoc xem nhu account workspace rieng tren cung VPS, nhung `ChannelRecord` van global theo `channel_id` nen `confirm_browser_session` phai chan viec link them mot kenh da thuoc account khac | Active | admin workspace + shared VPS isolation | High |
| DEC-021 | Neu worker phai dung snap-backed `chromium-browser`, runtime phai override `SNAP_USER_COMMON`, `SNAP_USER_DATA`, `SNAP_REAL_HOME` va `XDG_*` theo tung `browser_profile_path`; bootstrap BOT moi van uu tien browser non-snap, nhung login va upload phai dung cung mot env builder de profile isolation hoat dong ca voi snap | Active | worker runtime + browser profile isolation | High |
| DEC-022 | Production worker browser canonical chuyen sang native `google-chrome-stable`; login noVNC va upload phai dung chung `profile_path + _build_browser_env()` voi `HOME`, `DBUS`, `XDG_*`, `password-store=basic`, shutdown graceful, va khong pin `chromedriver` co dinh trong bootstrap | Active | worker runtime + browser profile | High |
| DEC-023 | Live stream dung chung account/role model voi upload, nhung tach rieng live bot pool, live assignment `primary/backup`, va CRUD route cua live stream | Active | backend + admin/live workspace | High |
| DEC-024 | Workspace live cua admin/manager bo han `Danh sách Kênh`; username/password van dung chung mot account voi upload, con Telegram live se xu ly rieng o app workspace sau | Active | admin/live workspace + auth model | High |
| DEC-025 | Runtime live stream bo hoan toan nhanh `1080/4K`; chat luong phu thuoc media dau vao, audio URL la tuy chon va neu vang thi giu audio goc cua video | Active | live runtime + live form contract | High |
| DEC-026 | Runtime live phase 1 dung namespace `/api/live-workers/*` rieng va bam 4 pha `download -> pre-render -> wait -> RTMP stream`; worker entrypoint lazy-import theo `runtime_mode` de live khong phu thuoc upload stack | Active | live runtime + worker architecture | High |

| DEC-027 | Backup policy cua live phai bam sat app cu: luong co `EndTimeLive` chay song song primary + backup ngay tu dau, con luong `24/7` chi start backup sau khi primary mat ket noi qua `backup delay` va se huy backup neu primary hoi phuc | Superseded | live runtime + failover policy | High |
| DEC-028 | Route canon cua live app la `/app/live` cho moi role; `admin/manager` dung live app bang chinh account cua minh, con `/admin/live` chi giu de redirect tuong thich va cac man quan tri live van o cum `/admin/*?workspace=live` | Active | live workspace + route/auth model | High |
| DEC-029 | BOT live phai di qua cung SSH bootstrap/decommission pipeline voi upload va chon runtime bang `WORKER_RUNTIME_MODE=live`; khong cho local-create BOT live trong control-plane | Active | live worker bootstrap + admin ops | High |
| DEC-030 | Live worker bootstrap phai toi gian theo runtime live: tat browser session va YouTube upload flags de khong cai Chrome/noVNC stack cua upload tren VPS live | Active | live worker bootstrap + runtime isolation | High |
| DEC-031 | Trong control-plane hop nhat, upload va live worker phai mac dinh bootstrap/decommission vao path rieng theo workspace (`/opt/youtube-upload-lush*` cho upload, `/opt/youtube-upload-lush-live-worker*` cho live) thay vi dung chung mot bien global | Active | worker bootstrap + merged main deploy | High |
| DEC-032 | Live backup policy uu tien continuity cua YouTube stream: live co `EndTimeLive` van chay backup song song, con live `24/7` dung `hot standby`; live runtime giu `preparing` remux + RTMP `-c copy`; normalize source hien tam tat mac dinh bang `WORKER_LIVE_NORMALIZE_ENABLED=false`, nhung profile `1080p/1440p/2160p` va GOP ~`2s` van giu san de bat lai bang env | Active | live runtime + failover policy | High |
| DEC-033 | Live runtime chi khoa sua sau khi da vao `streaming/disconnected`; pre-stream edit phai abort runtime cu de worker claim lai cau hinh moi, va moi live VPS chi duoc chay toi da 1 luong overlap | Active | live runtime + scheduling safety | High |
| DEC-034 | Live worker phai coi `rendered.flv` la artifact tam ngan han: janitor quet nhanh hon voi `live-streams/*`, con `xrdp` neu co la van de van hanh host, khong phai bootstrap worker mac dinh can thiep | Active | live worker storage hygiene | Medium |
| DEC-035 | Telegram BOT-operation notifications phai route `admin = tat ca`, `manager = manager so huu BOT`; offline alert van theo `admin all + manager so huu BOT` | Active | ops notifications + role routing | Medium |
| DEC-036 | Telegram livestream notifications dung bot rieng va luu recipient o account-level `telegram_live`; recipient chi la owner cua luong live, khong bam theo BOT/VPS | Active | live notifications + account settings | Medium |
| DEC-037 | Live quota phai tach `worker.capacity` (tran cung cua VPS) khoi `allocated_threads` theo tung mapping `user + worker + role`; UI user/BOT phai hien theo quota cap phat va admin assignment phai chan tong quota vuot tran | Active | live admin assignment + scheduling | High |
| DEC-038 | Chuyen BOT giua `upload` va `live/backup` phai di qua SSH bootstrap lai tren cung VPS; source workspace chi cleanup sau khi target worker register thanh cong | Active | admin BOT conversion + bootstrap | High |
| DEC-039 | Live runtime retry sau loi media phai co cooldown, worker khong duoc reclaim lai chinh stream khi lease chua het, va Telegram disconnect phai throttle cho den khi stream hoi phuc that su | Active | live runtime resilience + alert dedupe | High |
| DEC-040 | Google Drive live downloader uu tien `gdown`, fallback sang direct URL; backup `24/7` duoc phep vao live dung gio neu primary chua ready, nhung van phai lui ve standby khi primary da `waiting/streaming` | Active | live downloader + hot-standby start policy | High |
| DEC-041 | Quan tri quota live phai theo safe policy: khong duoc ha quota hoac go assignment xuong thap hon so luong active hien tai cua user tren BOT; stream dang chay khong bi kill, quota moi chi co the ap khi runtime da co ve muc an toan | Active | live admin assignment + runtime safety | High |
| DEC-042 | Live worker duoc phep chay nhieu stream fast-path, nhung normalize media phai serialize theo tung worker/VPS (`1 normalize slot` mac dinh); stream khong can normalize van tiep tuc `copy` song song | Active | live runtime stability + CPU isolation | High |
| DEC-043 | VPS migration cho control-plane dung fresh Git checkout + runtime data copy; BOT worker duoc bootstrap/add lai rieng sau khi control-plane moi verify | Active | infra migration | High |
| DEC-044 | Inactive BOT alert chay hang ngay luc 8h sang, dem allocation `user + BOT` khong co upload/live job moi qua 10 ngay, route manager theo `manager_id`, va throttle theo daily attempt de tranh retry storm | Active | backend ops notifications | Medium |
| DEC-045 | Live job chi cho sua khi chua vao runtime nang hoac da den pha `waiting`; `downloading/preparing` phai khoa nut `Sửa` va backend update de tranh race runtime cu | Active | live runtime + edit safety | High |

| DEC-046 | BOT live/backup phai co `live_role` mac dinh tren chinh `WorkerRecord`; assignment `user + BOT` chi la quota/scope va khong duoc la noi duy nhat luu chuc nang BOT | Active | live admin assignment + bot metadata | High |

## Notes
- `docs/DECISIONS.md` van giu full history va ly do chi tiet.
- `docs/modules/*.md` nen reference `DEC-xxx` thay vi copy lai noi dung decision.
- Neu co conflict giua docs cu va workflow moi, uu tien `PROJECT_BRIEF + MEMORY_INDEX + DECISIONS_INDEX`, sau do ghi ro vao `WORKLOG`.
