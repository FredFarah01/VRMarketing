import csv
import io
import logging
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)

from checklist_pdf import build_checklist_pdf
from cqc import views as cqc_views
from cqc.schema import CQC_SCHEMA_SQL, PG_RLS_SQL

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
SALES_USER = os.environ.get("SALES_USER", "sales")
SALES_PASSWORD = os.environ.get("SALES_PASSWORD", "")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

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

    def executemany(self, sql: str, rows):
        with self.conn.cursor() as cur:
            cur.executemany(sql.replace("?", "%s"), rows)

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
    CQC_SCHEMA_SQL,
]

PG_MIGRATIONS = [
    MIGRATIONS[0].replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY") + """
    ALTER TABLE marketing_leads ENABLE ROW LEVEL SECURITY;
    ALTER TABLE marketing_events ENABLE ROW LEVEL SECURITY;
    ALTER TABLE marketing_demo_requests ENABLE ROW LEVEL SECURITY;
    ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
    """,
    CQC_SCHEMA_SQL.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY") + PG_RLS_SQL,
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
            session["role"] = "admin"
            session["username"] = ADMIN_USER
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/admin") else url_for("admin_leads"))
        elif (SALES_PASSWORD and secrets.compare_digest(request.form.get("username", ""), SALES_USER)
              and secrets.compare_digest(request.form.get("password", ""), SALES_PASSWORD)):
            session.pop("is_admin", None)
            session["role"] = "sales"
            session["username"] = SALES_USER
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/admin/cqc") else url_for("cqc.dashboard"))
        else:
            error = "Incorrect username or password."
    return render_template("admin_login.html", error=error)


@app.post("/admin/logout")
def admin_logout():
    for key in ("is_admin", "role", "username"):
        session.pop(key, None)
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
    return render_template("admin_leads.html", leads=leads, filters=filters, statuses=LEAD_STATUSES,
                           roles=JOB_ROLES, sizes=ORG_SIZES, funnel=funnel, total=total)


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


migrate()
cqc_views.init(app, get_db=get_db, connect=connect, check_csrf=check_csrf, clean=clean,
               rate_limited=rate_limited, client_ip=client_ip, use_pg=USE_PG)
print(f"Lead storage: {'Supabase/Postgres' if USE_PG else f'SQLite ({DB_PATH})'}")
if not PDF_PATH.exists():
    build_checklist_pdf(PDF_PATH)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
