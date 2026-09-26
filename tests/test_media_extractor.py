import unittest
from engine.media_extractor import MediaExtractor
from engine.universal_resolver import UniversalResolver
from engine.models import ResolvedItem


class MediaExtractorTests(unittest.TestCase):
    def setUp(self):
        self.extractor = MediaExtractor()

    def test_media_extractor_availability(self):
        self.assertTrue(self.extractor.available)

    def test_can_handle_detects_known_media_domains(self):
        # Known streaming / video sites handled by yt-dlp
        self.assertTrue(self.extractor.can_handle("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertTrue(self.extractor.can_handle("https://vimeo.com/76979871"))
        self.assertTrue(self.extractor.can_handle("https://soundcloud.com/octobersveryown/drake-gods-plan"))
        
        # Obvious direct file links or random text should not match a specialized extractor
        self.assertFalse(self.extractor.can_handle("https://example.com/file.zip"))
        self.assertFalse(self.extractor.can_handle(""))

    def test_universal_resolver_delegates_to_media_extractor(self):
        class DummyExtractor:
            def can_handle(self, url):
                return "stream-test" in url

            def extract(self, url, select_format="best"):
                return {
                    "source_url": url,
                    "title": "Stream Test Video",
                    "duration": 120.0,
                    "thumbnail": "https://stream-test.com/thumb.jpg",
                    "selected_url": "https://stream-test.com/direct_video.mp4",
                    "formats": [{"resolution": "1080p", "url": "https://stream-test.com/direct_video.mp4"}],
                    "resolved_item": ResolvedItem(
                        provider="media",
                        source_url=url,
                        display_name="Stream Test Video.mp4",
                        direct_url="https://stream-test.com/direct_video.mp4",
                        size=50000000,
                    )
                }

        resolver = UniversalResolver(media_extractor=DummyExtractor())
        res = resolver.inspect("https://stream-test.com/watch?v=123")
        self.assertEqual(res["title"], "Stream Test Video")
        self.assertEqual(res["final_url"], "https://stream-test.com/direct_video.mp4")
        self.assertEqual(len(res["candidates"]), 1)
        self.assertEqual(res["candidates"][0]["confidence"], 0.95)
        self.assertEqual(res["candidates"][0]["source"], "media_extractor")


if __name__ == "__main__":
    unittest.main()
