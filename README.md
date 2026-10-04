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


## CQC sales platform branch

The `feature/cqc-sales-platform` branch adds a separate account-intelligence layer. Imported CQC organisations are stored in `cqc_*` and `sales_*` tables and are not treated as consented inbound `marketing_leads`.

### CQC API sync

Create/access a CQC Syndication API subscription in the CQC Developer Portal, then set `CQC_API_KEY` in `.env`. The importer is manual by design and does not run when Flask starts.

Safe first test:

```
python cqc_sync.py --limit 10 --dry-run
python cqc_sync.py --limit 10
```

Only after validating the imported records:

```
python cqc_sync.py --full
```

The sync imports providers first, then locations, retains the raw API payload for traceability, normalises service types/specialisms/regulated activities, and records each run in `cqc_sync_runs`. Never commit the CQC API key.


### Account classification and scoring

After a CQC import, generate/update sales accounts with explainable CQC-derived scores:

```
python account_scoring.py --limit 25
python account_scoring.py
```

Priority bands: A1 = 80+, A2 = 60–79, B = 40–59, C = below 40. The score uses target-care service signals, multi-site scale, recent registrations, selected CQC rating signals and website availability. Each score stores its reasons. Re-scoring updates derived fields without overwriting manual sales ownership/status.


### Sales CRM / work queue

After importing and scoring providers, open `/admin/sales/work-queue`. The queue prioritises A1/A2 accounts and upcoming actions. Each scored provider has an account workspace for sales ownership, lifecycle/status, manual contacts, outbound activity notes and follow-up tasks. All CRM writes require the existing admin session and CSRF token.

Recommended operator flow: CQC Prospects → scored account → assign owner → add decision-maker → log call/email/LinkedIn activity → schedule next task → work from the queue each day.


### Public contact enrichment

Run a small batch first:

```
python contact_enrichment.py --limit 25 --priority A1
```

The worker scans only provider-owned public websites and stores discovered business emails/phones as review candidates with source evidence. It does not guess email patterns or mark discovered emails as verified. Review suggestions in the account workspace; Accept copies a non-duplicate candidate into `sales_contacts`, while Reject retains the candidate record with rejected status for audit history.


### Manual-first outbound campaigns

Create a 21-day multi-channel campaign:

```
python campaign_engine.py seed --name "Veridyn Recruit 21-Day Outreach"
```

Then enrol a controlled batch using the returned campaign ID:

```
python campaign_engine.py enrol CAMPAIGN_ID --priority A1 --limit 25
```

The engine creates Email, LinkedIn and Call actions but does not send messages. Enrolment skips do-not-contact contacts and matches in `sales_suppressions` by email, domain or CQC provider. Salespeople execute due actions from `/admin/sales/campaigns`; completing an action records it in CRM activity history.


### Sales Command Centre

Open `/admin/sales/dashboard` for management reporting across target accounts, A1/A2 priorities, contact coverage, CRM activities, demos, trials, customers, account-to-customer conversion, pipeline stage, segment performance, salesperson performance and campaign execution.

Revenue/MRR is intentionally not inferred from list pricing. Add contract/subscription revenue fields before using this dashboard for financial attribution.
