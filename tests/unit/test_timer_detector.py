"""Unit tests for TimerDetector and multi-tab timer synchronization."""

import time
import unittest
from engine.timer_detector import TimerDetector, TimerDetectionResult


class TestTimerDetector(unittest.TestCase):

    def test_detect_datanodes_vue_component(self):
        """Detects Vue.js dynamic :countdown prop on <download-countdown> tag."""
        html = """
        <div class="container">
            <download-countdown :countdown="10"
                code="xyz123"
                rand="abc456"
                :has-countdown="true">
            </download-countdown>
        </div>
        """
        res = TimerDetector.detect_from_html(html)
        self.assertTrue(res.has_timer)
        self.assertEqual(res.seconds_remaining, 10)
        self.assertEqual(res.detection_source, "vue_prop")

    def test_detect_datanodes_static_vue_prop(self):
        """Detects static countdown attribute on download-countdown tag."""
        html = '<download-countdown countdown="15" code="file99"></download-countdown>'
        res = TimerDetector.detect_from_html(html)
        self.assertTrue(res.has_timer)
        self.assertEqual(res.seconds_remaining, 15)
        self.assertEqual(res.detection_source, "vue_prop")

    def test_detect_data_attributes(self):
        """Detects data-timer, data-countdown, and data-wait attributes."""
        html_timer = '<div class="download-box" data-timer="25">Please wait...</div>'
        res1 = TimerDetector.detect_from_html(html_timer)
        self.assertTrue(res1.has_timer)
        self.assertEqual(res1.seconds_remaining, 25)
        self.assertEqual(res1.detection_source, "data_attribute")

        html_countdown = '<button data-countdown="8">Download</button>'
        res2 = TimerDetector.detect_from_html(html_countdown)
        self.assertTrue(res2.has_timer)
        self.assertEqual(res2.seconds_remaining, 8)
        self.assertEqual(res2.detection_source, "data_attribute")

    def test_detect_html_countdown_tag(self):
        """Detects countdown number inside span or div with timer ID/class."""
        html1 = '<span id="countdown">12</span>'
        res1 = TimerDetector.detect_from_html(html1)
        self.assertTrue(res1.has_timer)
        self.assertEqual(res1.seconds_remaining, 12)
        self.assertEqual(res1.detection_source, "html_tag")

        html2 = '<div class="wait_timer">45s</div>'
        res2 = TimerDetector.detect_from_html(html2)
        self.assertTrue(res2.has_timer)
        self.assertEqual(res2.seconds_remaining, 45)
        self.assertEqual(res2.detection_source, "html_tag")

    def test_detect_javascript_timer_variables(self):
        """Detects countdown variables and timer function calls in inline script."""
        html_var = "<script>var seconds = 20; function tick() { seconds--; }</script>"
        res1 = TimerDetector.detect_from_html(html_var)
        self.assertTrue(res1.has_timer)
        self.assertEqual(res1.seconds_remaining, 20)
        self.assertEqual(res1.detection_source, "js_variable")

        html_let = "<script>let countdown = 14;</script>"
        res2 = TimerDetector.detect_from_html(html_let)
        self.assertTrue(res2.has_timer)
        self.assertEqual(res2.seconds_remaining, 14)
        self.assertEqual(res2.detection_source, "js_variable")

        html_call = "<script>download_timer(30);</script>"
        res3 = TimerDetector.detect_from_html(html_call)
        self.assertTrue(res3.has_timer)
        self.assertEqual(res3.seconds_remaining, 30)
        self.assertEqual(res3.detection_source, "js_variable")

        html_timeout = "<script>setTimeout(function() { showDownload(); }, 8000);</script>"
        res4 = TimerDetector.detect_from_html(html_timeout)
        self.assertTrue(res4.has_timer)
        self.assertEqual(res4.seconds_remaining, 8)
        self.assertEqual(res4.detection_source, "js_variable")

    def test_detect_meta_refresh(self):
        """Detects <meta http-equiv="refresh" content="7; url=...">."""
        html = '<meta http-equiv="refresh" content="7; url=https://example.com/download">'
        res = TimerDetector.detect_from_html(html)
        self.assertTrue(res.has_timer)
        self.assertEqual(res.seconds_remaining, 7)
        self.assertEqual(res.detection_source, "meta_refresh")

    def test_detect_http_headers(self):
        """Detects Retry-After and X-Wait HTTP headers."""
        res_retry = TimerDetector.detect_from_html("", headers={"Retry-After": "18"})
        self.assertTrue(res_retry.has_timer)
        self.assertEqual(res_retry.seconds_remaining, 18)
        self.assertEqual(res_retry.detection_source, "http_header")

        res_wait = TimerDetector.detect_from_html("", headers={"X-Wait": "5"})
        self.assertTrue(res_wait.has_timer)
        self.assertEqual(res_wait.seconds_remaining, 5)
        self.assertEqual(res_wait.detection_source, "http_header")

    def test_no_timer_and_edge_cases(self):
        """Verifies clean handling when no timer exists or false-positive numbers are present."""
        html_none = "<html><body><h1>Direct Download Link</h1><p>Click below to download</p></body></html>"
        res1 = TimerDetector.detect_from_html(html_none)
        self.assertFalse(res1.has_timer)
        self.assertEqual(res1.seconds_remaining, 0)
        self.assertEqual(res1.detection_source, "none")

        res_empty = TimerDetector.detect_from_html("")
        self.assertFalse(res_empty.has_timer)
        self.assertEqual(res_empty.seconds_remaining, 0)

        # Non-timer 0-second or negative values
        html_zero = '<div data-timer="0">Ready</div>'
        res_zero = TimerDetector.detect_from_html(html_zero)
        self.assertFalse(res_zero.has_timer)

    def test_extract_dom_script_validity(self):
        """Verifies extract_dom_script produces a valid JavaScript function snippet."""
        script = TimerDetector.extract_dom_script()
        self.assertTrue(script.strip().startswith("() => {"))
        self.assertIn("download-countdown", script)
        self.assertIn("has_timer", script)

    def test_multipart_parallel_offset_simulation(self):
        """Simulates parallel multi-part timer progression with staggered offsets."""
        # 3 parts launched with 1.2s stagger:
        # Part 1: 3s timer started at T+0.0s -> ends at T+3.0s
        # Part 2: 3s timer started at T+1.2s -> ends at T+4.2s
        # Part 3: 2s timer started at T+2.4s -> ends at T+4.4s

        parts = [
            {"id": "part1", "duration": 3, "start_offset": 0.0},
            {"id": "part2", "duration": 3, "start_offset": 1.2},
            {"id": "part3", "duration": 2, "start_offset": 2.4},
        ]

        t0 = time.monotonic()
        completed_events: list[tuple[str, float]] = []

        # Step through simulated timeline in 0.5s increments up to 5.0s
        for step in range(11):
            now_offset = step * 0.5
            for p in parts:
                if now_offset >= p["start_offset"]:
                    elapsed = now_offset - p["start_offset"]
                    remaining = max(0, p["duration"] - int(elapsed))
                    if remaining == 0 and p["id"] not in [x[0] for x in completed_events]:
                        completed_events.append((p["id"], now_offset))

        # Assert all 3 parts reached 0s
        self.assertEqual(len(completed_events), 3)
        # Part 1 finished first at ~3.0s
        self.assertEqual(completed_events[0][0], "part1")
        self.assertAlmostEqual(completed_events[0][1], 3.0, delta=0.5)
        # Part 2 finished second at ~4.5s
        self.assertEqual(completed_events[1][0], "part2")
        self.assertAlmostEqual(completed_events[1][1], 4.5, delta=0.5)
        # Part 3 finished third at ~4.5s
        self.assertEqual(completed_events[2][0], "part3")
        self.assertAlmostEqual(completed_events[2][1], 4.5, delta=0.5)


if __name__ == "__main__":
    unittest.main()
