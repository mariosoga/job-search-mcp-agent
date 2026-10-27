"""
Write tools for the job-search database. The only module that changes data.

Each tool is two steps, so the server can put an approval gate between them:
  prepare(tool, args)  validate with Pydantic and against the database, and
                       return the normalized arguments plus a plain-English
                       summary of exactly what would change. Changes nothing.
  execute(tool, args)  apply it on a read-write connection, in one transaction
                       that also appends to write_log.

Risk levels (server.py requires an approval token for anything above "low"):
  draft_followup      low     saves a draft; sends nothing
  update_status       medium  changes one application's status
  send_email          high    marks a draft sent and appends it to outbox/outbox.jsonl;
                              never sends real email
  delete_application  high    soft delete: the row is hidden, not erased

Like queries.py, nothing here raises on bad input: it returns {"error": ...}.
"""
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

import queries

OUTBOX = Path(os.environ.get("JOBSEARCH_OUTBOX",
                             Path(__file__).with_name("outbox") / "outbox.jsonl"))

Status = Literal["applied", "responded", "assessment", "interviewing", "rejected", "closed"]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DraftFollowup(_Args):
    company: str = Field(min_length=1, max_length=200)
    contact_name: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    application_id: Optional[int] = Field(default=None, gt=0)


class UpdateStatus(_Args):
    application_id: int = Field(gt=0)
    status: Status
    note: Optional[str] = Field(default=None, max_length=500)


class SendEmail(_Args):
    draft_id: int = Field(gt=0)


class DeleteApplication(_Args):
    application_id: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=300)


TOOLS = {
    "draft_followup": (DraftFollowup, "low"),
    "update_status": (UpdateStatus, "medium"),
    "send_email": (SendEmail, "high"),
    "delete_application": (DeleteApplication, "high"),
}


def needs_approval(tool):
    return TOOLS[tool][1] != "low"


def connect_rw():
    conn = sqlite3.connect(queries.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _validation_error(tool, e):
    problems = [f"{'.'.join(map(str, err['loc'])) or 'arguments'}: {err['msg']}"
                for err in e.errors()]
    return {"error": f"Invalid arguments for {tool}: " + "; ".join(problems)}


def _application(conn, application_id):
    """A live (not deleted) application with its company name, or None."""
    row = conn.execute("""SELECT a.id, a.title, a.status, a.applied_date, a.company_id,
                                 c.name AS company
                          FROM applications a JOIN companies c ON c.id = a.company_id
                          WHERE a.id = ?""", (application_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# prepare: validate and describe, change nothing
# --------------------------------------------------------------------------
def prepare(tool, arguments):
    """Return (normalized_args, summary, None) or (None, None, error_dict)."""
    if tool not in TOOLS:
        return None, None, {"error": f"Unknown write tool {tool!r}"}
    model_cls, _ = TOOLS[tool]
    try:
        args = model_cls(**{k: v for k, v in (arguments or {}).items() if v is not None})
    except ValidationError as e:
        return None, None, _validation_error(tool, e)
    with connect_rw() as conn:
        summary, err = globals()[f"_prepare_{tool}"](conn, args)
    if err:
        return None, None, err
    return args.model_dump(exclude_none=True), summary, None


def _prepare_draft_followup(conn, a):
    company, err = queries.resolve_company(conn, a.company)
    if err:
        return None, err
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM contacts WHERE company_id = ? ORDER BY name", (company["id"],))]
    if a.contact_name.lower() not in {n.lower() for n in names}:
        return None, {"error": f"{a.contact_name!r} is not a recorded contact at {company['name']}.",
                      "did_you_mean": names[:20]}
    if a.application_id is not None:
        app = _application(conn, a.application_id)
        if not app or app["company_id"] != company["id"]:
            return None, {"error": f"Application {a.application_id} is not a live application "
                                   f"at {company['name']}."}
    return (f"Save a draft to {a.contact_name} at {company['name']} "
            f"(subject: {a.subject!r}). Nothing is sent."), None


def _prepare_update_status(conn, a):
    app = _application(conn, a.application_id)
    if not app:
        return None, {"error": f"No live application with id {a.application_id}"}
    if app["status"] == a.status:
        return None, {"error": f"Application {a.application_id} is already {a.status!r}; "
                               "nothing to change."}
    return (f"Change application {app['id']} ({app['company']}: {app['title']}, applied "
            f"{app['applied_date']}) from {app['status']!r} to {a.status!r}"
            + (f". Note: {a.note}" if a.note else "")), None


def _prepare_send_email(conn, a):
    d = conn.execute("""SELECT d.*, c.name AS company FROM drafts d
                        JOIN companies c ON c.id = d.company_id WHERE d.id = ?""",
                     (a.draft_id,)).fetchone()
    if not d:
        return None, {"error": f"No draft with id {a.draft_id}. Save one with draft_followup first."}
    if d["sent_at"]:
        return None, {"error": f"Draft {a.draft_id} was already sent at {d['sent_at']}."}
    return (f"Send draft {d['id']} to {d['contact_name']} at {d['company']} "
            f"(written to the local outbox only).\n"
            f"  Subject: {d['subject']}\n  Body:\n"
            + "\n".join("    " + line for line in d["body"].splitlines())), None


def _prepare_delete_application(conn, a):
    app = _application(conn, a.application_id)
    if not app:
        return None, {"error": f"No live application with id {a.application_id}"}
    return (f"Hide application {app['id']} ({app['company']}: {app['title']}, applied "
            f"{app['applied_date']}, status {app['status']!r}). Reason: {a.reason}. "
            "The row is kept and can be restored."), None


# --------------------------------------------------------------------------
# execute: apply one prepared write
# --------------------------------------------------------------------------
def execute(tool, args):
    """Apply a write whose args came from prepare(). Returns the result dict."""
    with connect_rw() as conn:  # commits on success, rolls back on any exception
        result = globals()[f"_execute_{tool}"](conn, args)
        conn.execute("INSERT INTO write_log(at, tool, arguments, result) VALUES (?,?,?,?)",
                     (_now(), tool, json.dumps(args, ensure_ascii=False),
                      json.dumps(result, ensure_ascii=False)))
    return result


def _execute_draft_followup(conn, a):
    company, _ = queries.resolve_company(conn, a["company"])
    contact = conn.execute("SELECT name FROM contacts WHERE company_id = ? AND lower(name) = lower(?)",
                           (company["id"], a["contact_name"])).fetchone()["name"]
    cur = conn.execute("""INSERT INTO drafts(company_id, application_id, contact_name, subject,
                                             body, created_at) VALUES (?,?,?,?,?,?)""",
                       (company["id"], a.get("application_id"), contact, a["subject"],
                        a["body"], _now()))
    return {"draft_id": cur.lastrowid, "company": company["name"], "contact_name": contact,
            "note": "Draft saved. Nothing was sent."}


def _execute_update_status(conn, a):
    old = conn.execute("SELECT status FROM applications WHERE id = ?",
                       (a["application_id"],)).fetchone()["status"]
    conn.execute("UPDATE application_records SET status = ? WHERE id = ?",
                 (a["status"], a["application_id"]))
    return {"application_id": a["application_id"], "old_status": old, "new_status": a["status"]}


def _execute_send_email(conn, a):
    d = dict(conn.execute("""SELECT d.*, c.name AS company FROM drafts d
                             JOIN companies c ON c.id = d.company_id WHERE d.id = ?""",
                          (a["draft_id"],)).fetchone())
    sent_at = _now()
    conn.execute("UPDATE drafts SET sent_at = ? WHERE id = ?", (sent_at, d["id"]))
    # File last: if it fails, the exception rolls back the UPDATE above.
    OUTBOX.parent.mkdir(exist_ok=True)
    with OUTBOX.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"sent_at": sent_at, "draft_id": d["id"], "to": d["contact_name"],
                            "company": d["company"], "subject": d["subject"],
                            "body": d["body"]}, ensure_ascii=False) + "\n")
    return {"draft_id": d["id"], "sent_at": sent_at,
            "note": "Written to the local outbox. No real email was sent."}


def _execute_delete_application(conn, a):
    deleted_at = _now()
    conn.execute("UPDATE application_records SET deleted_at = ?, deleted_reason = ? WHERE id = ?",
                 (deleted_at, a["reason"], a["application_id"]))
    return {"application_id": a["application_id"], "deleted_at": deleted_at,
            "note": "Hidden from all searches; the row is kept."}
