"""Structured decision logging: one entry per decision point.

Entries are stored in a Postgres-backed `decision_log` table when
DATABASE_URL is configured (the standard env var Supabase / Neon / Render
Postgres all export), and fall back to the JSON-lines file
(DECISION_LOG_PATH) otherwise — local dev without a database still works.

The public API (`log_decision`, `read_entries`) is identical regardless of
the backend, so no call site needs to know which store is active. The
dashboard (GET /dashboard) reads the same source.

One entry is written per decision point — not only on errors — so a review
can replay exactly what the agent did for every call: booked, cancelled,
rescheduled, escalated, or failed.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from app.config import settings

logger = logging.getLogger(__name__)

Outcome = Literal["success", "failure", "escalated"]

# ---------------------------------------------------------------------------
# Postgres store (SQLAlchemy, optional).
# Imported lazily so the app still runs in an environment without SQLAlchemy
# installed (the store degrades to the JSON-lines file).
# ---------------------------------------------------------------------------

try:
    from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
    from sqlalchemy.orm import declarative_base, sessionmaker

    _SQLALCHEMY_AVAILABLE = True

    Base = declarative_base()

    class DecisionLogRecord(Base):
        """One structured decision entry in the `decision_log` table."""

        __tablename__ = "decision_log"

        id = Column(Integer, primary_key=True, autoincrement=True)
        call_id = Column(String(255), nullable=False)
        timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
        test_case_tag = Column(String(255), nullable=True)
        intent_detected = Column(String(64), nullable=False)
        action_taken = Column(String(64), nullable=False)
        outcome = Column(String(16), nullable=False)
        notes = Column(Text, nullable=False, default="")

except ImportError:
    _SQLALCHEMY_AVAILABLE = False
    logger.warning(
        "SQLAlchemy is not installed — the decision log will use the "
        "JSON-lines file even if DATABASE_URL is set"
    )


# Engines / session makers are cached per connection-string so the table is
# created (and the pool built) exactly once per database.
_engines: dict[str, Any] = {}
_session_makers: dict[str, Any] = {}


def _normalized_url(url: str) -> str:
    """Map plain postgres:// URLs to the psycopg3 dialect SQLAlchemy expects."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _database_enabled() -> bool:
    """True when DATABASE_URL is set *and* SQLAlchemy is importable."""
    return bool(settings.database_url) and _SQLALCHEMY_AVAILABLE


def _session_maker() -> Any:
    """Create (once per URL) and return a sessionmaker bound to the DB."""
    url = _normalized_url(settings.database_url)
    if url not in _session_makers:
        engine = create_engine(url, pool_pre_ping=True)
        # Demo-friendly: auto-create the table on first use (no migration step).
        Base.metadata.create_all(engine)
        _engines[url] = engine
        _session_makers[url] = sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False
        )
    return _session_makers[url]


def _iso(value: datetime) -> str:
    """UTC offset-aware ISO-8601 string (the dashboard sorts on this)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _record_to_dict(record: Any) -> dict[str, Any]:
    """Map an ORM row to the dict shape the JSON-lines path used."""
    return {
        "call_id": record.call_id,
        "timestamp": _iso(record.timestamp),
        "test_case_tag": record.test_case_tag,
        "intent_detected": record.intent_detected,
        "action_taken": record.action_taken,
        "outcome": record.outcome,
        "notes": record.notes or "",
    }


def log_decision(
    *,
    call_id: str,
    intent_detected: str,
    action_taken: str,
    outcome: Outcome,
    notes: str = "",
    test_case_tag: str | None = None,
) -> None:
    """Append one structured decision entry to the active store.

    Writes to the `decision_log` table when DATABASE_URL is configured, and
    to the JSON-lines file otherwise. A failure to persist is logged but
    never raised — the booking flow must not crash over telemetry.

    Args:
        call_id: The call / tool-call identifier this decision belongs to.
        intent_detected: The detected intent (book/reschedule/cancel/unclear).
        action_taken: The specific action the system performed for this
            decision (e.g. "book", "rescheduled", "escalate_to_human").
        outcome: One of "success", "failure", or "escalated".
        notes: Free-form context (booking reference, reason, latency, ...).
        test_case_tag: Optional harness label; stored as null when absent.
    """
    if _database_enabled():
        try:
            with _session_maker()() as session:
                session.add(
                    DecisionLogRecord(
                        call_id=call_id,
                        timestamp=datetime.now(timezone.utc),
                        test_case_tag=test_case_tag,
                        intent_detected=intent_detected,
                        action_taken=action_taken,
                        outcome=outcome,
                        notes=notes,
                    )
                )
                session.commit()
            return
        except Exception:
            logger.exception(
                "Failed to insert decision log entry into %s", settings.database_url
            )
            # Fall through to the file fallback so the entry isn't lost.

    path = Path(settings.decision_log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            entry = {
                "call_id": call_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "test_case_tag": test_case_tag,
                "intent_detected": intent_detected,
                "action_taken": action_taken,
                "outcome": outcome,
                "notes": notes,
            }
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("Failed to append decision log entry to %s", path)


def read_entries(limit: int = 50) -> list[dict[str, Any]]:
    """Read the most recent `limit` decision entries, newest first.

    Reads from the `decision_log` table when DATABASE_URL is configured, and
    from the JSON-lines file otherwise. Corrupt/partial file lines are skipped
    rather than failing the whole read; an unreachable database returns [] so
    the dashboard renders an empty table instead of erroring. A store that has
    nothing recorded yet is not an error.
    """
    if _database_enabled():
        try:
            with _session_maker()() as session:
                rows = (
                    session.query(DecisionLogRecord)
                    .order_by(DecisionLogRecord.id.desc())
                    .limit(limit)
                    .all()
                )
            # id desc == newest inserts first (stable even for identical
            # timestamps), matching the append-only file's newest-first order.
            return [_record_to_dict(row) for row in rows]
        except Exception:
            logger.exception(
                "Failed to read decision log from %s", settings.database_url
            )
            return []

    path = Path(settings.decision_log_path)
    if not path.exists():
        return []

    entries: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping unparseable decision log line in %s", path)
    except OSError:
        logger.exception("Failed to read decision log at %s", path)
        return []

    # The file is append-only, so later entries are at the end. Reverse the
    # newest `limit` so callers get most-recent-first ordering.
    return list(reversed(entries[-limit:]))