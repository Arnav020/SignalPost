"""Offline regression tests (stdlib unittest; no network). Run: python -m unittest discover -s tests"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signalpost.claims import Envelope, add_financial_claims  # noqa: E402
from signalpost.cli import read_inputs  # noqa: E402
from signalpost.extract import extract_page, find_org_numbers, valid_org_number  # noqa: E402
from signalpost.financials import parse_accounts  # noqa: E402
from signalpost.refresh import apply_refresh  # noqa: E402
from signalpost.registry import Registry, host_key  # noqa: E402
from signalpost.website import Candidate, SiteCrawler, SiteResult, build_candidates, guess_candidates  # noqa: E402


class OrgNumbers(unittest.TestCase):
    def test_checksum(self):
        self.assertTrue(valid_org_number("979543883"))  # DIPS AS
        self.assertFalse(valid_org_number("979543884"))

    def test_find_formats(self):
        text = "Org.nr: 979 543 883 MVA · tlf 12345678 · NO979543883MVA"
        self.assertEqual(find_org_numbers(text), {"979543883"})


class Inputs(unittest.TestCase):
    def _write(self, name, text):
        path = Path(tempfile.mkdtemp()) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_formats(self):
        self.assertEqual(read_inputs(self._write("a.jsonl", '{"organisation_number":"979543883"}\n{"organisation_number":"810034882"}\n')), ["979543883", "810034882"])
        self.assertEqual(read_inputs(self._write("a.json", '["979543883", "810034882"]')), ["979543883", "810034882"])
        self.assertEqual(read_inputs(self._write("a.json", '{"organisation_numbers": ["979543883"]}')), ["979543883"])
        self.assertEqual(read_inputs(self._write("a.txt", "979543883\n810 034 882\n")), ["979543883", "810 034 882"])
        self.assertEqual(read_inputs(self._write("a.csv", "organisation_number,name\n979543883,DIPS AS\n")), ["979543883"])


class Candidates(unittest.TestCase):
    def test_shared_email_domain_not_exclusive(self):
        reg = Registry()
        for org in ("111111111", "222222222"):
            reg._own("styrerommet.no", org)
        reg._own("sandneselektriske.no", "810034882")
        cands, _ = build_candidates("810034882", {"website": "", "email": "post@sandneselektriske.no"}, [], reg)
        self.assertEqual([(c.host, c.exclusive) for c in cands], [("sandneselektriske.no", True)])
        cands, _ = build_candidates("111111111", {"website": "", "email": "x@styrerommet.no"}, [], reg)
        self.assertEqual(cands, [])  # directory/manager host filtered

    def test_registry_social_as_website(self):
        cands, social = build_candidates("1", {"website": "www.facebook.com/minbedrift", "email": "a@gmail.com"}, [], Registry())
        self.assertEqual(cands, [])
        self.assertEqual(social[0]["platform"], "facebook")

    def test_no_guess_for_housing(self):
        self.assertEqual(guess_candidates("1", {"name": "LIENE BORETTSLAG", "legal_form": "BRL"}, Registry()), [])
        self.assertTrue(guess_candidates("1", {"name": "NORDCAD AS", "legal_form": "AS"}, Registry()))

    def test_host_key(self):
        self.assertEqual(host_key("https://www.Dips.com/no/"), "dips.com")
        self.assertEqual(host_key("post@dips.no"), "dips.no")


class SocialFilter(unittest.TestCase):
    def test_partner_links_dropped(self):
        html = """<html><head><title>Pinovo</title></head><body>
        <a href="https://www.linkedin.com/company/pinovo">LinkedIn</a>
        <a href="https://www.linkedin.com/company/deloitte">partner</a>
        <a href="https://www.facebook.com/pinovo">fb</a></body></html>"""
        page = extract_page(html, "https://pinovo.no/")
        site = SiteResult(candidate=Candidate("https://pinovo.no/", "pinovo.no", "name_guess", True, 1), home_url="https://pinovo.no/")
        social = {u: {"platform": p, "url": u, "source_url": "https://pinovo.no/"} for p, us in page["social"].items() for u in us}
        kept = SiteCrawler._own_profiles({"name": "PINOVO AS"}, site, social, [], page)
        self.assertEqual(sorted(kept), ["https://www.facebook.com/pinovo", "https://www.linkedin.com/company/pinovo"])


class FinancialsAndRefresh(unittest.TestCase):
    BODY = [{"regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "valuta": "NOK", "regnskapstype": "SELSKAP",
             "virksomhet": {"morselskap": False}, "resultatregnskapResultat": {"aarsresultat": 10.0, "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 100.0}}}}]

    class Res:
        status, retrieved_at, sha256, blocked, error = 200, "2026-10-16T00:00:00Z", "abc", False, None

    def test_missing_values_not_zero(self):
        filings = parse_accounts(self.BODY)
        self.assertEqual(set(filings[0]["values"]), {"net_result", "revenue"})  # nothing invented

    def test_refresh_idempotent_and_detects_change(self):
        env = Envelope("123456785", "r1", "t")
        add_financial_claims(env, parse_accounts(self.BODY), self.Res(), {})
        changes, state = apply_refresh(env, None, "t1", True)
        self.assertEqual(changes, [])
        env2 = Envelope("123456785", "r2", "t")
        add_financial_claims(env2, parse_accounts(self.BODY), self.Res(), {})
        changes, state2 = apply_refresh(env2, json.loads(json.dumps(state)), "t2", True)
        self.assertEqual(changes, [])  # idempotent rerun
        body = json.loads(json.dumps(self.BODY))
        body[0]["resultatregnskapResultat"]["aarsresultat"] = 20.0
        env3 = Envelope("123456785", "r3", "t")
        add_financial_claims(env3, parse_accounts(body), self.Res(), {})
        changes, _ = apply_refresh(env3, state2, "t3", True)
        self.assertEqual([(c["field"], c["previous_value"], c["current_value"]) for c in changes], [("net_result", 10.0, 20.0)])


class AuditFixes(unittest.TestCase):
    """Builderr audit (23 Oct): group sites, footer social links, non-news items, accounts periods."""

    def test_news_must_be_real_dated_posts(self):
        from signalpost.website import article_like, real_title, valid_publish_date
        home = "https://firma.no/"
        self.assertFalse(article_like("https://firma.no/om-oss", home))                     # menu page
        self.assertFalse(article_like("https://firma.no/nyheter/", home, "https://firma.no/nyheter/"))  # index
        self.assertFalse(article_like("https://firma.no/nyheter/ukategorisert/", home))    # category label
        self.assertFalse(article_like("https://firma.no/produkt/skyrc-lapwiz-smart-timer", home))  # product page
        self.assertTrue(article_like("https://firma.no/nyheter/vi-flytter-til-nye-lokaler", home))
        self.assertFalse(real_title("SkyRC LapWiz Pris NOK 885,00 inkl. mva. Kjøp"))
        self.assertTrue(real_title("Vi flytter til nye lokaler i Bergen"))
        self.assertFalse(valid_publish_date(None))
        self.assertFalse(valid_publish_date("2999-01-01"))

    def test_feed_updated_is_not_a_publish_date(self):
        from signalpost.extract import parse_feed
        xml = "<feed><entry><title>Nytt fra oss i dag</title><link href='https://firma.no/nyheter/nytt-fra-oss-i-dag'/><updated>2026-10-01T10:00:00Z</updated></entry></feed>"
        self.assertIsNone(parse_feed(xml)[0]["date"])

    def test_social_handles_must_name_the_company(self):
        site = SiteResult(candidate=Candidate("https://holla-autosenter.no/", "holla-autosenter.no", "registry_website", True, 1),
                          home_url="https://holla-autosenter.no/")
        social = {u: {"platform": p, "url": u, "source_url": "x"} for p, u in [
            ("facebook", "https://www.facebook.com/531887476655112"),      # numeric id: unverifiable
            ("instagram", "https://www.instagram.com/heksem.mekaniske"),    # other company in the footer
            ("linkedin", "https://www.linkedin.com/company/holla-autosenter")]}
        kept = SiteCrawler._own_profiles({"name": "HOLLA AUTOSENTER AS"}, site, social, [], {"site_name": "My Site"})
        self.assertEqual(list(kept), ["https://www.linkedin.com/company/holla-autosenter"])

    def test_accounts_compare_company_periods_of_same_length(self):
        from signalpost.synthesis import build_summary
        body = [
            {"regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "valuta": "NOK", "regnskapstype": "SELSKAP",
             "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 110.0}}}},
            {"regnskapsperiode": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "valuta": "NOK", "regnskapstype": "KONSERN",
             "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 9000.0}}}},
            {"regnskapsperiode": {"fraDato": "2024-06-01", "tilDato": "2024-12-31"}, "valuta": "NOK", "regnskapstype": "SELSKAP",
             "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": 50.0}}}},
        ]
        env = Envelope("123456785", "r", "t")
        add_financial_claims(env, parse_accounts(body), FinancialsAndRefresh.Res(), {"latest_submitted_accounts": "2025"})
        text = " ".join(s["text"] for s in build_summary(env, [])["statements"] if s["topic"] == "finances")
        self.assertIn("Operating revenue in FY2025 was NOK 110", text)   # no growth vs a 7-month period, no group mixing
        self.assertIn("different length", text)
        self.assertIn("Group (konsern)", text)
        latest = next(c for c in env.claims if c["field"] == "latest_filed_accounts_year")
        self.assertEqual((latest["value"], latest["fiscal_year_label"]), ("2025", "FY2025"))


class RefreshFailure(unittest.TestCase):
    def test_failed_source_carries_forward_instead_of_removal(self):
        env = Envelope("123456785", "r1", "t")
        env.claim("legal_identity_and_brand", "wikidata_entity", {"url": "http://www.wikidata.org/entity/Q1"}, [])
        _, state = apply_refresh(env, None, "t1", True)
        env2 = Envelope("123456785", "r2", "t")
        env2.absent("legal_identity_and_brand", "wikidata_entity", "failed", "Wikidata could not be queried in this run.")
        changes, _ = apply_refresh(env2, state, "t2", True)
        self.assertEqual(changes, [])
        carried = [c for c in env2.claims if c["field"] == "wikidata_entity" and c.get("stale")]
        self.assertEqual(len(carried), 1)


class Workforce(unittest.TestCase):
    def test_statements(self):
        from signalpost.workforce import extract_workforce
        self.assertEqual(extract_workforce("Note 1\nAntall årsverk i regnskapsåret\n3.00\n")[0], 3)
        self.assertEqual(extract_workforce("Tal på årsverk i rekneskapsåret 0 00")[0], 0)
        self.assertEqual(extract_workforce("Selskapet har i 2025 sysselsatt 1,5 årsverk.")[0], 1.5)
        self.assertEqual(extract_workforce("Selskapet har ingen ansatte.")[0], 0)

    def test_abstains(self):
        from signalpost.workforce import extract_workforce
        self.assertIsNone(extract_workforce("Antall årsverk i regnskapsåret 100"))  # "1.00" with a lost dot
        self.assertIsNone(extract_workforce("Konsernet har 40 årsverk. Antall årsverk i konsernet 40"))
        self.assertIsNone(extract_workforce("Lønnskostnader 120 000"))


class ResearchAgent(unittest.TestCase):
    def test_suite_queries_plan_exactly(self):
        from signalpost.ask import plan_screen
        cases = {
            "companies in Oslo with above 7 employees": [("municipality", "eq", "OSLO"), ("employees", ">", 7)],
            "legal form = STI": [("legal_form", "eq", "STI")],
            "companies with fewer than 100 employees": [("employees", "<", 100)],
            "companies with revenue >= NOK 250 million": [("revenue", ">=", 250000000)],
        }
        for query, expected in cases.items():
            self.assertEqual([(f["field"], f["operator"], f["value"]) for f in plan_screen(query)["filters"]], expected)

    def test_abstention(self):
        from signalpost.ask import screen
        self.assertTrue(screen([], "find the highest Glassdoor culture scores")["abstained"])
        self.assertTrue(screen([], "which businesses are probably fraudulent")["abstained"])


if __name__ == "__main__":
    unittest.main()
