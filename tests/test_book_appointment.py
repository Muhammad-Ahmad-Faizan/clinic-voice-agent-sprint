"""Endpoint tests for POST /tools/book-appointment (n8n mocked out).

Includes the idempotency acceptance check: replaying the same toolCallId
returns the cached booking_reference and never calls n8n a second time.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import n8n_client
from app.services.n8n_client import N8NError

client = TestClient(app)

BOOKING_PAYLOAD = {
    "message": {
        "type": "tool-calls",
        "toolCalls": [
            {
                "id": "toolu_book_01",
                "type": "function",
                "function": {
                    "name": "bookAppointment",
                    "parameters": {
                        "patient_name": "Jane Doe",
                        "phone_number": "+1-555-0100",
                        "date": "2026-09-12",
                        "time": "10:30",
                        "service_type": "cleaning",
                    },
                },
            }
        ],
    }
}


def test_book_appointment_success_shape():
    mocked = AsyncMock(return_value="BK-2026-0001")
    with patch("app.services.n8n_client.create_booking", mocked):
        response = client.post("/tools/book-appointment", json=BOOKING_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    result = body["results"][0]
    assert result["toolCallId"] == "toolu_book_01"
    assert result["error"] is None
    assert result["result"]["booking_reference"] == "BK-2026-0001"
    assert "Jane Doe" in result["result"]["confirmation"]
    assert "BK-2026-0001" in result["result"]["confirmation"]
    mocked.assert_awaited_once_with(
        "Jane Doe", "+1-555-0100", "2026-09-12", "10:30", "cleaning"
    )


def test_book_appointment_idempotent_same_tool_call_id():
    """Acceptance: same toolCallId twice -> same reference, n8n called once."""
    mocked = AsyncMock(return_value="BK-2026-0001")
    with patch("app.services.n8n_client.create_booking", mocked):
        first = client.post("/tools/book-appointment", json=BOOKING_PAYLOAD)
        second = client.post("/tools/book-appointment", json=BOOKING_PAYLOAD)

    assert first.status_code == second.status_code == 200
    first_result = first.json()["results"][0]
    second_result = second.json()["results"][0]
    assert (
        first_result["result"]["booking_reference"]
        == second_result["result"]["booking_reference"]
        == "BK-2026-0001"
    )
    assert first_result["result"]["confirmation"] == second_result["result"]["confirmation"]
    assert mocked.await_count == 1  # never double-booked


def test_book_appointment_different_tool_call_ids_book_again():
    second_payload = json.loads(json.dumps(BOOKING_PAYLOAD))
    second_payload["message"]["toolCalls"][0]["id"] = "toolu_book_02"

    mocked = AsyncMock(side_effect=["BK-2026-0001", "BK-2026-0002"])
    with patch("app.services.n8n_client.create_booking", mocked):
        client.post("/tools/book-appointment", json=BOOKING_PAYLOAD)
        second = client.post("/tools/book-appointment", json=second_payload)

    assert mocked.await_count == 2
    assert second.json()["results"][0]["result"]["booking_reference"] == "BK-2026-0002"


def test_book_appointment_invalid_args_graceful():
    payload = {
        "message": {
            "toolCalls": [
                {
                    "id": "toolu_bad_01",
                    "function": {
                        "name": "bookAppointment",
                        "parameters": {
                            "patient_name": "Jane Doe",
                            "date": "2026-09-12",  # missing phone_number, time, service_type
                        },
                    },
                }
            ]
        }
    }
    mocked = AsyncMock()
    with patch("app.services.n8n_client.create_booking", mocked):
        response = client.post("/tools/book-appointment", json=payload)

    assert response.status_code == 200
    assert "didn't get all your booking details" in response.json()["results"][0]["result"]
    mocked.assert_not_awaited()


def test_book_appointment_n8n_error_fallback_then_retry_books():
    # First attempt fails in n8n; a retry with the same toolCallId must NOT be
    # served from cache (only successes are cached) and should actually book.
    mocked = AsyncMock(side_effect=[N8NError("n8n down"), "BK-2026-0001"])
    with patch("app.services.n8n_client.create_booking", mocked):
        first = client.post("/tools/book-appointment", json=BOOKING_PAYLOAD)
        second = client.post("/tools/book-appointment", json=BOOKING_PAYLOAD)

    assert "trouble booking" in first.json()["results"][0]["result"]
    assert second.json()["results"][0]["result"]["booking_reference"] == "BK-2026-0001"
    assert mocked.await_count == 2


@pytest.mark.anyio
async def test_create_booking_parses_booking_reference(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/webhook/book-appointment"
        assert json.loads(request.content) == {
            "patient_name": "Jane Doe",
            "phone_number": "+1-555-0100",
            "date": "2026-09-12",
            "time": "10:30",
            "service_type": "cleaning",
        }
        return httpx.Response(200, json={"booking_reference": "BK-2026-0001"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(n8n_client, "get_client", lambda: client)
        monkeypatch.setattr(
            n8n_client.settings, "n8n_webhook_url", "https://n8n.example.com/webhook"
        )
        reference = await n8n_client.create_booking(
            "Jane Doe", "+1-555-0100", "2026-09-12", "10:30", "cleaning"
        )

    assert reference == "BK-2026-0001"


@pytest.mark.anyio
async def test_create_booking_missing_reference_raises(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(n8n_client, "get_client", lambda: client)
        monkeypatch.setattr(
            n8n_client.settings, "n8n_webhook_url", "https://n8n.example.com/webhook"
        )
        with pytest.raises(N8NError, match="no booking_reference"):
            await n8n_client.create_booking(
                "Jane Doe", "+1-555-0100", "2026-09-12", "10:30", "cleaning"
            )