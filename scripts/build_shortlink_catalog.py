from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FASTFORWARD = ROOT / "clone_reference" / "fastforward" / "src" / "bypasses"
FASTFORWARD_RULES = ROOT / "clone_reference" / "fastforward" / "src" / "js" / "rules.json"
BASD = ROOT / "clone_reference" / "bypass-all-shortlinks-debloated" / "supported_sites.txt"
OUTPUT = ROOT / "plugins" / "shortlink" / "catalog.json"
HOST_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?![A-Za-z0-9_-])")


def normalize(host: str) -> str:
    return host.lower().removeprefix("www.").strip("./")


def main() -> None:
    entries: dict[str, dict] = {}
    if FASTFORWARD.is_dir():
        for path in FASTFORWARD.glob("*.js"):
            if path.name == "BypassDefinition.js":
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            matches = re.search(r"export const matches\s*=\s*\[(.*?)\]", text, re.S)
            if not matches:
                continue
            for value in re.findall(r"['\"]([^'\"]+)['\"]", matches.group(1)):
                host = normalize(value.split("/", 3)[2] if "://" in value else value)
                if "." not in host:
                    continue
                entries.setdefault(host, {"host": host, "sources": [], "definitions": [], "status": "inventory"})
                entries[host]["sources"].append("fastforward")
                entries[host]["definitions"].append(path.stem)
    if BASD.is_file():
        for line in BASD.read_text(encoding="utf-8", errors="replace").splitlines():
            for host in {normalize(value) for value in HOST_RE.findall(line)}:
                if host in {"example.com", "google.com", "youtube.com"}:
                    continue
                entries.setdefault(host, {"host": host, "sources": [], "definitions": [], "status": "inventory"})
                if "basd" not in entries[host]["sources"]:
                    entries[host]["sources"].append("basd")
    if FASTFORWARD_RULES.is_file():
        rules = json.loads(FASTFORWARD_RULES.read_text(encoding="utf-8", errors="replace"))
        def visit(value):
            if isinstance(value, dict):
                for child in value.values():
                    yield from visit(child)
            elif isinstance(value, list):
                for child in value:
                    yield from visit(child)
            elif isinstance(value, str) and value.startswith("*://"):
                yield value
        for pattern in visit(rules):
            host = normalize(pattern.split("//", 1)[1].split("/", 1)[0].removeprefix("*."))
            if "." not in host:
                continue
            entries.setdefault(host, {"host": host, "sources": [], "definitions": [], "status": "inventory"})
            if "fastforward-rules" not in entries[host]["sources"]:
                entries[host]["sources"].append("fastforward-rules")
    for host in ("network-loop.com",):
        entries.setdefault(host, {"host": host, "sources": ["built-in-safe-pattern"], "definitions": [], "status": "inventory"})
    for value in entries.values():
        value["sources"] = sorted(set(value["sources"]))
        value["definitions"] = sorted(set(value["definitions"]))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"version": 1, "entries": sorted(entries.values(), key=lambda item: item["host"])}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(entries)} shortlink hosts to {OUTPUT}")


if __name__ == "__main__":
    main()
