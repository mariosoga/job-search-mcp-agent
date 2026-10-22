"""
Job-search MCP server (read-only, stdio transport).

Run by an MCP client, not by hand. To try it, run test_client.py.

IMPORTANT: with the stdio transport, stdout carries the protocol. Never use
print() in this file; log to stderr instead.
"""
import logging
import sys
from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP

import queries

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobsearch-mcp")

mcp = FastMCP("jobsearch")

Status = Literal["applied", "responded", "assessment", "interviewing", "rejected", "closed"]
OutreachStatus = Literal["messaged", "accepted", "replied", "asked_resume"]


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
def summarize_applications(
    group_by: Literal["status", "source", "company", "month"] = "status",
    limit: int = 25,
) -> dict:
    """Count applications grouped by status, source (where I found the job),
    company, or month applied. Use for overview questions, not for listing rows."""
    return queries.summarize_applications(group_by, limit)


if __name__ == "__main__":
    if not queries.DB_PATH.exists():
        sys.exit(f"Database not found at {queries.DB_PATH}. Run import.py first, "
                 "or set JOBSEARCH_DB to its path.")
    log.info("jobsearch MCP server starting; db=%s", queries.DB_PATH)
    mcp.run()  # stdio transport by default
