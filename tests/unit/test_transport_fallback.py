"""The policy deciding when a failed transfer is worth a second transport."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.errors import DownloadCanceled, DownloadPaused
from engine.reliability import TransportSignal, TransportSignalError
from engine.transport_fallback import classify_failure, fallback_backend


class ClassifyFailureTests(unittest.TestCase):
    def test_user_intent_is_never_retried_elsewhere(self):
        # Retrying a pause on a second transport would restart the transfer the
        # user just stopped.
        for error in (DownloadPaused("paused"), DownloadCanceled("canceled")):
            decision = classify_failure(error)
            self.assertFalse(decision.should_fallback, type(error).__name__)
            self.assertIn("paused or canceled", decision.reason)

    def test_server_answers_are_not_transport_defects(self):
        # A 429 or an auth rejection is what the server decided; a different
        # client gets told exactly the same thing.
        for category, status in (("throttle", 429), ("auth", 401)):
            signal = TransportSignal(category=category, status_code=status, evidence="server said no")
            decision = classify_failure(TransportSignalError(signal, f"{category.upper()}:{status}:0"))
            self.assertFalse(decision.should_fallback, category)

    def test_expired_sources_are_left_to_the_refresh_loop(self):
        decision = classify_failure(RuntimeError("download link has expired"))
        self.assertFalse(decision.should_fallback)
        self.assertIn("refreshing the URL", decision.reason)

    def test_transport_failures_are_worth_a_second_attempt(self):
        # A TLS rejection is the case the second transport exists for: rustls
        # refuses legacy TLS that Python's stack accepts.
        decision = classify_failure(RuntimeError("invalid peer certificate: UnsupportedCertVersion"))
        self.assertTrue(decision.should_fallback)
        self.assertIn("RuntimeError", decision.reason)
        self.assertIn("UnsupportedCertVersion", decision.reason)

    def test_reason_is_bounded_and_single_line(self):
        decision = classify_failure(RuntimeError("a" * 500 + "\n\nsecond line"))
        self.assertLessEqual(len(decision.reason), 240)
        self.assertNotIn("\n", decision.reason)

    def test_reason_survives_an_empty_message(self):
        decision = classify_failure(RuntimeError())
        self.assertTrue(decision.should_fallback)
        self.assertEqual(decision.reason, "RuntimeError")


class FallbackBackendTests(unittest.TestCase):
    def test_rust_falls_back_to_custom(self):
        self.assertEqual(fallback_backend("rust", {"custom": True}), "custom")

    def test_custom_falls_back_to_rust(self):
        self.assertEqual(fallback_backend("custom", {"rust": True}), "rust")

    def test_an_incompatible_alternate_is_not_offered(self):
        # No second transport is better than one that cannot handle the item.
        self.assertIsNone(fallback_backend("rust", {"custom": False}))

    def test_the_primary_is_never_its_own_fallback(self):
        self.assertIsNone(fallback_backend("rust", {"rust": True}))


if __name__ == "__main__":
    unittest.main()
