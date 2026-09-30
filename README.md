# Veridyn Recruit — Lead Magnet Landing Page

Lead magnet landing page for the "2026 Care Recruitment Compliance Checklist".

Quick start: double-click `start.bat` (Windows) or run `./start.sh` (Mac/Linux).

Manual:
```
pip install -r requirements.txt
python app.py            # http://localhost:5000/care-recruitment-compliance-checklist
```

Routes
- `/care-recruitment-compliance-checklist` — landing page
- `/resources/care-recruitment-checklist/thank-you` — thank-you page (after form submit)
- `/resources/care-recruitment-checklist/download` — checklist PDF (requires a captured lead in session)
- `/demo` — demo request
- `/admin/marketing/leads` — Platform Admin > Marketing > Leads (search, filters, status, CSV export)

Env: `ADMIN_USER` (default `admin`), `ADMIN_PASSWORD` (default `veridyn-admin` — change it), `SECRET_KEY`, `VERIDYN_DB`, `PORT`.

Database: set `DATABASE_URL` to a Postgres/Supabase connection string to store leads in Supabase; otherwise
SQLite (`instance/marketing.db`) is used. Supabase setup:
1. Create a free project at https://supabase.com (region: London / eu-west-2).
2. Project > Connect > copy the "Session pooler" URI and insert your database password.
3. `cp .env.example .env` and set `DATABASE_URL=` to that URI, then run `./start.sh` / `start.bat`.
Tables are created automatically on first start with Row Level Security enabled, so they are not readable via
Supabase's public API keys.

Data is stored in `marketing_*` tables, kept separate from candidate records.
Lead scoring rules live in `SCORING_RULES` in `app.py`; scores are internal only.
