# Verification — 2026-09-26

- Extension TypeScript: zero errors.
- Behavioral regression tests: 13 passed (handshake/ack ordering, signed URLs, opt-in behavior, browser-download preservation, partial/batched delivery, session readiness, origin checks).
- Python native/browser suites: 23 passed across extension build, install, protocol, admission/security, and inbox behavior.
- Firefox web-ext lint: zero errors, zero warnings, zero notices.
- Firefox installed the built package as a temporary add-on in an isolated headless profile; normal user profiles and native-host registrations were not changed.
- Real Chromium extension smoke: service worker and popup loaded, defaults/preferences persisted, restricted-page feedback worked, no popup JavaScript errors.
- Store archives contain only explicit runtime assets and manifest.json at ZIP root. Native manifests, node_modules, tests and developer paths are excluded.
- Native host origin wildcards removed; Firefox identity fixed; prebuilt desktop resources exclude build dependencies.

## Limits

Store IDs, account verification and store approval cannot be tested before submission. The desktop source's protocol is tested, but a public packaged Windows installer has not been supplied or installed on a clean machine with final store IDs. This is not an end-to-end certification of the unreleased desktop app. Firefox temporary installation is a startup check, not full Firefox UI automation.

The listing screenshot shows the actual Chromium popup with no native host in the isolated test profile. It does not invent download counts or a connected app. The UI bundle is shared with Firefox; no Firefox-specific screenshot is claimed.

The proposed extension privacy URL must be publicly hosted before submitting for final review. See START-HERE.md for required account/installer steps.

- Extracted reviewer source into a fresh temporary directory, installed locked dependencies and rebuilt: every runtime file matched both upload ZIPs byte for byte.
- Root TypeScript and all 10 frontend test suites passed; website check/build and six-route artifact verification passed.
