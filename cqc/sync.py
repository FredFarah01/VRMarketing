"""CQC synchronisation jobs: full import, incremental refresh, single-record refresh and retries.

Jobs run in a background thread, write progress to ``cqc_sync_log`` and record per-record failures in
``cqc_sync_failures`` so they can be retried. Only ``cqc_*`` tables are written.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from .client import CQCClient, CQCError, CQCNotFound
from .mapping import now_iso, upsert_location, upsert_provider
from .rules import reclassify

log = logging.getLogger("cqc.sync")

JOB_TYPES = {
    "full": "Full import (providers + locations)",
    "providers": "Sync providers",
    "locations": "Sync locations",
    "incremental": "Incremental refresh (changes since last sync)",
    "retry": "Retry failed records",
    "provider": "Refresh single provider",
    "location": "Refresh single location",
}

_lock = threading.Lock()
_connect = None
_client_factory = CQCClient


def init(connect_fn, client_factory=CQCClient) -> None:
    global _connect, _client_factory
    _connect = connect_fn
    _client_factory = client_factory
    db = _connect()
    db.execute("UPDATE cqc_sync_log SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
               (now_iso(),))
    db.commit()
    db.close()


def is_running() -> bool:
    return _lock.locked()


def _limit() -> int | None:
    value = os.environ.get("CQC_SYNC_LIMIT", "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def _location_filters() -> dict:
    return dict(parse_qsl(os.environ.get("CQC_LOCATION_FILTERS", "")))


class Job:
    def __init__(self, db, job_type: str, window_start=None, window_end=None):
        self.db = db
        self.processed = 0
        self.failed = 0
        cur = db.execute("INSERT INTO cqc_sync_log (job_type, status, started_at, window_start, window_end) "
                         "VALUES (?, 'running', ?, ?, ?) RETURNING id",
                         (job_type, now_iso(), window_start, window_end))
        self.id = cur.fetchone()[0]
        db.commit()

    def progress(self, message: str | None = None) -> None:
        self.db.execute("UPDATE cqc_sync_log SET records_processed = ?, records_failed = ?, message = COALESCE(?, message) "
                        "WHERE id = ?", (self.processed, self.failed, message, self.id))
        self.db.commit()

    def finish(self, status: str, message: str | None = None) -> None:
        self.db.execute("UPDATE cqc_sync_log SET status = ?, finished_at = ?, records_processed = ?, records_failed = ?, "
                        "message = ? WHERE id = ?",
                        (status, now_iso(), self.processed, self.failed, message, self.id))
        self.db.commit()


def _record_failure(db, entity: str, entity_id: str, error: str) -> None:
    db.execute("INSERT INTO cqc_sync_failures (entity_type, entity_id, error, attempts, last_attempt_at) "
               "VALUES (?, ?, ?, 1, ?) ON CONFLICT (entity_type, entity_id) DO UPDATE SET error = excluded.error, "
               "attempts = cqc_sync_failures.attempts + 1, last_attempt_at = excluded.last_attempt_at",
               (entity, entity_id, error[:500], now_iso()))


def refresh_one(db, client, entity: str, entity_id: str) -> str | None:
    """Fetch and upsert one provider/location. Returns the affected provider ID."""
    try:
        if entity == "provider":
            doc = client.get_provider(entity_id)
            pid = upsert_provider(db, doc)
        else:
            doc = client.get_location(entity_id)
            upsert_location(db, doc)
            pid = doc.get("providerId")
        db.execute("DELETE FROM cqc_sync_failures WHERE entity_type = ? AND entity_id = ?", (entity, entity_id))
        return pid
    except CQCNotFound:
        _record_failure(db, entity, entity_id, "Not found in CQC API (404)")
        raise
    except (CQCError, KeyError, ValueError) as exc:
        _record_failure(db, entity, entity_id, str(exc) or type(exc).__name__)
        raise


def _process(job: Job, client, entity: str, ids, affected: set) -> None:
    for entity_id in ids:
        try:
            pid = refresh_one(job.db, client, entity, entity_id)
            affected.add(pid)
            job.processed += 1
        except CQCError as exc:
            job.failed += 1
            if exc.status in (401, 403):
                job.db.commit()
                raise
        except (KeyError, ValueError):
            job.failed += 1
        if (job.processed + job.failed) % 50 == 0:
            job.db.commit()
            job.progress(f"{entity}: {job.processed} synced, {job.failed} failed")
    job.db.commit()


def _last_success_end(db) -> str | None:
    row = db.execute("SELECT MAX(COALESCE(window_end, started_at)) FROM cqc_sync_log "
                     "WHERE status = 'success' AND job_type IN ('full', 'incremental')").fetchone()
    return row[0] if row else None


def run_job(job_type: str, entity_id: str | None = None) -> dict:
    """Run a sync job synchronously. Raises RuntimeError if another job is running."""
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A CQC sync job is already running")
    db = _connect()
    try:
        client = _client_factory()
        window_start = window_end = None
        if job_type == "incremental":
            window_end = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            window_start = _last_success_end(db) or (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
        if job_type == "full":
            window_end = now_iso()
        job = Job(db, job_type, window_start, window_end)
        affected: set = set()
        try:
            if not client.configured:
                raise CQCError("CQC_API_KEY is not configured")
            limit = _limit()
            if job_type in ("full", "providers"):
                ids = (p["providerId"] for p in client.iter_ids("providers", "providerId", limit))
                _process(job, client, "provider", ids, affected)
            if job_type in ("full", "locations"):
                ids = (l["locationId"] for l in client.iter_ids("locations", "locationId", limit,
                                                                  **_location_filters()))
                _process(job, client, "location", ids, affected)
            if job_type == "incremental":
                for entity in ("provider", "location"):
                    _process(job, client, entity, client.iter_changes(entity, window_start, window_end), affected)
            if job_type == "retry":
                failures = db.execute("SELECT entity_type, entity_id FROM cqc_sync_failures ORDER BY last_attempt_at").fetchall()
                for f in failures:
                    _process(job, client, f[0], [f[1]], affected)
            if job_type in ("provider", "location"):
                _process(job, client, job_type, [entity_id], affected)
            job.progress("Classifying records")
            if job_type in ("full", "providers", "locations"):
                reclassify(db)
            else:
                reclassify(db, [p for p in affected if p])
            status = "success" if not job.failed else "partial"
            job.finish(status, f"{job.processed} synced, {job.failed} failed, {client.request_count} API requests")
        except CQCError as exc:
            db.commit()
            job.finish("failed", str(exc))
        except Exception as exc:  # noqa: BLE001 - recorded in the sync log for the admin
            log.exception("CQC sync job %s crashed", job_type)
            job.finish("failed", f"Unexpected error: {type(exc).__name__}")
        return dict(db.execute("SELECT * FROM cqc_sync_log WHERE id = ?", (job.id,)).fetchone())
    finally:
        db.close()
        _lock.release()


def start_job(job_type: str, entity_id: str | None = None) -> None:
    if is_running():
        raise RuntimeError("A CQC sync job is already running")
    threading.Thread(target=_safe_run, args=(job_type, entity_id), daemon=True, name=f"cqc-{job_type}").start()


def _safe_run(job_type, entity_id):
    try:
        run_job(job_type, entity_id)
    except RuntimeError as exc:
        log.warning("%s", exc)


def start_scheduler() -> None:
    """Run an incremental refresh every CQC_AUTO_SYNC_HOURS (default 24; 0 disables) once an initial import exists."""
    hours = float(os.environ.get("CQC_AUTO_SYNC_HOURS", 24))
    if hours <= 0:
        return

    def loop():
        while True:
            time.sleep(600)
            try:
                db = _connect()
                last = _last_success_end(db)
                db.close()
                if not last or not _client_factory().configured or is_running():
                    continue
                last_dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_dt >= timedelta(hours=hours):
                    run_job("incremental")
            except Exception:  # noqa: BLE001
                log.exception("CQC scheduler tick failed")

    threading.Thread(target=loop, daemon=True, name="cqc-scheduler").start()
