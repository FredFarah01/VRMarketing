"""Explainable Veridyn sales-account classification and scoring."""

import argparse
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from app import connect, migrate, now_iso

DOMICILIARY_TERMS = ("domiciliary", "homecare", "home care", "care at home")
SUPPORTED_TERMS = ("supported living", "supported living services")
CARE_HOME_TERMS = ("care home", "residential", "nursing home", "nursing homes")
COMPLEX_TERMS = (
    "complex care", "learning disabilities", "autism", "physical disabilities",
    "mental health", "younger adults", "sensory impairment"
)


def _domain(url):
    if not url:
        return None
    candidate = url if "://" in url else "https://" + url
    try:
        host = (urlparse(candidate).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host or None
    except ValueError:
        return None


def _age_months(date_text):
    if not date_text:
        return None
    try:
        dt = datetime.fromisoformat(str(date_text)[:10]).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    now = datetime.now(timezone.utc)
    return max(0, (now.year - dt.year) * 12 + now.month - dt.month)


def _contains(text, terms):
    value = (text or "").lower()
    return any(term in value for term in terms)


def classify_and_score(provider, locations, service_names, specialism_names):
    combined = " | ".join([provider.get("provider_name") or "", *service_names, *specialism_names])
    location_count = len(locations)
    has_domiciliary = _contains(combined, DOMICILIARY_TERMS)
    has_supported = _contains(combined, SUPPORTED_TERMS)
    has_care_home = _contains(combined, CARE_HOME_TERMS)
    has_complex = _contains(combined, COMPLEX_TERMS)

    if location_count >= 5:
        segment = "Multi-Site Care Organisation"
    elif has_domiciliary:
        segment = "Domiciliary Care"
    elif has_supported or has_complex:
        segment = "Complex / Supported Care"
    elif has_care_home:
        segment = "Care Home"
    else:
        segment = "Other CQC Provider"

    score = 0
    reasons = []
    def add(points, reason):
        nonlocal score
        score += points
        reasons.append({"points": points, "reason": reason})

    if has_domiciliary:
        add(25, "Domiciliary/home-care service signal")
    if has_supported:
        add(20, "Supported-living service signal")
    if has_complex:
        add(20, "Complex-care/specialism signal")
    if has_care_home:
        add(15, "Care-home/residential service signal")

    if location_count >= 10:
        add(30, "10+ registered locations")
    elif location_count >= 5:
        add(20, "5–9 registered locations")
    elif location_count >= 2:
        add(10, "2–4 registered locations")

    registrations = [_age_months(l.get("registration_date")) for l in locations]
    registrations = [m for m in registrations if m is not None]
    if registrations:
        newest = min(registrations)
        if newest <= 6:
            add(20, "New CQC location registered within 6 months")
        elif newest <= 24:
            add(15, "CQC location registered within 24 months")

    ratings = {(l.get("overall_rating") or "").strip().lower() for l in locations}
    if "requires improvement" in ratings:
        add(15, "At least one location rated Requires Improvement")
    if "inadequate" in ratings:
        add(15, "At least one location rated Inadequate")

    website = provider.get("website") or next((l.get("website") for l in locations if l.get("website")), None)
    if website:
        add(5, "Website available for contact enrichment")

    # Cap CQC-only score at 100. Engagement/contact signals can later re-score the account.
    score = min(score, 100)
    if score >= 80:
        priority = "A1"
    elif score >= 60:
        priority = "A2"
    elif score >= 40:
        priority = "B"
    else:
        priority = "C"
    return segment, score, priority, reasons, website


def score_all(limit=None, provider_ids=None):
    migrate()
    db = connect()
    if provider_ids:
        placeholders = ",".join("?" for _ in provider_ids)
        providers = db.execute(f"SELECT * FROM cqc_providers WHERE provider_id IN ({placeholders}) ORDER BY provider_name", tuple(provider_ids)).fetchall()
    else:
        providers = db.execute("SELECT * FROM cqc_providers ORDER BY provider_name").fetchall()
    if limit:
        providers = providers[:limit]
    processed = 0
    for p_row in providers:
        p = dict(p_row)
        locations = [dict(r) for r in db.execute(
            "SELECT * FROM cqc_locations WHERE provider_id=?", (p["provider_id"],)
        ).fetchall()]
        services = [r[0] for r in db.execute(
            """SELECT DISTINCT st.service_type_name FROM cqc_locations l
               JOIN cqc_location_service_types st ON st.location_id=l.location_id
               WHERE l.provider_id=? AND st.service_type_name IS NOT NULL""", (p["provider_id"],)
        ).fetchall()]
        specialisms = [r[0] for r in db.execute(
            """SELECT DISTINCT sp.specialism_name FROM cqc_locations l
               JOIN cqc_location_specialisms sp ON sp.location_id=l.location_id
               WHERE l.provider_id=? AND sp.specialism_name IS NOT NULL""", (p["provider_id"],)
        ).fetchall()]
        segment, score, priority, reasons, website = classify_and_score(p, locations, services, specialisms)
        existing = db.execute("SELECT account_id FROM sales_accounts WHERE provider_id=?", (p["provider_id"],)).fetchone()
        account_id = existing[0] if existing else str(uuid.uuid4())
        now = now_iso()
        db.execute(
            """INSERT INTO sales_accounts (
                account_id, provider_id, company_name, website, domain, target_segment,
                location_count, account_score, priority, lifecycle_stage, sales_status,
                score_reasons, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,'Prospect','Unworked',?,?,?)
            ON CONFLICT(provider_id) DO UPDATE SET
                company_name=excluded.company_name,
                website=excluded.website,
                domain=excluded.domain,
                target_segment=excluded.target_segment,
                location_count=excluded.location_count,
                account_score=excluded.account_score,
                priority=excluded.priority,
                score_reasons=excluded.score_reasons,
                updated_at=excluded.updated_at""",
            (account_id, p["provider_id"], p["provider_name"], website, _domain(website), segment,
             len(locations), score, priority, json.dumps(reasons), now, now),
        )
        processed += 1
    db.commit()
    counts = {r[0]: r[1] for r in db.execute(
        "SELECT priority, COUNT(*) FROM sales_accounts GROUP BY priority"
    ).fetchall()}
    db.close()
    return {"processed": processed, "priority_counts": counts}


def main():
    parser = argparse.ArgumentParser(description="Classify and score imported CQC providers.")
    parser.add_argument("--limit", type=int, help="Score only the first N providers for testing.")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    print(json.dumps(score_all(args.limit), indent=2))


if __name__ == "__main__":
    main()
