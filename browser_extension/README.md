# MossDL Capture

Chrome and Firefox companion for the MossDL Windows application.

## Reproduce a build (reviewers)

Requirements: Node 24 with npm. No Python, desktop app, accounts, or private keys are needed to build the extension.

```text
npm ci
npm run check
npm test
npm run build
npm run lint:firefox
```

`build/chrome` and `build/firefox` are the complete extension folders. The Firefox reviewer source ZIP contains this folder's source, pinned dependencies, manifests and existing PNG icons. esbuild bundles the local TypeScript files without remote code or minification. No generated background image or mock product state is used.

For the optional real-browser smoke test: `npx playwright install chromium` then `npx playwright test`. It creates and removes an isolated browser profile, never modifies the normal user profile, and tests without a registered native host.

## Store output

After building and testing, run `python scripts/package_store.py` from this folder. `release/` contains the two upload ZIPs, Firefox reviewer sources, listing assets/text, hashes, and START-HERE instructions. This packaging script uses only Python's standard library.

The store files exclude tests, native-host manifests, development dependencies, and local paths. Native-host installation belongs to the desktop app. Chrome's store-assigned ID must be added to the desktop native-host allowlist after the first draft upload. Firefox uses the stable ID `capture@mossdl.com`. Minimum browsers: Chrome 120, Firefox 142 desktop. Target OS: Windows with MossDL installed.

## Integration

The existing versioned `browser-capture/1` native protocol is preserved. Captures require an acknowledged native handshake and result; disconnected actions fail visibly instead of simulating a queue. Capture requests are split into at most 128 candidates per batch. No browser download is cancelled until the app acknowledges delivery.

Signed URL parameters are preserved during handoff. The engine remains responsible for validating and handling captured content and for its public redaction/storage policy.

`npm run build` also emits validated prebuilt common scripts for desktop packaging. The desktop resource list excludes node_modules and release/test artifacts. Installed runtime use of the Python registration helper can copy these verified bundles without Node/npm; source changes require rebuilding them.

Privacy: [PRIVACY.md](PRIVACY.md). Store kit: [CHROMEWEBSTORE.md](CHROMEWEBSTORE.md).
