from __future__ import annotations

import unittest
from pathlib import Path
from engine.db import TaskStore
from engine.models import DownloadTask
from engine.providers.hosted import GoogleDriveProvider
from engine.service import EngineService


class AccountsAndOAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path("test_scratch_accounts")
        self.temp_dir.mkdir(exist_ok=True)
        self.service = EngineService(self.temp_dir / "service_data")

    def tearDown(self) -> None:
        import shutil
        self.service.close()
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_account_create_list_delete_lifecycle(self) -> None:
        created = self.service.dispatch("account_create", {
            "provider_id": "google-drive",
            "label": "My Work Google Account",
            "account_type": "oauth",
            "secret_value": "ya29.mock_oauth_token_12345",
        })
        self.assertIsNotNone(created.get("id"))
        self.assertEqual(created["provider_id"], "google-drive")
        self.assertEqual(created["label"], "My Work Google Account")

        # List accounts
        accounts = self.service.dispatch("list_accounts", {"provider_id": "google-drive"})
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["id"], created["id"])

        # Delete account
        del_result = self.service.dispatch("account_delete", {"account_id": created["id"]})
        self.assertEqual(del_result["deleted"], created["id"])

        # Confirm deleted
        accounts_after = self.service.dispatch("list_accounts", {"provider_id": "google-drive"})
        self.assertEqual(len(accounts_after), 0)

    def test_google_oauth_get_auth_url(self) -> None:
        result = self.service.dispatch("google_oauth_get_auth_url", {})
        self.assertIn("auth_url", result)
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", result["auth_url"])
        self.assertIn("drive.readonly", result["auth_url"])

    def test_google_drive_provider_authenticated_resolution(self) -> None:
        sample_url = "https://drive.google.com/file/d/1ebK2Ja1CZq8dSyzO-bcBWRYsdWmf54gN/view"
        
        # 1. Unauthenticated resolution
        items_unauth = GoogleDriveProvider.resolve(sample_url)
        self.assertEqual(len(items_unauth), 1)
        self.assertIn("drive.usercontent.google.com", items_unauth[0].direct_url)
        self.assertEqual(items_unauth[0].headers, {})

        # 2. Authenticated resolution with OAuth token
        items_auth = GoogleDriveProvider.resolve(sample_url, secrets={"api_token": "test_token_abc"})
        self.assertEqual(len(items_auth), 1)
        self.assertIn("googleapis.com/drive/v3/files", items_auth[0].direct_url)
        self.assertEqual(items_auth[0].headers.get("Authorization"), "Bearer test_token_abc")


if __name__ == "__main__":
    unittest.main()
