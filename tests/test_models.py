"""Quick pytest suite for the Vapi tool-call models.

Run from the project root with:

    .venv\\Scripts\\python.exe -m pytest -q
"""

import pytest
from pydantic import ValidationError

from app.models import (
    BookAppointmentArgs,
    CancelAppointmentArgs,
    CheckAvailabilityArgs,
    VapiToolCallRequest,
    VapiToolResponse,
    VapiToolResult,
    tool_error,
    tool_result,
)

# Sample payload matching Vapi's function-calling webhook wire format.
SAMPLE_AVAILABILITY_PAYLOAD = {
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
                        "preferred_time_range": "morning",
                    },
                },
            }
        ],
    }
}


# --- VapiToolCallRequest ------------------------------------------------------


def test_request_extracts_tool_call_id_name_and_arguments():
    request = VapiToolCallRequest.model_validate(SAMPLE_AVAILABILITY_PAYLOAD)
    call = request.calls[0]
    assert call.id == "toolu_01DTPAzUm5Gk3zxrpJ969oMF"
    assert call.type == "function"
    assert call.function.name == "checkAvailability"

    args = CheckAvailabilityArgs.model_validate(call.function.parameters)
    assert args.date == "2026-09-10"
    assert args.service_type == "cleaning"
    assert args.preferred_time_range == "morning"


def test_request_accepts_legacy_tool_call_list_field():
    payload = {
        "message": {
            "type": "tool-calls",
            "toolCallList": SAMPLE_AVAILABILITY_PAYLOAD["message"]["toolCalls"],
        }
    }
    request = VapiToolCallRequest.model_validate(payload)
    assert len(request.calls) == 1
    assert request.calls[0].function.name == "checkAvailability"


def test_request_accepts_openai_style_arguments_string():
    payload = {
        "message": {
            "toolCalls": [
                {
                    "id": "call_2",
                    "function": {
                        "name": "checkAvailability",
                        "arguments": '{"date": "2026-09-11"}',
                    },
                }
            ]
        }
    }
    request = VapiToolCallRequest.model_validate(payload)
    args = CheckAvailabilityArgs.model_validate(request.calls[0].function.parameters)
    assert args.date == "2026-09-11"
    assert args.service_type is None
    assert args.preferred_time_range is None


# --- Per-tool argument models --------------------------------------------------


def test_check_availability_optional_fields_default_to_none():
    args = CheckAvailabilityArgs.model_validate({"date": "2026-09-12"})
    assert args.date == "2026-09-12"
    assert args.service_type is None
    assert args.preferred_time_range is None


def test_check_availability_requires_date():
    with pytest.raises(ValidationError):
        CheckAvailabilityArgs.model_validate({"service_type": "cleaning"})


def test_book_appointment_valid():
    args = BookAppointmentArgs.model_validate(
        {
            "patient_name": "Jane Doe",
            "phone_number": "+1-555-0100",
            "date": "2026-09-12",
            "time": "10:30",
            "service_type": "cleaning",
        }
    )
    assert args.patient_name == "Jane Doe"
    assert args.phone_number == "+1-555-0100"
    assert args.time == "10:30"


def test_book_appointment_requires_phone_number():
    with pytest.raises(ValidationError):
        BookAppointmentArgs.model_validate(
            {
                "patient_name": "Jane Doe",
                "date": "2026-09-12",
                "time": "10:30",
                "service_type": "cleaning",
            }
        )


def test_cancel_appointment_valid():
    args = CancelAppointmentArgs.model_validate({"booking_reference": "BK-2026-0001"})
    assert args.booking_reference == "BK-2026-0001"


def test_cancel_appointment_requires_reference():
    with pytest.raises(ValidationError):
        CancelAppointmentArgs.model_validate({})


# --- VapiToolResponse ---------------------------------------------------------


def test_response_string_result():
    response = VapiToolResponse(
        results=[VapiToolResult(tool_call_id="call_1", result="Open slots: 9:00, 10:30")]
    )
    data = response.model_dump(by_alias=True)
    assert data == {
        "results": [
            {"toolCallId": "call_1", "result": "Open slots: 9:00, 10:30", "error": None}
        ]
    }


def test_response_object_result():
    slots = {"available": True, "slots": ["9:00", "10:30"]}
    response = VapiToolResponse(results=[VapiToolResult(tool_call_id="call_2", result=slots)])
    data = response.model_dump(by_alias=True)
    assert data["results"][0]["toolCallId"] == "call_2"
    assert data["results"][0]["result"] == slots


def test_tool_result_helper_uses_vapi_tool_call_id():
    request = VapiToolCallRequest.model_validate(SAMPLE_AVAILABILITY_PAYLOAD)
    entry = tool_result(request.calls[0], "Open slots: 9:00, 10:30")
    assert entry.model_dump(by_alias=True)["toolCallId"] == "toolu_01DTPAzUm5Gk3zxrpJ969oMF"


def test_tool_error_helper():
    request = VapiToolCallRequest.model_validate(SAMPLE_AVAILABILITY_PAYLOAD)
    entry = tool_error(request.calls[0], "No slots available")
    data = entry.model_dump(by_alias=True)
    assert data["result"] is None
    assert data["error"] == "No slots available"