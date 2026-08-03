# Admin History Pagination And Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render only one admin history page and automatically remove genuinely completed upload/live history after 30 days.

**Architecture:** FastAPI routes pass `page` and `q` to `AppStore`, which scopes, searches, sorts, and slices records before building template rows. The existing table enhancer receives server-pagination metadata and navigates with query parameters. The existing monitor performs a daily control-plane-only cleanup with strict upload/live terminal predicates.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, Jinja2, vanilla JavaScript, pytest, systemd.

## Global Constraints

- Page size is fixed at 20.
- Retention defaults to 30 days.
- Only upload `completed` and genuinely terminal live `ended` records are eligible.
- No worker contract, worker service, FFmpeg, upload runtime, or failover change.
- Preserve UTF-8 Vietnamese UI and the current admin table visual language.

---

### Task 1: Store Pagination

**Files:**
- Modify: `backend/app/store.py`
- Modify: `backend/app/routers/web.py`
- Test: `tests/test_admin_history.py`

**Interfaces:**
- Consumes: scoped `jobs` and visible `live_streams`.
- Produces: `get_admin_render_index_context(..., page: int, query: str)` with `history_pagination` metadata.

- [ ] Write failing tests proving upload/live contexts return at most 20 rows, clamp invalid pages, preserve global row indexes, and search the full scoped history.
- [ ] Run `python -m pytest tests/test_admin_history.py -q` and confirm the pagination tests fail.
- [ ] Add route query parameters plus store search/slice helpers and page metadata.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Server-Paginated Table UI

**Files:**
- Modify: `backend/app/templates/admin/render_index.html`
- Modify: `backend/app/templates/admin/live_render_index.html`
- Modify: `backend/app/static/js/admin_tables.js`
- Test: `tests/test_navigation_performance.py`

**Interfaces:**
- Consumes: `dashboard.history_pagination`.
- Produces: table data attributes consumed by `admin_tables.js` server mode.

- [ ] Add failing static tests for server-pagination markers, query-based search, and absence of client-only paging on history tables.
- [ ] Run the focused navigation tests and confirm failure.
- [ ] Add server metadata attributes and make search/page controls update `page`/`q` while retaining existing query parameters.
- [ ] Run `node --check backend/app/static/js/admin_tables.js` and the focused tests.

### Task 3: Thirty-Day Completed History Retention

**Files:**
- Modify: `backend/app/store.py`
- Test: `tests/test_admin_history.py`

**Interfaces:**
- Produces: `_cleanup_completed_history(now: datetime) -> dict[str, int]` and daily monitor scheduling.

- [ ] Add failing tests for upload completed cutoff, live genuine-ended checks, active backup protection, stopped/error preservation, clone removal, and artifact cleanup calls.
- [ ] Run the focused tests and confirm failure.
- [ ] Implement the strict predicates, cleanup operation, environment defaults, daily throttle, and monitor log.
- [ ] Re-run focused tests and confirm pass.

### Task 4: Verification And Production Rollout

**Files:**
- Modify: `docs/WORKLOG.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:**
- Consumes: tested control-plane patch.
- Produces: production commit and measured evidence.

- [ ] Run full `python -m pytest -q`, backend compileall, JS syntax check, and `git diff --check`.
- [ ] Back up production `app_state.db`, record pre-cleanup active counts, and deploy by restarting only `youtube-upload-web.service`.
- [ ] Verify health, active job continuity, cleanup counts, payload size, DOM rows/elements, and browser reload timings.
- [ ] Commit and push the verified implementation.
