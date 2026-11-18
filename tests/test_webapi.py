"""End-to-end HTTP behaviour through the FastAPI app."""


def test_health_and_metric_listing(client):
    assert client.get("/api/health").json()["status"] == "ok"
    codes = {m["code"] for m in client.get("/api/metrics").json()}
    assert {"revenue", "active_buyers", "avg_order_value", "total_cost"} <= codes


def test_query_endpoint_and_export(client):
    body = {"time_grain": "month", "date_from": "2026-01-01", "date_to": "2026-03-31"}
    r1 = client.post("/api/query/revenue", json=body)
    assert r1.status_code == 200 and r1.json()["cache"] == "MISS"
    r2 = client.post("/api/query/revenue", json=body)
    assert r2.json()["cache"] == "HIT"

    csv_res = client.post("/api/export/revenue?fmt=csv", json=body)
    assert csv_res.status_code == 200
    assert "attachment" in csv_res.headers["content-disposition"]
    assert csv_res.content.split(b"\n", 1)[0].startswith(b"period")


def test_conflict_returns_409(client):
    res = client.post("/api/metrics/revenue_avg_conflict/publish")
    assert res.status_code == 409
    assert "口径冲突" in res.json()["detail"]


def test_permission_denied_403(client):
    client.headers["X-User"] = "bob"
    res = client.post("/api/query/total_cost", json={"time_grain": "month"})
    assert res.status_code == 403
    assert "受限字段" in res.json()["detail"]


def test_register_new_metric_and_lineage(client):
    payload = {
        "code": "gross_margin", "name": "毛利额", "owner": "alice",
        "change_note": "新增",
        "spec": {
            "source_table": "orders", "measure_field": "sales_amount",
            "agg_func": "SUM", "dimensions": ["region"],
            "filters": [{"field": "channel", "op": "=", "value": "线上"}],
            "time_grain": "month", "additivity": "additive",
        },
    }
    r = client.post("/api/metrics", json=payload)
    assert r.status_code == 200 and r.json()["status"] == "published"

    lineage = client.get("/api/metrics/gross_margin/lineage").json()
    fields = {e["field_name"] for e in lineage["upstream"]}
    assert {"sales_amount", "region", "channel"} <= fields


def test_duplicate_caliber_registration_blocked(client):
    # mirror the current published revenue v2 definition exactly
    payload = {
        "code": "revenue_copy", "name": "收入复制", "owner": "bob",
        "spec": {
            "source_table": "orders", "measure_field": "sales_amount",
            "agg_func": "SUM",
            "dimensions": ["region", "category", "channel"],
            "filters": [{"field": "region", "op": "!=", "value": "西南"}],
            "time_grain": "day", "additivity": "additive",
        },
    }
    res = client.post("/api/metrics", json=payload)
    assert res.status_code == 409
    assert "重复" in res.json()["detail"]


def test_rollback_then_publish_flow(client):
    rb = client.post("/api/metrics/revenue/rollback?version=1")
    assert rb.status_code == 200
    new_version = rb.json()["draft_version"]
    pub = client.post("/api/metrics/revenue/publish")
    assert pub.status_code == 200 and pub.json()["version"] == new_version


def test_diff_endpoint(client):
    d = client.get("/api/metrics/revenue/diff?left=1&right=2").json()
    assert not d["identical"]
    assert {c["field"] for c in d["changes"]} >= {"filters", "description"}


def test_cache_invalidation_endpoint(client):
    client.post("/api/query/revenue", json={"time_grain": "month"})
    n = client.post("/api/cache/invalidate").json()["invalidated"]
    assert n >= 1
    assert client.get("/api/cache").json() == []


def test_static_index_served(client):
    res = client.get("/")
    assert res.status_code == 200 and "指标语义层" in res.text


def test_audit_endpoint_records_block(client):
    client.post("/api/metrics/revenue_avg_conflict/publish")
    actions = [a["action"] for a in client.get("/api/audit").json()]
    assert "publish_blocked" in actions
