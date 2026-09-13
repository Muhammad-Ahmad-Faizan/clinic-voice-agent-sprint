"""POST /tools/booking — unified intent-based booking decision endpoint.

Receives a normalized booking request from Vapi (flat body matching
`BookingRequest`), validates it up-front, and dispatches on `intent`:

  - book       → creates the appointment via n8n (Google Calendar)
  - cancel     → cancels via n8n
  - reschedule → moves the appointment via the n8n reschedule webhook
  - unclear    → asks the caller to repeat

Malformed payloads (missing `call_id`, unknown `intent`, extra fields) are
rejected by Pydantic with a clean 422 before any business logic runs.

Every outbound n8n/calendar call is wrapped in try/except: on failure the
endpoint still answers 200 with a spoken-friendly fallback `message` (the
voice agent relays it and offers a human callback) — a raw exception or
stack trace never reaches the caller. Every decision (booked, cancelled,
escalated, rejected, failed) is appended to the structured decision log
(see app/decision_log.py).

Mounted under the /tools prefix (see app/main.py) and protected by the same
`x-vapi-secret` header check as the other tool endpoints.
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, Request

from app.dependencies import verify_vapi_secret
from app.decision_log import log_decision
from app.models import BookingRequest, BookingResponse
from app.services import n8n_client
from app.services.n8n_client import N8NError, RescheduleConflictError

logger = logging.getLogger(__name__)



def _temporary_debug_verify_vapi_secret(
    request: Request, x_vapi_secret: str | None = Header(default=None)
) -> None:
    # TEMPORARY DEBUG LOGGING: remove this wrapper once header diagnostics are complete.
    logger.info("TEMPORARY DEBUG LOGGING: /tools/booking headers=%s", dict(request.headers))
    verify_vapi_secret(x_vapi_secret)


router = APIRouter(
    tags=["booking"], dependencies=[Depends(_temporary_debug_verify_vapi_secret)]
)

# Spoken-friendly fallback whenever the calendar backend (n8n) can't be
# reached — the voice agent relays it, takes the caller's number, and someone
# calls back.
HUMAN_CALLBACK_FALLBACK = (
    "I'm having trouble checking availability right now, let me take your "
    "number and have someone call you back"
)
COLLECT_DETAILS_MESSAGE = (
    "I need your name, the date, and a preferred time to book that. "
    "Could you repeat them for me?"
)
MISSING_REFERENCE_MESSAGE = (
    "Sorry, I didn't catch your booking reference. Could you repeat it?"
)
CLARIFY_INTENT_MESSAGE = (
    "Sorry, I didn't quite catch that. Could you tell me whether you'd like "
    "to book, reschedule, or cancel an appointment?"
)
RESCHEDULE_DETAILS_MESSAGE = (
    "To move your appointment I need your booking reference plus the new "
    "date and time. Could you repeat them for me?"
)
NO_ALTERNATIVES_MESSAGE = (
    "That time isn't available. Could you give me a different day or time "
    "so I can find an opening for you?"
)


@router.post("/booking", response_model=BookingResponse)
async def booking(request: BookingRequest) -> BookingResponse:
    """Validate and dispatch a normalized booking decision.

    Pydantic has already validated the payload by the time this handler runs
    (a missing/empty `call_id`, an unknown `intent`, or any unrecognized
    field → clean 422). This handler only ever executes on a well-formed
    request, and every n8n call is guarded so a downed calendar becomes a
    graceful `message`, never an exception on the wire.
    """
    if request.intent == "book":
        return await _handle_book(request)
    if request.intent == "cancel":
        return await _handle_cancel(request)
    if request.intent == "reschedule":
        return await _handle_reschedule(request)
    return _handle_unclear(request)


async def _handle_book(request: BookingRequest) -> BookingResponse:
    """Book an appointment via n8n; never raises past this helper."""
    if not (request.patient_name and request.requested_date and request.requested_time):
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="book",
            action_taken="book_rejected",
            outcome="failure",
            notes="missing patient_name/requested_date/requested_time",
        )
        return BookingResponse(
            call_id=request.call_id,
            message=COLLECT_DETAILS_MESSAGE,
            action_taken="book_rejected",
        )

    try:
        booking_reference = await n8n_client.create_booking(
            request.patient_name,
            "",  # normalized schema has no phone field; name/date/time are enough
            request.requested_date,
            request.requested_time,
            request.reason or "Appointment",
            call_id=request.call_id,
        )
    except N8NError as exc:
        logger.error("booking: n8n failed for call %s: %s", request.call_id, exc)
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="book",
            action_taken="book",
            outcome="failure",
            notes=f"n8n/calendar unavailable: {exc}",
        )
        return BookingResponse(
            call_id=request.call_id,
            message=HUMAN_CALLBACK_FALLBACK,
            action_taken="book",
        )
    except Exception:
        # Safety net: a mid-call bug must never leak a stack trace to Vapi.
        logger.exception("booking: unexpected error for call %s", request.call_id)
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="book",
            action_taken="book",
            outcome="failure",
            notes="unexpected internal error",
        )
        return BookingResponse(
            call_id=request.call_id,
            message=HUMAN_CALLBACK_FALLBACK,
            action_taken="book",
        )

    confirmation = (
        f"Great news, {request.patient_name}! Your "
        f"{request.reason or 'appointment'} on {request.requested_date} "
        f"at {request.requested_time} is confirmed. Your booking reference "
        f"is {booking_reference}."
    )
    log_decision(
        call_id=request.call_id,
        test_case_tag=request.test_case_tag,
        intent_detected="book",
        action_taken="book",
        outcome="success",
        notes=(
            f"patient={request.patient_name} date={request.requested_date} "
            f"time={request.requested_time} booking_reference={booking_reference}"
        ),
    )
    return BookingResponse(
        call_id=request.call_id,
        message=confirmation,
        action_taken="book",
        booking_reference=booking_reference,
    )


async def _handle_cancel(request: BookingRequest) -> BookingResponse:
    """Cancel an appointment via n8n; never raises past this helper."""
    if not request.existing_appointment_id:
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="cancel",
            action_taken="cancel_rejected",
            outcome="failure",
            notes="missing existing_appointment_id",
        )
        return BookingResponse(
            call_id=request.call_id,
            message=MISSING_REFERENCE_MESSAGE,
            action_taken="cancel_rejected",
        )

    try:
        await n8n_client.cancel_booking(request.existing_appointment_id)
    except N8NError as exc:
        logger.error("booking: n8n cancel failed for call %s: %s", request.call_id, exc)
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="cancel",
            action_taken="cancel",
            outcome="failure",
            notes=f"n8n/calendar unavailable: {exc}",
        )
        return BookingResponse(
            call_id=request.call_id,
            message=HUMAN_CALLBACK_FALLBACK,
            action_taken="cancel",
        )
    except Exception:
        logger.exception("booking: unexpected cancel error for call %s", request.call_id)
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="cancel",
            action_taken="cancel",
            outcome="failure",
            notes="unexpected internal error",
        )
        return BookingResponse(
            call_id=request.call_id,
            message=HUMAN_CALLBACK_FALLBACK,
            action_taken="cancel",
        )

    log_decision(
        call_id=request.call_id,
        test_case_tag=request.test_case_tag,
        intent_detected="cancel",
        action_taken="cancel",
        outcome="success",
        notes=f"existing_appointment_id={request.existing_appointment_id}",
    )
    return BookingResponse(
        call_id=request.call_id,
        message=(
            f"Your appointment {request.existing_appointment_id} has been "
            "cancelled. Is there anything else I can help you with?"
        ),
        action_taken="cancel",
    )


async def _handle_reschedule(request: BookingRequest) -> BookingResponse:
    """Move an appointment to a new date/time via the n8n reschedule webhook.

    Mirrors the book/cancel patterns: missing fields → clarifying question;
    ``{"status": "conflict"}`` → offer alternative slots; any failure → the
    Phase-3 spoken-friendly fallback. Never raises past this helper.
    """
    if not (
        request.existing_appointment_id
        and request.requested_date
        and request.requested_time
    ):
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="reschedule",
            action_taken="reschedule_rejected",
            outcome="failure",
            notes="missing existing_appointment_id/requested_date/requested_time",
        )
        return BookingResponse(
            call_id=request.call_id,
            message=RESCHEDULE_DETAILS_MESSAGE,
            action_taken="reschedule_rejected",
        )

    try:
        appointment_id = await n8n_client.reschedule_booking(
            request.existing_appointment_id,
            request.requested_date,
            request.requested_time,
        )
    except RescheduleConflictError as exc:
        alternative_message = await _offer_alternative_slots(request.requested_date)
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="reschedule",
            action_taken="reschedule_conflict",
            outcome="failure",
            notes=(
                f"appointment={request.existing_appointment_id} "
                f"new_date={request.requested_date} new_time={request.requested_time} "
                f"new slot not free: {exc}"
            ),
        )
        return BookingResponse(
            call_id=request.call_id,
            message=alternative_message,
            action_taken="reschedule_conflict",
        )
    except N8NError as exc:
        logger.error("booking: n8n reschedule failed for call %s: %s", request.call_id, exc)
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="reschedule",
            action_taken="reschedule",
            outcome="failure",
            notes=f"n8n/calendar unavailable: {exc}",
        )
        return BookingResponse(
            call_id=request.call_id,
            message=HUMAN_CALLBACK_FALLBACK,
            action_taken="reschedule",
        )
    except Exception:
        # Safety net: a mid-call bug must never leak a stack trace to Vapi.
        logger.exception("booking: unexpected reschedule error for call %s", request.call_id)
        log_decision(
            call_id=request.call_id,
            test_case_tag=request.test_case_tag,
            intent_detected="reschedule",
            action_taken="reschedule",
            outcome="failure",
            notes="unexpected internal error",
        )
        return BookingResponse(
            call_id=request.call_id,
            message=HUMAN_CALLBACK_FALLBACK,
            action_taken="reschedule",
        )

    log_decision(
        call_id=request.call_id,
        test_case_tag=request.test_case_tag,
        intent_detected="reschedule",
        action_taken="rescheduled",
        outcome="success",
        notes=(
            f"appointment={request.existing_appointment_id} "
            f"new_date={request.requested_date} new_time={request.requested_time} "
            f"appointment_id={appointment_id}"
        ),
    )
    return BookingResponse(
        call_id=request.call_id,
        message=(
            f"Great news! Your appointment {request.existing_appointment_id} "
            f"has been moved to {request.requested_date} at "
            f"{request.requested_time}."
        ),
        action_taken="rescheduled",
        booking_reference=appointment_id,
    )


async def _offer_alternative_slots(
    requested_date: str, max_alternatives: int = 3
) -> str:
    """Offer up to 2-3 open slots near the requested date.

    Reuses the availability path (`n8n_client.get_availability` — the same
    source the check-availability tool uses): queries the requested date
    first, then the next two calendar days, and lists the first
    `max_alternatives` open slots. Returns a generic prompt if nothing is
    found or the availability backend can't be reached.
    """
    dates_to_try = [requested_date]
    try:
        base = datetime.strptime(requested_date, "%Y-%m-%d").date()
        dates_to_try.extend(
            (base + timedelta(days=offset)).isoformat() for offset in (1, 2)
        )
    except ValueError:
        # Unparseable date from the caller — only try the date as given.
        pass

    alternatives: list[str] = []
    for day in dates_to_try:
        if len(alternatives) >= max_alternatives:
            break
        try:
            data = await n8n_client.get_availability(day, None)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for slot in data.get("alternative_slots") or data.get("available_slots") or []:
            alternatives.append(f"{day} at {slot}")
            if len(alternatives) >= max_alternatives:
                break

    if not alternatives:
        return NO_ALTERNATIVES_MESSAGE
    return (
        "That time isn't available, but I do have some other openings: "
        + "; ".join(alternatives)
        + ". Would any of those work for you?"
    )


def _handle_unclear(request: BookingRequest) -> BookingResponse:
    """What the caller said didn't map to an intent — ask them to repeat."""
    log_decision(
        call_id=request.call_id,
        test_case_tag=request.test_case_tag,
        intent_detected="unclear",
        action_taken="ask_clarification",
        outcome="escalated",
        notes="no clear booking intent in the request",
    )
    return BookingResponse(
        call_id=request.call_id,
        message=CLARIFY_INTENT_MESSAGE,
        action_taken="ask_clarification",
    )