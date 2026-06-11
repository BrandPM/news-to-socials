-- NTS_056 Task 3 — one-shot cleanup of zombie runs stuck in 'running' >24h.
-- Closes the zombies from NTS_055 (#13 from 25.05, #21 from 06.06) and any
-- other stale run. The hourly APScheduler job in the admin API does the same
-- thing going forward; this file is for the initial manual sweep.
--
-- DB path on the VPS is ./admin.db relative to the repo (ADMIN_DB_PATH unset
-- → /opt/news-to-socials/repo/admin.db). The VPS has no sqlite3 CLI, so run
-- it through the venv Python:
--
--   cd /opt/news-to-socials/repo
--   .venv/bin/python -c "import sqlite3; sqlite3.connect('admin.db').executescript(open('scripts/cleanup_stale_runs.sql').read()); print('done')"
--
-- (With the sqlite3 CLI present elsewhere: sqlite3 admin.db < scripts/cleanup_stale_runs.sql)
-- Note: the admin API's hourly startup sweep also closes these on restart.
--
UPDATE runs
SET status = 'failed',
    finished_at = CURRENT_TIMESTAMP,
    log_excerpt = COALESCE(log_excerpt, '') || char(10)
                  || '[NTS_056 cleanup] marked failed — stuck running > 24h'
WHERE status = 'running'
  AND started_at < datetime('now', '-24 hours');

SELECT id, status, started_at, finished_at FROM runs WHERE status = 'failed'
  AND log_excerpt LIKE '%NTS_056 cleanup%';
