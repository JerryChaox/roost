-- roost's database (contracts §11): what roost decides, in three tables, plus
-- the serve lock (contracts §12). Timestamps are RFC 3339 UTC text with a
-- fixed width (2006-01-02T15:04:05.000Z), so they also compare as text.

CREATE TABLE IF NOT EXISTS workspaces (
    tenant         TEXT NOT NULL,
    name           TEXT NOT NULL,
    owner          TEXT NOT NULL,
    forked_from    TEXT,
    created_at     TEXT NOT NULL,
    runtime_config TEXT NOT NULL,          -- resolved JSON, never a secret value
    phase          TEXT NOT NULL CHECK (phase IN ('provisioning', 'active', 'restoring', 'failed')),
    phase_since    TEXT NOT NULL,
    phase_detail   TEXT,                   -- JSON: the error in failed, the restore in restoring
    PRIMARY KEY (tenant, name),
    CHECK (
        (phase IN ('provisioning', 'active') AND phase_detail IS NULL) OR
        (phase IN ('failed', 'restoring') AND phase_detail IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS grants (
    id             TEXT PRIMARY KEY,       -- the grant's start: g_<ulid>
    tenant         TEXT NOT NULL,
    workspace      TEXT NOT NULL,
    sandbox_id     TEXT NOT NULL,
    driver_token   TEXT NOT NULL,
    backup_token   TEXT NOT NULL,
    model_key_ref  TEXT,                   -- the LLM gateway's id of the model key; none on E2B Cloud
    issued_at      TEXT NOT NULL,
    ended_at       TEXT,
    end_reason     TEXT CHECK (end_reason IN ('driver_lost', 'stalled', 'rebooted', 'recover', 'upgrade', 'restore')),
    run            TEXT,                   -- the conversation and run a restart or reboot was for
    key_revoked_at TEXT,
    FOREIGN KEY (tenant, workspace) REFERENCES workspaces (tenant, name),
    CHECK (driver_token <> backup_token),
    CHECK ((ended_at IS NULL) = (end_reason IS NULL))
);

-- At most one live grant per workspace (invariant 1).
CREATE UNIQUE INDEX IF NOT EXISTS grants_live ON grants (tenant, workspace) WHERE ended_at IS NULL;

-- Append-only: the store only ever inserts into it.
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    time    TEXT NOT NULL,
    caller  TEXT NOT NULL,
    tenant  TEXT NOT NULL,
    action  TEXT NOT NULL,
    target  TEXT NOT NULL,
    reason  TEXT,
    request TEXT NOT NULL,                 -- JSON, never a secret
    outcome TEXT
);

-- One roost serve acts at a time: the holder of an unexpired lease.
CREATE TABLE IF NOT EXISTS serve_lock (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    holder     TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
