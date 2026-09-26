# Copy-and-paste listing fields

Name: MossDL Capture

Summary: Send download links and media to the MossDL desktop app. Requires MossDL for Windows.

Single purpose: Send browser download links and the context needed to retrieve them to the local MossDL application.

Description:

MossDL Capture connects Chrome or Firefox to the MossDL Windows download manager.

Send a link from the right-click menu, find available file links on a page, or send media to MossDL. Optional stream detection notices supported media addresses for the current tab. You choose when to send those addresses to the app.

Catch downloads is optional and starts disabled. When enabled, a browser download is stopped only after MossDL acknowledges the handoff. If the desktop connection fails, your browser download is left untouched.

Optional site-session sharing asks before passing cookies to your local MossDL installation. The app decides which session cookies it supports. This does not guarantee access to every site or protected stream.

Requires the MossDL Windows desktop application and its browser connection. This extension is not a standalone downloader. Chrome 120+ or Firefox 142+ on desktop is required.

Support: https://github.com/alecvs3/MossDL/issues

## Dashboard fields (do not paste into Description)

Category: Tools. Language: English.
Publisher/contact email: enter your actual monitored publishing details; none invented.
Firefox license: GNU GPL v3 or later (project package/license declaration; confirm the source release license on submission).

## Permission explanations

nativeMessaging: Send selected URLs and user-requested session data to the local MossDL application and wait for its acknowledgement.
storage: Save capture preferences and the accepted-link counter locally in the browser.
tabs: Identify the current page and associate capture/session actions with the correct tab.
webRequest: Detect media addresses for Find streams when the user enables it.
downloads: Observe browser downloads and cancel one only after the user-enabled handoff is acknowledged by MossDL.
contextMenus: Provide Send link, Send selection, page capture and session-sharing actions.
cookies: Read only the selected site's cookies for confirmed session sharing or a verification flow initiated by MossDL.
notifications: Show whether capture/connection actions succeeded or failed.
http://*/* and https://*/*: Support capture on user-selected websites and media sources, rather than a single fixed site. Observed data is not sent automatically just because a page loads.

Remote code: No. All executable JavaScript is bundled locally; no CDN, eval, or remote script loader is used.

## Privacy disclosure mapping

The extension handles browsing activity (page/file URLs), website content (links, request metadata and cookies), and authentication information (session cookies/verification answers). Declare these categories; do not select "no data". It communicates with a local native application. Capture preferences and an accepted-link count stay in browser-local storage. No analytics/debug HTTP collector is included. The desktop app contacts the selected download sources.

Data is not sold, used for unrelated purposes, advertising, creditworthiness or lending. Review the dashboard's exact questions against PRIVACY.md. Website privacy and extension privacy are separate.

Privacy URL: https://mossdownloader.com/extension-privacy/ — use only AFTER publishing and checking that page.

## Reviewer notes

MossDL Capture is a Windows desktop companion. Native host name: ai.transfer.manager.browser. Firefox ID: capture@mossdl.com. Chrome ID is assigned by the dashboard and must be allowlisted by the desktop installation.

Reproduce extension: extract the source archive; Node 24; npm ci; npm run check; npm test; npm run build. Outputs: build/chrome and build/firefox. esbuild versions and all dependencies are pinned by package-lock.json; no private services or keys are required to build.

Without the app, the popup honestly shows Offline, lets you change preferences, and reports connection errors for capture. Catch downloads and Find streams start off. To test a complete handoff, install the provided release of MossDL, enable its browser connection, open a normal HTTPS page with a downloadable file, and use Send link to MossDL. Verify receipt in the app. Enable Catch downloads and repeat, then close/disconnect the app and confirm browser downloads are preserved. Share site session requires explicit confirmation; use a test account only.

Desktop installer URL/version: REQUIRED BEFORE REVIEW; no public installer existed when this kit was prepared. Do not claim a local developer installation is a distributable companion. The listing screenshot shows the real popup with no native host, not fabricated connected/download data.
