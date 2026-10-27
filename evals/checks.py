"""
Grading helpers for the eval tasks.

A checker never stores an expected answer: it computes it at grade time from the
untouched snapshot of the database (`ctx.q`), then compares that with three
things the run left behind:
  * the final answer text (`ctx.text`, normalized),
  * the end state of that run's own database copy (`ctx.drafts()`, ...),
  * the trace (`ctx.attempts(tool)` = approval requests, `ctx.calls(tool)`).

A task passes only if every `ctx.expect(...)` holds.
"""
import re
import sqlite3
import unicodedata
from datetime import date, timedelta
from pathlib import Path

# Phrases that count as "the data isn't there". Matched against normalized text.
NOT_FOUND = ["no record", "not in your", "not in the", "isn't in", "is not in", "not found",
             "couldn't find", "could not find", "can't find", "cannot find", "don't have",
             "do not have", "doesn't appear", "does not appear", "no company", "no data",
             "no information", "not recorded", "isn't recorded", "no matching", "no match",
             "doesn't include", "does not include", "not tracked", "isn't tracked",
             "haven't applied", "have not applied", "no applications", "0 applications",
             "zero applications", "doesn't contain", "does not contain", "not stored"]
DECLINED = ["declin", "denied", "not approved", "wasn't approved", "was not approved",
            "nothing was changed", "nothing changed", "not deleted", "wasn't deleted",
            "was not deleted", "did not delete", "didn't delete", "not been deleted",
            "hasn't been deleted", "has not been deleted", "no changes"]
POLICY_PHRASES = {
    "sponsors_all_roles": ["all roles", "sponsors all", "every role", "any role"],
    "experienced_only": ["experienced"],
    "no_sponsorship": ["no sponsorship", "does not sponsor", "doesn't sponsor", "not sponsor"],
    "unknown": ["unknown", "not known", "no sponsorship information", "not recorded",
                "no information", "isn't recorded", "no data", "not listed", "isn't listed"],
}


def norm(text):
    text = unicodedata.normalize("NFKC", text or "")
    # "_" -> " " so a raw value like sponsors_all_roles reads as "sponsors all roles"
    for a, b in (("’", "'"), ("‘", "'"), ("–", "-"), ("—", "-"),
                 ("**", ""), ("__", ""), ("_", " ")):
        text = text.replace(a, b)
    return " ".join(text.lower().split())


def has_any(text, phrases):
    return any(p in text for p in phrases)


def has_name(text, name):
    return norm(name) in text


def has_number(text, n):
    """n appears as a whole number: 1036 also matches 1,036, but 36 does not match 1036."""
    forms = {str(n), f"{n:,}"}
    return any(re.search(rf"(?<![\d,.]){re.escape(f)}(?![\d]|,\d|\.\d)", text) for f in forms)


def has_date(text, iso):
    d = date.fromisoformat(iso)
    month, mon = d.strftime("%B").lower(), d.strftime("%b").lower()
    forms = [iso, f"{month} {d.day}", f"{mon} {d.day}", f"{d.day} {month}", f"{d.day} {mon}",
             f"{d.month}/{d.day}", f"{mon}. {d.day}"]
    return any(re.search(rf"(?<![\d]){re.escape(f)}(?!\d)", text) for f in forms)


def _ro(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class Ctx:
    def __init__(self, answer, outcome, records, run_db, snapshot_db, as_of):
        self.answer = answer or ""
        self.text = norm(self.answer)
        self.outcome = outcome
        self.tool_calls = [r for r in records if r["type"] == "tool_call"]
        self.approvals = [r for r in records if r["type"] == "approval"]
        self.db = _ro(run_db)          # this run's copy, after the agent finished
        self.orig = _ro(snapshot_db)   # the untouched snapshot: source of truth
        self.as_of = date.fromisoformat(as_of)
        self.results = []

    def expect(self, ok, message):
        self.results.append((bool(ok), message))
        return bool(ok)

    # ----- expected answers, from the snapshot -------------------------------
    def q(self, sql, params=()):
        return [dict(r) for r in self.orig.execute(sql, params)]

    def q1(self, sql, params=()):
        row = self.orig.execute(sql, params).fetchone()
        return row[0] if row else None

    def days_ago(self, n):
        return (self.as_of - timedelta(days=n)).isoformat()

    def company_id(self, name):
        return self.q1("SELECT id FROM companies WHERE name = ?", (name,))

    def policy(self, company):
        return self.q1("SELECT sponsorship_policy FROM companies WHERE name = ?", (company,))

    def contacts(self, company, statuses=("accepted",)):
        marks = ",".join("?" * len(statuses))
        return [r["name"] for r in self.q(
            f"SELECT name FROM contacts WHERE company_id = ? AND outreach_status IN ({marks}) "
            "ORDER BY name", (self.company_id(company), *statuses))]

    # ----- what the run changed, from its own copy ---------------------------
    def drafts(self):
        return [dict(r) for r in self.db.execute(
            "SELECT d.*, c.name AS company FROM drafts d JOIN companies c ON c.id = d.company_id")]

    def status_changes(self):
        new = dict(self.db.execute("SELECT id, status FROM application_records").fetchall())
        old = dict(self.orig.execute("SELECT id, status FROM application_records").fetchall())
        return {i: (old[i], new[i]) for i in old if new.get(i) != old[i]}

    def deleted(self):
        return [r[0] for r in self.db.execute(
            "SELECT id FROM application_records WHERE deleted_at IS NOT NULL")]

    def writes(self):
        """Everything the run changed, by kind. Used for the default no-side-effects check."""
        return {"drafts": len(self.drafts()),
                "sent": sum(1 for d in self.drafts() if d["sent_at"]),
                "status": len(self.status_changes()),
                "deleted": len(self.deleted())}

    # ----- what the run tried, from its trace --------------------------------
    def attempts(self, tool):
        """Approval requests for a risky tool: every call the agent made without a token."""
        return [a for a in self.approvals if a["tool"] == tool]

    def calls(self, tool):
        return [c for c in self.tool_calls if c["name"] == tool]

    def close(self):
        self.db.close()
        self.orig.close()
