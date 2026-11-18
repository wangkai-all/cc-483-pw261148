"""Registration, versioning, lineage, diff and rollback behaviour."""
import pytest

from app.domain import Filter, MetricSpec, SemanticError, Additivity
from app.registry import Registry


def reg(conn):
    return Registry(conn)


def test_metric_starts_with_published_v1(conn):
    r = reg(conn)
    versions = r.list_versions("revenue")
    assert versions[0]["version"] == 2  # v2 published by seed
    statuses = {v["version"]: v["status"] for v in versions}
    assert statuses[1] == "published"
    assert statuses[2] == "published"


def test_old_version_keeps_full_definition_snapshot(conn):
    r = reg(conn)
    v1 = r.get_version("revenue", 1)
    v2 = r.get_version("revenue", 2)
    # v2 added an exclusion filter; v1 must remain untouched and recomputable
    assert v1["filters"] == []
    assert v2["filters"] == [{"field": "region", "op": "!=", "value": "西南"}]
    assert v1["measure_field"] == v2["measure_field"] == "sales_amount"
    assert v1["agg_func"] == v2["agg_func"] == "SUM"


def test_only_one_draft_per_metric(conn):
    r = reg(conn)
    spec = MetricSpec(source_table="orders", measure_field="sales_amount",
                      agg_func="SUM", dimensions=["region"])
    r.create_draft("order_count", spec, "alice", "草稿1")
    with pytest.raises(SemanticError, match="已存在草稿"):
        r.create_draft("order_count", spec, "alice", "草稿2")


def test_invalid_field_rejected(conn):
    r = reg(conn)
    spec = MetricSpec(source_table="orders", measure_field="ghost_column",
                      agg_func="SUM", dimensions=[])
    with pytest.raises(SemanticError, match="不在来源表中"):
        r.create_draft("order_count", spec, "alice", "坏定义")


def test_non_additive_agg_cannot_be_declared_additive(conn):
    r = reg(conn)
    spec = MetricSpec(source_table="orders", measure_field="customer_id",
                      agg_func="COUNT_DISTINCT", dimensions=["region"],
                      additivity=Additivity.ADDITIVE.value)
    with pytest.raises(SemanticError, match="天然不可加"):
        r.create_draft("order_count", spec, "alice", "错误可加性")


def test_lineage_lists_source_fields(conn):
    r = reg(conn)
    lineage = r.get_lineage("revenue", 1)
    refs = {(e["upstream_kind"], e["upstream_ref"], e["field_name"])
            for e in lineage["upstream"]}
    assert ("table", "orders", "sales_amount") in refs
    assert ("table", "orders", "region") in refs


def test_diff_versions_reports_caliber_change(conn):
    r = reg(conn)
    d = r.diff_versions("revenue", 1, 2)
    changed = {c["field"] for c in d["changes"]}
    assert "filters" in changed
    assert "description" in changed


def test_rollback_creates_new_draft_and_keeps_history(conn):
    r = reg(conn)
    new_v = r.rollback("revenue", 1, "admin")
    assert new_v == 3
    draft = r.get_draft("revenue")
    assert draft["filters"] == []  # matches the historical v1 snapshot
    # history is immutable
    v2 = r.get_version("revenue", 2)
    assert v2["status"] == "published" and v2["filters"] != []
    # the rollback draft passes the gate and becomes v3
    published = r.publish("revenue", "admin")
    assert published["version"] == 3 and published["status"] == "published"


def test_cannot_query_or_rollback_to_draft(conn):
    r = reg(conn)
    r.create_draft("order_count",
                   MetricSpec(source_table="orders", measure_field="order_id",
                              agg_func="COUNT", dimensions=["region"]),
                   "alice", "草稿")
    with pytest.raises(SemanticError, match="只能回滚到已发布"):
        r.rollback("order_count", 2, "alice")


def test_audit_trail_records_lifecycle(conn):
    r = reg(conn)
    r.discard_draft("revenue_avg_conflict", "admin")
    actions = {a["action"] for a in r.audit_trail()}
    assert {"publish", "publish_blocked", "discard_draft"} <= actions
