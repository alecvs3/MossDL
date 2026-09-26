from engine.errors import NeedsUser
from engine.models import ResolvedItem
from engine.shortlinks import catalog_hosts, extract_static_targets, fetch_and_extract, is_catalogued, load_catalog

_catalog = load_catalog()
_hosts = catalog_hosts(_catalog)


import urllib.parse

_DIRECT_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2", ".iso", ".bin", ".exe", ".dmg",
    ".mp4", ".mkv", ".mp3", ".flac", ".apk", ".msi", ".001", ".002", ".003", ".004", ".005",
    ".r00", ".r01", ".z01", ".z02"
}


def extract_links(params):
    url = params["url"]
    clean_path = urllib.parse.urlsplit(url).path.lower().split("?", 1)[0].split("#")[0]
    if any(clean_path.endswith(ext) for ext in _DIRECT_EXTENSIONS):
        return []
    if not is_catalogued(url, _catalog):
        return []

    targets = extract_static_targets(url)
    if not targets:
        targets = fetch_and_extract(url)
    if not targets:
        raise NeedsUser(
            "This shortlink requires browser interaction, a timer, login, or CAPTCHA",
            "browser_handoff",
            {"url": url, "host": next((host for host in _hosts if host in url.lower()), "unknown"),
             "reason": "page_interaction_required"},
        )
    return [ResolvedItem("shortlink", url, "resolved-link", direct_url=target,
                         metadata={"shortlink": True, "definition_status": "inventory"}).to_dict()
            for target in targets]
