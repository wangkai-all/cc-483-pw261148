"""Domain models and semantic validation rules.

The domain layer is deliberately independent of FastAPI so the same rules
are exercised by tests, the CLI seed script and the web service.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

AGG_FUNCTIONS = {"SUM", "COUNT", "COUNT_DISTINCT", "AVG", "MAX", "MIN", "RATIO"}
TIME_GRAINS = ("day", "week", "month", "quarter", "year")
FILTER_OPS = ("=", "!=", ">", ">=", "<", "<=", "IN", "NOT IN", "LIKE")

# Which aggregations may legitimately be summed again at a coarser grain.
NON_ADDITIVE_AGGS = {"COUNT_DISTINCT", "AVG", "RATIO"}
SEMI_ADDITIVE_AGGS: set[str] = set()  # MAX/MIN treated non-additive across time below


class SemanticError(ValueError):
    """Raised when a metric definition violates a semantic-layer rule."""


class ConflictError(SemanticError):
    """Raised when publishing would create a conflicting metric definition."""


class Additivity(str, Enum):
    ADDITIVE = "additive"
    SEMI = "semi_additive"
    NON = "non_additive"


@dataclass(frozen=True)
class Filter:
    field_name: str
    op: str
    value: Any

    def to_dict(self) -> dict:
        return {"field": self.field_name, "op": self.op, "value": self.value}

    @staticmethod
    def from_dict(d: dict) -> "Filter":
        return Filter(field_name=d["field"], op=d["op"], value=d["value"])

    def sql(self) -> tuple[str, tuple]:
        """Render as a parameterised SQL fragment."""
        if self.op in ("IN", "NOT IN"):
            if not isinstance(self.value, list) or not self.value:
                raise SemanticError(f"过滤条件 {self.field_name} {self.op} 需要非空列表")
            placeholders = ",".join("?" * len(self.value))
            return f'"{self.field_name}" {self.op} ({placeholders})', tuple(self.value)
        if self.op == "LIKE":
            return f'"{self.field_name}" LIKE ?', (self.value,)
        return f'"{self.field_name}" {self.op} ?', (self.value,)


@dataclass
class MetricSpec:
    """The full semantic definition captured in every metric version."""

    source_table: str
    measure_field: str
    agg_func: str
    dimensions: list[str] = field(default_factory=list)
    filters: list[Filter] = field(default_factory=list)
    time_grain: str = "day"
    additivity: str = Additivity.ADDITIVE.value
    ratio_num_field: str | None = None
    ratio_den_field: str | None = None
    description: str = ""

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["filters"] = [f.to_dict() for f in self.filters]
        return d

    @staticmethod
    def from_dict(d: dict) -> "MetricSpec":
        return MetricSpec(
            source_table=d["source_table"],
            measure_field=d["measure_field"],
            agg_func=d["agg_func"],
            dimensions=list(d.get("dimensions", [])),
            filters=[Filter.from_dict(f) for f in d.get("filters", [])],
            time_grain=d.get("time_grain", "day"),
            additivity=d.get("additivity", Additivity.ADDITIVE.value),
            ratio_num_field=d.get("ratio_num_field"),
            ratio_den_field=d.get("ratio_den_field"),
            description=d.get("description", ""),
        )

    def signature(self) -> tuple:
        """Structural signature used by conflict detection.

        Two metrics with the same signature implement the *same* business
        caliber; a different agg over the same scope is a conflict instead.
        """
        return (
            self.source_table,
            self.measure_field,
            self.agg_func,
            tuple(sorted(self.dimensions)),
            tuple(sorted(f.to_dict().items() for f in self.filters)),
            self.ratio_num_field,
            self.ratio_den_field,
        )

    def scope(self) -> tuple:
        """The data scope (ignoring aggregation): table, dimensions, filters."""
        return (
            self.source_table,
            tuple(sorted(self.dimensions)),
            tuple(sorted(f.to_dict().items() for f in self.filters)),
        )


def validate_spec(spec: MetricSpec, source_columns: set[str]) -> None:
    """Structural validation against the registered source schema."""
    if spec.agg_func not in AGG_FUNCTIONS:
        raise SemanticError(f"不支持的聚合函数: {spec.agg_func}")
    if spec.time_grain not in TIME_GRAINS:
        raise SemanticError(f"不支持的时间粒度: {spec.time_grain}")
    if spec.additivity not in {a.value for a in Additivity}:
        raise SemanticError(f"非法可加性: {spec.additivity}")

    for col in spec.dimensions:
        if col not in source_columns:
            raise SemanticError(f"维度字段 {col} 不在来源表中")
    for f in spec.filters:
        if f.op not in FILTER_OPS:
            raise SemanticError(f"不支持的过滤运算符: {f.op}")
        if f.field_name not in source_columns:
            raise SemanticError(f"过滤字段 {f.field_name} 不在来源表中")

    if spec.agg_func == "RATIO":
        if not spec.ratio_num_field or not spec.ratio_den_field:
            raise SemanticError("RATIO 指标必须提供分子与分母字段")
        for c in (spec.ratio_num_field, spec.ratio_den_field):
            if c not in source_columns:
                raise SemanticError(f"比率字段 {c} 不在来源表中")
    else:
        if spec.measure_field not in source_columns:
            raise SemanticError(f"度量字段 {spec.measure_field} 不在来源表中")

    # Rule: declared additivity must be consistent with the aggregation.
    implied = implied_additivity(spec.agg_func)
    if implied == Additivity.NON and spec.additivity == Additivity.ADDITIVE:
        raise SemanticError(
            f"{spec.agg_func} 天然不可加，不能声明为 additive（防止跨粒度重复聚合）"
        )


def implied_additivity(agg_func: str) -> Additivity:
    if agg_func in NON_ADDITIVE_AGGS or agg_func in {"MAX", "MIN"}:
        return Additivity.NON
    return Additivity.ADDITIVE


# -- conflict detection ------------------------------------------------------

def detect_conflict(candidate: MetricSpec, others: list[MetricSpec]) -> str | None:
    """Return a human-readable conflict reason, or None.

    Blocks when another *published* metric:
      1. has the identical signature (duplicate caliber), or
      2. aggregates the same measure with a different function over the same
         table/dimensions and overlapping filter scopes — two numbers for one
         business question, a genuine 口径冲突. A narrower filtered metric
         nested inside an unfiltered one still overlaps, so it is blocked;
         only provably disjoint scopes are allowed.
    """
    for other in others:
        if other.signature() == candidate.signature():
            return (
                f"与已有指标口径完全重复（同源 {other.source_table} / "
                f"{other.agg_func}({other.measure_field}) / 同维度同过滤）"
            )
        same_scope = (
            other.source_table == candidate.source_table
            and sorted(other.dimensions) == sorted(candidate.dimensions)
            and _filters_overlap(other.filters, candidate.filters)
        )
        # RATIO metrics are defined by their numerator/denominator pair, not by
        # a scalar aggregation over the measure field, so they never clash with
        # scalar aggregations (an identical RATIO is caught by the signature).
        scalar_pair = "RATIO" not in (other.agg_func, candidate.agg_func)
        if (
            scalar_pair
            and same_scope
            and other.measure_field == candidate.measure_field
            and other.agg_func != candidate.agg_func
        ):
            return (
                f"口径冲突：同一数据范围与度量 {candidate.measure_field} 上，"
                f"已有 {other.agg_func} 聚合，不能再发布 {candidate.agg_func} 聚合"
            )
    return None


def _predicate_sets(filters: list[Filter]) -> dict[str, set]:
    out: dict[str, set] = {}
    for f in filters:
        if f.op in ("=", "IN"):
            vals = set(f.value) if isinstance(f.value, list) else {f.value}
            out.setdefault(f.field_name, set()).update(vals)
    return out


def _filters_overlap(a: list[Filter], b: list[Filter]) -> bool:
    """Conservative overlap test for two filter lists.

    Empty filters mean full-table scope, which overlaps everything. The only
    provably disjoint case we recognise is equality/IN predicates on the same
    field with disjoint value sets; anything else is treated as overlapping
    (the safe direction — block rather than silently allow a conflict).
    """
    if not a or not b:
        return True
    pa, pb = _predicate_sets(a), _predicate_sets(b)
    for field_name, vals_a in pa.items():
        if field_name in pb and vals_a.isdisjoint(pb[field_name]):
            return False
    return True
