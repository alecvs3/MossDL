import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.build_browser_extension import (
    BROWSERS,
    HOST_NAME,
    build_extension,
    install_native_host,
    registration_status,
    uninstall_native_host,
)


class BrowserInstallTests(unittest.TestCase):
    def _run_cli(self, *arguments):
        return subprocess.run([sys.executable, "-m", "engine.cli", *arguments], capture_output=True, text=True)

    def test_install_is_deterministic_idempotent_and_scoped(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output = root / "extension-output"
            registration = root / "registration"
            fake_host = root / "transfer-engine.exe"
            fake_host.write_text("native host fixture", encoding="utf-8")
            first = install_native_host(output, host_path=fake_host, registration_root=registration)
            first_bytes = {browser: (registration / browser / f"{HOST_NAME}.json").read_bytes() for browser in BROWSERS}
            second = install_native_host(output, host_path=fake_host, registration_root=registration)
            self.assertEqual(first["files"], second["files"])
            self.assertTrue(all((registration / browser / f"{HOST_NAME}.json").is_file() for browser in BROWSERS))
            self.assertEqual(first_bytes, {browser: (registration / browser / f"{HOST_NAME}.json").read_bytes() for browser in BROWSERS})
            self.assertTrue(registration_status(registration)["installed"])
            self.assertTrue((output / "chrome" / "manifest.json").is_file())

            removed = uninstall_native_host(registration)
            self.assertFalse(removed["installed"])
            self.assertFalse(registration_status(registration)["installed"])
            self.assertTrue((output / "chrome" / "manifest.json").is_file(), "uninstall must not delete extension output")
            self.assertFalse(any(registration.glob("**/*.json")))

    def test_generated_host_paths_are_absolute_and_browser_admission_is_explicit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_host = root / "host"
            fake_host.write_text("host", encoding="utf-8")
            result = build_extension(root / "out", host_path=fake_host, check=True)
            self.assertEqual(set(result["browsers"]), set(BROWSERS))
            for browser in BROWSERS:
                manifest = json.loads((root / "out" / browser / "manifest.json").read_text(encoding="utf-8"))
                host_manifest = json.loads((root / "out" / browser / f"{HOST_NAME}.json").read_text(encoding="utf-8"))
                browser_config = json.loads((root / "out" / browser / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["manifest_version"], 3)
                self.assertEqual(browser_config["protocol_version"], "browser-capture/1")
                self.assertEqual(browser_config["passive_capture"], False)
                if browser == "firefox":
                    self.assertIn("allowed_extensions", host_manifest)
                else:
                    self.assertIn("allowed_origins", host_manifest)

    def test_engine_cli_and_entrypoint_expose_lifecycle_without_creating_a_browser_queue(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data_dir = root / "data"
            output = root / "extension"
            registration = root / "registration"
            fake_host = root / "host"
            fake_host.write_text("host", encoding="utf-8")
            common = ["--data-dir", str(data_dir)]
            installed = self._run_cli(*common, "browser-install", "--output", str(output), "--host-path", str(fake_host),
                                      "--registration-root", str(registration))
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertTrue(json.loads(installed.stdout)["installed"])
            status = self._run_cli(*common, "browser-status", "--registration-root", str(registration))
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertTrue(json.loads(status.stdout)["installed"])
            entrypoint = subprocess.run([sys.executable, "engine_entrypoint.py", "browser-status",
                                         "--registration-root", str(registration)], capture_output=True, text=True)
            self.assertEqual(entrypoint.returncode, 0, entrypoint.stderr)
            self.assertTrue(json.loads(entrypoint.stdout)["installed"])
            uninstalled = self._run_cli(*common, "browser-uninstall", "--registration-root", str(registration))
            self.assertEqual(uninstalled.returncode, 0, uninstalled.stderr)
            self.assertFalse(json.loads(uninstalled.stdout)["installed"])
            self.assertTrue((output / "chrome" / "manifest.json").is_file())

    def test_native_host_origins_do_not_contain_unsupported_wildcards(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            host = root / "host.exe"
            host.write_text("test host", encoding="utf-8")
            result = install_native_host(root / "out", host_path=host, registration_root=root / "registration",
                                         extension_id="abcdefghijklmnopabcdefghijklmnop")
            for browser in ("chrome", "edge"):
                manifest = json.loads(Path(result["files"][browser]).read_text())
                self.assertTrue(all("*" not in origin for origin in manifest["allowed_origins"]))
                self.assertIn("chrome-extension://abcdefghijklmnopabcdefghijklmnop/", manifest["allowed_origins"])


if __name__ == "__main__":
    unittest.main()
