"""Deterministic secret-hygiene scanner for durable artifacts, logs, fixtures, and events.

Provides a fail-closed security gate for SEC-01 without echoing secrets in outputs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import parse_qsl, urlsplit

SCHEMA_VERSION = 1

# Opaque reference patterns and allowed placeholder tokens
SAFE_REF_SCHEMES = ("keychain://", "secret://")
SAFE_PLACEHOLDERS = ("[redacted]", "<url-redacted>", "redacted", "")

# Secret key names / sensitive field patterns
COOKIE_KEY_PATTERN = re.compile(r"(?i)^(?:set-)?cookie[s]?$")
AUTH_KEY_PATTERN = re.compile(r"(?i)^(?:authorization|proxy-authorization|bearer)$")
PASSWORD_KEY_PATTERN = re.compile(r"(?i)^.*(?:password|passwd).*$")
ACCOUNT_KEY_PATTERN = re.compile(r"(?i)^.*(?:account_key|account_secret|api_key|apikey).*$")
HELPER_KEY_PATTERN = re.compile(r"(?i)^.*(?:helper_secret|helper_env).*$")
SENSITIVE_HEADER_PATTERN = re.compile(r"(?i)^(?:authorization|cookie|set-cookie|x-.*secret.*|x-.*api-key.*|x-.*token.*)$")

# Query parameter secret keys
SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "auth",
    "signature",
    "sig",
    "password",
    "secret",
    "cookie",
    "expires",
    "x-amz-signature",
    "x-amz-credential",
    "x-amz-security-token",
}

# Value patterns for bearer tokens or basic auth strings
BEARER_VAL_PATTERN = re.compile(r"(?i)^Bearer\s+[A-Za-z0-9._~+/-]+=*\s*$")
BASIC_VAL_PATTERN = re.compile(r"(?i)^Basic\s+[A-Za-z0-9+/=]+\s*$")


@dataclass(frozen=True, slots=True)
class Finding:
    """Schema-versioned security finding. Never includes raw secret string."""

    artifact_path: str
    field_path: str
    matched_class: str

    def to_dict(self) -> dict[str, str]:
        return {
            "artifact_path": self.artifact_path,
            "field_path": self.field_path,
            "matched_class": self.matched_class,
        }


def is_safe_value(val: Any) -> bool:
    if val is None or isinstance(val, (int, float, bool)):
        return True
    if not isinstance(val, str):
        return False
    trimmed = val.strip()
    if not trimmed:
        return True
    if trimmed.lower() in SAFE_PLACEHOLDERS:
        return True
    if trimmed.startswith(SAFE_REF_SCHEMES):
        return True
    if trimmed.startswith("session_ref:") or trimmed.startswith("credential_ref:") or trimmed.startswith("account_ref:"):
        return True
    return False


def inspect_url(url_str: str, artifact_path: str, field_path: str) -> List[Finding]:
    findings: List[Finding] = []
    try:
        parsed = urlsplit(url_str)
    except Exception:
        return findings

    # Check query params for signed URL or credential material
    if parsed.query:
        for key, val in parse_qsl(parsed.query, keep_blank_values=True):
            k_lower = key.lower()
            if k_lower in SENSITIVE_QUERY_KEYS or any(marker in k_lower for marker in ("token", "signature", "secret", "sig")):
                if not is_safe_value(val):
                    findings.append(
                        Finding(
                            artifact_path=artifact_path,
                            field_path=f"{field_path}?{key}",
                            matched_class="signed_url_query",
                        )
                    )
    return findings


def scan_value(val: Any, artifact_path: str, current_path: str) -> List[Finding]:
    findings: List[Finding] = []

    if isinstance(val, dict):
        for k, v in val.items():
            k_str = str(k)
            child_path = f"{current_path}.{k_str}" if current_path else k_str
            k_lower = k_str.lower()

            # Classify based on key semantics
            if COOKIE_KEY_PATTERN.match(k_str):
                if not is_safe_value(v):
                    findings.append(Finding(artifact_path, child_path, "cookie"))
            elif AUTH_KEY_PATTERN.match(k_str):
                if not is_safe_value(v):
                    findings.append(Finding(artifact_path, child_path, "authorization"))
            elif PASSWORD_KEY_PATTERN.match(k_str):
                if not is_safe_value(v):
                    findings.append(Finding(artifact_path, child_path, "password"))
            elif ACCOUNT_KEY_PATTERN.match(k_str):
                if not is_safe_value(v):
                    findings.append(Finding(artifact_path, child_path, "account_secret"))
            elif HELPER_KEY_PATTERN.match(k_str):
                if not is_safe_value(v):
                    findings.append(Finding(artifact_path, child_path, "helper_secret"))
            elif SENSITIVE_HEADER_PATTERN.match(k_str):
                if not is_safe_value(v):
                    findings.append(Finding(artifact_path, child_path, "sensitive_header"))

            # Recurse down
            findings.extend(scan_value(v, artifact_path, child_path))

    elif isinstance(val, list):
        for idx, item in enumerate(val):
            child_path = f"{current_path}[{idx}]"
            findings.extend(scan_value(item, artifact_path, child_path))

    elif isinstance(val, str):
        trimmed = val.strip()
        if not is_safe_value(trimmed):
            if BEARER_VAL_PATTERN.match(trimmed) or BASIC_VAL_PATTERN.match(trimmed):
                findings.append(Finding(artifact_path, current_path, "authorization"))
            elif "=" in trimmed and (";" in trimmed or "session" in trimmed.lower()) and ("cookie" in current_path.lower() or "cookie" in trimmed.lower()):
                findings.append(Finding(artifact_path, current_path, "cookie"))
            elif ":" in trimmed and any(trimmed.lower().startswith(h) for h in ("x-secret", "authorization:", "cookie:", "x-api-key")):
                findings.append(Finding(artifact_path, current_path, "sensitive_header"))

            # Check if this string is a URL containing sensitive queries
            if (trimmed.startswith("http://") or trimmed.startswith("https://")) and "?" in trimmed:
                findings.extend(inspect_url(trimmed, artifact_path, current_path))

    return findings


def scan_file(path: Path) -> List[Finding]:
    findings: List[Finding] = []
    norm_path = path.as_posix()

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        # Bounded read error is treated as a finding (fail-closed)
        return [Finding(norm_path, "root", f"unreadable_file:{type(exc).__name__}")]

    if not content.strip():
        return [Finding(norm_path, "root", "empty_artifact")]

    # Check if JSONL vs JSON
    if norm_path.endswith(".jsonl"):
        for line_num, line in enumerate(content.splitlines(), start=1):
            line_str = line.strip()
            if not line_str:
                continue
            try:
                data = json.loads(line_str)
                findings.extend(scan_value(data, norm_path, f"line[{line_num}]"))
            except Exception:
                findings.append(Finding(norm_path, f"line[{line_num}]", "malformed_jsonl"))
    else:
        try:
            data = json.loads(content)
            findings.extend(scan_value(data, norm_path, ""))
        except Exception:
            findings.append(Finding(norm_path, "root", "malformed_json"))

    unique: dict[tuple[str, str, str], Finding] = {}
    for f in findings:
        unique[(f.artifact_path, f.field_path, f.matched_class)] = f
    return sorted(unique.values(), key=lambda f: (f.artifact_path, f.field_path, f.matched_class))


def scan_root(root_path: Path) -> List[Finding]:
    if not root_path.exists():
        return [Finding(root_path.as_posix(), "root", "missing_root")]

    findings: List[Finding] = []
    if root_path.is_file():
        findings.extend(scan_file(root_path))
    else:
        # Recursive traversal over json and jsonl files
        found_any = False
        for current_dir, _, files in os.walk(root_path):
            for file in sorted(files):
                if file.endswith(".json") or file.endswith(".jsonl"):
                    found_any = True
                    file_path = Path(current_dir) / file
                    findings.extend(scan_file(file_path))
        if not found_any:
            findings.append(Finding(root_path.as_posix(), "root", "empty_directory"))

    # Return deterministically sorted, deduplicated findings
    unique: dict[tuple[str, str, str], Finding] = {}
    for f in findings:
        unique[(f.artifact_path, f.field_path, f.matched_class)] = f
    return sorted(unique.values(), key=lambda f: (f.artifact_path, f.field_path, f.matched_class))


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic security scanner for serialized artifacts.")
    parser.add_argument("--root", required=True, help="Path to file or directory to scan.")
    parser.add_argument("--format", choices=["json", "text"], default="json", help="Output format.")
    args = parser.parse_args()

    target = Path(args.root)
    findings = scan_root(target)

    report = {
        "schema_version": SCHEMA_VERSION,
        "root": target.as_posix(),
        "total_findings": len(findings),
        "status": "passed" if len(findings) == 0 else "failed",
        "findings": [f.to_dict() for f in findings],
    }

    if args.format == "json":
        print(json.dumps(report, indent=2))
    else:
        print(f"Security Scan Report: {report['status'].upper()}")
        print(f"Root: {report['root']}")
        print(f"Findings: {report['total_findings']}")
        for f in findings:
            print(f"  - [{f.matched_class}] {f.artifact_path} -> {f.field_path}")

    return 0 if len(findings) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())