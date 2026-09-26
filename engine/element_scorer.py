from __future__ import annotations

import hashlib
import re
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

from .archive_joiner import MultiPartDetector
from .dom_cleaner import CleanedElement, is_ad_domain
from .file_classifier import classify_file
from .shortlink_resolver import _DIRECT_FILE_EXTENSIONS, _KNOWN_STORAGE_HOSTS
from .title_scorer import RE_SITE_TAG

_HIGH_INTENT_KEYWORDS = {
    "download", "direct download", "mirror", "direct link", "get file",
    "save file", "file download", "download now", "click here to download",
    "telecharger", "télécharger", "descargar", "herunterladen", "download file"
}

_SECONDARY_KEYWORDS = {
    "link", "mirror 1", "mirror 2", "mirror 3", "source", "release",
    "asset", "package", "build", "installer", "setup", "portable"
}

# A site's own file server ("cloud.site.com", "dl.site.com") linked with download wording.
_FILE_SERVER_LABELS = {"cloud", "dl", "download", "downloads", "files", "file", "cdn", "storage", "mirror", "get"}

# "Share this post" links: never the download, however download-like the post title in them is.
_SHARE_LINK_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:x\.com/intent|twitter\.com/intent|pinterest\.com/pin/create|api\.whatsapp\.com/send|"
    r"wa\.me/|facebook\.com/sharer|reddit\.com/submit|t\.me/share|telegram\.me/share|linkedin\.com/share|tumblr\.com/share)",
    re.I,
)

_SIZE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:[kKmMgGtT][bB]|[kKmMgGtT]i[bB]|bytes)\b")
_VERSION_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)*\b")
_FILENAME_RE = re.compile(r"([a-zA-Z0-9_\-.]+\.(?:zip|rar|7z|tar|gz|iso|bin|exe|dmg|mp4|mkv|mp3|flac|apk|msi))", re.I)

_KNOWN_SHORTLINK_DOMAINS = {
    "ouo.io", "ouo.press", "shrink-service.it", "adshnk.com", "ashnk.com", "linkvertise.com",
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "adf.ly", "bc.vc", "sh.st",
    "shortener.com", "cuty.io", "shrinkme.io", "shrinkearn.com", "exe.io",
    "gplinks.co", "droplink.co"
}

_STORAGE_BRAND_DOMAINS = {
    "mediafire": "mediafire.com",
    "mega": "mega.nz",
    "meganz": "mega.nz",
    "mega.nz": "mega.nz",
    "datanodes": "datanodes.to",
    "terabox": "terabox.com",
    "pixeldrain": "pixeldrain.com",
    "pixel": "pixeldrain.com",
    "viking": "vikingfile.com",
    "vikingfile": "vikingfile.com",
    "1fichier": "1fichier.com",
    "gofile": "gofile.io",
    "rapidgator": "rapidgator.net",
    "google drive": "drive.google.com",
    "gdrive": "drive.google.com",
    "dropbox": "dropbox.com",
    "krakenfiles": "krakenfiles.com",
    "catbox": "catbox.moe",
    "bunkr": "bunkr.cr",
    "buzzheavier": "buzzheavier.com",
    "qiwi": "qiwi.gg",
    "akira": "akira.to",
}


def normalize_package_name(base_name: str) -> str:
    """Normalizes multi-part archive base name into a clean, human-readable title."""
    # 1. Clean unicode dashes and replacement chars
    s = re.sub(r'[\u2013\u2014\ufffd]+', ' - ', base_name)
    # 2. Strip known repack / community site tags
    s = RE_SITE_TAG.sub(' ', s)
    s = re.sub(r'(?:[-_ ]+)?(?:www\.)?[a-z0-9-]+\.(?:com|org|site|net|to|cr|is|ru)(?:[-_ ]+)?', ' ', s, flags=re.I)
    # 3. Clean underscores and excessive dashes
    s = re.sub(r'[_]+', ' ', s)
    s = re.sub(r'[-]{2,}', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip(" -_")
    return s or base_name


@dataclass
class MatrixElement:
    id: str
    tag: str  # 'a', 'button', 'form'
    text: str
    target_url: str
    host: str
    score: int
    category: str  # 'high_utility' | 'candidate' | 'secondary'
    is_shortlink: bool = False
    method: str = "GET"
    form_data: dict[str, str] | None = None
    filename_hint: str = ""
    size_hint: str = ""
    favicon: str = ""
    page_title: str = ""
    is_multipart: bool = False
    multipart_base: str = ""
    multipart_part: int = 0
    multipart_total: int = 0
    multipart_format: str = ""
    package_name: str = ""
    # Appended: what the target file is (engine/file_classifier.py category),
    # separate from `category`, which ranks how useful the link is.
    file_kind: str = "other"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ElementScorer:
    """
    Evaluates extracted HTML elements and scores them from 0 to 100 based on
    storage host signatures, direct file extensions, high-intent button text,
    metadata clues, and ad/tracker penalties.
    """

    @classmethod
    def extract_host(cls, url: str) -> str:
        if not url:
            return ""
        try:
            parsed = urllib.parse.urlsplit(url)
            host = (parsed.hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            return host
        except Exception:
            return ""

    @classmethod
    def is_storage_host(cls, host: str) -> bool:
        if not host:
            return False
        for sh in _KNOWN_STORAGE_HOSTS:
            if host == sh or host.endswith("." + sh):
                return True
        return False

    @classmethod
    def is_shortlink_host(cls, host: str) -> bool:
        if not host:
            return False
        for sld in _KNOWN_SHORTLINK_DOMAINS:
            if host == sld or host.endswith("." + sld):
                return True
        return False

    @classmethod
    def score_element(cls, elem: CleanedElement, source_url: str = "") -> MatrixElement:
        host = cls.extract_host(elem.target_url)
        target_path = urllib.parse.urlsplit(elem.target_url).path if elem.target_url else ""
        text_lower = (elem.text or "").lower()
        combined_text = f"{elem.text} {elem.target_url} {getattr(elem, 'context_hint', '')}".strip()

        # Detect storage brand mention in button or anchor text
        detected_brand_domain = None
        for brand, brand_domain in _STORAGE_BRAND_DOMAINS.items():
            if re.search(r'\b' + re.escape(brand) + r'\b', text_lower):
                detected_brand_domain = brand_domain
                break

        if detected_brand_domain and (not host or elem.method == "POST" or (source_url and host == cls.extract_host(source_url))):
            host = detected_brand_domain

        score = 0
        filename_hint = ""
        size_hint = ""

        # 1. Hoster / Direct File Match (+40 pts max)
        is_storage = cls.is_storage_host(host) or bool(detected_brand_domain)
        is_direct_file = any(target_path.lower().endswith(ext) for ext in _DIRECT_FILE_EXTENSIONS)

        if is_storage or is_direct_file:
            score += 40

        # 2. Button / Keyword Anchor (+25 pts for high-intent, +10 for secondary)
        high_intent = any(keyword in text_lower for keyword in _HIGH_INTENT_KEYWORDS)
        if detected_brand_domain or high_intent:
            score += 25
        elif any(keyword in text_lower for keyword in _SECONDARY_KEYWORDS):
            score += 10
        if high_intent and host.split(".")[0] in _FILE_SERVER_LABELS and len(host.split(".")) > 2:
            score += 25
        if _SHARE_LINK_RE.match(elem.target_url or ""):
            score -= 80

        # 3. Container & Filename Context (+15 pts)
        size_match = _SIZE_RE.search(combined_text)
        if size_match:
            size_hint = size_match.group(0)

        filename_match = _FILENAME_RE.search(combined_text)
        if filename_match:
            filename_hint = filename_match.group(1)

        version_match = _VERSION_RE.search(combined_text)

        if size_match or filename_match or version_match:
            score += 15

        # Multi-part detection check
        mp_info = None
        target_leaf = urllib.parse.unquote(target_path.split("/")[-1]) if target_path else ""
        candidates = [
            target_leaf,
            re.sub(r'[\u2010-\u2015\u2212\ufffd]+', '-', elem.text or ''),
            re.sub(r'[\u2010-\u2015\u2212\ufffd]+', '-', getattr(elem, "context_hint", "")),
            filename_hint,
        ]
        best_info = None
        for cand in candidates:
            cand_clean = cand.strip()
            if cand_clean:
                detected = MultiPartDetector.detect(cand_clean)
                if detected and len(detected.base_name.strip(" _-.")) > 1:
                    best_info = detected
                    break
                elif detected and not best_info:
                    best_info = detected

        if best_info:
            mp_info = best_info
            if not filename_hint or len(filename_hint) < len(best_info.original_name):
                filename_hint = best_info.original_name

        is_multipart = bool(mp_info and mp_info.is_multipart)
        multipart_base = mp_info.base_name if mp_info else ""
        multipart_part = mp_info.part_number if mp_info else 0
        multipart_format = mp_info.format_type if mp_info else ""

        # 4. Penalties (-50 pts for ads, -20 pts for self-referential links)
        if elem.is_ad or is_ad_domain(elem.target_url):
            score -= 50

        if source_url and elem.target_url and elem.method != "POST":
            s_clean = urllib.parse.urldefrag(source_url)[0]
            t_clean = urllib.parse.urldefrag(elem.target_url)[0]
            if s_clean == t_clean:
                score -= 20

        # 5. Form POST bonus (+15 pts if form has action and tokens)
        if elem.method == "POST" and elem.form_inputs:
            score += 15

        # Clamp score between 0 and 100
        score = max(0, min(100, score))

        # Categorization (Decision D-04)
        if score >= 70:
            category = "high_utility"
        elif score >= 40:
            category = "candidate"
        else:
            category = "secondary"

        # Shortlink detection
        is_shortlink = cls.is_shortlink_host(host)

        # Unique deterministic identifier
        seed = f"{elem.tag}:{elem.method}:{elem.target_url}:{elem.text}"
        elem_id = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()[:16]

        return MatrixElement(
            id=elem_id,
            tag=elem.tag,
            text=elem.text or (host or "Download Link"),
            target_url=elem.target_url,
            host=host,
            score=score,
            category=category,
            is_shortlink=is_shortlink,
            method=elem.method,
            form_data=elem.form_inputs if elem.form_inputs else None,
            filename_hint=filename_hint,
            size_hint=size_hint,
            is_multipart=is_multipart,
            multipart_base=multipart_base,
            multipart_part=multipart_part,
            multipart_format=multipart_format,
            file_kind=classify_file(filename=filename_hint or None, source_url=elem.target_url).category,
        )

    @classmethod
    def score_all(cls, elements: list[CleanedElement], source_url: str = "") -> list[MatrixElement]:
        # 1. Deduplicate exact duplicate elements (target_url, method, text)
        unique_elements: list[CleanedElement] = []
        seen_exact: set[tuple[str, str, str]] = set()
        for el in elements:
            key = (el.target_url.strip(), el.method.upper(), el.text.strip())
            if key in seen_exact:
                continue
            seen_exact.add(key)
            unique_elements.append(el)

        results = [cls.score_element(elem, source_url) for elem in unique_elements]

        # 2. Guarantee unique element IDs across all elements
        seen_ids: set[str] = set()
        for item in results:
            if item.id in seen_ids:
                suffix = 2
                while f"{item.id}-{suffix}" in seen_ids:
                    suffix += 1
                item.id = f"{item.id}-{suffix}"
            seen_ids.add(item.id)

        # 3. Multi-part grouping by host with strict false-positive prevention (len >= 2)
        host_base_counts: dict[tuple[str, str], list[MatrixElement]] = {}
        for elem in results:
            if elem.is_multipart and elem.multipart_base:
                key = (elem.host, elem.multipart_base)
                host_base_counts.setdefault(key, []).append(elem)

        for (host, base), items in host_base_counts.items():
            if len(items) >= 2:
                total_parts = max(len(items), max((it.multipart_part for it in items), default=len(items)))
                pkg_name = normalize_package_name(base)
                for it in items:
                    it.multipart_total = total_parts
                    it.package_name = pkg_name
                    # Multi-part archive packages get a priority boost
                    it.score = min(100, it.score + 15)
                    if it.score >= 70:
                        it.category = "high_utility"
            else:
                # Reset solitary items to non-multipart
                for it in items:
                    it.is_multipart = False
                    it.multipart_base = ""
                    it.multipart_part = 0
                    it.multipart_total = 0
                    it.multipart_format = ""
                    it.package_name = ""

        # Sort descending by score, high_utility first
        results.sort(key=lambda item: item.score, reverse=True)
        return results
