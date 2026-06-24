# Worker Download Timeout Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent an upload worker from hanging indefinitely when a Google Drive download stops responding, then retry cleanly before failing the job.

**Architecture:** Run `gdown` as a cancellable subprocess instead of inside the long-lived worker process. Monitor output-file growth, terminate the subprocess after a configurable stall window, remove partial artifacts, and retry a bounded number of times before raising an actionable error.

**Tech Stack:** Python 3.12, `subprocess`, `httpx`, `unittest`/`pytest`, systemd upload workers.

---

### Task 1: Downloader regression tests

**Files:**
- Create: `tests/test_worker_downloader.py`
- Modify: `workers/agent/downloader.py`

- [ ] **Step 1: Write failing tests**

Cover process termination after no file growth, partial-file cleanup before retry, successful second attempt, and final error after all attempts fail.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest tests/test_worker_downloader.py -q`

Expected: FAIL because cancellable `gdown` and retry helpers do not exist yet.

- [ ] **Step 3: Implement minimal runtime behavior**

Add env-backed defaults:

```text
WORKER_REMOTE_DOWNLOAD_STALL_TIMEOUT_SECONDS=180
WORKER_REMOTE_DOWNLOAD_ATTEMPTS=3
WORKER_REMOTE_DOWNLOAD_RETRY_DELAY_SECONDS=5
```

Use `sys.executable -m gdown --fuzzy --quiet <url> -O <target>` and terminate/kill the child process when the target file has not grown for the stall window. Remove the target and gdown partial files between attempts.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
python -m pytest tests/test_worker_downloader.py -q
python -m pytest tests/test_worker_cleanup.py tests/test_worker_poll_intervals.py -q
python -m compileall workers/agent
```

Expected: all tests pass and compileall exits `0`.

### Task 2: Production worker rollout

**Files:**
- Modify: `docs/WORKLOG.md`
- Modify: `docs/CHANGELOG.md`

- [ ] **Step 1: Inventory upload workers**

Read production `app_state.db`, classify each upload worker as idle, busy, or offline, and obtain its saved SSH profile without exposing credentials.

- [ ] **Step 2: Deploy artifact**

Sync the verified worker source to reachable upload workers. Do not restart `youtube-upload-web.service`.

- [ ] **Step 3: Apply safely**

Restart `youtube-upload-worker.service` immediately only on idle workers. For busy workers, install a one-shot watcher that restarts the service only after control-plane reports no active job for that worker.

- [ ] **Step 4: Verify rollout**

Confirm the new source hash/file content on each reachable worker, immediate restart success for idle workers, deferred watcher state for busy workers, and unchanged active-job status.

- [ ] **Step 5: Record incident**

Append concise rollout and behavior notes to `docs/WORKLOG.md` and `docs/CHANGELOG.md`.
