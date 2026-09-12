"""Endpoint tests for POST /webhook/call-ended (logging only)."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

LEGACY_CALL_ENDED_PAYLOAD = {
    "message": {
        "type": "end-of-call-report",
        "callReport": {
            "endedReason": "customer-didnt-understand",
            "summary": "Booked a cleaning appointment and confirmed the follow-up.",
            "transcript": "[transcript]",
        },
    }
}

CURRENT_CALL_ENDED_PAYLOAD = {
    "message": {
        "type": "end-of-call-report",
        "endedReason": "customer-ended-call",
        "cost": 0.42,
        "artifact": {
            "summary": "Great, see you then! Bye.",
            "planSummary": "...",
        },
        "analysis": {"summary": "Booked a cleaning appointment for Sep 12."},
        "call": {"id": "call-uuid-123", "type": "phone"},
    }
}


def test_call_ended_legacy_payload_returns_ok():
    response = client.post("/webhook/call-ended", json=LEGACY_CALL_ENDED_PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_call_ended_current_payload_returns_ok():
    response = client.post("/webhook/call-ended", json=CURRENT_CALL_ENDED_PAYLOAD)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_call_ended_empty_payload_ok():
    response = client.post("/webhook/call-ended", json={})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}