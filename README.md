# 指标语义层（Metric Semantic Layer）

统一登记指标定义、粒度、过滤条件、版本与来源字段，为页面查询和导出提供
同一条执行路径；旧版本可复算，口径冲突阻断发布，跨时间粒度不重复聚合。

## 能力一览

| 需求 | 实现 |
| --- | --- |
| 登记指标定义/粒度/过滤/来源字段 | `metric_versions` 全量快照，来源表与字段先登记后引用（`app/registry.py`） |
| 版本管理 | 每次改版生成新版本行，已发布版本不可变；草稿唯一，可丢弃 |
| 旧版本复算 | 查询可指定 `version`，SQL 始终由该版本快照编译（`app/engine.py`） |
| 口径冲突阻断发布 | 发布门禁：重复签名 + 同范围不同聚合检测，冲突返回 409 并写审计（`app/domain.py`） |
| 不同时间粒度不重复聚合 | 所有粒度只从原始事件表重算；非可加指标（COUNT_DISTINCT/AVG/RATIO/MAX/MIN）有显式防再聚合守卫 |
| 页面与导出统一结果 | `/api/query/{code}` 与 `/api/export/{code}` 共用同一引擎，CSV/JSON 同源 |
| 血缘 | 每个版本记录来源表 + 度量/维度/过滤全部字段的血缘边 |
| 版本差异 | `/api/metrics/{code}/diff?left=&right=` 字段级对比 |
| 权限 | 用户角色 + 列级授权（`column_grants`），受限字段无授权返回 403 |
| 缓存 | 查询结果按「用户+版本+粒度+维度+过滤+授权」哈希缓存，可按指标或全量失效 |
| 回滚 | 以历史版本快照克隆为新草稿（不重写历史），仍需通过冲突门禁后发布 |
| 审计 | 发布、阻断、回滚、下线等动作全部落 `audit_log` |

## 目录结构

```
app/
  storage.py    # SQLite 表结构与连接
  domain.py     # 指标模型、语义校验、冲突检测、防聚合规则
  registry.py   # 登记/草稿/发布/回滚/血缘/差异/审计
  engine.py     # SQL 编译、查询、缓存、导出、权限校验、防重复聚合守卫
  webapi.py     # FastAPI REST 服务 + 静态页面托管
web/            # 原生 JS 单页：定义/血缘/差异/查询/缓存审计/管控六个标签页
scripts/seed.py # 演示数据与样例指标（含冲突样例、受限字段、不可加指标）
tests/          # 45 个 pytest 用例，覆盖全部上述能力
```

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 初始化演示库（90 天订单明细 795 行 + 7 个指标，含一个被阻断的冲突草稿）
.venv/bin/python -m scripts.seed

# 启动服务
METRICS_DB=$PWD/data/metrics.db .venv/bin/uvicorn app.webapi:app --port 8077
# 打开 http://127.0.0.1:8077 ，右上角可切换 admin / alice / bob 体验权限差异

# 测试
.venv/bin/python -m pytest tests/ -q
```

## 关键设计说明

1. **版本即快照，不是覆盖**。`metric_versions` 保存来源表、度量、聚合函数、
   维度、过滤、可加性、比率分子分母的完整定义；历史版本永远可以按当时口径重算。
2. **冲突检测取保守方向**。同表同维度、过滤域重叠且对同一度量使用不同聚合
   即阻断；只有能证明过滤域不相交（同字段等值/IN 集合互斥）才放行。
3. **防重复聚合靠架构而非自觉**。引擎没有任何"读预聚合结果再聚合"的代码路径，
   周/月/季/年都是直接 `GROUP BY` 原始明细；`assert_no_reaggregation()` 为
   接入外部预聚合数据源的场景提供显式拦截。
4. **缓存键包含版本号与用户授权签名**，改版后旧缓存不会污染新口径，不同用户
   的列级权限差异也不会串缓存。
5. **回滚是新建草稿而非删除历史**，回滚动作本身在审计日志可追溯。

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/sources` | 登记来源表及字段（支持 `restricted`、`semantic=event_time`） |
| POST | `/api/metrics` | 登记指标并直接走草稿→发布（冲突返回 409） |
| POST | `/api/metrics/{code}/drafts` | 新建改版草稿 |
| POST | `/api/metrics/{code}/publish` | 发布（经过冲突门禁） |
| POST | `/api/metrics/{code}/rollback?version=n` | 回滚为新草稿 |
| GET | `/api/metrics/{code}/lineage?version=n` | 字段级血缘 |
| GET | `/api/metrics/{code}/diff?left=a&right=b` | 版本差异 |
| POST | `/api/query/{code}` | 统一查询（body 指定粒度/维度/日期/版本） |
| POST | `/api/export/{code}?fmt=csv|json` | 统一导出 |
| GET/POST | `/api/cache`、`/api/cache/invalidate` | 缓存查看与失效 |
| GET | `/api/audit` | 审计日志 |

请求头 `X-User` 标识调用用户（admin / alice / bob）。
