from __future__ import annotations
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.browser_solver import BrowserSolverDaemon


class BrowserSolverActorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.daemon = BrowserSolverDaemon()

    def test_concurrent_requests_execute_on_actor_thread(self) -> None:
        observed_threads = []

        def mock_execute(*args, **kwargs):
            observed_threads.append(threading.get_ident())
            time.sleep(0.05)
            return {'success': True, 'engine': 'mock', 'cookies': {}}

        with patch.object(self.daemon, '_execute_solve_on_actor', side_effect=mock_execute):
            results = []
            threads = []

            def worker():
                res = self.daemon.solve_challenge_sync('http://example.com/test', timeout_seconds=5.0)
                results.append(res)

            for _ in range(5):
                t = threading.Thread(target=worker)
                threads.append(t)
                t.start()

            for t in threads:
                t.join(timeout=5.0)

            self.assertEqual(len(results), 5)
            for res in results:
                self.assertTrue(res.get('success'))

            self.assertEqual(len(observed_threads), 5)
            self.assertEqual(len(set(observed_threads)), 1)
            self.assertEqual(observed_threads[0], self.daemon._actor_thread.ident)

    def test_timeout_propagates_without_deadlock(self) -> None:
        def slow_execute(*args, **kwargs):
            time.sleep(1.0)
            return {'success': True}

        with patch.object(self.daemon, '_execute_solve_on_actor', side_effect=slow_execute):
            res = self.daemon.solve_challenge_sync('http://example.com/timeout', timeout_seconds=0.1)
            self.assertIn('success', res)


if __name__ == '__main__':
    unittest.main()
