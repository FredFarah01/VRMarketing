import csv
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import threading
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

from checklist_pdf import build_checklist_pdf
from cqc_sales_schema import CQC_SALES_MIGRATION, CQC_SALES_PG_MIGRATION

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = Path(os.environ.get("VERIDYN_DB", BASE_DIR / "instance" / "marketing.db"))
PDF_PATH = BASE_DIR / "instance" / "2026-care-recruitment-compliance-checklist.pdf"

LEAD_MAGNET = "2026 Care Recruitment Compliance Checklist"
LANDING_PATH = "/care-recruitment-compliance-checklist"

JOB_ROLES = ["Owner / Director", "Registered Manager", "Recruitment Manager", "Compliance Manager",
             "HR", "Operations", "Administrator", "Other"]
ORG_SIZES = ["1–25", "26–50", "51–100", "101–250", "251–500", "500+"]
LEAD_STATUSES = ["New Lead", "Contacted", "Demo Requested", "Demo Booked", "Qualified", "Customer",
                 "Not Interested"]
ANALYTICS_EVENTS = {"landing_page_view", "checklist_cta_clicked", "lead_form_started",
                    "lead_form_submitted", "checklist_downloaded", "product_section_viewed",
                    "demo_cta_clicked", "demo_form_submitted", "pricing_page_viewed"}
UTM_KEYS = ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"]

SCORING_RULES = {
    "downloaded_checklist": 10,
    "business_email": 5,
    "size_51_plus": 10,
    "size_101_plus": 15,
    "roles": {"Owner / Director": 10, "Registered Manager": 8, "Recruitment Manager": 8,
              "Compliance Manager": 8},
    "visited_pricing": 10,
    "clicked_demo": 20,
    "submitted_demo": 30,
    "thresholds": {"Hot": 50, "Warm": 25},
}
FREE_EMAIL_DOMAINS = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
                      "hotmail.co.uk", "outlook.com", "live.com", "live.co.uk", "icloud.com", "me.com",
                      "aol.com", "btinternet.com", "sky.com", "virginmedia.com", "protonmail.com",
                      "proton.me", "mail.com", "gmx.com", "msn.com"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "veridyn-admin")

_rate_buckets: dict[str, deque] = defaultdict(deque)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PgRow(dict):
    """Row supporting both column-name and positional access, like sqlite3.Row."""

    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self._values = values

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else super().__getitem__(key)


def pg_row_factory(cursor):
    cols = [c.name for c in cursor.description or []]
    return lambda values: PgRow(cols, values)


class PgConnection:
    """Minimal sqlite3-style wrapper so the same SQL (with ? placeholders) runs on Postgres/Supabase."""

    def __init__(self, url: str):
        self.conn = psycopg.connect(url, row_factory=pg_row_factory, prepare_threshold=None, connect_timeout=10)

    def execute(self, sql: str, params=()):
        return self.conn.execute(sql.replace("?", "%s"), params)

    def executescript(self, sql: str):
        self.conn.execute(sql)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def connect():
    if USE_PG:
        return PgConnection(DATABASE_URL)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


CQC_RESUME_MIGRATION = """
ALTER TABLE cqc_sync_runs ADD COLUMN IF NOT EXISTS phase TEXT DEFAULT 'providers';
ALTER TABLE cqc_sync_runs ADD COLUMN IF NOT EXISTS provider_page INTEGER DEFAULT 1;
ALTER TABLE cqc_sync_runs ADD COLUMN IF NOT EXISTS location_page INTEGER DEFAULT 1;
"""

CQC_MANAGER_MIGRATION = """
CREATE TABLE IF NOT EXISTS cqc_registered_managers (
    location_id TEXT NOT NULL,
    manager_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (location_id, manager_name)
);
CREATE INDEX IF NOT EXISTS idx_cqc_managers_location ON cqc_registered_managers(location_id);
"""

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS marketing_leads (
        lead_id TEXT PRIMARY KEY,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT NOT NULL,
        organisation TEXT NOT NULL,
        job_role TEXT NOT NULL,
        organisation_size TEXT,
        lead_magnet TEXT NOT NULL,
        marketing_consent INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        source TEXT,
        utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT, utm_term TEXT,
        landing_page TEXT,
        status TEXT NOT NULL DEFAULT 'New Lead',
        lead_score INTEGER NOT NULL DEFAULT 0,
        lead_temperature TEXT NOT NULL DEFAULT 'Cold',
        visitor_id TEXT,
        checklist_downloaded INTEGER NOT NULL DEFAULT 0,
        clicked_demo INTEGER NOT NULL DEFAULT 0,
        visited_pricing INTEGER NOT NULL DEFAULT 0,
        demo_submitted INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_marketing_leads_email ON marketing_leads(email);
    CREATE INDEX IF NOT EXISTS idx_marketing_leads_created ON marketing_leads(created_at);
    CREATE TABLE IF NOT EXISTS marketing_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_name TEXT NOT NULL,
        visitor_id TEXT,
        lead_id TEXT,
        page TEXT,
        utm_source TEXT, utm_medium TEXT, utm_campaign TEXT, utm_content TEXT, utm_term TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_marketing_events_name ON marketing_events(event_name);
    CREATE TABLE IF NOT EXISTS marketing_demo_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id TEXT,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        email TEXT NOT NULL,
        organisation TEXT NOT NULL,
        phone TEXT,
        message TEXT,
        created_at TEXT NOT NULL
    );
    """,
    CQC_SALES_MIGRATION,
    CQC_MANAGER_MIGRATION,
    CQC_RESUME_MIGRATION,
]

PG_MIGRATIONS = [
    MIGRATIONS[0].replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY") + """
    ALTER TABLE marketing_leads ENABLE ROW LEVEL SECURITY;
    ALTER TABLE marketing_events ENABLE ROW LEVEL SECURITY;
    ALTER TABLE marketing_demo_requests ENABLE ROW LEVEL SECURITY;
    ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
    """,
    CQC_SALES_PG_MIGRATION,
    CQC_MANAGER_MIGRATION,
    CQC_RESUME_MIGRATION,
]


def migrate() -> None:
    conn = connect()
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    for version, sql in enumerate(PG_MIGRATIONS if USE_PG else MIGRATIONS, start=1):
        if version not in applied:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, now_iso()))
    conn.commit()
    conn.close()


def is_business_email(email: str) -> bool:
    return email.rsplit("@", 1)[-1].lower() not in FREE_EMAIL_DOMAINS


def score_lead(lead) -> tuple[int, str]:
    r = SCORING_RULES
    score = 0
    if lead["checklist_downloaded"]:
        score += r["downloaded_checklist"]
    if is_business_email(lead["email"]):
        score += r["business_email"]
    size = lead["organisation_size"] or ""
    if size in ("101–250", "251–500", "500+"):
        score += r["size_101_plus"]
    elif size == "51–100":
        score += r["size_51_plus"]
    score += r["roles"].get(lead["job_role"], 0)
    if lead["visited_pricing"]:
        score += r["visited_pricing"]
    if lead["clicked_demo"]:
        score += r["clicked_demo"]
    if lead["demo_submitted"]:
        score += r["submitted_demo"]
    temp = "Cold"
    if score >= r["thresholds"]["Hot"]:
        temp = "Hot"
    elif score >= r["thresholds"]["Warm"]:
        temp = "Warm"
    return score, temp


def rescore(db, lead_id: str) -> None:
    lead = db.execute("SELECT * FROM marketing_leads WHERE lead_id = ?", (lead_id,)).fetchone()
    if lead:
        score, temp = score_lead(lead)
        db.execute("UPDATE marketing_leads SET lead_score=?, lead_temperature=?, updated_at=? WHERE lead_id=?",
                   (score, temp, now_iso(), lead_id))


def rate_limited(key: str, limit: int, window: int) -> bool:
    bucket = _rate_buckets[key]
    t = time.time()
    while bucket and bucket[0] < t - window:
        bucket.popleft()
    if len(bucket) >= limit:
        return True
    bucket.append(t)
    return False


def client_ip() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


def clean(value, max_len: int = 120) -> str:
    return (value or "").strip()[:max_len] if isinstance(value, str) else ""


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf(token: str) -> bool:
    return bool(token) and secrets.compare_digest(token, session.get("csrf", ""))


app.jinja_env.globals.update(csrf_token=csrf_token)


def log_event(db, name, visitor_id=None, lead_id=None, page=None, utm=None):
    utm = utm or {}
    db.execute(
        "INSERT INTO marketing_events (event_name, visitor_id, lead_id, page, utm_source, utm_medium, "
        "utm_campaign, utm_content, utm_term, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (name, visitor_id, lead_id, page, *(clean(utm.get(k), 200) or None for k in UTM_KEYS), now_iso()))


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    if request.path.startswith("/admin/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ---------- Public pages ----------

@app.get("/")
def index():
    return redirect(LANDING_PATH)


@app.get(LANDING_PATH)
def landing():
    session["form_rendered_at"] = time.time()
    return render_template("landing.html", job_roles=JOB_ROLES, org_sizes=ORG_SIZES,
                           canonical=request.url_root.rstrip("/") + LANDING_PATH)


@app.get("/resources/care-recruitment-checklist/thank-you")
def thank_you():
    return render_template("thank_you.html", has_lead=bool(session.get("lead_id")))


@app.get("/resources/care-recruitment-checklist/download")
def download_checklist():
    lead_id = session.get("lead_id")
    if not lead_id:
        return redirect(LANDING_PATH + "#get-checklist")
    db = get_db()
    db.execute("UPDATE marketing_leads SET checklist_downloaded=1 WHERE lead_id=?", (lead_id,))
    rescore(db, lead_id)
    log_event(db, "checklist_downloaded", session.get("visitor_id"), lead_id, request.path)
    db.commit()
    if not PDF_PATH.exists():
        build_checklist_pdf(PDF_PATH)
    return send_file(PDF_PATH, as_attachment=True, download_name="2026-Care-Recruitment-Compliance-Checklist.pdf",
                     mimetype="application/pdf")


@app.get("/demo")
def demo():
    session["form_rendered_at"] = time.time()
    return render_template("demo.html")


@app.get("/privacy")
def privacy():
    return render_template("legal.html", title="Privacy Policy", kind="privacy")


@app.get("/terms")
def terms():
    return render_template("legal.html", title="Terms", kind="terms")


@app.get("/login")
def login_placeholder():
    return redirect(url_for("admin_login"))


# ---------- APIs ----------

def validate_lead(data: dict) -> tuple[dict, dict]:
    errors = {}
    lead = {
        "first_name": clean(data.get("first_name"), 80),
        "last_name": clean(data.get("last_name"), 80),
        "email": clean(data.get("email"), 254).lower(),
        "organisation": clean(data.get("organisation"), 160),
        "job_role": clean(data.get("job_role"), 60),
        "organisation_size": clean(data.get("organisation_size"), 20) or None,
    }
    if not lead["first_name"]:
        errors["first_name"] = "Please enter your first name."
    if not lead["last_name"]:
        errors["last_name"] = "Please enter your last name."
    if not lead["email"]:
        errors["email"] = "Please enter your work email."
    elif not EMAIL_RE.match(lead["email"]):
        errors["email"] = "Please enter a valid email address, e.g. name@organisation.co.uk."
    if not lead["organisation"]:
        errors["organisation"] = "Please enter your organisation name."
    if lead["job_role"] not in JOB_ROLES:
        errors["job_role"] = "Please select your job role."
    if lead["organisation_size"] and lead["organisation_size"] not in ORG_SIZES:
        errors["organisation_size"] = "Please select a valid option."
    if data.get("consent") not in (True, "true", "on", "1", 1):
        errors["consent"] = "Please confirm you agree to receive the requested resource."
    return lead, errors


def spam_check(data: dict) -> str | None:
    if clean(data.get("website")):
        return "honeypot"
    rendered = session.get("form_rendered_at")
    if rendered is None or time.time() - rendered < 2:
        return "too_fast"
    if rate_limited(f"lead:{client_ip()}", limit=5, window=600):
        return "rate"
    return None


@app.post("/api/leads")
def create_lead():
    data = request.get_json(silent=True) or request.form.to_dict()
    if not check_csrf(data.get("csrf_token", "")):
        return jsonify(ok=False, errors={"form": "Your session has expired. Please refresh the page and try again."}), 400
    spam = spam_check(data)
    if spam == "rate":
        return jsonify(ok=False, errors={"form": "Too many submissions. Please try again in a few minutes."}), 429
    if spam:
        return jsonify(ok=False, errors={"form": "We couldn't process your request. Please try again."}), 400
    lead, errors = validate_lead(data)
    if errors:
        return jsonify(ok=False, errors=errors), 422

    utm = {k: clean(data.get(k), 200) or None for k in UTM_KEYS}
    visitor_id = clean(data.get("visitor_id"), 64) or None
    lead_id = str(uuid.uuid4())
    source = utm["utm_source"] or clean(data.get("referrer"), 300) or "direct"
    db = get_db()
    db.execute(
        "INSERT INTO marketing_leads (lead_id, first_name, last_name, email, organisation, job_role, "
        "organisation_size, lead_magnet, marketing_consent, created_at, source, utm_source, utm_medium, "
        "utm_campaign, utm_content, utm_term, landing_page, status, visitor_id, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lead_id, lead["first_name"], lead["last_name"], lead["email"], lead["organisation"], lead["job_role"],
         lead["organisation_size"], LEAD_MAGNET, 1, now_iso(), source, *utm.values(),
         clean(data.get("landing_page"), 300) or LANDING_PATH, "New Lead", visitor_id, now_iso()))
    if visitor_id:
        db.execute("UPDATE marketing_events SET lead_id=? WHERE visitor_id=? AND lead_id IS NULL", (lead_id, visitor_id))
        prior = {r[0] for r in db.execute("SELECT DISTINCT event_name FROM marketing_events WHERE visitor_id=?", (visitor_id,))}
        db.execute("UPDATE marketing_leads SET clicked_demo=?, visited_pricing=? WHERE lead_id=?",
                   (int("demo_cta_clicked" in prior), int("pricing_page_viewed" in prior), lead_id))
    log_event(db, "lead_form_submitted", visitor_id, lead_id, LANDING_PATH, utm)
    rescore(db, lead_id)
    db.commit()
    session["lead_id"] = lead_id
    session["visitor_id"] = visitor_id
    return jsonify(ok=True, redirect="/resources/care-recruitment-checklist/thank-you")


@app.post("/api/events")
def track_event():
    data = request.get_json(silent=True) or {}
    name = clean(data.get("event"), 60)
    if name not in ANALYTICS_EVENTS or name == "lead_form_submitted":
        return jsonify(ok=False), 400
    if rate_limited(f"evt:{client_ip()}", limit=120, window=60):
        return jsonify(ok=False), 429
    visitor_id = clean(data.get("visitor_id"), 64) or None
    lead_id = session.get("lead_id")
    db = get_db()
    log_event(db, name, visitor_id, lead_id, clean(data.get("page"), 300), data.get("utm") or {})
    if lead_id and name in ("demo_cta_clicked", "pricing_page_viewed"):
        col = "clicked_demo" if name == "demo_cta_clicked" else "visited_pricing"
        db.execute(f"UPDATE marketing_leads SET {col}=1 WHERE lead_id=?", (lead_id,))
        rescore(db, lead_id)
    db.commit()
    return jsonify(ok=True)


@app.post("/api/demo-requests")
def create_demo_request():
    data = request.get_json(silent=True) or {}
    if not check_csrf(data.get("csrf_token", "")):
        return jsonify(ok=False, errors={"form": "Your session has expired. Please refresh the page and try again."}), 400
    if clean(data.get("website")) or time.time() - session.get("form_rendered_at", time.time()) < 2:
        return jsonify(ok=False, errors={"form": "We couldn't process your request. Please try again."}), 400
    if rate_limited(f"demo:{client_ip()}", limit=5, window=600):
        return jsonify(ok=False, errors={"form": "Too many submissions. Please try again in a few minutes."}), 429
    fields = {k: clean(data.get(k), 160) for k in ("first_name", "last_name", "email", "organisation", "phone")}
    fields["email"] = fields["email"].lower()
    message = clean(data.get("message"), 2000)
    errors = {}
    for k, label in (("first_name", "first name"), ("last_name", "last name"), ("organisation", "organisation name")):
        if not fields[k]:
            errors[k] = f"Please enter your {label}."
    if not EMAIL_RE.match(fields["email"]):
        errors["email"] = "Please enter a valid work email address."
    if errors:
        return jsonify(ok=False, errors=errors), 422
    db = get_db()
    lead_id = session.get("lead_id")
    if not lead_id:
        row = db.execute("SELECT lead_id FROM marketing_leads WHERE email=? ORDER BY created_at DESC LIMIT 1",
                         (fields["email"],)).fetchone()
        lead_id = row["lead_id"] if row else None
    db.execute("INSERT INTO marketing_demo_requests (lead_id, first_name, last_name, email, organisation, phone, "
               "message, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (lead_id, fields["first_name"], fields["last_name"], fields["email"], fields["organisation"],
                fields["phone"] or None, message or None, now_iso()))
    if lead_id:
        db.execute("UPDATE marketing_leads SET demo_submitted=1, clicked_demo=1, "
                   "status=CASE WHEN status IN ('New Lead','Contacted') THEN 'Demo Requested' ELSE status END "
                   "WHERE lead_id=?", (lead_id,))
        rescore(db, lead_id)
    log_event(db, "demo_form_submitted", clean(data.get("visitor_id"), 64) or None, lead_id, "/demo")
    db.commit()
    return jsonify(ok=True)


# ---------- Admin: Marketing > Leads ----------

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return fn(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if rate_limited(f"login:{client_ip()}", limit=10, window=300):
            error = "Too many attempts. Please wait and try again."
        elif not check_csrf(request.form.get("csrf_token", "")):
            error = "Session expired. Please try again."
        elif (secrets.compare_digest(request.form.get("username", ""), ADMIN_USER)
              and secrets.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD)):
            session["is_admin"] = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/admin") else url_for("admin_leads"))
        else:
            error = "Incorrect username or password."
    return render_template("admin_login.html", error=error)


@app.post("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


def filtered_leads():
    q = clean(request.args.get("q"), 120)
    status = clean(request.args.get("status"), 40)
    role = clean(request.args.get("role"), 60)
    size = clean(request.args.get("size"), 20)
    temp = clean(request.args.get("temperature"), 10)
    sql = "SELECT * FROM marketing_leads WHERE 1=1"
    params: list = []
    if q:
        like = "ILIKE" if USE_PG else "LIKE"
        sql += (f" AND (first_name || ' ' || last_name {like} ? OR email {like} ? OR organisation {like} ? "
                f"OR COALESCE(source,'') {like} ?)")
        params += [f"%{q}%"] * 4
    for col, val in (("status", status), ("job_role", role), ("organisation_size", size), ("lead_temperature", temp)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    sql += " ORDER BY created_at DESC"
    return get_db().execute(sql, params).fetchall(), dict(q=q, status=status, role=role, size=size, temperature=temp)


@app.get("/admin/marketing/leads")
@admin_required
def admin_leads():
    leads, filters = filtered_leads()
    db = get_db()
    funnel_names = ["landing_page_view", "checklist_cta_clicked", "lead_form_started", "lead_form_submitted",
                    "checklist_downloaded", "product_section_viewed", "demo_cta_clicked", "demo_form_submitted"]
    counts = {r[0]: r[1] for r in db.execute(
        "SELECT event_name, COUNT(DISTINCT COALESCE(visitor_id, CAST(id AS TEXT))) FROM marketing_events GROUP BY event_name")}
    funnel = [(n, counts.get(n, 0)) for n in funnel_names]
    total = db.execute("SELECT COUNT(*) FROM marketing_leads").fetchone()[0]
    demo_requests = db.execute("""SELECT d.*, l.status AS lead_status, l.lead_temperature, l.lead_score
        FROM marketing_demo_requests d
        LEFT JOIN marketing_leads l ON l.lead_id=d.lead_id
        ORDER BY d.created_at DESC LIMIT 100""").fetchall()
    demo_total = db.execute("SELECT COUNT(*) FROM marketing_demo_requests").fetchone()[0]
    return render_template("admin_leads.html", leads=leads, filters=filters, statuses=LEAD_STATUSES,
                           roles=JOB_ROLES, sizes=ORG_SIZES, funnel=funnel, total=total,
                           demo_requests=demo_requests, demo_total=demo_total)


def filtered_cqc_prospects():
    q = clean(request.args.get("q"), 120)
    area = clean(request.args.get("area"), 80)
    service = clean(request.args.get("service"), 120)
    rating = clean(request.args.get("rating"), 40)
    status = clean(request.args.get("status"), 60)
    multi = clean(request.args.get("multi"), 10)
    segment = clean(request.args.get("segment"), 80)
    priority = clean(request.args.get("priority"), 10)
    try:
        page = max(1, int(request.args.get("page", "1")))
    except ValueError:
        page = 1
    per_page = 50
    like = "ILIKE" if USE_PG else "LIKE"
    where = ["1=1"]
    params = []
    if q:
        where.append(f"""(p.provider_name {like} ? OR EXISTS (
            SELECT 1 FROM cqc_locations ql WHERE ql.provider_id=p.provider_id AND ql.location_name {like} ?
        ))""")
        params += [f"%{q}%", f"%{q}%"]
    if area:
        where.append(f"""(COALESCE(p.postcode,'') {like} ? OR COALESCE(p.town_city,'') {like} ?
            OR COALESCE(p.county,'') {like} ? OR EXISTS (
              SELECT 1 FROM cqc_locations al WHERE al.provider_id=p.provider_id
              AND (COALESCE(al.postcode,'') {like} ? OR COALESCE(al.town_city,'') {like} ?
                   OR COALESCE(al.county,'') {like} ?)
            ))""")
        params += [f"%{area}%"] * 6
    if service:
        where.append("""EXISTS (SELECT 1 FROM cqc_locations sl
            JOIN cqc_location_service_types st ON st.location_id=sl.location_id
            WHERE sl.provider_id=p.provider_id AND st.service_type_name=?)""")
        params.append(service)
    if rating:
        where.append("EXISTS (SELECT 1 FROM cqc_locations rl WHERE rl.provider_id=p.provider_id AND rl.overall_rating=?)")
        params.append(rating)
    if status:
        where.append("p.registration_status=?")
        params.append(status)
    try:
        min_locations = int(multi) if multi else 0
    except ValueError:
        min_locations = 0
    if min_locations:
        where.append("(SELECT COUNT(*) FROM cqc_locations ml WHERE ml.provider_id=p.provider_id) >= ?")
        params.append(min_locations)
    if segment:
        where.append("EXISTS (SELECT 1 FROM sales_accounts sa WHERE sa.provider_id=p.provider_id AND sa.target_segment=?)")
        params.append(segment)
    if priority:
        where.append("EXISTS (SELECT 1 FROM sales_accounts sa WHERE sa.provider_id=p.provider_id AND sa.priority=?)")
        params.append(priority)

    where_sql = " AND ".join(where)
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) FROM cqc_providers p WHERE {where_sql}", params).fetchone()[0]
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    offset = (page - 1) * per_page
    aggregate = "STRING_AGG(DISTINCT {col}, ', ')" if USE_PG else "GROUP_CONCAT(DISTINCT {col})"
    service_agg = aggregate.format(col="st.service_type_name")
    rating_agg = aggregate.format(col="r.overall_rating")
    rows = db.execute(f"""
        SELECT p.*,
          (SELECT COUNT(*) FROM cqc_locations l WHERE l.provider_id=p.provider_id) AS location_count,
          (SELECT {service_agg} FROM cqc_locations l
             JOIN cqc_location_service_types st ON st.location_id=l.location_id
             WHERE l.provider_id=p.provider_id) AS service_types,
          (SELECT {rating_agg} FROM cqc_locations r
             WHERE r.provider_id=p.provider_id AND r.overall_rating IS NOT NULL) AS ratings,
          a.account_id, a.account_score, a.priority, a.sales_status, a.target_segment, a.score_reasons
        FROM cqc_providers p
        LEFT JOIN sales_accounts a ON a.provider_id=p.provider_id
        WHERE {where_sql}
        ORDER BY location_count DESC, p.provider_name
        LIMIT ? OFFSET ?
    """, params + [per_page, offset]).fetchall()
    prospect_rows = []
    for row in rows:
        item = dict(row)
        try:
            reasons = json.loads(item.get("score_reasons") or "[]")
            if not isinstance(reasons, list):
                reasons = []
        except (TypeError, ValueError, json.JSONDecodeError):
            reasons = []
        item["score_reason_items"] = reasons
        prospect_rows.append(item)
    return prospect_rows, total, page, pages, dict(q=q, area=area, service=service, rating=rating, status=status, multi=multi, segment=segment, priority=priority)


@app.post("/admin/sales/cqc-sync")
@admin_required
def admin_cqc_sync():
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    db = get_db()
    active = db.execute("""SELECT sync_id FROM cqc_sync_runs
                           WHERE status IN ('Queued','Running')
                           ORDER BY started_at DESC LIMIT 1""").fetchone()
    if active:
        return redirect(url_for("admin_cqc_prospects", sync="running"))
    mode = clean(request.form.get("mode"), 20)
    sync_id = str(uuid.uuid4())
    db.execute("""INSERT INTO cqc_sync_runs
                  (sync_id, sync_type, status, started_at, providers_seen, locations_seen, records_changed)
                  VALUES (?,?, 'Queued', ?,0,0,0)""",
               (sync_id, "full" if mode == "full" else "limited", now_iso()))
    db.commit()
    return redirect(url_for("admin_cqc_prospects", sync="started"))

@app.get("/admin/sales/cqc-prospects")
@admin_required
def admin_cqc_prospects():
    prospects, total, page, pages, filters = filtered_cqc_prospects()
    db = get_db()
    imported_total = db.execute("SELECT COUNT(*) FROM cqc_providers").fetchone()[0]
    imported_locations = db.execute("SELECT COUNT(*) FROM cqc_locations").fetchone()[0]
    latest_sync = db.execute("""SELECT sync_id, sync_type, status, started_at, completed_at,
        providers_seen, locations_seen, records_changed, error_message
        FROM cqc_sync_runs ORDER BY started_at DESC LIMIT 1""").fetchone()
    sync_is_running = False
    if latest_sync and latest_sync["status"] in ("Queued", "Running"):
        try:
            sync_started = datetime.fromisoformat(str(latest_sync["started_at"]).replace("Z", "+00:00"))
            if sync_started.tzinfo is None:
                sync_started = sync_started.replace(tzinfo=timezone.utc)
            sync_age = datetime.now(timezone.utc) - sync_started
            no_progress = int(latest_sync["providers_seen"] or 0) == 0 and int(latest_sync["locations_seen"] or 0) == 0
            active_window = timedelta(minutes=5) if no_progress else (timedelta(hours=6) if latest_sync["sync_type"] == "full" else timedelta(minutes=15))
            sync_is_running = sync_age <= active_window
        except (TypeError, ValueError):
            sync_is_running = False
    service_types = [r[0] for r in db.execute(
        "SELECT DISTINCT service_type_name FROM cqc_location_service_types WHERE service_type_name IS NOT NULL ORDER BY service_type_name"
    ).fetchall()]
    ratings = [r[0] for r in db.execute(
        "SELECT DISTINCT overall_rating FROM cqc_locations WHERE overall_rating IS NOT NULL ORDER BY overall_rating"
    ).fetchall()]
    segments = [r[0] for r in db.execute("SELECT DISTINCT target_segment FROM sales_accounts WHERE target_segment IS NOT NULL ORDER BY target_segment").fetchall()]
    registration_statuses = [r[0] for r in db.execute(
        "SELECT DISTINCT registration_status FROM cqc_providers WHERE registration_status IS NOT NULL ORDER BY registration_status"
    ).fetchall()]

    def page_url(n):
        args = request.args.to_dict()
        args["page"] = str(n)
        return url_for("admin_cqc_prospects", **args)

    return render_template("admin_cqc_prospects.html", prospects=prospects, total=total,
                           imported_total=imported_total, page=page, pages=pages, filters=filters,
                           service_types=service_types, ratings=ratings, segments=segments,
                           registration_statuses=registration_statuses, page_url=page_url,
                           imported_locations=imported_locations, latest_sync=latest_sync,
                           sync_is_running=sync_is_running, sync_notice=request.args.get("sync"))


SALES_STAGES = ["Prospect", "Contacted", "Qualified", "Demo", "Trial", "Customer", "Closed"]
SALES_STATUSES = ["Unworked", "Researching", "Attempting Contact", "Connected", "Follow-up", "Demo Booked", "Proposal", "Won", "Lost", "Do Not Contact"]


def _sales_account_or_404(account_id):
    row = get_db().execute("SELECT * FROM sales_accounts WHERE account_id=?", (account_id,)).fetchone()
    if not row:
        abort(404)
    return row


@app.get("/admin/sales/dashboard")
@admin_required
def admin_sales_dashboard():
    db = get_db()
    scalar = lambda sql, params=(): db.execute(sql, params).fetchone()[0]
    providers = scalar("SELECT COUNT(*) FROM cqc_providers")
    locations = scalar("SELECT COUNT(*) FROM cqc_locations")
    registered = scalar("SELECT COUNT(*) FROM cqc_providers WHERE LOWER(COALESCE(registration_status,'')) LIKE ? AND LOWER(COALESCE(registration_status,'')) NOT LIKE ?", ("%registered%", "%deregister%"))
    accounts = scalar("SELECT COUNT(*) FROM sales_accounts")
    priority_a = scalar("SELECT COUNT(*) FROM sales_accounts WHERE priority IN ('A1','A2')")
    multi_site = scalar("SELECT COUNT(*) FROM sales_accounts WHERE location_count >= 2")
    websites = scalar("SELECT COUNT(*) FROM cqc_providers WHERE COALESCE(website,'') <> ''")
    contacts = scalar("SELECT COUNT(DISTINCT account_id) FROM sales_contacts WHERE do_not_contact=0")
    unworked = scalar("SELECT COUNT(*) FROM sales_accounts WHERE sales_status='Unworked'")
    poor_rated = scalar("""SELECT COUNT(DISTINCT provider_id) FROM cqc_locations
                           WHERE LOWER(COALESCE(overall_rating,'')) IN ('requires improvement','inadequate')""")
    m = {
        "providers": providers, "locations": locations, "registered": registered,
        "accounts": accounts, "priority_a": priority_a, "multi_site": multi_site,
        "poor_rated": poor_rated, "unworked": unworked,
        "website_coverage": round(100*websites/providers,1) if providers else 0,
        "contact_coverage": round(100*contacts/accounts,1) if accounts else 0,
    }
    sync = db.execute("""SELECT sync_type,status,started_at,completed_at,providers_seen,locations_seen,
                        records_changed,error_message FROM cqc_sync_runs ORDER BY started_at DESC LIMIT 1""").fetchone()
    segments = [{"name":r[0] or "Unclassified","count":r[1]} for r in db.execute(
        "SELECT target_segment,COUNT(*) FROM sales_accounts GROUP BY target_segment ORDER BY COUNT(*) DESC LIMIT 8").fetchall()]
    priorities = [{"name":r[0] or "C","count":r[1]} for r in db.execute(
        """SELECT priority,COUNT(*) FROM sales_accounts GROUP BY priority
           ORDER BY CASE priority WHEN 'A1' THEN 1 WHEN 'A2' THEN 2 WHEN 'B' THEN 3 ELSE 4 END""").fetchall()]
    ratings = [{"name":r[0] or "Not rated","count":r[1]} for r in db.execute(
        """SELECT COALESCE(NULLIF(overall_rating,''),'Not rated'),COUNT(*) FROM cqc_locations
           GROUP BY COALESCE(NULLIF(overall_rating,''),'Not rated') ORDER BY COUNT(*) DESC""").fetchall()]
    areas = [{"name":r[0] or "Unknown","count":r[1]} for r in db.execute(
        """SELECT COALESCE(NULLIF(county,''),NULLIF(town_city,''),'Unknown'),COUNT(*)
           FROM cqc_locations GROUP BY COALESCE(NULLIF(county,''),NULLIF(town_city,''),'Unknown')
           ORDER BY COUNT(*) DESC LIMIT 10""").fetchall()]
    services = [{"name":r[0] or r[1] or "Unknown","count":r[2]} for r in db.execute(
        """SELECT service_type_name,service_type_code,COUNT(DISTINCT location_id)
           FROM cqc_location_service_types GROUP BY service_type_name,service_type_code
           ORDER BY COUNT(DISTINCT location_id) DESC LIMIT 10""").fetchall()]
    top_accounts = [{"id":r[0],"name":r[1],"segment":r[2] or "Unclassified","locations":r[3],
                     "score":r[4],"priority":r[5],"status":r[6]} for r in db.execute(
        """SELECT account_id,company_name,target_segment,location_count,account_score,priority,sales_status
           FROM sales_accounts ORDER BY account_score DESC,location_count DESC,company_name LIMIT 12""").fetchall()]
    lifecycle = [{"name":r[0] or "Prospect","count":r[1]} for r in db.execute(
        "SELECT lifecycle_stage,COUNT(*) FROM sales_accounts GROUP BY lifecycle_stage ORDER BY COUNT(*) DESC").fetchall()]
    return render_template("admin_sales_dashboard.html", m=m, sync=sync, segments=segments,
                           priorities=priorities, ratings=ratings, areas=areas, services=services,
                           top_accounts=top_accounts, lifecycle=lifecycle)


@app.get("/admin/sales/work-queue")
@admin_required
def admin_sales_queue():
    owner = clean(request.args.get("owner"), 120)
    priority = clean(request.args.get("priority"), 10)
    sql = "SELECT * FROM sales_accounts WHERE 1=1"
    params = []
    if owner:
        like = "ILIKE" if USE_PG else "LIKE"
        sql += f" AND COALESCE(owner,'') {like} ?"
        params.append(f"%{owner}%")
    if priority in ("A1", "A2", "B", "C"):
        sql += " AND priority=?"
        params.append(priority)
    sql += """ ORDER BY CASE priority WHEN 'A1' THEN 1 WHEN 'A2' THEN 2 WHEN 'B' THEN 3 ELSE 4 END,
               CASE WHEN next_action_at IS NULL THEN 1 ELSE 0 END, next_action_at, account_score DESC LIMIT 250"""
    db = get_db()
    accounts = db.execute(sql, params).fetchall()
    now = now_iso()
    metrics = {
        "a1": db.execute("SELECT COUNT(*) FROM sales_accounts WHERE priority='A1'").fetchone()[0],
        "a2": db.execute("SELECT COUNT(*) FROM sales_accounts WHERE priority='A2'").fetchone()[0],
        "open_tasks": db.execute("SELECT COUNT(*) FROM sales_tasks WHERE status='Open'").fetchone()[0],
        "overdue": db.execute("SELECT COUNT(*) FROM sales_tasks WHERE status='Open' AND due_at IS NOT NULL AND due_at < ?", (now,)).fetchone()[0],
    }
    return render_template("admin_sales_queue.html", accounts=accounts, metrics=metrics, owner=owner, priority=priority)


@app.get("/admin/sales/campaigns")
@admin_required
def admin_sales_campaigns():
    db = get_db()
    campaigns = db.execute("""SELECT c.*,
        (SELECT COUNT(*) FROM sales_campaign_enrolments e WHERE e.campaign_id=c.campaign_id) AS enrolled,
        (SELECT COUNT(*) FROM sales_campaign_actions a JOIN sales_campaign_enrolments e ON e.enrolment_id=a.enrolment_id
         WHERE e.campaign_id=c.campaign_id AND a.status='Due') AS due_actions,
        (SELECT COUNT(*) FROM sales_campaign_actions a JOIN sales_campaign_enrolments e ON e.enrolment_id=a.enrolment_id
         WHERE e.campaign_id=c.campaign_id AND a.status='Completed') AS completed_actions
        FROM sales_campaigns c ORDER BY c.created_at DESC""").fetchall()
    actions = db.execute("""SELECT a.*, s.channel, s.subject_template, s.task_title,
        ac.account_id, ac.company_name, ac.priority, ac.target_segment, ct.email, ct.phone
        FROM sales_campaign_actions a
        JOIN sales_campaign_enrolments e ON e.enrolment_id=a.enrolment_id
        JOIN sales_campaign_steps s ON s.step_id=a.step_id
        JOIN sales_accounts ac ON ac.account_id=e.account_id
        LEFT JOIN sales_contacts ct ON ct.contact_id=e.contact_id
        WHERE a.status='Due' AND a.due_at <= ?
        ORDER BY a.due_at, CASE ac.priority WHEN 'A1' THEN 1 WHEN 'A2' THEN 2 WHEN 'B' THEN 3 ELSE 4 END
        LIMIT 200""", (now_iso(),)).fetchall()
    return render_template("admin_sales_campaigns.html", campaigns=campaigns, actions=actions)


@app.post("/admin/sales/campaign-actions/<action_id>/complete")
@admin_required
def admin_sales_campaign_action_complete(action_id):
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    db = get_db()
    action = db.execute("""SELECT a.*, e.account_id, e.enrolment_id, s.channel, s.step_number
        FROM sales_campaign_actions a
        JOIN sales_campaign_enrolments e ON e.enrolment_id=a.enrolment_id
        JOIN sales_campaign_steps s ON s.step_id=a.step_id WHERE a.action_id=?""", (action_id,)).fetchone()
    if not action or action["status"] != "Due":
        abort(404)
    outcome = clean(request.form.get("outcome"), 160) or "Completed"
    now = now_iso()
    db.execute("UPDATE sales_campaign_actions SET status='Completed', completed_at=?, outcome=? WHERE action_id=?",
               (now, outcome, action_id))
    db.execute("""INSERT INTO sales_activities
        (activity_id, account_id, activity_type, direction, outcome, notes, occurred_at, created_by)
        VALUES (?,?,?,'Outbound',?,?,?,?)""",
        (str(uuid.uuid4()), action["account_id"], action["channel"], outcome,
         "Completed from campaign sequence.", now, ADMIN_USER))
    db.execute("UPDATE sales_accounts SET last_contacted_at=?, updated_at=? WHERE account_id=?",
               (now, now, action["account_id"]))
    remaining = db.execute("SELECT COUNT(*) FROM sales_campaign_actions WHERE enrolment_id=? AND status='Due'",
                           (action["enrolment_id"],)).fetchone()[0]
    if remaining == 0:
        db.execute("UPDATE sales_campaign_enrolments SET status='Completed', completed_at=? WHERE enrolment_id=?",
                   (now, action["enrolment_id"]))
    else:
        db.execute("UPDATE sales_campaign_enrolments SET current_step=? WHERE enrolment_id=?",
                   (action["step_number"] + 1, action["enrolment_id"]))
    db.commit()
    return redirect(url_for("admin_sales_campaigns"))


@app.get("/admin/sales/accounts/<account_id>")
@admin_required
def admin_sales_account(account_id):
    account = _sales_account_or_404(account_id)
    db = get_db()
    contacts = db.execute("SELECT * FROM sales_contacts WHERE account_id=? ORDER BY is_decision_maker DESC, created_at DESC", (account_id,)).fetchall()
    opportunity = db.execute("SELECT * FROM sales_opportunities WHERE account_id=?", (account_id,)).fetchone()
    candidates = db.execute("SELECT * FROM sales_contact_candidates WHERE account_id=? AND status=? ORDER BY first_found_at DESC", (account_id, "Review")).fetchall()
    activities = db.execute("SELECT * FROM sales_activities WHERE account_id=? ORDER BY occurred_at DESC LIMIT 100", (account_id,)).fetchall()
    tasks = db.execute("SELECT * FROM sales_tasks WHERE account_id=? ORDER BY CASE status WHEN 'Open' THEN 0 ELSE 1 END, due_at, created_at DESC", (account_id,)).fetchall()
    manager_rows = db.execute("""SELECT m.manager_name,m.source_url,l.location_name,l.location_id
        FROM cqc_registered_managers m JOIN cqc_locations l ON l.location_id=m.location_id
        WHERE l.provider_id=? ORDER BY l.location_name,m.manager_name""", (account["provider_id"],)).fetchall()
    if not manager_rows:
        location_rows = db.execute("SELECT location_id FROM cqc_locations WHERE provider_id=? ORDER BY location_name", (account["provider_id"],)).fetchall()
        location_ids = [row["location_id"] for row in location_rows]
        if location_ids:
            try:
                from cqc_managers import fetch_registered_managers
                with ThreadPoolExecutor(max_workers=min(6, len(location_ids))) as pool:
                    results = list(pool.map(fetch_registered_managers, location_ids))
                fetched_at = now_iso()
                for result in results:
                    for manager_name in result["managers"]:
                        db.execute("""INSERT INTO cqc_registered_managers(location_id,manager_name,source_url,fetched_at)
                                      VALUES (?,?,?,?) ON CONFLICT(location_id,manager_name)
                                      DO UPDATE SET source_url=excluded.source_url,fetched_at=excluded.fetched_at""",
                                   (result["location_id"], manager_name, result["source_url"], fetched_at))
                db.commit()
                manager_rows = db.execute("""SELECT m.manager_name,m.source_url,l.location_name,l.location_id
                    FROM cqc_registered_managers m JOIN cqc_locations l ON l.location_id=m.location_id
                    WHERE l.provider_id=? ORDER BY l.location_name,m.manager_name""", (account["provider_id"],)).fetchall()
            except Exception as exc:
                print(f"CQC registered-manager lookup failed for {account['provider_id']}: {exc}", flush=True)
    try:
        score_reasons = json.loads(account["score_reasons"] or "[]")
        if not isinstance(score_reasons, list):
            score_reasons = []
    except (TypeError, ValueError, json.JSONDecodeError):
        score_reasons = []
    return render_template("admin_sales_account.html", account=account, opportunity=opportunity, contacts=contacts, candidates=candidates, activities=activities,
                           tasks=tasks, stages=SALES_STAGES, statuses=SALES_STATUSES, score_reasons=score_reasons, registered_managers=manager_rows)


@app.post("/admin/sales/accounts/<account_id>/update")
@admin_required
def admin_sales_account_update(account_id):
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    _sales_account_or_404(account_id)
    owner = clean(request.form.get("owner"), 120) or None
    stage = clean(request.form.get("lifecycle_stage"), 40)
    status = clean(request.form.get("sales_status"), 40)
    if stage not in SALES_STAGES or status not in SALES_STATUSES:
        abort(400)
    db = get_db()
    db.execute("UPDATE sales_accounts SET owner=?, lifecycle_stage=?, sales_status=?, updated_at=? WHERE account_id=?",
               (owner, stage, status, now_iso(), account_id))
    db.commit()
    return redirect(url_for("admin_sales_account", account_id=account_id))


@app.post("/admin/sales/accounts/<account_id>/commercial")
@admin_required
def admin_sales_commercial_update(account_id):
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    _sales_account_or_404(account_id)
    def money(name):
        raw = clean(request.form.get(name), 30)
        if not raw:
            return None
        try:
            value = round(float(raw), 2)
        except ValueError:
            abort(400)
        if value < 0:
            abort(400)
        return value
    try:
        probability = int(request.form.get("probability", "0"))
    except ValueError:
        abort(400)
    if probability < 0 or probability > 100:
        abort(400)
    status = clean(request.form.get("commercial_status"), 30)
    if status not in ("Open", "Proposal", "Negotiation", "Won", "Lost"):
        abort(400)
    db = get_db()
    existing = db.execute("SELECT opportunity_id FROM sales_opportunities WHERE account_id=?", (account_id,)).fetchone()
    oid = existing["opportunity_id"] if existing else str(uuid.uuid4())
    now = now_iso()
    db.execute("""INSERT INTO sales_opportunities
        (opportunity_id, account_id, proposal_value, monthly_license, setup_fee, probability,
         expected_close_date, contract_start_date, commercial_status, won_lost_reason, notes, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id) DO UPDATE SET proposal_value=excluded.proposal_value,
        monthly_license=excluded.monthly_license, setup_fee=excluded.setup_fee, probability=excluded.probability,
        expected_close_date=excluded.expected_close_date, contract_start_date=excluded.contract_start_date,
        commercial_status=excluded.commercial_status, won_lost_reason=excluded.won_lost_reason,
        notes=excluded.notes, updated_at=excluded.updated_at""",
        (oid, account_id, money("proposal_value"), money("monthly_license"), money("setup_fee"), probability,
         clean(request.form.get("expected_close_date"), 20) or None,
         clean(request.form.get("contract_start_date"), 20) or None, status,
         clean(request.form.get("won_lost_reason"), 300) or None, clean(request.form.get("commercial_notes"), 2000) or None,
         now, now))
    if status == "Won":
        db.execute("UPDATE sales_accounts SET lifecycle_stage='Customer', sales_status='Won', updated_at=? WHERE account_id=?", (now, account_id))
    elif status == "Lost":
        db.execute("UPDATE sales_accounts SET lifecycle_stage='Closed', sales_status='Lost', updated_at=? WHERE account_id=?", (now, account_id))
    db.commit()
    return redirect(url_for("admin_sales_account", account_id=account_id))


@app.post("/admin/sales/accounts/<account_id>/contacts")
@admin_required
def admin_sales_add_contact(account_id):
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    _sales_account_or_404(account_id)
    email = clean(request.form.get("email"), 254).lower() or None
    if email and not EMAIL_RE.match(email):
        abort(400)
    db = get_db()
    now = now_iso()
    db.execute("""INSERT INTO sales_contacts
        (contact_id, account_id, first_name, last_name, job_title, email, phone, source,
         is_decision_maker, email_verified, do_not_contact, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,'Manual',?,0,0,?,?)""",
        (str(uuid.uuid4()), account_id, clean(request.form.get("first_name"), 80) or None,
         clean(request.form.get("last_name"), 80) or None, clean(request.form.get("job_title"), 120) or None,
         email, clean(request.form.get("phone"), 60) or None, int(request.form.get("is_decision_maker") == "1"), now, now))
    db.commit()
    return redirect(url_for("admin_sales_account", account_id=account_id))


@app.post("/admin/sales/contact-candidates/<candidate_id>/review")
@admin_required
def admin_sales_review_candidate(candidate_id):
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    action = request.form.get("action", "")
    if action not in ("accept", "reject"):
        abort(400)
    db = get_db()
    candidate = db.execute("SELECT * FROM sales_contact_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
    if not candidate or candidate["status"] != "Review":
        abort(404)
    if action == "accept":
        duplicate = db.execute(
            "SELECT contact_id FROM sales_contacts WHERE account_id=? AND COALESCE(email,'')=? AND COALESCE(phone,'')=?",
            (candidate["account_id"], candidate["email"] or "", candidate["phone"] or "")
        ).fetchone()
        if not duplicate:
            now = now_iso()
            db.execute("""INSERT INTO sales_contacts
                (contact_id, account_id, email, phone, source, source_url, is_decision_maker,
                 email_verified, do_not_contact, created_at, updated_at)
                VALUES (?,?,?,?,?,?,0,0,0,?,?)""",
                (str(uuid.uuid4()), candidate["account_id"], candidate["email"], candidate["phone"],
                 "Provider website", candidate["source_url"], now, now))
        db.execute("UPDATE sales_contact_candidates SET status='Accepted' WHERE candidate_id=?", (candidate_id,))
    else:
        db.execute("UPDATE sales_contact_candidates SET status='Rejected' WHERE candidate_id=?", (candidate_id,))
    db.commit()
    return redirect(url_for("admin_sales_account", account_id=candidate["account_id"]))


@app.post("/admin/sales/accounts/<account_id>/activities")
@admin_required
def admin_sales_add_activity(account_id):
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    _sales_account_or_404(account_id)
    activity_type = clean(request.form.get("activity_type"), 40)
    if activity_type not in ("Call", "Email", "LinkedIn", "Meeting", "Demo", "Note"):
        abort(400)
    now = now_iso()
    db = get_db()
    db.execute("""INSERT INTO sales_activities
        (activity_id, account_id, activity_type, direction, outcome, notes, occurred_at, created_by)
        VALUES (?,?,?,'Outbound',?,?,?,?)""",
        (str(uuid.uuid4()), account_id, activity_type, clean(request.form.get("outcome"), 160) or None,
         clean(request.form.get("notes"), 2000) or None, now, ADMIN_USER))
    db.execute("UPDATE sales_accounts SET last_contacted_at=?, sales_status=CASE WHEN sales_status='Unworked' THEN 'Attempting Contact' ELSE sales_status END, updated_at=? WHERE account_id=?",
               (now, now, account_id))
    db.commit()
    return redirect(url_for("admin_sales_account", account_id=account_id))


def _refresh_next_action(db, account_id):
    row = db.execute("""SELECT title, due_at FROM sales_tasks WHERE account_id=? AND status='Open'
                        ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, created_at LIMIT 1""",
                     (account_id,)).fetchone()
    db.execute("UPDATE sales_accounts SET next_action=?, next_action_at=?, updated_at=? WHERE account_id=?",
               ((row["title"] if row else None), (row["due_at"] if row else None), now_iso(), account_id))


@app.post("/admin/sales/accounts/<account_id>/tasks")
@admin_required
def admin_sales_add_task(account_id):
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    _sales_account_or_404(account_id)
    title = clean(request.form.get("title"), 180)
    task_type = clean(request.form.get("task_type"), 40)
    if not title or task_type not in ("Call", "Email", "LinkedIn", "Demo", "Follow-up", "Research"):
        abort(400)
    due_at = clean(request.form.get("due_at"), 40) or None
    db = get_db()
    db.execute("""INSERT INTO sales_tasks
        (task_id, account_id, assigned_to, task_type, title, due_at, status, priority, created_at)
        VALUES (?,?,?,?,?,?,'Open','Normal',?)""",
        (str(uuid.uuid4()), account_id, clean(request.form.get("assigned_to"), 120) or None,
         task_type, title, due_at, now_iso()))
    _refresh_next_action(db, account_id)
    db.commit()
    return redirect(url_for("admin_sales_account", account_id=account_id))


@app.post("/admin/sales/tasks/<task_id>/complete")
@admin_required
def admin_sales_complete_task(task_id):
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    db = get_db()
    task = db.execute("SELECT account_id FROM sales_tasks WHERE task_id=?", (task_id,)).fetchone()
    if not task:
        abort(404)
    db.execute("UPDATE sales_tasks SET status='Completed', completed_at=? WHERE task_id=?", (now_iso(), task_id))
    _refresh_next_action(db, task["account_id"])
    db.commit()
    return redirect(url_for("admin_sales_account", account_id=task["account_id"]))


@app.post("/admin/marketing/leads/<lead_id>/status")
@admin_required
def admin_update_status(lead_id):
    if not check_csrf(request.form.get("csrf_token", "")):
        abort(400)
    status = request.form.get("status", "")
    if status not in LEAD_STATUSES:
        abort(400)
    db = get_db()
    db.execute("UPDATE marketing_leads SET status=?, updated_at=? WHERE lead_id=?", (status, now_iso(), lead_id))
    db.commit()
    return redirect(request.referrer or url_for("admin_leads"))


@app.get("/admin/marketing/leads.csv")
@admin_required
def admin_leads_csv():
    leads, _ = filtered_leads()
    cols = ["lead_id", "first_name", "last_name", "email", "organisation", "job_role", "organisation_size",
            "lead_magnet", "marketing_consent", "created_at", "source", *UTM_KEYS, "landing_page", "status",
            "lead_score", "lead_temperature"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for row in leads:
        w.writerow([str(row[c]) if row[c] is not None else "" for c in cols])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=veridyn-leads-{datetime.now():%Y%m%d}.csv"})


def _cqc_background_worker():
    """Persistent DB-backed CQC worker. Queued work survives web restarts."""
    time.sleep(2)
    while True:
        db = None
        try:
            db = connect()
            job = db.execute("""SELECT sync_id, sync_type FROM cqc_sync_runs
                                WHERE status='Queued' ORDER BY started_at ASC LIMIT 1""").fetchone()
            if job:
                sync_id, sync_type = job["sync_id"], job["sync_type"]
                db.close()
                db = None
                from cqc_sync import run_sync
                result = run_sync(limit=100 if sync_type == "limited" else None, dry_run=False, sync_id=sync_id)
                # Full imports deliberately process one bounded page at a time.
                # Re-queue the same persistent job until both collections are complete.
                if not result.get("completed"):
                    db = connect()
                    db.execute("UPDATE cqc_sync_runs SET status='Queued' WHERE sync_id=?", (sync_id,))
                    db.commit(); db.close(); db = None
                    time.sleep(1)
                else:
                    from account_scoring import score_all
                    score_all()
            else:
                time.sleep(5)
        except Exception as exc:
            print(f"CQC background worker error: {exc}", flush=True)
            time.sleep(10)
        finally:
            if db is not None:
                db.close()


migrate()
# A Render restart cannot leave a job permanently orphaned: put unfinished work back in the queue.
_recovery_db = connect()
_recovery_db.execute("""UPDATE cqc_sync_runs SET status='Queued', error_message=?
                        WHERE status='Running'""",
                     ("Worker restarted; queued automatically to resume.",))
_recovery_db.commit()
_recovery_db.close()
threading.Thread(target=_cqc_background_worker, name="cqc-sync-worker", daemon=True).start()
print(f"Lead storage: {'Supabase/Postgres' if USE_PG else f'SQLite ({DB_PATH})'}")
if not PDF_PATH.exists():
    build_checklist_pdf(PDF_PATH)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
