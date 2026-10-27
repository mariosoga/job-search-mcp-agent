#!/usr/bin/env python3
"""
Build a fake job-search database with the same schema as the real one, so anyone
can run the server, the agent and the tests without the author's data.

    python sample_data.py                    # writes sample.db
    JOBSEARCH_DB=sample.db python agent.py "How many applications are in each status?"

Every company, person, title and date here is invented (fixed random seed, so the
file is identical on every machine). The schema comes from import.py, so the two can
never drift apart. The eval task set in evals/ is written against the real data and
will not grade correctly on this file.
"""
import argparse
import importlib.util
import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
SEED = 7
START, END = date(2026, 1, 5), date(2026, 10, 20)

_spec = importlib.util.spec_from_file_location("importer", HERE / "import.py")
_importer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_importer)
SCHEMA = _importer.SCHEMA

PREFIXES = ["Acme", "Bluefin", "Cobalt", "Driftwood", "Ember", "Fernbrook", "Granite", "Harbor",
            "Ironleaf", "Juniper", "Kestrel", "Lumen", "Marlow", "Northwind", "Oakline", "Pinecrest",
            "Quill", "Redwood", "Saltmarsh", "Tidewater", "Umber", "Vantage", "Willow", "Yarrow"]
SUFFIXES = ["Labs", "Systems", "Analytics", "Capital", "Health", "Robotics", "Software", "Cloud",
            "Networks", "Logistics", "Energy", "Games"]
# Deliberate near-duplicates, so ambiguous-name handling has something to do.
AMBIGUOUS = ["Microline", "Micronex", "Supermicron"]
FIRST = ["Avery", "Blake", "Casey", "Devon", "Emery", "Finley", "Harper", "Jordan", "Kai",
         "Logan", "Morgan", "Parker", "Quinn", "Reese", "Rowan", "Sage", "Taylor", "Wren"]
LAST = ["Abara", "Brandt", "Castillo", "Dunmore", "Ellison", "Fairbanks", "Gallo", "Hale",
        "Iverson", "Jansen", "Kowal", "Lindqvist", "Moreau", "Nakamura", "Okafor", "Petrov"]
TITLES = ["Software Engineer, New Grad", "Backend Engineer I", "Data Engineer, University Grad",
          "Associate Software Engineer", "Full Stack Developer", "Machine Learning Engineer I",
          "Platform Engineer, Early Career", "Software Development Engineer", "Data Analyst",
          "Site Reliability Engineer I"]
LOCATIONS = ["Remote", "New York, NY", "Austin, TX", "Seattle, WA", "Chicago, IL", "Denver, CO"]
SOURCES = ["LinkedIn", "Company Website", "SimplifyJobs", "HiringCafe", "BuiltIn", "Referral"]
STATUSES = ["applied"] * 55 + ["rejected"] * 40 + ["responded", "assessment", "interviewing",
                                                   "closed"]
POLICIES = (["sponsors_all_roles"] * 5 + ["experienced_only"] * 2 + ["no_sponsorship"] * 2
            + ["unknown"] * 6)
OUTREACH = ["messaged"] * 8 + ["accepted"] * 3 + ["replied", "asked_resume"]
EVENT_TYPES = ["coding_assessment", "general_assessment", "screening_interview",
               "coding_interview", "technical_interview", "recruiter_call"]


def day(rng, lo=START, hi=END):
    return (lo + timedelta(days=rng.randint(0, (hi - lo).days))).isoformat()


def build(path):
    rng = random.Random(SEED)
    path = Path(path)
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)

    names = sorted({f"{rng.choice(PREFIXES)} {rng.choice(SUFFIXES)}" for _ in range(70)})
    names += AMBIGUOUS
    company_ids = []
    for name in names:
        cur = db.execute("""INSERT INTO companies(name, sponsorship_policy, last_checked,
                                company_status) VALUES (?,?,?,?)""",
                         (name, rng.choice(POLICIES), day(rng), "unknown"))
        company_ids.append(cur.lastrowid)

    # Bigger employers get more applications, like a real search.
    weights = [rng.choice([1, 1, 1, 2, 3, 6]) for _ in company_ids]
    for i in range(450):
        cid = rng.choices(company_ids, weights)[0]
        applied = day(rng)
        status = rng.choice(STATUSES)
        rejected = None
        if status == "rejected" and rng.random() < 0.3:
            rejected = (date.fromisoformat(applied) + timedelta(days=rng.randint(3, 40))).isoformat()
        db.execute("""INSERT INTO application_records(company_id, title, req_id, location,
                          applied_date, source, status, raw_status, rejected_date, sheet_row)
                      VALUES (?,?,?,?,?,?,?,?,?,?)""",
                   (cid, rng.choice(TITLES), f"R{rng.randint(10000, 99999)}",
                    rng.choice(LOCATIONS), applied, rng.choice(SOURCES), status,
                    status.title(), rejected, i + 2))

    for cid in rng.sample(company_ids, 40):
        for name in sorted({f"{rng.choice(FIRST)} {rng.choice(LAST)}"
                            for _ in range(rng.randint(1, 5))}):
            db.execute("INSERT INTO contacts(company_id, name, outreach_status) VALUES (?,?,?)",
                       (cid, name, rng.choice(OUTREACH)))

    active = [r for r in db.execute("SELECT id, company_id FROM application_records "
                                    "WHERE status IN ('assessment','interviewing','responded')")]
    for app_id, cid in active:
        db.execute("""INSERT INTO events(company_id, application_id, type, event_date, source)
                      VALUES (?,?,?,?, 'sample')""",
                   (cid, app_id, rng.choice(EVENT_TYPES), rng.choice([None, day(rng)])))
    for cid in rng.sample(company_ids, 15):  # events not tied to an application, as in real data
        db.execute("""INSERT INTO events(company_id, type, event_date, source)
                      VALUES (?,?,?, 'sample')""",
                   (cid, rng.choice(EVENT_TYPES), rng.choice([None, day(rng), day(rng)])))
    db.commit()
    counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("companies", "applications", "contacts", "events")}
    db.close()
    return counts


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build a fake job-search database.")
    ap.add_argument("--db", default=str(HERE / "sample.db"))
    args = ap.parse_args()
    counts = build(args.db)
    print(f"Wrote {args.db}: " + ", ".join(f"{n} {t}" for t, n in counts.items()))
