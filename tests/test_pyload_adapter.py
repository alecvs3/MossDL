import unittest
from engine.pyload_adapter import PyLoadHosterStub, PyLoadPluginAdapter
from engine.errors import ProviderMappedError


class SampleRapidgatorHoster(PyLoadHosterStub):
    __name__ = "RapidgatorNet"
    __version__ = "0.50"
    __pattern__ = r"https?://(?:www\.)?rapidgator\.net/file/(?P<id>[a-zA-Z0-9]+)"

    def process(self, url):
        # Simulated extraction logic
        if "offline" in url:
            self.offline()
        self.filename = "presentation.pptx"
        self.filesize = 5242880
        return f"https://pr.rapidgator.net/dl/{url.split('/')[-1]}/presentation.pptx"


class TestPyLoadAdapter(unittest.TestCase):
    def test_pattern_matching(self):
        adapter = PyLoadPluginAdapter(SampleRapidgatorHoster)
        self.assertTrue(adapter.can_handle("https://rapidgator.net/file/8b9281a8c9"))
        self.assertTrue(adapter.can_handle("http://www.rapidgator.net/file/abcdef123"))
        self.assertFalse(adapter.can_handle("https://youtube.com/watch?v=12345"))

    def test_resolve_success(self):
        adapter = PyLoadPluginAdapter(SampleRapidgatorHoster)
        items = adapter.resolve({"url": "https://rapidgator.net/file/8b9281a8c9"})
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["provider"], "rapidgatornet")
        self.assertEqual(item["display_name"], "presentation.pptx")
        self.assertEqual(item["size"], 5242880)
        self.assertEqual(item["direct_url"], "https://pr.rapidgator.net/dl/8b9281a8c9/presentation.pptx")

    def test_resolve_offline(self):
        adapter = PyLoadPluginAdapter(SampleRapidgatorHoster)
        with self.assertRaises(ProviderMappedError) as ctx:
            adapter.resolve({"url": "https://rapidgator.net/file/offline123"})
        self.assertEqual(ctx.exception.category, "not_found")


if __name__ == "__main__":
    unittest.main()
