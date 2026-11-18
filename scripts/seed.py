"""Seed a demo database: raw order events, sources, metrics, users, grants.

Run:  .venv/bin/python -m scripts.seed
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from app import storage
from app.domain import Additivity, Filter, MetricSpec
from app.registry import Registry

DB = Path(__file__).resolve().parent.parent / "data" / "metrics.db"

REGIONS = ["华东", "华南", "华北", "西南"]
CATEGORIES = ["电子产品", "家居", "服装", "食品"]
CHANNELS = ["线上", "门店"]

ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    event_date   TEXT NOT NULL,
    region       TEXT NOT NULL,
    category     TEXT NOT NULL,
    channel      TEXT NOT NULL,
    customer_id  TEXT NOT NULL,
    sales_amount REAL NOT NULL,
    cost_amount  REAL NOT NULL,
    line_cnt     INTEGER NOT NULL DEFAULT 1
)
"""


def seed_raw_orders(conn: sqlite3.Connection) -> int:
    conn.execute("DROP TABLE IF EXISTS orders")
    conn.execute(ORDERS_DDL)
    rng = random.Random(42)
    start = date(2026, 1, 1)
    rows = []
    oid = 0
    for day_offset in range(90):  # 2026-01-01 .. 2026-03-31
        the_day = start + timedelta(days=day_offset)
        for _ in range(rng.randint(6, 12)):
            oid += 1
            sales = round(rng.uniform(80, 4000), 2)
            cost = round(sales * rng.uniform(0.45, 0.85), 2)
            rows.append((
                f"ORD{oid:06d}", the_day.isoformat(),
                rng.choice(REGIONS), rng.choice(CATEGORIES), rng.choice(CHANNELS),
                f"CUST{rng.randint(1, 120):04d}", sales, cost, 1,
            ))
    conn.executemany(
        "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    conn.commit()
    return len(rows)


def seed(conn: sqlite3.Connection) -> dict:
    reg = Registry(conn)
    n_rows = seed_raw_orders(conn)

    columns = [
        {"name": "order_id", "type": "TEXT"},
        {"name": "event_date", "type": "TEXT", "semantic": "event_time"},
        {"name": "region", "type": "TEXT"},
        {"name": "category", "type": "TEXT"},
        {"name": "channel", "type": "TEXT"},
        {"name": "customer_id", "type": "TEXT"},
        {"name": "sales_amount", "type": "REAL"},
        {"name": "cost_amount", "type": "REAL", "restricted": True},
        {"name": "line_cnt", "type": "INTEGER"},
    ]
    reg.register_source("orders", columns, "订单明细事件表（每行一个订单行）")

    # users & column-level grants
    conn.execute("DELETE FROM users")
    conn.execute("DELETE FROM column_grants")
    for username, role in (("admin", "admin"), ("alice", "analyst"), ("bob", "viewer")):
        conn.execute("INSERT INTO users(username, role, created_at) VALUES (?, ?, ?)",
                     (username, role, storage.utcnow()))
    conn.execute(
        "INSERT INTO column_grants(username, resource) VALUES ('admin', 'orders.cost_amount')"
    )
    conn.commit()

    dims = ["region", "category", "channel"]

    # 1) revenue — additive SUM
    reg.register_metric("revenue", "销售收入", "alice")
    reg.create_draft("revenue", MetricSpec(
        source_table="orders", measure_field="sales_amount", agg_func="SUM",
        dimensions=dims, additivity=Additivity.ADDITIVE.value,
        description="按订单销售金额汇总的收入口径",
    ), "alice", "首次登记收入口径")
    reg.publish("revenue", "alice")

    # 2) order_count — additive COUNT
    reg.register_metric("order_count", "订单量", "alice")
    reg.create_draft("order_count", MetricSpec(
        source_table="orders", measure_field="order_id", agg_func="COUNT",
        dimensions=dims, additivity=Additivity.ADDITIVE.value,
        description="订单行数计数",
    ), "alice", "首次登记")
    reg.publish("order_count", "alice")

    # 3) active_buyers — COUNT_DISTINCT, non-additive
    reg.register_metric("active_buyers", "活跃买家数", "alice")
    reg.create_draft("active_buyers", MetricSpec(
        source_table="orders", measure_field="customer_id",
        agg_func="COUNT_DISTINCT", dimensions=dims,
        additivity=Additivity.NON.value,
        description="去重客户数；跨粒度必须从明细重算，不能对日结果再聚合",
    ), "alice", "首次登记不可加指标")
    reg.publish("active_buyers", "alice")

    # 4) avg_order_value — RATIO (non-additive)
    reg.register_metric("avg_order_value", "客单价", "alice")
    reg.create_draft("avg_order_value", MetricSpec(
        source_table="orders", measure_field="sales_amount", agg_func="RATIO",
        ratio_num_field="sales_amount", ratio_den_field="line_cnt",
        dimensions=dims, additivity=Additivity.NON.value,
        description="收入/订单行数，比率型指标，禁止对日值取平均",
    ), "alice", "首次登记比率指标")
    reg.publish("avg_order_value", "alice")

    # 5) total_cost — restricted measure, admin-only
    reg.register_metric("total_cost", "总成本", "admin")
    reg.create_draft("total_cost", MetricSpec(
        source_table="orders", measure_field="cost_amount", agg_func="SUM",
        dimensions=["region", "category"], additivity=Additivity.ADDITIVE.value,
        description="成本字段为受限列，仅授权用户可查询",
    ), "admin", "登记受限指标")
    reg.publish("total_cost", "admin")

    # 6) online_revenue — disjoint filter vs revenue, allowed to publish
    reg.register_metric("online_revenue", "线上销售收入", "alice")
    reg.create_draft("online_revenue", MetricSpec(
        source_table="orders", measure_field="sales_amount", agg_func="SUM",
        dimensions=dims, filters=[Filter("channel", "=", "线上")],
        additivity=Additivity.ADDITIVE.value,
        description="仅线上渠道；与全渠道收入过滤域不相交",
    ), "alice", "分渠道口径")
    reg.publish("online_revenue", "alice")

    # 7) revenue_v2 — changed caliber: now excludes 西南 region (new draft → publish)
    reg.create_draft("revenue", MetricSpec(
        source_table="orders", measure_field="sales_amount", agg_func="SUM",
        dimensions=dims, filters=[Filter("region", "!=", "西南")],
        additivity=Additivity.ADDITIVE.value,
        description="v2 口径：剔除西南区域后的收入",
    ), "alice", "口径改版：剔除西南")
    reg.publish("revenue", "alice")

    # 8) conflicting draft — AVG over the exact same scope as revenue (blocked)
    reg.register_metric("revenue_avg_conflict", "收入均值（冲突样例）", "bob")
    reg.create_draft("revenue_avg_conflict", MetricSpec(
        source_table="orders", measure_field="sales_amount", agg_func="AVG",
        dimensions=dims, additivity=Additivity.NON.value,
        description="试图在收入口径同一范围上发布不同聚合，应被阻断",
    ), "bob", "冲突登记尝试")
    blocked = None
    try:
        reg.publish("revenue_avg_conflict", "bob")
    except Exception as ex:  # noqa: BLE001 - surfaced in seed report
        blocked = str(ex)

    return {"rows": n_rows, "blocked_publish": blocked}


if __name__ == "__main__":
    if DB.exists():
        DB.unlink()
    storage.init_db(DB)
    with storage.get_db(DB) as conn:
        report = seed(conn)
    print("seed complete:", report)
