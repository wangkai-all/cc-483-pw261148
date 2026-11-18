"""RBAC: restricted columns are blocked for users without grants."""
import pytest

from app.engine import PermissionError_, QueryEngine, QueryRequest
from app.registry import Registry


def test_unprivileged_user_cannot_query_restricted_measure(conn):
    e = QueryEngine(conn, Registry(conn))
    with pytest.raises(PermissionError_, match="无权访问受限字段"):
        e.run(QueryRequest(metric_code="total_cost", time_grain="month",
                           username="bob"))


def test_analyst_blocked_admin_allowed(conn):
    e = QueryEngine(conn, Registry(conn))
    with pytest.raises(PermissionError_):
        e.run(QueryRequest(metric_code="total_cost", time_grain="month",
                           username="alice"))
    r = e.run(QueryRequest(metric_code="total_cost", time_grain="month",
                           username="admin"))
    assert r["rows"] and r["cache"] == "MISS"


def test_restricted_filter_field_also_guarded(conn):
    from app.domain import Filter
    e = QueryEngine(conn, Registry(conn))
    with pytest.raises(PermissionError_):
        e.run(QueryRequest(metric_code="revenue", time_grain="month",
                           username="bob",
                           filters=[Filter("cost_amount", ">", 100)]))


def test_unknown_user_rejected(conn):
    e = QueryEngine(conn, Registry(conn))
    with pytest.raises(PermissionError_, match="不存在"):
        e.run(QueryRequest(metric_code="revenue", time_grain="month",
                           username="ghost"))


def test_grant_allows_access(conn):
    conn.execute(
        "INSERT INTO column_grants(username, resource) VALUES ('alice', 'orders.cost_amount')"
    )
    conn.commit()
    r = QueryEngine(conn, Registry(conn)).run(
        QueryRequest(metric_code="total_cost", time_grain="month", username="alice"))
    assert r["rows"]
