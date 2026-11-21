
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
    task_id         TEXT PRIMARY KEY,
    repo            TEXT,
    created         TEXT NOT NULL,
    updated         TEXT NOT NULL,
    status          TEXT NOT NULL,
    current_phase   TEXT NOT NULL,
    next_step       INTEGER NOT NULL DEFAULT 1,
    deploy_intent   INTEGER NOT NULL DEFAULT 0,
    worktree_path   TEXT,
    branch          TEXT,
    base            TEXT,
    schema_version  INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS steps (
    task_id             TEXT NOT NULL REFERENCES tasks(task_id),
    step                INTEGER NOT NULL,
    phase               TEXT NOT NULL,
    verdict             TEXT,
    artifact_filename   TEXT NOT NULL,
    content_hash        TEXT,
    created_at          TEXT,  -- nullable: a migrated step's real timestamp may
                                -- genuinely be unrecoverable from the legacy
                                -- corpus; NULL means "unknown", never fabricated.
                                -- record_step_and_save() (the live save path,
                                -- not migration) always supplies a real value.
    PRIMARY KEY (task_id, step)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(task_id),
    event_type  TEXT NOT NULL,
    phase       TEXT,
    step        INTEGER,
    verdict     TEXT,
    payload     TEXT,
    timestamp   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(task_id, event_type);
CREATE INDEX IF NOT EXISTS idx_steps_task  ON steps(task_id, step);
