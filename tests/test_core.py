"""Offline tests: python -m unittest discover -s tests"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from jobagent import ats, check, config, filters, pipeline, profile

PREFS = config.load_yaml(config.EXAMPLE / "preferences.yml")
TODAY = date(2026, 11, 16)

PROFILE = """
## Experience
### X1 | Example Bank | Analyst | Jan 2024–Present | Baku
- [E1] (confirmed) Built a scorecard-based risk-scoring tool on prudential data using SQL and Excel.
- [E2] (needs-proof: never measured) Cut inspectors' workload by more than half across 22 banks.
## Skills
- [S1] (confirmed) Python with AI assistance; SQL (basic joins).
"""


def job(**kw):
    base = {"id": "x:1", "employer": "Acme", "employer_slug": "acme", "title": "Credit Risk Analyst",
            "location": "Dubai, UAE", "description": "We need 2 years of experience with SQL.",
            "posted_at": "2026-11-10", "remote": False}
    return {**base, **kw}


class Filters(unittest.TestCase):
    def test_pass(self):
        self.assertEqual(filters.hard_filter(job(), PREFS, TODAY), [])

    def test_years(self):
        self.assertEqual(filters.min_years_required("5+ years of experience in credit risk"), 5)
        self.assertEqual(filters.min_years_required("6+ years on the investment team"), 6)
        self.assertEqual(filters.min_years_required("2-4 years experience"), 2)
        self.assertEqual(filters.min_years_required("Founded 15 years ago, we serve 3 markets"), 0)
        self.assertTrue(any("years" in r for r in filters.hard_filter(
            job(description="Minimum 7 years of experience."), PREFS, TODAY)))

    def test_language(self):
        self.assertEqual(filters.language_required("Fluent Arabic is required.", ["Arabic"]), "Arabic")
        self.assertEqual(filters.language_required("Arabic is a plus.", ["Arabic"]), "")

    def test_location_and_remote(self):
        self.assertTrue(filters.hard_filter(job(location="New York, US"), PREFS, TODAY))
        self.assertEqual(filters.hard_filter(job(location="Remote - EMEA", remote=True), PREFS, TODAY), [])
        self.assertTrue(filters.hard_filter(job(location="Remote - US", remote=True), PREFS, TODAY))
        self.assertTrue(filters.hard_filter(job(location="Kyiv, Ukraine"), PREFS, TODAY))  # 'UK' must not match

    def test_citizens_seniority_age(self):
        self.assertTrue(filters.hard_filter(job(description="Open to UAE nationals only."), PREFS, TODAY))
        self.assertTrue(filters.hard_filter(job(description="You must have the right to work in the UK."), PREFS, TODAY))
        self.assertTrue(filters.hard_filter(job(title="Head of Credit Risk"), PREFS, TODAY))
        self.assertTrue(filters.hard_filter(job(posted_at="2025-01-01"), PREFS, TODAY))
        stale = job(posted_at="2026-09-01")
        self.assertEqual(filters.hard_filter(stale, PREFS, TODAY), [])
        self.assertIn("stale_days", stale)


class Fabrication(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "p.md").write_text(PROFILE, encoding="utf-8")
        self.p = profile.load(self.dir / "p.md")

    def cv(self, text, sources):
        return {"experience": [{"entry": "X1", "bullets": [{"text": text, "sources": sources}]}]}

    def test_faithful_rewording_passes(self):
        self.assertEqual(check.check_cv(self.cv(
            "Built a risk-scoring scorecard tool on prudential data with SQL and Excel.", ["E1"]), self.p), [])

    def test_catches_inventions(self):
        for bad, needle in [
            ("Built a scoring tool used by 22 banks.", "number"),
            ("Built a scoring tool in PyTorch.", "tool"),
            ("Built a scoring tool with Oliver Wyman.", "name"),
            ("Led the build of a scoring tool.", "stronger"),
        ]:
            errs = check.check_cv(self.cv(bad, ["E1"]), self.p)
            self.assertTrue(any(needle in e for e in errs), (bad, errs))

    def test_needs_proof_and_missing_sources(self):
        self.assertTrue(check.check_cv(self.cv("Cut workload.", ["E2"]), self.p))
        self.assertTrue(check.check_cv(self.cv("Built a tool.", []), self.p))
        self.assertTrue(check.check_cv(self.cv("Built a tool.", ["E9"]), self.p))
        errs = check.check_cv(self.cv("Built a tool that cut inspectors' workload by more than expected.", ["E1"]), self.p)
        self.assertTrue(any("needs-proof" in e for e in errs))


class Misc(unittest.TestCase):
    def test_strip_html_double_escaped(self):
        self.assertEqual(ats.strip_html("&lt;p&gt;Hello &amp;amp; bye&lt;/p&gt;"), "Hello & bye")

    def test_skill_cap(self):
        reqs = [{"must": True, "status": "partial"}, {"must": True, "status": "missing"}]
        self.assertEqual(pipeline._skill_cap(reqs), 2.0)
        self.assertEqual(pipeline._skill_cap([{"must": True, "status": "met"}]), 5.0)

    def test_portals_load(self):
        emps = config.portals()
        self.assertGreater(len(emps), 40)
        for e in emps:
            self.assertIn(e["ats"], list(ats.FETCHERS) + ["search"])
            if e["ats"] == "workday":
                self.assertTrue(e["host"] and e["tenant"] and e["site"])


if __name__ == "__main__":
    unittest.main()
