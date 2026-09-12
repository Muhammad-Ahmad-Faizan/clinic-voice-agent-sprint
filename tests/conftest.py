"""Shared pytest fixtures for the clinic-voice-agent test suite."""

import pytest

from app.config import settings
from app.routers import tools as tools_router
from app.services import n8n_client


@pytest.fixture(autouse=True)
def _isolate_decision_log(tmp_path_factory, monkeypatch):
    """Keep every test's decision log out of the repo (and off any real DB).

    Runs for every test: points DECISION_LOG_PATH at a session-temp file and
    pins DATABASE_URL empty, so neither the default `data/decision_log.jsonl`
    nor a developer's real Postgres is ever written to by the suite. Tests
    that exercise the Postgres path (test_decision_log_postgres.py) override
    DATABASE_URL in their own fixture.
    """
    log_dir = tmp_path_factory.mktemp("decision_log")
    monkeypatch.setattr(
        settings, "decision_log_path", str(log_dir / "decision_log.jsonl")
    )
    monkeypatch.setattr(settings, "database_url", "")


@pytest.fixture(autouse=True)
def _clear_booking_store():
    """Reset the in-memory idempotency store between tests.

    The store lives at module scope (it models a cross-request cache), so
    tests must start from a clean slate.
    """
    tools_router.BOOKING_STORE.clear()
    yield
    tools_router.BOOKING_STORE.clear()


@pytest.fixture(autouse=True)
def _reset_n8n_client():
    """Discard the shared n8n httpx client before (and after) each test.

    respx intercepts requests on httpx clients that are created while its mock
    router is active. The production client module caches a singleton, so
    tests drop it to guarantee a fresh client that picks up the respx
    transport.
    """
    n8n_client._client = None
    yield
    n8n_client._client = None