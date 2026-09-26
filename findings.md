# Findings & Release Readiness Dossier

## 1. Browser Extensions Status & Identification
- **Chrome Web Store**:
  - Name: `MossDL Capture`
  - Extension ID: `dknlgnaknfaoflbmobdknjfjdagieccm`
  - Status: Submitted, Pending Google Review
  - Public Store URL: `https://chromewebstore.google.com/detail/mossdl-capture/dknlgnaknfaoflbmobdknjfjdagieccm`
  - Target Browsers: Google Chrome, Microsoft Edge, Brave, Opera, Vivaldi
- **Mozilla Firefox Add-ons (AMO)**:
  - Add-on Slug: `mossdl-capture`
  - Internal ID: `capture@mossdl.com`
  - Developer Dashboard: `https://addons.mozilla.org/en-US/developers/addon/mossdl-capture/edit`
  - Public Store URL: `https://addons.mozilla.org/firefox/addon/mossdl-capture/`
  - Status: Submitted, Pending Mozilla Review

## 2. Domains, DNS & Support Infrastructure
- **Domains**:
  - Primary Domain: `mossdownloader.com` (HTTP 200 OK via Cloudflare Pages)
  - Redirect Domain: `mossdl.com` (HTTP 301 Redirect to `mossdownloader.com` via Cloudflare Pages)
  - DNS: Resolved via 1.1.1.1 / 8.8.8.8 to Cloudflare edge IPs `172.67.x.x` and `104.21.x.x`
- **Support Contact**:
  - Email Forwarder: `support@mossdl.com`
  - Integration Points:
    - Website footer & SiteLayout
    - Extension privacy policy & Website privacy policy
    - Root README and documentation
    - Onboarding & help links

## 3. Brand Graphics Inventory (`docs/brand/`)
Claude pre-rendered a complete suite of brand assets from live stylesheets:
- `hero-dark.png` (1600×800) & `hero-light.png` (1600×800): Theme-paired hero graphics
- `feature-downloads.png` (1200×700): Segmented downloading on Rust transfer-core
- `feature-explore.png` (1200×700): Hierarchical link exploration tree
- `feature-following.png` (1200×700): Mirror navigation through countdowns with adblock
- `feature-connections.png` (1200×700): WireGuard / Mullvad multi-connection routing
- `feature-captchas.png` (1200×700): Clearcote / Turnstile automated solving & browser handoff
- `demo-dark.gif` (880px, 10fps), `demo-light.gif`, `demo.mp4` (1080p H.264), `demo-poster.png`
- `social-preview.png` (1280×640): GitHub social card (<1MB)

## 4. Website Current State (`website/`)
- Framework: Astro (Static site generator, 0 runtime JS by default)
- Current build: 6 pages (`/`, `/download/`, `/guides/getting-started/`, `/privacy/`, `/extension-privacy/`, `/404.html`)
- Hosting: Cloudflare Pages Direct Upload project (`mossdl-website`)
- Gap identified:
  - Homepage only has an empty-queue screenshot (`assets/screenshots/downloads.png`) and vector artwork.
  - Doesn't feature the rich brand feature graphics (`feature-downloads`, `feature-connections`, `feature-captchas`, etc.) or the interactive/demo video.
  - Release status is currently `"unavailable"` in `generated/release.json`; once v0.1.0 is published on GitHub, `npm run build` will automatically populate the download links!

## 5. App Onboarding Current State (`src/figma/components/WelcomeWizard.tsx`)
- Step 0: Clearcote Solver Engine install & verification
- Step 1: Appearance & Theme selection
- Step 2: Browser Extension onboarding
  - Currently contains manual instructions for `chrome://extensions` "Developer mode" and "Load unpacked" pointing to a hardcoded local development path (`N:\transfer\.test-artifacts\browser-extension\chrome`).
  - Needs update to showcase official store install buttons (Chrome Web Store and Firefox AMO) with store review status badges, plus streamlined manual unpacked fallback for testing.

## 6. Offline Quality Gate Status (`python scripts/check_all.py --profile fast`)
- Passed 18/19 checks:
  - `rust-check`, `rust-tests`, `frontend-build`, `python-tests`, `dead-code-python`, `dead-code-frontend`, `duplication-check`, `challenge-foundation`, etc.
- 1 Failing check:
  - `contract-scanner`: `Public method 'load_persisted_profiles' on HostConcurrencyAuditor has 0 call-sites in engine/`
  - Cause: Claude removed `concurrency_auditor.load_persisted_profiles()` from `service.py` right before hitting the session limit.
  - Fix: Restore call in `service.py` after `attach_store`.
