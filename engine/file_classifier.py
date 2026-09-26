"""Deterministic, provider-neutral file categorization."""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit


FileCategory = str

# Append-only: stored tasks and settings refer to these names.
DEFAULT_CATEGORIES = (
    "video", "audio", "music", "pictures", "documents", "archives",
    "applications", "code_data", "subtitles", "other",
    "ebooks", "disk_images", "fonts", "models_3d", "torrents", "checksums",
)

_MIME_PREFIXES: tuple[tuple[str, FileCategory], ...] = (
    ("video/", "video"),
    ("audio/", "audio"),
    ("image/", "pictures"),
    ("font/", "fonts"),
    ("model/", "models_3d"),
    ("text/", "documents"),
)

# One line per category; the first category to list an extension owns it.
_EXTENSION_LISTS: tuple[tuple[FileCategory, str], ...] = (
    ("music", "mp3 flac wav ogg m4a aac alac opus ape wma aiff aif dsf dff mka wv mid midi amr ac3 dts"),
    # .ts is MPEG transport stream here: a download manager sees far more video
    # segments than TypeScript sources.
    ("video", "mp4 mkv webm avi mov wmv m4v mpeg mpg ts m2ts mts vob 3gp flv f4v ogv rm rmvb divx asf m3u8 mpd"),
    ("pictures", "jpg jpeg png gif webp bmp tiff tif svg heic heif avif jxl ico psd raw cr2 cr3 nef arw dng orf rw2 xcf"),
    ("ebooks", "epub mobi azw azw3 fb2 djvu cbz cbr cb7"),
    ("documents", "pdf doc docx odt rtf txt md xls xlsx ods csv ppt pptx odp pages numbers key tex"),
    ("disk_images", "iso img cue nrg mdf mds vhd vhdx vmdk qcow2 wim esd"),
    ("archives", "zip 7z rar tar gz bz2 xz zst lz lz4 lzma tgz tbz2 txz cab arj lzh ace"),
    ("applications", "exe msi msix appx appimage dmg pkg deb rpm apk xapk apks ipa jar flatpak snap"),
    ("code_data", "py js tsx jsx rs go java c cpp h cs json yaml yml xml sql toml ini cfg db sqlite"),
    ("subtitles", "srt vtt ass ssa sub idx sup lrc ttml"),
    ("fonts", "ttf otf woff woff2 eot"),
    ("models_3d", "obj fbx glb gltf stl blend 3mf dae usdz ply"),
    ("torrents", "torrent"),
    ("checksums", "sfv md5 sha1 sha256 nfo par2"),
)

_EXTENSIONS: dict[str, FileCategory] = {}
for _category, _names in _EXTENSION_LISTS:
    for _name in _names.split():
        _EXTENSIONS.setdefault(f".{_name}", _category)

# Split archive volumes: .r00, .z01, .001 (also .7z.001 and .zip.002).
_SPLIT_VOLUME = re.compile(r"^\.(?:r\d{2}|z\d{2}|\d{3})$")


@dataclass(frozen=True, slots=True)
class FileClassification:
    category: FileCategory
    source: str
    confidence: float
    extension: str | None = None
    mime: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extension(value: str | None) -> str | None:
    if not value:
        return None
    path = urlsplit(value).path if "://" in value else value
    name = PureWindowsPath(path).name or PurePosixPath(path).name
    suffix = PurePosixPath(name).suffix.lower()
    return suffix or None


def classify_file(*, mime: str | None = None, filename: str | None = None,
                  source_url: str | None = None, provider_hint: str | None = None,
                  overrides: dict[str, str] | None = None) -> FileClassification:
    """Classify without network access or provider-specific code.

    MIME wins, followed by the resolved filename, source URL filename, then
    provider hints.  Overrides are keyed by lowercase extension and are only
    applied at the extension stages so MIME remains authoritative.
    """
    normalized_mime = (mime or "").split(";", 1)[0].strip().lower() or None
    for prefix, category in _MIME_PREFIXES:
        if normalized_mime and normalized_mime.startswith(prefix):
            return FileClassification(category, "mime", 1.0, _extension(filename or source_url), normalized_mime)
    if normalized_mime == "application/pdf":
        return FileClassification("documents", "mime", 1.0, _extension(filename or source_url), normalized_mime)
    if normalized_mime in {"application/zip", "application/x-7z-compressed", "application/x-rar-compressed", "application/gzip"}:
        return FileClassification("archives", "mime", 1.0, _extension(filename or source_url), normalized_mime)

    mapping = {str(key).lower(): str(value) for key, value in (overrides or {}).items()}
    for candidate, source in ((filename, "resolved_extension"), (source_url, "source_extension")):
        extension = _extension(candidate)
        if extension and extension in mapping and mapping[extension] in DEFAULT_CATEGORIES:
            return FileClassification(mapping[extension], source, 0.9 if source.startswith("resolved") else 0.8, extension, normalized_mime)
        if extension and extension in _EXTENSIONS:
            return FileClassification(_EXTENSIONS[extension], source, 0.9 if source.startswith("resolved") else 0.8, extension, normalized_mime)
        if extension and _SPLIT_VOLUME.match(extension):
            return FileClassification("archives", source, 0.8 if source.startswith("resolved") else 0.7, extension, normalized_mime)
    hint = (provider_hint or "").lower()
    if any(token in hint for token in ("music", "song", "album")):
        return FileClassification("music", "provider_hint", 0.5, _extension(filename or source_url), normalized_mime)
    if any(token in hint for token in ("video", "movie", "film")):
        return FileClassification("video", "provider_hint", 0.5, _extension(filename or source_url), normalized_mime)
    return FileClassification("other", "fallback", 0.0, _extension(filename or source_url), normalized_mime)


def category_for_item(item: Any, overrides: dict[str, str] | None = None) -> FileClassification:
    metadata = getattr(item, "metadata", {}) or {}
    filename = getattr(item, "display_name", None) or metadata.get("filename")
    return classify_file(mime=metadata.get("content_type") or metadata.get("mime"),
                         filename=filename, source_url=getattr(item, "source_url", None),
                         provider_hint=metadata.get("category") or metadata.get("type"),
                         overrides=overrides)
