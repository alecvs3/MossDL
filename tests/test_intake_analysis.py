import unittest
from email.message import Message

from engine import intake_analysis as ia


def _inspect(mapping):
    def inspect(url):
        provider = next((pid for host, pid in mapping.items() if host in url), "generic")
        return {"provider_id": provider, "display_name": provider.title(),
                "ui_mode": "tree_picker" if provider == "gofile" else "download"}
    return inspect


def analyze(url, *, providers=None, shortlinks=(), unwrap=None, probe=None, crawl=None):
    providers = providers or {"datanodes.to": "datanodes", "gofile.io": "gofile"}
    inspect = _inspect(providers)

    def fail(*_args):
        raise AssertionError("should not be called")

    return ia.analyze_link(
        url,
        inspect=inspect,
        provider_for=lambda target: inspect(target)["provider_id"],
        is_shortlink=lambda target: any(host in target for host in shortlinks),
        unwrap_shortlink=unwrap or fail,
        probe=probe or fail,
        crawl=crawl or fail,
    )


class TestAnalyzeLink(unittest.TestCase):
    def test_known_hosts_skip_the_network(self):
        self.assertEqual(analyze("https://datanodes.to/abc/file.rar")["kind"], "hoster")
        self.assertEqual(analyze("https://gofile.io/d/abc")["kind"], "folder")

    def test_magnets_are_explained_not_attempted(self):
        result = analyze("magnet:?xt=urn:btih:abc")
        self.assertEqual(result["kind"], "unsupported")
        self.assertIn("Magnet", result["message"])

    def test_direct_file_reports_name_and_size(self):
        result = analyze("https://cdn.example/f.zip", probe=lambda url: {
            "ok": True, "final_url": url, "is_page": False, "filename": "f.zip",
            "size": 42, "content_type": "application/zip"})
        self.assertEqual(result["kind"], "file")
        self.assertEqual(result["file"], {"name": "f.zip", "size": 42, "mime": "application/zip"})

    def test_page_is_crawled_and_download_links_get_providers(self):
        crawl = lambda url: {"title": "Game: Deluxe?", "elements": [
            {"id": "1", "category": "high_utility", "target_url": "https://datanodes.to/x/a.part1.rar"},
            {"id": "2", "category": "secondary", "target_url": "https://site.example/about"},
            {"id": "3", "category": "secondary", "target_url": "magnet:?xt=urn:btih:abc"},
        ]}
        result = analyze("https://site.example/game/", crawl=crawl, probe=lambda url: {
            "ok": True, "final_url": url, "is_page": True, "content_type": "text/html"})
        self.assertEqual(result["kind"], "page")
        elements = {element["id"]: element for element in result["page"]["elements"]}
        self.assertEqual(elements["1"]["provider_id"], "datanodes")
        self.assertNotIn("provider_id", elements["2"])
        self.assertTrue(elements["3"]["unsupported"])
        # Page titles become safe file names for "save the page itself".
        self.assertEqual(result["page"]["save_as"], "Game  Deluxe.html")

    def test_redirect_onto_a_file_host_uses_its_provider(self):
        result = analyze("https://redirect.example/go", probe=lambda url: {
            "ok": True, "final_url": "https://datanodes.to/x/file.rar", "is_page": True})
        self.assertEqual(result["kind"], "hoster")
        self.assertEqual(result["url"], "https://datanodes.to/x/file.rar")
        self.assertEqual(result["redirected_from"], "https://redirect.example/go")

    def test_shortlinks_are_unwrapped_then_classified(self):
        result = analyze("https://ouo.io/abc", shortlinks=("ouo.io",),
                         unwrap=lambda url: ("https://datanodes.to/x/file.rar", [{}, {}]))
        self.assertEqual(result["kind"], "hoster")
        self.assertEqual(result["via_shortlink"], {"url": "https://ouo.io/abc", "hops": 2})

    def test_shortlink_failure_can_still_be_added(self):
        def unwrap(_url):
            raise TimeoutError("captcha wall")
        result = analyze("https://ouo.io/abc", shortlinks=("ouo.io",), unwrap=unwrap)
        self.assertEqual(result["kind"], "shortlink")
        self.assertIn("unwrapped when the download starts", result["message"])

    def test_unreachable_link_is_unknown(self):
        result = analyze("https://gone.example/x", probe=lambda url: {"ok": False, "error": "no route"})
        self.assertEqual(result["kind"], "unknown")
        self.assertIn("no route", result["message"])

    def test_crawl_failure_still_offers_saving_the_page(self):
        def crawl(_url):
            raise RuntimeError("blocked")
        result = analyze("https://site.example/", crawl=crawl, probe=lambda url: {
            "ok": True, "final_url": url, "is_page": True})
        self.assertEqual(result["kind"], "page")
        self.assertEqual(result["page"]["elements"], [])
        self.assertEqual(result["page"]["save_as"], "site.example.html")


class TestHeaderParsing(unittest.TestCase):
    def test_content_disposition_wins_over_url(self):
        headers = Message()
        headers["Content-Disposition"] = 'attachment; filename="Real Name.zip"'
        self.assertEqual(ia._filename_from_headers(headers, "https://x/download.php?id=5"), "Real Name.zip")

    def test_url_without_extension_gives_no_name(self):
        self.assertIsNone(ia._filename_from_headers(Message(), "https://x/download"))
        self.assertEqual(ia._filename_from_headers(Message(), "https://x/files/a%20b.bin"), "a b.bin")


if __name__ == "__main__":
    unittest.main()
