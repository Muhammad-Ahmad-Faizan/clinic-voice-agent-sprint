"""Hardening tests for POST /tools/booking and GET /dashboard.

Covers the acceptance checks from Phases 1-3:
  - a malformed request (missing `call_id`, unknown `intent`, unknown extra
    field) returns a clean 4xx, never crashes or runs business logic;
  - an n8n failure returns a spoken-friendly fallback `message` (with a
    `failure` decision-log entry), never a raw exception or stack trace;
  - a successful booking returns a confirmation and writes a `success`
    decision-log entry;
  - reschedule/unclear escalate; cancel is guarded; the dashboard renders the
    recorded entries (no auth, plain HTML).
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.decision_log import log_decision, read_entries
from app.main import app
from app.services.n8n_client import N8NError

client = TestClient(app)

VALID_BOOKING = {
    "call_id": "call_book_ok",
    "intent": "book",
    "patient_name": "Jane Doe",
    "requested_date": "2026-09-15",
    "requested_time": "10:30",
    "reason": "cleaning",
    "test_case_tag": "suite-booking-success",
}


@pytest.fixture(autouse=True)
def _isolated_decision_log(tmp_path, monkeypatch):
    """Point the decision log at a temp file (and disable the DB backend).

    These tests exercise the JSON-lines fallback; pin database_url empty so a
    dev .env with DATABASE_URL set can't redirect entries into a real database.
    """
    monkeypatch.setattr(
        settings, "decision_log_path", str(tmp_path / "decision_log.jsonl")
    )
    monkeypatch.setattr(settings, "database_url", "")


# --- Phase 1: request validation -------------------------------------------


def test_booking_missing_call_id_returns_4xx():
    response = client.post("/tools/booking", json={"intent": "book"})
    assert response.status_code == 422
    body = response.json()
    assert body["detail"][0]["loc"] == ["body", "call_id"]


def test_booking_empty_call_id_returns_4xx():
    response = client.post("/tools/booking", json={"call_id": "", "intent": "book"})
    assert response.status_code == 422


def test_booking_unknown_intent_returns_4xx():
    response = client.post(
        "/tools/booking", json={"call_id": "call_1", "intent": "schedule"}
    )
    assert response.status_code == 422
    assert "intent" in response.text


def test_booking_unknown_extra_field_returns_4xx():
    response = client.post(
        "/tools/booking", json={"call_id": "call_1", "intent": "book", "surprise": True}
    )
    assert response.status_code == 422


# --- Phase 3: fallback error handling --------------------------------------


def test_booking_n8n_failure_falls_back_to_graceful_message():
    mocked = AsyncMock(side_effect=N8NError("calendar down"))
    with patch("app.services.n8n_client.create_booking", mocked):
        response = client.post("/tools/booking", json=VALID_BOOKING)

    assert response.status_code == 200
    body = response.json()
    # Spoken-friendly fallback, never a traceback:
    assert "having trouble" in body["message"]
    assert "call you back" in body["message"]
    assert body["action_taken"] == "book"
    assert body["booking_reference"] is None

    # The same decision is recorded as a structured failure entry:
    entries = read_entries()
    assert len(entries) == 1
    assert entries[0]["call_id"] == "call_book_ok"
    assert entries[0]["outcome"] == "failure"
    assert entries[0]["action_taken"] == "book"


def test_booking_unexpected_error_also_falls_back():
    mocked = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.services.n8n_client.create_booking", mocked):
        response = client.post("/tools/booking", json=VALID_BOOKING)

    assert response.status_code == 200
    assert "call you back" in response.json()["message"]


# --- Phase 2: successful booking writes a log entry ------------------------


def test_booking_success_writes_decision_log_entry():
    mocked = AsyncMock(return_value="BK-2026-0042")
    with patch("app.services.n8n_client.create_booking", mocked):
        response = client.post("/tools/booking", json=VALID_BOOKING)

    assert response.status_code == 200
    body = response.json()
    assert body["action_taken"] == "book"
    assert body["booking_reference"] == "BK-2026-0042"
    assert "BK-2026-0042" in body["message"]

    mocked.assert_awaited_once_with(
        "Jane Doe", "", "2026-09-15", "10:30", "cleaning"
    )

    entries = read_entries()
    assert len(entries) == 1
    assert entries[0]["call_id"] == "call_book_ok"
    assert entries[0]["intent_detected"] == "book"
    assert entries[0]["action_taken"] == "book"
    assert entries[0]["outcome"] == "success"
    assert entries[0]["test_case_tag"] == "suite-booking-success"
    assert entries[0]["timestamp"]


# --- Phase 7: real reschedule ---------------------------------------------


def test_booking_reschedule_missing_fields_asks_for_details():
    """No crash without all three fields — ask a clarifying question."""
    mocked = AsyncMock()
    with patch(
        "app.services.n8n_client.reschedule_booking", mocked
    ) as reschedule_mock, patch(
        "app.services.n8n_client.get_availability", AsyncMock()
    ) as availability_mock:
        response = client.post(
            "/tools/booking",
            json={
                "call_id": "call_resched_missing",
                "intent": "reschedule",
                "existing_appointment_id": "BK-2026-0001",  # no new date/time
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["action_taken"] == "reschedule_rejected"
    assert "booking reference plus the new date and time" in body["message"]
    reschedule_mock.assert_not_awaited()
    availability_mock.assert_not_awaited()

    entries = read_entries()
    assert entries[0]["intent_detected"] == "reschedule"
    assert entries[0]["action_taken"] == "reschedule_rejected"
    assert entries[0]["outcome"] == "failure"


def test_booking_reschedule_success_logs_rescheduled():
    mocked = AsyncMock(return_value="APPT-9876")
    with patch("app.services.n8n_client.reschedule_booking", mocked):
        response = client.post(
            "/tools/booking",
            json={
                "call_id": "call_resched_ok",
                "intent": "reschedule",
                "existing_appointment_id": "BK-2026-0001",
                "requested_date": "2026-09-20",
                "requested_time": "14:00",
                "test_case_tag": "suite-reschedule-success",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["action_taken"] == "rescheduled"
    assert body["booking_reference"] == "APPT-9876"
    assert "BK-2026-0001 has been moved to 2026-09-20 at 14:00" in body["message"]

    mocked.assert_awaited_once_with("BK-2026-0001", "2026-09-20", "14:00")

    entry = read_entries()[0]
    assert entry["intent_detected"] == "reschedule"
    assert entry["action_taken"] == "rescheduled"
    assert entry["outcome"] == "success"
    assert entry["test_case_tag"] == "suite-reschedule-success"


def test_booking_reschedule_conflict_offers_alternative_slots():
    from app.services.n8n_client import RescheduleConflictError

    with patch(
        "app.services.n8n_client.reschedule_booking",
        AsyncMock(side_effect=RescheduleConflictError("slot taken")),
    ), patch(
        "app.services.n8n_client.get_availability",
        AsyncMock(return_value={"available_slots": ["11:00 AM", "2:30 PM"]}),
    ):
        response = client.post(
            "/tools/booking",
            json={
                "call_id": "call_resched_conflict",
                "intent": "reschedule",
                "existing_appointment_id": "BK-2026-0001",
                "requested_date": "2026-09-20",
                "requested_time": "10:00",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["action_taken"] == "reschedule_conflict"
    assert "11:00 AM" in body["message"]
    assert "2:30 PM" in body["message"]
    assert "Would any of those work" in body["message"]

    entry = read_entries()[0]
    assert entry["action_taken"] == "reschedule_conflict"
    assert entry["outcome"] == "failure"


def test_booking_reschedule_n8n_exception_falls_back():
    mocked = AsyncMock(side_effect=N8NError("reschedule webhook down"))
    with patch("app.services.n8n_client.reschedule_booking", mocked):
        response = client.post(
            "/tools/booking",
            json={
                "call_id": "call_resched_fail",
                "intent": "reschedule",
                "existing_appointment_id": "BK-2026-0001",
                "requested_date": "2026-09-20",
                "requested_time": "10:00",
            },
        )

    assert response.status_code == 200
    assert "call you back" in response.json()["message"]
    entry = read_entries()[0]
    assert entry["action_taken"] == "reschedule"
    assert entry["outcome"] == "failure"


def test_booking_unclear_asks_for_clarification():
    response = client.post(
        "/tools/booking", json={"call_id": "call_unclear", "intent": "unclear"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action_taken"] == "ask_clarification"
    assert "didn't quite catch that" in body["message"]
    assert read_entries()[0]["outcome"] == "escalated"


def test_booking_incomplete_book_rejected_without_calling_n8n():
    mocked = AsyncMock()
    with patch("app.services.n8n_client.create_booking", mocked):
        response = client.post(
            "/tools/booking",
            json={"call_id": "call_inc", "intent": "book", "patient_name": "Jane Doe"},
        )

    assert response.status_code == 200
    assert "name, the date, and a preferred time" in response.json()["message"]
    mocked.assert_not_awaited()
    assert read_entries()[0]["outcome"] == "failure"


def test_booking_cancel_success_logged():
    mocked = AsyncMock(return_value={})
    with patch("app.services.n8n_client.cancel_booking", mocked):
        response = client.post(
            "/tools/booking",
            json={
                "call_id": "call_cancel_ok",
                "intent": "cancel",
                "existing_appointment_id": "BK-2026-0001",
            },
        )

    assert response.status_code == 200
    assert "BK-2026-0001 has been cancelled" in response.json()["message"]
    mocked.assert_awaited_once_with("BK-2026-0001")
    entry = read_entries()[0]
    assert entry["action_taken"] == "cancel"
    assert entry["outcome"] == "success"


# --- Phase 5: read-only dashboard ------------------------------------------


def test_dashboard_renders_log_entries_no_auth():
    # No secret is configured here; the dashboard must be public regardless.
    log_decision(
        call_id="call_dash",
        intent_detected="book",
        action_taken="book",
        outcome="success",
        notes="notes with <b>markup</b> to verify escaping",
    )
    response = client.get("/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<table" in response.text
    assert "call_dash" in response.text
    assert "<b>markup</b>" not in response.text  # escaped, not raw HTML
    assert 'class="badge success"' in response.text