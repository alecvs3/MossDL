from __future__ import annotations

import json
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from typing import Any


def _validate_endpoint(endpoint: str) -> None:
    if urlsplit(endpoint).scheme not in {"http", "https"}:
        raise ValueError("notification endpoint must use http or https")


def deliver(sink: dict[str, Any], event: dict[str, Any], timeout: float = 10.0) -> None:
    """Deliver one redacted outbox event. Credentials are resolved outside this module."""
    if sink.get("kind") == "desktop":
        # Keep this dependency-free: the Windows shell provides the native beep
        # primitive and other platforms simply retain the durable event.
        if event.get("notification_sound", True):
            try:
                import winsound
                sounds = {
                    "Windows Notify": winsound.MB_OK,
                    "Chime": winsound.MB_ICONASTERISK,
                    "Ding": winsound.MB_ICONEXCLAMATION,
                }
                winsound.MessageBeep(sounds.get(event.get("sound_preset"), winsound.MB_OK))
            except (ImportError, RuntimeError, AttributeError):
                pass
        return
    if sink.get("kind") != "webhook":
        raise ValueError("unsupported notification sink kind")
    _validate_endpoint(sink["endpoint"])
    body = json.dumps({
        "id": event["id"], "type": event["event_type"], "task_id": event.get("task_id"),
        "created_at": event["created_at"], "payload": event.get("payload", {}),
    }).encode("utf-8")
    request = Request(sink["endpoint"], data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        if response.status >= 300:
            raise ValueError(f"notification endpoint returned HTTP {response.status}")
