"""HTTP-level integration tests: the n8n webhook is mocked with respx.

Unlike the endpoint tests that patch the n8n client functions, these drive
every /tools endpoint end-to-end (HTTP request in, Vapi-shaped JSON out) with
the *outbound HTTP call to n8n* simulated by respx at the httpx transport
level. This exercises the real n8n client code: URL building, JSON parsing,
timeouts and the single transient-failure retry.

Run with:  .venv\\Scripts\\python.exe -m pytest tests/test_n8n_http_mock.py -v
"""

import json
import uuid

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app

client = TestClient(app)

# Pinned n8n base URL; respx intercepts requests to https://n8n.test/... before
# they leave the process.
N8N_BASE = "https://n8n.test/webhook"

CHECK_PAYLOAD = {
    "message": {
        "type": "tool-calls",
        "toolCalls": [
            {
                "id": "toolu_check_http_01",
                "type": "function",
                "function": {
                    "name": "checkAvailability",
                    "parameters": {
                        "date": "2026-09-10",
                        "service_type": "cleaning",
                    },
                },
            }
        ],
    }
}

BOOK_PAYLOAD = {
    "message": {
        "type": "tool-calls",
        "toolCalls": [
            {
                "id": "toolu_book_http_01",
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

CANCEL_PAYLOAD = {
    "message": {
        "type": "tool-calls",
        "toolCalls": [
            {
                "id": "toolu_cancel_http_01",
                "type": "function",
                "function": {
                    "name": "cancelAppointment",
                    "parameters": {"booking_reference": "BK-2026-0001"},
                },
            }
        ],
    }
}

FALLBACK_MESSAGE = (
    "I'm having trouble checking availability right now, "
    "let me take your number and have someone call you back"
)


@pytest.fixture(autouse=True)
def _pin_n8n_settings(monkeypatch):
    """Point n8n at the respx base URL and pin the retry count to 1.

    Pinning the max retries keeps the 503 test deterministic regardless of a
    developer's local .env value.
    """
    monkeypatch.setattr(settings, "n8n_webhook_url", N8N_BASE)
    monkeypatch.setattr(settings, "n8n_max_retries", 1)


# --- POST /tools/check-availability -------------------------------------------


def test_check_availability_with_mocked_n8n():
    with respx.mock(base_url=N8N_BASE) as router:
        route = router.post(N8N_BASE).mock(
            return_value=httpx.Response(
            200,
            json={"available": False, "alternative_slots": ["10:00 AM", "2:30 PM"]},
            )
        )
        response = client.post("/tools/check-availability", json=CHECK_PAYLOAD)

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["toolCallId"] == "toolu_check_http_01"
    assert result["error"] is None
    assert result["result"] == "Open slots: 10:00 AM, 2:30 PM"
    assert route.call_count == 1


def test_check_availability_no_slots_with_mocked_n8n():
    with respx.mock(base_url=N8N_BASE) as router:
        route = router.post(N8N_BASE).mock(
            return_value=httpx.Response(200, json={"available": False, "alternative_slots": []})
        )
        response = client.post("/tools/check-availability", json=CHECK_PAYLOAD)

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert "no open slots" in result["result"]
    assert route.call_count == 1


def test_check_availability_n8n_503_retries_then_fallback():
    """A transient n8n 503 is retried once, then surfaced as a fallback."""
    with respx.mock(base_url=N8N_BASE) as router:
        route = router.post(N8N_BASE).mock(
            return_value=httpx.Response(503, json={"error": "n8n down"})
        )
        response = client.post("/tools/check-availability", json=CHECK_PAYLOAD)

    assert response.status_code == 200
    assert response.json()["results"][0]["result"] == FALLBACK_MESSAGE
    assert route.call_count == 2  # initial attempt + 1 retry (N8N_MAX_RETRIES=1)


# --- POST /tools/book-appointment ---------------------------------------------


def test_book_appointment_with_mocked_n8n():
    received: list[dict] = []

    def book_handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "success", "appointment_id": "APPT-2026-0001"}
        )

    with respx.mock(base_url=N8N_BASE) as router:
        route = router.post(N8N_BASE).mock(side_effect=book_handler)
        response = client.post("/tools/book-appointment", json=BOOK_PAYLOAD)

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["toolCallId"] == "toolu_book_http_01"
    assert result["error"] is None
    assert result["result"]["booking_reference"] == "APPT-2026-0001"
    assert "Jane Doe" in result["result"]["confirmation"]

    # The exact JSON body n8n received:
    assert received == [
        {
            "action": "book",
            "patient_name": "Jane Doe",
            "reason": "cleaning",
            "requested_date": "2026-09-12",
            "requested_time": "10:30",
            "call_id": "toolu_book_http_01",
        }
    ]
    assert route.call_count == 1


def test_book_appointment_idempotent_with_mocked_n8n():
    """Same toolCallId twice -> same reference, n8n receives exactly ONE call."""
    with respx.mock(base_url=N8N_BASE) as router:
        route = router.post(N8N_BASE).mock(
            return_value=httpx.Response(
            200, json={"status": "success", "appointment_id": "APPT-2026-0001"}
            )
        )
        first = client.post("/tools/book-appointment", json=BOOK_PAYLOAD)
        second = client.post("/tools/book-appointment", json=BOOK_PAYLOAD)

    assert first.status_code == second.status_code == 200
    first_result = first.json()["results"][0]
    second_result = second.json()["results"][0]
    assert (
        first_result["result"]["booking_reference"]
        == second_result["result"]["booking_reference"]
        == "APPT-2026-0001"
    )
    assert (
        first_result["result"]["confirmation"]
        == second_result["result"]["confirmation"]
    )
    # The cache short-circuited the second call: n8n was hit exactly once.
    assert route.call_count == 1


def test_book_appointment_different_tool_call_ids_book_twice():
    """A genuinely new toolCallId must hit n8n again (not served from cache)."""
    second_payload = json.loads(json.dumps(BOOK_PAYLOAD))
    second_payload["message"]["toolCalls"][0][
        "id"
    ] = f"toolu_book_http_{uuid.uuid4().hex[:8]}"

    with respx.mock(base_url=N8N_BASE) as router:
        route = router.post(N8N_BASE).mock(
            side_effect=[
            httpx.Response(200, json={"status": "success", "appointment_id": "APPT-2026-0001"}),
            httpx.Response(200, json={"status": "success", "appointment_id": "APPT-2026-0002"}),
            ]
        )
        first = client.post("/tools/book-appointment", json=BOOK_PAYLOAD)
        second = client.post("/tools/book-appointment", json=second_payload)

    assert route.call_count == 2
    assert first.json()["results"][0]["result"]["booking_reference"] == "APPT-2026-0001"
    assert second.json()["results"][0]["result"]["booking_reference"] == "APPT-2026-0002"


# --- POST /tools/cancel-appointment --------------------------------------------


def test_cancel_appointment_with_mocked_n8n():
    received: list[dict] = []

    def cancel_handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "success"})

    with respx.mock(base_url=N8N_BASE) as router:
        route = router.post(N8N_BASE).mock(side_effect=cancel_handler)
        response = client.post("/tools/cancel-appointment", json=CANCEL_PAYLOAD)

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["toolCallId"] == "toolu_cancel_http_01"
    assert result["error"] is None
    assert "BK-2026-0001 has been cancelled" in result["result"]
    assert received == [{"action": "cancel", "existing_appointment_id": "BK-2026-0001"}]
    assert route.call_count == 1


# --- POST /tools/booking (reschedule, dedicated webhook) -----------------------

RESCHEDULE_BASE = "https://n8n.test/reschedule"
RESCHEDULE_PAYLOAD = {
    "call_id": "call_resched_http_01",
    "intent": "reschedule",
    "existing_appointment_id": "BK-2026-0001",
    "requested_date": "2026-09-20",
    "requested_time": "14:00",
}


def test_reschedule_success_with_mocked_n8n(monkeypatch):
    """The exact payload hits N8N_RESCHEDULE_WEBHOOK_URL; success is confirmed."""
    monkeypatch.setattr(settings, "n8n_reschedule_webhook_url", RESCHEDULE_BASE)
    received: list[dict] = []

    def reschedule_handler(request: httpx.Request) -> httpx.Response:
        received.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "success", "appointment_id": "APPT-9999"})

    with respx.mock() as router:
        route = router.post(RESCHEDULE_BASE).mock(side_effect=reschedule_handler)
        response = client.post("/tools/booking", json=RESCHEDULE_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["action_taken"] == "rescheduled"
    assert body["booking_reference"] == "APPT-9999"
    assert "has been moved to 2026-09-20 at 14:00" in body["message"]

    # The exact JSON body the reschedule workflow received:
    assert received == [
        {
            "existing_appointment_id": "BK-2026-0001",
            "new_date": "2026-09-20",
            "new_time": "14:00",
        }
    ]
    assert route.call_count == 1


def test_reschedule_conflict_offers_alternatives_with_mocked_n8n(monkeypatch):
    """A `conflict` answer triggers the alternative-slot offer via availability."""
    monkeypatch.setattr(settings, "n8n_reschedule_webhook_url", RESCHEDULE_BASE)
    with respx.mock() as router:
        router.post(RESCHEDULE_BASE).mock(
            return_value=httpx.Response(200, json={"status": "conflict"})
        )
        router.post(N8N_BASE).mock(
            return_value=httpx.Response(200, json={"available_slots": ["11:00 AM", "2:30 PM"]})
        )
        response = client.post("/tools/booking", json=RESCHEDULE_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["action_taken"] == "reschedule_conflict"
    assert "11:00 AM" in body["message"]
    assert "Would any of those work" in body["message"]