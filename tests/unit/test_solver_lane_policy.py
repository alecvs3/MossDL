import unittest

from engine.solver_lane_policy import SolverLanePolicy


class TestSolverLanePolicy(unittest.TestCase):
    def test_requires_package_larger_than_lane_count(self):
        policy = SolverLanePolicy(material_wait_seconds=2.0)
        self.assertEqual(policy.recommended_limit(
            current_limit=3, package_demand=3, queue_wait_seconds=20.0), 3)

    def test_requires_material_queue_wait(self):
        policy = SolverLanePolicy(material_wait_seconds=2.0)
        self.assertEqual(policy.recommended_limit(
            current_limit=3, package_demand=4, queue_wait_seconds=0.01), 3)
        self.assertEqual(policy.recommended_limit(
            current_limit=3, package_demand=4, queue_wait_seconds=2.1), 4)

    def test_respects_ceiling(self):
        policy = SolverLanePolicy(ceiling=4, material_wait_seconds=1.0)
        self.assertEqual(policy.recommended_limit(
            current_limit=4, package_demand=8, queue_wait_seconds=10.0), 4)


if __name__ == "__main__":
    unittest.main()
