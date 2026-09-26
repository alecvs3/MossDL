import unittest

from engine.resolve_stagger import ResolveStaggerController


class TestResolveStaggerController(unittest.TestCase):
    def test_rate_rejection_increases_spacing_and_success_recovers_to_floor(self):
        controller = ResolveStaggerController(initial=1.5, floor=0.5, recovery_successes=2)
        self.assertEqual(controller.delay("Example.COM"), 1.5)
        self.assertEqual(controller.observe_rejection("example.com", status_code=429), 2.625)
        controller.observe_success("example.com")
        reduced = controller.observe_success("example.com")
        self.assertLess(reduced, 2.625)
        for _ in range(40):
            controller.observe_success("example.com")
        self.assertGreaterEqual(controller.delay("example.com"), 0.5)

    def test_expected_captcha_does_not_penalize_host(self):
        controller = ResolveStaggerController()
        self.assertEqual(controller.observe_rejection("example.com", reason="captcha_required"), 1.5)
        self.assertEqual(controller.snapshot("example.com")["rejections"], 0)

    def test_rejected_continuation_is_evidence(self):
        controller = ResolveStaggerController()
        self.assertGreater(
            controller.observe_rejection("example.com", reason="continuation_rejected"),
            1.5,
        )


if __name__ == "__main__":
    unittest.main()
