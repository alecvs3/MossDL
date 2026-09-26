# Modular provider SDK

Built-in providers live under `plugins/<id>/` and consist of a validated
`manifest.jsonc` plus a small `hooks.py` module. The hooks run in a persistent,
line-delimited JSON-RPC subprocess; they never receive UI handles or write to
the task database.

Manifests use schema version 1 or additive schema version 2 and declare URL matching, capabilities, limits,
roles, permissions, and hook names. The additive SDK surface supports resolver,
decrypter, downloader, account, postprocess, media, and notifier roles plus
match, metadata, enumerate, resolve, refresh, authenticate, health, and event
hooks. The loader strips comments and trailing commas with a safe scanner,
rejects duplicate/unknown fields and invalid regular expressions, and does not
execute manifest content. The engine owns filename safety, path validation,
admission limits, retry classification, checksums, and task state.

External plugin roots are opt-in through `TRANSFER_PLUGIN_DIRS`. Every plugin
must pass the same manifest validation and worker timeout/output/crash checks.
Credentials are references to keychain entries; plaintext credentials are
operation-scoped and are never written to logs or event payloads.

Plugins that fail three isolated calls are quarantined in SQLite and excluded
from routing until explicitly re-enabled. `NeedsUser` is a structured result
for passwords, login, CAPTCHA, browser handoff, or file selection; automated
CAPTCHA solving is intentionally not part of this SDK. Signed direct URLs are
temporary resolution data and are not persisted as task identity.

The current built-ins are `transfer.it`, `gofile`, `mega`, generic HTTP/FTP,
ordinary HTML link extraction, and non-encrypted HLS/DASH manifest inspection.
Their hooks delegate to the existing provider implementations so the migration
preserves current behavior while making future providers independent of the
desktop UI. Media manifests are parsed into segment plans before any future
engine-owned assembly step; encrypted or DRM workflows are intentionally
rejected pending review.

## Schema v2 recipe providers

Common REST and HTML sites can be added without engine code or a custom Python
hook. A schema-v2 manifest sets `"implementation": "recipe"` and points to a
`recipe.jsonc` file. Recipes provide bounded HTTP requests, JSONPath/CSS/header
extraction, cookies and secret references, pagination, refresh rules, filename
fallbacks, and standardized error categories. They cannot execute Python or
JavaScript, access the filesystem, spawn processes, or contact hosts outside the
manifest allowlist. Enumeration never returns signed URLs; selected files are
resolved later from the durable source URL and opaque item ID.

Generate a built-in provider skeleton with:

```text
python -m engine.cli plugin scaffold example-host --host example.com
```

Use `--implementation python` only for cryptography, complex authentication,
browser-assisted flows, unusual protocols, or provider-specific transforms.
Run the shared contract suite with `python -m engine.cli plugin test`; it checks
every built-in manifest, matcher, operation declaration, and worker boundary.

The backend `inspect_url` operation derives provider capabilities from the
validated manifest and registered operations. The UI therefore opens the same
tree picker for any provider declaring both `folders` and `enumerate`, including
future recipe providers.

## Cyberdrop-DL compatibility import

The repository includes a deterministic AST-based importer for comparing local
providers with `clone_reference/cyberdrop-dl`. It extracts crawler metadata and
sanitized fixture expectations without importing or executing Cyberdrop-DL
code. Generated adapters remain ordinary isolated plugins and use the
transfer-manager provider runtime.

Run the migration in stages:

```text
python scripts/import_cyberdrop_plugins.py inventory
python scripts/import_cyberdrop_plugins.py generate --staging .cyberdrop-generated
python scripts/import_cyberdrop_plugins.py test --staging .cyberdrop-generated
python scripts/import_cyberdrop_plugins.py promote --staging .cyberdrop-generated --only-passed
python scripts/import_cyberdrop_plugins.py check
```

`check` runs the deterministic offline health matrix across every active
plugin, including routing, isolated worker startup, contract validation,
generated fixture coverage, and available HAR fixture parsing. It writes
`.cyberdrop-generated/provider-check-report.json`. Actual provider requests
are opt-in and require a user-authorized sample URL per provider, supplied
through `TRANSFER_MANAGER_LIVE_URL_<PROVIDER_ID>`:

```text
$env:TRANSFER_MANAGER_LIVE_URL_MEDIAFIRE = 'https://...'
python scripts/import_cyberdrop_plugins.py check --live
python scripts/import_cyberdrop_plugins.py check --live --download --max-bytes 10485760
```

Live reports retain only status, counts, and redacted error categories; they
do not persist URLs, cookies, authorization headers, signed URLs, or tokens.

Promotion keeps replaced plugins in the staging backup directory and defaults
to pass-only. Use `--all` only for local development. Vikingfile is reported as
a local-only provider because the reference checkout has no Vikingfile crawler.

## Explicit alternates and routes

`add_task` accepts `alternate_urls`/`alternatives` as objects containing a URL
and optional `provider_id`. These are persisted with the task. The fallback
ledger is keyed by task, source fingerprint, provider, and route, so an
attempted candidate is excluded until the explicit candidate set is exhausted.

Route profiles are provider-neutral. `direct`, `http_proxy`, `socks5`,
`docker_socks5`, and `system_vpn` are recognized; the first adapter for the
ExpressVPN Docker reference is a user-owned SOCKS5 endpoint such as
`socks5://127.0.0.1:1080`. The app does not create, privilege, or control the
Docker container. Automatic route switching is permitted only for connection,
DNS, timeout, and reconnect failures—not quota, 403/429 policy, account,
CAPTCHA, or authentication responses.
