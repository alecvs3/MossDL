from __future__ import annotations

import unittest

from engine.segment_budget import segment_budget


class SegmentBudgetTests(unittest.TestCase):
    def test_datanodes_multipart_uses_one_transport_connection_per_file(self) -> None:
        budget = segment_budget("datanodes", is_multipart=True)
        self.assertIsNotNone(budget)
        self.assertEqual(budget.max_segments, 1)
        self.assertEqual(budget.reason, "multipart_file_parallelism")

    def test_datanodes_single_file_keeps_backend_default(self) -> None:
        self.assertIsNone(segment_budget("datanodes", is_multipart=False))

    def test_other_providers_keep_backend_default(self) -> None:
        self.assertIsNone(segment_budget("pixeldrain", is_multipart=True))


if __name__ == "__main__":
    unittest.main()
