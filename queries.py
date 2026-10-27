"""
Read-only query layer for the job-search database.

No MCP imports live here, so every function can be tested (and later reused by
the evaluation harness) without the MCP SDK. server.py just exposes these as tools.

Design rules that matter for the agent:
  * The database is opened read-only, so no tool here can change data.
  * Results are capped (MAX_LIMIT) and report `total`, so the agent knows when
    it is only seeing part of the data.
  * Bad input never raises: it returns {"error": ..., plus suggestions}, so the
    agent can recover, and your traces can classify the failure.
  * "Today" comes from one place (today()). Set JOBSEARCH_AS_OF=YYYY-MM-DD to
    freeze it, which keeps date-dependent eval tasks reproducible.
"""
import os
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path(os.environ.get("JOBSEARCH_DB", Path(__file__).with_name("jobsearch.db"))).resolve()
MAX_LIMIT = 100

STATUSES = ("applied", "responded", "assessment", "interviewing", "rejected", "closed")
OUTREACH_STATUSES = ("messaged", "accepted", "replied", "asked_resume")
EVENT_TYPES = ("screening_questions", "general_assessment", "coding_assessment",
               "screening_interview", "coding_interview", "assessment",
               "technical_interview", "recruiter_call", "position_closed")

APP_COLUMNS = """a.id AS application_id, c.name AS company, a.title, a.req_id, a.location,
                 a.applied_date, a.source, a.status, a.rejected_date, a.days_to_rejection,
                 a.possible_duplicate, a.notes"""


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------
def connect():
    # as_uri() percent-encodes spaces, which matters for a folder named "Resume Projects"
    conn = sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def today():
    v = os.environ.get("JOBSEARCH_AS_OF")
    return date.fromisoformat(v) if v else date.today()


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def _limit(n):
    try:
        return max(1, min(int(n), MAX_LIMIT))
    except (TypeError, ValueError):
        return 25


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _check_date(value, field):
    """Return (iso_string_or_None, error_dict_or_None)."""
    if value is None or value == "":
        return None, None
    try:
        return date.fromisoformat(value).isoformat(), None
    except ValueError:
        return None, {"error": f"{field} must be an ISO date like 2026-09-01, got {value!r}"}


def resolve_company(conn, name):
    """Return (company_row_dict | None, error_dict | None).

    Exact (case-insensitive) match first, then a single partial match.
    Several partial matches -> error listing candidates so the agent can retry.
    """
    name = " ".join((name or "").split())
    if not name:
        return None, {"error": "company name is empty"}
    row = conn.execute("SELECT * FROM companies WHERE lower(name) = lower(?)", (name,)).fetchone()
    if row:
        return dict(row), None
    like = _rows(conn,
                 "SELECT name FROM companies WHERE lower(name) LIKE '%' || lower(?) || '%' "
                 "ESCAPE '\\' ORDER BY name LIMIT 10", (_like_escape(name),))
    names = [r["name"] for r in like]
    if len(names) == 1:
        return dict(conn.execute("SELECT * FROM companies WHERE name = ?", (names[0],)).fetchone()), None
    if names:
        return None, {"error": f"No company named {name!r}; several partial matches.",
                      "did_you_mean": names}
    return None, {"error": f"No company matching {name!r} in the database.", "did_you_mean": []}


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------
def get_today():
    d = today()
    return {"today": d.isoformat(), "weekday": d.strftime("%A")}


def search_applications(company=None, status=None, source=None, title_contains=None,
                        applied_after=None, applied_before=None, limit=25, offset=0):
    if status is not None and status not in STATUSES:
        return {"error": f"status must be one of {list(STATUSES)}, got {status!r}"}
    after, err = _check_date(applied_after, "applied_after")
    if err:
        return err
    before, err = _check_date(applied_before, "applied_before")
    if err:
        return err

    with connect() as conn:
        where, params = [], []
        if company:
            row, err = resolve_company(conn, company)
            if err:
                return err
            where.append("a.company_id = ?")
            params.append(row["id"])
        if status:
            where.append("a.status = ?")
            params.append(status)
        if source:
            where.append("lower(a.source) = lower(?)")
            params.append(source)
        if title_contains:
            where.append("lower(a.title) LIKE '%' || lower(?) || '%' ESCAPE '\\'")
            params.append(_like_escape(title_contains))
        if after:
            where.append("a.applied_date >= ?")
            params.append(after)
        if before:
            where.append("a.applied_date <= ?")
            params.append(before)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(
            f"SELECT COUNT(*) FROM applications a {clause}", params).fetchone()[0]
        lim, off = _limit(limit), max(0, int(offset or 0))
        rows = _rows(conn, f"""SELECT {APP_COLUMNS} FROM applications a
                               JOIN companies c ON c.id = a.company_id {clause}
                               ORDER BY a.applied_date DESC, a.id DESC LIMIT ? OFFSET ?""",
                     params + [lim, off])
    return {"total": total, "returned": len(rows), "offset": off, "applications": rows}


def get_application(application_id):
    with connect() as conn:
        rows = _rows(conn, f"""SELECT {APP_COLUMNS}, a.raw_status, a.raw_response_time
                               FROM applications a JOIN companies c ON c.id = a.company_id
                               WHERE a.id = ?""", (application_id,))
        if not rows:
            return {"error": f"No application with id {application_id}"}
        events = _rows(conn, "SELECT type, event_date, details FROM events "
                             "WHERE application_id = ? ORDER BY event_date", (application_id,))
    return {"application": rows[0], "events": events}


def get_company(name):
    with connect() as conn:
        company, err = resolve_company(conn, name)
        if err:
            return err
        cid = company["id"]
        app_counts = {r["status"]: r["n"] for r in _rows(
            conn, "SELECT status, COUNT(*) AS n FROM applications WHERE company_id=? GROUP BY status", (cid,))}
        contact_counts = {r["outreach_status"]: r["n"] for r in _rows(
            conn, "SELECT outreach_status, COUNT(*) AS n FROM contacts WHERE company_id=? GROUP BY 1", (cid,))}
        latest = conn.execute(
            "SELECT MAX(applied_date) FROM applications WHERE company_id=?", (cid,)).fetchone()[0]
        events = _rows(conn, "SELECT type, event_date, details, application_id FROM events "
                             "WHERE company_id=? ORDER BY event_date", (cid,))
    company.pop("id", None)
    return {"company": company,
            "application_counts": app_counts,
            "total_applications": sum(app_counts.values()),
            "latest_application_date": latest,
            "contact_counts": contact_counts,
            "events": events}


def list_contacts(company, outreach_status=None, limit=50):
    if outreach_status is not None and outreach_status not in OUTREACH_STATUSES:
        return {"error": f"outreach_status must be one of {list(OUTREACH_STATUSES)}, got {outreach_status!r}"}
    with connect() as conn:
        row, err = resolve_company(conn, company)
        if err:
            return err
        sql = "SELECT name, outreach_status, team FROM contacts WHERE company_id = ?"
        params = [row["id"]]
        if outreach_status:
            sql += " AND outreach_status = ?"
            params.append(outreach_status)
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        rows = _rows(conn, sql + " ORDER BY outreach_status, name LIMIT ?", params + [_limit(limit)])
    return {"company": row["name"], "total": total, "returned": len(rows), "contacts": rows}


def find_stale_applications(min_days=21, require_accepted_contact=False, limit=25, offset=0):
    """Applications still at status 'applied' for at least min_days (oldest first)."""
    try:
        min_days = int(min_days)
    except (TypeError, ValueError):
        return {"error": f"min_days must be an integer, got {min_days!r}"}
    if min_days < 0:
        return {"error": "min_days must be 0 or more"}
    now = today()
    cutoff = date.fromordinal(now.toordinal() - min_days).isoformat()
    sql = """
        SELECT * FROM (
            SELECT a.id AS application_id, c.name AS company, a.title, a.applied_date,
                   CAST(julianday(?) - julianday(a.applied_date) AS INTEGER) AS days_since_applied,
                   (SELECT COUNT(*) FROM contacts k WHERE k.company_id = c.id
                     AND k.outreach_status IN ('accepted','replied','asked_resume')) AS accepted_contacts
            FROM applications a JOIN companies c ON c.id = a.company_id
            WHERE a.status = 'applied' AND a.applied_date IS NOT NULL AND a.applied_date <= ?
        ) """ + ("WHERE accepted_contacts > 0 " if require_accepted_contact else "")
    params = [now.isoformat(), cutoff]
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
        off = max(0, int(offset or 0))
        rows = _rows(conn, sql + " ORDER BY applied_date ASC, application_id LIMIT ? OFFSET ?",
                     params + [_limit(limit), off])
    return {"as_of": now.isoformat(), "min_days": min_days, "total": total,
            "returned": len(rows), "offset": off, "applications": rows}


def list_events(event_type=None, company=None, after=None, before=None, limit=50):
    """Interview/assessment events, oldest first; undated events sort last.

    A date filter cannot match an undated event, so those are counted in
    `undated_excluded` instead of silently disappearing."""
    if event_type is not None and event_type not in EVENT_TYPES:
        return {"error": f"event_type must be one of {list(EVENT_TYPES)}, got {event_type!r}"}
    after, err = _check_date(after, "after")
    if err:
        return err
    before, err = _check_date(before, "before")
    if err:
        return err

    with connect() as conn:
        where, params = [], []
        if company:
            row, err = resolve_company(conn, company)
            if err:
                return err
            where.append("e.company_id = ?")
            params.append(row["id"])
        if event_type:
            where.append("e.type = ?")
            params.append(event_type)
        base = list(where)
        if after:
            where.append("e.event_date >= ?")
            params.append(after)
        if before:
            where.append("e.event_date <= ?")
            params.append(before)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM events e {clause}", params).fetchone()[0]
        rows = _rows(conn, f"""SELECT e.type, e.event_date, c.name AS company, e.application_id,
                                      a.title, e.details
                               FROM events e JOIN companies c ON c.id = e.company_id
                               LEFT JOIN applications a ON a.id = e.application_id {clause}
                               ORDER BY e.event_date IS NULL, e.event_date, e.id LIMIT ?""",
                     params + [_limit(limit)])
        undated = 0
        if after or before:
            undated_clause = " AND ".join(base + ["e.event_date IS NULL"])
            undated = conn.execute(f"SELECT COUNT(*) FROM events e WHERE {undated_clause}",
                                   params[:len(base)]).fetchone()[0]
    return {"total": total, "returned": len(rows), "undated_excluded": undated, "events": rows}


def summarize_applications(group_by="status", limit=25):
    exprs = {"status": "a.status", "source": "COALESCE(a.source, '(none)')",
             "company": "c.name", "month": "substr(a.applied_date, 1, 7)"}
    if group_by not in exprs:
        return {"error": f"group_by must be one of {list(exprs)}, got {group_by!r}"}
    order = "1" if group_by == "month" else "2 DESC, 1"  # months read best in date order
    with connect() as conn:
        rows = _rows(conn, f"""SELECT {exprs[group_by]} AS {group_by}, COUNT(*) AS applications
                               FROM applications a JOIN companies c ON c.id = a.company_id
                               GROUP BY 1 ORDER BY {order} LIMIT ?""", (_limit(limit),))
        total = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    return {"group_by": group_by, "total_applications": total, "groups": rows}
