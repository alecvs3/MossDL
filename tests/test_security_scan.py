import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.security_scan import Finding, scan_file, scan_root, scan_value


class SecurityScanTests(unittest.TestCase):
    def setUp(self):
        self.fixtures_dir = Path(__file__).parent / "security" / "fixtures"
        self.bearing_path = self.fixtures_dir / "secret-bearing-inputs.json"
        self.free_path = self.fixtures_dir / "secret-free-inputs.json"

    def test_fixtures_exist(self):
        self.assertTrue(self.bearing_path.exists(), "secret-bearing fixture must exist")
        self.assertTrue(self.free_path.exists(), "secret-free fixture must exist")

    def test_secret_free_corpus_passes_with_zero_findings(self):
        findings = scan_root(self.free_path)
        self.assertEqual(len(findings), 0, f"Expected 0 findings in safe fixture, got: {findings}")

    def test_secret_bearing_corpus_detects_all_required_classes(self):
        findings = scan_root(self.bearing_path)
        self.assertGreater(len(findings), 0, "Expected findings in secret-bearing fixture")

        classes_found = {f.matched_class for f in findings}
        required_classes = {
            "cookie",
            "authorization",
            "password",
            "account_secret",
            "helper_secret",
            "signed_url_query",
            "sensitive_header",
        }
        for req in required_classes:
            self.assertIn(req, classes_found, f"Missing expected secret class: {req}")

    def test_no_secret_string_echoed_in_findings(self):
        bearing_content = self.bearing_path.read_text(encoding="utf-8")
        findings = scan_root(self.bearing_path)

        sentinel_secrets = [
            "sentinel-secret-auth-token-12345",
            "sentinel-secret-cookie-val-67890",
            "sentinel-secret-password-xyz",
            "sentinel-secret-account-key-abc",
            "sentinel-secret-sig-999",
            "sentinel-secret-url-token-888",
            "c2VudGluZWw6c2VudGluZWw=",
            "sentinel-secret-query-sig",
            "sentinel-secret-provider-key",
            "sentinel-secret-helper-env-val",
            "sentinel-secret-header-value",
            "sentinel-secret-helper-token",
        ]
        # Verify sentinels are indeed in the source file
        for secret in sentinel_secrets:
            self.assertIn(secret, bearing_content)

        # Verify NO sentinel appears in any finding's attributes or string representations
        for finding in findings:
            finding_repr = repr(finding)
            finding_dict = json.dumps(finding.to_dict())
            for secret in sentinel_secrets:
                self.assertNotIn(secret, finding.artifact_path)
                self.assertNotIn(secret, finding.field_path)
                self.assertNotIn(secret, finding.matched_class)
                self.assertNotIn(secret, finding_repr)
                self.assertNotIn(secret, finding_dict)

    def test_safe_opaque_references_and_placeholders_pass(self):
        safe_data = {
            "token_handle": "keychain://account/token",
            "secret_handle": "secret://vault/key",
            "session_id": "session_ref:session-abc-123",
            "cred_id": "credential_ref:cred-456",
            "acct_id": "account_ref:acct-789",
            "redacted_1": "[redacted]",
            "redacted_2": "<url-redacted>",
            "redacted_3": "redacted",
            "empty": "",
            "number": 42,
            "flag": True,
            "url_clean": "https://example.com/download/file.tar.gz?part=1&id=abc",
        }
        findings = scan_value(safe_data, "memory", "")
        self.assertEqual(len(findings), 0, f"Expected no findings for safe values, got {findings}")

    def test_jsonl_support_and_line_indexed_findings(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({"status": "ok", "url": "https://example.com/test"}) + "\n")
            f.write(json.dumps({"status": "error", "headers": {"Authorization": "Bearer leaked-token"}}) + "\n")
            temp_path = Path(f.name)

        try:
            findings = scan_file(temp_path)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].matched_class, "authorization")
            self.assertTrue(findings[0].field_path.startswith("line[2]"))
        finally:
            temp_path.unlink(missing_ok=True)

    def test_empty_or_malformed_artifact_fails_closed(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
            f.write("")  # empty
            empty_path = Path(f.name)

        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as f:
            f.write("{invalid json...")
            malformed_path = Path(f.name)

        missing_path = Path("tests/security/fixtures/nonexistent-fixture-12345.json")

        try:
            findings_empty = scan_file(empty_path)
            self.assertEqual(len(findings_empty), 1)
            self.assertEqual(findings_empty[0].matched_class, "empty_artifact")

            findings_malformed = scan_file(malformed_path)
            self.assertEqual(len(findings_malformed), 1)
            self.assertEqual(findings_malformed[0].matched_class, "malformed_json")

            findings_missing = scan_root(missing_path)
            self.assertEqual(len(findings_missing), 1)
            self.assertEqual(findings_missing[0].matched_class, "missing_root")
        finally:
            empty_path.unlink(missing_ok=True)
            malformed_path.unlink(missing_ok=True)

    def test_cli_exit_codes(self):
        scanner_script = Path(__file__).parents[1] / "scripts" / "security_scan.py"

        # Safe corpus must exit 0
        cmd_safe = [sys.executable, str(scanner_script), "--root", str(self.free_path), "--format", "json"]
        proc_safe = subprocess.run(cmd_safe, capture_output=True, text=True)
        self.assertEqual(proc_safe.returncode, 0, f"Expected exit 0 for safe corpus, got: {proc_safe.stderr}")
        report_safe = json.loads(proc_safe.stdout)
        self.assertEqual(report_safe["status"], "passed")
        self.assertEqual(report_safe["total_findings"], 0)

        # Secret-bearing corpus must exit 1
        cmd_bearing = [sys.executable, str(scanner_script), "--root", str(self.bearing_path), "--format", "json"]
        proc_bearing = subprocess.run(cmd_bearing, capture_output=True, text=True)
        self.assertEqual(proc_bearing.returncode, 1, "Expected exit 1 for secret-bearing corpus")
        report_bearing = json.loads(proc_bearing.stdout)
        self.assertEqual(report_bearing["status"], "failed")
        self.assertGreater(report_bearing["total_findings"], 0)


if __name__ == "__main__":
    unittest.main()