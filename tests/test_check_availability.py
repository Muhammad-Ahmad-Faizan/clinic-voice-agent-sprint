"""Endpoint tests for POST /tools/check-availability (n8n mocked out)."""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import n8n_client
from app.services.n8n_client import N8NError

client = TestClient(app)

AVAILABILITY_PAYLOAD = {
    "message": {
        "type": "tool-calls",
        "toolCalls": [
            {
                "id": "toolu_01DTPAzUm5Gk3zxrpJ969oMF",
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

FALLBACK_MESSAGE = (
    "I'm having trouble checking availability right now, "
    "let me take your number and have someone call you back"
)


def test_check_availability_success_shape():
    mocked = AsyncMock(return_value={"available_slots": ["10:00 AM", "2:30 PM"]})
    with patch("app.services.n8n_client.get_availability", mocked):
        response = client.post("/tools/check-availability", json=AVAILABILITY_PAYLOAD)

    assert response.status_code == 200
    assert response.json() == {
        "results": [
            {
                "toolCallId": "toolu_01DTPAzUm5Gk3zxrpJ969oMF",
                "result": "Open slots: 10:00 AM, 2:30 PM",
                "error": None,
            }
        ]
    }
    mocked.assert_awaited_once_with("2026-09-10", None)


def test_check_availability_service_type_optional():
    payload = {
        "message": {
            "toolCalls": [
                {
                    "id": "toolu_02",
                    "function": {
                        "name": "checkAvailability",
                        "parameters": {"date": "2026-09-12"},
                    },
                }
            ]
        }
    }
    mocked = AsyncMock(return_value={"available_slots": ["9:00 AM"]})
    with patch("app.services.n8n_client.get_availability", mocked):
        response = client.post("/tools/check-availability", json=payload)

    assert response.status_code == 200
    mocked.assert_awaited_once_with("2026-09-12", None)
    assert response.json()["results"][0]["result"] == "Open slots: 9:00 AM"


def test_check_availability_no_slots_message():
    mocked = AsyncMock(return_value={"available_slots": []})
    with patch("app.services.n8n_client.get_availability", mocked):
        response = client.post("/tools/check-availability", json=AVAILABILITY_PAYLOAD)

    assert response.status_code == 200
    assert "no open slots" in response.json()["results"][0]["result"]


def test_check_availability_n8n_error_fallback():
    mocked = AsyncMock(side_effect=N8NError("n8n webhook request failed"))
    with patch("app.services.n8n_client.get_availability", mocked):
        response = client.post("/tools/check-availability", json=AVAILABILITY_PAYLOAD)

    assert response.status_code == 200
    assert response.json()["results"][0]["result"] == FALLBACK_MESSAGE


def test_check_availability_invalid_args_graceful():
    payload = {
        "message": {
            "toolCalls": [
                {
                    "id": "toolu_03",
                    "function": {
                        "name": "checkAvailability",
                        "parameters": {"service_type": "cleaning"},  # missing date
                    },
                }
            ]
        }
    }
    response = client.post("/tools/check-availability", json=payload)

    assert response.status_code == 200
    assert "didn't catch the date" in response.json()["results"][0]["result"]


def test_check_availability_multiple_calls_one_result_each():
    payload = {
        "message": {
            "toolCalls": [
                {
                    "id": "toolu_10",
                    "function": {
                        "name": "checkAvailability",
                        "parameters": {"date": "2026-09-10"},
                    },
                },
                {
                    "id": "toolu_11",
                    "function": {
                        "name": "checkAvailability",
                        "parameters": {"date": "2026-09-11"},
                    },
                },
            ]
        }
    }
    mocked = AsyncMock(
        side_effect=[
            {"available_slots": ["10:00 AM"]},
            {"available_slots": ["2:30 PM"]},
        ]
    )
    with patch("app.services.n8n_client.get_availability", mocked):
        response = client.post("/tools/check-availability", json=payload)

    results = response.json()["results"]
    assert [r["toolCallId"] for r in results] == ["toolu_10", "toolu_11"]
    assert results[1]["result"] == "Open slots: 2:30 PM"
    assert mocked.await_count == 2


class _SlowWebhookHandler(BaseHTTPRequestHandler):
    """Deliberately delays its reply so the client's read timeout fires."""

    def do_POST(self) -> None:  # noqa: N802 (http.server method naming)
        time.sleep(1)
        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, format: str, *args) -> None:
        pass


@pytest.mark.anyio
async def test_call_n8n_webhook_timeout_becomes_n8n_error(monkeypatch):
    """An httpx timeout must surface as N8NError so endpoints can catch it."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowWebhookHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with httpx.AsyncClient(timeout=0.1) as slow_client:
            monkeypatch.setattr(n8n_client, "get_client", lambda: slow_client)
            monkeypatch.setattr(
                n8n_client.settings,
                "n8n_webhook_url",
                f"http://127.0.0.1:{port}/webhook",
            )
            with pytest.raises(N8NError, match="ReadTimeout"):
                await n8n_client.call_n8n_webhook(
                    {"date": "2026-09-12"}
                )
    finally:
        server.shutdown()
        server.server_close()