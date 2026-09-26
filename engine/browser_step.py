"""Evidence-based browser form advancement shared by challenge providers."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Iterable

from . import critical_trace


@dataclass(frozen=True, slots=True)
class StepAdvanceResult:
    acknowledged: bool
    signal: str
    retried: bool
    wait_seconds: float


async def advance_and_wait(
    page: Any,
    button: Any,
    *,
    step_selector: str,
    readiness_selectors: Iterable[str],
    request_event: asyncio.Event | None = None,
    click_script: str | None = None,
    timeout_seconds: float = 12.0,
    poll_seconds: float = 0.05,
) -> StepAdvanceResult:
    """Click once and wait for observed progress; retry only if step one remains."""
    started = time.perf_counter()
    initial_url = str(getattr(page, "url", "") or "")

    async def _click(target: Any) -> None:
        if click_script:
            await page.evaluate(click_script)
        else:
            await target.click(no_wait_after=True)

    critical_trace.mark("solver.step_advance.click", event="state", click_number=1)
    await _click(button)
    deadline = started + max(0.0, timeout_seconds)
    signal = ""
    selectors = tuple(readiness_selectors)
    while time.perf_counter() <= deadline:
        try:
            if request_event is not None and request_event.is_set():
                signal = "request_dispatched"
                break
            current_url = str(getattr(page, "url", "") or "")
            if current_url and initial_url and current_url != initial_url:
                signal = "url_changed"
                break
            if await page.query_selector(step_selector) is None:
                signal = "step_one_removed"
                break
            for selector in selectors:
                if await page.query_selector(selector) is not None:
                    signal = f"selector:{selector}"
                    break
            if signal:
                break
        except Exception:
            # A navigation can temporarily invalidate the execution context. The
            # next iteration observes its URL or replacement DOM.
            pass
        await asyncio.sleep(max(0.001, poll_seconds))

    retried = False
    if not signal:
        try:
            remaining_button = await page.query_selector(step_selector)
        except Exception:
            remaining_button = None
        if remaining_button is not None:
            retried = True
            critical_trace.mark(
                "solver.step_advance.retry", event="state", click_number=2,
                reason="step_one_still_present_after_timeout",
            )
            await _click(remaining_button)
        else:
            signal = "step_one_removed_after_timeout"

    waited = time.perf_counter() - started
    critical_trace.mark(
        "solver.step_advance.acknowledged" if signal else "solver.step_advance.unacknowledged",
        event="state", signal=signal or "timeout", retried=retried,
        wait_seconds=waited,
    )
    return StepAdvanceResult(bool(signal), signal or "timeout", retried, waited)


def step2_click_ready(*, has_token: bool, timer_detected: bool,
                      timer_remaining: int | None, saw_step_advance: bool,
                      has_widget: bool) -> bool:
    """Require timer evidence before a challenge token can submit step two."""
    if has_token:
        if timer_detected:
            return timer_remaining == 0
        # A known step-one flow may reveal its mandatory timer after the token.
        # Unknown duration remains waiting; it is never interpreted as zero.
        return not saw_step_advance
    return not has_widget and timer_detected and timer_remaining == 0
