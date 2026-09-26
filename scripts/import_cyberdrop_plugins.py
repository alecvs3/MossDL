from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.cyberdrop_import import build_specs, check_active_plugins, generate_staging, promote_staging, test_staging


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Analyze and stage Cyberdrop-DL-compatible provider adapters")
    parser.add_argument("command", choices=("inventory", "generate", "test", "check", "promote"))
    parser.add_argument("--plugin-root", default=str(root / "plugins"))
    parser.add_argument("--cyberdrop-root", default=str(root / "clone_reference" / "cyberdrop-dl"))
    parser.add_argument("--staging", default=str(root / ".cyberdrop-generated"))
    parser.add_argument("--backup-root")
    parser.add_argument("--only-passed", action="store_true", default=True,
                        help="promote only providers with a passing staged contract (default)")
    parser.add_argument("--all", dest="only_passed", action="store_false",
                        help="allow promotion of failed/manual providers; use only for development")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--live", action="store_true", help="run configured live URLs; offline by default")
    parser.add_argument("--download", action="store_true", help="read up to --max-bytes from one live result")
    parser.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    args = parser.parse_args()
    plugin_root = Path(args.plugin_root)
    reference_root = Path(args.cyberdrop_root)
    staging_root = Path(args.staging)
    if args.command == "inventory":
        specs = [spec.to_dict() for spec in build_specs(plugin_root, reference_root)]
        output = {"summary": {"providers": len(specs), "fixture_cases": sum(len(item["fixture_cases"]) for item in specs)},
                  "providers": specs}
    elif args.command == "generate":
        output = generate_staging(plugin_root, reference_root, staging_root)
    elif args.command == "test":
        output = test_staging(staging_root)
    elif args.command == "check":
        output = check_active_plugins(plugin_root, reference_root, live=args.live,
                                      download=args.download, max_bytes=args.max_bytes)
        staging_root.mkdir(parents=True, exist_ok=True)
        (staging_root / "provider-check-report.json").write_text(
            json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    else:
        output = promote_staging(plugin_root, staging_root, only_passed=args.only_passed,
                                  backup_root=args.backup_root)
    if args.json or args.command != "inventory":
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        print(f"Cyberdrop-DL overlap inventory: {output['summary']['providers']} providers")
        for provider in output["providers"]:
            print(f"{provider['provider_id']}: {provider['crawler']} ({len(provider['fixture_cases'])} fixture cases)")
    return 0 if output.get("summary", {}).get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
