"""score-based title extractor for download tasks.

Workers extract candidate titles from multiple sources; the highest-scoring
candidate wins. Penalises raw hash/numeric IDs, bonus for human-readable names.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from email.message import Message
from typing import Any


# ---------------------------------------------------------------------------
# Scoring constants
# ---------------------------------------------------------------------------
_BONUS_WORD_RATIO = 20          # at least 40 % real word characters
_BONUS_MEDIA_EXT = 25           # recognised media / archive extension
_BONUS_RELEASE_TAG = 15         # version or year token present
_BONUS_TITLE_CASE = 10          # at least one capitalised word

_PENALTY_PURE_DIGITS = -45      # entirely numeric slug (e.g. "48194819.bin")
_PENALTY_RANDOM_HEX = -40       # looks like MD5/UUID/sha256 without extension
_PENALTY_GENERIC_WORD = -35     # download / file / archive / stream
_PENALTY_URL_SLUG = -25         # contains % encoding or raw query

_GENERIC_WORDS = frozenset({
    "download", "file", "archive", "attachment", "stream",
    "get", "fetch", "resource", "index", "temp", "tmp", "data",
    "unknown", "unnamed", "untitled", "media",
})

_MEDIA_EXTENSIONS = frozenset({
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm",
    "mp3", "flac", "aac", "wav", "ogg", "m4a",
    "rar", "7z", "zip", "tar", "gz", "iso", "bin",
    "exe", "msi", "dmg", "apk",
    "pdf", "epub", "cbz", "cbr",
})

_RE_HEX_HASH = re.compile(r'^[0-9a-f]{16,}$', re.I)
_RE_UUID = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I
)
_RE_PURE_DIGITS = re.compile(r'^\d+$')
_RE_VERSION = re.compile(r'\bv?\d+\.\d+(?:\.\d+)*\b|\b(19|20)\d{2}\b')
_RE_WORD_CHARS = re.compile(r'[a-zA-Z]')
_RE_SITE_SUFFIX = re.compile(
    r'(?:\s*[|\-–—]\s*)(?:www\.)?[a-z0-9.-]+\.[a-z]{2,6}\s*$', re.I
)
# A release site's tag in a file name: "Game_--_site.example_--_.part1.rar".
# Any domain between "--" separators, so no site needs naming here; the last
# label must be letters, so versions like "--_v1.2.3" are kept.
RE_SITE_TAG = re.compile(
    r'[-_ ]*--[-_ ]*(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}(?:[-_ ]*--)?[-_ ]*', re.I
)
_RE_REPACK_TAG = RE_SITE_TAG


@dataclass
class TitleCandidate:
    text: str
    source: str   # "provider"|"header"|"url_param"|"url_path"|"page_title"|"sibling"
    score: int = 0
    rationale: str = ""


# ---------------------------------------------------------------------------
# Candidate extractors
# ---------------------------------------------------------------------------

def extract_header_candidates(headers: dict[str, str] | Message, url: str = "") -> list[TitleCandidate]:
    """Pull filename from Content-Disposition (RFC 5987 UTF-8 and ASCII)."""
    if isinstance(headers, dict):
        disposition = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    else:
        disposition = headers.get("Content-Disposition") or ""

    candidates: list[TitleCandidate] = []
    # RFC 5987 encoded form: filename*=UTF-8''...
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", disposition, re.I)
    if m:
        name = urllib.parse.unquote(m.group(1).strip().strip('"'))
        if name:
            candidates.append(TitleCandidate(name, "header"))
    # Plain form: filename="..."
    m = re.search(r'filename=["\']?([^"\';\r\n]+)["\']?', disposition, re.I)
    if m:
        name = m.group(1).strip().strip('"\'')
        if name:
            candidates.append(TitleCandidate(name, "header"))
    # Fallback: leaf of URL path
    if not candidates and url:
        leaf = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].split("?")[0])
        if leaf and "." in leaf:
            candidates.append(TitleCandidate(leaf, "url_path"))
    return candidates


def extract_url_candidates(url: str) -> list[TitleCandidate]:
    """Pull filename from query params and URL path."""
    candidates: list[TitleCandidate] = []
    try:
        parsed = urllib.parse.urlsplit(url)
        params = urllib.parse.parse_qs(parsed.query)
        for key in ("filename", "file", "name", "title", "fn", "fname"):
            if key in params and params[key]:
                val = urllib.parse.unquote(params[key][0]).strip()
                if val:
                    candidates.append(TitleCandidate(val, "url_param"))
        leaf = urllib.parse.unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1].split("?")[0])
        if leaf and "." in leaf:
            candidates.append(TitleCandidate(leaf, "url_path"))
    except Exception:
        pass
    return candidates


def extract_page_candidates(page_title: str, meta_tags: dict[str, str] | None = None) -> list[TitleCandidate]:
    """Derive a clean name from an HTML page title."""
    candidates: list[TitleCandidate] = []
    for raw in [page_title, (meta_tags or {}).get("og:title", ""), (meta_tags or {}).get("description", "")]:
        if not raw:
            continue
        # strip "title | SiteName" brand suffix
        cleaned = _RE_SITE_SUFFIX.sub("", raw).strip()
        if cleaned:
            candidates.append(TitleCandidate(cleaned, "page_title"))
    return candidates


def extract_provider_candidates(metadata: dict[str, Any]) -> list[TitleCandidate]:
    """Pull titles from provider-supplied metadata dicts."""
    candidates: list[TitleCandidate] = []
    for key in ("title", "name", "display_name", "filename", "file_name", "subject"):
        val = metadata.get(key)
        if val and isinstance(val, str):
            candidates.append(TitleCandidate(val.strip(), "provider"))
    return candidates


def extract_sibling_candidates(sibling_names: list[str]) -> list[TitleCandidate]:
    """Infer a package title from the common prefix across siblings."""
    if len(sibling_names) < 2:
        return []
    cleaned = []
    for name in sibling_names:
        # drop part suffix: .part01.rar, .001, .r00
        base = re.sub(r'[._-]?(?:part\.?\d+|\d{2,3}|r\d{2,}|z\d{2,})\.[a-z0-9]{1,5}$', '', name, flags=re.I)
        base = re.sub(r'\.[a-z0-9]{2,5}$', '', base, flags=re.I)
        cleaned.append(base.strip())
    # longest common prefix
    prefix = cleaned[0]
    for s in cleaned[1:]:
        while not s.startswith(prefix) and prefix:
            prefix = prefix[:-1]
    prefix = re.sub(r'[-_. ]+$', '', prefix).strip()
    if len(prefix) >= 4:
        return [TitleCandidate(prefix, "sibling")]
    return []


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _stem(text: str) -> str:
    """Filename stem without extension."""
    return text.rsplit(".", 1)[0] if "." in text else text


def score_title_candidate(cand: TitleCandidate) -> TitleCandidate:
    """Assign and return the scored candidate (mutates in place)."""
    raw = cand.text.strip()
    stem = _stem(raw)
    ext = raw.rsplit(".", 1)[-1].lower() if "." in raw else ""

    score = 0
    notes: list[str] = []

    # Source baseline
    source_baseline = {"provider": 30, "header": 25, "page_title": 20, "url_param": 18,
                       "sibling": 15, "url_path": 8}.get(cand.source, 5)
    score += source_baseline

    # Penalties
    if _RE_PURE_DIGITS.match(stem):
        score += _PENALTY_PURE_DIGITS
        notes.append("pure_digits")
    elif _RE_HEX_HASH.match(stem) or _RE_UUID.match(stem):
        score += _PENALTY_RANDOM_HEX
        notes.append("hash_or_uuid")

    if stem.lower() in _GENERIC_WORDS:
        score += _PENALTY_GENERIC_WORD
        notes.append("generic_word")

    if "%" in raw or raw.startswith("?"):
        score += _PENALTY_URL_SLUG
        notes.append("url_encoded")

    # Bonuses
    if ext in _MEDIA_EXTENSIONS:
        score += _BONUS_MEDIA_EXT
        notes.append("media_ext")

    letters = len(_RE_WORD_CHARS.findall(stem))
    total = len(stem)
    if total > 0 and letters / total >= 0.4:
        score += _BONUS_WORD_RATIO
        notes.append("word_ratio")

    if _RE_VERSION.search(raw):
        score += _BONUS_RELEASE_TAG
        notes.append("has_version")

    # Check for at least one capitalised word (not all-caps)
    words = re.split(r'[\s_.-]+', stem)
    if any(w[:1].isupper() and not w.isupper() for w in words if len(w) > 1):
        score += _BONUS_TITLE_CASE
        notes.append("title_case")

    cand.score = score
    cand.rationale = ", ".join(notes)
    return cand


def best_title(candidates: list[TitleCandidate], fallback: str = "") -> str:
    """Return the text of the highest-scoring candidate, or `fallback`."""
    if not candidates:
        return fallback
    scored = [score_title_candidate(c) for c in candidates]
    winner = max(scored, key=lambda c: c.score)
    if winner.score < 0 and fallback:
        return fallback
    return winner.text or fallback


def best_package_title(part_names: list[str], provider_title: str = "") -> str:
    """Derive a clean parent package name across a list of part filenames."""
    sibling_cands = extract_sibling_candidates(part_names)
    all_cands: list[TitleCandidate] = []

    # Sibling inference
    all_cands.extend(sibling_cands)

    # Provider override
    if provider_title:
        all_cands.append(TitleCandidate(provider_title, "provider"))

    # Also derive from each part's page title heuristic
    for name in part_names:
        all_cands.extend(extract_url_candidates(name))

    if not all_cands:
        return part_names[0] if part_names else "Package"

    title = best_title(all_cands, part_names[0] if part_names else "Package")

    # Strip repack site tags and normalise whitespace
    title = _RE_REPACK_TAG.sub(" ", title)
    title = re.sub(r'[_]+', ' ', title)
    title = re.sub(r'[-]{2,}', ' ', title)
    title = re.sub(r'\s+', ' ', title).strip(" -_")
    return title or "Package"


def name_from_items(items: list[Any]) -> str:
    """A task name from what the provider resolved: the file, the shared folder, or the package.

    A shared folder (Transfer.it, MEGA, Gofile) names the task after the folder
    and the selected sub-folder: "Show (1994) - 480p — Season 02".
    """
    names = [str(getattr(i, "display_name", "") or "") for i in items]
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    folders = []
    for item in items:
        parts = [p for p in str(getattr(item, "relative_path", "") or "").replace("\\", "/").split("/") if p]
        if parts and parts[-1] == getattr(item, "display_name", None):
            parts = parts[:-1]
        folders.append(parts)
    common: list[str] = []
    for level in zip(*folders):
        if len(set(level)) != 1:
            break
        common.append(level[0])
    if common:
        return common[0] if len(common) == 1 or common[-1] == common[0] else f"{common[0]} — {common[-1]}"
    shared = extract_sibling_candidates(names)  # "Game.part1.rar", "Game.part2.rar" -> "Game"
    if shared:
        prefix = shared[0].text
        if names[0][len(prefix):len(prefix) + 1].isalnum() and prefix[-1:].isalnum():
            prefix = re.sub(r"[^\s._-]*$", "", prefix)  # "Show S01E0" -> "Show"
        prefix = _RE_REPACK_TAG.sub(" ", prefix).strip(" -_.")
        if len(prefix) >= 4:
            return prefix
    return f"{names[0]} (+{len(names) - 1} items)"
