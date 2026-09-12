"""Phase 8: Postgres-backed decision log via SQLAlchemy.

Exercises the same DB code path a production Postgres DATABASE_URL uses, but
with SQLite connection strings so the tests run anywhere (the ORM layer is
dialect-agnostic). Automatically *skipped* when SQLAlchemy isn't installed
(e.g. an offline environment) — run `pip install -r requirements.txt` to
enable them.

Engines are cached per connection-string, and every test uses its own tmp
SQLite file, so tests never bleed into each other.
"""

import pytest

from app.config import settings
from app.decision_log import log_decision, read_entries
from app.main import app

pytest.importorskip("sqlalchemy")

from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def _database_backed_store(tmp_path, monkeypatch):
    """Point the decision log at a fresh SQLite database per test."""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'decision_log.db'}")
    # Guard: this file must exercise the DB path, not the file fallback. If a
    # conftest fixture overwrote DATABASE_URL after us, scream instead of
    # silently testing the wrong backend.
    from app.decision_log import _database_enabled

    assert _database_enabled(), "database_url did not activate the DB decision log"


def test_log_decision_round_trips_through_database():
    log_decision(
        call_id="call_db_1",
        intent_detected="book",
        action_taken="book",
        outcome="success",
        notes="booking_reference=BK-1",
        test_case_tag="suite-db",
    )
    entries = read_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["call_id"] == "call_db_1"
    assert entry["intent_detected"] == "book"
    assert entry["action_taken"] == "book"
    assert entry["outcome"] == "success"
    assert entry["test_case_tag"] == "suite-db"
    assert entry["timestamp"]  # ISO string present


def test_read_entries_newest_first_with_limit():
    # ids 1..5 in insertion order; the two most recent come back newest first.
    for i in range(1, 6):
        log_decision(
            call_id=f"call_db_{i}",
            intent_detected="book",
            action_taken="book",
            outcome="success",
            notes=f"n={i}",
        )
    entries = read_entries(limit=2)
    assert [entry["call_id"] for entry in entries] == ["call_db_5", "call_db_4"]


def test_dashboard_renders_database_entries():
    log_decision(
        call_id="call_dash_db",
        intent_detected="cancel",
        action_taken="cancel",
        outcome="success",
    )
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "<table" in response.text
    assert "call_dash_db" in response.text
    assert 'class="badge success"' in response.text