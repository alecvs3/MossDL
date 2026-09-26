# Contributing to MossDL

Thanks for helping. Bug reports, fixes and new site plugins are all welcome.

## Before you start

- For anything bigger than a small fix, open an issue first so we can agree on the approach.
- Site support requests use the [plugin request form](https://github.com/alecvs3/MossDL/issues/new?template=plugin_request.yml).
- Please don't include links to material you don't have the right to download, in issues, tests or fixtures.
  Use public test files (Linux ISOs, Blender open movies, the Internet Archive) or made-up `*.test` hosts.

## Working on the code

See [Building from source](README.md#building-from-source) to get the app running. The rules the codebase follows
are in [AGENTS.md](AGENTS.md); the short version:

- The engine is the source of truth for downloads. The UI shows what the engine reports and never simulates it.
- No silent failures: no empty `catch`/`except`, no arbitrary timeouts. When something is skipped, log why.
- Reuse existing helpers (formatting lives in `src/lib/format.ts`) instead of adding near-copies.
- Keep files focused, around 300 lines where practical.
- Provider data is a public contract: add fields, never rename or remove them.

## Before you open a pull request

```powershell
python scripts/check_all.py --profile fast
```

It must pass with zero errors: type checks, the UI build, and the Python, UI and Rust tests. CI runs the same gate.

## Writing a site plugin

Most sites need only a declarative `recipe.jsonc`. Start with

```powershell
python -m engine.cli plugin scaffold example-host --host example.com
python -m engine.cli plugin test example-host
```

and read the [plugin SDK](PLUGIN_SDK.md). Give the manifest a readable `display_name` and a `category`
(`cloud`, `file-host`, `video`, `images`, `audio`, `social` or `any-site`) so it shows properly in Settings → Plugins.

## Licence

By contributing you agree that your work is released under the project's licence, [GPL-3.0-or-later](LICENSE).
