"""Metric registry: registration, versioning, publish-gate, lineage, rollback."""
from __future__ import annotations

import sqlite3
from typing import Any

from . import storage
from .domain import (
    ConflictError,
    Filter,
    MetricSpec,
    SemanticError,
    validate_spec,
    detect_conflict,
)


class Registry:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- sources -------------------------------------------------------------

    def register_source(self, name: str, columns: list[dict], description: str = "") -> int:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO source_tables(name, description, columns, created_at) "
            "VALUES (?, ?, ?, ?)",
            (name, description, storage.dumps(columns), storage.utcnow()),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM source_tables WHERE name = ?", (name,)
        ).fetchone()
        return row["id"]

    def get_source(self, name: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM source_tables WHERE name = ?", (name,)
        ).fetchone()
        if not row:
            raise SemanticError(f"来源表 {name} 未登记")
        return row

    def list_sources(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM source_tables ORDER BY name"))

    def _source_columns(self, table_name: str) -> set[str]:
        return {c["name"] for c in storage.loads(self.get_source(table_name)["columns"])}

    # -- metric lifecycle ----------------------------------------------------

    def register_metric(self, code: str, name: str, owner: str) -> int:
        try:
            cur = self.conn.execute(
                "INSERT INTO metrics(code, name, owner, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (code, name, owner, storage.utcnow(), storage.utcnow()),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            raise SemanticError(f"指标编码 {code} 已存在")

    def list_metrics(self) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT m.*, mv.version, mv.status, mv.agg_func, mv.measure_field,
                   mv.time_grain, mv.additivity, mv.change_note, mv.published_at
            FROM metrics m
            JOIN metric_versions mv ON mv.metric_id = m.id
            WHERE mv.id = (SELECT id FROM metric_versions
                           WHERE metric_id = m.id ORDER BY version DESC LIMIT 1)
            ORDER BY m.code
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_metric(self, code: str) -> dict:
        row = self.conn.execute("SELECT * FROM metrics WHERE code = ?", (code,)).fetchone()
        if not row:
            raise SemanticError(f"指标 {code} 不存在")
        return dict(row)

    def list_versions(self, code: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT mv.*, st.name AS source_table_name
               FROM metric_versions mv
               JOIN metrics m ON m.id = mv.metric_id
               JOIN source_tables st ON st.id = mv.source_table_id
               WHERE m.code = ? ORDER BY mv.version DESC""",
            (code,),
        ).fetchall()
        return [self._version_to_dict(r) for r in rows]

    def get_version(self, code: str, version: int | None = None) -> dict:
        """Return one version; default = latest published, else latest draft."""
        m = self.get_metric(code)
        if version is None:
            row = self.conn.execute(
                "SELECT * FROM metric_versions WHERE metric_id = ? "
                "ORDER BY CASE status WHEN 'published' THEN 0 ELSE 1 END, version DESC LIMIT 1",
                (m["id"],),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM metric_versions WHERE metric_id = ? AND version = ?",
                (m["id"], version),
            ).fetchone()
        if not row:
            raise SemanticError(f"指标 {code} 没有可用版本")
        src = self.conn.execute(
            "SELECT name FROM source_tables WHERE id = ?", (row["source_table_id"],)
        ).fetchone()
        d = self._version_to_dict(row)
        d["source_table"] = src["name"]
        return d

    def get_draft(self, code: str) -> dict | None:
        m = self.get_metric(code)
        row = self.conn.execute(
            "SELECT * FROM metric_versions WHERE metric_id = ? AND status = 'draft' "
            "ORDER BY version DESC LIMIT 1",
            (m["id"],),
        ).fetchone()
        if not row:
            return None
        d = self._version_to_dict(row)
        d["source_table"] = self.conn.execute(
            "SELECT name FROM source_tables WHERE id = ?", (row["source_table_id"],)
        ).fetchone()["name"]
        return d

    def create_draft(self, code: str, spec: MetricSpec, actor: str,
                     change_note: str = "") -> int:
        """Create a new draft version, copying the latest version when unspecified."""
        m = self.get_metric(code)
        validate_spec(spec, self._source_columns(spec.source_table))
        next_version = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM metric_versions WHERE metric_id = ?",
            (m["id"],),
        ).fetchone()[0]
        if self.get_draft(code) is not None:
            raise SemanticError(f"指标 {code} 已存在草稿，请先发布或丢弃")
        source_id = self.get_source(spec.source_table)["id"]
        cur = self.conn.execute(
            """INSERT INTO metric_versions
               (metric_id, version, status, description, source_table_id, measure_field,
                agg_func, ratio_num_field, ratio_den_field, dimensions, filters,
                time_grain, additivity, change_note, created_by, created_at)
               VALUES (?,?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m["id"], next_version, spec.description, source_id, spec.measure_field,
             spec.agg_func, spec.ratio_num_field, spec.ratio_den_field,
             storage.dumps(spec.dimensions), storage.dumps(
                 [f.to_dict() for f in spec.filters]),
             spec.time_grain, spec.additivity, change_note, actor, storage.utcnow()),
        )
        self._rebuild_lineage(cur.lastrowid, spec)
        self.conn.execute(
            "UPDATE metrics SET updated_at = ? WHERE id = ?", (storage.utcnow(), m["id"])
        )
        self._audit(actor, "create_draft", f"{code}@v{next_version}", change_note)
        self.conn.commit()
        return next_version

    def publish(self, code: str, actor: str) -> dict:
        """Publish the pending draft. Blocks on semantic/conflict violations."""
        draft = self.get_draft(code)
        if draft is None:
            raise SemanticError(f"指标 {code} 没有待发布草稿")
        spec = MetricSpec.from_dict(draft)

        # Re-validate structurally (source schema may have changed since drafting).
        validate_spec(spec, self._source_columns(spec.source_table))

        # Global conflict gate against every *other published* metric.
        others = self._published_specs(exclude_code=code)
        other_specs = [s for _, s in others]
        reason = detect_conflict(spec, other_specs)
        if reason:
            self._audit(actor, "publish_blocked", f"{code}@v{draft['version']}", reason)
            self.conn.commit()
            raise ConflictError(f"发布被阻断：{reason}")
        duplicate = next((c for c, s in others if s.signature() == spec.signature()), None)
        if duplicate:
            reason = f"与已发布指标 {duplicate} 的口径完全重复（同源/同聚合/同维度同过滤）"
            self._audit(actor, "publish_blocked", f"{code}@v{draft['version']}", reason)
            self.conn.commit()
            raise ConflictError(f"发布被阻断：{reason}")

        self.conn.execute(
            "UPDATE metric_versions SET status = 'published', published_at = ? WHERE id = ?",
            (storage.utcnow(), draft["id"]),
        )
        self._audit(actor, "publish", f"{code}@v{draft['version']}",
                    draft.get("change_note", ""))
        self.conn.commit()
        return self.get_version(code, draft["version"])

    def discard_draft(self, code: str, actor: str) -> None:
        draft = self.get_draft(code)
        if not draft:
            raise SemanticError(f"指标 {code} 没有草稿")
        self.conn.execute("DELETE FROM metric_versions WHERE id = ?", (draft["id"],))
        self._audit(actor, "discard_draft", f"{code}@v{draft['version']}")
        self.conn.commit()

    def rollback(self, code: str, target_version: int, actor: str) -> int:
        """Roll back by creating a new draft that clones a historical version.

        Published history is never rewritten — the rollback itself is auditable
        and the new draft still passes the conflict gate before going live.
        """
        target = self.get_version(code, target_version)
        if target["status"] != "published":
            raise SemanticError("只能回滚到已发布版本")
        if self.get_draft(code) is not None:
            raise SemanticError(f"指标 {code} 已存在草稿，请先发布或丢弃")
        spec = MetricSpec.from_dict(target)
        new_version = self.create_draft(
            code, spec, actor, change_note=f"回滚自 v{target_version}"
        )
        self._audit(actor, "rollback", f"{code}@v{new_version}",
                    f"克隆历史版本 v{target_version}")
        self.conn.commit()
        return new_version

    def deprecate(self, code: str, actor: str) -> None:
        m = self.get_metric(code)
        self.conn.execute(
            "UPDATE metric_versions SET status = 'deprecated' WHERE metric_id = ? "
            "AND status = 'published'",
            (m["id"],),
        )
        self._audit(actor, "deprecate", code)
        self.conn.commit()

    # -- lineage -------------------------------------------------------------

    def get_lineage(self, code: str, version: int | None = None) -> dict:
        v = self.get_version(code, version)
        edges = self.conn.execute(
            "SELECT * FROM lineage_edges WHERE version_id = ? ORDER BY upstream_kind, upstream_ref",
            (v["id"],),
        ).fetchall()
        return {
            "metric": code,
            "version": v["version"],
            "status": v["status"],
            "upstream": [dict(e) for e in edges],
        }

    def _rebuild_lineage(self, version_id: int, spec: MetricSpec) -> None:
        self.conn.execute("DELETE FROM lineage_edges WHERE version_id = ?", (version_id,))
        fields = {spec.measure_field, *spec.dimensions, *(f.field_name for f in spec.filters)}
        if spec.ratio_num_field:
            fields.update([spec.ratio_num_field, spec.ratio_den_field])
        for f in sorted(fields):
            self.conn.execute(
                "INSERT OR IGNORE INTO lineage_edges(version_id, upstream_kind, upstream_ref, field_name) "
                "VALUES (?, 'table', ?, ?)",
                (version_id, spec.source_table, f),
            )

    # -- diff ----------------------------------------------------------------

    def diff_versions(self, code: str, left: int, right: int) -> dict:
        a = self.get_version(code, left)
        b = self.get_version(code, right)
        keys = ("source_table", "measure_field", "agg_func", "dimensions", "filters",
                "time_grain", "additivity", "ratio_num_field", "ratio_den_field",
                "description")
        changes = []
        for k in keys:
            if a.get(k) != b.get(k):
                changes.append({"field": k, "from": a.get(k), "to": b.get(k)})
        return {
            "metric": code,
            "left_version": left,
            "right_version": right,
            "changes": changes,
            "identical": not changes,
        }

    # -- helpers -------------------------------------------------------------

    def _published_specs(self, exclude_code: str | None = None) -> list[tuple[str, MetricSpec]]:
        rows = self.conn.execute(
            """SELECT mv.*, m.code, st.name AS source_table_name
               FROM metric_versions mv
               JOIN metrics m ON m.id = mv.metric_id
               JOIN source_tables st ON st.id = mv.source_table_id
               WHERE mv.status = 'published'
                 AND mv.id = (SELECT id FROM metric_versions
                              WHERE metric_id = m.id AND status = 'published'
                              ORDER BY version DESC LIMIT 1)""",
        ).fetchall()
        specs = []
        for r in rows:
            if exclude_code and r["code"] == exclude_code:
                continue
            d = self._version_to_dict(r)
            d["source_table"] = r["source_table_name"]
            specs.append((r["code"], MetricSpec.from_dict(d)))
        return specs

    @staticmethod
    def _version_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["dimensions"] = storage.loads(d["dimensions"])
        d["filters"] = storage.loads(d["filters"])
        if "source_table_name" in d:
            d["source_table"] = d.pop("source_table_name")
        d.pop("source_table_id", None)
        d.pop("metric_id", None)
        return d

    def _audit(self, actor: str, action: str, entity: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO audit_log(ts, actor, action, entity, detail) VALUES (?, ?, ?, ?, ?)",
            (storage.utcnow(), actor, action, entity, detail),
        )

    def audit_trail(self, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
