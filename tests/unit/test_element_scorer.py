from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import unittest

from engine.dom_cleaner import CleanedElement
from engine.element_scorer import ElementScorer

PAGE = "https://www.site.test/some-post/"


def score(tag: str, text: str, url: str, method: str = "GET") -> int:
    return ElementScorer.score_element(CleanedElement(tag=tag, text=text, target_url=url, method=method), source_url=PAGE).score


class ElementScorerTests(unittest.TestCase):
    def test_mirror_buttons_score_alike_whatever_the_spelling(self) -> None:
        # Mirror buttons that post to the site's own "processing" page.
        names = ["Mediafire", "MegaNZ", "PixelDrain", "Datanodes", "VikingFile"]
        scores = {n: score("button", n, "https://www.site.test/processing/", "POST") for n in names}
        self.assertEqual(len(set(scores.values())), 1, scores)

    def test_sites_own_file_server_outranks_plain_links(self) -> None:
        own = score("a", "RyuuCloud Direct Download", "http://cloud.site.test/2026/09/RJ01")
        plain = score("a", "Direct Download", "https://www.site.test/somewhere")
        self.assertGreater(own, plain)

    def test_share_links_are_never_promising(self) -> None:
        self.assertEqual(score("a", "X", "https://x.com/intent/post?text=Game+v1.2+download"), 0)
        self.assertEqual(score("a", "Pinterest", "https://pinterest.com/pin/create/button/?url=x"), 0)


if __name__ == "__main__":
    unittest.main()
