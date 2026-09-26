from dataclasses import dataclass
from typing import Any


class EngineError(Exception):
    """Base engine error."""


class NeedsUser(EngineError):
    """The provider needs a user action such as a password or CAPTCHA."""

    def __init__(self, message: str, action: str = "user_action", challenge: dict | None = None) -> None:
        super().__init__(message)
        self.action = action
        self.challenge = challenge or {"action": action, "message": message}


class NeedsCaptcha(NeedsUser):
    """A provider or host requires a CAPTCHA challenge to be solved."""

    def __init__(self, message: str, captcha_type: str, params: dict | None = None,
                 timeout: float = 90.0, challenge_id: str | None = None) -> None:
        challenge = {
            "action": "captcha",
            "captcha_type": captcha_type,
            "params": params or {},
            "timeout_seconds": timeout,
            "challenge_id": challenge_id,
            "message": message,
        }
        super().__init__(message, action="captcha", challenge=challenge)
        self.captcha_type = captcha_type
        self.params = params or {}
        self.timeout = timeout
        self.challenge_id = challenge_id


class ProviderUnavailable(EngineError):
    """A provider cannot resolve the supplied URL."""


class ProviderMappedError(ProviderUnavailable):
    """A provider error classified by a manifest/recipe mapping."""

    def __init__(self, message: str, category: str, status_code: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_after = retry_after


class DownloadPaused(EngineError):
    """A transfer was intentionally paused."""


class DownloadCanceled(EngineError):
    """A transfer was intentionally canceled."""


class QuotaExceeded(EngineError):
    """A provider or route has exceeded its bandwidth/quota limit."""

    def __init__(self, message: str = "quota exceeded", provider_id: str | None = None,
                 retry_after: float | None = None, status_code: int | None = 429) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.retry_after = retry_after
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ProviderFailurePolicy:
    """Stable engine policy for provider/account outcomes."""

    category: str
    retryable: bool = False
    retry_after: float | None = None
    user_action: str | None = None
    quarantine: bool = False


def classify_provider_failure(error: BaseException | str, *, status_code: int | None = None,
                              retry_after: float | None = None) -> ProviderFailurePolicy:
    """Map provider failures to durable, actionable engine semantics.

    Provider messages are untrusted input. Classification is deliberately
    conservative: only explicit plugin faults are quarantine candidates;
    normal quota, authentication, and user-action outcomes remain recoverable
    provider states.
    """
    category = str(getattr(error, "category", "") or "").lower().replace("-", "_")
    text = str(error).lower()
    if isinstance(error, QuotaExceeded) or category in {"quota", "quota_exceeded"} or \
            status_code in {429, 509} or any(marker in text for marker in ("quota", "traffic exhausted", "bandwidth limit")):
        return ProviderFailurePolicy("quota", retryable=True,
                                    retry_after=retry_after if retry_after is not None else getattr(error, "retry_after", None))
    if category in {"captcha", "challenge"} or "captcha" in text:
        return ProviderFailurePolicy("captcha", user_action="captcha")
    if category in {"login_required", "authentication", "auth_required"} or \
            any(marker in text for marker in ("login required", "authentication required", "not authenticated")):
        return ProviderFailurePolicy("login_required", user_action="login")
    if category in {"expired_source", "refresh_required", "refresh_failed"} or "expired source" in text:
        return ProviderFailurePolicy("refresh_failed" if category == "refresh_failed" else "expired_source",
                                    retryable=category != "refresh_failed", user_action="refresh")
    if category in {"unsupported", "not_supported"} or "unsupported" in text:
        return ProviderFailurePolicy("unsupported")
    if category in {"admission_rejected", "disabled", "quarantined"}:
        return ProviderFailurePolicy("admission_rejected")
    if category in {"provider_unavailable", "unavailable", "network"} or \
            any(marker in text for marker in ("provider unavailable", "temporarily unavailable")):
        return ProviderFailurePolicy("provider_unavailable", retryable=True,
                                    retry_after=retry_after)
    if category in {"plugin_fault", "malformed_result", "sandbox_violation"}:
        return ProviderFailurePolicy("plugin_fault", quarantine=True)
    return ProviderFailurePolicy(category or "provider_failure", retryable=False)
