# Upload MossDL Capture

The extension files are built and tested. Store approval is separate; do not publish the listing until the desktop app dependency and public privacy link below are available.

## Chrome

Upload page: https://chrome.google.com/webstore/devconsole/

1. New item → upload **mossdl-chrome-0.1.0.zip**. Upload it as a draft; do not unzip it.
2. Assigned Chrome ID: dknlgnaknfaoflbmobdknjfjdagieccm. This is now recorded in build.config.json for future desktop native-host builds. The desktop installer still needs to be built and distributed with it; no registry changes were made. The already-uploaded extension ZIP needs no ID edit.
3. Paste the listing and privacy answers from LISTING.md. Upload listing/icon-128.png, listing/chrome-popup.png, and listing/small-promo.png to their corresponding graphic slots.
4. Add the live extension privacy URL and working desktop installer/reviewer instructions, then submit for review. Account registration, developer agreement, fee, contact/trader verification and two-step verification are handled by you in the dashboard.

## Firefox

Upload page: https://addons.mozilla.org/developers/addon/submit/distribution

1. Choose **On this site** (listed on addons.mozilla.org).
2. Upload **mossdl-firefox-0.1.0.zip** without extracting it.
3. When asked for source code, upload **mossdl-reviewer-source-0.1.0.zip**. The extension uses bundled TypeScript; source and reproducible instructions are included.
4. Paste listing/reviewer information from LISTING.md. Select Firefox desktop on Windows; the native companion is not an Android extension.
5. Attach the icon and honest screenshot, supply the live privacy URL and desktop installer information, then submit. Stable Firefox ID: capture@mossdl.com; minimum Firefox 142.

## Two release dependencies that ZIP files cannot supply

- A public **Windows MossDL installer with browser/native messaging support**, configured for the Chrome store ID and Firefox capture@mossdl.com. Existing development registrations are not changed by this kit. The extension is a companion, not a standalone downloader. No public desktop installer was available during preparation.
- A live **extension privacy-policy URL**. The policy is supplied as PRIVACY.md, and a website page has been added at /extension-privacy/. Proposed URL: https://mossdownloader.com/extension-privacy/. It still needs public hosting; localhost is not a store privacy URL.

Build and store validation passed as recorded in VERIFICATION.md. These are upload artifacts, not a guarantee of store approval or clean-machine desktop integration. No accounts, payments, publication or registry changes were performed.
