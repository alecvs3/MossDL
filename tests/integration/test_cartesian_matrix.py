# Cartesian Matrix Test Suite
import unittest
from tests.fixtures.seam_harness import EngineSeamHarness

ALL_STATES = [
    'queued', 'resolving', 'preflight', 'downloading', 'verifying',
    'postprocessing', 'needs_user', 'paused', 'retrying', 'completed',
    'failed', 'canceled',
]

class TestCartesianMatrix(unittest.TestCase):
    def setUp(self):
        self.harness = EngineSeamHarness()
        self.service = self.harness.start()

    def tearDown(self):
        self.harness.close()

    def test_download_task_idempotent_on_completed(self):
        task = self.harness.add_task('https://example.com/matrix_comp.rar', state='completed')
        res = self.service.dispatch('download_task', {'id': task.id})
        self.assertEqual(res['state'], 'completed')
        self.assertEqual(self.service.store.get(task.id).state, 'completed')

    def test_download_task_idempotent_on_canceled(self):
        task = self.harness.add_task('https://example.com/matrix_canc.rar', state='canceled')
        res = self.service.dispatch('download_task', {'id': task.id})
        self.assertEqual(res['state'], 'canceled')
        self.assertEqual(self.service.store.get(task.id).state, 'canceled')

    def test_download_task_matrix_activatable(self):
        for st in ('queued', 'paused', 'needs_user'):
            with self.subTest(state=st):
                task = self.harness.add_task(f'https://example.com/matrix_{st}.rar', state=st)
                res = self.service.dispatch('download_task', {'id': task.id})
                self.assertEqual(res['state'], 'queued')

    def test_download_task_matrix_in_flight_is_idempotent(self):
        for st in ('resolving', 'preflight', 'downloading', 'verifying', 'postprocessing', 'retrying'):
            with self.subTest(state=st):
                task = self.harness.add_task(f'https://example.com/matrix_{st}.rar', state=st)
                res = self.service.dispatch('download_task', {'id': task.id})
                self.assertEqual(res['state'], st)

    def test_download_task_matrix_failed_still_requires_resume(self):
        task = self.harness.add_task('https://example.com/matrix_failed.rar', state='failed')
        with self.assertRaises(ValueError):
            self.service.dispatch('download_task', {'id': task.id})

    def test_pause_task_matrix(self):
        for st in ALL_STATES:
            with self.subTest(state=st):
                task = self.harness.add_task(f'https://example.com/pause_{st}.rar', state=st)
                if st in ('completed', 'canceled'):
                    with self.assertRaises(ValueError):
                        self.service.dispatch('pause_task', {'id': task.id})
                else:
                    res = self.service.dispatch('pause_task', {'id': task.id})
                    self.assertEqual(res['state'], 'paused')

    def test_resume_task_matrix(self):
        for st in ALL_STATES:
            with self.subTest(state=st):
                task = self.harness.add_task(f'https://example.com/resume_{st}.rar', state=st)
                if st == 'completed':
                    with self.assertRaises(ValueError):
                        self.service.dispatch('resume_task', {'id': task.id})
                elif st in ('failed', 'canceled'):
                    res = self.service.dispatch('resume_task', {'id': task.id})
                    self.assertEqual(res['state'], 'queued')
                elif st in ('queued', 'paused', 'needs_user'):
                    res = self.service.dispatch('resume_task', {'id': task.id})
                    self.assertEqual(res['state'], 'queued')
                else:
                    with self.assertRaises(ValueError):
                        self.service.dispatch('resume_task', {'id': task.id})

    def test_retry_task_matrix(self):
        for st in ALL_STATES:
            with self.subTest(state=st):
                task = self.harness.add_task(f'https://example.com/retry_{st}.rar', state=st)
                res = self.service.dispatch('retry_task', {'id': task.id})
                self.assertEqual(res['state'], 'queued')

    def test_cancel_task_matrix(self):
        for st in ALL_STATES:
            with self.subTest(state=st):
                task = self.harness.add_task(f'https://example.com/cancel_{st}.rar', state=st)
                if st == 'completed':
                    with self.assertRaises(ValueError):
                        self.service.dispatch('cancel_task', {'id': task.id})
                else:
                    res = self.service.dispatch('cancel_task', {'id': task.id})
                    self.assertEqual(res['state'], 'canceled')

    def test_delete_task_matrix(self):
        for st in ALL_STATES:
            with self.subTest(state=st):
                task = self.harness.add_task(f'https://example.com/del_{st}.rar', state=st)
                res = self.service.dispatch('delete_task', {'id': task.id})
                self.assertEqual(res['deleted'], task.id)
                self.assertIsNone(self.service.store.get(task.id))

    def test_set_task_options_matrix(self):
        for st in ALL_STATES:
            with self.subTest(state=st):
                task = self.harness.add_task(f'https://example.com/opt_{st}.rar', state=st)
                res = self.service.dispatch('set_task_options', {'id': task.id, 'priority': 42})
                self.assertEqual(res['priority'], 42)
                self.assertEqual(self.service.store.get(task.id).state, st)

    def test_captcha_solve_matrix(self):
        for st in ALL_STATES:
            with self.subTest(state=st):
                task = self.harness.add_task(f'https://example.com/cap_{st}.rar', state=st)
                res = self.service.dispatch('captcha_solve', {'challenge_id': f'ch_{st}', 'task_id': task.id, 'solution': {}})
                self.assertIn('success', res)
                self.assertEqual(self.service.store.get(task.id).state, st)

if __name__ == '__main__':
    unittest.main()
