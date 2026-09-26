from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit
from typing import Any


def source_fingerprint(url: str) -> str:
    parts = urlsplit(url.strip())
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def route_error_is_switchable(error: str = "", status_code: int | None = None,
                              category: str | None = None) -> bool:
    """Only connectivity failures can trigger automatic route changes."""
    if status_code in {401, 403, 429, 451} or category in {"quota", "auth", "captcha", "policy", "rate_limit"}:
        return False
    text = error.lower()
    return category in {"network", "dns", "timeout", "reconnect"} or any(
        marker in text for marker in ("timed out", "timeout", "dns", "connection reset", "connection refused", "name or service")
    )


class ProviderFallback:
    def __init__(self, store):
        self.store = store

    def lock_quota(
        self,
        task_id: str,
        provider_id: str,
        url: str,
        route_profile_id: str = "direct",
        retry_after: float | None = None,
        error: str | None = None,
    ) -> None:
        """
        Locks out a provider / mirror URL due to quota or bandwidth exhaustion.
        Records an outcome of 'quota_exceeded' which permanently prevents backtracking.
        """
        fingerprint = source_fingerprint(url)
        self.store.record_provider_attempt(
            task_id,
            fingerprint,
            provider_id,
            route_profile_id,
            outcome="quota_exceeded",
            status_code=429,
            retry_after=retry_after,
            error=error or "Quota/bandwidth limit reached",
        )

    def is_quota_locked(self, task_id: str, url: str, provider_id: str | None = None) -> bool:
        """
        Checks if a URL or provider has been locked out due to quota exhaustion.
        """
        fingerprint = source_fingerprint(url)
        attempts = self.store.list_provider_attempts(task_id, fingerprint)
        for row in attempts:
            if row.get("outcome") == "quota_exceeded":
                return True
            if provider_id and row.get("provider_id") == provider_id and row.get("outcome") == "quota_exceeded":
                return True
        return False

    def candidates(
        self,
        task_id: str,
        alternatives: list[dict[str, Any]],
        route_profile_id: str = "direct",
        strict_quota_exclusion: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Returns alternate candidates, strictly filtering out any provider or mirror
        that has been quota'd or previously failed on the given route profile.
        """
        result = []
        all_task_attempts = self.store.list_provider_attempts(task_id)

        # Collect set of non-generic providers that have been quota'd for this task
        quota_locked_providers = {
            row["provider_id"] for row in all_task_attempts
            if row.get("outcome") == "quota_exceeded" and row.get("provider_id") != "generic"
        }
        quota_locked_fingerprints = {
            row["source_fingerprint"] for row in all_task_attempts
            if row.get("outcome") == "quota_exceeded"
        }

        for candidate in alternatives:
            url = str(candidate.get("url", "")).strip()
            if not url:
                continue
            provider = str(candidate.get("provider_id") or candidate.get("provider") or "generic")
            fingerprint = source_fingerprint(url)

            # Strict Quota Rule: Never switch back to a provider or URL that has been quota'd
            if strict_quota_exclusion:
                if fingerprint in quota_locked_fingerprints:
                    continue
                if provider != "generic" and provider in quota_locked_providers:
                    continue

            # Standard duplicate attempt rule on the same route profile
            attempted = [row for row in all_task_attempts if row["source_fingerprint"] == fingerprint]
            if any(row["provider_id"] == provider and row["route_profile_id"] == route_profile_id for row in attempted):
                continue

            result.append({
                **candidate,
                "provider_id": provider,
                "source_fingerprint": fingerprint,
                "route_profile_id": route_profile_id,
            })
        return result

    def record(self, task_id: str, url: str, provider_id: str, route_profile_id: str,
               outcome: str, status_code: int | None = None, retry_after: float | None = None,
               error: str | None = None) -> None:
        self.store.record_provider_attempt(task_id, source_fingerprint(url), provider_id,
                                           route_profile_id, outcome, status_code, retry_after, error)
