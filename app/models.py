"""Pydantic models for Vapi.ai tool-call requests and responses.

When Vapi's LLM decides to invoke a tool, Vapi POSTs a `message` envelope
containing the requested tool calls (function name + arguments), and expects
a `results` list back — one entry per tool call, keyed by Vapi's `toolCallId`.

Wire format (from Vapi's function-calling docs):

    {
      "message": {
        "type": "tool-calls",
        "toolCalls": [
          {
            "id": "toolu_...",
            "type": "function",
            "function": {
              "name": "checkAvailability",
              "parameters": { "date": "2026-09-10", ... }
            }
          }
        ]
      }
    }
"""

import json
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class ToolCallFunction(BaseModel):
    """Function details of a single tool call."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("parameters", "arguments"),
        serialization_alias="parameters",
    )

    @field_validator("parameters", mode="before")
    @classmethod
    def _parse_json_arguments(cls, value: Any) -> Any:
        """Also accept OpenAI-style `arguments` given as a JSON string."""
        if isinstance(value, str):
            try:
                return json.loads(value) if value else {}
            except json.JSONDecodeError:
                return {"raw": value}
        return value


class ToolCall(BaseModel):
    """A single tool invocation requested by Vapi mid-conversation."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(
        validation_alias=AliasChoices("id", "toolCallId"),
        serialization_alias="toolCallId",
    )
    type: str = "function"
    function: ToolCallFunction | None = None


class VapiMessage(BaseModel):
    """The `message` envelope Vapi sends with tool-call requests.

    Some Vapi payload versions use `toolCallList` instead of `toolCalls`,
    so both field names are accepted.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str | None = None
    tool_calls: list[ToolCall] = Field(
        default_factory=list,
        validation_alias=AliasChoices("toolCalls", "toolCallList"),
        serialization_alias="toolCalls",
    )


class VapiToolCallRequest(BaseModel):
    """Full request body Vapi POSTs to a tool endpoint.

    Parses the `message.toolCalls` envelope and exposes each call's
    `toolCallId`, function name and arguments dict via `calls`.

    Extra top-level fields (assistant, call, chat, ...) are allowed and
    ignored so the models stay forward-compatible with Vapi's payload.
    """

    model_config = ConfigDict(extra="allow")

    message: VapiMessage

    @property
    def calls(self) -> list[ToolCall]:
        """Shortcut to the extracted tool calls passed by Vapi."""
        return self.message.tool_calls


# ---------------------------------------------------------------------------
# Per-tool argument models.
#
# These mirror the JSON schema you declare on each Vapi function tool. Extra
# keys are rejected (`extra="forbid"`) so a mismatch between the Vapi tool
# schema and this code fails loudly on the very first call.
# ---------------------------------------------------------------------------


class CheckAvailabilityArgs(BaseModel):
    """Arguments for the `checkAvailability` tool."""

    model_config = ConfigDict(extra="forbid")

    date: str
    service_type: str | None = None
    preferred_time_range: str | None = None


class BookAppointmentArgs(BaseModel):
    """Arguments for the `bookAppointment` tool."""

    model_config = ConfigDict(extra="forbid")

    patient_name: str
    phone_number: str
    date: str
    time: str
    service_type: str


class CancelAppointmentArgs(BaseModel):
    """Arguments for the `cancelAppointment` tool."""

    model_config = ConfigDict(extra="forbid")

    booking_reference: str


# ---------------------------------------------------------------------------
# Response models — Vapi expects a `results` array with one entry per tool
# call, each keyed by `toolCallId` and carrying `result` (string or object).
# ---------------------------------------------------------------------------


class VapiToolResult(BaseModel):
    """Outcome of one tool call. Set exactly one of `result` / `error`."""

    model_config = ConfigDict(populate_by_name=True)

    tool_call_id: str = Field(serialization_alias="toolCallId")
    # `result` may be any JSON value: a string ("Open slots: 9:00, 10:30") or
    # an object ({"available": true, ...}) that Vapi reads back into the LLM.
    result: Any = None
    error: str | None = None


class VapiToolResponse(BaseModel):
    """Response body Vapi expects: one result entry per requested tool call."""

    results: list[VapiToolResult] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Shape of the GET /health response."""

    status: Literal["ok"] = "ok"


# ---------------------------------------------------------------------------
# End-of-call webhook models — Vapi POSTs one of these to /webhook/call-ended
# when a call finishes and post-processing is complete. This endpoint only
# logs, so the models are intentionally permissive.
# ---------------------------------------------------------------------------


class CallReport(BaseModel):
    """The (legacy, nested) `callReport` Vapi used to send on call end."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    ended_reason: str | None = Field(
        default=None, validation_alias=AliasChoices("endedReason", "ended_reason")
    )
    summary: str | None = None
    transcript: Any = None


class EndOfCallMessage(BaseModel):
    """Vapi's `end-of-call-report` server message.

    Covers both payload generations Vapi has shipped:
      - legacy: report fields nested under `callReport`;
      - current (Fern SDK): fields flat on the message with the summary under
        `artifact` / `analysis`.
    `extra="allow"` keeps us tolerant of whatever Vapi adds next.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: str | None = None
    ended_reason: str | None = Field(
        default=None, validation_alias=AliasChoices("endedReason", "ended_reason")
    )
    call_report: CallReport | None = Field(
        default=None, validation_alias=AliasChoices("callReport", "endOfCallReport")
    )
    artifact: dict[str, Any] | None = None
    analysis: dict[str, Any] | None = None
    call: dict[str, Any] | None = None


class CallEndedWebhookRequest(BaseModel):
    """Body Vapi POSTs to /webhook/call-ended when a call finishes."""

    model_config = ConfigDict(extra="allow")

    message: EndOfCallMessage | None = None


# Backwards-compatible aliases used before the generic naming landed.
ToolCallResult = VapiToolResult
ToolCallResponse = VapiToolResponse


def tool_result(tool_call: ToolCall, result: Any) -> VapiToolResult:
    """Build a success entry for `tool_call` (keeps endpoint code one-liners)."""
    return VapiToolResult(tool_call_id=tool_call.id, result=result)


def tool_error(tool_call: ToolCall, error: str) -> VapiToolResult:
    """Build a failure entry for `tool_call`; Vapi reads `error` back into the LLM."""
    return VapiToolResult(tool_call_id=tool_call.id, error=error)
