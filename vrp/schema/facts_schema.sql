PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    fid TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    label TEXT NOT NULL,
    name TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT '',
    start_line INTEGER,
    end_line INTEGER,
    content_hash TEXT NOT NULL DEFAULT '',
    properties_json TEXT,
    UNIQUE (project, qualified_name)
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_label ON symbols(label);

CREATE TABLE IF NOT EXISTS edges (
    src_fid TEXT NOT NULL,
    dst_fid TEXT NOT NULL,
    type TEXT NOT NULL,
    evidence TEXT NOT NULL,
    strategy TEXT,
    confidence REAL,
    line INTEGER,
    properties_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_fid, type);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_fid, type);
CREATE INDEX IF NOT EXISTS idx_edges_evidence ON edges(evidence);

CREATE TABLE IF NOT EXISTS usage_hints (
    src_fid TEXT NOT NULL,
    dst_fid TEXT NOT NULL,
    properties_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_src ON usage_hints(src_fid);
CREATE INDEX IF NOT EXISTS idx_usage_dst ON usage_hints(dst_fid);

CREATE TABLE IF NOT EXISTS coverage_gaps (
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_coverage_kind ON coverage_gaps(kind);

CREATE TABLE IF NOT EXISTS file_hashes (
    file_path TEXT PRIMARY KEY,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history_coupling (
    src_fid TEXT NOT NULL,
    dst_fid TEXT NOT NULL,
    properties_json TEXT
);

CREATE TABLE IF NOT EXISTS test_linkage (
    test_fid TEXT NOT NULL,
    target_fid TEXT NOT NULL,
    source TEXT NOT NULL,
    properties_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_test_target ON test_linkage(target_fid);

CREATE TABLE IF NOT EXISTS runtime_evidence (
    id INTEGER PRIMARY KEY,
    caller_key TEXT,
    callee_key TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    pathology TEXT NOT NULL,
    kind TEXT NOT NULL,
    anchor_fid TEXT,
    related_fid TEXT,
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_findings_pathology ON findings(pathology);
CREATE INDEX IF NOT EXISTS idx_findings_anchor ON findings(anchor_fid);

CREATE TABLE IF NOT EXISTS discrepancy_ledger (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    src_key TEXT,
    dst_key TEXT,
    cbm_edge_type TEXT,
    ca_confidence REAL,
    ca_provenance TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ledger_category ON discrepancy_ledger(category);
