"""Database schema for CQC data (source of truth: CQC Syndication API) and Veridyn CRM data.

CQC-sourced tables are prefixed ``cqc_``. Veridyn-generated or sales data lives in separate
tables (``cqc_classifications``, ``lead_*``, ``saved_searches``) so a CQC re-sync never
overwrites CRM notes, contacts or sales status.
"""

CQC_TABLES = [
    "cqc_providers", "cqc_locations", "cqc_provider_locations", "cqc_regulated_activities",
    "cqc_service_types", "cqc_specialisms", "cqc_ratings", "cqc_reports", "cqc_relationships",
    "cqc_classifications", "cqc_sync_log", "cqc_sync_failures", "cqc_settings",
    "lead_accounts", "lead_contacts", "lead_notes", "lead_activities", "lead_lists",
    "lead_list_members", "saved_searches",
]

CQC_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cqc_providers (
    provider_id TEXT PRIMARY KEY,
    name TEXT,
    also_known_as TEXT,
    organisation_type TEXT,
    ownership_type TEXT,
    type TEXT,
    companies_house_number TEXT,
    charity_number TEXT,
    brand_id TEXT,
    brand_name TEXT,
    uprn TEXT,
    ods_code TEXT,
    registration_status TEXT,
    registration_date TEXT,
    deregistration_date TEXT,
    website TEXT,
    main_phone_number TEXT,
    address_line_1 TEXT,
    address_line_2 TEXT,
    town_city TEXT,
    county TEXT,
    region TEXT,
    postal_code TEXT,
    latitude REAL,
    longitude REAL,
    icb_code TEXT,
    icb_name TEXT,
    inspection_directorate TEXT,
    constituency TEXT,
    local_authority TEXT,
    last_inspection_date TEXT,
    last_report_date TEXT,
    location_ids TEXT,
    regulated_activities TEXT,
    inspection_categories TEXT,
    inspection_areas TEXT,
    current_rating TEXT,
    current_rating_date TEXT,
    contacts_json TEXT,
    raw_json TEXT,
    source TEXT NOT NULL DEFAULT 'CQC',
    first_seen_at TEXT,
    cqc_synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cqc_providers_name ON cqc_providers(name);
CREATE INDEX IF NOT EXISTS idx_cqc_providers_status ON cqc_providers(registration_status);
CREATE INDEX IF NOT EXISTS idx_cqc_providers_regdate ON cqc_providers(registration_date);
CREATE INDEX IF NOT EXISTS idx_cqc_providers_ch ON cqc_providers(companies_house_number);
CREATE INDEX IF NOT EXISTS idx_cqc_providers_postcode ON cqc_providers(postal_code);

CREATE TABLE IF NOT EXISTS cqc_locations (
    location_id TEXT PRIMARY KEY,
    provider_id TEXT,
    name TEXT,
    also_known_as TEXT,
    organisation_type TEXT,
    type TEXT,
    ods_code TEXT,
    brand_id TEXT,
    brand_name TEXT,
    uprn TEXT,
    registration_status TEXT,
    registration_date TEXT,
    deregistration_date TEXT,
    dormancy TEXT,
    dormancy_start_date TEXT,
    dormancy_end_date TEXT,
    number_of_beds INTEGER,
    registered_manager_absent_date TEXT,
    care_home TEXT,
    website TEXT,
    main_phone_number TEXT,
    address_line_1 TEXT,
    address_line_2 TEXT,
    town_city TEXT,
    county TEXT,
    region TEXT,
    postal_code TEXT,
    latitude REAL,
    longitude REAL,
    icb_code TEXT,
    icb_name TEXT,
    ccg_code TEXT,
    ccg_name TEXT,
    inspection_directorate TEXT,
    constituency TEXT,
    local_authority TEXT,
    last_inspection_date TEXT,
    last_report_date TEXT,
    location_types TEXT,
    service_types TEXT,
    specialisms TEXT,
    regulated_activities TEXT,
    inspection_categories TEXT,
    current_rating TEXT,
    current_rating_date TEXT,
    raw_json TEXT,
    source TEXT NOT NULL DEFAULT 'CQC',
    first_seen_at TEXT,
    cqc_synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_provider ON cqc_locations(provider_id);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_status ON cqc_locations(registration_status);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_regdate ON cqc_locations(registration_date);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_postcode ON cqc_locations(postal_code);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_region ON cqc_locations(region);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_la ON cqc_locations(local_authority);

CREATE TABLE IF NOT EXISTS cqc_provider_locations (
    provider_id TEXT NOT NULL,
    location_id TEXT NOT NULL,
    PRIMARY KEY (provider_id, location_id)
);
CREATE INDEX IF NOT EXISTS idx_cqc_pl_location ON cqc_provider_locations(location_id);

CREATE TABLE IF NOT EXISTS cqc_regulated_activities (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    contacts_json TEXT,
    PRIMARY KEY (entity_type, entity_id, code)
);
CREATE TABLE IF NOT EXISTS cqc_service_types (
    location_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    PRIMARY KEY (location_id, name)
);
CREATE TABLE IF NOT EXISTS cqc_specialisms (
    location_id TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (location_id, name)
);
CREATE TABLE IF NOT EXISTS cqc_ratings (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    report_link_id TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    report_date TEXT,
    overall_rating TEXT,
    key_questions_json TEXT,
    service_ratings_json TEXT,
    PRIMARY KEY (entity_type, entity_id, report_link_id)
);
CREATE TABLE IF NOT EXISTS cqc_reports (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    link_id TEXT NOT NULL,
    report_date TEXT,
    report_uri TEXT,
    report_type TEXT,
    first_visit_date TEXT,
    PRIMARY KEY (entity_type, entity_id, link_id)
);
CREATE TABLE IF NOT EXISTS cqc_relationships (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    related_id TEXT NOT NULL,
    related_name TEXT,
    type TEXT NOT NULL DEFAULT '',
    reason TEXT,
    PRIMARY KEY (entity_type, entity_id, related_id, type)
);

CREATE TABLE IF NOT EXISTS cqc_classifications (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    segments TEXT,
    primary_segment TEXT,
    is_target INTEGER NOT NULL DEFAULT 0,
    location_count INTEGER,
    active_location_count INTEGER,
    total_beds INTEGER,
    size_tier TEXT,
    signals TEXT,
    lead_score INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_cqc_class_score ON cqc_classifications(entity_type, lead_score);

CREATE TABLE IF NOT EXISTS cqc_sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    window_start TEXT,
    window_end TEXT,
    records_processed INTEGER NOT NULL DEFAULT 0,
    records_failed INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
CREATE TABLE IF NOT EXISTS cqc_sync_failures (
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_attempt_at TEXT,
    PRIMARY KEY (entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS cqc_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS lead_accounts (
    account_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'NEW',
    sales_owner TEXT,
    tags TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS lead_contacts (
    contact_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    organisation_website TEXT,
    general_email TEXT,
    recruitment_email TEXT,
    telephone TEXT,
    decision_maker TEXT,
    decision_maker_role TEXT,
    decision_maker_email TEXT,
    linkedin_url TEXT,
    company_linkedin TEXT,
    source TEXT,
    last_verified TEXT,
    confidence TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_lead_contacts_account ON lead_contacts(account_id);
CREATE TABLE IF NOT EXISTS lead_notes (
    note_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lead_activities (
    activity_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    activity TEXT NOT NULL,
    detail TEXT,
    author TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lead_lists (
    list_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lead_list_members (
    list_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (list_id, provider_id)
);
CREATE TABLE IF NOT EXISTS saved_searches (
    search_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

PG_RLS_SQL = "\n".join(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;" for t in CQC_TABLES)
