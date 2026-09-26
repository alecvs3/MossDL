"""In-process media extractor utilizing yt-dlp for universal stream extraction.

Extracts direct audio/video streams, qualities, duration, and thumbnails without
executing external downloader binaries or downloading media payloads.
"""

from __future__ import annotations

import logging
import importlib.util
from typing import Any

from .models import ResolvedItem
from . import route_http

logger = logging.getLogger(__name__)

# yt-dlp takes ~0.4 s to import; load it on first media link, not at engine start.
_HAS_YTDLP = importlib.util.find_spec("yt_dlp") is not None


def _ytdlp() -> Any:
    import yt_dlp
    return yt_dlp


class MediaExtractor:
    """Delegated media extraction engine covering 1,800+ streaming and video sites."""

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout
        self.available = _HAS_YTDLP

    def can_handle(self, url: str) -> bool:
        """Quick check if yt-dlp has a matching non-generic extractor for the URL."""
        if not self.available or not url:
            return False
        try:
            extractors = _ytdlp().extractor.gen_extractors()
            for ie in extractors:
                if ie.IE_NAME != "generic" and ie.suitable(url):
                    return True
        except Exception:
            pass
        return False

    def extract(self, url: str, *, select_format: str = "best") -> dict[str, Any] | None:
        """Extract media metadata and direct stream URLs.
        
        Returns:
            dict with:
                - title: str
                - duration: float | None
                - thumbnail: str | None
                - formats: list of available format options
                - selected_url: direct download stream URL
                - resolved_items: list of ResolvedItem contracts
        """
        if not self.available or not url:
            return None

        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
            "noplaylist": True,
            "socket_timeout": self.timeout,
            # The task's or active route; "" (not None) keeps yt-dlp off HTTP_PROXY too.
            "proxy": route_http.active_proxy() or "",
        }

        try:
            with _ytdlp().YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None

                title = info.get("title") or "media_stream"
                duration = info.get("duration")
                thumbnail = info.get("thumbnail")
                ext = info.get("ext") or "mp4"

                # Parse formats
                raw_formats = info.get("formats") or []
                formats = []
                best_format = None

                for fmt in raw_formats:
                    fmt_url = fmt.get("url")
                    if not fmt_url:
                        continue
                    vcodec = fmt.get("vcodec", "none")
                    acodec = fmt.get("acodec", "none")
                    is_video = vcodec != "none"
                    is_audio = acodec != "none"
                    height = fmt.get("height")
                    fps = fmt.get("fps")
                    filesize = fmt.get("filesize") or fmt.get("filesize_approx")

                    fmt_summary = {
                        "format_id": fmt.get("format_id", ""),
                        "ext": fmt.get("ext", ext),
                        "url": fmt_url,
                        "height": height,
                        "resolution": f"{height}p" if height else ("audio only" if not is_video else "unknown"),
                        "fps": fps,
                        "filesize": filesize,
                        "vcodec": vcodec,
                        "acodec": acodec,
                        "is_video": is_video,
                        "is_audio": is_audio,
                        "protocol": fmt.get("protocol", "https"),
                        "http_headers": fmt.get("http_headers") or {},
                    }
                    formats.append(fmt_summary)

                    # Match specific requested format if specified
                    if select_format and select_format != "best" and fmt.get("format_id") == select_format:
                        best_format = fmt_summary
                        break

                    # Progressive single-file format preference (has both video and audio)
                    if is_video and is_audio and fmt.get("protocol") in ("https", "http"):
                        if best_format is None or (height or 0) > (best_format.get("height") or 0):
                            best_format = fmt_summary

                # If no combined progressive format, pick the top format or root url
                if not best_format and formats:
                    best_format = formats[-1]

                selected_url = best_format.get("url") if best_format else info.get("url")
                if not selected_url:
                    return None

                selected_headers = (best_format or {}).get("http_headers") or {}
                resolved_item = ResolvedItem(
                    provider="media",
                    source_url=url,
                    display_name=f"{title}.{ext}" if not title.endswith(f".{ext}") else title,
                    direct_url=selected_url,
                    size=(best_format or {}).get("filesize"),
                    headers=selected_headers,
                    metadata={
                        "title": title,
                        "duration": duration,
                        "thumbnail": thumbnail,
                        "extractor": info.get("extractor"),
                        "format_id": (best_format or {}).get("format_id"),
                        "resolution": (best_format or {}).get("resolution"),
                    }
                )

                return {
                    "source_url": url,
                    "title": title,
                    "duration": duration,
                    "thumbnail": thumbnail,
                    "selected_url": selected_url,
                    "selected_format": best_format,
                    "formats": formats,
                    "resolved_item": resolved_item,
                }
        except Exception as exc:
            logger.debug("yt-dlp extraction failed for %s: %s", url, exc)
            return None
