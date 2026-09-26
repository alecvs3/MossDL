"""Anti-bot clearance must survive the engine -> plugin-worker process hop.

Providers run in a separate process with their own `clearance_cache`. Before
this was wired, clearance won by solving part 1 of a package never reached the
worker resolving part 2, so every sibling re-challenged (`clearance_hit=False`
on every provider request) and the "solve once for the whole package" contract
could never hold.
"""

from __future__ import annotations

import unittest

from engine.http_client import clearance_cache
from engine.plugin_worker import _seed_clearance
from engine.plugins import PluginRegistry


COOKIES = {"cf_clearance": "abc123", "PHPSESSID": "sess456"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TestAgent/1.0"
URL = "https://datanodes.to/ffq3ck038tbm/Package_--_site_--_.part2.rar"


class ClearanceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        clearance_cache.clear()
        self.addCleanup(clearance_cache.clear)

    def test_snapshot_is_none_without_cached_clearance(self) -> None:
        self.assertIsNone(PluginRegistry._clearance_for({"url": URL}))

    def test_snapshot_is_none_without_a_url(self) -> None:
        clearance_cache.set_clearance(URL, COOKIES, UA)
        self.assertIsNone(PluginRegistry._clearance_for({"secrets": {}}))

    def test_snapshot_carries_cookies_and_user_agent(self) -> None:
        clearance_cache.set_clearance(URL, COOKIES, UA)
        snapshot = PluginRegistry._clearance_for({"url": URL})
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["domain"], "datanodes.to")
        self.assertEqual(snapshot["cookies"], COOKIES)
        self.assertEqual(snapshot["user_agent"], UA)
        self.assertGreater(snapshot["ttl_remaining"], 0)

    def test_snapshot_resolves_the_url_from_a_refresh_item(self) -> None:
        clearance_cache.set_clearance(URL, COOKIES, UA)
        snapshot = PluginRegistry._clearance_for({"item": {"source_url": URL}})
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["cookies"], COOKIES)


class ClearanceReplantTests(unittest.TestCase):
    """The worker half: a snapshot must rebuild a usable cache entry."""

    def setUp(self) -> None:
        clearance_cache.clear()
        self.addCleanup(clearance_cache.clear)

    def _snapshot(self) -> dict:
        clearance_cache.set_clearance(URL, COOKIES, UA)
        snapshot = PluginRegistry._clearance_for({"url": URL})
        # Stand in for the process boundary: the worker starts cold.
        clearance_cache.clear()
        self.assertIsNone(clearance_cache.get_clearance(URL))
        return snapshot

    def test_seeding_makes_the_host_a_clearance_hit(self) -> None:
        _seed_clearance(self._snapshot())
        entry = clearance_cache.get_clearance(URL)
        self.assertIsNotNone(entry, "worker cache is still cold after seeding")
        self.assertEqual(entry.cookies, COOKIES)
        self.assertEqual(entry.user_agent, UA)

    def test_seeding_is_idempotent(self) -> None:
        snapshot = self._snapshot()
        _seed_clearance(snapshot)
        first = clearance_cache.get_clearance(URL)
        _seed_clearance(snapshot)
        self.assertIs(clearance_cache.get_clearance(URL), first,
                      "re-seeding replaced a valid entry; every request would log CLEARANCE_CACHED")

    def test_expired_snapshot_is_refused(self) -> None:
        snapshot = self._snapshot()
        snapshot["ttl_remaining"] = 0.0
        _seed_clearance(snapshot)
        self.assertIsNone(clearance_cache.get_clearance(URL),
                          "an expired snapshot must not be replanted as live clearance")

    def test_snapshot_without_cookies_is_refused(self) -> None:
        snapshot = self._snapshot()
        snapshot["cookies"] = {}
        _seed_clearance(snapshot)
        self.assertIsNone(clearance_cache.get_clearance(URL))

    def test_round_trip_survives_json(self) -> None:
        import json

        snapshot = json.loads(json.dumps(self._snapshot()))
        _seed_clearance(snapshot)
        entry = clearance_cache.get_clearance(URL)
        self.assertIsNotNone(entry, "snapshot did not survive JSON-RPC serialization")
        self.assertEqual(entry.cookies, COOKIES)


if __name__ == "__main__":
    unittest.main()
