"""Search/filter query builder for CQC providers and locations (parameterised SQL only)."""
import math
import time
from datetime import date, timedelta

import requests

from .rules import SEGMENTS

LEAD_STATUSES = ["NEW", "RESEARCHING", "CONTACT FOUND", "CONTACTED", "FOLLOW-UP", "DEMO BOOKED", "TRIAL",
                 "PROPOSAL", "CUSTOMER", "NOT INTERESTED", "LOST"]
NEW_REG_WINDOWS = [(7, "Last 7 days"), (30, "Last 30 days"), (60, "Last 60 days"), (90, "Last 90 days"),
                   (182, "Last 6 months"), (365, "Last 12 months")]
RATINGS = ["Outstanding", "Good", "Requires improvement", "Inadequate"]

FILTER_KEYS = ["q", "status", "segment", "org_type", "ownership", "care_home", "service_type", "specialism",
               "activity", "location_type", "region", "county", "town", "la", "icb", "constituency", "postcode",
               "radius", "rating", "inspected", "reg_from", "reg_to", "new", "min_locations", "max_locations",
               "size_tier", "min_beds", "max_beds", "signal", "lead_status", "in_leads", "source"]

PROVIDER_SORTS = {"name": "p.name", "score": "COALESCE(c.lead_score, 0)", "locations": "COALESCE(c.location_count, 0)",
                  "registered": "p.registration_date", "report": "p.last_report_date",
                  "beds": "COALESCE(c.total_beds, 0)", "rating": "p.current_rating"}
LOCATION_SORTS = {"name": "l.name", "score": "COALESCE(c.lead_score, 0)", "locations": "COALESCE(pc.location_count, 0)",
                  "registered": "l.registration_date", "report": "l.last_report_date",
                  "beds": "COALESCE(l.number_of_beds, 0)", "rating": "l.current_rating"}

GEO = {"region": "region", "county": "county", "town": "town_city", "la": "local_authority", "icb": "icb_name",
       "constituency": "constituency"}
PIPED = {"service_type": "service_types", "specialism": "specialisms", "activity": "regulated_activities",
         "location_type": "location_types"}

_postcode_cache: dict[str, tuple[float, float] | None] = {}


def clean_filters(args, clean) -> dict:
    f = {k: clean(args.get(k), 120) for k in FILTER_KEYS}
    f["status"] = f["status"] if f["status"] in ("active", "inactive", "all") else "active"
    for k in ("radius", "new", "min_locations", "max_locations", "min_beds", "max_beds", "inspected"):
        f[k] = f[k] if f[k].isdigit() else ""
    for k in ("reg_from", "reg_to"):
        try:
            f[k] = date.fromisoformat(f[k]).isoformat() if f[k] else ""
        except ValueError:
            f[k] = ""
    if f["segment"] and f["segment"] not in SEGMENTS:
        f["segment"] = ""
    if f["lead_status"] and f["lead_status"] not in LEAD_STATUSES:
        f["lead_status"] = ""
    return f


def geocode_postcode(db, postcode: str) -> tuple[float, float] | None:
    key = postcode.replace(" ", "").upper()
    if key in _postcode_cache:
        return _postcode_cache[key]
    row = db.execute("SELECT latitude, longitude FROM cqc_locations WHERE REPLACE(UPPER(postal_code), ' ', '') = ? "
                     "AND latitude IS NOT NULL LIMIT 1", (key,)).fetchone()
    coords = (row[0], row[1]) if row else None
    if coords is None:
        try:
            resp = requests.get(f"https://api.postcodes.io/postcodes/{requests.utils.quote(key)}", timeout=5)
            if resp.ok:
                res = resp.json().get("result") or {}
                if res.get("latitude") is not None:
                    coords = (res["latitude"], res["longitude"])
        except requests.RequestException:
            coords = None
    _postcode_cache[key] = coords
    return coords


def _radius_sql(alias: str, lat: float, lon: float, miles: float) -> tuple[str, list]:
    dlat = miles / 69.0
    k = math.cos(math.radians(lat))
    dlon = dlat / max(k, 0.01)
    sql = (f"({alias}.latitude BETWEEN ? AND ? AND {alias}.longitude BETWEEN ? AND ? AND "
           f"(({alias}.latitude - ?) * ({alias}.latitude - ?) + ({alias}.longitude - ?) * ({alias}.longitude - ?) * ?) <= ?)")
    return sql, [lat - dlat, lat + dlat, lon - dlon, lon + dlon, lat, lat, lon, lon, k * k, dlat * dlat]


def _status_sql(alias: str, status: str) -> str:
    if status == "active":
        return f"{alias}.registration_status = 'Registered'"
    if status == "inactive":
        return f"COALESCE({alias}.registration_status, '') <> 'Registered'"
    return "1=1"


def _common_location_conds(f: dict, like: str, db, notes: list) -> tuple[list[str], list]:
    """Conditions on a location row aliased ``l``."""
    conds, params = [], []
    if f["care_home"] in ("Y", "N"):
        conds.append("COALESCE(l.care_home, 'N') = ?")
        params.append(f["care_home"])
    for key, col in PIPED.items():
        if f[key]:
            conds.append(f"l.{col} {like} ?")
            params.append(f"%|{f[key]}|%")
    if f["min_beds"] or f["max_beds"]:
        if f["min_beds"]:
            conds.append("COALESCE(l.number_of_beds, 0) >= ?")
            params.append(int(f["min_beds"]))
        if f["max_beds"]:
            conds.append("COALESCE(l.number_of_beds, 0) <= ?")
            params.append(int(f["max_beds"]))
    if f["postcode"]:
        coords = geocode_postcode(db, f["postcode"])
        if coords:
            sql, p = _radius_sql("l", coords[0], coords[1], float(f["radius"] or 10))
            conds.append(sql)
            params += p
        else:
            conds.append(f"REPLACE(UPPER(l.postal_code), ' ', '') {like} ?")
            params.append(f["postcode"].replace(" ", "").upper() + "%")
            notes.append("Postcode could not be located, so matching on postcode prefix instead of radius.")
    return conds, params


def build_query(db, f: dict, view: str, use_pg: bool) -> tuple[str, list, str, list[str]]:
    """Return (from_where_sql, params, select_cols, notes) for providers or locations."""
    like = "ILIKE" if use_pg else "LIKE"
    notes: list[str] = []
    where, params = ["1=1"], []
    today = date.today()
    loc_conds, loc_params = _common_location_conds(f, like, db, notes)

    if view == "locations":
        select = ("l.location_id, l.provider_id, l.name, l.registration_status, l.registration_date, l.town_city, "
                  "l.postal_code, l.region, l.local_authority, l.care_home, l.number_of_beds, l.service_types, "
                  "l.current_rating, l.last_report_date, l.website, l.main_phone_number, l.source, "
                  "p.name AS provider_name, c.primary_segment, c.segments, c.lead_score, c.signals, "
                  "pc.location_count, pc.size_tier, a.status AS lead_status")
        sql = ("FROM cqc_locations l LEFT JOIN cqc_providers p ON p.provider_id = l.provider_id "
               "LEFT JOIN cqc_classifications c ON c.entity_type = 'location' AND c.entity_id = l.location_id "
               "LEFT JOIN cqc_classifications pc ON pc.entity_type = 'provider' AND pc.entity_id = l.provider_id "
               "LEFT JOIN lead_accounts a ON a.provider_id = l.provider_id")
        where.append(_status_sql("l", f["status"]))
        where += loc_conds
        params += loc_params
        if f["q"]:
            where.append(f"(l.name {like} ? OR l.location_id {like} ? OR l.provider_id {like} ? OR p.name {like} ? "
                         f"OR COALESCE(p.companies_house_number, '') {like} ? OR COALESCE(l.postal_code, '') {like} ? "
                         f"OR COALESCE(l.town_city, '') {like} ? OR COALESCE(l.local_authority, '') {like} ?)")
            params += [f"%{f['q']}%"] * 8
        for key, col in GEO.items():
            if f[key]:
                where.append(f"l.{col} = ?")
                params.append(f[key])
        for key, col in (("org_type", "l.organisation_type"), ("ownership", "p.ownership_type"),
                         ("rating", "l.current_rating"), ("size_tier", "pc.size_tier"), ("source", "l.source")):
            if f[key]:
                where.append(f"{col} = ?")
                params.append(f[key])
        alias, seg_alias, count_alias = "l", "c", "pc"
    else:
        select = ("p.provider_id, p.name, p.registration_status, p.registration_date, p.town_city, p.postal_code, "
                  "p.region, p.local_authority, p.website, p.main_phone_number, p.organisation_type, p.ownership_type, "
                  "p.current_rating, p.companies_house_number, p.last_report_date, p.source, c.primary_segment, "
                  "c.segments, c.location_count, c.active_location_count, c.total_beds, c.size_tier, c.signals, "
                  "c.lead_score, a.status AS lead_status, a.sales_owner")
        sql = ("FROM cqc_providers p "
               "LEFT JOIN cqc_classifications c ON c.entity_type = 'provider' AND c.entity_id = p.provider_id "
               "LEFT JOIN lead_accounts a ON a.provider_id = p.provider_id")
        where.append(_status_sql("p", f["status"]))
        loc_status = _status_sql("l", f["status"])
        if loc_conds:
            where.append(f"EXISTS (SELECT 1 FROM cqc_locations l WHERE l.provider_id = p.provider_id AND {loc_status} "
                         f"AND {' AND '.join(loc_conds)})")
            params += loc_params
        if f["q"]:
            where.append(f"(p.name {like} ? OR p.provider_id {like} ? OR COALESCE(p.companies_house_number, '') {like} ? "
                         f"OR COALESCE(p.postal_code, '') {like} ? OR COALESCE(p.town_city, '') {like} ? "
                         f"OR COALESCE(p.local_authority, '') {like} ? OR COALESCE(p.also_known_as, '') {like} ? "
                         f"OR EXISTS (SELECT 1 FROM cqc_locations l WHERE l.provider_id = p.provider_id AND "
                         f"(l.name {like} ? OR l.location_id {like} ? OR COALESCE(l.postal_code, '') {like} ? "
                         f"OR COALESCE(l.town_city, '') {like} ?)))")
            params += [f"%{f['q']}%"] * 11
        for key, col in GEO.items():
            if f[key]:
                where.append(f"(p.{col} = ? OR EXISTS (SELECT 1 FROM cqc_locations l WHERE l.provider_id = p.provider_id "
                             f"AND l.{col} = ?))")
                params += [f[key], f[key]]
        if f["rating"]:
            where.append("(p.current_rating = ? OR EXISTS (SELECT 1 FROM cqc_locations l WHERE "
                         "l.provider_id = p.provider_id AND l.current_rating = ?))")
            params += [f["rating"], f["rating"]]
        for key, col in (("org_type", "p.organisation_type"), ("ownership", "p.ownership_type"),
                         ("size_tier", "c.size_tier"), ("source", "p.source")):
            if f[key]:
                where.append(f"{col} = ?")
                params.append(f[key])
        alias, seg_alias, count_alias = "p", "c", "c"

    if f["segment"]:
        where.append(f"{seg_alias}.segments {like} ?")
        params.append(f"%|{f['segment']}|%")
    if f["signal"]:
        where.append(f"{seg_alias}.signals {like} ?")
        params.append(f"%{f['signal']}%")
    if f["inspected"]:
        cutoff = (today - timedelta(days=int(f["inspected"]))).isoformat()
        if view == "locations":
            where.append("l.last_inspection_date >= ?")
            params.append(cutoff)
        else:
            where.append("(p.last_inspection_date >= ? OR EXISTS (SELECT 1 FROM cqc_locations l WHERE "
                         "l.provider_id = p.provider_id AND l.last_inspection_date >= ?))")
            params += [cutoff, cutoff]
    if f["new"]:
        where.append(f"{alias}.registration_date >= ?")
        params.append((today - timedelta(days=int(f["new"]))).isoformat())
    if f["reg_from"]:
        where.append(f"{alias}.registration_date >= ?")
        params.append(f["reg_from"])
    if f["reg_to"]:
        where.append(f"{alias}.registration_date <= ?")
        params.append(f["reg_to"])
    if f["min_locations"]:
        where.append(f"COALESCE({count_alias}.location_count, 0) >= ?")
        params.append(int(f["min_locations"]))
    if f["max_locations"]:
        where.append(f"COALESCE({count_alias}.location_count, 0) <= ?")
        params.append(int(f["max_locations"]))
    if f["lead_status"]:
        where.append("a.status = ?")
        params.append(f["lead_status"])
    if f["in_leads"] == "yes":
        where.append("a.account_id IS NOT NULL")
    elif f["in_leads"] == "no":
        where.append("a.account_id IS NULL")
    return f"{sql} WHERE {' AND '.join(where)}", params, select, notes


def order_by(view: str, sort: str, direction: str) -> tuple[str, str, str]:
    sorts = LOCATION_SORTS if view == "locations" else PROVIDER_SORTS
    sort = sort if sort in sorts else "score"
    direction = "asc" if direction == "asc" else "desc"
    if sort == "name" and direction not in ("asc", "desc"):
        direction = "asc"
    tie = "l.location_id" if view == "locations" else "p.provider_id"
    return f"ORDER BY {sorts[sort]} {direction.upper()}, {tie}", sort, direction


_facet_cache: dict[str, tuple[float, list]] = {}
FACET_SQL = {
    "region": "SELECT DISTINCT region FROM cqc_locations WHERE region IS NOT NULL ORDER BY 1",
    "county": "SELECT DISTINCT county FROM cqc_locations WHERE county IS NOT NULL AND county <> '' ORDER BY 1",
    "la": "SELECT DISTINCT local_authority FROM cqc_locations WHERE local_authority IS NOT NULL ORDER BY 1",
    "icb": "SELECT DISTINCT icb_name FROM cqc_locations WHERE icb_name IS NOT NULL ORDER BY 1",
    "constituency": "SELECT DISTINCT constituency FROM cqc_locations WHERE constituency IS NOT NULL ORDER BY 1",
    "town": "SELECT DISTINCT town_city FROM cqc_locations WHERE town_city IS NOT NULL ORDER BY 1",
    "service_type": "SELECT DISTINCT name FROM cqc_service_types ORDER BY 1",
    "specialism": "SELECT DISTINCT name FROM cqc_specialisms ORDER BY 1",
    "activity": "SELECT DISTINCT name FROM cqc_regulated_activities WHERE name IS NOT NULL ORDER BY 1",
    "org_type": "SELECT DISTINCT organisation_type FROM cqc_providers WHERE organisation_type IS NOT NULL "
                "UNION SELECT DISTINCT organisation_type FROM cqc_locations WHERE organisation_type IS NOT NULL ORDER BY 1",
    "ownership": "SELECT DISTINCT ownership_type FROM cqc_providers WHERE ownership_type IS NOT NULL ORDER BY 1",
    "size_tier": "SELECT DISTINCT size_tier FROM cqc_classifications WHERE size_tier IS NOT NULL ORDER BY 1",
}


def facets(db) -> dict:
    out = {}
    now = time.monotonic()
    for key, sql in FACET_SQL.items():
        hit = _facet_cache.get(key)
        if hit and now - hit[0] < 300:
            out[key] = hit[1]
            continue
        values = [r[0] for r in db.execute(sql)]
        _facet_cache[key] = (now, values)
        out[key] = values
    lt: set = set()
    for (v,) in db.execute("SELECT DISTINCT location_types FROM cqc_locations WHERE location_types IS NOT NULL"):
        lt.update(x for x in v.split("|") if x)
    out["location_type"] = sorted(lt)
    return out


def clear_facet_cache() -> None:
    _facet_cache.clear()
