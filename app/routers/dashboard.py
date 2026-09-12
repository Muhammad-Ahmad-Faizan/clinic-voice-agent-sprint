"""GET /dashboard — minimal read-only decision-log viewer.

Serves a plain HTML page (no auth, no build step, no JS framework) that
renders the most recent decision-log entries as a table, newest first.
Data comes from the JSON Lines file written by `app/decision_log.py`
(`settings.decision_log_path`).
"""

import html
import logging
from string import Template

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.decision_log import read_entries

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

_PAGE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clinic Voice Agent — Booking decisions</title>
<style>
  body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 0; background: #f6f7f9; color: #1f2933; }
  main { max-width: 1150px; margin: 0 auto; padding: 28px 16px 56px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  p.sub { color: #52606d; margin: 0 0 20px; font-size: 14px; }
  table { width: 100%; border-collapse: collapse; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.08); border-radius: 8px; overflow: hidden; font-size: 13px; }
  th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid #e4e7eb; vertical-align: top; }
  th { background: #edeff2; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #3e4c59; }
  tr:last-child td { border-bottom: none; }
  .ts { white-space: nowrap; font-variant-numeric: tabular-nums; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .success { background: #e3f7ec; color: #147d3e; }
  .failure { background: #fde3e3; color: #b53737; }
  .escalated { background: #fdf1dc; color: #a1650a; }
  .empty { text-align: center; color: #616e7c; padding: 40px 0; }
</style>
</head>
<body>
<main>
  <h1>Booking decisions</h1>
  <p class="sub">Most recent $count entries (newest first). Refresh the page to update.</p>
  <table>
    <thead>
      <tr><th>Timestamp</th><th>Call ID</th><th>Intent</th><th>Action</th><th>Outcome</th><th>Tag</th><th>Notes</th></tr>
    </thead>
    <tbody>
$rows
    </tbody>
  </table>
</main>
</body>
</html>"""
)


def _cell(value: object | None) -> str:
    """One table cell; missing values become empty cells, everything is escaped."""
    if value is None or value == "":
        return "<td></td>"
    return f"<td>{html.escape(str(value))}</td>"


def _render_rows(entries: list[dict]) -> str:
    """Render log entries as <tr> rows (all user-generated text HTML-escaped)."""
    if not entries:
        return '<tr><td colspan="7" class="empty">No decisions recorded yet.</td></tr>'

    rows: list[str] = []
    for entry in entries:
        rows.append("<tr>")
        rows.append(f'<td class="ts">{html.escape(str(entry.get("timestamp", "")))}</td>')
        rows.append(_cell(entry.get("call_id")))
        rows.append(_cell(entry.get("intent_detected")))
        rows.append(_cell(entry.get("action_taken")))
        outcome = str(entry.get("outcome", "unknown"))
        rows.append(
            f'<td><span class="badge {html.escape(outcome)}">{html.escape(outcome)}</span></td>'
        )
        rows.append(_cell(entry.get("test_case_tag")))
        rows.append(_cell(entry.get("notes")))
        rows.append("</tr>")
    return "\n".join(rows)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Read-only page: the last 50 decision-log entries, newest first."""
    entries = read_entries(limit=50)
    # Timestamps are UTC ISO-8601 strings, so lexicographic sort == chronological.
    entries.sort(key=lambda entry: str(entry.get("timestamp", "")), reverse=True)
    return HTMLResponse(_PAGE.substitute(rows=_render_rows(entries), count=str(len(entries))))