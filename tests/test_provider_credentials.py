import json
import tempfile
import time
import unittest

from engine.errors import ProviderMappedError
from engine.models import ResolutionContext, ResolvedItem
from engine.resolution import ResolutionBroker, public_item
from engine.secrets import InMemorySecretBackend, SecretManager


class ProviderCredentialTests(unittest.TestCase):
    def test_account_reference_refreshes_in_memory_and_never_persists_secret(self):
        backend = InMemorySecretBackend()
        secrets = SecretManager(backend)
        reference = secrets.put("refresh-token-secret", kind="account", name="account/test")
        broker = ResolutionBroker()
        calls = []

        def resolve(_source, credentials):
            calls.append(("resolve", dict(credentials)))
            if len(calls) == 1:
                raise ProviderMappedError("login required", "login_required")
            return [ResolvedItem("fixture", "https://files.example/item", "item.bin",
                                 direct_url="https://cdn.example/item?token=secret-token",
                                 headers={"Authorization": "Bearer refresh-token-secret"},
                                 cookies={"session": "refresh-token-secret"},
                                 expires_at=time.time() + 3600,
                                 metadata={"account_ref": "acct-1"})]

        refreshed = []
        items = broker.resolve_with_account(
            ResolutionContext("https://files.example/page", provider_id="fixture", account_ref="acct-1"),
            resolve,
            lambda _ref: {"credential": secrets.resolve_operation(reference)},
            refresh=lambda _source, credentials: refreshed.append(dict(credentials)) or True,
        )
        self.assertEqual(items[0].display_name, "item.bin")
        self.assertEqual(refreshed[0]["credential"], "refresh-token-secret")
        self.assertEqual(broker.current(ResolutionContext("https://files.example/page", item_id=None))[0].display_name,
                         "item.bin")
        self.assertNotIn("refresh-token-secret", json.dumps(public_item(items[0])))

    def test_expiry_refresh_preserves_item_identity(self):
        broker = ResolutionBroker()
        context = ResolutionContext("https://example.test/root", item_id="stable")
        old = ResolvedItem("fixture", context.source_url, "old", item_id="node-1", expires_at=1)
        broker._active[(context.source_url, context.item_id)] = [old]
        refreshed = ResolvedItem("fixture", context.source_url, "new", item_id="node-1", expires_at=9999999999)
        result = broker.refresh_if_needed(context, lambda _source, _credentials: [refreshed], now=2,
                                          credentials={"credential": "secret"},
                                          refresh=lambda _source, _credentials: [refreshed])
        self.assertEqual(result[0].item_id, "node-1")
        self.assertEqual(result[0].display_name, "new")


if __name__ == "__main__":
    unittest.main()
