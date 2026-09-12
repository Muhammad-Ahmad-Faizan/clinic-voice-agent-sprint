"""Tool endpoints Vapi.ai calls when its LLM invokes a tool mid-call.

All routes are mounted under the /tools prefix (see app/main.py). Each
endpoint validates Vapi's tool-call payload, performs the action (normally by
forwarding to the n8n Google Calendar workflow), and replies in the exact shape
Vapi expects — fast, so the phone call never stalls.

- `verify_vapi_secret` protects every route on this router.
- `app.models` defines the request/response shapes Vapi expects.
- `app.services.n8n_client` wraps the outbound n8n calls.
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import ValidationError

from app.dependencies import verify_vapi_secret
from app.decision_log import log_decision
from app.models import (
    BookAppointmentArgs,
    CancelAppointmentArgs,
    CheckAvailabilityArgs,
    VapiToolCallRequest,
    VapiToolResponse,
    VapiToolResult,
    tool_result,
)
from app.services import n8n_client
from app.services.n8n_client import N8NError

logger = logging.getLogger(__name__)

# Graceful text fed back to the LLM instead of crashing; the bot relays it.
CALENDAR_UNAVAILABLE_MESSAGE = (
    "I'm having trouble checking availability right now, "
    "let me take your number and have someone call you back"
)
INVALID_DATE_MESSAGE = (
    "Sorry, I didn't catch the date you'd like to check. Could you repeat it?"
)
BOOKING_UNAVAILABLE_MESSAGE = (
    "I'm having trouble booking your appointment right now, "
    "let me take your number and have someone call you back."
)
INVALID_BOOKING_MESSAGE = (
    "Sorry, I didn't get all your booking details. "
    "Could you repeat your name, phone number, and preferred time?"
)
CANCEL_UNAVAILABLE_MESSAGE = (
    "I'm having trouble cancelling the appointment right now, "
    "let me take your number and have someone call you back."
)
INVALID_CANCEL_MESSAGE = (
    "Sorry, I didn't catch the booking reference. Could you repeat it?"
)

# In-memory idempotency store: maps Vapi's toolCallId -> cached booking result.
# When Vapi retries a tool call (e.g. after a network hiccup) the same
# toolCallId comes back, so we return the cached confirmation instead of
# booking a second appointment. A plain dict is fine for this single-process
# demo — production would use Redis/Postgres (shared across workers/instances,
# with a TTL). Note that `--reload` or multiple uvicorn workers reset/shard it.
BOOKING_STORE: dict[str, Any] = {}


router = APIRouter(prefix="", tags=["tools"], dependencies=[Depends(verify_vapi_secret)])


@router.post("/check-availability", response_model=VapiToolResponse)
async def check_availability(request: VapiToolCallRequest) -> VapiToolResponse:
    """Check open appointment slots via the n8n Google Calendar workflow.

    Vapi may include several tool calls in one payload (e.g. checking a few
    dates); each call gets its own `result` entry. Failures (invalid args,
    n8n timeouts/errors) never crash the endpoint — the LLM gets a graceful
    message it can relay to the caller.
    """
    results: list[VapiToolResult] = []
    started = time.perf_counter()

    for call in request.calls:
        call_started = time.perf_counter()
        try:
            args = CheckAvailabilityArgs.model_validate(call.function.parameters)
            data = await n8n_client.get_availability(args.date, args.service_type)

            if not isinstance(data, dict):
                logger.warning(
                    "check-availability: n8n returned non-object payload for call %s: %r",
                    call.id,
                    data,
                )
                data = {}

            slots = [str(slot) for slot in (data.get("available_slots") or [])]
            if slots:
                message = f"Open slots: {', '.join(slots)}"
            else:
                message = (
                    "There are no open slots that day. "
                    "Would you like to check another date?"
                )

            results.append(tool_result(call, message))
            log_decision(
                call_id=call.id,
                intent_detected="check_availability",
                action_taken="check_availability",
                outcome="success",
                notes=(
                    f"date={args.date} service_type={args.service_type or 'any'} "
                    f"open_slots={len(slots)} "
                    f"latency_ms={(time.perf_counter() - call_started) * 1000:.0f}"
                ),
            )
            logger.info(
                "check-availability: call=%s date=%s service_type=%s slots=%s "
                "latency=%.0fms",
                call.id,
                args.date,
                args.service_type or "any",
                slots,
                (time.perf_counter() - call_started) * 1000,
            )
        except ValidationError as exc:
            logger.warning("check-availability: invalid args for call %s: %s", call.id, exc)
            log_decision(
                call_id=call.id,
                intent_detected="check_availability",
                action_taken="check_availability_rejected",
                outcome="failure",
                notes=f"invalid request args: {exc}",
            )
            results.append(tool_result(call, INVALID_DATE_MESSAGE))
        except N8NError as exc:
            logger.error("check-availability: n8n failed for call %s: %s", call.id, exc)
            log_decision(
                call_id=call.id,
                intent_detected="check_availability",
                action_taken="check_availability",
                outcome="failure",
                notes=f"n8n/calendar unavailable: {exc}",
            )
            results.append(tool_result(call, CALENDAR_UNAVAILABLE_MESSAGE))
        except Exception:
            # Safety net: a mid-call bug must never turn into a 500 on a
            # webhook Vapi is waiting on.
            logger.exception("check-availability: unexpected error for call %s", call.id)
            log_decision(
                call_id=call.id,
                intent_detected="check_availability",
                action_taken="check_availability",
                outcome="failure",
                notes="unexpected internal error",
            )
            results.append(tool_result(call, CALENDAR_UNAVAILABLE_MESSAGE))

    total_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "check-availability: handled %d tool call(s) in %.0f ms", len(results), total_ms
    )
    return VapiToolResponse(results=results)


@router.post("/book-appointment", response_model=VapiToolResponse)
async def book_appointment(request: VapiToolCallRequest) -> VapiToolResponse:
    """Book an appointment via the n8n Google Calendar workflow.

    Vapi's toolCallId is used as an idempotency key: if this exact tool call
    was already processed, the cached confirmation is returned and n8n is not
    called again — no double-booking on retries. Failures never crash the
    endpoint; the LLM gets a graceful message it can relay to the caller.
    """
    results: list[VapiToolResult] = []
    started = time.perf_counter()

    for call in request.calls:
        call_started = time.perf_counter()

        cached = BOOKING_STORE.get(call.id)
        if cached is not None:
            reference = (
                cached.get("booking_reference")
                if isinstance(cached, dict)
                else None
            )
            logger.info(
                "book-appointment: cache hit call=%s reference=%s (retried tool call)",
                call.id,
                reference,
            )
            log_decision(
                call_id=call.id,
                intent_detected="book",
                action_taken="book_cached",
                outcome="success",
                notes=f"served from idempotency cache, booking_reference={reference}",
            )
            results.append(tool_result(call, cached))
            continue

        try:
            args = BookAppointmentArgs.model_validate(call.function.parameters)
        except ValidationError as exc:
            logger.warning("book-appointment: invalid args for call %s: %s", call.id, exc)
            log_decision(
                call_id=call.id,
                intent_detected="book",
                action_taken="book_rejected",
                outcome="failure",
                notes=f"invalid request args: {exc}",
            )
            results.append(tool_result(call, INVALID_BOOKING_MESSAGE))
            continue

        try:
            booking_reference = await n8n_client.create_booking(
                args.patient_name,
                args.phone_number,
                args.date,
                args.time,
                args.service_type,
            )
            confirmation = {
                "booking_reference": booking_reference,
                "confirmation": (
                    f"Great news, {args.patient_name}! Your {args.service_type} "
                    f"appointment on {args.date} at {args.time} is confirmed. "
                    f"Your booking reference is {booking_reference}."
                ),
            }
            # Success is what we cache — a failed attempt must NOT mark the
            # toolCallId as done, so a retry actually retries.
            BOOKING_STORE[call.id] = confirmation
            results.append(tool_result(call, confirmation))
            log_decision(
                call_id=call.id,
                intent_detected="book",
                action_taken="book",
                outcome="success",
                notes=(
                    f"date={args.date} time={args.time} service_type={args.service_type} "
                    f"booking_reference={booking_reference} "
                    f"latency_ms={(time.perf_counter() - call_started) * 1000:.0f}"
                ),
            )
            logger.info(
                "book-appointment: confirmed call=%s date=%s time=%s "
                "service_type=%s reference=%s latency=%.0fms",
                call.id,
                args.date,
                args.time,
                args.service_type,
                booking_reference,
                (time.perf_counter() - call_started) * 1000,
            )
        except N8NError as exc:
            logger.error("book-appointment: n8n failed for call %s: %s", call.id, exc)
            log_decision(
                call_id=call.id,
                intent_detected="book",
                action_taken="book",
                outcome="failure",
                notes=f"n8n/calendar unavailable: {exc}",
            )
            results.append(tool_result(call, BOOKING_UNAVAILABLE_MESSAGE))
        except Exception:
            # Safety net: a mid-call bug must never turn into a 500 on a
            # webhook Vapi is waiting on.
            logger.exception("book-appointment: unexpected error for call %s", call.id)
            log_decision(
                call_id=call.id,
                intent_detected="book",
                action_taken="book",
                outcome="failure",
                notes="unexpected internal error",
            )
            results.append(tool_result(call, BOOKING_UNAVAILABLE_MESSAGE))

    total_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "book-appointment: handled %d tool call(s) in %.0f ms", len(results), total_ms
    )
    return VapiToolResponse(results=results)


@router.post("/cancel-appointment", response_model=VapiToolResponse)
async def cancel_appointment(request: VapiToolCallRequest) -> VapiToolResponse:
    """Cancel an appointment via the n8n Google Calendar workflow.

    Parses `CancelAppointmentArgs`, forwards the booking_reference to n8n's
    cancel-appointment workflow, and replies with a friendly confirmation.
    Failures never crash the endpoint — the LLM gets a graceful message.
    """
    results: list[VapiToolResult] = []
    started = time.perf_counter()

    for call in request.calls:
        call_started = time.perf_counter()
        try:
            args = CancelAppointmentArgs.model_validate(call.function.parameters)
        except ValidationError as exc:
            logger.warning("cancel-appointment: invalid args for call %s: %s", call.id, exc)
            log_decision(
                call_id=call.id,
                intent_detected="cancel",
                action_taken="cancel_rejected",
                outcome="failure",
                notes=f"invalid request args: {exc}",
            )
            results.append(tool_result(call, INVALID_CANCEL_MESSAGE))
            continue

        try:
            await n8n_client.cancel_booking(args.booking_reference)
            confirmation = (
                f"Your appointment {args.booking_reference} has been cancelled. "
                "Is there anything else I can help you with?"
            )
            results.append(tool_result(call, confirmation))
            log_decision(
                call_id=call.id,
                intent_detected="cancel",
                action_taken="cancel",
                outcome="success",
                notes=(
                    f"booking_reference={args.booking_reference} "
                    f"latency_ms={(time.perf_counter() - call_started) * 1000:.0f}"
                ),
            )
            logger.info(
                "cancel-appointment: cancelled call=%s reference=%s latency=%.0fms",
                call.id,
                args.booking_reference,
                (time.perf_counter() - call_started) * 1000,
            )
        except N8NError as exc:
            logger.error("cancel-appointment: n8n failed for call %s: %s", call.id, exc)
            log_decision(
                call_id=call.id,
                intent_detected="cancel",
                action_taken="cancel",
                outcome="failure",
                notes=f"n8n/calendar unavailable: {exc}",
            )
            results.append(tool_result(call, CANCEL_UNAVAILABLE_MESSAGE))
        except Exception:
            # Safety net: a mid-call bug must never turn into a 500 on a
            # webhook Vapi is waiting on.
            logger.exception("cancel-appointment: unexpected error for call %s", call.id)
            log_decision(
                call_id=call.id,
                intent_detected="cancel",
                action_taken="cancel",
                outcome="failure",
                notes="unexpected internal error",
            )
            results.append(tool_result(call, CANCEL_UNAVAILABLE_MESSAGE))

    total_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "cancel-appointment: handled %d tool call(s) in %.0f ms", len(results), total_ms
    )
    return VapiToolResponse(results=results)
