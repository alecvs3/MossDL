from __future__ import annotations

import ctypes
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4
from typing import Any, Callable


_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://[^\s<>\"']+")

# Links typed without a scheme ("example.com/file", "www.site.org"). A bare
# "name.tld" also looks like a file name ("setup.zip", "main.py"), so a token
# only counts when its last label is a real top-level domain (IANA list in
# tlds.txt) and, if that TLD doubles as a common file extension, the token
# carries a path, a port or "www.".
_BARE_RE = re.compile(r"(?i)(?<![\w@/.:-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}|xn--[a-z0-9-]+)(:\d{1,5})?(/[^\s<>\"']*)?")
_EXTENSION_LIKE_TLDS = {"zip", "mov", "py", "sh", "rs", "md", "pl", "ps", "ai", "cc", "sx", "gz", "tv", "so", "bz", "js", "cs", "ts", "go", "pm", "rb", "php", "exe", "app", "dev", "cat", "bat", "cab", "mp", "ms", "gg"}
_TLDS: set[str] | None = None


def _tlds() -> set[str]:
    global _TLDS
    if _TLDS is None:
        from pathlib import Path
        lines = Path(__file__).with_name("tlds.txt").read_text(encoding="ascii").splitlines()
        _TLDS = {line.strip().lower() for line in lines if line.strip() and not line.startswith("#")}
    return _TLDS


def bare_url(token: str) -> str | None:
    """https:// URL for a scheme-less link, or None when the token is not one."""
    m = _BARE_RE.fullmatch(token.strip().rstrip(".,;:!?)"))
    if not m:
        return None
    host, port, path = m.group(1).lower(), m.group(2) or "", m.group(3) or ""
    tld = host.rsplit(".", 1)[-1]
    if tld not in _tlds():
        return None
    if tld in _EXTENSION_LIKE_TLDS and not (path or port or host.startswith("www.")):
        return None
    return f"https://{host}{port}{path}"



def normalize_url(value: str) -> str | None:
    value = value.strip().strip("()[]{}<>,.;\"")
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https", "ftp"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))


def extract_urls(text: str) -> list[str]:
    """Every link in the text, in order; scheme-less links get https://."""
    seen: set[str] = set()
    found: list[tuple[int, str]] = []
    text = text or ""
    covered: list[tuple[int, int]] = []
    for match in _URL_RE.finditer(text):
        covered.append(match.span())
        normalized = normalize_url(match.group(0))
        if normalized:
            found.append((match.start(), normalized))
    for match in _BARE_RE.finditer(text):
        if any(a <= match.start() < b for a, b in covered):
            continue
        candidate = bare_url(match.group(0))
        normalized = normalize_url(candidate) if candidate else None
        if normalized:
            found.append((match.start(), normalized))
    result: list[str] = []
    for _, url in sorted(found):
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


class ClipboardWatcher:
    """Consent-gated Windows clipboard watcher; no clipboard access when disabled."""

    def __init__(self, on_text: Callable[[str], Any], interval: float = 0.8) -> None:
        self.on_text = on_text
        self.interval = max(0.25, interval)
        self.enabled = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last = ""

    def start(self) -> None:
        if self.enabled and self._thread and self._thread.is_alive():
            return
        self.enabled = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="linkgrabber-clipboard")
        self._thread.start()

    def stop(self) -> None:
        self.enabled = False
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)

    @staticmethod
    def _read_windows() -> str:
        if not hasattr(ctypes, "windll"):
            return ""
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                text = self._read_windows()
                if text and text != self._last:
                    self._last = text
                    self.on_text(text)
            except Exception:
                # Clipboard contention and unsupported platforms are non-fatal.
                continue


def new_link(url: str, source: str = "clipboard", source_context: str | None = None) -> dict[str, Any]:
    normalized = normalize_url(url)
    if not normalized:
        raise ValueError("invalid download URL")
    return {"id": str(uuid4()), "url": url.strip(), "normalized_url": normalized,
            "source": source, "source_context": source_context, "state": "pending",
            "selected": False, "created_at": time.time()}
