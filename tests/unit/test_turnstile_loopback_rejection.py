from __future__ import annotations
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.captcha import BrowserLoopbackSolver, CaptchaChallenge, CaptchaManager, CaptchaType


class TurnstileLoopbackRejectionTests(unittest.TestCase):
    def test_loopback_rejects_turnstile(self) -> None:
        manager = CaptchaManager()
        solver = BrowserLoopbackSolver(manager)
        challenge = CaptchaChallenge(
            id='test-turnstile',
            captcha_type=CaptchaType.TURNSTILE,
            params={'site_key': '0x4AAAAAAAE...',
                    'page_url': 'https://datanodes.to/download'},
        )
        self.assertFalse(solver.can_solve(challenge), 'BrowserLoopbackSolver must reject Turnstile challenges')

    def test_loopback_still_handles_other_supported_types(self) -> None:
        manager = CaptchaManager()
        solver = BrowserLoopbackSolver(manager)
        for c_type in (CaptchaType.RECAPTCHA_V2, CaptchaType.RECAPTCHA_V3, CaptchaType.HCAPTCHA):
            c = CaptchaChallenge(id='test', captcha_type=c_type, params={})
            self.assertTrue(solver.can_solve(c), f'BrowserLoopbackSolver should support {c_type}')


if __name__ == '__main__':
    unittest.main()
