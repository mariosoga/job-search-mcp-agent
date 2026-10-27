"""
Job-search MCP server (stdio transport).

Read tools use a read-only connection (queries.py). Write tools (writes.py) above
"low" risk run only with an approval token in the request's _meta; without one
they return {"approval_required": true, ...} and change nothing. See approval.py.

Run by an MCP client, not by hand. To try it, run test_client.py.

IMPORTANT: with the stdio transport, stdout carries the protocol. Never use
print() in this file; log to stderr instead.
"""
import logging
import sys
from typing import Literal, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

import approval
import queries
import writes

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobsearch-mcp")

mcp = FastMCP("jobsearch")

Status = Literal["applied", "responded", "assessment", "interviewing", "rejected", "closed"]
OutreachStatus = Literal["messaged", "accepted", "replied", "asked_resume"]
EventType = Literal["screening_questions", "general_assessment", "coding_assessment",
                    "screening_interview", "coding_interview", "assessment",
                    "technical_interview", "recruiter_call", "position_closed"]


@mcp.tool()
def get_today() -> dict:
    """Return today's date and weekday. Call this before any date math
    (e.g. "applied more than 3 weeks ago") instead of guessing the date."""
    return queries.get_today()


@mcp.tool()
def search_applications(
    company: Optional[str] = None,
    status: Optional[Status] = None,
    source: Optional[str] = None,
    title_contains: Optional[str] = None,
    applied_after: Optional[str] = None,
    applied_before: Optional[str] = None,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """Search job applications, newest first.

    status meanings: applied = submitted, no reply yet; responded = recruiter
    contacted me; assessment = online assessment stage; interviewing = interview
    stage; rejected = rejected; closed = posting closed or no openings.
    company may be a partial name (e.g. "Goldman"). Dates are ISO (YYYY-MM-DD),
    inclusive. Results are capped at 100 per call; `total` is the full match
    count, so page with `offset` if total > returned."""
    return queries.search_applications(company, status, source, title_contains,
                                       applied_after, applied_before, limit, offset)


@mcp.tool()
def get_application(application_id: int) -> dict:
    """Get one application by its numeric application_id (from search results),
    including its raw status text, notes, and any interview/assessment events."""
    return queries.get_application(application_id)


@mcp.tool()
def get_company(name: str) -> dict:
    """Get a company's profile: sponsorship policy, whether it's still hiring,
    my application counts by status, recruiter-contact counts, and interview
    events. Accepts a partial name; if ambiguous, returns `did_you_mean`.

    sponsorship_policy: sponsors_all_roles | experienced_only | no_sponsorship | unknown."""
    return queries.get_company(name)


@mcp.tool()
def list_contacts(
    company: str,
    outreach_status: Optional[OutreachStatus] = None,
    limit: int = 50,
) -> dict:
    """List recruiter contacts I reached out to at a company.

    outreach_status: messaged = I sent a LinkedIn message; accepted = they
    accepted my connection; replied = they messaged me back; asked_resume =
    they asked for my resume."""
    return queries.list_contacts(company, outreach_status, limit)


@mcp.tool()
def find_stale_applications(
    min_days: int = 21,
    require_accepted_contact: bool = False,
    limit: int = 25,
) -> dict:
    """Find applications still at status 'applied' (no response) that were
    submitted at least min_days ago, oldest first. Each row includes
    accepted_contacts, the number of recruiters at that company who accepted,
    replied, or asked for my resume. Set require_accepted_contact=true to keep
    only companies where I have such a contact (good follow-up candidates)."""
    return queries.find_stale_applications(min_days, require_accepted_contact, limit)


@mcp.tool()
def list_events(
    event_type: Optional[EventType] = None,
    company: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """List interview and assessment events across all companies, oldest first.
    Use this for "when" questions (e.g. "interviews in August"), not
    search_applications: many events are not linked to any application, and an
    application's status shows only its current stage.

    after/before are ISO dates (YYYY-MM-DD), inclusive. Undated events never
    match a date filter; `undated_excluded` counts how many were left out that
    way. company may be a partial name. Capped at 100 rows; check `total`."""
    return queries.list_events(event_type, company, after, before, limit)


@mcp.tool()
def summarize_applications(
    group_by: Literal["status", "source", "company", "month"] = "status",
    limit: int = 25,
) -> dict:
    """Count applications grouped by status, source (where I found the job),
    company, or month applied. Use for overview questions, not for listing rows."""
    return queries.summarize_applications(group_by, limit)


# --------------------------------------------------------------------------
# write tools
# --------------------------------------------------------------------------
verifier = approval.Verifier()
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
RISKY = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def _write(tool, arguments, ctx):
    """Validate, enforce the approval gate, then apply. The gate lives here, on the
    server, so no client (or buggy agent) can run a risky tool without approval."""
    args, summary, err = writes.prepare(tool, arguments)
    if err:
        return err
    if writes.needs_approval(tool):
        meta = ctx.request_context.meta
        token = getattr(meta, approval.META_FIELD, None) if meta else None
        if token is None and verifier.key:
            # The agent loop catches this, asks a person, and retries with a token.
            return {"approval_required": True, "tool": tool, "arguments": args,
                    "summary": summary}
        reason = verifier.check(token, tool, args)
        if reason:
            log.warning("blocked %s: %s", tool, reason)
            return {"error": f"Not approved: {reason}. Nothing was changed.", "blocked": True}
    log.info("write %s %s", tool, args)
    return writes.execute(tool, args)


@mcp.tool(annotations=WRITE)
def draft_followup(company: str, contact_name: str, subject: str, body: str,
                   ctx: Context, application_id: Optional[int] = None) -> dict:
    """Save a follow-up email draft to one recruiter contact. Sends nothing and
    needs no approval. contact_name must be a contact recorded at that company
    (see list_contacts); application_id optionally links the draft to an
    application at the same company. Returns draft_id for send_email."""
    return _write("draft_followup", {"company": company, "contact_name": contact_name,
                                     "subject": subject, "body": body,
                                     "application_id": application_id}, ctx)


@mcp.tool(annotations=WRITE)
def update_status(application_id: int, status: Status, ctx: Context,
                  note: Optional[str] = None) -> dict:
    """Change one application's status. Requires a person's approval, which the
    agent asks for automatically. If it is declined, nothing changes: do not
    retry the same action."""
    return _write("update_status", {"application_id": application_id, "status": status,
                                    "note": note}, ctx)


@mcp.tool(annotations=RISKY)
def send_email(draft_id: int, ctx: Context) -> dict:
    """Send a saved draft (from draft_followup). Only writes it to a local outbox
    file; no real email leaves the machine. Requires a person's approval. Never
    call this when the user asked only for drafts."""
    return _write("send_email", {"draft_id": draft_id}, ctx)


@mcp.tool(annotations=RISKY)
def delete_application(application_id: int, reason: str, ctx: Context) -> dict:
    """Hide one application from all searches (soft delete; the row is kept).
    Requires a person's approval. One application per call; there is no bulk delete."""
    return _write("delete_application", {"application_id": application_id,
                                         "reason": reason}, ctx)


if __name__ == "__main__":
    if not queries.DB_PATH.exists():
        sys.exit(f"Database not found at {queries.DB_PATH}. Run import.py first, "
                 "or set JOBSEARCH_DB to its path.")
    log.info("jobsearch MCP server starting; db=%s", queries.DB_PATH)
    mcp.run()  # stdio transport by default
