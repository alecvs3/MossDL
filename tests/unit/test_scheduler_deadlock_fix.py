from __future__ import annotations
import asyncio
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from engine.service import EngineService
from engine.models import DownloadTask
from engine.errors import NeedsCaptcha
from engine.captcha import CaptchaType


class SchedulerDeadlockFixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.service = EngineService(Path(self.temp_dir.name))

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    def test_challenge_yields_scheduler_slot_immediately(self) -> None:
        task = DownloadTask(
            id='task-test-challenge',
            source_url='https://datanodes.to/download/test',
            destination=str(Path(self.temp_dir.name) / 'file.bin'),
            state='downloading',
        )
        self.service.store.save(task)

        mock_challenge = {'page_url': 'https://datanodes.to/download/test', 'site_key': '0x4ABC'}
        with patch.object(self.service.captcha, 'request_solution', new_callable=AsyncMock) as mock_solve:
            mock_solve.return_value = {'turnstile_token': 'dummy_token', 'cf_clearance': 'dummy_clearance'}
            control = self.service._controls.get(task.id) or unittest.mock.MagicMock()
            with patch.object(self.service, '_download_item_with_refresh', side_effect=NeedsCaptcha(CaptchaType.TURNSTILE, mock_challenge)):
                asyncio.run(self.service._run_task_async(task.id, {}, control, 'custom'))

            updated = self.service.store.get(task.id)
            self.assertEqual(updated.state, 'needs_user')


if __name__ == '__main__':
    unittest.main()
