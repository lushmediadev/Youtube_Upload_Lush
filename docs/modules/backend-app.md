# Backend App

## Responsibility
- Chua FastAPI control plane, auth, store/schema, web routes, user/admin APIs, worker APIs.
- Serve UI server-rendered qua Jinja + static JS/CSS.

## Entry Points
- Run app: `backend.app.main:app`
- Web routes: `backend/app/web.py`
- User API: `backend/app/api_user.py`
- Worker API: `backend/app/api_worker.py`
- Auth/session: `backend/app/auth.py`

## Key Files
- `backend/app/main.py`
- `backend/app/web.py`
- `backend/app/api_user.py`
- `backend/app/api_worker.py`
- `backend/app/store.py`
- `backend/app/schemas.py`
- `backend/app/auth.py`

## Depends On
- `backend/app/templates/*`
- `backend/app/static/*`
- Runtime env, local SQLite snapshot trong `backend/app/data`
- Worker contract trong `workers/agent` thong qua API

## Used By
- User workspace
- Admin workspace
- Worker VPS
- Local/prod deploy runtime

## Invariants
- Route handler giu vai tro thin adapter; business state/contract tap trung o `store.py` + `schemas.py`.
- UI user/admin duoc render tu backend nay, khong coi React/Vite la source chinh.
- Khi doi worker/user/channel/job contract, phai dong bo store, schema, route va docs.
- Inactive BOT alert/admin snapshot la BOT-level: chi canh bao khi tat ca assigned users tren worker deu idle qua nguong; live BOT co stream non-terminal/standby van tinh la dang hoat dong.

## Known Pitfalls
- `store.py` dang gom nhieu nghiep vu va la noi de phat sinh drift nhat.
- Template + JS co query-string cache key; sua JS ma quen bump key co the lam browser van giu bundle cu.
- Local bootstrap SQLite va production target Postgres/Redis chua dong bo hoan toan; khong duoc nham lam bootstrap state la architecture cuoi cung.
- Khong quay lai dem inactive theo tung `user + BOT` trong Telegram alert, vi BOT multi-user se bi spam false positive du van con user khac dang su dung.

## Related Decisions
- `DEC-001`
- `DEC-005`
- `DEC-006`
- `DEC-007`
