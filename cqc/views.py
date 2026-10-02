"""Admin UI for CQC market intelligence, prospect search, CRM linkage, lists and the CQC control centre."""
import csv
import io
import json
import os
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import urlencode

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, session, url_for
from openpyxl import Workbook

from . import sync
from .client import CQCClient, CQCError
from .mapping import now_iso, split_piped
from .rules import SEGMENTS, load_rules, reclassify, save_rules, validate_rules
from .sample import clear_sample, load_sample
from .search import (LEAD_STATUSES, NEW_REG_WINDOWS, RATINGS, build_query, clean_filters, clear_facet_cache,
                     facets, order_by)

bp = Blueprint("cqc", __name__)
deps: dict = {}

ENRICH_FIELDS = ["organisation_website", "general_email", "recruitment_email", "telephone", "decision_maker",
                 "decision_maker_role", "decision_maker_email", "linkedin_url", "company_linkedin", "source",
                 "last_verified", "confidence"]
DECISION_ROLES = ["Owner", "Director", "Managing Director", "CEO", "Operations Director", "HR Director",
                  "Recruitment Manager", "Registered Manager", "Regional Manager"]
CONFIDENCE = ["High", "Medium", "Low"]
PER_PAGE = 25
EXPORT_LIMIT = 100_000


def init(app, *, get_db, connect, check_csrf, clean, rate_limited, client_ip, use_pg) -> None:
    deps.update(get_db=get_db, check_csrf=check_csrf, clean=clean, rate_limited=rate_limited,
                client_ip=client_ip, use_pg=use_pg)
    app.register_blueprint(bp)
    sync.init(connect)
    sync.start_scheduler()

    @app.template_filter("pipes")
    def pipes(value):
        return split_piped(value)

    @app.template_filter("fromjson")
    def fromjson(value):
        try:
            return json.loads(value) if value else []
        except ValueError:
            return []


def db():
    return deps["get_db"]()


def role() -> str | None:
    return session.get("role") or ("admin" if session.get("is_admin") else None)


def staff_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if role() not in ("admin", "sales"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


def admin_only(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if role() != "admin":
            if role() is None:
                return redirect(url_for("admin_login", next=request.path))
            abort(403)
        return fn(*args, **kwargs)
    return wrapper


def require_csrf():
    if not deps["check_csrf"](request.form.get("csrf_token", "")):
        abort(400)


def _id(value: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > 40 or not all(ch.isalnum() or ch in "-_" for ch in value):
        abort(404)
    return value


@bp.app_context_processor
def inject():
    return {"staff_role": role(), "lead_statuses": LEAD_STATUSES}


# ---------- Dashboard ----------

def _count(sql, params=()):
    return db().execute(sql, params).fetchone()[0] or 0


@bp.get("/admin/cqc")
@staff_required
def dashboard():
    active_p = "p.registration_status = 'Registered'"
    seg_count = {}
    for seg in ("DOMICILIARY CARE", "RESIDENTIAL CARE HOME", "NURSING HOME", "SUPPORTED LIVING", "COMPLEX CARE",
                "MULTI-SITE CARE GROUP"):
        seg_count[seg] = _count("SELECT COUNT(*) FROM cqc_providers p JOIN cqc_classifications c ON c.entity_type = "
                                f"'provider' AND c.entity_id = p.provider_id WHERE {active_p} AND c.segments LIKE ?",
                                (f"%|{seg}|%",))
    since30 = (date.today() - timedelta(days=30)).isoformat()
    month_start = date.today().replace(day=1).isoformat()
    kpis = [
        ("Active Target Providers", _count("SELECT COUNT(*) FROM cqc_providers p JOIN cqc_classifications c ON "
                                           "c.entity_type = 'provider' AND c.entity_id = p.provider_id WHERE "
                                           f"{active_p} AND c.is_target = 1"), "/admin/cqc/search"),
        ("Active Target Locations", _count("SELECT COUNT(*) FROM cqc_locations l JOIN cqc_classifications c ON "
                                           "c.entity_type = 'location' AND c.entity_id = l.location_id WHERE "
                                           "l.registration_status = 'Registered' AND c.is_target = 1"),
         "/admin/cqc/search?view=locations"),
        ("Domiciliary Care Providers", seg_count["DOMICILIARY CARE"], "/admin/cqc/search?segment=DOMICILIARY+CARE"),
        ("Care Home Providers", _count("SELECT COUNT(*) FROM cqc_providers p JOIN cqc_classifications c ON "
                                       "c.entity_type = 'provider' AND c.entity_id = p.provider_id WHERE "
                                       f"{active_p} AND (c.segments LIKE ? OR c.segments LIKE ?)",
                                       ("%|RESIDENTIAL CARE HOME|%", "%|NURSING HOME|%")), "/admin/cqc/search?care_home=Y"),
        ("Supported Living Providers", seg_count["SUPPORTED LIVING"], "/admin/cqc/search?segment=SUPPORTED+LIVING"),
        ("Complex Care Providers", seg_count["COMPLEX CARE"], "/admin/cqc/search?segment=COMPLEX+CARE"),
        ("Multi-Site Groups", seg_count["MULTI-SITE CARE GROUP"], "/admin/cqc/search?segment=MULTI-SITE+CARE+GROUP"),
        ("New Registrations (30 days)", _count("SELECT COUNT(*) FROM cqc_locations WHERE registration_status = "
                                               "'Registered' AND registration_date >= ?", (since30,)),
         "/admin/cqc/new-registrations?new=30"),
        ("Leads Added This Month", _count("SELECT COUNT(*) FROM lead_accounts WHERE created_at >= ?", (month_start,)),
         "/admin/cqc/leads"),
        ("Leads Contacted", _count("SELECT COUNT(*) FROM lead_accounts WHERE status IN ('CONTACTED', 'FOLLOW-UP', "
                                   "'DEMO BOOKED', 'TRIAL', 'PROPOSAL', 'CUSTOMER')"), "/admin/cqc/leads"),
        ("Demos Booked", _count("SELECT COUNT(*) FROM lead_accounts WHERE status = 'DEMO BOOKED'"),
         "/admin/cqc/leads?status=DEMO+BOOKED"),
        ("Customers", _count("SELECT COUNT(*) FROM lead_accounts WHERE status = 'CUSTOMER'"),
         "/admin/cqc/leads?status=CUSTOMER"),
    ]

    def grouped(sql, params=()):
        return [(r[0] or "Unknown", r[1]) for r in db().execute(sql, params)]

    target_loc = ("FROM cqc_locations l JOIN cqc_classifications c ON c.entity_type = 'location' AND "
                  "c.entity_id = l.location_id WHERE l.registration_status = 'Registered' AND c.is_target = 1")
    segments = []
    for seg in SEGMENTS:
        n = _count("SELECT COUNT(*) FROM cqc_providers p JOIN cqc_classifications c ON c.entity_type = 'provider' AND "
                   f"c.entity_id = p.provider_id WHERE {active_p} AND c.segments LIKE ?", (f"%|{seg}|%",))
        segments.append((seg, n))
    tiers = load_rules(db())["size_tiers"]
    tier_counts = dict(grouped("SELECT c.size_tier, COUNT(*) FROM cqc_providers p JOIN cqc_classifications c ON "
                               "c.entity_type = 'provider' AND c.entity_id = p.provider_id WHERE "
                               f"{active_p} AND c.is_target = 1 GROUP BY c.size_tier"))
    since12 = (date.today().replace(day=1) - timedelta(days=335)).replace(day=1).isoformat()
    monthly = grouped("SELECT SUBSTR(registration_date, 1, 7), COUNT(*) FROM cqc_locations WHERE registration_date >= ? "
                      "GROUP BY SUBSTR(registration_date, 1, 7) ORDER BY 1", (since12,))
    charts = [
        ("Target locations by region", grouped(f"SELECT l.region, COUNT(*) {target_loc} GROUP BY l.region ORDER BY 2 DESC")),
        ("Target locations by service type",
         grouped("SELECT st.name, COUNT(*) FROM cqc_service_types st JOIN cqc_locations l ON l.location_id = st.location_id "
                 "JOIN cqc_classifications c ON c.entity_type = 'location' AND c.entity_id = l.location_id WHERE "
                 "l.registration_status = 'Registered' AND c.is_target = 1 GROUP BY st.name ORDER BY 2 DESC LIMIT 12")),
        ("Target locations by CQC rating", grouped(f"SELECT COALESCE(l.current_rating, 'Not yet rated'), COUNT(*) "
                                                   f"{target_loc} GROUP BY COALESCE(l.current_rating, 'Not yet rated') "
                                                   "ORDER BY 2 DESC")),
        ("Providers by target segment", [s for s in segments if s[1]]),
        ("Target providers by location count", [(t["name"], tier_counts.get(t["name"], 0)) for t in tiers]),
        ("New location registrations by month", monthly),
    ]
    last = db().execute("SELECT * FROM cqc_sync_log WHERE status IN ('success', 'partial') ORDER BY id DESC LIMIT 1").fetchone()
    sample = _count("SELECT COUNT(*) FROM cqc_providers WHERE source = 'SAMPLE'")
    return render_template("cqc/dashboard.html", kpis=kpis, charts=charts, last_sync=last, sample=sample,
                           total=_count("SELECT COUNT(*) FROM cqc_providers"))


# ---------- Search ----------

def _search(args, view, export=False):
    f = clean_filters(args, deps["clean"])
    from_where, params, select, notes = build_query(db(), f, view, deps["use_pg"])
    order, sort, direction = order_by(view, deps["clean"](args.get("sort"), 20), deps["clean"](args.get("dir"), 4))
    total = db().execute(f"SELECT COUNT(*) {from_where}", params).fetchone()[0]
    if export:
        rows = db().execute(f"SELECT {select} {from_where} {order} LIMIT {EXPORT_LIMIT}", params).fetchall()
        return f, rows, total
    page = max(1, int(args.get("page", "1")) if str(args.get("page", "1")).isdigit() else 1)
    rows = db().execute(f"SELECT {select} {from_where} {order} LIMIT {PER_PAGE} OFFSET {(page - 1) * PER_PAGE}",
                        params).fetchall()
    return dict(filters=f, rows=rows, total=total, page=page, pages=max(1, -(-total // PER_PAGE)), sort=sort,
                direction=direction, notes=notes)


def _query_without(*keys, **overrides):
    args = {k: v for k, v in request.args.items() if k not in keys and v}
    args.update({k: v for k, v in overrides.items() if v is not None})
    return urlencode(args)


def _render_search(template_title, preset_new=False):
    view = "locations" if request.args.get("view") == "locations" else "providers"
    ctx = _search(request.args, view)
    lists = db().execute("SELECT list_id, name FROM lead_lists ORDER BY name").fetchall()
    return render_template("cqc/search.html", view=view, facets=facets(db()), segments=SEGMENTS, ratings=RATINGS,
                           windows=NEW_REG_WINDOWS, lists=lists, title=template_title, preset_new=preset_new,
                           qs=_query_without, query_string=request.query_string.decode(), **ctx)


@bp.get("/admin/cqc/search")
@staff_required
def search():
    return _render_search("Search CQC Registered Care Providers")


@bp.get("/admin/cqc/new-registrations")
@staff_required
def new_registrations():
    if not request.args.get("new"):
        args = request.args.to_dict()
        args.setdefault("new", "30")
        args.setdefault("sort", "registered")
        args.setdefault("view", "locations")
        return redirect(url_for("cqc.new_registrations", **args))
    return _render_search("New CQC Registrations", preset_new=True)


# ---------- Export ----------

PROVIDER_EXPORT = [("CQC Provider ID", "provider_id"), ("Provider Name", "name"), ("Target Segment", "primary_segment"),
                   ("All Segments", "segments"), ("Registration Status", "registration_status"),
                   ("Registration Date", "registration_date"), ("Organisation Type", "organisation_type"),
                   ("Ownership Type", "ownership_type"), ("Companies House Number", "companies_house_number"),
                   ("Number of Locations", "location_count"), ("Active Locations", "active_location_count"),
                   ("Location Count Tier", "size_tier"), ("Total Beds", "total_beds"), ("CQC Rating", "current_rating"),
                   ("Last Report Date", "last_report_date"), ("Town/City", "town_city"), ("Postcode", "postal_code"),
                   ("Region", "region"), ("Local Authority", "local_authority"), ("Telephone", "main_phone_number"),
                   ("Website", "website"), ("Veridyn Lead Score", "lead_score"), ("Opportunity Signals", "signals"),
                   ("Internal Lead Status", "lead_status"), ("Sales Owner", "sales_owner"), ("Data Source", "source")]
LOCATION_EXPORT = [("CQC Location ID", "location_id"), ("Location Name", "name"), ("CQC Provider ID", "provider_id"),
                   ("Provider Name", "provider_name"), ("Target Segment", "primary_segment"), ("All Segments", "segments"),
                   ("Registration Status", "registration_status"), ("Registration Date", "registration_date"),
                   ("Service Types", "service_types"), ("Care Home", "care_home"), ("Number of Beds", "number_of_beds"),
                   ("CQC Rating", "current_rating"), ("Last Report Date", "last_report_date"),
                   ("Provider Location Count", "location_count"), ("Location Count Tier", "size_tier"),
                   ("Town/City", "town_city"), ("Postcode", "postal_code"), ("Region", "region"),
                   ("Local Authority", "local_authority"), ("Telephone", "main_phone_number"), ("Website", "website"),
                   ("Veridyn Lead Score", "lead_score"), ("Opportunity Signals", "signals"),
                   ("Internal Lead Status", "lead_status"), ("Data Source", "source")]
CONTACT_EXPORT = [("Decision Maker", "decision_maker"), ("Decision Maker Role", "decision_maker_role"),
                  ("Decision Maker Email", "decision_maker_email"), ("General Email", "general_email"),
                  ("Recruitment Email", "recruitment_email"), ("Contact Telephone", "telephone")]


def _cell(value, key):
    if value is None:
        return ""
    if key in ("segments", "service_types"):
        return ", ".join(split_piped(value))
    if key == "signals":
        try:
            return "; ".join(json.loads(value))
        except ValueError:
            return value
    return value


def _contacts_by_provider(provider_ids):
    out = {}
    ids = list(provider_ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ("SELECT a.provider_id, lc.* FROM lead_contacts lc JOIN lead_accounts a ON a.account_id = lc.account_id "
             f"WHERE a.provider_id IN ({', '.join('?' for _ in chunk)}) ORDER BY lc.created_at")
        for r in db().execute(q, chunk):
            out.setdefault(r["provider_id"], r)
    return out


def _manager_names(contacts_json) -> list[str]:
    try:
        people = json.loads(contacts_json or "[]")
    except ValueError:
        return []
    return [" ".join(v for v in (c.get("personTitle"), c.get("personGivenName"), c.get("personFamilyName")) if v)
            for c in people if isinstance(c, dict) and "Registered Manager" in (c.get("personRoles") or [])]


def _registered_managers(key: str, ids) -> dict:
    """Registered managers named by CQC on active locations, keyed by location_id or provider_id."""
    out: dict = {}
    ids = [i for i in dict.fromkeys(ids) if i]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = ("SELECT l.provider_id, l.location_id, l.name, ra.contacts_json FROM cqc_locations l "
             "JOIN cqc_regulated_activities ra ON ra.entity_type = 'location' AND ra.entity_id = l.location_id "
             f"WHERE l.{key} IN ({', '.join('?' for _ in chunk)}) AND l.registration_status = 'Registered' "
             "AND ra.contacts_json LIKE ? ORDER BY l.name")
        for r in db().execute(q, [*chunk, "%Registered Manager%"]):
            found = out.setdefault(r[key], [])
            for name in _manager_names(r["contacts_json"]):
                entry = {"name": name, "location_id": r["location_id"], "location_name": r["name"]}
                if entry not in found:
                    found.append(entry)
    return out


def _managers_text(entries, with_location: bool) -> str:
    return "; ".join(f"{e['name']} ({e['location_name']})" if with_location else e["name"] for e in entries)


def _export(rows, columns, filename, fmt):
    contacts = _contacts_by_provider({r["provider_id"] for r in rows if r["provider_id"]})
    key = "location_id" if columns is LOCATION_EXPORT else "provider_id"
    managers = _registered_managers(key, [r[key] for r in rows])
    header = [c[0] for c in columns] + ["Registered Manager(s)"] + [c[0] for c in CONTACT_EXPORT]
    data = []
    for r in rows:
        contact = contacts.get(r["provider_id"])
        data.append([_cell(r[k], k) for _, k in columns] +
                    [_managers_text(managers.get(r[key], []), key == "provider_id")] +
                    [(contact[k] or "") if contact else "" for _, k in CONTACT_EXPORT])
    stamp = f"{datetime.now():%Y%m%d-%H%M}"
    if fmt == "xlsx":
        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Veridyn export")
        ws.append(header)
        for row in data:
            ws.append([str(v) if v is not None else "" for v in row])
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={filename}-{stamp}.xlsx"})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in data:
        w.writerow(["'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v for v in row])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}-{stamp}.csv"})


@bp.get("/admin/cqc/export.<fmt>")
@admin_only
def export_search(fmt):
    if fmt not in ("csv", "xlsx"):
        abort(404)
    if deps["rate_limited"](f"export:{deps['client_ip']()}", limit=20, window=300):
        abort(429)
    view = "locations" if request.args.get("view") == "locations" else "providers"
    _, rows, _ = _search(request.args, view, export=True)
    cols = LOCATION_EXPORT if view == "locations" else PROVIDER_EXPORT
    return _export(rows, cols, f"veridyn-cqc-{view}", fmt)


# ---------- Profiles ----------

def _ratings(entity_type, entity_id):
    return db().execute("SELECT * FROM cqc_ratings WHERE entity_type = ? AND entity_id = ? "
                        "ORDER BY is_current DESC, report_date DESC", (entity_type, entity_id)).fetchall()


def _account(provider_id):
    acc = db().execute("SELECT * FROM lead_accounts WHERE provider_id = ?", (provider_id,)).fetchone()
    if not acc:
        return None, [], [], []
    aid = acc["account_id"]
    contacts = db().execute("SELECT * FROM lead_contacts WHERE account_id = ? ORDER BY created_at", (aid,)).fetchall()
    notes = db().execute("SELECT * FROM lead_notes WHERE account_id = ? ORDER BY created_at DESC", (aid,)).fetchall()
    acts = db().execute("SELECT * FROM lead_activities WHERE account_id = ? ORDER BY created_at DESC LIMIT 50",
                        (aid,)).fetchall()
    return acc, contacts, notes, acts


@bp.get("/admin/cqc/providers/<provider_id>")
@staff_required
def provider_profile(provider_id):
    provider_id = _id(provider_id)
    p = db().execute("SELECT * FROM cqc_providers WHERE provider_id = ?", (provider_id,)).fetchone()
    if not p:
        abort(404)
    cls = db().execute("SELECT * FROM cqc_classifications WHERE entity_type = 'provider' AND entity_id = ?",
                       (provider_id,)).fetchone()
    status = request.args.get("loc_status", "all")
    loc_where = {"active": " AND l.registration_status = 'Registered'",
                 "inactive": " AND COALESCE(l.registration_status, '') <> 'Registered'"}.get(status, "")
    locations = db().execute(
        "SELECT l.*, c.primary_segment, c.segments FROM cqc_provider_locations pl "
        "LEFT JOIN cqc_locations l ON l.location_id = pl.location_id "
        "LEFT JOIN cqc_classifications c ON c.entity_type = 'location' AND c.entity_id = pl.location_id "
        f"WHERE pl.provider_id = ?{loc_where} ORDER BY l.registration_status DESC, l.name", (provider_id,)).fetchall()
    linked_ids = [r[0] for r in db().execute("SELECT location_id FROM cqc_provider_locations WHERE provider_id = ?",
                                             (provider_id,))]
    synced_ids = {r["location_id"] for r in locations if r["location_id"]}
    unsynced = [i for i in linked_ids if i not in synced_ids] if status == "all" else []
    acts = db().execute("SELECT * FROM cqc_regulated_activities WHERE entity_type = 'provider' AND entity_id = ?",
                        (provider_id,)).fetchall()
    rels = db().execute("SELECT * FROM cqc_relationships WHERE entity_type = 'provider' AND entity_id = ?",
                        (provider_id,)).fetchall()
    reports = db().execute("SELECT * FROM cqc_reports WHERE entity_type = 'provider' AND entity_id = ? "
                           "ORDER BY report_date DESC", (provider_id,)).fetchall()
    dupes = []
    if p["companies_house_number"]:
        dupes = db().execute("SELECT provider_id, name, registration_status FROM cqc_providers WHERE "
                             "companies_house_number = ? AND provider_id <> ?",
                             (p["companies_house_number"], provider_id)).fetchall()
    acc, contacts, notes, activities = _account(provider_id)
    lists = db().execute("SELECT list_id, name FROM lead_lists ORDER BY name").fetchall()
    raw = json.loads(p["raw_json"] or "{}")
    return render_template("cqc/provider.html", p=p, cls=cls, locations=locations, unsynced=unsynced,
                           activities_cqc=acts, rels=rels, reports=reports, ratings=_ratings("provider", provider_id),
                           dupes=dupes, acc=acc, contacts=contacts, notes=notes, activities=activities, lists=lists,
                           raw=raw, loc_status=status, managers=_registered_managers("location_id", synced_ids),
                           enrich_fields=ENRICH_FIELDS, decision_roles=DECISION_ROLES,
                           confidence=CONFIDENCE)


@bp.get("/admin/cqc/locations/<location_id>")
@staff_required
def location_profile(location_id):
    location_id = _id(location_id)
    loc = db().execute("SELECT * FROM cqc_locations WHERE location_id = ?", (location_id,)).fetchone()
    if not loc:
        abort(404)
    provider = db().execute("SELECT provider_id, name, registration_status FROM cqc_providers WHERE provider_id = ?",
                            (loc["provider_id"],)).fetchone()
    cls = db().execute("SELECT * FROM cqc_classifications WHERE entity_type = 'location' AND entity_id = ?",
                       (location_id,)).fetchone()
    pcls = db().execute("SELECT * FROM cqc_classifications WHERE entity_type = 'provider' AND entity_id = ?",
                        (loc["provider_id"],)).fetchone()
    services = db().execute("SELECT * FROM cqc_service_types WHERE location_id = ?", (location_id,)).fetchall()
    acts = db().execute("SELECT * FROM cqc_regulated_activities WHERE entity_type = 'location' AND entity_id = ?",
                        (location_id,)).fetchall()
    rels = db().execute("SELECT * FROM cqc_relationships WHERE entity_type = 'location' AND entity_id = ?",
                        (location_id,)).fetchall()
    reports = db().execute("SELECT * FROM cqc_reports WHERE entity_type = 'location' AND entity_id = ? "
                           "ORDER BY report_date DESC", (location_id,)).fetchall()
    siblings = db().execute("SELECT location_id, name, registration_status, town_city FROM cqc_locations WHERE "
                            "provider_id = ? AND location_id <> ? ORDER BY name LIMIT 50",
                            (loc["provider_id"], location_id)).fetchall()
    acc = db().execute("SELECT * FROM lead_accounts WHERE provider_id = ?", (loc["provider_id"],)).fetchone()
    raw = json.loads(loc["raw_json"] or "{}")
    return render_template("cqc/location.html", loc=loc, provider=provider, cls=cls, pcls=pcls, services=services,
                           activities_cqc=acts, rels=rels, reports=reports, siblings=siblings,
                           ratings=_ratings("location", location_id), acc=acc, raw=raw,
                           managers=list(dict.fromkeys(n for a in acts for n in _manager_names(a["contacts_json"]))))


@bp.post("/admin/cqc/<entity>/<entity_id>/refresh")
@staff_required
def refresh_entity(entity, entity_id):
    require_csrf()
    if entity not in ("providers", "locations"):
        abort(404)
    entity_id = _id(entity_id)
    target = url_for("cqc.provider_profile" if entity == "providers" else "cqc.location_profile",
                     **{("provider_id" if entity == "providers" else "location_id"): entity_id})
    if entity_id.startswith("SAMPLE-"):
        flash("Sample records are not in the CQC API and cannot be refreshed.", "error")
        return redirect(target)
    try:
        result = sync.run_job(entity[:-1], entity_id)
        flash(f"Refresh {result['status']}: {result['message']}", "ok" if result["status"] == "success" else "error")
    except RuntimeError as exc:
        flash(str(exc), "error")
    clear_facet_cache()
    return redirect(target)


# ---------- CRM ----------

def _log_activity(account_id, activity, detail=None):
    db().execute("INSERT INTO lead_activities VALUES (?, ?, ?, ?, ?, ?)",
                 (secrets.token_hex(8), account_id, activity, detail, session.get("username") or role(), now_iso()))


def _ensure_account(provider_id):
    acc = db().execute("SELECT account_id FROM lead_accounts WHERE provider_id = ?", (provider_id,)).fetchone()
    if acc:
        return acc[0], False
    aid = secrets.token_hex(8)
    db().execute("INSERT INTO lead_accounts (account_id, provider_id, status, created_at, updated_at) "
                 "VALUES (?, ?, 'NEW', ?, ?) ON CONFLICT (provider_id) DO NOTHING",
                 (aid, provider_id, now_iso(), now_iso()))
    _log_activity(aid, "Added to leads")
    return aid, True


@bp.post("/admin/cqc/providers/<provider_id>/lead")
@staff_required
def add_to_leads(provider_id):
    require_csrf()
    provider_id = _id(provider_id)
    if not db().execute("SELECT 1 FROM cqc_providers WHERE provider_id = ?", (provider_id,)).fetchone():
        abort(404)
    _, created = _ensure_account(provider_id)
    db().commit()
    flash("Added to leads." if created else "This provider is already a lead.", "ok")
    return redirect(request.referrer or url_for("cqc.provider_profile", provider_id=provider_id))


def _account_or_404(account_id):
    acc = db().execute("SELECT * FROM lead_accounts WHERE account_id = ?", (_id(account_id),)).fetchone()
    if not acc:
        abort(404)
    return acc


@bp.post("/admin/cqc/leads/<account_id>/update")
@staff_required
def update_lead(account_id):
    require_csrf()
    acc = _account_or_404(account_id)
    clean = deps["clean"]
    status = request.form.get("status", acc["status"])
    if status not in LEAD_STATUSES:
        abort(400)
    owner = clean(request.form.get("sales_owner", acc["sales_owner"] or ""), 80)
    tags = clean(request.form.get("tags", acc["tags"] or ""), 200)
    db().execute("UPDATE lead_accounts SET status = ?, sales_owner = ?, tags = ?, updated_at = ? WHERE account_id = ?",
                 (status, owner or None, tags or None, now_iso(), acc["account_id"]))
    if status != acc["status"]:
        _log_activity(acc["account_id"], "Status changed", f"{acc['status']} → {status}")
    db().commit()
    return redirect(request.referrer or url_for("cqc.leads"))


@bp.post("/admin/cqc/leads/<account_id>/notes")
@staff_required
def add_note(account_id):
    require_csrf()
    acc = _account_or_404(account_id)
    body = (request.form.get("body") or "").strip()[:4000]
    if body:
        db().execute("INSERT INTO lead_notes VALUES (?, ?, ?, ?, ?)",
                     (secrets.token_hex(8), acc["account_id"], body, session.get("username") or role(), now_iso()))
        _log_activity(acc["account_id"], "Note added")
        db().commit()
    return redirect(url_for("cqc.provider_profile", provider_id=acc["provider_id"]) + "#crm")


@bp.post("/admin/cqc/leads/<account_id>/contacts")
@staff_required
def add_contact(account_id):
    require_csrf()
    acc = _account_or_404(account_id)
    clean = deps["clean"]
    values = {k: clean(request.form.get(k), 200) or None for k in ENRICH_FIELDS}
    if values["confidence"] and values["confidence"] not in CONFIDENCE:
        values["confidence"] = None
    if values["last_verified"]:
        try:
            date.fromisoformat(values["last_verified"])
        except ValueError:
            values["last_verified"] = None
    for k in ("general_email", "recruitment_email", "decision_maker_email"):
        if values[k] and "@" not in values[k]:
            flash(f"{k.replace('_', ' ').title()} is not a valid email address.", "error")
            return redirect(url_for("cqc.provider_profile", provider_id=acc["provider_id"]) + "#crm")
    if any(values.values()):
        cols = ["contact_id", "account_id", *ENRICH_FIELDS, "created_at", "updated_at"]
        db().execute(f"INSERT INTO lead_contacts ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                     [secrets.token_hex(8), acc["account_id"], *values.values(), now_iso(), now_iso()])
        _log_activity(acc["account_id"], "Contact enrichment added", values["decision_maker"] or values["general_email"])
        if acc["status"] in ("NEW", "RESEARCHING") and (values["decision_maker_email"] or values["general_email"]
                                                         or values["recruitment_email"]):
            db().execute("UPDATE lead_accounts SET status = 'CONTACT FOUND', updated_at = ? WHERE account_id = ?",
                         (now_iso(), acc["account_id"]))
        db().commit()
    return redirect(url_for("cqc.provider_profile", provider_id=acc["provider_id"]) + "#crm")


@bp.post("/admin/cqc/contacts/<contact_id>/delete")
@staff_required
def delete_contact(contact_id):
    require_csrf()
    row = db().execute("SELECT a.provider_id FROM lead_contacts c JOIN lead_accounts a ON a.account_id = c.account_id "
                       "WHERE c.contact_id = ?", (_id(contact_id),)).fetchone()
    if not row:
        abort(404)
    db().execute("DELETE FROM lead_contacts WHERE contact_id = ?", (contact_id,))
    db().commit()
    return redirect(url_for("cqc.provider_profile", provider_id=row[0]) + "#crm")


@bp.get("/admin/cqc/leads")
@staff_required
def leads():
    status = request.args.get("status", "")
    owner = deps["clean"](request.args.get("owner"), 80)
    sql = ("SELECT a.*, p.name, p.town_city, p.region, p.registration_status, c.primary_segment, c.location_count, "
           "c.size_tier, c.lead_score FROM lead_accounts a LEFT JOIN cqc_providers p ON p.provider_id = a.provider_id "
           "LEFT JOIN cqc_classifications c ON c.entity_type = 'provider' AND c.entity_id = a.provider_id WHERE 1=1")
    params = []
    if status in LEAD_STATUSES:
        sql += " AND a.status = ?"
        params.append(status)
    if owner:
        sql += " AND a.sales_owner = ?"
        params.append(owner)
    rows = db().execute(sql + " ORDER BY a.updated_at DESC LIMIT 500", params).fetchall()
    counts = {r[0]: r[1] for r in db().execute("SELECT status, COUNT(*) FROM lead_accounts GROUP BY status")}
    return render_template("cqc/leads.html", rows=rows, counts=counts, status=status, owner=owner,
                           managers=_registered_managers("provider_id", [r["provider_id"] for r in rows]))


# ---------- Lead lists ----------

@bp.post("/admin/cqc/lists/add")
@staff_required
def add_to_list():
    require_csrf()
    ids = [_id(i) for i in request.form.getlist("provider_ids")][:1000]
    if not ids:
        flash("Select at least one provider first.", "error")
        return redirect(request.referrer or url_for("cqc.search"))
    list_id = request.form.get("list_id", "")
    name = deps["clean"](request.form.get("new_list"), 80)
    if name:
        list_id = secrets.token_hex(8)
        db().execute("INSERT INTO lead_lists VALUES (?, ?, ?)", (list_id, name, now_iso()))
    elif not db().execute("SELECT 1 FROM lead_lists WHERE list_id = ?", (list_id,)).fetchone():
        flash("Choose a list or enter a new list name.", "error")
        return redirect(request.referrer or url_for("cqc.search"))
    for pid in ids:
        db().execute("INSERT INTO lead_list_members VALUES (?, ?, ?) ON CONFLICT DO NOTHING", (list_id, pid, now_iso()))
    if request.form.get("also_leads"):
        for pid in ids:
            _ensure_account(pid)
    db().commit()
    flash(f"Added {len(ids)} provider(s) to the list.", "ok")
    return redirect(url_for("cqc.list_detail", list_id=list_id))


@bp.get("/admin/cqc/lists")
@staff_required
def lists():
    rows = db().execute("SELECT l.*, (SELECT COUNT(*) FROM lead_list_members m WHERE m.list_id = l.list_id) AS members "
                        "FROM lead_lists l ORDER BY l.name").fetchall()
    return render_template("cqc/lists.html", rows=rows)


def _list_rows(list_id):
    return db().execute(
        "SELECT p.provider_id, p.name, p.registration_status, p.registration_date, p.organisation_type, p.ownership_type, "
        "p.companies_house_number, p.current_rating, p.last_report_date, p.town_city, p.postal_code, p.region, "
        "p.local_authority, p.main_phone_number, p.website, p.source, c.primary_segment, c.segments, c.location_count, "
        "c.active_location_count, c.size_tier, c.total_beds, c.lead_score, c.signals, a.status AS lead_status, "
        "a.sales_owner FROM lead_list_members m JOIN cqc_providers p ON p.provider_id = m.provider_id "
        "LEFT JOIN cqc_classifications c ON c.entity_type = 'provider' AND c.entity_id = p.provider_id "
        "LEFT JOIN lead_accounts a ON a.provider_id = p.provider_id WHERE m.list_id = ? ORDER BY p.name",
        (list_id,)).fetchall()


@bp.get("/admin/cqc/lists/<list_id>")
@staff_required
def list_detail(list_id):
    lst = db().execute("SELECT * FROM lead_lists WHERE list_id = ?", (_id(list_id),)).fetchone()
    if not lst:
        abort(404)
    rows = _list_rows(list_id)
    return render_template("cqc/list_detail.html", lst=lst, rows=rows,
                           managers=_registered_managers("provider_id", [r["provider_id"] for r in rows]))


@bp.get("/admin/cqc/lists/<list_id>/export.<fmt>")
@admin_only
def list_export(list_id, fmt):
    lst = db().execute("SELECT * FROM lead_lists WHERE list_id = ?", (_id(list_id),)).fetchone()
    if not lst or fmt not in ("csv", "xlsx"):
        abort(404)
    return _export(_list_rows(list_id), PROVIDER_EXPORT, "veridyn-list", fmt)


@bp.post("/admin/cqc/lists/<list_id>/remove")
@staff_required
def list_remove(list_id):
    require_csrf()
    db().execute("DELETE FROM lead_list_members WHERE list_id = ? AND provider_id = ?",
                 (_id(list_id), _id(request.form.get("provider_id", ""))))
    db().commit()
    return redirect(url_for("cqc.list_detail", list_id=list_id))


@bp.post("/admin/cqc/lists/<list_id>/delete")
@staff_required
def list_delete(list_id):
    require_csrf()
    db().execute("DELETE FROM lead_list_members WHERE list_id = ?", (_id(list_id),))
    db().execute("DELETE FROM lead_lists WHERE list_id = ?", (list_id,))
    db().commit()
    return redirect(url_for("cqc.lists"))


# ---------- Saved searches ----------

@bp.get("/admin/cqc/saved-searches")
@staff_required
def saved_searches():
    rows = db().execute("SELECT * FROM saved_searches ORDER BY name").fetchall()
    return render_template("cqc/saved_searches.html", rows=rows)


@bp.post("/admin/cqc/saved-searches")
@staff_required
def save_search():
    require_csrf()
    name = deps["clean"](request.form.get("name"), 80)
    query = (request.form.get("query") or "").strip()[:2000].lstrip("?")
    if not name:
        flash("Give the search a name.", "error")
        return redirect(request.referrer or url_for("cqc.search"))
    db().execute("INSERT INTO saved_searches VALUES (?, ?, ?, ?)", (secrets.token_hex(8), name, query, now_iso()))
    db().commit()
    flash(f"Saved search “{name}”.", "ok")
    return redirect(url_for("cqc.saved_searches"))


@bp.post("/admin/cqc/saved-searches/<search_id>/delete")
@staff_required
def delete_search(search_id):
    require_csrf()
    db().execute("DELETE FROM saved_searches WHERE search_id = ?", (_id(search_id),))
    db().commit()
    return redirect(url_for("cqc.saved_searches"))


# ---------- Settings > Integrations > CQC ----------

@bp.get("/admin/settings/integrations/cqc")
@admin_only
def control_centre():
    client = CQCClient()
    stats = {
        "providers": _count("SELECT COUNT(*) FROM cqc_providers WHERE source = 'CQC'"),
        "active_providers": _count("SELECT COUNT(*) FROM cqc_providers WHERE source = 'CQC' AND registration_status = 'Registered'"),
        "locations": _count("SELECT COUNT(*) FROM cqc_locations WHERE source = 'CQC'"),
        "active_locations": _count("SELECT COUNT(*) FROM cqc_locations WHERE source = 'CQC' AND registration_status = 'Registered'"),
        "failed": _count("SELECT COUNT(*) FROM cqc_sync_failures"),
        "sample": _count("SELECT COUNT(*) FROM cqc_providers WHERE source = 'SAMPLE'") +
                  _count("SELECT COUNT(*) FROM cqc_locations WHERE source = 'SAMPLE'"),
    }
    last_ok = db().execute("SELECT * FROM cqc_sync_log WHERE status IN ('success', 'partial') ORDER BY id DESC LIMIT 1").fetchone()
    errors = db().execute("SELECT * FROM cqc_sync_log WHERE status = 'failed' ORDER BY id DESC LIMIT 5").fetchall()
    failures = db().execute("SELECT * FROM cqc_sync_failures ORDER BY last_attempt_at DESC LIMIT 20").fetchall()
    log_rows = db().execute("SELECT * FROM cqc_sync_log ORDER BY id DESC LIMIT 25").fetchall()
    dupes = db().execute("SELECT companies_house_number, COUNT(*) AS n FROM cqc_providers WHERE companies_house_number "
                         "IS NOT NULL AND companies_house_number <> '' GROUP BY companies_house_number HAVING COUNT(*) > 1 "
                         "ORDER BY n DESC LIMIT 50").fetchall()
    dupe_detail = {}
    for d in dupes:
        dupe_detail[d[0]] = db().execute("SELECT provider_id, name, registration_status FROM cqc_providers WHERE "
                                         "companies_house_number = ?", (d[0],)).fetchall()
    rules = load_rules(db())
    return render_template("cqc/control_centre.html", connected=client.configured, base_url=client.base_url,
                           stats=stats, last_ok=last_ok, errors=errors, failures=failures, log_rows=log_rows,
                           running=sync.is_running(), job_types=sync.JOB_TYPES, dupes=dupe_detail,
                           rules_json=json.dumps(rules, indent=2), segments=SEGMENTS,
                           auto_hours=os.environ.get("CQC_AUTO_SYNC_HOURS", "24"))


@bp.post("/admin/settings/integrations/cqc/action")
@admin_only
def control_action():
    require_csrf()
    action = request.form.get("action", "")
    back = url_for("cqc.control_centre")
    if action == "test":
        try:
            info = CQCClient(max_retries=1).test_connection()
            flash(f"Connected to the CQC API. CQC reports {info['total_providers']:,} providers.", "ok")
        except CQCError as exc:
            flash(f"Connection failed: {exc}", "error")
    elif action in ("full", "providers", "locations", "incremental", "retry"):
        if not CQCClient().configured:
            flash("Add the CQC_API_KEY secret before syncing.", "error")
        else:
            try:
                sync.start_job(action)
                flash(f"Started: {sync.JOB_TYPES[action]}. Progress appears in the sync log below.", "ok")
            except RuntimeError as exc:
                flash(str(exc), "error")
    elif action == "sample_load":
        n = load_sample(db())
        clear_facet_cache()
        flash(f"Loaded {n} fictional sample records (IDs start with SAMPLE-).", "ok")
    elif action == "sample_clear":
        clear_sample(db())
        clear_facet_cache()
        flash("Sample records removed.", "ok")
    elif action == "reclassify":
        n = reclassify(db())
        flash(f"Reclassified {n} records.", "ok")
    else:
        abort(400)
    return redirect(back)


@bp.post("/admin/settings/integrations/cqc/rules")
@admin_only
def save_rules_view():
    require_csrf()
    try:
        rules = json.loads(request.form.get("rules") or "")
        if not isinstance(rules, dict):
            raise ValueError("Rules must be a JSON object")
    except ValueError as exc:
        flash(f"Rules not saved: invalid JSON ({exc}).", "error")
        return redirect(url_for("cqc.control_centre") + "#rules")
    errors = validate_rules(rules)
    if errors:
        flash("Rules not saved: " + "; ".join(errors[:5]), "error")
        return redirect(url_for("cqc.control_centre") + "#rules")
    save_rules(db(), rules)
    db().commit()
    n = reclassify(db())
    clear_facet_cache()
    flash(f"Rules saved and {n} records reclassified.", "ok")
    return redirect(url_for("cqc.control_centre") + "#rules")


@bp.post("/admin/settings/integrations/cqc/rules/reset")
@admin_only
def reset_rules():
    require_csrf()
    db().execute("DELETE FROM cqc_settings WHERE key = 'rules'")
    db().commit()
    reclassify(db())
    flash("Rules reset to defaults.", "ok")
    return redirect(url_for("cqc.control_centre") + "#rules")
