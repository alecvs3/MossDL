import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import unittest
from engine.plugins import PluginRegistry
from engine.providers.cyberdrop_hosts import (
    CYBERDROP_PROVIDERS, VikingfileProvider, FilesterProvider, KoofrProvider,
    BunkrProvider, CatboxProvider, PostImgProvider
)
from engine.providers.hosted import MediaFireProvider
from engine.providers.gofile import GofileProvider
from engine.errors import NeedsUser


class CyberdropProvidersTest(unittest.TestCase):
    def setUp(self):
        self.registry = PluginRegistry()

    def test_all_hosts_route_to_dedicated_plugins(self):
        sample_urls = [
            ("https://vikingfile.com/f/A94TpVDc66", "vikingfile"),
            ("https://vik1ngfile.site/f/A94TpVDc66", "vikingfile"),
            ("https://filester.me/d/sample-file", "filester"),
            ("https://koofr.eu/links/sample-link", "koofr"),
            ("https://app.koofr.net/links/abc", "koofr"),
            ("https://fileditchfiles.me/file.php?f=abc", "fileditch"),
            ("https://theditch.st/short123", "fileditch"),
            ("https://iceyfile.com/123", "iceyfile"),
            ("https://cyberfile.me/456", "cyberfile"),
            ("https://imglike.com/img/abc", "imglike"),
            ("https://imagepond.net/a/gallery", "imagepond"),
            ("https://imgbb.com/xyz", "imgbb"),
            ("https://ibb.co/xyz", "imgbb"),
            ("https://catbox.moe/c/abc", "catbox"),
            ("https://files.catbox.moe/abc.mp4", "catbox"),
            ("https://litter.catbox.moe/def.zip", "catbox"),
            ("https://www.upload.ee/files/123/name.html", "upload_ee"),
            ("https://bunkr.cr/v/12345", "bunkr"),
            ("https://bunkr.site/a/albumid", "bunkr"),
            ("https://anontransfer.com/file/123", "anontransfer"),
            ("https://pcloud.link/publink/show?code=abc", "pcloud"),
            ("https://pc.cd/abc", "pcloud"),
            ("https://1drv.ms/u/s!Amg...", "onedrive"),
            ("https://onedrive.live.com/?cid=123", "onedrive"),
            ("https://www.dropbox.com/s/sample/file.zip", "dropbox"),
            ("https://app.box.com/s/samplehash", "box"),
            ("https://postimg.cc/gallery/album123", "postimg"),
            ("https://i.postimg.cc/sample.png", "postimg"),
            ("https://imgbox.com/g/gallery", "imgbox"),
            ("https://imagebam.com/view/MEABC", "imagebam"),
            ("https://imx.to/i/123", "imx_to"),
            ("https://imagevenue.com/view/123", "imagevenue"),
            ("https://pixhost.to/show/123/img.jpg", "pixhost"),
            ("https://imgur.com/gallery/123", "imgur"),
            ("https://streamable.com/abc12", "streamable"),
            ("https://sendvid.com/vid123", "sendvid"),
            ("https://whyp.it/tracks/123", "whyp_it"),
            ("https://buzzheavier.com/f/sample", "buzzheavier"),
            ("https://pillowcase.su/f/sample", "pillowcase"),
            ("https://mixdrop.co/f/123", "mixdrop"),
            ("https://doodstream.com/d/123", "doodstream"),
            ("https://streamtape.com/v/123", "streamtape"),
            ("https://voe.sx/video123", "voe"),
            ("https://fuckingfast.co/file123", "fuckingfast"),
            ("https://webmshare.com/play/123", "webmshare"),
            ("https://send.now/file/123", "send_now"),
            ("https://giphy.com/gifs/sample", "giphy"),
            ("https://clyp.it/sampleaudio", "clyp_it"),
            ("https://bandcamp.com/track/sample", "bandcamp"),
            ("https://vipr.im/video/123", "vipr"),
            ("https://www.mediafire.com/file/abc/file.zip", "mediafire"),
            ("https://gofile.io/d/folder123", "gofile"),
            ("https://wetransfer.com/downloads/12345/67890", "wetransfer"),
            ("https://we.tl/t-12345", "wetransfer"),
            ("https://cloud.mail.ru/public/abc/xyz", "cloud_mail_ru"),
            ("https://disk.yandex.com/d/123", "yandex_disk"),
            ("https://yadi.sk/d/123", "yandex_disk"),
            ("https://www.rootz.so/file/abc-123", "rootz"),
            ("https://gupload.xyz/data/e/123", "gupload"),
            ("https://archive.org/details/identifier123", "archive_org"),
            ("https://nova.storage/d/file123", "nova_storage"),
            ("https://www.flickr.com/photos/user/123", "flickr"),
            ("https://vsco.co/user/media/123", "vsco"),
            ("https://x.com/user/status/123456", "twitter"),
            ("https://twitter.com/user/status/123456", "twitter"),
            ("https://www.tiktok.com/@user/video/123456", "tiktok"),
            ("https://www.pinterest.com/pin/123456/", "pinterest"),
            ("https://odysee.com/@channel:1/video:2", "odysee"),
            ("https://rumble.com/v123-title.html", "rumble"),
            ("https://www.dailymotion.com/video/x123abc", "dailymotion"),
            ("https://www.twitch.tv/videos/123456", "twitch"),
            ("https://forums.plex.tv/t/topic-title/1234", "discourse"),
        ]

        for url, expected_plugin_id in sample_urls:
            matched = self.registry.provider_for(url)
            self.assertEqual(
                matched, expected_plugin_id,
                f"URL '{url}' matched '{matched}' instead of expected '{expected_plugin_id}'"
            )

    def test_vikingfile_direct_and_turnstile(self):
        # Direct R2 link
        r2_url = "https://vikingfile.04b3d96d52475741e6b10f97f0a84a16.r2.cloudflarestorage.com/download.zip"
        resolved = VikingfileProvider.resolve(r2_url)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].direct_url, r2_url)
        self.assertEqual(resolved[0].display_name, "download.zip")

        # Direct /d/ link
        direct_url = "https://vikingfile.com/d/abc123token/sample.mp4"
        resolved = VikingfileProvider.resolve(direct_url)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].display_name, "sample.mp4")

    def test_mediafire_keys_and_scrambled_decoding(self):
        fkey, quickkey = MediaFireProvider._parse_keys("https://www.mediafire.com/folder/abc123xyz/Documents")
        self.assertEqual(fkey, "abc123xyz")
        self.assertIsNone(quickkey)

        fkey, quickkey = MediaFireProvider._parse_keys("https://www.mediafire.com/file/xyz789quick/photo.jpg")
        self.assertIsNone(fkey)
        self.assertEqual(quickkey, "xyz789quick")

        # Scrambled URL decoding
        import base64
        real_url = "https://download123.mediafire.com/testfile.zip"
        scrambled = base64.b64encode(real_url.encode()).decode()
        html = f'<html><body><a id="downloadButton" data-scrambled-url="{scrambled}" href="#">Download</a></body></html>'.encode()
        direct = MediaFireProvider._extract_direct_from_html(html, "https://www.mediafire.com/file/xyz")
        self.assertEqual(direct, real_url)


    def test_wetransfer_payload_and_api(self):
        from unittest.mock import patch
        from engine.providers.cyberdrop_hosts import WeTransferProvider
        fake_resp = {"direct_link": "https://download.wetransfer.com/file123/archive.zip"}
        with patch("engine.providers.cyberdrop_hosts._post_json", return_value=fake_resp) as mock_post:
            items = WeTransferProvider.resolve("https://wetransfer.com/downloads/file123/recipient456/hash789")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].direct_url, "https://download.wetransfer.com/file123/archive.zip")
            self.assertEqual(items[0].display_name, "archive.zip")
            self.assertIn("file123", mock_post.call_args[0][0])

    def test_rootz_resolve_payload(self):
        from unittest.mock import patch
        from engine.providers.cyberdrop_hosts import RootzProvider
        fake_resp = {"data": {"filename": "test.rar", "url": "https://cdn.rootz.so/dl/123", "size": 1024}}
        with patch("engine.providers.cyberdrop_hosts._json", return_value=fake_resp):
            items = RootzProvider.resolve("https://www.rootz.so/file/abc-123")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].direct_url, "https://cdn.rootz.so/dl/123")
            self.assertEqual(items[0].display_name, "test.rar")
            self.assertEqual(items[0].size, 1024)

    def test_archive_org_resolve(self):
        from unittest.mock import patch
        from engine.providers.cyberdrop_hosts import ArchiveOrgProvider
        direct_url = "https://archive.org/download/item123/file.mp3"
        items = ArchiveOrgProvider.resolve(direct_url)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].direct_url, direct_url)
        self.assertEqual(items[0].display_name, "file.mp3")

    def test_twitter_resolve_mock(self):
        from unittest.mock import patch
        from engine.providers.cyberdrop_hosts import TwitterProvider
        fake_fxtwitter = {
            "tweet": {
                "media": {
                    "videos": [{"url": "https://video.twimg.com/ext_tw_video/123.mp4"}],
                    "photos": [{"url": "https://pbs.twimg.com/media/pic1.jpg"}]
                }
            }
        }
        with patch("engine.providers.cyberdrop_hosts._json", return_value=fake_fxtwitter):
            items = TwitterProvider.resolve("https://x.com/user/status/123456789")
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0].direct_url, "https://video.twimg.com/ext_tw_video/123.mp4")
            self.assertEqual(items[1].direct_url, "https://pbs.twimg.com/media/pic1.jpg")


if __name__ == "__main__":
    unittest.main()
