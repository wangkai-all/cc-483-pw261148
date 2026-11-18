"""Shared pytest fixtures: isolated temp DB, seeded app, HTTP client."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import storage  # noqa: E402
from scripts.seed import seed  # noqa: E402


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test_metrics.db"


@pytest.fixture()
def conn(db_path):
    storage.init_db(db_path)
    c = storage.get_db(db_path)
    report = seed(c)
    c.commit()
    yield c
    c.commit()
    c.close()


@pytest.fixture()
def client(db_path):
    # Seed through a short-lived connection and close it before the app opens
    # its own connections, so no uncommitted transaction hides the data.
    storage.init_db(db_path)
    seeder = storage.get_db(db_path)
    seed(seeder)
    seeder.commit()
    seeder.close()

    from app.webapi import create_app
    with TestClient(create_app(db_path)) as c:
        c.headers.update({"X-User": "alice"})
        yield c
