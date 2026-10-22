#!/usr/bin/env python3
"""
Build jobsearch.db (SQLite) from the three Google Sheets CSV exports.

Usage (from the folder that holds the CSVs):
    python3 import.py
    python3 import.py --csv-dir ~/Documents/job-search --db jobsearch.db

Uses only the Python standard library. Re-running rebuilds the database from
scratch, so it is safe to run again after you fix the CSVs or this script.
"""
import argparse
import csv
import glob
import os
import re
import sqlite3
import sys
from datetime import date

DEFAULT_YEAR = 2026  # your sheet's dates are all 2026 and often omit the year

# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE companies (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    sponsorship_policy  TEXT NOT NULL DEFAULT 'unknown'
        CHECK (sponsorship_policy IN
               ('sponsors_all_roles','experienced_only','no_sponsorship','unknown')),
    careers_pages       TEXT,              -- e.g. 'New Graduates, All Roles'
    last_checked        TEXT,              -- ISO date
    recruiter_outreach  INTEGER NOT NULL DEFAULT 0,
    company_status      TEXT NOT NULL DEFAULT 'unknown'
        CHECK (company_status IN
               ('applied_rejected','applied_no_response','applied_in_progress',
                'no_posting','not_applied','unknown')),
    no_more_recruiters  INTEGER NOT NULL DEFAULT 0,
    notes               TEXT
);

CREATE TABLE applications (
    id                  INTEGER PRIMARY KEY,
    company_id          INTEGER NOT NULL REFERENCES companies(id),
    title               TEXT NOT NULL,
    req_id              TEXT,
    location            TEXT,
    applied_date        TEXT,              -- ISO date
    source              TEXT,
    recruiter_hunt      INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL
        CHECK (status IN
               ('applied','responded','assessment','interviewing','rejected','closed')),
    raw_status          TEXT,
    response_bucket     TEXT
        CHECK (response_bucket IS NULL OR response_bucket IN
               ('within_week','1_2_weeks','2_4_weeks','under_month')),
    raw_response_time   TEXT,
    rejected_date       TEXT,              -- ISO date, only where the notes gave one
    days_to_rejection   INTEGER,
    possible_duplicate  INTEGER NOT NULL DEFAULT 0,
    notes               TEXT,
    sheet_row           INTEGER            -- row number in the Applications CSV
);

CREATE TABLE contacts (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    name            TEXT NOT NULL,
    outreach_status TEXT NOT NULL
        CHECK (outreach_status IN ('messaged','accepted','replied','asked_resume')),
    team            TEXT,
    UNIQUE (company_id, name)
);

CREATE TABLE events (
    id              INTEGER PRIMARY KEY,
    company_id      INTEGER NOT NULL REFERENCES companies(id),
    application_id  INTEGER REFERENCES applications(id),
    type            TEXT NOT NULL
        CHECK (type IN
               ('screening_questions','general_assessment','coding_assessment',
                'screening_interview','coding_interview','assessment',
                'technical_interview','recruiter_call','position_closed')),
    event_date      TEXT,                  -- ISO date, may be NULL if unknown
    details         TEXT,
    source          TEXT NOT NULL          -- 'summary_tab' | 'applications_status' | 'applications_notes'
);

CREATE TABLE data_issues (
    id          INTEGER PRIMARY KEY,
    table_name  TEXT,
    row_ref     TEXT,
    kind        TEXT NOT NULL,
    detail      TEXT
);

CREATE INDEX idx_app_company ON applications(company_id);
CREATE INDEX idx_app_status  ON applications(status);
CREATE INDEX idx_app_date    ON applications(applied_date);
CREATE INDEX idx_contacts_co ON contacts(company_id);
CREATE INDEX idx_events_co   ON events(company_id);

CREATE VIEW stale_applications AS
SELECT a.id AS application_id, c.name AS company, a.title, a.applied_date,
       CAST(julianday('now') - julianday(a.applied_date) AS INTEGER) AS days_since_applied
FROM applications a JOIN companies c ON c.id = a.company_id
WHERE a.status = 'applied' AND a.applied_date IS NOT NULL;
"""

# --------------------------------------------------------------------------
# Normalization tables
# --------------------------------------------------------------------------
# Keys are lowercase with whitespace collapsed. Anything not listed keeps its
# spelling (case variants of the same name still merge onto the first one seen).
COMPANY_ALIASES = {
    "akuna": "Akuna Capital",
    "amex": "Amex",
    "apex fintech solutions (apex)": "Apex Fintech Solutions",
    "aquatic capital management": "Aquatic",
    "atlassians": "Atlassian",
    "aveva": "AVEVA",
    "bitdeer (nasdaq: btdr)": "Bitdeer",
    "checkr, inc.": "Checkr",
    "cienna": "Ciena",
    "cisco": "Cisco Systems",
    "citadale": "Citadel",
    "clearwater analytics (cwan)": "Clearwater Analytics",
    "clearwater analytics - cwan": "Clearwater Analytics",
    "cloudfare": "Cloudflare",
    "cerebras systems": "Cerebras",
    "compass real estate - peninsula": "Compass",
    "dell technologies": "Dell",
    "doordash": "DoorDash",
    "ebay": "eBay",
    "ernst & young (ey)": "EY",
    "ey (ernst & young)": "EY",
    "everpure data": "Everpure",
    "geico": "GEICO",
    "github": "GitHub",
    "hcl tech": "HCLTech",
    "hewlett packard enterprise (hpe)": "Hewlett Packard Enterprise",
    "hpe": "Hewlett Packard Enterprise",
    "hudson river trading (hrt)": "Hudson River Trading",
    "imc": "IMC Trading",
    "linkedin": "LinkedIn",
    "london stock exchange group (lseg)": "LSEG",
    "ltimindtree (ltm)": "LTM",
    "ltimindtree": "LTM",
    "lucid": "Lucid Motors",
    "mckinsey & company": "McKinsey & Company",
    "nelnet business services (nbs)": "Nelnet",
    "netapp": "NetApp",
    "openai": "OpenAI",
    "optum services": "Optum",
    "pros, inc.": "PROS",
    "pwc": "PwC",
    "qualtrix": "Qualtrics",
    "quantumblack, ai by mckinsey": "QuantumBlack",
    "quantumblack-mckinsey": "QuantumBlack",
    "retell ai": "Retell AI",
    "service now": "ServiceNow",
    "servicenow": "ServiceNow",
    "sig (susquehanna international group)": "Susquehanna International Group",
    "simplisafe": "SimpliSafe",
    "snap": "Snapchat",
    "sony interactive entertainment (playstation)": "Sony Interactive Entertainment",
    "state street charles river development (crd)": "State Street",
    "stubhub": "StubHub",
    "the walt disney company": "Disney",
    "tiktok": "TikTok",
    "uber": "Uber",
    "ubs business solutions us llc": "UBS",
    "us bank national association": "USBank",
    "walmart tech": "Walmart",
    "warner bros.(cnn)": "Warner Bros.",
    "wd": "Western Digital",
    "zebra": "Zebra Technologies",
    "whatnot": "Whatnot",
}

SOURCE_ALIASES = {
    "linkedin": "LinkedIn",
    "linkedin posts search": "LinkedIn",
    "linkedin - post search": "LinkedIn",
    "website": "Company Website",
    "direct ats": "Company Website",
    "simplifyjobs": "SimplifyJobs",
    "simplify": "SimplifyJobs",
    "simplfiy": "SimplifyJobs",
    "simplify website": "SimplifyJobs",
    "simplify[github]": "SimplifyJobs",
    "2026-swe-college-jobs": "2026-SWE-College-Jobs",
    "2026-swe [github]": "2026-SWE-College-Jobs",
    "hiringcafe": "HiringCafe",
    "jobright.ai": "Jobright.ai",
    "builtin": "BuiltIn",
    "dreamwork": "Dreamwork",
    "chatgpt prompt": "ChatGPT",
    "gemini": "Gemini",
    "gemini prompt": "Gemini",
    "openquant": "OpenQuant",
}

SPONSORSHIP_MAP = {
    "sponsorship all roles": "sponsors_all_roles",
    "sponsorship only experienced roles": "experienced_only",
    "no sponsorship [explicitly stated]": "no_sponsorship",
}

COMPANY_STATUS_MAP = {
    "have applied, was rejected": "applied_rejected",
    "have applied, no response": "applied_no_response",
    "have applied, in progress": "applied_in_progress",
    "no job posting that i can apply": "no_posting",
    "haven't applied": "not_applied",
}

RESPONSE_BUCKETS = {
    "> within a week": "within_week",
    "1-2 weeks": "1_2_weeks",
    "2-4 weeks": "2_4_weeks",
    "< month": "under_month",
}
BUCKET_MAX_DAYS = {"within_week": 7, "1_2_weeks": 14, "2_4_weeks": 28, "under_month": 31}

SUMMARY_EVENT_TYPES = {
    "screening questions": "screening_questions",
    "general assesment": "general_assessment",
    "general assessment": "general_assessment",
    "coding assesment": "coding_assessment",
    "coding assessment": "coding_assessment",
    "screening interview": "screening_interview",
    "coding interviews 1r": "coding_interview",
}

CONTACT_RANK = {"messaged": 0, "accepted": 1, "replied": 2, "asked_resume": 3}
NOT_NAME_WORDS = {
    "staffing", "company", "interview", "recruiter", "call", "role", "sponsorship",
    "applied", "rejection", "email", "only", "senior", "roles", "position",
    "visa", "screening", "coding", "phone",
    "software", "engineer", "engineering", "developer", "professional", "financial",
}

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})"
    r"(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?\b", re.I)


def ws(s):
    """Collapse all whitespace (incl. newlines) to single spaces."""
    return re.sub(r"\s+", " ", s or "").strip()


def find_date(text, default_year=DEFAULT_YEAR):
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        return date(int(m.group(3) or default_year), MONTHS[m.group(1).lower()], int(m.group(2)))
    except ValueError:
        return None


def iso(d):
    return d.isoformat() if d else None


def read_csv(path):
    """Yield (row_number, dict) with whitespace-normalized headers; skips blank rows."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = [ws(h) for h in next(reader)]
        for i, row in enumerate(reader, start=2):  # row 1 is the header
            if not any(c.strip() for c in row):
                continue
            row = row + [""] * (len(header) - len(row))
            yield i, dict(zip(header, row))


def find_csv(csv_dir, suffix):
    # Match "Company Status.csv" and "Company_Status.csv" alike.
    want = (suffix.replace("_", " ") + ".csv").lower()
    hits = sorted(
        p for p in glob.glob(os.path.join(csv_dir, "*.csv"))
        if os.path.basename(p).replace("_", " ").lower().endswith(want))
    if not hits:
        sys.exit(f"Could not find a CSV ending in '{suffix.replace('_', ' ')}.csv' in {os.path.abspath(csv_dir)}")
    return hits[0]


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------
class Builder:
    def __init__(self, db_path):
        if os.path.exists(db_path):
            os.remove(db_path)
        self.db = sqlite3.connect(db_path)
        self.db.executescript(SCHEMA)
        self.display = {}      # lowercase key -> canonical display name
        self.company_ids = {}  # canonical name -> id

    # ---- issues ---------------------------------------------------------
    def issue(self, table, ref, kind, detail):
        self.db.execute(
            "INSERT INTO data_issues(table_name,row_ref,kind,detail) VALUES (?,?,?,?)",
            (table, str(ref), kind, detail))

    # ---- companies ------------------------------------------------------
    def canon(self, raw):
        key = ws(raw).lower()
        if key in COMPANY_ALIASES:
            name = COMPANY_ALIASES[key]
            self.display.setdefault(name.lower(), name)
            return self.display[name.lower()]
        self.display.setdefault(key, ws(raw))
        return self.display[key]

    def company_id(self, name):
        if name not in self.company_ids:
            cur = self.db.execute("INSERT INTO companies(name) VALUES (?)", (name,))
            self.company_ids[name] = cur.lastrowid
        return self.company_ids[name]

    # ---- contacts / notes parsing --------------------------------------
    @staticmethod
    def clean_item(raw):
        status = None
        if re.search(r"\(\s*(accepted and messaged me|messaged me)\s*\)", raw, re.I):
            status = "replied"
        elif re.search(r"\(\s*asked for resume\s*\)", raw, re.I):
            status = "asked_resume"
        elif re.search(r"\(\s*accepted\s*\)", raw, re.I):
            status = "accepted"
        name = re.sub(r"\(\s*(accepted and messaged me|messaged me|asked for resume|accepted)\s*\)",
                      "", raw, flags=re.I)
        name = ws(name).strip(" -,")
        name = re.sub(r"(?<=[a-z])\.$", "", name)  # "Touchard." -> "Touchard" (keeps "Brandon J.")
        return name, status

    @staticmethod
    def looks_like_name(text):
        if not re.fullmatch(r"[^\d:;/@!?]{2,60}", text):
            return False
        words = text.replace("(", " ").replace(")", " ").split()
        if not 1 <= len(words) <= 7:
            return False
        return not any(w.lower().strip(".,") in NOT_NAME_WORDS for w in words)

    def parse_note(self, note):
        """Return (contacts, free_text, no_more_recruiters).
        contacts = list of (name, status, team)."""
        text = (note or "").replace("\r", "").strip().strip('"')
        no_more = bool(re.search(r"no more recruiters to message", text, re.I))
        text = re.sub(r"no more recruiters to message!?", "", text, flags=re.I)

        m = re.search(r"(Recruiters|People) I messaged on Linkedin:", text, re.I)
        if not m:
            return [], "\n".join(l.strip() for l in text.split("\n") if l.strip()), no_more

        free = [l.strip() for l in text[:m.start()].split("\n") if l.strip()]
        rest = re.sub(r"-?\s*Accepted:\s*", "\n@@ACC@@", text[m.end():])
        # IXL lists recruiters by office on one line: "Raleigh - A, B San Mateo - C, D"
        rest = re.sub(r"\s*\b(Raleigh|San Mateo)\s+-\s+", r"\n\1 - ", rest)

        contacts, mode, team = [], "messaged", None
        for line in rest.split("\n"):
            line = line.strip()
            if line.startswith("@@ACC@@"):
                mode, line = "accepted", line[len("@@ACC@@"):].strip()
            line = re.sub(r"^-\s*", "", line).strip()
            # Groups like "Credit Karma: A, B" or "Raleigh - A, B" set a team/location
            # that stays in effect until the next group header.
            mt = re.match(r"^(Credit Karma):\s*(.*)$", line) or \
                 re.match(r"^([A-Z][A-Za-z.' ]{1,25}?)\s+-\s+(.+)$", line)
            if mt:
                team, line = ws(mt.group(1)), mt.group(2)
            if not line:
                continue
            items = []
            for part in (p.strip() for p in line.split(",")):
                if not part:
                    continue
                if part.upper() in {"MBA", "PHD", "JR", "SR"} and items:
                    items[-1] += ", " + part
                else:
                    items.append(part)
            parsed = [self.clean_item(it) for it in items]
            if parsed and all(self.looks_like_name(n) for n, _ in parsed):
                for name, flag in parsed:
                    contacts.append((name, flag or mode, team))
            else:
                free.append(line)
        return contacts, "\n".join(free), no_more

    def add_contacts(self, company_id, contacts):
        for name, status, team in contacts:
            row = self.db.execute(
                "SELECT id, outreach_status FROM contacts WHERE company_id=? AND name=?",
                (company_id, name)).fetchone()
            if row is None:
                self.db.execute(
                    "INSERT INTO contacts(company_id,name,outreach_status,team) VALUES (?,?,?,?)",
                    (company_id, name, status, team))
            elif CONTACT_RANK[status] > CONTACT_RANK[row[1]]:
                self.db.execute("UPDATE contacts SET outreach_status=? WHERE id=?", (status, row[0]))

    # ---- Company_Status tab --------------------------------------------
    def load_company_status(self, path):
        grouped = {}  # canonical name -> list of parsed rows
        for rownum, r in read_csv(path):
            raw_name = ws(r.get("Company"))
            if not raw_name or raw_name.lower() == "focused area":
                continue  # blank row or section header
            name = self.canon(raw_name)
            contacts, free, no_more = self.parse_note(r.get("Notes"))
            careers = ", ".join(l.strip() for l in (r.get("Links") or "").split("\n") if l.strip())
            checked = find_date(r.get("Checked Date"))
            grouped.setdefault(name, []).append({
                "row": rownum,
                "policy": SPONSORSHIP_MAP.get(ws(r.get("Sponsorship Status")).lower()),
                "careers": careers or None,
                "checked": checked,
                "outreach": 1 if ws(r.get("Recruiter Reachout!")).upper() == "YES" else 0,
                "status": COMPANY_STATUS_MAP.get(ws(r.get("Status")).lower()),
                "free": free, "no_more": no_more, "contacts": contacts,
            })

        for name, rows in grouped.items():
            rows.sort(key=lambda x: (x["checked"] is not None, x["checked"] or date.min))
            policies = {x["policy"] for x in rows if x["policy"]}
            if len(policies) > 1:
                self.issue("companies", name, "conflicting_sponsorship",
                           f"{len(rows)} rows disagree ({', '.join(sorted(policies))}); kept the most recently checked")
            merged = {"policy": None, "careers": None, "checked": None, "outreach": 0,
                      "status": None, "no_more": 0}
            notes, all_contacts = [], []
            for x in rows:  # oldest -> newest, so newer non-null values win
                for k in ("policy", "careers", "checked", "status"):
                    if x[k]:
                        merged[k] = x[k]
                merged["outreach"] |= x["outreach"]
                merged["no_more"] |= 1 if x["no_more"] else 0
                if x["free"] and x["free"] not in notes:
                    notes.append(x["free"])
                all_contacts += x["contacts"]
            cid = self.company_id(name)
            self.db.execute(
                """UPDATE companies SET sponsorship_policy=?, careers_pages=?, last_checked=?,
                          recruiter_outreach=?, company_status=?, no_more_recruiters=?, notes=?
                   WHERE id=?""",
                (merged["policy"] or "unknown", merged["careers"], iso(merged["checked"]),
                 merged["outreach"], merged["status"] or "unknown", merged["no_more"],
                 "\n---\n".join(notes) or None, cid))
            self.add_contacts(cid, all_contacts)

    # ---- Applications tab ----------------------------------------------
    @staticmethod
    def split_req_id(title):
        title = ws(title)
        m = re.search(r"\(\s*([A-Za-z0-9_\-]*\d[A-Za-z0-9_\-]*)\s*\)\s*$", title)
        if m and len(m.group(1)) >= 4 and not re.fullmatch(r"(19|20)\d{2}", m.group(1)):
            return ws(title[:m.start()]), m.group(1)
        m = re.search(r"\s[-–]?\s*((?:[A-Z]{1,4}-?)?\d{4,}[A-Z0-9]*)$", title)
        if m and not re.fullmatch(r"(19|20)\d{2}", m.group(1)):
            return ws(title[:m.start()]).rstrip(" -–"), m.group(1)
        return title, None

    @staticmethod
    def map_status(raw, title):
        tokens = [t.strip().lower() for t in (raw or "").split(",") if t.strip()]
        if "rejected" in tokens:
            return "rejected", tokens
        if "no openings" in tokens or (not tokens and title.upper().startswith("NO OPENINGS")):
            return "closed", tokens
        if "technical interview" in tokens:
            return "interviewing", tokens
        if "assessment" in tokens:
            return "assessment", tokens
        if "responded" in tokens:
            return "responded", tokens
        if "applied" in tokens:
            return "applied", tokens
        return "closed", tokens

    def load_applications(self, path):
        seen = {}
        for rownum, r in read_csv(path):
            raw_company = ws(r.get("Company"))
            location = ws(r.get("Location")).strip(" ·.")
            # Two rows in your sheet have the wrong text in the Company column.
            if raw_company == "Software Engineer" and "Redmond" in location:
                self.issue("applications", rownum, "company_name_repaired",
                           "Company cell said 'Software Engineer'; req number/Redmond location "
                           "match a Microsoft posting, so it was set to Microsoft")
                raw_company = "Microsoft"
            elif raw_company == "Boise,ID":
                self.issue("applications", rownum, "company_name_missing",
                           f"Company cell contains a location. Title: {ws(r.get('Job Title'))}. "
                           "Edit this row's company by hand.")
                raw_company = "Unknown (Boise, ID)"
            name = self.canon(raw_company)
            cid = self.company_id(name)

            title, req_id = self.split_req_id(r.get("Job Title"))
            applied = find_date(r.get("Applied Date"))
            if applied is None:
                self.issue("applications", rownum, "applied_date_unparsed", r.get("Applied Date"))

            source_raw = ws(r.get("Source"))
            source = SOURCE_ALIASES.get(source_raw.lower(), source_raw) if source_raw else None

            raw_status = ws(r.get("Status"))
            status, tokens = self.map_status(raw_status, title)

            raw_resp = ws(r.get("Response Time"))
            bucket = RESPONSE_BUCKETS.get(raw_resp.lower()) if raw_resp else None

            note = "\n".join(l.strip() for l in (r.get("Notes") or "").split("\n") if l.strip())
            low = note.lower()
            rejected, note_event_date = None, None
            if note and applied:
                d = find_date(note, applied.year)
                if d and ("reject" in low or DATE_RE.fullmatch(note.strip())):
                    rejected = d
                elif d and "assess" in low:
                    note_event_date = d
                if rejected and rejected < applied:
                    try:
                        rejected = rejected.replace(year=rejected.year + 1)
                    except ValueError:
                        rejected = None
                    if rejected is None or rejected < applied:
                        self.issue("applications", rownum, "rejection_before_applied",
                                   f"note '{note[:60]}' gives a date earlier than applied date {applied}")
                        rejected = None
            days = (rejected - applied).days if rejected and applied else None
            if days is not None and bucket and days > BUCKET_MAX_DAYS[bucket] + 2:
                self.issue("applications", rownum, "response_bucket_mismatch",
                           f"{name}: bucket '{raw_resp}' but notes imply {days} days to rejection")

            key = (name, title.lower(), location.lower(), iso(applied))
            dup = 1 if key in seen else 0
            cur = self.db.execute(
                """INSERT INTO applications(company_id,title,req_id,location,applied_date,source,
                       recruiter_hunt,status,raw_status,response_bucket,raw_response_time,
                       rejected_date,days_to_rejection,possible_duplicate,notes,sheet_row)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cid, title, req_id, location or None, iso(applied), source,
                 1 if ws(r.get("Recruiter Hunt")).upper() == "YES" else 0,
                 status, raw_status or None, bucket, raw_resp or None,
                 iso(rejected), days, dup, note or None, rownum))
            app_id = cur.lastrowid
            if dup:
                self.db.execute("UPDATE applications SET possible_duplicate=1 WHERE id=?",
                                (seen[key],))
                self.issue("applications", rownum, "possible_duplicate",
                           f"{name} | {title} | {location} | {applied} also appears earlier")
            else:
                seen[key] = app_id

            # Stage information that lives inside the Status text
            if "assessment" in tokens:
                self.event(cid, app_id, "assessment", note_event_date, note or None, "applications_status")
            if "technical interview" in tokens:
                self.event(cid, app_id, "technical_interview", None, note or None, "applications_status")
            if "responded" in tokens:
                self.event(cid, app_id, "recruiter_call", None, note or None, "applications_status")
            if "no openings" in tokens or re.search(r"position (closed|was filled)", low):
                self.event(cid, app_id, "position_closed", None, note or None, "applications_notes")

    def event(self, cid, app_id, etype, d, details, source):
        self.db.execute(
            "INSERT INTO events(company_id,application_id,type,event_date,details,source) "
            "VALUES (?,?,?,?,?,?)", (cid, app_id, etype, iso(d), details, source))

    # ---- Summary tab ----------------------------------------------------
    def load_summary(self, path):
        item_re = re.compile(r"([^(),]+?)\s*\(([^)]*)\)")
        for rownum, r in read_csv(path):
            etype = SUMMARY_EVENT_TYPES.get(ws(r.get("Criteria")).lower())
            if not etype:
                self.issue("events", rownum, "unknown_summary_criteria", r.get("Criteria"))
                continue
            for m in item_re.finditer(r.get("Details") or ""):
                raw_name, inner = ws(m.group(1)), ws(m.group(2))
                d = find_date(inner)
                if not raw_name or d is None:
                    self.issue("events", rownum, "summary_item_unparsed", m.group(0))
                    continue
                cid = self.company_id(self.canon(raw_name))
                extra = ws(DATE_RE.sub("", inner, count=1)).strip(" ,")
                self.event(cid, None, etype, d, extra or None, "summary_tab")

    def finish(self):
        self.db.commit()


# --------------------------------------------------------------------------
def report(db):
    q = lambda sql: db.execute(sql).fetchall()
    print("\n=== Row counts ===")
    for t in ("companies", "applications", "contacts", "events", "data_issues"):
        print(f"  {t:<13}{q(f'SELECT COUNT(*) FROM {t}')[0][0]:>6}")
    print("\n=== Applications by status ===")
    for s, n in q("SELECT status, COUNT(*) FROM applications GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {s:<13}{n:>6}")
    print("\n=== Companies by sponsorship policy ===")
    for s, n in q("SELECT sponsorship_policy, COUNT(*) FROM companies GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {s:<20}{n:>5}")
    print("\n=== Contacts by outreach status ===")
    for s, n in q("SELECT outreach_status, COUNT(*) FROM contacts GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {s:<13}{n:>6}")
    print("\n=== Events by type ===")
    for s, n in q("SELECT type, COUNT(*) FROM events GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {s:<22}{n:>4}")
    n_rej = q("SELECT COUNT(*) FROM applications WHERE status='rejected'")[0][0]
    n_dated = q("SELECT COUNT(*) FROM applications WHERE rejected_date IS NOT NULL")[0][0]
    print(f"\nRejected applications with an exact rejection date: {n_dated} of {n_rej}")
    print("\n=== Data issues found (details saved in the data_issues table) ===")
    for k, n in q("SELECT kind, COUNT(*) FROM data_issues GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {k:<28}{n:>5}")
    print("\nDone. Open the file in DB Browser for SQLite, or run: sqlite3 jobsearch.db")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv-dir", default=".", help="folder containing the three CSV exports")
    ap.add_argument("--db", default="jobsearch.db", help="output SQLite file")
    args = ap.parse_args()
    csv_dir = os.path.expanduser(args.csv_dir)

    apps = find_csv(csv_dir, "Applications")
    status = find_csv(csv_dir, "Company_Status")
    summary = find_csv(csv_dir, "Summary")
    print("Reading:\n ", "\n  ".join(os.path.basename(p) for p in (status, apps, summary)))

    b = Builder(os.path.expanduser(args.db))
    b.load_company_status(status)   # first, so company-level data is the base
    b.load_applications(apps)
    b.load_summary(summary)
    b.finish()
    report(b.db)


if __name__ == "__main__":
    main()
