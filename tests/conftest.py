"""Shared test harness (rebuild contract, owned by §1).

Sets the environment BEFORE `main` is imported: hash embedder, temp SQLite hooks DB / projects / graph dirs,
SQLite vector + store + graph backends, crons off, sign-in and API key off. Provides:
    client       FastAPI TestClient with lifespan (function scope)
    fresh_store  wipes every vector collection + the projects table (use when a test needs an empty store)
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="kh-tests-")

os.environ.update({
    "KH_EMBEDDER": "hash",
    "KH_HOOKS_DB": os.path.join(_TMP, "hooks.db"),
    "KH_PROJECTS_PATH": os.path.join(_TMP, "projects"),
    "KH_KUZU_PROJECTS_PATH": os.path.join(_TMP, "kuzu_projects"),
    "CHROMA_PATH": os.path.join(_TMP, "chroma"),
    "KH_VECTOR_BACKEND": "sqlite",
    "KH_STORE_BACKEND": "sqlite",
    "GRAPH_BACKEND": "sqlite",
    "KH_FEEDBACK_CRON": "false",
    "KH_MESSAGE_CRON": "false",
    "KH_CAPS_CRONS": "false",
    "KH_PRELOAD_VISION": "false",
    "KH_EVENTS": "off",
})
for _var in ("KH_AUTH_ISSUER", "KH_API_KEY", "KH_DB_BACKEND", "KH_LEARNBACK_LLM", "KH_EMBED_URL", "KH_VERSION"):
    os.environ.pop(_var, None)

import main  # noqa: E402  (after the environment is set)
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client():
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def fresh_store():
    """Empty vector store and projects table (orchestrator is recreated by the next client lifespan)."""
    from app import sqldb
    from app import vector_store as vs

    with sqldb.sqlite_connect() as c:
        c.execute("DELETE FROM KH_VEC_ROWS")
        c.execute("DELETE FROM KH_VEC_COLLECTIONS")
        try:
            c.execute("DELETE FROM projects")
        except Exception:  # noqa: BLE001
            pass
    vs.reset_caches()
    vs._client.get_or_create_collection(vs.config.COLLECTION_NAME, metadata=vs.COSINE)
    vs._client.get_or_create_collection(vs.config.KEYS_COLLECTION_NAME, metadata=vs.COSINE)
    yield


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TMP, ignore_errors=True)
