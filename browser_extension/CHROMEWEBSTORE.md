# Chrome Web Store submission

The current submission kit is in `release/`. Start with [store/START-HERE.md](store/START-HERE.md) and use [store/LISTING.md](store/LISTING.md) for listing and privacy answers.

Upload `release/mossdl-chrome-0.1.0.zip` at https://chrome.google.com/webstore/devconsole/ as a draft. Send the assigned Chrome extension ID back so it can be included in the desktop native-host allowlist. The ZIP itself needs no guessed ID.

Icons, the actual extension screenshot and 440Ã—280 promotional tile are in `release/listing/`. Firefox has its own ZIP and reviewer source archive; do not upload the source archive as the extension.

Runtime fixes and validation results are recorded in [store/VERIFICATION.md](store/VERIFICATION.md). Final submission still requires a publicly hosted extension privacy policy and a distributable Windows companion installer with the final extension IDs. No store publication or approval is claimed.

## Draft listing — 2026-09-26

Chrome ID: dknlgnaknfaoflbmobdknjfjdagieccm. Recorded in build.config.json for generated desktop native-host admission. Installed app/registry not changed. The uploaded extension ZIP is unchanged.

Description: paste only store/CHROME-DESCRIPTION.txt. Do not paste category, contact, license or reviewer instructions into Description.

- Category: Tools; language: English.
- Store icon: release/listing/icon-128.png (128×128 PNG).
- Screenshot: release/listing/chrome-popup.png (1280×800 RGB PNG).
- Small promo: release/listing/small-promo.png (440×280 RGB PNG).
- Promo video and marquee: leave empty for now; optional.
- Official URL: leave None until domain ownership is verified in Search Console.
- Homepage: use a working HTTPS project page; https://github.com/alecvs3/MossDL until the website is public.
- Support: https://github.com/alecvs3/MossDL/issues
- Mature content: unchecked; this general-purpose extension is not marketed for mature content.
- Item support: enable if you will monitor questions; use the GitHub issue URL above for bug reports.
- Privacy: complete permission justifications and disclosures from store/LISTING.md, and publish the extension privacy page before submitting.
- Test instructions: provide the downloadable Windows companion installer/version and the handoff steps in store/LISTING.md. No website account login is required for a basic public-file test.
- Distribution: public when ready for general release; retain draft until installer/privacy dependencies are ready.
- Analytics, installs, impressions, users and ratings are reporting pages, not listing text to fill in.

Official asset requirements: https://developer.chrome.com/docs/webstore/images
