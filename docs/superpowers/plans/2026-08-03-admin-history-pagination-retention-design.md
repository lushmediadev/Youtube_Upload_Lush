# Admin History Pagination And Retention Design

## Scope

- Apply server-side pagination to admin `Danh sach Upload` and `Danh sach Live Stream`.
- Keep manager, channel, and user scope filters across search and page navigation.
- Default to 20 rows per page, newest records first.
- Add control-plane-only retention for successful history older than 30 days.
- Do not change worker APIs, worker services, upload execution, FFmpeg, or live failover behavior.

## Pagination

- Routes accept `page` and `q`; the store scopes and searches the full data set before slicing one page.
- Templates render only the selected page and expose total/from/to metadata to `admin_tables.js`.
- `admin_tables.js` keeps the existing toolbar and delete-current-page workflow, but server mode changes search and page buttons into query-string navigation instead of filtering hundreds of DOM rows.
- Search remains accent-insensitive and covers IDs, titles/names, owner/user, manager, channel/worker, and status fields.

## Retention

- `CONTROL_PLANE_COMPLETED_HISTORY_RETENTION_DAYS` defaults to `30`.
- Cleanup runs from the existing control-plane monitor no more than once per day and persists state only when records were removed.
- Upload eligibility requires `status == "completed"` and `completed_at <= cutoff`.
- Live eligibility applies only to visible primary records and requires all of:
  - `status == "ended"`;
  - `ended_at` is present;
  - no worker claim, no lease, and `is_live_now == false`;
  - terminal anchor is at least 30 days old (`end_time_live` for timed live, otherwise `ended_at`);
  - the backup clone, if present, has no claim/lease, is not live, and has terminal status `stopped`, `ended`, or `error`.
- Cleanup removes eligible upload records plus unreferenced local upload assets/previews, and eligible live primary/clone records plus previews.
- `error`, `cancelled`, `stopped`, and all active records are retained.

## Deployment Safety

- Back up `app_state.db` before first production cleanup.
- Restart only `youtube-upload-web.service`.
- Verify health, active upload/live counts, retention removal counts, page DOM size, and browser reload latency.
