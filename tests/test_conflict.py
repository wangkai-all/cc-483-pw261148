"""Conflict gate: same-scope different aggregation blocks, disjoint filters pass."""
import pytest

from app.domain import Additivity, Filter, MetricSpec
from app.registry import Registry
from app.domain import detect_conflict


def _spec(agg="SUM", filters=None, measure="sales_amount"):
    return MetricSpec(
        source_table="orders", measure_field=measure, agg_func=agg,
        dimensions=["region", "category", "channel"],
        filters=filters or [], additivity=Additivity.NON.value if agg != "SUM"
        else Additivity.ADDITIVE.value,
    )


def test_identical_signature_is_duplicate_conflict():
    reason = detect_conflict(_spec(), [_spec()])
    assert reason and "重复" in reason


def test_same_scope_different_agg_is_caliber_conflict():
    reason = detect_conflict(_spec("AVG"), [_spec("SUM")])
    assert reason and "口径冲突" in reason


def test_disjoint_equality_filters_are_not_conflicts():
    online = _spec(filters=[Filter("channel", "=", "线上")])
    offline = _spec(filters=[Filter("channel", "=", "门店")])
    assert detect_conflict(offline, [online]) is None


def test_empty_filter_overlaps_everything():
    from app.domain import _filters_overlap
    full = []
    scoped = [Filter("channel", "=", "线上")]
    assert _filters_overlap(full, scoped) is True
    assert _filters_overlap(scoped, full) is True
    # different aggregation over nested scope must still be blocked
    nested = _spec("AVG", filters=[Filter("channel", "=", "线上")])
    assert detect_conflict(nested, [_spec("SUM")]) is not None


def test_different_measure_no_conflict():
    other = MetricSpec(source_table="orders", measure_field="line_cnt",
                       agg_func="SUM", dimensions=["region", "category", "channel"])
    assert detect_conflict(other, [_spec()]) is None


def test_publish_blocked_by_gate_and_audit_logged(conn):
    r = Registry(conn)
    # seed left revenue_avg_conflict in draft state
    with pytest.raises(Exception, match="发布被阻断"):
        r.publish("revenue_avg_conflict", "bob")
    blocked = [a for a in r.audit_trail() if a["action"] == "publish_blocked"]
    assert blocked and "AVG" in blocked[0]["detail"]
    # draft stays unpublished and can still be discarded
    assert r.get_draft("revenue_avg_conflict") is not None


def test_disjoint_filter_metric_publishes_fine(conn):
    r = Registry(conn)
    versions = r.list_versions("online_revenue")
    assert versions[0]["status"] == "published"
