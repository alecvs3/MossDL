import unittest
from unittest.mock import patch, MagicMock
from engine.dom_cleaner import DomCleaner, CleanedElement, is_ad_domain, is_cosmetic_ad_attribute, is_clickjacking_overlay
from engine.element_scorer import ElementScorer, MatrixElement


class TestDomCleaner(unittest.TestCase):
    def test_ad_domain_detection(self):
        self.assertTrue(is_ad_domain("https://popads.net/serve?id=123"))
        self.assertTrue(is_ad_domain("https://sub.adsterra.com/click"))
        self.assertTrue(is_ad_domain("https://propellerads.com/out"))
        self.assertFalse(is_ad_domain("https://mediafire.com/file/abc"))
        self.assertFalse(is_ad_domain("https://1fichier.com/?xyz"))
        self.assertFalse(is_ad_domain("https://github.com/repo/releases"))

    def test_cosmetic_attribute_detection(self):
        self.assertTrue(is_cosmetic_ad_attribute("download-ad-banner"))
        self.assertTrue(is_cosmetic_ad_attribute("fake-download-btn"))
        self.assertTrue(is_cosmetic_ad_attribute("adsterra-container"))
        self.assertTrue(is_cosmetic_ad_attribute("monetag_slot"))
        self.assertFalse(is_cosmetic_ad_attribute("btn btn-primary download-link"))
        self.assertFalse(is_cosmetic_ad_attribute("mediafire-download-box"))

    def test_clickjacking_overlay_detection(self):
        style1 = "position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 99999; opacity: 0;"
        self.assertTrue(is_clickjacking_overlay(style1))

        style2 = "position: absolute; inset: 0; z-index: 100000; background: transparent;"
        self.assertTrue(is_clickjacking_overlay(style2))

        normal_style = "position: relative; display: inline-block; padding: 10px 20px;"
        self.assertFalse(is_clickjacking_overlay(normal_style))

    def test_cleaner_strips_ads_and_keeps_genuine_links(self):
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Archive Download Portal</title></head>
        <body>
            <div class="ad-banner">
                <a href="https://adsterra.com/click?fake=1">
                    <img alt="Download High Speed (AD)" />
                </a>
            </div>

            <div class="content">
                <h1>Linux ISO Release</h1>
                <div class="fake-download-wrapper">
                    <a href="https://clickadu.com/redirect">Fast Free Download (Sponsor)</a>
                </div>

                <div class="download-section">
                    <a class="btn btn-download" href="/downloads/ubuntu-24.04.iso">Direct Download (HTTP)</a>
                    <a href="https://mediafire.com/file/12345/ubuntu.iso">Mirror 1 (MediaFire)</a>
                </div>

                <div class="overlay" style="position: fixed; width: 100vw; height: 100vh; z-index: 999999; opacity: 0;">
                    <a href="https://monetag.com/pop">Click Me</a>
                </div>
            </div>
        </body>
        </html>
        """
        cleaner = DomCleaner(base_url="https://distro-portal.org/page1")
        cleaner.feed(sample_html)

        self.assertEqual(cleaner.page_title, "Archive Download Portal")
        self.assertTrue(len(cleaner.stripped_ads) >= 2)
        stripped_targets = [a.target_url for a in cleaner.stripped_ads]
        self.assertTrue(any("adsterra.com" in u for u in stripped_targets))
        self.assertTrue(any("clickadu.com" in u for u in stripped_targets))

        clean_targets = [e.target_url for e in cleaner.elements]
        self.assertIn("https://distro-portal.org/downloads/ubuntu-24.04.iso", clean_targets)
        self.assertIn("https://mediafire.com/file/12345/ubuntu.iso", clean_targets)

        for elem in cleaner.elements:
            self.assertNotIn("adsterra.com", elem.target_url)
            self.assertNotIn("clickadu.com", elem.target_url)
            self.assertNotIn("monetag.com", elem.target_url)

    def test_form_post_extraction_with_hidden_tokens(self):
        sample_html = """
        <html>
        <body>
            <form method="POST" action="/get-file" id="dl-form">
                <input type="hidden" name="csrf_token" value="abc998877" />
                <input type="hidden" name="file_id" value="88219" />
                <input type="hidden" name="timestamp" value="1725700000" />
                <button type="submit" class="btn-submit">Generate Download Link</button>
            </form>
        </body>
        </html>
        """
        cleaner = DomCleaner(base_url="https://vikingfile.com")
        cleaner.feed(sample_html)

        self.assertEqual(len(cleaner.elements), 1)
        elem = cleaner.elements[0]
        self.assertEqual(elem.target_url, "https://vikingfile.com/get-file")
        self.assertEqual(elem.method, "POST")
        self.assertEqual(elem.text, "Generate Download Link")
        self.assertEqual(elem.form_inputs["csrf_token"], "abc998877")
        self.assertEqual(elem.form_inputs["file_id"], "88219")
        self.assertEqual(elem.form_inputs["timestamp"], "1725700000")

    def test_countdown_timer_detection(self):
        sample_html = """
        <html>
        <body>
            <div class="timer-box">
                <p>Your download link will be ready in 15 seconds...</p>
            </div>
            <a href="https://mediafire.com/file/abc">Wait...</a>
        </body>
        </html>
        """
        cleaner = DomCleaner()
        cleaner.feed(sample_html)
        self.assertTrue(cleaner.has_countdown_timer)
    def test_safelink_button_extraction_and_scoring(self):
        sample_html = """
        <html>
        <head>
            <title>Game Releases</title>
            <link rel="icon" href="/favicon.png" />
        </head>
        <body>
            <div class="ryuu-safelink-container">
                <button class="ryuu-sl-link-btn" data-link-key="1a_1" data-post-id="130735" data-shortcode-id="ryuu1">
                    Mediafire
                </button>
                <button class="ryuu-sl-link-btn" data-link-key="1a_2" data-post-id="130735" data-shortcode-id="ryuu1">
                    Mega
                </button>
            </div>
        </body>
        </html>
        """
        cleaner = DomCleaner(base_url="https://example.com/games/123")
        cleaner.feed(sample_html)
        cleaner.close()

        self.assertEqual(cleaner.favicon_url, "https://example.com/favicon.png")
        self.assertEqual(len(cleaner.elements), 2)
        btn1 = cleaner.elements[0]
        self.assertEqual(btn1.tag, "button")
        self.assertEqual(btn1.text, "Mediafire")
        self.assertEqual(btn1.method, "POST")
        self.assertEqual(btn1.target_url, "https://example.com/processing/")
        self.assertEqual(btn1.form_inputs["link_key"], "1a_1")
        self.assertEqual(btn1.form_inputs["post_id"], "130735")

        scored = ElementScorer.score_all(cleaner.elements, source_url="https://example.com/games/123")
        self.assertEqual(len(scored), 2)
        self.assertEqual(scored[0].category, "high_utility")
        self.assertEqual(scored[0].host, "mediafire.com")
        self.assertEqual(scored[1].category, "high_utility")
        self.assertEqual(scored[1].host, "mega.nz")



class TestElementScorer(unittest.TestCase):
    def test_high_utility_storage_hoster_scoring(self):
        elem = CleanedElement(
            tag="a",
            text="Download from MediaFire (1.2 GB)",
            target_url="https://mediafire.com/file/xyz123/game.iso",
            method="GET"
        )
        scored = ElementScorer.score_element(elem, source_url="https://forum.org/threads/1")

        # 40 (hoster) + 25 (download keyword) + 15 (size/filename) = 80
        self.assertGreaterEqual(scored.score, 70)
        self.assertEqual(scored.category, "high_utility")
        self.assertEqual(scored.host, "mediafire.com")
        self.assertEqual(scored.size_hint, "1.2 GB")
        self.assertEqual(scored.filename_hint, "game.iso")
        self.assertFalse(scored.is_shortlink)

    def test_candidate_scoring_generic_download(self):
        elem = CleanedElement(
            tag="a",
            text="Direct Link to Mirror",
            target_url="https://cdn.example.org/archive",
            method="GET"
        )
        scored = ElementScorer.score_element(elem, source_url="https://forum.org/threads/1")

        # 25 (keyword) = 25 -> secondary; with mirror keyword
        self.assertIn(scored.category, ["candidate", "secondary"])

    def test_shortlink_detection(self):
        elem = CleanedElement(
            tag="a",
            text="Click here to download",
            target_url="https://ouo.io/xreallcygo/abc",
            method="GET"
        )
        scored = ElementScorer.score_element(elem, source_url="https://forum.org/threads/1")
        self.assertTrue(scored.is_shortlink)

    def test_ad_element_penalty(self):
        elem = CleanedElement(
            tag="a",
            text="Fast Download Free",
            target_url="https://adsterra.com/click",
            is_ad=True
        )
        scored = ElementScorer.score_element(elem, source_url="https://forum.org/threads/1")
        self.assertLess(scored.score, 40)
        self.assertEqual(scored.category, "secondary")

    def test_score_all_sorts_descending(self):
        elements = [
            CleanedElement(tag="a", text="Home", target_url="https://example.org/home"),
            CleanedElement(tag="a", text="Download from Mega (700 MB)", target_url="https://mega.nz/file/abc"),
            CleanedElement(tag="a", text="Mirror 1", target_url="https://1fichier.com/?dl=123"),
        ]
        scored_list = ElementScorer.score_all(elements)
        self.assertEqual(len(scored_list), 3)
        self.assertGreaterEqual(scored_list[0].score, scored_list[1].score)
        self.assertGreaterEqual(scored_list[1].score, scored_list[2].score)
        self.assertEqual(scored_list[0].category, "high_utility")

    def test_multipart_grouping_and_false_positive_prevention(self):
        # 1. Multi-part parts on Mega (2 parts)
        elements = [
            CleanedElement(tag="a", text="game.part1.rar", target_url="https://mega.nz/file/p1"),
            CleanedElement(tag="a", text="game.part2.rar", target_url="https://mega.nz/file/p2"),
            # Solitary file that matches a number pattern but has no siblings
            CleanedElement(tag="a", text="solitary_update.001", target_url="https://pixeldrain.com/u/abc"),
        ]
        scored = ElementScorer.score_all(elements)
        mega_parts = [e for e in scored if e.host == "mega.nz" and e.is_multipart]
        self.assertEqual(len(mega_parts), 2)
        self.assertEqual(mega_parts[0].multipart_total, 2)
        self.assertEqual(mega_parts[0].package_name, "game")

        # Verify solitary file is NOT marked as multipart (zero false positives)
        pixel_single = next(e for e in scored if e.host == "pixeldrain.com")
        self.assertFalse(pixel_single.is_multipart)
        self.assertEqual(pixel_single.package_name, "")


class TestCrawlPageMatrixRPC(unittest.TestCase):
    @patch("engine.route_http.urlopen")
    def test_crawl_page_matrix_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = b"""
        <html>
        <head><title>File Repo Portal</title></head>
        <body>
            <div class="ad-banner"><a href="https://adsterra.com/fake">Fake</a></div>
            <a href="https://mediafire.com/file/test.zip">Download Zip (50 MB)</a>
            <a href="https://1fichier.com/?abc">Mirror (1fichier)</a>
        </body>
        </html>
        """
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_urlopen.return_value.__enter__.return_value = mock_response

        from engine.service import EngineService
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            service = EngineService(temp_dir)
            result = service.dispatch("crawl_page_matrix", {"url": "https://filerepo.com/downloads"})

            self.assertEqual(result["url"], "https://filerepo.com/downloads")
            self.assertEqual(result["title"], "File Repo Portal")
            self.assertEqual(result["ads_stripped"], 1)
            self.assertEqual(len(result["elements"]), 2)
            self.assertEqual(result["elements"][0]["category"], "high_utility")
            self.assertIn("mediafire.com", result["elements"][0]["target_url"])
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
