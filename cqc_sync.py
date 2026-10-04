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


def run_sync(limit=None, dry_run=False):
    if dry_run:
        providers = list(iter_collection("providers", "providers", limit=limit))
        locations = list(iter_collection("locations", "locations", limit=limit))
        return {"dry_run": True, "providers_seen": len(providers), "locations_seen": len(locations)}

    migrate()
    db = connect()
    sync_id = str(uuid.uuid4())
    started = now_iso()
    db.execute(
        "INSERT INTO cqc_sync_runs (sync_id, sync_type, status, started_at) VALUES (?,?,?,?)",
        (sync_id, "limited" if limit else "full", "Running", started),
    )
    db.commit()
    provider_count = location_count = changed = 0
    try:
        for summary in iter_collection("providers", "providers", limit=limit):
            provider_id = _first(summary, "providerId", "providerID", "id")
            detail = fetch_provider(provider_id) if provider_id else summary
            provider_count += 1
            changed += int(_upsert_provider(db, detail))

        # Providers are loaded first to satisfy the provider/location foreign key.
        for summary in iter_collection("locations", "locations", limit=limit):
            location_id = _first(summary, "locationId", "locationID", "id")
            detail = fetch_location(location_id) if location_id else summary
            parent_id = _first(detail, "providerId", "providerID")
            parent_exists = db.execute("SELECT provider_id FROM cqc_providers WHERE provider_id=?", (parent_id,)).fetchone() if parent_id else None
            if parent_id and not parent_exists:
                parent_detail = fetch_provider(parent_id)
                _upsert_provider(db, parent_detail)
            location_count += 1
            changed += int(_upsert_location(db, detail))

        db.execute(
            """UPDATE cqc_sync_runs SET status='Completed', completed_at=?,
               providers_seen=?, locations_seen=?, records_changed=? WHERE sync_id=?""",
            (now_iso(), provider_count, location_count, changed, sync_id),
        )
        db.commit()
        return {"sync_id": sync_id, "providers_seen": provider_count, "locations_seen": location_count, "records_changed": changed}
    except Exception as exc:
        db.rollback()
        db.execute(
            "UPDATE cqc_sync_runs SET status='Failed', completed_at=?, error_message=? WHERE sync_id=?",
            (now_iso(), str(exc)[:2000], sync_id),
        )
        db.commit()
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
