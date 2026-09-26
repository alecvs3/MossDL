from __future__ import annotations

import asyncio
import unittest

from engine.browser_step import advance_and_wait, step2_click_ready


class _Button:
    def __init__(self, on_click=None):
        self.clicks = 0
        self._on_click = on_click

    async def click(self, **_kwargs):
        self.clicks += 1
        if self._on_click:
            self._on_click()


class _Page:
    def __init__(self):
        self.url = "https://provider.test/file"
        self.step_present = True
        self.ready = set()
        self.button = _Button()
        self.evaluations = []

    async def evaluate(self, script):
        self.evaluations.append(script)
        self.button.clicks += 1

    async def query_selector(self, selector):
        if selector == "#method_free":
            return self.button if self.step_present else None
        return object() if selector in self.ready else None


class BrowserStepTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_client_side_step_selector_without_retry(self):
        page = _Page()
        button = _Button(lambda: page.ready.add("download-countdown"))
        page.button = button
        result = await advance_and_wait(
            page, button, step_selector="#method_free",
            readiness_selectors=("download-countdown",), timeout_seconds=0.1,
        )
        self.assertTrue(result.acknowledged)
        self.assertEqual("selector:download-countdown", result.signal)
        self.assertEqual(1, button.clicks)

    async def test_accepts_url_change_without_retry(self):
        page = _Page()
        button = _Button(lambda: setattr(page, "url", "https://provider.test/download"))
        page.button = button
        result = await advance_and_wait(
            page, button, step_selector="#method_free",
            readiness_selectors=(), timeout_seconds=0.1,
        )
        self.assertEqual("url_changed", result.signal)
        self.assertEqual(1, button.clicks)

    async def test_retries_only_when_step_one_remains(self):
        page = _Page()
        result = await advance_and_wait(
            page, page.button, step_selector="#method_free",
            readiness_selectors=(), timeout_seconds=0.003, poll_seconds=0.001,
        )
        self.assertFalse(result.acknowledged)
        self.assertTrue(result.retried)
        self.assertEqual(2, page.button.clicks)

    async def test_does_not_retry_when_step_one_disappeared(self):
        page = _Page()
        button = _Button(lambda: setattr(page, "step_present", False))
        page.button = button
        result = await advance_and_wait(
            page, button, step_selector="#method_free",
            readiness_selectors=(), timeout_seconds=0.1,
        )
        self.assertEqual("step_one_removed", result.signal)
        self.assertFalse(result.retried)
        self.assertEqual(1, button.clicks)

    async def test_request_dispatch_acknowledges_click_before_dom_changes(self):
        page = _Page()
        request_event = asyncio.Event()
        button = _Button(request_event.set)
        page.button = button
        result = await advance_and_wait(
            page, button, step_selector="#method_free", readiness_selectors=(),
            request_event=request_event, timeout_seconds=0.1,
        )
        self.assertEqual("request_dispatched", result.signal)
        self.assertEqual(1, button.clicks)

    async def test_direct_dom_click_avoids_actionability_wait(self):
        page = _Page()
        result = await advance_and_wait(
            page, page.button, step_selector="#method_free", readiness_selectors=(),
            click_script="() => document.getElementById('method_free')?.click()",
            timeout_seconds=0.003, poll_seconds=0.001,
        )
        self.assertTrue(result.retried)
        self.assertEqual(2, page.button.clicks)
        self.assertEqual(2, len(page.evaluations))

    def test_step2_token_waits_for_late_timer_observation(self):
        self.assertFalse(step2_click_ready(
            has_token=True, timer_detected=False, timer_remaining=None,
            saw_step_advance=True, has_widget=True,
        ))
        self.assertFalse(step2_click_ready(
            has_token=True, timer_detected=True, timer_remaining=10,
            saw_step_advance=True, has_widget=True,
        ))
        self.assertTrue(step2_click_ready(
            has_token=True, timer_detected=True, timer_remaining=0,
            saw_step_advance=True, has_widget=True,
        ))

    def test_step2_allows_captcha_only_provider_without_timer(self):
        self.assertTrue(step2_click_ready(
            has_token=True, timer_detected=False, timer_remaining=None,
            saw_step_advance=False, has_widget=True,
        ))


if __name__ == "__main__":
    unittest.main()
