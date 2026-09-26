"""Write THIRD_PARTY_NOTICES.md from the dependencies the app actually ships.

Direct dependencies only (Rust crates from src-tauri/Cargo.toml, UI packages from
package.json "dependencies", Python packages from requirements.txt), with the
licence each one declares. Run before a release: python scripts/third_party_notices.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rust() -> list[tuple[str, str, str]]:
    meta = json.loads(subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--manifest-path", str(ROOT / "src-tauri/Cargo.toml")],
        capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    by_id = {p["id"]: p for p in meta["packages"]}
    root_id = meta["workspace_members"][0]
    node = next(n for n in meta["resolve"]["nodes"] if n["id"] == root_id)
    direct = [d["pkg"] for d in node["deps"] if any(k.get("kind") is None for k in d["dep_kinds"])]
    rows = {(by_id[i]["name"], by_id[i]["version"], by_id[i].get("license") or "see crate") for i in direct}
    return sorted(rows)


def npm() -> list[tuple[str, str, str]]:
    deps = json.loads((ROOT / "package.json").read_text(encoding="utf-8")).get("dependencies", {})
    rows = []
    for name in deps:
        manifest = ROOT / "node_modules" / name / "package.json"
        info = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
        rows.append((name, info.get("version", "?"), info.get("license", "see package")))
    return sorted(rows)


def python() -> list[tuple[str, str, str]]:
    rows = []
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        name = re.split(r"[\s\[<>=#]", line.strip(), maxsplit=1)[0]
        if not name:
            continue
        try:
            dist = metadata.distribution(name)
            licence = dist.metadata.get("License-Expression") or dist.metadata.get("License") or ""
            if not licence or len(licence) > 40:
                classifiers = [c.split("::")[-1].strip() for c in dist.metadata.get_all("Classifier") or []
                               if c.startswith("License ::")]
                licence = ", ".join(classifiers) or "see package"
            rows.append((name, dist.version, licence))
        except metadata.PackageNotFoundError:
            print(f"[NOTICES_SKIPPED] {name} is not installed; its licence could not be read", file=sys.stderr)
            rows.append((name, "?", "see package"))
    return sorted(rows)


def table(rows: list[tuple[str, str, str]]) -> str:
    return "| Name | Version | Licence |\n|---|---|---|\n" + "\n".join(f"| {n} | {v} | {l} |" for n, v, l in rows)


def main() -> int:
    text = f"""# Third-party notices

MossDL is licensed under GPL-3.0-or-later. It ships with the direct dependencies
below, each under its own licence (their dependencies are listed in `Cargo.lock`
and `package-lock.json`).

Not shipped: RAR archives are extracted by the user's own 7-Zip if installed; the
solver browser (Clearcote) is downloaded on request from its own releases; ad-block
filter lists are downloaded from their publishers and keep their own licences.

## Rust (transfer core, archive worker, desktop shell)

{table(rust())}

## Interface

{table(npm())}

## Python engine

{table(python())}
"""
    (ROOT / "THIRD_PARTY_NOTICES.md").write_text(text, encoding="utf-8")
    print("wrote THIRD_PARTY_NOTICES.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
