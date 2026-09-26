"""Concurrent solves must not share a hoster download session.

DataNodes registers a pending download server-side against the session cookie
and then serves every file from one shared URL (`/download`). Three solver lanes
sharing a browser context therefore share that pending download: the last
step-one POST wins and the other lanes read a sibling's page. Turnstile still
passes, because clearance is domain-wide, so the solve reports success while the
per-file link is silently lost -- and the task re-resolves forever.

Observed live on 2026-09-18 (reports/live/20260918_232707_Trails): parts 05, 06
and 08 each navigated 4-5 times, 21 solves against 15 distinct challenges, and
not one byte transferred for those parts.
"""

from __future__ import annotations

import unittest

from engine import browser_solver
from engine.browser_solver import direct_url_matches_request


PART05 = "https://datanodes.to/n69czxcv3m3v/Trails_in_the_Sky_2nd_Chapter_--_example-repacks.test_--_.part05.rar"
PART09 = "https://tunnel5.dlproxy.uk/d/abc123/Trails_in_the_Sky_2nd_Chapter_--_example-repacks.test_--_.part09.rar?ttl=900"
PART05_DIRECT = "https://tunnel5.dlproxy.uk/d/abc123/Trails_in_the_Sky_2nd_Chapter_--_example-repacks.test_--_.part05.rar?ttl=900"


class DirectUrlMatchTests(unittest.TestCase):
    def test_matching_part_is_accepted(self) -> None:
        self.assertTrue(direct_url_matches_request(PART05, PART05_DIRECT))

    def test_sibling_part_is_rejected(self) -> None:
        """The corruption case: a valid link for the wrong volume.

        No size or checksum check can catch this -- the bytes are a complete,
        correct volume. Only the filename disagrees.
        """
        self.assertFalse(direct_url_matches_request(PART05, PART09))

    def test_percent_encoding_is_decoded_before_comparing(self) -> None:
        encoded = PART05_DIRECT.replace("_--_", "%5F--%5F")
        self.assertTrue(direct_url_matches_request(PART05, encoded))

    def test_case_differences_do_not_reject(self) -> None:
        self.assertTrue(direct_url_matches_request(PART05, PART05_DIRECT.upper()))

    def test_query_string_is_ignored(self) -> None:
        self.assertTrue(
            direct_url_matches_request(PART05, PART05_DIRECT + "&token=xyz&expires=1"))

    def test_shortlink_without_a_filename_is_allowed_through(self) -> None:
        """An id-only hoster page carries nothing to compare against."""
        self.assertTrue(direct_url_matches_request("https://datanodes.to/n69czxcv3m3v", PART05_DIRECT))

    def test_opaque_direct_url_is_allowed_through(self) -> None:
        self.assertTrue(
            direct_url_matches_request(PART05, "https://tunnel5.dlproxy.uk/stream/9f2a1c"))

    def test_empty_inputs_do_not_raise(self) -> None:
        self.assertTrue(direct_url_matches_request("", ""))
        self.assertTrue(direct_url_matches_request(PART05, ""))


class SharedContextRegressionTests(unittest.TestCase):
    """The structural guarantee, asserted on the source itself.

    A unit test cannot launch Chromium, but it can hold the line that no shared
    context field comes back: that field is precisely what made three lanes share
    one cookie jar.
    """

    def test_solver_keeps_no_shared_browser_context(self) -> None:
        import inspect

        src = inspect.getsource(browser_solver.BrowserSolverDaemon.__init__)
        self.assertNotIn(
            "self._context", src,
            "a process-wide browser context is back; concurrent solves would "
            "again share one hoster download session")

    def test_each_solve_opens_its_own_context(self) -> None:
        import inspect

        src = inspect.getsource(browser_solver.BrowserSolverDaemon._async_execute_solve)
        self.assertIn(
            "browser.new_context(", src,
            "the solve no longer creates a private context per lane")
        self.assertIn(
            "ctx.close()", src,
            "a per-solve context is created but never closed; contexts would leak "
            "for the browser's whole lifetime")


if __name__ == "__main__":
    unittest.main()
