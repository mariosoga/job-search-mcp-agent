"""
Unit tests for the query layer, the write layer and approval tokens. They run on
the fake sample database (built fresh in a temp folder), never on real data.

    python -m unittest discover tests
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import approval  # noqa: E402
import queries  # noqa: E402
import sample_data  # noqa: E402
import writes  # noqa: E402

AS_OF = "2026-10-27"


class SampleDB(unittest.TestCase):
    """Each test class gets its own fresh sample database and a frozen date."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.db = cls.tmp / "sample.db"
        sample_data.build(cls.db)
        cls._old = (queries.DB_PATH, os.environ.get("JOBSEARCH_AS_OF"), writes.OUTBOX)
        queries.DB_PATH = cls.db.resolve()
        writes.OUTBOX = cls.tmp / "outbox.jsonl"
        os.environ["JOBSEARCH_AS_OF"] = AS_OF

    @classmethod
    def tearDownClass(cls):
        queries.DB_PATH, as_of, writes.OUTBOX = cls._old
        if as_of is None:
            os.environ.pop("JOBSEARCH_AS_OF", None)
        else:
            os.environ["JOBSEARCH_AS_OF"] = as_of
        shutil.rmtree(cls.tmp)

    def sql(self, query, params=()):
        with closing(sqlite3.connect(self.db)) as c:
            return c.execute(query, params).fetchall()


class TestQueries(SampleDB):
    def test_today_is_frozen(self):
        self.assertEqual(queries.get_today()["today"], AS_OF)

    def test_connection_is_read_only(self):
        with queries.connect() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM companies")

    def test_search_reports_total_and_caps_limit(self):
        r = queries.search_applications(limit=500)
        self.assertEqual(r["total"], self.sql("SELECT COUNT(*) FROM applications")[0][0])
        self.assertEqual(r["returned"], queries.MAX_LIMIT)

    def test_search_pages_cover_every_row_once(self):
        total = queries.search_applications(status="applied", limit=1)["total"]
        seen = []
        for off in range(0, total, 100):
            seen += [a["application_id"] for a in
                     queries.search_applications(status="applied", limit=100, offset=off)["applications"]]
        self.assertEqual(len(seen), total)
        self.assertEqual(len(set(seen)), total)

    def test_search_rejects_bad_input_without_raising(self):
        self.assertIn("error", queries.search_applications(status="pending"))
        self.assertIn("error", queries.search_applications(applied_after="last month"))

    def test_resolve_company(self):
        exact = queries.get_company("microline")
        self.assertEqual(exact["company"]["name"], "Microline")
        ambiguous = queries.get_company("Micro")
        self.assertEqual(ambiguous["did_you_mean"], ["Microline", "Micronex", "Supermicron"])
        missing = queries.get_company("Nonexistent Corp")
        self.assertEqual(missing["did_you_mean"], [])

    def test_like_wildcards_are_escaped(self):
        self.assertIn("error", queries.get_company("%"))
        self.assertEqual(queries.search_applications(title_contains="_")["total"], 0)

    def test_company_counts_add_up(self):
        name = self.sql("SELECT name FROM companies ORDER BY id LIMIT 1")[0][0]
        r = queries.get_company(name)
        self.assertEqual(sum(r["application_counts"].values()), r["total_applications"])

    def test_stale_is_oldest_first_and_pages(self):
        first = queries.find_stale_applications(min_days=21, limit=100)
        second = queries.find_stale_applications(min_days=21, limit=100, offset=100)
        dates = [a["applied_date"] for a in first["applications"]]
        self.assertEqual(dates, sorted(dates))
        ids = {a["application_id"] for a in first["applications"] + second["applications"]}
        self.assertEqual(len(ids), min(first["total"], 200))
        self.assertTrue(all(a["days_since_applied"] >= 21 for a in first["applications"]))

    def test_stale_accepted_contact_filter(self):
        r = queries.find_stale_applications(min_days=21, require_accepted_contact=True, limit=100)
        self.assertTrue(all(a["accepted_contacts"] > 0 for a in r["applications"]))

    def test_events_date_filter_counts_undated(self):
        r = queries.list_events(after="2026-01-01", before="2026-12-31")
        dated = self.sql("SELECT COUNT(*) FROM events WHERE event_date IS NOT NULL")[0][0]
        undated = self.sql("SELECT COUNT(*) FROM events WHERE event_date IS NULL")[0][0]
        self.assertEqual(r["total"], dated)
        self.assertEqual(r["undated_excluded"], undated)

    def test_summary_total_matches(self):
        r = queries.summarize_applications("status")
        self.assertEqual(sum(g["applications"] for g in r["groups"]), r["total_applications"])


class TestWrites(SampleDB):
    def app_id(self, status="applied"):
        return self.sql("SELECT id FROM applications WHERE status = ? ORDER BY id LIMIT 1",
                        (status,))[0][0]

    def test_prepare_validates_and_changes_nothing(self):
        logged = self.sql("SELECT COUNT(*) FROM write_log")[0][0]
        _, _, err = writes.prepare("delete_application", {"application_id": 1, "reason": "x"})
        self.assertIn("reason", err["error"])
        _, _, err = writes.prepare("update_status", {"application_id": 1, "status": "pending"})
        self.assertIn("status", err["error"])
        _, _, err = writes.prepare("update_status", {"application_id": 1, "bogus": 1,
                                                     "status": "rejected"})
        self.assertIn("bogus", err["error"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM write_log")[0][0], logged)

    def test_update_status(self):
        app = self.app_id()
        args, summary, err = writes.prepare("update_status", {"application_id": app,
                                                              "status": "rejected"})
        self.assertIsNone(err)
        self.assertIn("'applied' to 'rejected'", summary)
        r = writes.execute("update_status", args)
        self.assertEqual((r["old_status"], r["new_status"]), ("applied", "rejected"))
        _, _, err = writes.prepare("update_status", args)
        self.assertIn("already", err["error"])

    def test_soft_delete_hides_but_keeps_the_row(self):
        app = self.app_id()
        args, _, _ = writes.prepare("delete_application", {"application_id": app,
                                                           "reason": "withdrew"})
        writes.execute("delete_application", args)
        self.assertIn("error", queries.get_application(app))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM application_records WHERE id = ?",
                                  (app,))[0][0], 1)

    def test_draft_requires_a_recorded_contact_then_sends_once(self):
        company, contact = self.sql("""SELECT c.name, k.name FROM contacts k
                                       JOIN companies c ON c.id = k.company_id LIMIT 1""")[0]
        _, _, err = writes.prepare("draft_followup", {"company": company, "contact_name": "Nobody",
                                                      "subject": "Hi", "body": "Hello"})
        self.assertIn("did_you_mean", err)
        args, _, err = writes.prepare("draft_followup", {"company": company,
                                                         "contact_name": contact.upper(),
                                                         "subject": "Hi", "body": "Hello"})
        self.assertIsNone(err)
        draft = writes.execute("draft_followup", args)
        self.assertEqual(draft["contact_name"], contact)
        send, summary, _ = writes.prepare("send_email", {"draft_id": draft["draft_id"]})
        self.assertIn("Hello", summary)
        writes.execute("send_email", send)
        self.assertEqual(len(writes.OUTBOX.read_text().splitlines()), 1)
        _, _, err = writes.prepare("send_email", send)
        self.assertIn("already sent", err["error"])

    def test_risk_levels(self):
        self.assertFalse(writes.needs_approval("draft_followup"))
        for tool in ("update_status", "send_email", "delete_application"):
            self.assertTrue(writes.needs_approval(tool))


class TestApproval(unittest.TestCase):
    def setUp(self):
        self.key = approval.new_key()
        self.v = approval.Verifier(self.key)
        self.args = {"application_id": 7, "status": "rejected"}

    def test_valid_token_works_once(self):
        token = approval.mint(self.key, "update_status", self.args)
        self.assertIsNone(self.v.check(token, "update_status", self.args))
        self.assertIn("already used", self.v.check(token, "update_status", self.args))

    def test_token_is_bound_to_tool_and_arguments(self):
        token = approval.mint(self.key, "update_status", self.args)
        self.assertIn("does not match", self.v.check(token, "delete_application", self.args))
        self.assertIn("does not match",
                      self.v.check(token, "update_status", {**self.args, "application_id": 8}))

    def test_argument_order_and_none_do_not_matter(self):
        token = approval.mint(self.key, "update_status", {"status": "rejected", "application_id": 7,
                                                          "note": None})
        self.assertIsNone(self.v.check(token, "update_status", self.args))

    def test_expired_forged_and_foreign_tokens(self):
        old = approval.mint(self.key, "update_status", self.args, now=time.time() - 3600)
        self.assertIn("expired", self.v.check(old, "update_status", self.args))
        self.assertIn("malformed", self.v.check("nonsense", "update_status", self.args))
        foreign = approval.mint(approval.new_key(), "update_status", self.args)
        self.assertIn("does not match", self.v.check(foreign, "update_status", self.args))

    def test_no_key_disables_risky_tools(self):
        self.assertIn("not configured", approval.Verifier("").check("x", "update_status", {}))


if __name__ == "__main__":
    unittest.main()
