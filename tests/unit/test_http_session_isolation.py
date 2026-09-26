"""Two files from one hoster must not share a cookie jar.

The session pool was keyed only by impersonation profile, so every task in the
engine shared one jar. DataNodes registers the pending download server-side
against the session cookie, so two parts posting step one into that jar
overwrite each other: the last POST wins, and the losers read a sibling's page.

Downstream that presented three different ways, all of which we chased
separately before finding this: a missing `rand` in the step-two form (reported
as `PrimaryFlowUnsupported`, so the plugin fell back to the legacy provider), a
rejected Turnstile token, and a task that resolved forever without ever
transferring a byte.
"""

from __future__ import annotations

import unittest

from engine import http_client


class SessionPoolKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        if not http_client._HAVE_CURL_CFFI:
            self.skipTest("curl_cffi not installed; session pool is inactive")
        self._saved = dict(http_client._SESSION_POOL)
        http_client._SESSION_POOL.clear()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        http_client._SESSION_POOL.clear()
        http_client._SESSION_POOL.update(self._saved)

    def test_distinct_keys_get_distinct_sessions(self) -> None:
        """The fix: one jar per file, so pending downloads cannot collide."""
        a = http_client._get_session("chrome131", "datanodes:aaa111")
        b = http_client._get_session("chrome131", "datanodes:bbb222")
        self.assertIsNot(a, b, "two files shared one hoster download session")

    def test_same_key_is_sticky(self) -> None:
        """Landing -> step one -> step two for one file must keep its cookies."""
        a = http_client._get_session("chrome131", "datanodes:aaa111")
        b = http_client._get_session("chrome131", "datanodes:aaa111")
        self.assertIs(a, b, "a file's own sequence lost its session cookies mid-flow")

    def test_unkeyed_callers_still_share_one_session(self) -> None:
        a = http_client._get_session("chrome131")
        b = http_client._get_session("chrome131")
        self.assertIs(a, b)

    def test_keyed_and_unkeyed_are_separate(self) -> None:
        shared = http_client._get_session("chrome131")
        keyed = http_client._get_session("chrome131", "datanodes:aaa111")
        self.assertIsNot(shared, keyed)

    def test_impersonation_still_separates_sessions(self) -> None:
        a = http_client._get_session("chrome131", "datanodes:aaa111")
        b = http_client._get_session("safari17", "datanodes:aaa111")
        self.assertIsNot(a, b)

    def test_keyed_sessions_are_bounded(self) -> None:
        """A long-lived engine must not hold one session per file it ever saw."""
        for i in range(http_client._MAX_KEYED_SESSIONS + 25):
            http_client._get_session("chrome131", f"datanodes:file{i}")
        keyed = [k for k in http_client._SESSION_POOL if k[1] != http_client._SHARED_SESSION_KEY]
        self.assertLessEqual(len(keyed), http_client._MAX_KEYED_SESSIONS)

    def test_eviction_is_least_recently_used(self) -> None:
        first = http_client._get_session("chrome131", "datanodes:keepme")
        for i in range(http_client._MAX_KEYED_SESSIONS - 1):
            http_client._get_session("chrome131", f"datanodes:filler{i}")
        # Touch the original so it is the most recent, then overflow by one.
        http_client._get_session("chrome131", "datanodes:keepme")
        http_client._get_session("chrome131", "datanodes:overflow")
        self.assertIs(
            http_client._get_session("chrome131", "datanodes:keepme"), first,
            "an in-flight flow was evicted while an idle one survived")

    def test_shared_session_survives_eviction_pressure(self) -> None:
        shared = http_client._get_session("chrome131")
        for i in range(http_client._MAX_KEYED_SESSIONS + 25):
            http_client._get_session("chrome131", f"datanodes:file{i}")
        self.assertIs(http_client._get_session("chrome131"), shared)

    def test_drop_session_releases_every_profile_for_that_key(self) -> None:
        http_client._get_session("chrome131", "datanodes:aaa111")
        http_client._get_session("safari17", "datanodes:aaa111")
        http_client._get_session("chrome131", "datanodes:bbb222")
        http_client.drop_session("datanodes:aaa111")
        remaining = {k[1] for k in http_client._SESSION_POOL}
        self.assertNotIn("datanodes:aaa111", remaining)
        self.assertIn("datanodes:bbb222", remaining)

    def test_drop_session_ignores_the_shared_key(self) -> None:
        shared = http_client._get_session("chrome131")
        http_client.drop_session("")
        self.assertIs(http_client._get_session("chrome131"), shared)


class ProviderThreadsTheKeyTests(unittest.TestCase):
    """The plumbing: a per-file jar is useless if the provider never asks for one."""

    def test_datanodes_provider_keys_its_requests_by_file(self) -> None:
        import inspect

        from plugins.datanodes import provider

        src = inspect.getsource(provider)
        self.assertIn("session_key=jar", src,
                      "the DataNodes landing/step-one requests dropped back to the shared jar")

    def test_datanodes_step_two_uses_the_same_jar(self) -> None:
        import inspect

        from engine.providers import datanodes_resume

        src = inspect.getsource(datanodes_resume)
        self.assertIn('session_key=f"datanodes:{file_code}"', src,
                      "step two posted into a different session than step one, so the "
                      "hoster had no pending download for it")


if __name__ == "__main__":
    unittest.main()
