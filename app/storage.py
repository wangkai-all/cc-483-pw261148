"""SQLite persistence layer for the metric semantic layer.

All tables use surrogate integer ids; metric identity is (code, version).
Published versions are immutable — only drafts may be updated or discarded.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "metrics.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Registered source tables (the physical layer the semantic layer sits on)
CREATE TABLE IF NOT EXISTS source_tables (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    columns     TEXT NOT NULL,                -- JSON: [{name, type, semantic}]
    created_at  TEXT NOT NULL
);

-- One row per metric code; lifecycle state lives on the current draft.
CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    owner       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Every published (or draft) revision of a metric is an immutable-ish row.
-- status: draft | published | deprecated
CREATE TABLE IF NOT EXISTS metric_versions (
    id               INTEGER PRIMARY KEY,
    metric_id        INTEGER NOT NULL REFERENCES metrics(id),
    version          INTEGER NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('draft','published','deprecated')),
    description      TEXT NOT NULL DEFAULT '',
    -- semantic definition, fully snapshotted so old versions stay recomputable
    source_table_id  INTEGER NOT NULL REFERENCES source_tables(id),
    measure_field    TEXT NOT NULL,
    agg_func         TEXT NOT NULL CHECK (agg_func IN
                         ('SUM','COUNT','COUNT_DISTINCT','AVG','MAX','MIN','RATIO')),
    ratio_num_field  TEXT,                    -- RATIO metrics: numerator field
    ratio_den_field  TEXT,                    -- RATIO metrics: denominator field
    dimensions       TEXT NOT NULL,           -- JSON list of dimension columns
    filters          TEXT NOT NULL,           -- JSON list of {field, op, value}
    time_grain       TEXT NOT NULL DEFAULT 'day'
                     CHECK (time_grain IN ('day','week','month','quarter','year')),
    additivity       TEXT NOT NULL CHECK (additivity IN ('additive','semi_additive','non_additive')),
    change_note      TEXT NOT NULL DEFAULT '',
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    published_at     TEXT,
    UNIQUE (metric_id, version)
);

-- lineage edges: version -> upstream source table / other metric version
CREATE TABLE IF NOT EXISTS lineage_edges (
    id                INTEGER PRIMARY KEY,
    version_id        INTEGER NOT NULL REFERENCES metric_versions(id) ON DELETE CASCADE,
    upstream_kind     TEXT NOT NULL CHECK (upstream_kind IN ('table','metric')),
    upstream_ref      TEXT NOT NULL,          -- table name or "code@version"
    field_name        TEXT NOT NULL DEFAULT '',
    UNIQUE (version_id, upstream_kind, upstream_ref, field_name)
);

-- lightweight RBAC: role -> permission set; users carry one role
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL CHECK (role IN ('admin','analyst','viewer')),
    created_at TEXT NOT NULL
);

-- per-user grants to read restricted dimensions/measures (column-level security)
CREATE TABLE IF NOT EXISTS column_grants (
    id        INTEGER PRIMARY KEY,
    username  TEXT NOT NULL REFERENCES users(username),
    resource  TEXT NOT NULL,                  -- fully-qualified: table.column
    UNIQUE (username, resource)
);

-- query cache; key is a hash of the full semantic request incl. metric version
CREATE TABLE IF NOT EXISTS query_cache (
    cache_key   TEXT PRIMARY KEY,
    metric_code TEXT NOT NULL,
    version     INTEGER NOT NULL,
    payload     TEXT NOT NULL,                -- JSON result rows + meta
    created_at  TEXT NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0
);

-- audit trail for publish / rollback / conflict-block events
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    entity     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT ''
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | str | None = None) -> None:
    with get_db(path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# -- small JSON helpers (definitions are stored as JSON text columns) -------

def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(text: str) -> Any:
    return json.loads(text) if text else None
