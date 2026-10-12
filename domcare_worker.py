"""Isolated, restart-safe CQC domiciliary import for a dedicated Render Cron Job.

Run: python domcare_worker.py
Each page runs in a fresh process so memory is released between pages.
The web service must keep CQC_SYNC_IN_WEB=0.
"""
import os
import subprocess
import sys
import time

MAX_PAGES = max(1, min(int(os.environ.get("CQC_PAGES_PER_RUN", "20")), 100))
MAX_SECONDS = max(60, min(int(os.environ.get("CQC_MAX_RUN_SECONDS", "840")), 3300))


def one_page():
    from app import connect
    from cqc_sync import run_sync
    from account_scoring import score_all

    db = connect()
    try:
        job = db.execute(
            "SELECT sync_id FROM cqc_sync_runs WHERE sync_type='domiciliary' "
            "AND status IN ('Queued','Running') ORDER BY started_at ASC LIMIT 1"
        ).fetchone()
        if not job:
            print("DCC sync: no unfinished job", flush=True)
            return 2
        sync_id = job[0]
    finally:
        db.close()
    result = run_sync(sync_id=sync_id)
    touched = result.get("touched_provider_ids") or []
    if touched:
        score_all(provider_ids=touched)
    db = connect()
    try:
        if not result.get("completed"):
            db.execute("UPDATE cqc_sync_runs SET status='Queued' WHERE sync_id=?", (sync_id,))
            db.commit()
    finally:
        db.close()
    print(f"DCC page committed, accounts updated: {len(touched)}; completed={result.get('completed')}", flush=True)
    return 2 if result.get("completed") else 0


def main():
    if "--one-page" in sys.argv:
        return one_page()
    deadline = time.monotonic() + MAX_SECONDS
    for n in range(MAX_PAGES):
        if time.monotonic() >= deadline:
            break
        completed = subprocess.run([sys.executable, __file__, "--one-page"], check=False)
        if completed.returncode == 2:
            return 0
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
