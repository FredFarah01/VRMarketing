"""Stage 1 schema for CQC account intelligence and sales operations.

Kept separate from marketing_* so imported CQC organisations are prospects/accounts,
not consented inbound marketing leads.
"""

CQC_SALES_MIGRATION = r"""
CREATE TABLE IF NOT EXISTS cqc_providers (
    provider_id TEXT PRIMARY KEY,
    provider_name TEXT NOT NULL,
    organisation_type TEXT,
    registration_status TEXT,
    registration_date TEXT,
    deregistration_date TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    town_city TEXT,
    county TEXT,
    postcode TEXT,
    website TEXT,
    cqc_url TEXT,
    raw_payload TEXT,
    source_updated_at TEXT,
    first_synced_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cqc_providers_name ON cqc_providers(provider_name);
CREATE INDEX IF NOT EXISTS idx_cqc_providers_postcode ON cqc_providers(postcode);
CREATE INDEX IF NOT EXISTS idx_cqc_providers_status ON cqc_providers(registration_status);

CREATE TABLE IF NOT EXISTS cqc_locations (
    location_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    location_name TEXT NOT NULL,
    registration_status TEXT,
    registration_date TEXT,
    deregistration_date TEXT,
    address_line1 TEXT,
    address_line2 TEXT,
    town_city TEXT,
    county TEXT,
    postcode TEXT,
    telephone TEXT,
    website TEXT,
    overall_rating TEXT,
    rating_date TEXT,
    report_publication_date TEXT,
    raw_payload TEXT,
    source_updated_at TEXT,
    first_synced_at TEXT NOT NULL,
    last_synced_at TEXT NOT NULL,
    FOREIGN KEY (provider_id) REFERENCES cqc_providers(provider_id)
);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_provider ON cqc_locations(provider_id);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_postcode ON cqc_locations(postcode);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_rating ON cqc_locations(overall_rating);
CREATE INDEX IF NOT EXISTS idx_cqc_locations_status ON cqc_locations(registration_status);

CREATE TABLE IF NOT EXISTS cqc_location_service_types (
    location_id TEXT NOT NULL,
    service_type_code TEXT NOT NULL,
    service_type_name TEXT,
    PRIMARY KEY (location_id, service_type_code),
    FOREIGN KEY (location_id) REFERENCES cqc_locations(location_id)
);
CREATE INDEX IF NOT EXISTS idx_cqc_service_type_code ON cqc_location_service_types(service_type_code);

CREATE TABLE IF NOT EXISTS cqc_location_specialisms (
    location_id TEXT NOT NULL,
    specialism_code TEXT NOT NULL,
    specialism_name TEXT,
    PRIMARY KEY (location_id, specialism_code),
    FOREIGN KEY (location_id) REFERENCES cqc_locations(location_id)
);

CREATE TABLE IF NOT EXISTS cqc_location_regulated_activities (
    location_id TEXT NOT NULL,
    activity_code TEXT NOT NULL,
    activity_name TEXT,
    PRIMARY KEY (location_id, activity_code),
    FOREIGN KEY (location_id) REFERENCES cqc_locations(location_id)
);

CREATE TABLE IF NOT EXISTS sales_accounts (
    account_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL UNIQUE,
    company_name TEXT NOT NULL,
    website TEXT,
    domain TEXT,
    target_segment TEXT,
    location_count INTEGER NOT NULL DEFAULT 0,
    account_score INTEGER NOT NULL DEFAULT 0,
    priority TEXT NOT NULL DEFAULT 'C',
    lifecycle_stage TEXT NOT NULL DEFAULT 'Prospect',
    owner TEXT,
    sales_status TEXT NOT NULL DEFAULT 'Unworked',
    email_status TEXT,
    telephone_status TEXT,
    last_contacted_at TEXT,
    next_action TEXT,
    next_action_at TEXT,
    score_reasons TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (provider_id) REFERENCES cqc_providers(provider_id)
);
CREATE INDEX IF NOT EXISTS idx_sales_accounts_priority ON sales_accounts(priority);
CREATE INDEX IF NOT EXISTS idx_sales_accounts_score ON sales_accounts(account_score);
CREATE INDEX IF NOT EXISTS idx_sales_accounts_owner ON sales_accounts(owner);
CREATE INDEX IF NOT EXISTS idx_sales_accounts_stage ON sales_accounts(lifecycle_stage);

CREATE TABLE IF NOT EXISTS sales_contacts (
    contact_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    first_name TEXT,
    last_name TEXT,
    job_title TEXT,
    email TEXT,
    phone TEXT,
    linkedin_url TEXT,
    source TEXT,
    source_url TEXT,
    is_decision_maker INTEGER NOT NULL DEFAULT 0,
    email_verified INTEGER NOT NULL DEFAULT 0,
    do_not_contact INTEGER NOT NULL DEFAULT 0,
    lawful_basis TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES sales_accounts(account_id)
);
CREATE INDEX IF NOT EXISTS idx_sales_contacts_account ON sales_contacts(account_id);
CREATE INDEX IF NOT EXISTS idx_sales_contacts_email ON sales_contacts(email);


CREATE TABLE IF NOT EXISTS sales_contact_candidates (
    candidate_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    contact_name TEXT,
    job_title TEXT,
    contact_type TEXT NOT NULL DEFAULT 'Business',
    source_url TEXT NOT NULL,
    source_page_title TEXT,
    discovery_method TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'Found',
    status TEXT NOT NULL DEFAULT 'Review',
    evidence TEXT,
    first_found_at TEXT NOT NULL,
    last_found_at TEXT NOT NULL,
    FOREIGN KEY (account_id) REFERENCES sales_accounts(account_id)
);
CREATE INDEX IF NOT EXISTS idx_contact_candidates_account ON sales_contact_candidates(account_id);
CREATE INDEX IF NOT EXISTS idx_contact_candidates_email ON sales_contact_candidates(email);
CREATE INDEX IF NOT EXISTS idx_contact_candidates_status ON sales_contact_candidates(status);

CREATE TABLE IF NOT EXISTS sales_activities (
    activity_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    contact_id TEXT,
    activity_type TEXT NOT NULL,
    direction TEXT,
    outcome TEXT,
    notes TEXT,
    occurred_at TEXT NOT NULL,
    created_by TEXT,
    FOREIGN KEY (account_id) REFERENCES sales_accounts(account_id),
    FOREIGN KEY (contact_id) REFERENCES sales_contacts(contact_id)
);
CREATE INDEX IF NOT EXISTS idx_sales_activities_account ON sales_activities(account_id);
CREATE INDEX IF NOT EXISTS idx_sales_activities_occurred ON sales_activities(occurred_at);

CREATE TABLE IF NOT EXISTS sales_tasks (
    task_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    contact_id TEXT,
    assigned_to TEXT,
    task_type TEXT NOT NULL,
    title TEXT NOT NULL,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'Open',
    priority TEXT NOT NULL DEFAULT 'Normal',
    created_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (account_id) REFERENCES sales_accounts(account_id),
    FOREIGN KEY (contact_id) REFERENCES sales_contacts(contact_id)
);
CREATE INDEX IF NOT EXISTS idx_sales_tasks_due ON sales_tasks(status, due_at);
CREATE INDEX IF NOT EXISTS idx_sales_tasks_assignee ON sales_tasks(assigned_to, status);

CREATE TABLE IF NOT EXISTS sales_suppressions (
    suppression_id TEXT PRIMARY KEY,
    email TEXT,
    domain TEXT,
    provider_id TEXT,
    reason TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_suppressions_email ON sales_suppressions(email);
CREATE INDEX IF NOT EXISTS idx_sales_suppressions_domain ON sales_suppressions(domain);

CREATE TABLE IF NOT EXISTS cqc_sync_runs (
    sync_id TEXT PRIMARY KEY,
    sync_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    providers_seen INTEGER NOT NULL DEFAULT 0,
    locations_seen INTEGER NOT NULL DEFAULT 0,
    records_changed INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);
"""

CQC_SALES_PG_MIGRATION = CQC_SALES_MIGRATION + r"""
ALTER TABLE cqc_providers ENABLE ROW LEVEL SECURITY;
ALTER TABLE cqc_locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE cqc_location_service_types ENABLE ROW LEVEL SECURITY;
ALTER TABLE cqc_location_specialisms ENABLE ROW LEVEL SECURITY;
ALTER TABLE cqc_location_regulated_activities ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_contact_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_activities ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE sales_suppressions ENABLE ROW LEVEL SECURITY;
ALTER TABLE cqc_sync_runs ENABLE ROW LEVEL SECURITY;
"""
