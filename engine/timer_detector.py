"""Modular Timer Detection Engine for File Hosters.

Analyzes raw HTTP responses, response headers, and live DOM trees to identify
server-enforced cooldowns, countdown timers, and wait delays across file hosters.

The ranked API is :meth:`TimerDetector.detect_candidates`, which returns *every*
candidate it finds (headers, JSON bodies, Vue props, data attributes, HTML tags,
JS variables, and human-readable wait text) ordered by confidence.  The legacy
:meth:`TimerDetector.detect_from_html` is preserved as a back-compat wrapper that
returns the single best candidate.  Candidates are never silently dropped: out
of range or unparseable observations are returned with ``valid=False``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class TimerCandidate:
    """One parsed wait-timer observation with provenance and confidence."""

    source: str
    seconds: int
    confidence: float
    raw: str | None = None
    selector: str | None = None
    valid: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "seconds": self.seconds,
            "confidence": self.confidence,
            "raw": self.raw,
            "selector": self.selector,
            "valid": self.valid,
            "note": self.note,
        }


@dataclass(frozen=True)
class TimerDetectionResult:
    has_timer: bool
    seconds_remaining: int
    detection_source: str  # "http_header" | "meta_refresh" | "vue_prop" | "data_attribute" | "html_tag" | "js_variable" | "json_wait" | "text_wait" | "none"
    raw_match: str | None = None
    selector: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_timer": self.has_timer,
            "seconds_remaining": self.seconds_remaining,
            "detection_source": self.detection_source,
            "raw_match": self.raw_match,
            "selector": self.selector,
        }


class TimerDetector:
    """Detects countdown timers from HTTP responses and DOM snippets."""

    # Bounds for a plausible countdown/wait value (seconds)
    MIN_SECONDS = 1
    MAX_SECONDS = 3600

    # Candidate confidences (higher wins)
    _CONF_HEADER = 0.97
    _CONF_BODY_JSON = 0.94
    _CONF_JSON_EMBEDDED = 0.90
    _CONF_META_REFRESH = 0.88
    _CONF_VUE_PROP = 0.85
    _CONF_DATA_ATTR = 0.80
    _CONF_HTML_TAG = 0.70
    _CONF_JS_OBJECT_PROP = 0.66
    _CONF_JS_VAR = 0.62
    _CONF_JS_FUNC = 0.58
    _CONF_JS_TIMEOUT = 0.52
    _CONF_TEXT_WAIT = 0.45

    # 1. HTTP Headers
    _HEADER_KEYS = ("retry-after", "x-wait", "x-cooldown", "x-timer", "x-wait-time")

    # 2. Meta Refresh: <meta http-equiv="refresh" content="15; url=...">
    _META_REFRESH_RE = re.compile(
        r'<meta\s+[^>]*http-equiv=["\']?refresh["\']?[^>]*content=["\']?(\d+)',
        re.IGNORECASE,
    )

    # 3. Vue / Component Dynamic Props: e.g. <download-countdown :countdown="10"
    _VUE_PROP_RE = re.compile(
        r'<[\w-]+[^\>]*\:(?:countdown|timer|wait|seconds)=["\'](\d+)["\']',
        re.IGNORECASE,
    )
    _VUE_STATIC_PROP_RE = re.compile(
        r'<download-countdown[^\>]*countdown=["\'](\d+)["\']',
        re.IGNORECASE,
    )

    # 4. Data Attributes: data-timer="15", data-countdown="10", data-wait="30"
    _DATA_ATTR_RE = re.compile(
        r'data-(?:timer|countdown|wait|seconds)=["\'](\d+)["\']',
        re.IGNORECASE,
    )

    # 5. HTML tags with countdown IDs/classes and number inside
    _HTML_TAG_RE = re.compile(
        r'<(?:span|div|p|b|strong|small)[^>]*(?:id|class)=["\'][^"\']*(?:countdown|timer|dl_countdown|wait_timer)[^"\']*["\'][^>]*>\s*(\d+)\s*(?:s|sec|seconds)?\s*<',
        re.IGNORECASE,
    )

    # 6. JavaScript variables & timer invocations
    _JS_VAR_RE = re.compile(
        r'(?:var|let|const)\s+(?:countdown|timer|seconds|sec|wait_time|wait_seconds|count)\s*=\s*(\d+)\s*;',
        re.IGNORECASE,
    )
    _JS_OBJECT_PROP_RE = re.compile(
        r'(?:countdown|wait_time|wait_seconds)\s*:\s*(\d+)',
        re.IGNORECASE,
    )
    _JS_FUNC_CALL_RE = re.compile(
        r'(?:download_timer|start_timer|count_down|countdown)\s*\(\s*(\d+)\s*\)',
        re.IGNORECASE,
    )
    _JS_TIMEOUT_RE = re.compile(
        r'setTimeout\s*\([^,]+,\s*(\d{4,6})\s*\)',
        re.IGNORECASE,
    )

    # 7. JSON wait fields (embedded JSON or pure JSON response bodies)
    _JSON_WAIT_RE = re.compile(
        r'["\']?(wait|wait_time|wait_seconds|retry_after|retry-after|cooldown)["\']?\s*[:=]\s*["\']?(\d+)',
        re.IGNORECASE,
    )
    _JSON_WAIT_KEYS = {"wait", "wait_time", "wait_seconds", "retry_after", "retry-after", "cooldown"}

    # 8. Human-readable wait text: "Please wait 45 seconds", "try again in 2 minutes"
    _TEXT_WAIT_PATTERNS = (
        re.compile(r"\b(?:please\s+)?wait(?:ing)?\s+(\d+)\s*(minutes?|mins?|m|seconds?|secs?|s)\b", re.IGNORECASE),
        re.compile(r"\bwait\s*(?:time)?\s*[:\-]\s*(\d+)\s*(minutes?|mins?|m|seconds?|secs?|s)?\b", re.IGNORECASE),
        re.compile(r"\btry\s+again\s+in\s+(\d+)\s*(minutes?|mins?|m|seconds?|secs?|s)?\b", re.IGNORECASE),
        re.compile(r"\b(?:ready|available|retry|resume[sd]?|reload)\s+in\s+(\d+)\s*(minutes?|mins?|m|seconds?|secs?|s)?\b", re.IGNORECASE),
        re.compile(r"\b(?:please\s+)?wait\s+(\d+)\b", re.IGNORECASE),
    )

    @classmethod
    def detect_candidates(
        cls,
        text: str | None,
        headers: Mapping[str, str] | None = None,
    ) -> list[TimerCandidate]:
        """Collects every timer candidate, ranked by confidence (best first)."""
        candidates: list[TimerCandidate] = []

        cls._collect_header_candidates(candidates, headers)
        if text:
            cls._collect_meta_candidates(candidates, text)
            cls._collect_json_candidates(candidates, text)
            cls._collect_vue_candidates(candidates, text)
            cls._collect_data_attr_candidates(candidates, text)
            cls._collect_html_tag_candidates(candidates, text)
            cls._collect_js_candidates(candidates, text)
            cls._collect_text_candidates(candidates, text)

        deduped: list[TimerCandidate] = []
        seen: set[tuple[str, int, str | None, str]] = set()
        for cand in candidates:
            signature = (cand.source, cand.seconds, cand.raw, cand.note)
            if signature in seen:
                continue
            seen.add(signature)
            deduped.append(cand)

        deduped.sort(key=lambda c: (0 if c.valid else 1, -c.confidence))
        return deduped

    @classmethod
    def best_candidate(cls, candidates: list[TimerCandidate] | None) -> TimerCandidate | None:
        """Returns the highest-confidence valid candidate, or ``None``."""
        if not candidates:
            return None
        ranked = sorted(candidates, key=lambda c: (0 if c.valid else 1, -c.confidence))
        for cand in ranked:
            if cand.valid and cand.seconds >= cls.MIN_SECONDS:
                return cand
        return None

    # ------------------------------------------------------------------
    # Back-compat single-result API
    # ------------------------------------------------------------------
    @classmethod
    def detect_from_html(
        cls,
        html_text: str | None,
        headers: Mapping[str, str] | None = None,
    ) -> TimerDetectionResult:
        """Inspects headers and HTML text for countdown timer indicators."""
        candidates = cls.detect_candidates(html_text, headers)
        best = cls.best_candidate(candidates)
        if best is None:
            return TimerDetectionResult(has_timer=False, seconds_remaining=0, detection_source="none")
        return TimerDetectionResult(
            has_timer=True,
            seconds_remaining=best.seconds,
            detection_source=best.source,
            raw_match=best.raw,
            selector=best.selector,
        )

    # ------------------------------------------------------------------
    # Collectors
    # ------------------------------------------------------------------
    @classmethod
    def _collect_header_candidates(cls, candidates: list[TimerCandidate], headers: Mapping[str, str] | None) -> None:
        if not headers:
            return
        normalized = {str(k).lower(): str(v) for k, v in headers.items()}
        for key in cls._HEADER_KEYS:
            val = normalized.get(key)
            if not val:
                continue
            raw = f"{key}: {val.strip()}"
            seconds, ok, note = cls._parse_header_seconds(val)
            valid = ok and cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
            candidates.append(TimerCandidate(
                source="http_header",
                seconds=max(0, seconds),
                confidence=cls._CONF_HEADER if valid else 0.0,
                raw=raw,
                selector=key,
                valid=valid,
                note=note if not valid else "",
            ))

    @staticmethod
    def _parse_header_seconds(val: str) -> tuple[int, bool, str]:
        clean = str(val).strip()
        if clean.isdigit():
            return int(clean), True, ""
        try:
            parsed = parsedate_to_datetime(clean)
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = int((parsed - datetime.now(timezone.utc)).total_seconds())
                return seconds, True, "http_date"
        except Exception:
            pass
        return 0, False, "unparseable"

    @classmethod
    def _collect_meta_candidates(cls, candidates: list[TimerCandidate], text: str) -> None:
        for m in cls._META_REFRESH_RE.finditer(text):
            seconds = int(m.group(1))
            valid = cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
            candidates.append(TimerCandidate(
                source="meta_refresh",
                seconds=seconds,
                confidence=cls._CONF_META_REFRESH if valid else 0.0,
                raw=m.group(0),
                selector='meta[http-equiv="refresh"]',
                valid=valid,
                note="" if valid else "out_of_range",
            ))

    @classmethod
    def _collect_json_candidates(cls, candidates: list[TimerCandidate], text: str) -> None:
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                data = json.loads(stripped)
                for path, seconds in cls._walk_json_wait(data):
                    valid = cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
                    candidates.append(TimerCandidate(
                        source="json_wait",
                        seconds=seconds,
                        confidence=cls._CONF_BODY_JSON if valid else 0.0,
                        raw=f"json:{path}",
                        selector=path,
                        valid=valid,
                        note="" if valid else "out_of_range",
                    ))
            except Exception:
                pass
        for m in cls._JSON_WAIT_RE.finditer(text):
            seconds = int(m.group(2))
            valid = cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
            candidates.append(TimerCandidate(
                source="json_wait",
                seconds=seconds,
                confidence=cls._CONF_JSON_EMBEDDED if valid else 0.0,
                raw=m.group(0),
                selector=str(m.group(1)).lower(),
                valid=valid,
                note="" if valid else "out_of_range",
            ))

    @classmethod
    def _walk_json_wait(cls, data: Any, prefix: str = "") -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        if isinstance(data, dict):
            for key, value in data.items():
                key_str = str(key)
                path = f"{prefix}.{key_str}" if prefix else key_str
                if key_str.lower() in cls._JSON_WAIT_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
                    found.append((path, int(value)))
                elif isinstance(value, (dict, list)):
                    found.extend(cls._walk_json_wait(value, path))
        elif isinstance(data, list):
            for idx, value in enumerate(data):
                found.extend(cls._walk_json_wait(value, f"{prefix}[{idx}]"))
        return found

    @classmethod
    def _collect_vue_candidates(cls, candidates: list[TimerCandidate], text: str) -> None:
        for regex in (cls._VUE_PROP_RE, cls._VUE_STATIC_PROP_RE):
            for m in regex.finditer(text):
                seconds = int(m.group(1))
                valid = cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
                candidates.append(TimerCandidate(
                    source="vue_prop",
                    seconds=seconds,
                    confidence=cls._CONF_VUE_PROP if valid else 0.0,
                    raw=m.group(0),
                    selector="download-countdown",
                    valid=valid,
                    note="" if valid else "out_of_range",
                ))

    @classmethod
    def _collect_data_attr_candidates(cls, candidates: list[TimerCandidate], text: str) -> None:
        for m in cls._DATA_ATTR_RE.finditer(text):
            seconds = int(m.group(1))
            valid = cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
            candidates.append(TimerCandidate(
                source="data_attribute",
                seconds=seconds,
                confidence=cls._CONF_DATA_ATTR if valid else 0.0,
                raw=m.group(0),
                valid=valid,
                note="" if valid else "out_of_range",
            ))

    @classmethod
    def _collect_html_tag_candidates(cls, candidates: list[TimerCandidate], text: str) -> None:
        for m in cls._HTML_TAG_RE.finditer(text):
            seconds = int(m.group(1))
            valid = cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
            candidates.append(TimerCandidate(
                source="html_tag",
                seconds=seconds,
                confidence=cls._CONF_HTML_TAG if valid else 0.0,
                raw=m.group(0),
                valid=valid,
                note="" if valid else "out_of_range",
            ))

    @classmethod
    def _collect_js_candidates(cls, candidates: list[TimerCandidate], text: str) -> None:
        specs = (
            (cls._JS_OBJECT_PROP_RE, cls._CONF_JS_OBJECT_PROP),
            (cls._JS_VAR_RE, cls._CONF_JS_VAR),
            (cls._JS_FUNC_CALL_RE, cls._CONF_JS_FUNC),
        )
        for regex, confidence in specs:
            for m in regex.finditer(text):
                seconds = int(m.group(1))
                valid = cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
                candidates.append(TimerCandidate(
                    source="js_variable",
                    seconds=seconds,
                    confidence=confidence if valid else 0.0,
                    raw=m.group(0),
                    valid=valid,
                    note="" if valid else "out_of_range",
                ))
        for m in cls._JS_TIMEOUT_RE.finditer(text):
            milliseconds = int(m.group(1))
            seconds = milliseconds // 1000
            valid = cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
            candidates.append(TimerCandidate(
                source="js_variable",
                seconds=seconds,
                confidence=cls._CONF_JS_TIMEOUT if valid else 0.0,
                raw=m.group(0),
                valid=valid,
                note="" if valid else "out_of_range",
            ))

    @classmethod
    def _collect_text_candidates(cls, candidates: list[TimerCandidate], text: str) -> None:
        for regex in cls._TEXT_WAIT_PATTERNS:
            for m in regex.finditer(text):
                seconds = int(m.group(1))
                unit = (m.group(2) or "") if (m.lastindex or 0) >= 2 else ""
                if unit.lower().startswith("m") and not unit.lower().startswith("ms"):
                    seconds *= 60
                valid = cls.MIN_SECONDS <= seconds <= cls.MAX_SECONDS
                candidates.append(TimerCandidate(
                    source="text_wait",
                    seconds=seconds,
                    confidence=cls._CONF_TEXT_WAIT if valid else 0.0,
                    raw=m.group(0),
                    valid=valid,
                    note="" if valid else "out_of_range",
                ))

    @classmethod
    def extract_dom_script(cls) -> str:
        """Returns a self-contained JavaScript snippet to evaluate inside a live browser page."""
        return """() => {
    let dl = document.querySelector('download-countdown');
    if (dl) {
        let cdAttr = dl.getAttribute(':countdown') || dl.getAttribute('countdown');
        if (cdAttr && /^\\d+$/.test(cdAttr)) {
            let s = parseInt(cdAttr);
            if (s > 0) return { has_timer: true, seconds: s, source: 'vue_prop' };
        }
        let textMatch = (dl.innerText || '').match(/(\\d+)\\s*s/i);
        if (textMatch) {
            let s = parseInt(textMatch[1]);
            if (s > 0) return { has_timer: true, seconds: s, source: 'vue_text' };
        }
    }
    let el = document.querySelector('[data-timer], [data-countdown], [data-wait], #countdown, #timer, .countdown, .timer, #seconds, #dl_countdown');
    if (el) {
        for (let attr of ['data-timer', 'data-countdown', 'data-wait', 'data-seconds']) {
            let v = el.getAttribute(attr);
            if (v && /^\\d+$/.test(v)) {
                let s = parseInt(v);
                if (s > 0 && s <= 3600) return { has_timer: true, seconds: s, source: 'data_attribute' };
            }
        }
        let m = (el.innerText || '').match(/(\\d+)/);
        if (m) {
            let s = parseInt(m[1]);
            if (s > 0 && s <= 3600) return { has_timer: true, seconds: s, source: 'html_text' };
        }
    }
    for (let k of ['countdown', 'timer', 'seconds', 'waitTime', 'wait_time']) {
        if (typeof window[k] === 'number' && window[k] > 0 && window[k] <= 3600) {
            return { has_timer: true, seconds: window[k], source: 'js_variable' };
        }
    }
    return { has_timer: false, seconds: 0, source: 'none' };
}"""
