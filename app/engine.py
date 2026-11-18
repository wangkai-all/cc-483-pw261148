"""Semantic query engine.

Single execution path for page queries and exports: every request is compiled
from the metric's *versioned definition* straight into SQL against the raw
source table. Pre-aggregated results are never fed back into an aggregation,
which is how the layer guarantees no double aggregation across time grains.
"""
from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from . import storage
from .domain import Filter, MetricSpec, SemanticError, Additivity


class PermissionError_(Exception):
    """Raised when the caller lacks rights on a restricted column."""


# SQLite period-start expressions keyed by supported grains.
_PERIOD_SQL = {
    "day": 'date("{ts}")',
    # 'weekday 1' advances to the next Monday (same day if already Monday);
    # subtract 7 days to land on the Monday of the containing ISO week.
    "week": "date(\"{ts}\", 'weekday 1', '-7 days')",
    "month": "strftime('%Y-%m-01', \"{ts}\")",
    "quarter": (
        "date(\"{ts}\", '-' || ((CAST(strftime('%m', \"{ts}\") AS INT) - 1) % 3) "
        "|| ' months', 'start of month')"
    ),
    "year": "strftime('%Y-01-01', \"{ts}\")",
}

_GRAIN_ORDER = ["day", "week", "month", "quarter", "year"]


@dataclass
class QueryRequest:
    metric_code: str
    version: int | None = None              # default: latest published
    time_grain: str = "day"
    dimensions: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    date_from: str | None = None            # inclusive YYYY-MM-DD
    date_to: str | None = None              # inclusive YYYY-MM-DD
    limit: int = 10000
    username: str = "anonymous"

    def cache_key(self, version: int, grant_sig: str) -> str:
        raw = storage.dumps({
            "user": self.username,
            "metric": self.metric_code,
            "version": version,
            "grain": self.time_grain,
            "dims": sorted(self.dimensions),
            "filters": sorted(sorted(f.to_dict().items()) for f in self.filters),
            "from": self.date_from,
            "to": self.date_to,
            "limit": self.limit,
            "grants": grant_sig,
        })
        return hashlib.sha256(raw.encode()).hexdigest()


class QueryEngine:
    def __init__(self, conn: sqlite3.Connection, registry):
        self.conn = conn
        self.registry = registry

    # -- public API ----------------------------------------------------------

    def run(self, req: QueryRequest, use_cache: bool = True) -> dict:
        version_row = self.registry.get_version(req.metric_code, req.version)
        if version_row["status"] != "published":
            raise SemanticError(
                f"只能查询已发布版本（{req.metric_code}@v{version_row['version']} "
                f"当前状态 {version_row['status']}）"
            )
        spec = MetricSpec.from_dict(version_row)
        source = self.registry.get_source(spec.source_table)
        source_cols = {c["name"]: c for c in storage.loads(source["columns"])}

        self._check_grain(req.time_grain)
        self._check_dimensions(req, spec)
        self._check_permissions(req, spec, source_cols)
        grant_sig = self._grant_signature(req.username)

        key = req.cache_key(version_row["version"], grant_sig)
        if use_cache:
            hit = self.conn.execute(
                "SELECT payload FROM query_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            if hit:
                self.conn.execute(
                    "UPDATE query_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
                    (key,),
                )
                self.conn.commit()
                result = storage.loads(hit["payload"])
                result["cache"] = "HIT"
                return result

        sql, params = self.compile_sql(req, spec, source_cols)
        cur = self.conn.execute(sql, params)
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if rows else \
            self._expected_columns(req, spec)
        data = [dict(r) for r in rows]

        result = {
            "metric": req.metric_code,
            "version": version_row["version"],
            "time_grain": req.time_grain,
            "additivity": spec.additivity,
            "columns": columns,
            "rows": data,
            "row_count": len(data),
            "sql": sql,
            "cache": "MISS",
            "definition_snapshot": spec.to_dict(),
        }
        self.conn.execute(
            "INSERT OR REPLACE INTO query_cache(cache_key, metric_code, version, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, req.metric_code, version_row["version"],
             storage.dumps({**result, "cache": "MISS"}), storage.utcnow()),
        )
        self.conn.commit()
        return result

    def export(self, req: QueryRequest, fmt: str = "csv") -> tuple[str, str, bytes]:
        """Same execution path as page queries, serialised for download."""
        result = self.run(req)
        if fmt == "json":
            body = storage.dumps(result).encode()
            return "application/json", f"{req.metric_code}.json", body
        if fmt != "csv":
            raise SemanticError(f"不支持的导出格式: {fmt}")
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=result["columns"])
        writer.writeheader()
        writer.writerows(result["rows"])
        return "text/csv; charset=utf-8", f"{req.metric_code}.csv", buf.getvalue().encode()

    def invalidate(self, metric_code: str | None = None) -> int:
        if metric_code:
            cur = self.conn.execute(
                "DELETE FROM query_cache WHERE metric_code = ?", (metric_code,)
            )
        else:
            cur = self.conn.execute("DELETE FROM query_cache")
        self.conn.commit()
        return cur.rowcount

    # -- SQL generation ------------------------------------------------------

    def compile_sql(self, req: QueryRequest, spec: MetricSpec,
                    source_cols: dict[str, dict]) -> tuple[str, list]:
        ts_col = self._event_time_column(source_cols)
        period_expr = _PERIOD_SQL[req.time_grain].format(ts=ts_col)

        select_cols = [f"{period_expr} AS period"]
        group_cols = [period_expr]
        for dim in req.dimensions:
            select_cols.append(f'"{dim}"')
            group_cols.append(f'"{dim}"')
        select_cols.append(self._agg_expr(spec) + ' AS metric_value')

        where, params = self._where_clause(req, spec, ts_col)
        order_dims = ", ".join(f'"{d}"' for d in req.dimensions)
        sql = (
            f'SELECT {", ".join(select_cols)}\n'
            f'FROM "{spec.source_table}"\n'
            f'WHERE {" AND ".join(where)}\n'
            f'GROUP BY {", ".join(group_cols)}\n'
            f'ORDER BY period ASC'
            + (f", {order_dims}" if order_dims else "")
            + f'\nLIMIT {int(req.limit)}'
        )
        return sql, params

    def _agg_expr(self, spec: MetricSpec) -> str:
        agg = spec.agg_func
        if agg == "RATIO":
            return (f'SUM("{spec.ratio_num_field}") * 1.0 / '
                    f'NULLIF(SUM("{spec.ratio_den_field}"), 0)')
        if agg == "COUNT":
            return f'COUNT("{spec.measure_field}")'
        if agg == "COUNT_DISTINCT":
            return f'COUNT(DISTINCT "{spec.measure_field}")'
        return f'{agg}("{spec.measure_field}")'

    def _where_clause(self, req: QueryRequest, spec: MetricSpec,
                      ts_col: str) -> tuple[list[str], list]:
        clauses, params = [], []
        for f in (*spec.filters, *req.filters):
            fragment, values = f.sql()
            clauses.append(fragment)
            params.extend(values)
        if req.date_from:
            clauses.append(f'date("{ts_col}") >= ?')
            params.append(req.date_from)
        if req.date_to:
            clauses.append(f'date("{ts_col}") <= ?')
            params.append(req.date_to)
        if not clauses:
            clauses.append("1=1")
        return clauses, params

    # -- guards --------------------------------------------------------------

    def _check_grain(self, grain: str) -> None:
        if grain not in _GRAIN_ORDER:
            raise SemanticError(f"非法时间粒度: {grain}")

    def _check_dimensions(self, req: QueryRequest, spec: MetricSpec) -> None:
        unknown = set(req.dimensions) - set(spec.dimensions)
        if unknown:
            raise SemanticError(
                f"维度 {sorted(unknown)} 未在指标 {req.metric_code} 定义中登记"
            )

    def _check_permissions(self, req: QueryRequest, spec: MetricSpec,
                           source_cols: dict[str, dict]) -> None:
        user = self.conn.execute(
            "SELECT role FROM users WHERE username = ?", (req.username,)
        ).fetchone()
        if user is None:
            raise PermissionError_(f"用户 {req.username} 不存在")
        restricted = {name for name, c in source_cols.items() if c.get("restricted")}
        if not restricted:
            return
        grants = {
            r["resource"] for r in self.conn.execute(
                "SELECT resource FROM column_grants WHERE username = ?", (req.username,)
            ).fetchall()
        }
        touched = set(req.dimensions) | {f.field_name for f in req.filters}
        if spec.agg_func == "RATIO":
            touched.update([spec.ratio_num_field, spec.ratio_den_field])
        else:
            touched.add(spec.measure_field)
        touched.update(f.field_name for f in spec.filters)
        blocked = sorted(
            f"{spec.source_table}.{c}" for c in touched
            if c in restricted and f"{spec.source_table}.{c}" not in grants
        )
        if blocked:
            raise PermissionError_(f"用户 {req.username} 无权访问受限字段: {blocked}")

    def _grant_signature(self, username: str) -> str:
        rows = self.conn.execute(
            "SELECT resource FROM column_grants WHERE username = ? ORDER BY resource",
            (username,),
        ).fetchall()
        return ",".join(r["resource"] for r in rows)

    @staticmethod
    def _event_time_column(source_cols: dict[str, dict]) -> str:
        for name, col in source_cols.items():
            if col.get("semantic") == "event_time":
                return name
        raise SemanticError("来源表未登记 event_time 语义字段，无法按时间聚合")

    @staticmethod
    def _expected_columns(req: QueryRequest, spec: MetricSpec) -> list[str]:
        return ["period", *req.dimensions, "metric_value"]

    # -- anti-double-aggregation guard ---------------------------------------

    @staticmethod
    def assert_no_reaggregation(spec: MetricSpec, source_is_preaggregated: bool,
                                target_grain: str) -> None:
        """Explicit rule used by the service and tests.

        The engine only ever scans raw source tables. If a caller ever supplies
        a pre-aggregated input (e.g. cached daily rows) and asks for a coarser
        grain, non-additive metrics must be recomputed from raw data instead.
        """
        if not source_is_preaggregated:
            return
        if spec.additivity != Additivity.ADDITIVE.value:
            raise SemanticError(
                f"{spec.agg_func} 指标为 {spec.additivity}，"
                "禁止对细粒度预聚合结果再次聚合；必须从原始明细重算"
            )
