# Admin Workspace

## Responsibility
- Chua admin shell, role gate, user/BOT/channel/render management, va flow dieu phoi BOT canon trong `Danh sach BOT`.

## Entry Points
- Admin templates: `backend/app/templates/admin/*`
- Admin JS: `backend/app/static/js/admin_tables.js`
- Web/admin context: `backend/app/web.py`
- Store/auth: `backend/app/store.py`, `backend/app/auth.py`

## Key Files
- `backend/app/templates/admin/_layout.html`
- `backend/app/templates/admin/user_index.html`
- `backend/app/templates/admin/worker_index.html`
- `backend/app/templates/admin/channel_index.html`
- `backend/app/templates/admin/render_index.html`
- `backend/app/static/js/admin_tables.js`

## Depends On
- `docs/UI_SYSTEM.md`
- `backend/app/store.py`
- Session auth va role gate

## Used By
- Admin
- Manager

## Invariants
- Admin/user van phai chung design system; flow BOT owner/assignment hien tai di truc tiep qua `worker_index.html`, khong duy tri them mot screen dieu phoi tach rieng.
- Role assignment, notice/toast, filter/search pattern phai dong bo giua cac trang admin.
- Multi-VPS assignment cho user la behavior hop le; khong duoc vo tinh overwrite ve 1 VPS.
- Danh sach upload/live history phai tim kiem va phan trang tren server (20 dong/trang); khong render toan bo lich su vao DOM roi moi loc o client.

## Known Pitfalls
- Cac route web BOT cu de lai shim redirect de tranh hidden write surface; khi them route moi phai doi chieu voi flow canon `/admin/ManagerBOT/index`.
- Encoding/UTF-8 trong admin templates tung gay loi copy/notice; can can than khi sua text.
- Admin templates de bi phinh do chua ca layout, data marker va inline behavior; nen giu tri thuc o module doc thay vi mo rong root AGENTS.
- Avatar initials cua user phai bo qua ky tu an/format o dau username; khong lay truc tiep codepoint dau tien vi zero-width/BOM se lam avatar trong nhu bi mat.
- Moi class background tra ve tu `_avatar_palette()` phai co trong `tailwind.admin.config.js` safelist va `admin-tailwind.css`; neu thieu, initials mau trang se hien tren nen trang.

## Related Decisions
- `DEC-002`
- `DEC-005`
- `DEC-008`
- `DEC-056`
