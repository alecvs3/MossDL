# Transfer Manager plugin SDK

Providers are isolated Python processes. The engine starts a provider with:

```text
python -m engine.plugin_worker --provider <provider-id>
```

Communication is line-delimited JSON-RPC 2.0 over stdin/stdout. A schema-v1
Python plugin must implement `manifest`, `match`, `resolve`, and optionally
`refresh`. Schema-v2 recipe plugins keep `hooks.py` as a placeholder but put
ordinary HTTP/API behavior in `recipe.jsonc`.

`resolve` returns a list of `ResolvedItem` objects. Plugins may request network
access and read secret references supplied by the engine, but they never own
the final destination path. They must not log signed URLs, passwords, cookies,
or account tokens.

The first-party providers are currently registered in `engine/providers/`:
Transfer.it, MEGA, Gofile, and generic HTTP/FTP.

To add a common provider:

```text
python -m engine.cli plugin scaffold example-host --host example.com
```

Edit the generated manifest and recipe, add deterministic fixtures, then run
`python -m engine.cli plugin test example-host`. Recipes support safe REST/HTML
extraction, headers, cookies, secret references, pagination, refresh, filename
fallback, and mapped provider errors. They reject arbitrary code, filesystem
access, subprocesses, unbounded redirects/items, unsafe paths, and hosts outside
permissions. Keep Transfer.it/MEGA-style cryptographic protocols in explicit
Python providers.
