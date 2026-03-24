PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- =============================================================
-- SCHEMA VERSION
-- =============================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  REAL NOT NULL
);

-- =============================================================
-- FILE REGISTRY
-- =============================================================
CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    sha256      TEXT NOT NULL,
    language    TEXT,
    line_count  INTEGER NOT NULL DEFAULT 0,
    byte_size   INTEGER NOT NULL DEFAULT 0,
    last_parsed REAL NOT NULL,
    is_entry    BOOLEAN NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_language ON files(language);

-- =============================================================
-- FILE TAGS (generated, vendored, etc.)
-- =============================================================
CREATE TABLE IF NOT EXISTS file_tags (
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    PRIMARY KEY (file_id, tag)
);

-- =============================================================
-- SYMBOLS (graph nodes)
-- =============================================================
CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    qualified   TEXT NOT NULL,
    kind        TEXT NOT NULL,
    line_start  INTEGER NOT NULL,
    line_end    INTEGER NOT NULL,
    signature   TEXT,
    docstring   TEXT,
    body_text   TEXT,
    visibility  TEXT DEFAULT 'public',
    decorators  TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);

-- =============================================================
-- EDGES (graph relationships)
-- =============================================================
CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    target_id   INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 1.0,
    metadata    TEXT,
    UNIQUE(source_id, target_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);

-- =============================================================
-- UNRESOLVED IMPORTS
-- =============================================================
CREATE TABLE IF NOT EXISTS unresolved_imports (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    import_path TEXT NOT NULL,
    line        INTEGER NOT NULL,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_unresolved_file ON unresolved_imports(file_id);

-- =============================================================
-- SESSION ACTIONS
-- =============================================================
CREATE TABLE IF NOT EXISTS session_actions (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL,
    symbol      TEXT,
    timestamp   REAL NOT NULL,
    metadata    TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_session ON session_actions(session_id);
CREATE INDEX IF NOT EXISTS idx_session_target ON session_actions(target);

-- =============================================================
-- QUERY HISTORY (analytics)
-- =============================================================
CREATE TABLE IF NOT EXISTS query_history (
    id          INTEGER PRIMARY KEY,
    query       TEXT NOT NULL,
    result_files TEXT,
    confidence  TEXT,
    session_id  TEXT NOT NULL,
    timestamp   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_query_history_ts ON query_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_query_history_query ON query_history(query);

-- =============================================================
-- CROSS-SESSION DECISIONS
-- =============================================================
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY,
    content     TEXT NOT NULL,
    type        TEXT NOT NULL DEFAULT 'decision',
    tags        TEXT,
    files       TEXT,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    session_id  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_expires ON decisions(expires_at);

-- =============================================================
-- CROSS-PROJECT EDGES
-- =============================================================
CREATE TABLE IF NOT EXISTS cross_project_edges (
    id              INTEGER PRIMARY KEY,
    source_project  TEXT NOT NULL,
    source_import   TEXT NOT NULL,
    target_project  TEXT NOT NULL,
    target_qualified TEXT NOT NULL,
    target_file     TEXT NOT NULL,
    UNIQUE(source_project, source_import, target_project, target_qualified)
);
CREATE INDEX IF NOT EXISTS idx_cross_edges_source ON cross_project_edges(source_project);
CREATE INDEX IF NOT EXISTS idx_cross_edges_target ON cross_project_edges(target_project);

-- =============================================================
-- SCAN METADATA
-- =============================================================
CREATE TABLE IF NOT EXISTS scan_meta (
    id              INTEGER PRIMARY KEY,
    started_at      REAL NOT NULL,
    completed_at    REAL,
    files_total     INTEGER NOT NULL DEFAULT 0,
    files_changed   INTEGER NOT NULL DEFAULT 0,
    symbols_total   INTEGER NOT NULL DEFAULT 0,
    edges_total     INTEGER NOT NULL DEFAULT 0,
    duration_ms     INTEGER
);
