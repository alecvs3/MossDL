from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.provider_compatibility import compatibility_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare local plugins with Cyberdrop-DL crawlers")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = compatibility_report(root / "plugins", root / "clone_reference" / "cyberdrop-dl")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print("Provider compatibility matrix")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    for provider in report["providers"]:
        if provider["cyberdrop_overlap"]:
            fixture = "fixture" if provider["reference_fixture"] else "synthetic"
            live = provider["live_url_env"]
            print(f"{provider['provider_id']}: {provider['cyberdrop_crawler']} ({fixture}; live via {live})")


if __name__ == "__main__":
    main()
