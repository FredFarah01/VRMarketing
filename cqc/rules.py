"""Veridyn target-market classification, multi-site tiers, opportunity signals and Veridyn Lead Score.

Everything here is Veridyn-generated sales intelligence, not CQC data or an official CQC assessment.
Rules are stored as JSON in ``cqc_settings`` (key ``rules``) and editable in the admin control centre.
"""
import copy
import json
from datetime import date, timedelta

from .mapping import now_iso

SEGMENTS = [
    "DOMICILIARY CARE", "SUPPORTED LIVING", "RESIDENTIAL CARE HOME", "NURSING HOME", "COMPLEX CARE",
    "LEARNING DISABILITY", "AUTISM", "MENTAL HEALTH", "EXTRA CARE", "LIVE-IN CARE", "NURSING AGENCY",
    "MULTI-SITE CARE GROUP", "OTHER SOCIAL CARE", "NON-TARGET",
]
MATCH_FIELDS = ["organisation_type", "location_types", "service_types", "specialisms", "regulated_activities",
                "inspection_categories", "care_home", "inspection_directorate", "name"]
ASC = [["inspection_directorate", "adult social care"]]

DEFAULT_RULES = {
    "segments": [
        {"segment": "DOMICILIARY CARE", "any": [["service_types", "homecare agenc"]]},
        {"segment": "SUPPORTED LIVING", "any": [["service_types", "supported living"]]},
        {"segment": "NURSING HOME", "any": [["service_types", "care home service with nursing"]]},
        {"segment": "RESIDENTIAL CARE HOME", "any": [["service_types", "care home service without nursing"]]},
        {"segment": "EXTRA CARE", "any": [["service_types", "extra care housing"]]},
        {"segment": "NURSING AGENCY", "any": [["service_types", "nurses agency"]]},
        {"segment": "LIVE-IN CARE", "any": [["name", "live-in"], ["name", "live in care"]],
         "all": [["service_types", "homecare"]]},
        {"segment": "COMPLEX CARE", "any": [["name", "complex care"], ["name", "complex needs"]], "all": ASC},
        {"segment": "LEARNING DISABILITY", "any": [["specialisms", "learning disabilit"]], "all": ASC},
        {"segment": "AUTISM", "any": [["specialisms", "autis"], ["name", "autis"]], "all": ASC},
        {"segment": "MENTAL HEALTH", "any": [["specialisms", "mental health"]], "all": ASC},
        {"segment": "OTHER SOCIAL CARE", "any": ASC, "fallback": True},
    ],
    "multi_site_min_locations": 2,
    "size_tiers": [
        {"name": "Independent", "min": 1, "max": 1},
        {"name": "Small Group", "min": 2, "max": 5},
        {"name": "Growing Group", "min": 6, "max": 10},
        {"name": "Multi-Site Group", "min": 11, "max": 25},
        {"name": "Large Group", "min": 26, "max": 50},
        {"name": "Enterprise Group", "min": 51, "max": None},
    ],
    "signals": {
        "new_registration_days": 90,
        "location_thresholds": [10, 25, 50],
        "bed_thresholds": [50, 100],
        "recent_report_days": 90,
        "new_location_days": 90,
    },
    "scoring": {
        "target_segment": 20,
        "segment_bonus": {"DOMICILIARY CARE": 10, "SUPPORTED LIVING": 10, "COMPLEX CARE": 10,
                          "NURSING HOME": 8, "RESIDENTIAL CARE HOME": 8, "LIVE-IN CARE": 8},
        "size_tier": {"Small Group": 10, "Growing Group": 20, "Multi-Site Group": 30, "Large Group": 35,
                      "Enterprise Group": 40},
        "new_registration": 10,
        "beds_50_plus": 5,
        "beds_100_plus": 10,
        "recent_report": 5,
        "new_location": 5,
        "max": 100,
    },
}


def load_rules(db) -> dict:
    row = db.execute("SELECT value FROM cqc_settings WHERE key = 'rules'").fetchone()
    rules = copy.deepcopy(DEFAULT_RULES)
    if row:
        rules.update(json.loads(row[0]))
    return rules


def validate_rules(rules: dict) -> list[str]:
    errors = []
    segments = rules.get("segments", [])
    if not isinstance(segments, list) or not all(isinstance(r, dict) for r in segments):
        return ["segments must be a list of rule objects"]
    for i, rule in enumerate(segments):
        if rule.get("segment") not in SEGMENTS:
            errors.append(f"segments[{i}]: unknown segment {rule.get('segment')!r}")
        for key in ("any", "all"):
            conds = rule.get(key, [])
            if not isinstance(conds, list):
                errors.append(f"segments[{i}].{key} must be a list")
                continue
            for cond in conds:
                if not (isinstance(cond, list) and len(cond) == 2 and cond[0] in MATCH_FIELDS
                        and isinstance(cond[1], str)):
                    errors.append(f"segments[{i}].{key}: conditions must be [field, text] with field in {MATCH_FIELDS}")
    tiers = rules.get("size_tiers", [])
    if not isinstance(tiers, list) or not all(isinstance(t, dict) for t in tiers):
        return errors + ["size_tiers must be a list of tier objects"]
    for t in tiers:
        if not isinstance(t.get("min"), int) or not (t.get("max") is None or isinstance(t.get("max"), int)):
            errors.append(f"size_tiers: {t.get('name')!r} needs integer min/max (max may be null)")
    if not isinstance(rules.get("multi_site_min_locations", 2), int):
        errors.append("multi_site_min_locations must be an integer")
    for key in ("signals", "scoring"):
        if key in rules and not isinstance(rules[key], dict):
            errors.append(f"{key} must be an object")
    return errors


def save_rules(db, rules: dict) -> None:
    db.execute("INSERT INTO cqc_settings (key, value, updated_at) VALUES ('rules', ?, ?) "
               "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
               (json.dumps(rules), now_iso()))


def _cond(record: dict, cond) -> bool:
    field, needle = cond
    return needle.lower() in (record.get(field) or "").lower()


def classify_record(record: dict, rules: dict) -> list[str]:
    matched = []
    for rule in rules["segments"]:
        if rule.get("fallback") and matched:
            continue
        if rule.get("any") and not any(_cond(record, c) for c in rule["any"]):
            continue
        if not all(_cond(record, c) for c in rule.get("all", [])):
            continue
        if rule["segment"] not in matched:
            matched.append(rule["segment"])
    return matched or ["NON-TARGET"]


def size_tier(count: int, rules: dict) -> str | None:
    for t in rules["size_tiers"]:
        if count >= t["min"] and (t["max"] is None or count <= t["max"]):
            return t["name"]
    return None


def _within(value: str | None, days: int, today: date) -> bool:
    try:
        return bool(value) and date.fromisoformat(value[:10]) >= today - timedelta(days=days)
    except ValueError:
        return False


def is_active(status: str | None) -> bool:
    return (status or "").lower() == "registered"


def _signals_and_score(entity: dict, segments: list[str], loc_count: int, beds: int, tier: str | None,
                       newest_location: str | None, rules: dict, today: date) -> tuple[list[str], int]:
    cfg, sc = rules["signals"], rules["scoring"]
    signals, score = [], 0
    target = segments != ["NON-TARGET"]
    if _within(entity.get("registration_date"), cfg["new_registration_days"], today):
        signals.append(f"New CQC registration (last {cfg['new_registration_days']} days)")
        score += sc["new_registration"]
    if loc_count >= rules["multi_site_min_locations"]:
        signals.append("Multi-site provider")
    for n in cfg["location_thresholds"]:
        if loc_count >= n:
            signals.append(f"{n}+ locations")
    for n in cfg["bed_thresholds"]:
        if beds >= n:
            signals.append(f"Care home {n}+ beds")
    if beds >= 100:
        score += sc["beds_100_plus"]
    elif beds >= 50:
        score += sc["beds_50_plus"]
    if _within(entity.get("last_report_date"), cfg["recent_report_days"], today):
        signals.append(f"CQC report published (last {cfg['recent_report_days']} days)")
        score += sc["recent_report"]
    if not entity.get("website"):
        signals.append("No website recorded")
    if newest_location and _within(newest_location, cfg["new_location_days"], today) \
            and newest_location != entity.get("registration_date"):
        signals.append(f"New location registered (last {cfg['new_location_days']} days)")
        score += sc["new_location"]
    if target:
        score += sc["target_segment"] + max((sc["segment_bonus"].get(s, 0) for s in segments), default=0)
        score += sc["size_tier"].get(tier or "", 0)
    if not is_active(entity.get("registration_status")):
        score = 0
    return signals, min(score, sc["max"])


LOCATION_COLS = ("location_id, provider_id, name, organisation_type, location_types, service_types, specialisms, "
                 "regulated_activities, inspection_categories, care_home, inspection_directorate, registration_status, "
                 "registration_date, last_report_date, website, number_of_beds")
PROVIDER_COLS = ("provider_id, name, organisation_type, regulated_activities, inspection_categories, "
                 "inspection_directorate, registration_status, registration_date, last_report_date, website, location_ids")

UPSERT_CLASS = (
    "INSERT INTO cqc_classifications (entity_type, entity_id, segments, primary_segment, is_target, location_count, "
    "active_location_count, total_beds, size_tier, signals, lead_score, computed_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (entity_type, entity_id) DO UPDATE SET "
    "segments = excluded.segments, primary_segment = excluded.primary_segment, is_target = excluded.is_target, "
    "location_count = excluded.location_count, active_location_count = excluded.active_location_count, "
    "total_beds = excluded.total_beds, size_tier = excluded.size_tier, signals = excluded.signals, "
    "lead_score = excluded.lead_score, computed_at = excluded.computed_at")


def _in_clause(ids):
    return ", ".join("?" for _ in ids)


def reclassify(db, provider_ids: list[str] | None = None, today: date | None = None) -> int:
    """Recompute classifications for the given providers (and their locations), or everything."""
    rules = load_rules(db)
    today = today or date.today()
    ts = now_iso()
    if provider_ids is not None:
        provider_ids = list(dict.fromkeys(p for p in provider_ids if p))
        if not provider_ids:
            return 0
        chunks = [provider_ids[i:i + 500] for i in range(0, len(provider_ids), 500)]
    else:
        chunks = [None]
    total = 0
    for chunk in chunks:
        where = f" WHERE provider_id IN ({_in_clause(chunk)})" if chunk else ""
        params = chunk or []
        providers = [dict(r) for r in db.execute(f"SELECT {PROVIDER_COLS} FROM cqc_providers{where}", params)]
        locations = [dict(r) for r in db.execute(f"SELECT {LOCATION_COLS} FROM cqc_locations{where}", params)]
        links: dict[str, set] = {}
        for r in db.execute(f"SELECT provider_id, location_id FROM cqc_provider_locations{where}", params):
            links.setdefault(r[0], set()).add(r[1])
        by_provider: dict[str, list] = {}
        rows = []
        for loc in locations:
            segs = classify_record(loc, rules)
            loc["_segments"] = segs
            by_provider.setdefault(loc["provider_id"], []).append(loc)
            beds = loc.get("number_of_beds") or 0
            signals, score = _signals_and_score(loc, segs, 1, beds, None, None, rules, today)
            signals = [s for s in signals if s != "No website recorded" or not loc.get("website")]
            rows.append(("location", loc["location_id"], "|" + "|".join(segs) + "|", segs[0],
                         int(segs != ["NON-TARGET"]), None, None, beds, None, json.dumps(signals), score, ts))
        for prov in providers:
            locs = by_provider.get(prov["provider_id"], [])
            active = [l for l in locs if is_active(l["registration_status"])]
            try:
                listed = set(json.loads(prov.get("location_ids") or "[]"))
            except ValueError:
                listed = set()
            loc_ids = listed | links.get(prov["provider_id"], set()) | {l["location_id"] for l in locs}
            loc_count = len(loc_ids)
            source_locs = active or locs
            if source_locs:
                segs = []
                for l in source_locs:
                    segs += [s for s in l["_segments"] if s not in segs]
                if len(segs) > 1 and "NON-TARGET" in segs:
                    segs.remove("NON-TARGET")
                if len(segs) > 1 and "OTHER SOCIAL CARE" in segs:
                    segs.remove("OTHER SOCIAL CARE")
            else:
                segs = classify_record(prov, rules)
            target = segs != ["NON-TARGET"]
            if target and loc_count >= rules["multi_site_min_locations"]:
                segs = segs + ["MULTI-SITE CARE GROUP"]
            beds = sum((l.get("number_of_beds") or 0) for l in active)
            tier = size_tier(loc_count, rules) if loc_count else None
            newest = max((l["registration_date"] for l in active if l.get("registration_date")), default=None)
            max_beds = max(((l.get("number_of_beds") or 0) for l in active), default=0)
            signals, score = _signals_and_score(prov, segs, loc_count, max_beds, tier, newest, rules, today)
            rows.append(("provider", prov["provider_id"], "|" + "|".join(segs) + "|", segs[0], int(target),
                         loc_count, len(active), beds, tier, json.dumps(signals), score, ts))
        if rows:
            db.executemany(UPSERT_CLASS, rows)
        total += len(rows)
    db.commit()
    return total
