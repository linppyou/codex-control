"""Schema used for newly initialized databases."""

_API_KEYS = """
CREATE TABLE IF NOT EXISTS {keys} (
    id INTEGER PRIMARY KEY,
    public_id TEXT NOT NULL UNIQUE,
    secret_hash BLOB NOT NULL,
    secret_ciphertext BLOB,
    secret_version INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    username TEXT,
    allow_fast INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    revoked_at INTEGER,
    last_used_at INTEGER,
    CHECK (revoked_at IS NOT NULL OR (username IS NOT NULL AND username <> ''))
)
"""


_API_USAGE = """
CREATE TABLE IF NOT EXISTS {usage} (
    request_id TEXT PRIMARY KEY,
    key_pk INTEGER NOT NULL REFERENCES {keys}(id),
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    model_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    status TEXT NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    first_output_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    reasoning_output_tokens INTEGER,
    total_tokens INTEGER,
    requested_tier TEXT,
    actual_tier TEXT
)
"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL UNIQUE,
    email TEXT,
    account_id TEXT NOT NULL UNIQUE,
    plan_type TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_refresh INTEGER NOT NULL,
    access_expires_at INTEGER NOT NULL,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at INTEGER,
    created_at INTEGER NOT NULL,
    display_name TEXT
);
CREATE TABLE IF NOT EXISTS account_secrets (
    account_pk INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    blob BLOB NOT NULL,
    version INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    started_at INTEGER NOT NULL,
    ended_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS assignments_one_active
    ON assignments(username) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS quota_samples (
    id INTEGER PRIMARY KEY,
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    ts INTEGER NOT NULL,
    win TEXT NOT NULL,
    used_percent REAL NOT NULL,
    window_minutes INTEGER,
    resets_at INTEGER,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quota_samples_latest ON quota_samples(account_pk, win, ts);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    account_pk INTEGER REFERENCES accounts(id),
    ts INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    reasoning_output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    session_file TEXT NOT NULL,
    model TEXT
);
CREATE INDEX IF NOT EXISTS usage_events_user_ts ON usage_events(username, ts);
CREATE INDEX IF NOT EXISTS usage_events_user_session ON usage_events(username, session_file);
CREATE TABLE IF NOT EXISTS log_offsets (
    path TEXT PRIMARY KEY,
    inode INTEGER NOT NULL,
    offset INTEGER NOT NULL,
    last_totals TEXT,
    last_model TEXT
);
CREATE TABLE IF NOT EXISTS model_prices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    input_usd REAL NOT NULL,
    cached_usd REAL NOT NULL,
    output_usd REAL NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS alerts_one_open
    ON alerts(kind, target) WHERE resolved_at IS NULL;
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS admins (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    password_configured INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    csrf TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    system_uid INTEGER
);
CREATE TABLE IF NOT EXISTS web_system_users (
    username TEXT PRIMARY KEY,
    uid INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS site_servers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    host_key TEXT,
    public_key TEXT NOT NULL,
    private_key BLOB NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    UNIQUE (host, port)
);
CREATE TABLE IF NOT EXISTS site_inventories (
    server_id TEXT PRIMARY KEY REFERENCES site_servers(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    attempted_at INTEGER NOT NULL,
    synced_at INTEGER,
    error TEXT,
    snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_enrollments (
    server_id TEXT PRIMARY KEY REFERENCES site_servers(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_ciphertext BLOB NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS site_inventory_generations (
    server_id TEXT PRIMARY KEY REFERENCES site_servers(id) ON DELETE CASCADE,
    generation INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS site_domain_refreshes (
    server_id TEXT NOT NULL REFERENCES site_servers(id) ON DELETE CASCADE,
    task_id TEXT NOT NULL,
    PRIMARY KEY (server_id, task_id)
);
CREATE TABLE IF NOT EXISTS dispatch_prompt_template (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    code TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_skills (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('dispatcher', 'migration')),
    name TEXT NOT NULL UNIQUE,
    version TEXT NOT NULL,
    label TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    archive BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS dispatch_targets (
    id TEXT PRIMARY KEY,
    server_id TEXT NOT NULL REFERENCES site_servers(id) ON DELETE CASCADE,
    server_revision INTEGER NOT NULL,
    domain TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    credential_env TEXT NOT NULL,
    project TEXT NOT NULL,
    users TEXT NOT NULL,
    UNIQUE (server_id, domain)
);
CREATE TABLE IF NOT EXISTS dispatch_tasks (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    recipient TEXT NOT NULL,
    recipient_uid INTEGER NOT NULL,
    target_id TEXT NOT NULL,
    target_snapshot TEXT NOT NULL,
    skills TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL,
    prompt TEXT NOT NULL,
    csv TEXT NOT NULL,
    upload BLOB,
    model TEXT,
    reasoning_effort TEXT,
    sandbox TEXT NOT NULL DEFAULT 'danger-full-access',
    working_directory TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    confirmed_at INTEGER,
    account_label TEXT,
    thread_id TEXT,
    turn_id TEXT,
    turn_status TEXT,
    result_text TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS dispatch_tasks_owner ON dispatch_tasks(owner, created_at DESC);
CREATE INDEX IF NOT EXISTS dispatch_tasks_queue ON dispatch_tasks(status, confirmed_at);
CREATE TABLE IF NOT EXISTS dispatch_children (
    task_id TEXT NOT NULL REFERENCES dispatch_tasks(id) ON DELETE CASCADE,
    row_number INTEGER NOT NULL,
    dispatch_id TEXT NOT NULL,
    title TEXT NOT NULL,
    thread_id TEXT,
    turn_id TEXT,
    status TEXT NOT NULL,
    result_text TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (task_id, row_number),
    UNIQUE (task_id, dispatch_id)
);
CREATE TABLE IF NOT EXISTS dispatch_tool_calls (
    task_id TEXT NOT NULL REFERENCES dispatch_tasks(id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    response TEXT,
    PRIMARY KEY (task_id, call_id)
);
CREATE TABLE IF NOT EXISTS account_models (
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    model_id TEXT NOT NULL,
    upstream_available INTEGER NOT NULL DEFAULT 1,
    selectable INTEGER NOT NULL DEFAULT 1,
    discovered_at INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    verified_at INTEGER,
    fast_verified_at INTEGER,
    PRIMARY KEY (account_pk, model_id)
);
CREATE TABLE IF NOT EXISTS account_model_catalogs (
    account_pk INTEGER NOT NULL REFERENCES accounts(id),
    client_version TEXT NOT NULL,
    catalog TEXT NOT NULL,
    synced_at INTEGER NOT NULL,
    PRIMARY KEY (account_pk, client_version)
);
""" + _API_KEYS.format(keys="api_keys") + ";" + _API_USAGE.format(usage="api_usage_events", keys="api_keys") + ";"

