"""Evidence gate for expanding browser-solver concurrency."""

from __future__ import annotations


class SolverLanePolicy:
    def __init__(self, *, ceiling: int = 5, material_wait_seconds: float = 2.0) -> None:
        self.ceiling = ceiling
        self.material_wait_seconds = material_wait_seconds

    def recommended_limit(self, *, current_limit: int, package_demand: int,
                          queue_wait_seconds: float) -> int:
        """Add one lane only when one package exceeds capacity and actually waits."""
        if current_limit >= self.ceiling:
            return current_limit
        if package_demand <= current_limit:
            return current_limit
        if queue_wait_seconds < self.material_wait_seconds:
            return current_limit
        return current_limit + 1
