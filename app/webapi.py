"""HTTP layer: pydantic request models and REST endpoints.

The service objects are wired per-request against a single SQLite file so the
same app works under uvicorn and under TestClient with a temp database.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import Response, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import storage
from .domain import ConflictError, Filter, MetricSpec, SemanticError
from .engine import PermissionError_, QueryEngine, QueryRequest
from .registry import Registry

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def get_db_path() -> Path:
    return Path(os.environ.get("METRICS_DB", storage.DB_PATH))


def get_registry() -> Registry:
    return Registry(storage.get_db(get_db_path()))


# -- request models ----------------------------------------------------------

class FilterIn(BaseModel):
    field: str
    op: str
    value: Any


class MetricSpecIn(BaseModel):
    source_table: str
    measure_field: str
    agg_func: str
    dimensions: list[str] = Field(default_factory=list)
    filters: list[FilterIn] = Field(default_factory=list)
    time_grain: str = "day"
    additivity: str = "additive"
    ratio_num_field: str | None = None
    ratio_den_field: str | None = None
    description: str = ""

    def to_domain(self) -> MetricSpec:
        return MetricSpec(
            source_table=self.source_table,
            measure_field=self.measure_field,
            agg_func=self.agg_func,
            dimensions=list(self.dimensions),
            filters=[Filter(f.field, f.op, f.value) for f in self.filters],
            time_grain=self.time_grain,
            additivity=self.additivity,
            ratio_num_field=self.ratio_num_field,
            ratio_den_field=self.ratio_den_field,
            description=self.description,
        )


class RegisterMetricIn(BaseModel):
    code: str
    name: str
    owner: str
    spec: MetricSpecIn
    change_note: str = ""


class DraftIn(BaseModel):
    spec: MetricSpecIn
    change_note: str = ""


class QueryIn(BaseModel):
    version: int | None = None
    time_grain: str = "day"
    dimensions: list[str] = Field(default_factory=list)
    filters: list[FilterIn] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    limit: int = 10000
    use_cache: bool = True


class SourceIn(BaseModel):
    name: str
    description: str = ""
    columns: list[dict]


def _actor(x_user: str | None) -> str:
    return x_user or "anonymous"


def create_app(db_path: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="指标语义层", version="1.0.0")
    resolved_db = Path(db_path) if db_path else get_db_path()
    storage.init_db(resolved_db)

    def connect() -> sqlite3.Connection:
        return storage.get_db(resolved_db)

    def get_registry() -> Registry:
        return Registry(connect())

    def error(ex: Exception, status: int) -> HTTPException:
        return HTTPException(status_code=status, detail=str(ex))

    # -- sources -------------------------------------------------------------

    @app.post("/api/sources")
    def register_source(body: SourceIn, x_user: str | None = Header(None)):
        reg = get_registry()
        try:
            sid = reg.register_source(body.name, body.columns, body.description)
        except SemanticError as ex:
            raise error(ex, 400)
        return {"id": sid, "name": body.name}

    @app.get("/api/sources")
    def list_sources():
        return [dict(r) for r in get_registry().list_sources()]

    # -- metrics -------------------------------------------------------------

    @app.post("/api/metrics")
    def register_metric(body: RegisterMetricIn, x_user: str | None = Header(None)):
        reg = get_registry()
        actor = _actor(x_user)
        try:
            mid = reg.register_metric(body.code, body.name, body.owner)
            version = reg.create_draft(body.code, body.spec.to_domain(), actor,
                                       body.change_note)
            published = reg.publish(body.code, actor)
        except ConflictError as ex:
            raise error(ex, 409)
        except SemanticError as ex:
            raise error(ex, 400)
        return {"id": mid, "code": body.code, "version": version,
                "status": published["status"]}

    @app.get("/api/metrics")
    def list_metrics():
        return get_registry().list_metrics()

    @app.get("/api/metrics/{code}")
    def get_metric(code: str):
        try:
            meta = get_registry().get_metric(code)
            versions = get_registry().list_versions(code)
        except SemanticError as ex:
            raise error(ex, 404)
        return {**meta, "versions": versions}

    @app.get("/api/metrics/{code}/versions/{version}")
    def get_version(code: str, version: int):
        try:
            return get_registry().get_version(code, version)
        except SemanticError as ex:
            raise error(ex, 404)

    @app.post("/api/metrics/{code}/drafts")
    def create_draft(code: str, body: DraftIn, x_user: str | None = Header(None)):
        try:
            version = get_registry().create_draft(
                code, body.spec.to_domain(), _actor(x_user), body.change_note)
        except SemanticError as ex:
            raise error(ex, 400)
        return {"code": code, "draft_version": version, "status": "draft"}

    @app.post("/api/metrics/{code}/publish")
    def publish(code: str, x_user: str | None = Header(None)):
        try:
            v = get_registry().publish(code, _actor(x_user))
        except ConflictError as ex:
            raise error(ex, 409)
        except SemanticError as ex:
            raise error(ex, 400)
        return {"code": code, "version": v["version"], "status": v["status"]}

    @app.delete("/api/metrics/{code}/drafts")
    def discard_draft(code: str, x_user: str | None = Header(None)):
        try:
            get_registry().discard_draft(code, _actor(x_user))
        except SemanticError as ex:
            raise error(ex, 400)
        return {"code": code, "discarded": True}

    @app.post("/api/metrics/{code}/rollback")
    def rollback(code: str, version: int = Query(...), x_user: str | None = Header(None)):
        try:
            new_version = get_registry().rollback(code, version, _actor(x_user))
        except SemanticError as ex:
            raise error(ex, 400)
        return {"code": code, "draft_version": new_version,
                "rolled_back_from": version, "note": "回滚生成新草稿，需重新发布"}

    @app.post("/api/metrics/{code}/deprecate")
    def deprecate(code: str, x_user: str | None = Header(None)):
        try:
            get_registry().deprecate(code, _actor(x_user))
        except SemanticError as ex:
            raise error(ex, 400)
        return {"code": code, "deprecated": True}

    @app.get("/api/metrics/{code}/lineage")
    def lineage(code: str, version: int | None = None):
        try:
            return get_registry().get_lineage(code, version)
        except SemanticError as ex:
            raise error(ex, 404)

    @app.get("/api/metrics/{code}/diff")
    def diff(code: str, left: int = Query(...), right: int = Query(...)):
        try:
            return get_registry().diff_versions(code, left, right)
        except SemanticError as ex:
            raise error(ex, 404)

    # -- query & export ------------------------------------------------------

    def _build_request(code: str, body: QueryIn, username: str) -> QueryRequest:
        return QueryRequest(
            metric_code=code,
            version=body.version,
            time_grain=body.time_grain,
            dimensions=list(body.dimensions),
            filters=[Filter(f.field, f.op, f.value) for f in body.filters],
            date_from=body.date_from,
            date_to=body.date_to,
            limit=body.limit,
            username=username,
        )

    @app.post("/api/query/{code}")
    def query(code: str, body: QueryIn, x_user: str | None = Header(None)):
        username = _actor(x_user)
        conn = connect()
        engine = QueryEngine(conn, Registry(conn))
        req = _build_request(code, body, username)
        try:
            return engine.run(req, use_cache=body.use_cache)
        except PermissionError_ as ex:
            raise error(ex, 403)
        except SemanticError as ex:
            raise error(ex, 400)

    @app.post("/api/export/{code}")
    def export(code: str, body: QueryIn, fmt: str = Query("csv"),
               x_user: str | None = Header(None)):
        username = _actor(x_user)
        conn = connect()
        engine = QueryEngine(conn, Registry(conn))
        req = _build_request(code, body, username)
        try:
            media, filename, data = engine.export(req, fmt)
        except PermissionError_ as ex:
            raise error(ex, 403)
        except SemanticError as ex:
            raise error(ex, 400)
        return Response(content=data, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.post("/api/cache/invalidate")
    def invalidate_cache(metric_code: str | None = Query(None)):
        conn = connect()
        n = QueryEngine(conn, Registry(conn)).invalidate(metric_code)
        return {"invalidated": n}

    @app.get("/api/cache")
    def list_cache():
        conn = connect()
        rows = conn.execute(
            "SELECT cache_key, metric_code, version, hit_count, created_at "
            "FROM query_cache ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/audit")
    def audit(limit: int = 100):
        return get_registry().audit_trail(limit)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    # -- static SPA ----------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


app = create_app()
