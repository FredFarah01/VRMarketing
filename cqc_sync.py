"""CQC Syndication API client and controlled importer.

Run manually; it is deliberately not invoked by Flask startup.

Examples:
    python cqc_sync.py --limit 25 --dry-run
    python cqc_sync.py --limit 25
    python cqc_sync.py --full
"""

import argparse
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

from app import connect, migrate, now_iso

load_dotenv()

CQC_API_BASE_URL = os.environ.get("CQC_API_BASE_URL", "https://api.service.cqc.org.uk/public/v1").rstrip("/")
CQC_API_KEY = os.environ.get("CQC_API_KEY", "").strip()
CQC_API_KEY_HEADER = os.environ.get("CQC_API_KEY_HEADER", "Ocp-Apim-Subscription-Key").strip()
CQC_PAGE_SIZE = max(1, min(int(os.environ.get("CQC_PAGE_SIZE", "100")), 500))
TIMEOUT_SECONDS = int(os.environ.get("CQC_API_TIMEOUT", "30"))


def _request_json(path, params=None):
    if not CQC_API_KEY:
        raise RuntimeError("CQC_API_KEY is not configured. Add it to .env before running a live sync.")
    url = f"{CQC_API_BASE_URL}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={
            CQC_API_KEY_HEADER: CQC_API_KEY,
            "Accept": "application/json",
            "User-Agent": "VeridynRecruit-CQC-Sync/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"CQC API HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CQC API connection failed: {exc.reason}") from exc


def _first(data, *keys, default=None):
    for key in keys:
        if isinstance(data, dict) and data.get(key) is not None:
            return data[key]
    return default


def _nested(data, *path, default=None):
    current = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _address(record):
    address = _first(record, "postalAddress", "address", default={}) or {}
    if not isinstance(address, dict):
        address = {}
    return {
        "address_line1": _first(address, "addressLine1", "line1"),
        "address_line2": _first(address, "addressLine2", "line2"),
        "town_city": _first(address, "townCity", "town", "city"),
        "county": _first(address, "county"),
        "postcode": _first(address, "postalCode", "postcode"),
    }


def _list_items(payload, singular):
    candidates = {
        "providers": ("providers", "provider"),
        "locations": ("locations", "location"),
    }[singular]
    if isinstance(payload, list):
        return payload
    for key in candidates:
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list):
            return value
    return []


def iter_collection(path, collection, limit=None):
    page = 1
    yielded = 0
    while True:
        payload = _request_json(path, {"page": page, "perPage": CQC_PAGE_SIZE})
        items = _list_items(payload, collection)
        if not items:
            return
        for item in items:
            yield item
            yielded += 1
            if limit and yielded >= limit:
                return
        total_pages = _nested(payload, "totalPages") or _nested(payload, "meta", "totalPages")
        if total_pages and page >= int(total_pages):
            return
        if len(items) < CQC_PAGE_SIZE:
            return
        page += 1


def fetch_provider(provider_id):
    return _request_json(f"providers/{urllib.parse.quote(provider_id, safe='')}")


def fetch_location(location_id):
    return _request_json(f"locations/{urllib.parse.quote(location_id, safe='')}")


def _upsert_provider(db, record):
    provider_id = _first(record, "providerId", "providerID", "id")
    if not provider_id:
        return False
    addr = _address(record)
    now = now_iso()
    values = (
        provider_id,
        _first(record, "name", "providerName", default="Unknown provider"),
        _first(record, "type", "organisationType", "organizationType"),
        _first(record, "registrationStatus", "status"),
        _first(record, "registrationDate"),
        _first(record, "deregistrationDate"),
        addr["address_line1"], addr["address_line2"], addr["town_city"], addr["county"], addr["postcode"],
        _first(record, "website", "websiteUrl"),
        _first(record, "cqcUrl", "url"),
        json.dumps(record, separators=(",", ":"), ensure_ascii=False),
        _first(record, "lastUpdated", "lastUpdatedDate"),
        now, now,
    )
    db.execute(
        """INSERT INTO cqc_providers (
            provider_id, provider_name, organisation_type, registration_status,
            registration_date, deregistration_date, address_line1, address_line2,
            town_city, county, postcode, website, cqc_url, raw_payload,
            source_updated_at, first_synced_at, last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(provider_id) DO UPDATE SET
            provider_name=excluded.provider_name,
            organisation_type=excluded.organisation_type,
            registration_status=excluded.registration_status,
            registration_date=excluded.registration_date,
            deregistration_date=excluded.deregistration_date,
            address_line1=excluded.address_line1,
            address_line2=excluded.address_line2,
            town_city=excluded.town_city,
            county=excluded.county,
            postcode=excluded.postcode,
            website=excluded.website,
            cqc_url=excluded.cqc_url,
            raw_payload=excluded.raw_payload,
            source_updated_at=excluded.source_updated_at,
            last_synced_at=excluded.last_synced_at""",
        values,
    )
    return True


def _replace_children(db, location_id, record):
    mappings = (
        ("cqc_location_service_types", "serviceTypes", "service_type_code", "service_type_name"),
        ("cqc_location_specialisms", "specialisms", "specialism_code", "specialism_name"),
        ("cqc_location_regulated_activities", "regulatedActivities", "activity_code", "activity_name"),
    )
    for table, source_key, code_col, name_col in mappings:
        db.execute(f"DELETE FROM {table} WHERE location_id = ?", (location_id,))
        for item in record.get(source_key, []) or []:
            if isinstance(item, str):
                code, name = item, item
            elif isinstance(item, dict):
                code = _first(item, "code", "id", "name")
                name = _first(item, "name", "description", default=code)
            else:
                continue
            if code:
                db.execute(
                    f"INSERT INTO {table} (location_id, {code_col}, {name_col}) VALUES (?,?,?) "
                    f"ON CONFLICT(location_id, {code_col}) DO UPDATE SET {name_col}=excluded.{name_col}",
                    (location_id, str(code), name),
                )


def _upsert_location(db, record):
    location_id = _first(record, "locationId", "locationID", "id")
    provider_id = _first(record, "providerId", "providerID")
    if not location_id or not provider_id:
        return False
    addr = _address(record)
    ratings = record.get("currentRatings") or record.get("ratings") or {}
    overall = ratings.get("overall") if isinstance(ratings, dict) else {}
    if isinstance(overall, str):
        overall_rating, rating_date = overall, None
    else:
        overall_rating = _first(overall or {}, "rating", "name")
        rating_date = _first(overall or {}, "reportDate", "ratingDate")
    now = now_iso()
    values = (
        location_id, provider_id, _first(record, "name", "locationName", default="Unknown location"),
        _first(record, "registrationStatus", "status"), _first(record, "registrationDate"),
        _first(record, "deregistrationDate"), addr["address_line1"], addr["address_line2"],
        addr["town_city"], addr["county"], addr["postcode"], _first(record, "telephone", "phone"),
        _first(record, "website", "websiteUrl"), overall_rating, rating_date,
        _first(record, "reportPublicationDate", "lastReportPublicationDate"),
        json.dumps(record, separators=(",", ":"), ensure_ascii=False),
        _first(record, "lastUpdated", "lastUpdatedDate"), now, now,
    )
    db.execute(
        """INSERT INTO cqc_locations (
            location_id, provider_id, location_name, registration_status, registration_date,
            deregistration_date, address_line1, address_line2, town_city, county, postcode,
            telephone, website, overall_rating, rating_date, report_publication_date,
            raw_payload, source_updated_at, first_synced_at, last_synced_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(location_id) DO UPDATE SET
            provider_id=excluded.provider_id,
            location_name=excluded.location_name,
            registration_status=excluded.registration_status,
            registration_date=excluded.registration_date,
            deregistration_date=excluded.deregistration_date,
            address_line1=excluded.address_line1,
            address_line2=excluded.address_line2,
            town_city=excluded.town_city,
            county=excluded.county,
            postcode=excluded.postcode,
            telephone=excluded.telephone,
            website=excluded.website,
            overall_rating=excluded.overall_rating,
            rating_date=excluded.rating_date,
            report_publication_date=excluded.report_publication_date,
            raw_payload=excluded.raw_payload,
            source_updated_at=excluded.source_updated_at,
            last_synced_at=excluded.last_synced_at""",
        values,
    )
    _replace_children(db, location_id, record)
    return True


def _page(path, collection, page):
    payload = _request_json(path, {"page": page, "perPage": CQC_PAGE_SIZE})
    items = _list_items(payload, collection)
    total_pages = _nested(payload, "totalPages") or _nested(payload, "meta", "totalPages")
    return items, int(total_pages) if total_pages else None


def run_sync(limit=None, dry_run=False, sync_id=None):
    """Run a bounded, resumable CQC sync.

    Full imports persist the current collection/page in cqc_sync_runs. Only one API
    page is processed per call, keeping memory bounded on small Render instances.
    """
    if dry_run:
        providers = list(iter_collection("providers", "providers", limit=limit))
        locations = list(iter_collection("locations", "locations", limit=limit))
        return {"dry_run": True, "providers_seen": len(providers), "locations_seen": len(locations)}

    migrate()
    db = connect()
    started = now_iso()
    if not sync_id:
        sync_id = str(uuid.uuid4())
        db.execute("""INSERT INTO cqc_sync_runs
            (sync_id,sync_type,status,started_at,providers_seen,locations_seen,records_changed,phase,provider_page,location_page)
            VALUES (?,?, 'Running', ?,0,0,0,'providers',1,1)""",
            (sync_id, "limited" if limit else "full", started))
        db.commit()

    job = db.execute("""SELECT sync_type,status,providers_seen,locations_seen,records_changed,
                       COALESCE(phase,'providers') AS phase,COALESCE(provider_page,1) AS provider_page,
                       COALESCE(location_page,1) AS location_page FROM cqc_sync_runs WHERE sync_id=?""",
                     (sync_id,)).fetchone()
    if not job:
        db.close()
        raise RuntimeError("CQC sync job not found")

    # Limited sync retains the original simple behaviour.
    if limit:
        db.execute("UPDATE cqc_sync_runs SET status='Running', started_at=?, error_message=NULL WHERE sync_id=?",
                   (started, sync_id)); db.commit()
        provider_count = location_count = changed = 0
        try:
            for summary in iter_collection("providers", "providers", limit=limit):
                pid = _first(summary, "providerId", "providerID", "id")
                changed += int(_upsert_provider(db, fetch_provider(pid) if pid else summary)); provider_count += 1
            for summary in iter_collection("locations", "locations", limit=limit):
                lid = _first(summary, "locationId", "locationID", "id")
                detail = fetch_location(lid) if lid else summary
                parent_id = _first(detail, "providerId", "providerID")
                if parent_id and not db.execute("SELECT provider_id FROM cqc_providers WHERE provider_id=?", (parent_id,)).fetchone():
                    _upsert_provider(db, fetch_provider(parent_id))
                changed += int(_upsert_location(db, detail)); location_count += 1
            db.execute("""UPDATE cqc_sync_runs SET status='Completed',completed_at=?,providers_seen=?,
                          locations_seen=?,records_changed=? WHERE sync_id=?""",
                       (now_iso(),provider_count,location_count,changed,sync_id)); db.commit()
            return {"sync_id":sync_id,"completed":True}
        finally:
            db.close()

    phase, ppage, lpage = job["phase"], int(job["provider_page"]), int(job["location_page"])
    providers_seen, locations_seen, changed = int(job["providers_seen"] or 0), int(job["locations_seen"] or 0), int(job["records_changed"] or 0)
    db.execute("UPDATE cqc_sync_runs SET status='Running',error_message=NULL WHERE sync_id=?", (sync_id,)); db.commit()
    try:
        if phase == "providers":
            items, total_pages = _page("providers", "providers", ppage)
            if not items:
                phase = "locations"
                db.execute("UPDATE cqc_sync_runs SET phase='locations' WHERE sync_id=?", (sync_id,)); db.commit()
            else:
                for summary in items:
                    pid = _first(summary, "providerId", "providerID", "id")
                    changed += int(_upsert_provider(db, fetch_provider(pid) if pid else summary))
                    providers_seen += 1
                next_page = ppage + 1
                if (total_pages and ppage >= total_pages) or len(items) < CQC_PAGE_SIZE:
                    phase = "locations"
                db.execute("""UPDATE cqc_sync_runs SET providers_seen=?,records_changed=?,provider_page=?,phase=?
                              WHERE sync_id=?""",(providers_seen,changed,next_page,phase,sync_id)); db.commit()
                print(f"CQC sync {sync_id}: providers page {ppage} committed; {providers_seen} processed", flush=True)
        else:
            items, total_pages = _page("locations", "locations", lpage)
            if not items:
                db.execute("UPDATE cqc_sync_runs SET status='Completed',completed_at=? WHERE sync_id=?",(now_iso(),sync_id)); db.commit()
                return {"sync_id":sync_id,"completed":True}
            for summary in items:
                lid = _first(summary, "locationId", "locationID", "id")
                detail = fetch_location(lid) if lid else summary
                parent_id = _first(detail, "providerId", "providerID")
                if parent_id and not db.execute("SELECT provider_id FROM cqc_providers WHERE provider_id=?", (parent_id,)).fetchone():
                    _upsert_provider(db, fetch_provider(parent_id))
                changed += int(_upsert_location(db, detail)); locations_seen += 1
            next_page = lpage + 1
            completed = bool((total_pages and lpage >= total_pages) or len(items) < CQC_PAGE_SIZE)
            db.execute("""UPDATE cqc_sync_runs SET locations_seen=?,records_changed=?,location_page=?,
                          status=?,completed_at=? WHERE sync_id=?""",
                       (locations_seen,changed,next_page,"Completed" if completed else "Running",
                        now_iso() if completed else None,sync_id)); db.commit()
            print(f"CQC sync {sync_id}: locations page {lpage} committed; {locations_seen} processed", flush=True)
            if completed:
                return {"sync_id":sync_id,"completed":True}
        return {"sync_id":sync_id,"completed":False,"phase":phase}
    except Exception as exc:
        db.rollback()
        db.execute("UPDATE cqc_sync_runs SET status='Queued',error_message=? WHERE sync_id=?",(str(exc)[:2000],sync_id)); db.commit()
        raise
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Synchronise CQC provider/location data into VRMarketing.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--limit", type=int, help="Test sync: import at most N providers and N locations.")
    group.add_argument("--full", action="store_true", help="Import the complete CQC provider/location collections.")
    parser.add_argument("--dry-run", action="store_true", help="Call the API and count records without writing to the database.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    result = run_sync(limit=args.limit if not args.full else None, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
