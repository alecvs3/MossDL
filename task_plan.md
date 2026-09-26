# Task Plan: MossDL First Public Release (v0.1.0) Launch

## Goal
Prepare, verify, and execute the official v0.1.0 release for MossDL: integrate Chrome Web Store and Firefox AMO extension links into desktop onboarding, enrich the website with brand graphics, integrate support email and domain redirects, fix the boundary contract test gate (19/19 pass), commit all prepped work, push to GitHub (`alecvs3/MossDL`), build & publish the GitHub release with installers and extension artifacts, and deploy the updated website to Cloudflare Pages.

---

## Phases

### Phase 1: Engine Quality Gate Integrity (Contract Repair & Delay Fix)
- [x] Fix `contract-scanner` check by restoring `concurrency_auditor.load_persisted_profiles()` call in `engine/service.py`
- [x] Fix loopback domain resolve stagger locks and resolve lanes in `engine/resolve_stagger.py` and `engine/service.py`
- [x] Rebuild `transfer-core` in release mode to incorporate scheduler adoption fix
- [x] Run `python scripts/contract_scanner.py` and verify 0 violations
- [x] Run `python scripts/check_all.py --profile fast` and verify all 19/19 checks pass
- **Status:** completed

### Phase 2: App Onboarding Extension Integration
- [x] Update `src/figma/components/WelcomeWizard.tsx` (Step 2: Extension):
  - Add primary store links with status badges:
    - Chrome Web Store (`https://chromewebstore.google.com/detail/mossdl-capture/dknlgnaknfaoflbmobdknjfjdagieccm`)
    - Firefox Add-ons (`https://addons.mozilla.org/firefox/addon/mossdl-capture/`)
  - Clean up manual unpacked developer fallback (remove hardcoded `N:\transfer\...` path)
- [x] Update `src/figma/pages/SettingsPage.tsx` settings dialog with extension store links
- [x] Run `npx tsc --noEmit` and build verification (`npm run build`)
- **Status:** completed

### Phase 3: Website Polish & Graphics Integration
- [x] Copy brand assets from `docs/brand/` into `website/src/assets/brand/` and `website/public/brand/`:
  - `feature-downloads.png`, `feature-explore.png`, `feature-following.png`, `feature-connections.png`, `feature-captchas.png`
  - `hero-dark.png`, `hero-light.png`
  - `demo.mp4` / `demo-poster.png` and `demo.gif`
- [x] Update `website/src/pages/index.astro`:
  - Add visual feature showcase cards using the rendered graphics
  - Add demo video/gif preview with responsive fallback
  - Add browser extension callout section with Chrome & Firefox store buttons
- [x] Integrate `support@mossdl.com`:
  - Add to `website/src/config/site.ts`
  - Add to `website/src/layouts/SiteLayout.astro` (footer contact link)
  - Add to `website/src/pages/privacy.astro` and `website/src/content/extension-privacy.md`
- [x] Verify `npm --prefix website run build` (6/6 static pages verified, 0 errors)
- **Status:** completed

### Phase 4: Git Repository Staging & Remote Push
- [x] Review git status and untracked files
- [x] Add remote `origin`: `https://github.com/alecvs3/MossDL.git`
- [x] Stage and commit all release preparations with descriptive conventional commit
- [x] Push to `main` on `origin` (`https://github.com/alecvs3/MossDL.git`)
- **Status:** completed

### Phase 5: Build Release Installers & Publish GitHub Release (v0.1.0)
- [x] Package browser extension artifacts (Chrome & Firefox ZIPs) via `scripts/build_browser_extension.py package`
- [x] Build release binaries and Windows installers (`scripts/release.ps1` or `npx tauri build`)
- [x] Generate SHA-256 checksums for all release artifacts
- [x] Create GitHub Release `v0.1.0` via `gh release create v0.1.0` with release notes and attach all assets:
  - `MossDL_0.1.0_x64-setup.exe` (NSIS installer)
  - `MossDL_0.1.0_x64_en-US.msi` (MSI installer)
  - `chrome-extension.zip`
  - `firefox-extension.zip`
  - `edge-extension.zip`
  - `SHA256SUMS.txt`
- **Status:** completed

### Phase 6: Website Deployment & Verification
- [x] Run `npm --prefix website run build` (fetched `v0.1.0` from GitHub API, installer download live!)
- [x] Deploy website via `npx wrangler pages deploy dist --project-name mossdl-website --branch main`
- [x] Verify live domains:
  - `https://mossdownloader.com/` (loads homepage with new graphics, video, and extension callout)
  - `https://mossdownloader.com/download/` (offers live v0.1.0 download button pointing to GitHub asset)
  - `https://mossdl.com/` (redirects 301 to `mossdownloader.com`)
- **Status:** completed

---

## Decisions Made
| Decision | Rationale |
|----------|-----------|
| Dual-Option Onboarding | Offer both direct store links (with "in review" badge) and manual local install fallback so users aren't blocked while Google/Mozilla approve listings |
| High-Fidelity Rendered Assets | Use the authentic UI render stills from `docs/brand/` instead of empty placeholder screenshots on the marketing site |
| Native Cloudflare Pages Direct Upload | Use existing `mossdl-website` Pages project with established Wrangler deployment configuration |
| Standard Multi-Artifact GitHub Release | Bundle Windows NSIS + MSI + Browser Extension ZIPs with SHA256 checksum file |

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| `contract-scanner` failed on `load_persisted_profiles` | 1 | Restore call in `engine/service.py` after store attachment |
