from __future__ import annotations

import json
import statistics
import time
import unittest
from pathlib import Path

from engine.challenge_classifier import CATALOG_VERSION, classify, observe_received_response, response_observation
from engine.http_client import HttpResponseAdapter


class ChallengeClassifierTests(unittest.TestCase):
    def test_catalog_is_deterministic_and_turnstile_is_typed(self) -> None:
        fixtures = json.loads((Path(__file__).parents[1] / "fixtures/challenges/catalog-v1.json").read_text())["fixtures"]
        observed = {}
        for fixture in fixtures:
            verdict = classify(response_observation(
                status=fixture["status"], headers={"content-type": fixture["content_type"]},
                final_url="https://example.test/download", body_prefix=fixture["body"].encode(),
            ))
            observed[fixture["name"]] = verdict
        turnstile = observed["turnstile"]
        self.assertEqual(CATALOG_VERSION, "challenge-rules/1")
        self.assertEqual(turnstile.family, "cloudflare")
        self.assertEqual(turnstile.challenge_type, "turnstile")
        self.assertEqual(turnstile.required_capability, "widget_token")
        self.assertTrue(turnstile.automation_eligible)
        self.assertTrue(turnstile.evidence)
        for name in ("rate_limit", "authentication", "ordinary_html", "binary"):
            self.assertFalse(observed[name].automation_eligible, name)

    def test_http_adapter_keeps_read_contract(self) -> None:
        adapter = HttpResponseAdapter(b"<div class='cf-turnstile'></div>", 403, "https://example.test", {"Content-Type": "text/html"})
        self.assertTrue(adapter.challenge_verdict.automation_eligible)
        self.assertEqual(adapter.read(), b"<div class='cf-turnstile'></div>")

    def test_observer_has_bounded_header_and_html_cost(self) -> None:
        header_samples, html_samples = [], []
        html = b"<div class='cf-turnstile'></div>" + b"x" * (64 * 1024)
        for _ in range(30):
            start = time.perf_counter()
            observe_received_response(source="test.header", status=200, headers={"content-type": "application/octet-stream"}, final_url="https://example.test")
            header_samples.append((time.perf_counter() - start) * 1000)
            start = time.perf_counter()
            observe_received_response(source="test.html", status=403, headers={"content-type": "text/html"}, final_url="https://example.test", body_prefix=html)
            html_samples.append((time.perf_counter() - start) * 1000)
        self.assertLessEqual(statistics.quantiles(header_samples, n=20)[-1], 0.25)
        self.assertLessEqual(statistics.quantiles(html_samples, n=20)[-1], 2.0)


if __name__ == "__main__":
    unittest.main()
