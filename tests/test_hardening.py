"""Hardening tests: signature auth (401), clean 422s/500s, outbound retries."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.middleware import setup_exception_handlers
from app.services import n8n_client
from app.services.n8n_client import N8NError

client = TestClient(app)

VALID_TOOL_PAYLOAD = {
    "message": {
        "type": "tool-calls",
        "toolCalls": [
            {
                "id": "toolu_sec_01",
                "type": "function",
                "function": {
                    "name": "checkAvailability",
                    "parameters": {"date": "2026-09-10"},
                },
            }
        ],
    }
}


# --- Signature / secret verification ----------------------------------------


def test_missing_secret_header_rejected_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "vapi_webhook_secret", "supersecret")
    response = client.post("/tools/check-availability", json=VALID_TOOL_PAYLOAD)
    assert response.status_code == 401


def test_wrong_secret_header_rejected_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "vapi_webhook_secret", "supersecret")
    response = client.post(
        "/tools/check-availability",
        json=VALID_TOOL_PAYLOAD,
        headers={"x-vapi-secret": "wrong-secret"},
    )
    assert response.status_code == 401


def test_wrong_secret_header_rejected_on_webhooks(monkeypatch):
    monkeypatch.setattr(settings, "vapi_webhook_secret", "supersecret")
    response = client.post(
        "/webhook/call-ended",
        json={"message": {"type": "end-of-call-report"}},
        headers={"x-vapi-secret": "nope"},
    )
    assert response.status_code == 401


def test_correct_secret_header_accepted(monkeypatch):
    monkeypatch.setattr(settings, "vapi_webhook_secret", "supersecret")
    mocked = AsyncMock(return_value={"available_slots": ["10:00 AM"]})
    with patch("app.services.n8n_client.get_availability", mocked):
        response = client.post(
            "/tools/check-availability",
            json=VALID_TOOL_PAYLOAD,
            headers={"x-vapi-secret": "supersecret"},
        )
    assert response.status_code == 200
    assert mocked.await_count == 1


def test_auth_disabled_when_secret_empty(monkeypatch):
    monkeypatch.setattr(settings, "vapi_webhook_secret", "")
    mocked = AsyncMock(return_value={"available_slots": ["10:00 AM"]})
    with patch("app.services.n8n_client.get_availability", mocked):
        response = client.post("/tools/check-availability", json=VALID_TOOL_PAYLOAD)
    assert response.status_code == 200


# --- Malformed payloads return clean 422s, not 500s --------------------------


def test_malformed_payload_returns_422_not_500():
    response = client.post("/tools/check-availability", json={})
    assert response.status_code == 422
    assert "detail" in response.json()


def test_bad_nested_payload_returns_422_not_500():
    response = client.post(
        "/tools/check-availability", json={"message": {"toolCalls": "nope"}}
    )
    assert response.status_code == 422
    assert "detail" in response.json()


# --- Global exception handler: clean JSON 500 --------------------------------


def test_global_error_handler_returns_clean_json():
    mini = FastAPI()
    setup_exception_handlers(mini)

    @mini.post("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    c = TestClient(mini, raise_server_exceptions=False)
    response = c.post("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}


# --- Outbound retry (max 1 on transient failures) ----------------------------


@pytest.mark.anyio
async def test_n8n_retry_recovers_on_second_attempt(monkeypatch):
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json={"available_slots": ["10:00 AM"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        monkeypatch.setattr(n8n_client, "get_client", lambda: c)
        monkeypatch.setattr(
            n8n_client.settings, "n8n_webhook_url", "https://n8n.example.com/webhook"
        )
        monkeypatch.setattr(n8n_client.settings, "n8n_max_retries", 1)
        data = await n8n_client.call_n8n_webhook(
            {"date": "2026-09-10"}, path="check-availability"
        )

    assert attempts["n"] == 2
    assert data == {"available_slots": ["10:00 AM"]}


@pytest.mark.anyio
async def test_n8n_retry_exhausts_after_max_retries(monkeypatch):
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json={"error": "still down"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        monkeypatch.setattr(n8n_client, "get_client", lambda: c)
        monkeypatch.setattr(
            n8n_client.settings, "n8n_webhook_url", "https://n8n.example.com/webhook"
        )
        monkeypatch.setattr(n8n_client.settings, "n8n_max_retries", 1)
        with pytest.raises(N8NError, match="HTTP 503"):
            await n8n_client.call_n8n_webhook({"date": "2026-09-10"})

    assert attempts["n"] == 2