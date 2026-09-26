"""VER-03: multipart packages of 2-20 members under faults, as a seeded model.

Real CaptchaManager, router, scheduler, verifier and artifact rules; a fake
site decides which members challenge and whether an answer is accepted. For
each seed and size the model checks: no challenge generation is solved twice,
the solver bound holds, no member's token or file state reaches another,
clearance only crosses on a verified answer, paused/cancelled members are
never resumed by automation, missing members block nothing, and the run
finishes (no deadlock) with every member in an explicit end state.
"""
from __future__ import annotations

import asyncio
import random
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine import challenge_lifecycle as lifecycle  # noqa: E402
from engine.captcha import CaptchaChallenge, CaptchaManager, CaptchaSolver, CaptchaType  # noqa: E402
from engine.challenge_artifacts import shareable_session  # noqa: E402
from engine.challenge_routing import PAGE_INPUTS, Health, SolverCapability  # noqa: E402
from engine.challenge_scheduler import ChallengeScheduler  # noqa: E402

LIMIT = 2


class _Solver(CaptchaSolver):
    def __init__(self):
        super().__init__("model_browser", "Model browser")
        self.solves: Counter = Counter()
        self.concurrent = 0
        self.peak = 0

    def capability(self):
        return SolverCapability(frozenset({"turnstile"}), "clearance", "reusable_clearance", inputs={"*": (PAGE_INPUTS,)})

    def health(self):
        return Health(True)

    def can_solve(self, challenge):
        return True

    async def solve(self, challenge):
        self.solves[(challenge.id, lifecycle.generation_of(challenge))] += 1
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        await asyncio.sleep(0)
        self.concurrent -= 1
        return {"token": f"TOKEN-{challenge.id}-{lifecycle.generation_of(challenge)}",
                "cookies": {"cf_clearance": "CL", "file_code": f"FILE-{challenge.task_id}"}}


def run_package(seed: int, size: int) -> dict:
    rng = random.Random(seed)
    per_file = rng.random() < 0.5            # host issues a challenge per file, or clearance covers all
    manager = CaptchaManager()
    solver = _Solver()
    manager.solvers = [solver, manager.interactive_ui]
    manager.interactive_ui.enabled = False   # no person in the model: unanswered means explicit failure
    scheduler = ChallengeScheduler(limit=LIMIT)
    fates = {f"m{i}": rng.choice(["ok"] * 6 + ["reject_once", "paused", "cancelled", "missing", "timeout"]) for i in range(size)}
    shared_clearance: list[dict] = []
    outcome: dict[str, str] = {}
    restart_at = rng.randrange(size) if rng.random() < 0.3 else None

    async def member(task_id: str, index: int) -> None:
        fate = fates[task_id]
        # Siblings start staggered behind part 1, as the engine's sentinel does.
        await asyncio.sleep(0.003 * index)
        if fate == "missing":
            outcome[task_id] = "missing"
            return
        if fate in {"paused", "cancelled"}:
            # An explicit pause or cancel dominates automation: never solved, never resumed.
            if fate == "cancelled":
                manager.cancel_challenges_for_task(task_id)
            outcome[task_id] = fate
            return
        if shared_clearance and not per_file:
            outcome[task_id] = "passed_with_clearance"
            return
        challenge = CaptchaChallenge(id=f"c-{task_id}", task_id=task_id, captcha_type=CaptchaType.TURNSTILE,
                                     params={"page_url": "https://host.test/f", "group_id": "pkg"},
                                     timeout_seconds=0.001 if fate == "timeout" else 90)
        manager.register_challenge(challenge)
        if fate == "timeout":
            await asyncio.sleep(0.005)
            manager._cleanup_expired()
            outcome[task_id] = lifecycle.state_of(challenge)
            return
        for attempt in range(3):
            if restart_at == index and attempt == 0:
                lifecycle.advance(challenge, lifecycle.ROUTING, "model")
                lifecycle.recover_after_restart(challenge)      # the engine stopped mid-attempt
            try:
                answer = await scheduler.run(challenge, lambda: manager.request_solution(challenge, force_automated=True))
            except (RuntimeError, TimeoutError):
                outcome[task_id] = "needs_you"
                return
            assert "TOKEN" not in str(shareable_session(answer)), "a token would cross members"
            if fate == "reject_once" and attempt == 0:
                manager.verifier.reject(challenge.id, "the site asked again")
                lifecycle.advance(challenge, lifecycle.ROUTING, "retry")
                challenge.status = "pending"
                continue
            manager.verifier.accept(challenge.id, "the site served the file")
            shared_clearance.append(shareable_session(challenge.solution))
            outcome[task_id] = "resolved"
            return
        outcome[task_id] = "gave_up"

    async def main():
        await asyncio.wait_for(asyncio.gather(*(member(f"m{i}", i) for i in range(size))), timeout=20)

    asyncio.run(main())
    return {"per_file": per_file, "fates": fates, "outcome": outcome, "solver": solver, "scheduler": scheduler,
            "shared": shared_clearance, "manager": manager}


class MultipartModelTests(unittest.TestCase):
    def test_packages_of_two_to_twenty_members_under_faults(self):
        for size in (2, 3, 5, 8, 13, 20):
            for seed in range(12):
                with self.subTest(size=size, seed=seed):
                    r = run_package(seed * 101 + size, size)
                    self.assertEqual(set(r["outcome"]), set(r["fates"]), "every member ends in an explicit state")
                    duplicates = [k for k, n in r["solver"].solves.items() if n > 1]
                    self.assertEqual(duplicates, [], "a challenge generation was solved twice")
                    self.assertLessEqual(r["solver"].peak, LIMIT, "solver work exceeded its bound")
                    self.assertEqual((r["scheduler"].running, r["scheduler"].waiting), (0, 0))
                    for task_id, fate in r["fates"].items():
                        got = r["outcome"][task_id]
                        if fate in {"paused", "cancelled", "missing"}:
                            self.assertEqual(got, fate, "automation never overrides the user or invents members")
                            self.assertFalse(any(k[0] == f"c-{task_id}" for k in r["solver"].solves))
                    for session in r["shared"]:
                        self.assertNotIn("FILE-", str(session), "file-bound state never crosses members")
                        self.assertEqual(set((session.get("cookies") or {})), {"cf_clearance"})
                    if not r["per_file"] and r["shared"]:
                        solved = {k[0] for k in r["solver"].solves}
                        self.assertLessEqual(len(solved), sum(1 for f in r["fates"].values() if f not in {"paused", "cancelled", "missing", "timeout"}))


if __name__ == "__main__":
    unittest.main()
