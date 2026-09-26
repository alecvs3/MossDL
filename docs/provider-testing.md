# Provider compatibility testing

The provider matrix is generated from the installed manifests and the local
Cyberdrop-DL reference checkout. It reports overlap, reference test-case
coverage, and the local operations exposed by each plugin. A crawler overlap
is a comparison signal; it is not a claim that either implementation is
production-complete.

Run the offline matrix and contract tests with:

```text
python scripts/provider_compatibility_report.py
python -m unittest -v test_provider_compatibility.py
```

The contract suite checks every local manifest, every overlap, and a synthetic
local-domain routing fixture. When Cyberdrop-DL has a literal test-case URL,
the report records it as reference evidence. Routing intentionally uses a
local synthetic URL because reference fixtures can use domains that are not
declared by our plugin (for example, an alternate Cyberdrop or Pixeldrain
domain).

Live checks are opt-in and provider-specific. Set `TRANSFER_MANAGER_LIVE=1`
and provide one supported URL per provider, for example:

```text
TRANSFER_MANAGER_LIVE_URL_MEGA=https://mega.nz/folder/<public-folder>#<key>
TRANSFER_MANAGER_LIVE_URL_MEDIAFIRE=https://www.mediafire.com/file/<id>/<name>/file
python -m unittest -v test_live_provider_matrix.py
```

Missing live URLs are skipped, keeping CI deterministic and offline-capable.
The test enumerates each configured URL, verifies routing, and requires at
least one non-empty resolved item. Failures are reported per provider so a
single broken site cannot be mistaken for ecosystem-wide health.

