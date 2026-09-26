"""Measure OCR solvers against a dated corpus, in shadow.

OCR answers are never submitted in production (challenge_routing marks OCR
solvers shadow-only). This harness is how that could change later: it runs
each OCR solver over a dated, scrubbed corpus and reports exact-answer
accuracy, abstentions and false acceptances per solver and family. No
approval threshold is defined, so the report always says the gate is closed.
"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_CASE_FIELDS = ("id", "family", "captcha_type", "params", "expected")
REQUIRED_CORPUS_FIELDS = ("version", "collected_on", "provenance", "redaction", "cases")


@dataclass
class SolverScore:
    solver_id: str
    family: str
    cases: int = 0
    correct: int = 0
    abstained: int = 0
    wrong: int = 0

    def to_dict(self) -> dict[str, Any]:
        answered = self.cases - self.abstained
        return {
            "solver_id": self.solver_id, "family": self.family, "cases": self.cases,
            "exact_accuracy": round(self.correct / self.cases, 4) if self.cases else None,
            "abstentions": self.abstained,
            # Wrong answers given with confidence: what would reach a provider.
            "false_acceptance_rate": round(self.wrong / answered, 4) if answered else None,
        }


def load_corpus(path: str | Path) -> dict[str, Any]:
    corpus = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [f for f in REQUIRED_CORPUS_FIELDS if not corpus.get(f)]
    if missing:
        raise ValueError(f"corpus is missing {', '.join(missing)}")
    for case in corpus["cases"]:
        absent = [f for f in REQUIRED_CASE_FIELDS if f not in case]
        if absent:
            raise ValueError(f"case {case.get('id', '?')} is missing {', '.join(absent)}")
    return corpus


def evaluate(corpus: dict[str, Any], solvers: list[Any]) -> dict[str, Any]:
    """Run every shadow solver on copies of each case; nothing is submitted."""
    from .captcha import CaptchaChallenge
    scores: dict[tuple[str, str], SolverScore] = {}

    async def run() -> None:
        for case in corpus["cases"]:
            for solver in solvers:
                challenge = CaptchaChallenge(id=f"eval-{case['id']}", provider_id="ocr-evaluation",
                                             captcha_type=case["captcha_type"], params=copy.deepcopy(case["params"]))
                score = scores.setdefault((solver.solver_id, case["family"]), SolverScore(solver.solver_id, case["family"]))
                score.cases += 1
                try:
                    proposal = await solver.solve(challenge) if solver.can_solve(challenge) else None
                except Exception:
                    proposal = None
                answer = str((proposal or {}).get("text") or "")
                if not answer:
                    score.abstained += 1
                elif answer == str(case["expected"]):
                    score.correct += 1
                else:
                    score.wrong += 1

    asyncio.run(run())
    return {
        "corpus_version": corpus["version"], "collected_on": corpus["collected_on"],
        "scores": [s.to_dict() for s in scores.values()],
        # No threshold has been approved: OCR stays shadow-only whatever the numbers say.
        "approval_gate": {"defined": False, "passed": False, "production_eligible": False},
    }
