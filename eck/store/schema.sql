-- ECK knowledge store.
--
-- Design rules this schema enforces, not merely permits:
--   BR-09  every derived item points at an exact source location
--   BR-10  indirect / unresolved links are marked 'inferred', never 'derived'
--   BR-11  rows carry no wall-clock time; time lives on refresh_run, so a
--          re-run over unchanged source produces byte-identical rows
--   BR-12  allowed node/edge kinds are a closed set — an unknown kind is a
--          constraint violation, i.e. a loud failure, not a silent skip
--   BR-13  what could NOT be interpreted is recorded, not discarded
--   BR-17  every row carries origin + confidence
--   BR-20  'derived' vs 'curated' vs 'inferred' is queryable

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- provenance

CREATE TABLE refresh_run (
  id             TEXT PRIMARY KEY,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  stage          TEXT NOT NULL,
  status         TEXT NOT NULL CHECK(status IN ('running','ok','failed')),
  estate_id      TEXT NOT NULL,
  register_sha   TEXT NOT NULL,          -- sha256 of estate.yaml as applied
  notes          TEXT
);

-- ---------------------------------------------------------------- CAP-1

CREATE TABLE asset (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  role           TEXT NOT NULL,
  owner          TEXT NOT NULL,
  tech           TEXT NOT NULL,
  source_kind    TEXT NOT NULL,          -- 'code' | 'wiki'
  abs_path       TEXT NOT NULL,          -- path on the BUILD machine
  -- Same location relative to the project root, when it sits under it. This
  -- is what lets a database built on one machine resolve source on another,
  -- and what lets a server with no source at all fail softly instead of hard.
  rel_path       TEXT,
  source_commit  TEXT,
  run_id         TEXT NOT NULL REFERENCES refresh_run(id)
);

CREATE TABLE exclusion (
  asset_id       TEXT NOT NULL REFERENCES asset(id),
  path_glob      TEXT NOT NULL,
  reason         TEXT NOT NULL,          -- BR-04: a reason is mandatory
  run_id         TEXT NOT NULL REFERENCES refresh_run(id)
);

-- ---------------------------------------------------------------- CAP-2

CREATE TABLE node (
  id             TEXT PRIMARY KEY,       -- deterministic: sha256(asset|kind|fqn)
  asset_id       TEXT NOT NULL REFERENCES asset(id),
  kind           TEXT NOT NULL CHECK(kind IN (
                   'module','package','class','interface','enum','record',
                   'method','field','entity','store','view','entry_point',
                   'service','event_handler','security_role','config_property',
                   'integration_point')),
  name           TEXT NOT NULL,
  fqn            TEXT NOT NULL,
  signature      TEXT,
  path           TEXT NOT NULL,          -- BR-09, relative to asset root
  start_line     INTEGER NOT NULL,
  end_line       INTEGER NOT NULL,
  span_sha       TEXT NOT NULL,          -- sha256 of the exact source span
  origin         TEXT NOT NULL CHECK(origin IN ('derived','curated','inferred')),
  confidence     REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
  attrs          TEXT NOT NULL DEFAULT '{}',   -- JSON, sorted keys
  run_id         TEXT NOT NULL REFERENCES refresh_run(id)
);

CREATE INDEX idx_node_fqn    ON node(fqn);
CREATE INDEX idx_node_kind   ON node(asset_id, kind);
CREATE INDEX idx_node_path   ON node(asset_id, path);

CREATE TABLE edge (
  id             TEXT PRIMARY KEY,
  src_id         TEXT NOT NULL REFERENCES node(id),
  dst_id         TEXT NOT NULL REFERENCES node(id),
  kind           TEXT NOT NULL CHECK(kind IN (
                   'belongs_to','invokes','reads','writes','exposes',
                   'extends','implements','injects','subscribes','declares')),
  path           TEXT NOT NULL,
  start_line     INTEGER NOT NULL,
  origin         TEXT NOT NULL CHECK(origin IN ('derived','curated','inferred')),
  confidence     REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
  attrs          TEXT NOT NULL DEFAULT '{}',
  run_id         TEXT NOT NULL REFERENCES refresh_run(id)
);

CREATE INDEX idx_edge_src ON edge(src_id, kind);
CREATE INDEX idx_edge_dst ON edge(dst_id, kind);

-- ---------------------------------------------------------------- BR-13
-- Everything the extractor could not interpret. This table existing and
-- being reported is what makes the coverage claim honest.

CREATE TABLE parse_failure (
  asset_id       TEXT NOT NULL REFERENCES asset(id),
  path           TEXT NOT NULL,
  reason         TEXT NOT NULL,
  detail         TEXT,
  run_id         TEXT NOT NULL REFERENCES refresh_run(id)
);

CREATE TABLE unresolved_ref (
  asset_id       TEXT NOT NULL REFERENCES asset(id),
  src_id         TEXT NOT NULL REFERENCES node(id),
  kind           TEXT NOT NULL,          -- what we were trying to resolve
  raw            TEXT NOT NULL,          -- the expression as written
  path           TEXT NOT NULL,
  start_line     INTEGER NOT NULL,
  reason         TEXT NOT NULL,
  run_id         TEXT NOT NULL REFERENCES refresh_run(id)
);

CREATE INDEX idx_unres_asset ON unresolved_ref(asset_id, reason);

-- ---------------------------------------------------------------- CAP-3 / CAP-5
-- Curated documentation and the retrieval index.
--
-- Wiki content is CURATED knowledge, kept apart from derived structure
-- (data requirement: generated and curated knowledge stay separate).
-- Note: vectors live in a BLOB column rather than a vector extension —
-- this Python's sqlite3 has extension loading disabled, so sqlite-vec
-- cannot be loaded. Brute-force cosine over ~20k chunks is milliseconds.

CREATE TABLE doc (
  id               TEXT PRIMARY KEY,
  asset_id         TEXT NOT NULL REFERENCES asset(id),
  title            TEXT NOT NULL,
  path             TEXT NOT NULL,
  -- BR-19: the wiki's own git history IS the curated version history
  last_commit      TEXT,
  last_author      TEXT,
  last_commit_date TEXT,
  origin           TEXT NOT NULL CHECK(origin IN ('derived','curated','inferred')),
  run_id           TEXT NOT NULL REFERENCES refresh_run(id)
);

CREATE TABLE chunk (
  id           TEXT PRIMARY KEY,
  asset_id     TEXT NOT NULL REFERENCES asset(id),
  source       TEXT NOT NULL CHECK(source IN ('wiki','code')),  -- BR-34
  doc_id       TEXT REFERENCES doc(id),
  node_id      TEXT REFERENCES node(id),
  heading      TEXT,                    -- heading path, for wiki chunks
  text         TEXT NOT NULL,
  path         TEXT NOT NULL,           -- BR-35: every result carries a location
  start_line   INTEGER NOT NULL,
  end_line     INTEGER NOT NULL,
  span_sha     TEXT NOT NULL,
  origin       TEXT NOT NULL CHECK(origin IN ('derived','curated','inferred')),
  run_id       TEXT NOT NULL REFERENCES refresh_run(id)
);

CREATE INDEX idx_chunk_source ON chunk(source, asset_id);
CREATE INDEX idx_chunk_node   ON chunk(node_id);

-- Keyword half of hybrid retrieval.
CREATE VIRTUAL TABLE chunk_fts USING fts5(
  text,
  chunk_id UNINDEXED,
  tokenize = 'porter unicode61'
);

-- Meaning half (BR-33). float32, L2-normalised, so cosine == dot product.
CREATE TABLE chunk_vec (
  chunk_id  TEXT PRIMARY KEY REFERENCES chunk(id),
  dim       INTEGER NOT NULL,
  vec       BLOB NOT NULL
);

CREATE TABLE embedding_model (
  run_id    TEXT NOT NULL REFERENCES refresh_run(id),
  name      TEXT NOT NULL,
  dim       INTEGER NOT NULL,
  local     INTEGER NOT NULL   -- 1 = ran inside the boundary (NFR-10)
);

-- ---------------------------------------------------------------- CAP-3 anchors
-- The link between a business statement and a specific software location.
-- BR-16: every business statement must point at real code.
--
-- These rows are PROJECTIONS. The durable record lives in curated/anchors.jsonl,
-- outside this file, because a refresh deletes and rebuilds knowledge.db and
-- curated knowledge must survive that (BR-68; "generated knowledge is
-- disposable, curated knowledge is retained"). Every refresh re-imports the
-- jsonl and re-validates it against freshly derived structure.

CREATE TABLE anchor (
  id                   TEXT PRIMARY KEY,
  -- the statement side
  chunk_id             TEXT,             -- wiki chunk; may vanish if the page changed
  statement            TEXT NOT NULL,
  statement_sha        TEXT NOT NULL,
  doc_path             TEXT NOT NULL,
  doc_start_line       INTEGER NOT NULL,
  doc_end_line         INTEGER NOT NULL,
  -- the software side, by DURABLE identity (fqn), not by node id
  target_asset         TEXT NOT NULL,
  target_kind          TEXT NOT NULL,
  target_fqn           TEXT NOT NULL,
  target_node_id       TEXT,             -- resolved at refresh; NULL when broken
  target_path          TEXT,
  target_start_line    INTEGER,
  target_end_line      INTEGER,
  -- BR-18: the content fingerprint recorded when a human approved this
  span_sha_at_approval TEXT NOT NULL,
  span_sha_now         TEXT,
  state                TEXT NOT NULL CHECK(state IN
                         ('resolved','stale','broken','orphaned')),
  -- BR-17: evidence and confidence
  confidence           REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
  justification        TEXT,
  proposer             TEXT NOT NULL,    -- 'retrieval' | 'llm:<model>'
  reviewed_by          TEXT NOT NULL,    -- BR-19: who signed it
  reviewed_at          TEXT NOT NULL,
  origin               TEXT NOT NULL CHECK(origin = 'curated'),
  run_id               TEXT NOT NULL REFERENCES refresh_run(id)
);

CREATE INDEX idx_anchor_state  ON anchor(state);
CREATE INDEX idx_anchor_target ON anchor(target_asset, target_fqn);
CREATE INDEX idx_anchor_chunk  ON anchor(chunk_id);
