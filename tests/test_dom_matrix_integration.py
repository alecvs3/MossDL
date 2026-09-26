import unittest
from pathlib import Path
from engine.dom_cleaner import DomCleaner
from engine.element_scorer import ElementScorer


class TestDomMatrixIntegration(unittest.TestCase):
    def setUp(self):
        fixture_path = Path(__file__).parent / "fixtures" / "crawler_matrix_sample.html"
        self.assertTrue(fixture_path.is_file(), f"Fixture file not found: {fixture_path}")
        with open(fixture_path, "r", encoding="utf-8") as f:
            self.sample_html = f.read()

    def test_end_to_end_crawler_pipeline_on_fixture(self):
        source_url = "https://distro-portal.org/ubuntu-24-04"
        cleaner = DomCleaner(base_url=source_url)
        cleaner.feed(self.sample_html)

        # 1. Page Title extraction
        self.assertEqual(cleaner.page_title, "Game Archive & Emulation Portal")

        # 2. Deceptive Ads & Clickjackers Stripped
        # 3 cosmetic/network ad blocks + 1 clickjacking overlay = 4 stripped ads
        self.assertEqual(len(cleaner.stripped_ads), 4)

        stripped_urls = [ad.target_url for ad in cleaner.stripped_ads]
        self.assertTrue(any("adsterra.com" in u for u in stripped_urls))
        self.assertTrue(any("onclickprediction.com" in u for u in stripped_urls))
        self.assertTrue(any("propellerads.com" in u for u in stripped_urls))
        self.assertTrue(any("popads.net" in u for u in stripped_urls))

        # None of the ad targets should exist in clean elements
        for elem in cleaner.elements:
            for bad_domain in ["adsterra.com", "onclickprediction.com", "propellerads.com", "popads.net"]:
                self.assertNotIn(bad_domain, elem.target_url)

        # 3. Clean Interactive Elements Extracted
        self.assertEqual(len(cleaner.elements), 4)

        # 4. Form POST with Hidden CSRF Inputs
        form_elem = next((e for e in cleaner.elements if "1fichier.com" in e.target_url), None)
        self.assertIsNotNone(form_elem, "1fichier form element not found")
        self.assertEqual(form_elem.method, "POST")
        self.assertEqual(form_elem.form_inputs.get("csrf_token"), "sec_tok_9988776655")
        self.assertEqual(form_elem.form_inputs.get("file_id"), "ubuntu-24.04-desktop-amd64.iso")
        self.assertIn("Download from 1fichier", form_elem.text)

        # 5. Multi-Factor Usefulness Scoring
        scored = ElementScorer.score_all(cleaner.elements, source_url=source_url)
        self.assertEqual(len(scored), 4)

        # High utility prime downloads (score >= 70)
        high_utility_items = [item for item in scored if item.category == "high_utility"]
        self.assertEqual(len(high_utility_items), 2)
        for item in high_utility_items:
            self.assertGreaterEqual(item.score, 70)

        # Candidate secondary mirrors (score 40-69)
        candidate_items = [item for item in scored if item.category == "candidate"]
        self.assertEqual(len(candidate_items), 2)
        for item in candidate_items:
            self.assertGreaterEqual(item.score, 40)
            self.assertLess(item.score, 70)

        # Verify sorting: highest score first
        for i in range(len(scored) - 1):
            self.assertGreaterEqual(scored[i].score, scored[i + 1].score)

        # Verify MediaFire item hints
        mf_item = next(e for e in scored if "mediafire.com" in e.host)
        self.assertEqual(mf_item.size_hint, "4.8 GB")
        self.assertEqual(mf_item.filename_hint, "ubuntu-24.04.iso")

        # Verify Mega item
        mega_item = next(e for e in scored if "mega.nz" in e.host)
        self.assertIn("Mega", mega_item.text)

        # Verify Direct ISO item
        iso_item = next(e for e in scored if "releases.ubuntu.com" in e.host)
        self.assertEqual(iso_item.filename_hint, "ubuntu-24.04-desktop-amd64.iso")


if __name__ == "__main__":
    unittest.main()
