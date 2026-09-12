"""Endpoint tests for POST /tools/cancel-appointment (n8n mocked out)."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.services.n8n_client import N8NError

client = TestClient(app)

CANCEL_PAYLOAD = {
    "message": {
        "type": "tool-calls",
        "toolCalls": [
            {
                "id": "toolu_cancel_01",
                "type": "function",
                "function": {
                    "name": "cancelAppointment",
                    "parameters": {"booking_reference": "BK-2026-0001"},
                },
            }
        ],
    }
}


def test_cancel_appointment_success():
    mocked = AsyncMock(return_value={})
    with patch("app.services.n8n_client.cancel_booking", mocked):
        response = client.post("/tools/cancel-appointment", json=CANCEL_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    result = body["results"][0]
    assert result["toolCallId"] == "toolu_cancel_01"
    assert result["error"] is None
    assert "BK-2026-0001 has been cancelled" in result["result"]
    mocked.assert_awaited_once_with("BK-2026-0001")


def test_cancel_appointment_invalid_args_graceful():
    payload = {
        "message": {
            "toolCalls": [
                {
                    "id": "toolu_cancel_bad",
                    "function": {
                        "name": "cancelAppointment",
                        "parameters": {},  # missing booking_reference
                    },
                }
            ]
        }
    }
    mocked = AsyncMock()
    with patch("app.services.n8n_client.cancel_booking", mocked):
        response = client.post("/tools/cancel-appointment", json=payload)

    assert response.status_code == 200
    assert "didn't catch the booking reference" in response.json()["results"][0]["result"]
    mocked.assert_not_awaited()


def test_cancel_appointment_n8n_error_fallback():
    mocked = AsyncMock(side_effect=N8NError("n8n down"))
    with patch("app.services.n8n_client.cancel_booking", mocked):
        response = client.post("/tools/cancel-appointment", json=CANCEL_PAYLOAD)

    assert response.status_code == 200
    assert "trouble cancelling" in response.json()["results"][0]["result"]