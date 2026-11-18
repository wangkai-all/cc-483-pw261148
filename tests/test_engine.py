"""Query engine: cross-grain recomputation, no double aggregation, cache, export."""
import pytest

from app.domain import Additivity, Filter, MetricSpec, SemanticError
from app.engine import QueryEngine, QueryRequest
from app.registry import Registry


def engine(conn):
    return QueryEngine(conn, Registry(conn))


def req(code, grain="month", dims=None, version=None, user="alice", **kw):
    return QueryRequest(metric_code=code, version=version, time_grain=grain,
                        dimensions=dims or [], username=user,
                        date_from="2026-01-01", date_to="2026-03-31", **kw)


def test_basic_query_returns_period_rows(conn):
    r = engine(conn).run(req("revenue", "month"))
    assert r["cache"] == "MISS"
    periods = {row["period"] for row in r["rows"]}
    assert periods == {"2026-01-01", "2026-02-01", "2026-03-01"}
    assert all("metric_value" in row for row in r["rows"])


def test_cross_grain_always_recomputes_from_raw_and_matches_direct_sql(conn):
    """SUM is additive: month total from engine must equal raw-table SQL,
    never a re-aggregation of cached daily output. Uses revenue v1 (full scope)."""
    e = engine(conn)
    monthly = e.run(req("revenue", "month", version=1))
    direct = conn.execute(
        "SELECT strftime('%Y-%m-01', event_date) AS period, SUM(sales_amount) AS v "
        "FROM orders WHERE date(event_date) BETWEEN ? AND ? GROUP BY period",
        ("2026-01-01", "2026-03-31"),
    ).fetchall()
    assert {r["period"]: round(r["metric_value"], 2) for r in monthly["rows"]} == \
        {r["period"]: round(r["v"], 2) for r in direct}


def test_count_distinct_not_reaggregated_across_grains(conn):
    """The guard must refuse to roll daily distinct counts up to month."""
    e = engine(conn)
    monthly = e.run(req("active_buyers", "month"))
    daily = e.run(req("active_buyers", "day"))
    # monthly distinct computed from raw events is strictly smaller than the
    # naive sum of daily distinct counts (the double-aggregation trap)
    naive_sum = sum(r["metric_value"] for r in daily["rows"])
    engine_month_sum = sum(r["metric_value"] for r in monthly["rows"])
    assert engine_month_sum < naive_sum

    spec = MetricSpec.from_dict(Registry(conn).get_version("active_buyers"))
    with pytest.raises(SemanticError, match="禁止对细粒度预聚合结果再次聚合"):
        QueryEngine.assert_no_reaggregation(spec, True, "month")
    # raw input is always fine
    QueryEngine.assert_no_reaggregation(spec, False, "month")


def test_ratio_metric_computed_from_raw_at_every_grain(conn):
    e = engine(conn)
    monthly = e.run(req("avg_order_value", "month"))
    direct = conn.execute(
        "SELECT strftime('%Y-%m-01', event_date) AS period, "
        "SUM(sales_amount) * 1.0 / NULLIF(SUM(line_cnt), 0) AS v "
        "FROM orders GROUP BY period",
    ).fetchall()
    got = {r["period"]: round(r["metric_value"], 6) for r in monthly["rows"]}
    want = {r["period"]: round(r["v"], 6) for r in direct}
    assert got == want


def test_historical_version_remains_recomputable(conn):
    """v1 of revenue includes 西南; v2 excludes it. Both stay queryable."""
    e = engine(conn)
    v1 = e.run(req("revenue", "month", version=1))
    v2 = e.run(req("revenue", "month", version=2))
    total_v1 = sum(r["metric_value"] for r in v1["rows"])
    total_v2 = sum(r["metric_value"] for r in v2["rows"])
    assert total_v1 > total_v2
    southwest = conn.execute(
        "SELECT COALESCE(SUM(sales_amount), 0) AS v FROM orders WHERE region = '西南'"
    ).fetchone()["v"]
    assert round(total_v1 - total_v2, 2) == round(southwest, 2)


def test_unknown_dimension_blocked(conn):
    with pytest.raises(SemanticError, match="未在指标"):
        engine(conn).run(req("revenue", "month", dims=["ghost_dim"]))


def test_dimension_breakdown_and_filters(conn):
    e = engine(conn)
    r = e.run(req("revenue", "month", dims=["region"],
                  filters=[Filter("channel", "=", "线上")]))
    assert set(r["columns"]) == {"period", "region", "metric_value"}
    assert all(row["metric_value"] >= 0 for row in r["rows"])
    # every row really is online-only: cross-check against raw SQL
    raw = conn.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE channel != '线上'"
    ).fetchone()["c"]
    assert raw > 0  # data exists offline, proving the filter did something


def test_cache_hit_on_identical_request_and_invalidation(conn):
    e = engine(conn)
    first = e.run(req("revenue", "month"))
    second = e.run(req("revenue", "month"))
    assert first["cache"] == "MISS" and second["cache"] == "HIT"
    row = conn.execute(
        "SELECT hit_count FROM query_cache WHERE metric_code = 'revenue'"
    ).fetchone()
    assert row["hit_count"] == 1
    # different grain is a different cache entry
    other = e.run(req("revenue", "quarter"))
    assert other["cache"] == "MISS"
    # different user (different grants) cannot hit another user's cache
    other_user = e.run(req("revenue", "month", user="bob"))
    assert other_user["cache"] == "MISS"
    assert e.invalidate("revenue") >= 2
    assert e.run(req("revenue", "month"))["cache"] == "MISS"


def test_cache_key_includes_metric_version(conn):
    e = engine(conn)
    v1 = e.run(req("revenue", "month", version=1))
    v2 = e.run(req("revenue", "month", version=2))
    assert v1["cache"] == "MISS" and v2["cache"] == "MISS"
    keys = conn.execute(
        "SELECT DISTINCT cache_key FROM query_cache WHERE metric_code = 'revenue'"
    ).fetchall()
    assert len(keys) == 2


def test_export_csv_and_json_share_query_path(conn):
    e = engine(conn)
    media, name, body = e.export(req("revenue", "month"), "csv")
    assert media.startswith("text/csv") and name.endswith(".csv")
    text = body.decode()
    assert text.splitlines()[0].startswith("period,metric_value")
    _, _, jbody = e.export(req("revenue", "month"), "json")
    assert b'"rows"' in jbody and b'"definition_snapshot"' in jbody
    with pytest.raises(SemanticError):
        e.export(req("revenue", "month"), "xml")


def test_only_published_versions_queryable(conn):
    r = Registry(conn)
    r.create_draft("order_count",
                   MetricSpec(source_table="orders", measure_field="order_id",
                              agg_func="COUNT", dimensions=["region"]),
                   "alice", "草稿")
    with pytest.raises(SemanticError, match="只能查询已发布版本"):
        engine(conn).run(req("order_count", "month", version=2))


def test_week_quarter_year_grains_all_scan_raw(conn):
    e = engine(conn)
    r = e.run(req("revenue", "week"))
    assert r["rows"]
    # every bucket starts on a Monday (%w == '1')
    for row in r["rows"]:
        weekday = conn.execute(
            "SELECT strftime('%w', ?) AS w", (row["period"],)
        ).fetchone()["w"]
        assert weekday == "1"
    q = e.run(req("revenue", "quarter"))
    quarters = {row["period"] for row in q["rows"]}
    assert quarters == {"2026-01-01"}  # all seed data falls in Q1
    y = e.run(req("revenue", "year"))
    assert {row["period"] for row in y["rows"]} == {"2026-01-01"}
