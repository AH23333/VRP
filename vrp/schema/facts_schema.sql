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
    source TEXT NOT NULL DEFAULT 'etl',
    anchor_fid TEXT,
    related_fid TEXT,
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_findings_pathology ON findings(pathology);
CREATE INDEX IF NOT EXISTS idx_findings_source ON findings(source);
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

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY,
    qualified_name TEXT NOT NULL,
    file_path TEXT NOT NULL DEFAULT '',
    start_line INTEGER,
    end_line INTEGER,
    symbol_hash TEXT NOT NULL DEFAULT '',
    window_hash TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT 'ai',
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_annotations_qn ON annotations(qualified_name);
CREATE INDEX IF NOT EXISTS idx_annotations_status ON annotations(status);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    tid TEXT NOT NULL,
    kind TEXT NOT NULL,
    declared_moves TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'proposed',
    payload_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_transactions_tid ON transactions(tid);

CREATE TABLE IF NOT EXISTS gate_results (
    id INTEGER PRIMARY KEY,
    gate TEXT NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_gate_results_gate ON gate_results(gate);

CREATE TABLE IF NOT EXISTS dossier_views (
    id INTEGER PRIMARY KEY,
    view TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_dossier_views_view ON dossier_views(view);

CREATE TABLE IF NOT EXISTS slices (
    id INTEGER PRIMARY KEY,
    slice_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_slices_status ON slices(status);

CREATE TABLE IF NOT EXISTS structure (
    id INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_decisions_target ON decisions(kind, target_id);

CREATE TABLE IF NOT EXISTS baseline (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

DROP VIEW IF EXISTS v_hotspots;
CREATE VIEW v_hotspots AS
SELECT fid, qualified_name, label, file_path, fan_in, fan_out, hint_in, tests, (fan_in + hint_in + tests) AS score FROM (
    SELECT s.fid AS fid, s.qualified_name AS qualified_name, s.label AS label, s.file_path AS file_path,
           (SELECT COUNT(*) FROM edges e WHERE e.dst_fid = s.fid) AS fan_in,
           (SELECT COUNT(*) FROM edges e WHERE e.src_fid = s.fid) AS fan_out,
           (SELECT COUNT(*) FROM usage_hints u WHERE u.dst_fid = s.fid) AS hint_in,
           (SELECT COUNT(*) FROM test_linkage t WHERE t.target_fid = s.fid) AS tests
    FROM symbols s
    WHERE s.file_path != ''
      AND s.file_path NOT GLOB '<*'
      AND s.file_path NOT GLOB '>*'
);

DROP VIEW IF EXISTS v_cycles;
CREATE VIEW v_cycles AS
SELECT a.src_fid AS fid_a, a.dst_fid AS fid_b
FROM edges a
JOIN edges b ON b.src_fid = a.dst_fid AND b.dst_fid = a.src_fid
WHERE a.src_fid < a.dst_fid
GROUP BY a.src_fid, a.dst_fid;

DROP VIEW IF EXISTS v_dead_code;
CREATE VIEW v_dead_code AS
WITH cand AS (
  SELECT s.*, replace(s.file_path, '\', '/') AS norm
  FROM symbols s
  WHERE s.label IN ('Function', 'Method')
    AND s.file_path != ''
    AND s.name != ''
    AND s.file_path NOT GLOB '<*'
    AND s.file_path NOT GLOB '>*'
)
SELECT c.fid, c.qualified_name, c.label, c.file_path, c.name, c.start_line, c.end_line
FROM cand c
WHERE c.name NOT IN ('main', 'run', 'app', 'index', 'setup', 'teardown', 'handler', 'cli')
  AND LOWER(c.name) != 'test'
  AND c.name NOT GLOB 'test_*'
  AND c.name NOT GLOB '*_test'
  AND c.name NOT GLOB '__*__'
  AND NOT (c.norm GLOB 'test_*' OR c.norm GLOB '*/test_*')
  AND c.norm NOT GLOB '*_test.go'
  AND c.norm NOT GLOB '*_test.py'
  AND c.norm NOT GLOB '*_test.rs'
  AND c.norm NOT GLOB '*_test.cpp'
  AND c.norm NOT GLOB '*_test.lua'
  AND c.norm NOT GLOB '*Test.java'
  AND c.norm NOT GLOB '*Test.kt'
  AND c.norm NOT GLOB '*Test.cs'
  AND c.norm NOT GLOB '*Test.php'
  AND c.norm NOT GLOB '*Spec.scala'
  AND c.norm NOT GLOB '*_spec.rb'
  AND c.norm NOT GLOB '*.test.ts*'
  AND c.norm NOT GLOB '*.spec.ts*'
  AND c.norm NOT GLOB '*.test.js*'
  AND c.norm NOT GLOB '*.spec.js*'
  AND c.norm NOT GLOB '*.test.tsx*'
  AND c.norm NOT GLOB '*.spec.tsx*'
  AND c.norm NOT GLOB '*__tests__/*'
  AND c.norm NOT GLOB '*tests/*'
  AND c.norm NOT GLOB '*test/*'
  AND c.norm NOT GLOB '*spec/*'
  AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.dst_fid = c.fid AND e.type IN ('CALLS', 'CALL_REFERENCE', 'MENTIONS', 'SPAWNS'))
  AND NOT EXISTS (SELECT 1 FROM usage_hints u WHERE u.dst_fid = c.fid)
  AND NOT EXISTS (SELECT 1 FROM test_linkage t WHERE t.target_fid = c.fid);

DROP VIEW IF EXISTS v_dup_clusters;
CREATE VIEW v_dup_clusters AS
SELECT content_hash, COUNT(*) AS copies,
       GROUP_CONCAT(qualified_name, '|') AS members,
       GROUP_CONCAT(DISTINCT file_path) AS files
FROM symbols
WHERE content_hash != ''
GROUP BY content_hash
HAVING COUNT(*) > 1;

DROP VIEW IF EXISTS v_coupling;
CREATE VIEW v_coupling AS
SELECT sf.file_path AS file_a, st.file_path AS file_b, COUNT(*) AS edge_count
FROM edges e
JOIN symbols sf ON sf.fid = e.src_fid
JOIN symbols st ON st.fid = e.dst_fid
WHERE sf.file_path != st.file_path
  AND sf.file_path != ''
  AND st.file_path != ''
  AND sf.file_path NOT GLOB '<*'
  AND sf.file_path NOT GLOB '>*'
  AND st.file_path NOT GLOB '<*'
  AND st.file_path NOT GLOB '>*'
GROUP BY sf.file_path, st.file_path;
