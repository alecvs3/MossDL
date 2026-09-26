import unittest
from engine.cms_signatures import CMSRecipeEngine
from engine.universal_resolver import UniversalResolver


class TestCMSRecipes(unittest.TestCase):
    def setUp(self):
        self.cms_engine = CMSRecipeEngine()

    def test_detect_xfilesharing(self):
        html = """
        <html>
        <head><title>Download file</title></head>
        <body>
            <form method="POST">
                <input type="hidden" name="op" value="download2">
                <input type="hidden" name="id" value="xyz789">
                <input type="hidden" name="fname" value="archive.tar.gz">
                <input type="hidden" name="size" value="1048576">
                <a href="https://dl.xfile.co/d/xyz789/archive.tar.gz">Download Now</a>
            </form>
            <div>Powered by XFileSharing / SibSoft</div>
        </body>
        </html>
        """
        detected = self.cms_engine.detect(html)
        self.assertEqual(detected, "xfilesharing")

        resolved = self.cms_engine.resolve("xfilesharing", "https://xfile.co/xyz789", page_html=html)
        self.assertTrue(len(resolved) > 0)
        self.assertEqual(resolved[0]["display_name"], "archive.tar.gz")
        self.assertEqual(resolved[0]["direct_url"], "https://dl.xfile.co/d/xyz789/archive.tar.gz")
        self.assertEqual(resolved[0]["size"], 1048576)

    def test_detect_yetishare(self):
        html = """
        <html>
        <head>
            <meta name="title" content="sample_document.pdf">
        </head>
        <body>
            <form>
                <input type="hidden" name="fileId" value="456">
                <a href="https://yetishare.net/download/token123" class="btn-free-download">Download</a>
                <span class="file-size">2048000</span>
            </form>
            <div class="yetishare-footer">YetiShare File Hosting</div>
        </body>
        </html>
        """
        detected = self.cms_engine.detect(html)
        self.assertEqual(detected, "yetishare")

        resolved = self.cms_engine.resolve("yetishare", "https://yetishare.net/456", page_html=html)
        self.assertTrue(len(resolved) > 0)
        self.assertEqual(resolved[0]["display_name"], "sample_document.pdf")
        self.assertEqual(resolved[0]["direct_url"], "https://yetishare.net/download/token123")
        self.assertEqual(resolved[0]["size"], 2048000)

    def test_universal_resolver_with_cms(self):
        html = """
        <html>
        <body>
            <input type="hidden" name="op" value="download2">
            <input type="hidden" name="fname" value="universal_test.zip">
            <a href="https://server.download/d/abc/universal_test.zip">Download Link</a>
            <div>xfilesharing host</div>
        </body>
        </html>
        """
        resolver = UniversalResolver(fetcher=lambda url: ("text/html", html))
        inspected = resolver.inspect("https://unknown-hoster.org/abc")
        self.assertEqual(inspected.get("cms"), "xfilesharing")
        self.assertEqual(inspected["candidates"][0]["filename"], "universal_test.zip")
        self.assertEqual(inspected["candidates"][0]["url"], "https://server.download/d/abc/universal_test.zip")


if __name__ == "__main__":
    unittest.main()
