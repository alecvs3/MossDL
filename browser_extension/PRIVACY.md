# MossDL Capture privacy

Updated 2026-09-26. Applies to MossDL Capture 0.1.0.

MossDL Capture sends download information to the MossDL Windows application installed on your computer. The desktop application is required to receive it.

## Information handled

When you send a link, scan a page, grab media, or enable Catch downloads, the extension handles file URLs, the associated page address, and available file/request metadata. URLs can contain access tokens needed to retrieve a file. They are passed to the local application for downloading, not sent to an analytics service.

Find streams is off by default. If enabled, detected media addresses are held in extension memory for the current tab until you use Grab media. The list is cleared when that tab navigates or closes, when detection is disabled, or when the background context restarts. It is not automatically uploaded to MossDL.

Share this site's login asks for confirmation before passing that site's cookies to the desktop application. Cookies can grant access to an authenticated session. The desktop application decides which supported cookies to retain. For a browser verification flow started in MossDL, the extension can return the verification answer, scoped cookies, and browser user agent to that same local application. The app validates the handoff ticket and origin.

## Storage and connections

Capture preferences and an accepted-link counter are stored in browser-local extension storage, not browser sync. Media observations remain in memory. No download queue or cookie jar is stored by the extension. Local console diagnostics contain event names/reasons rather than captured URLs, cookie values, or request payloads. There is no analytics SDK or debug HTTP collector in this extension.

Native messaging delivers information to the local desktop app. The app connects to the requested download sources and may persist download records and supported session information under its own settings. Removing the extension does not remove data already handed to the desktop app.

## Controls and deletion

Catch downloads and Find streams start disabled. You can change capture settings in the popup. Browser downloads are left untouched if MossDL cannot acknowledge a handoff. You can disable the Media button independently. Uninstalling the extension removes its browser-local settings and counter. Use the desktop app's controls for download/history/session data it already received.

Data is used for the capture/download functions described here, not sold, used for advertising, or used for creditworthiness decisions. MossDL Capture's use of information follows the Chrome Web Store User Data Policy, including its Limited Use requirements.

## Contact and changes

Privacy questions: https://github.com/alecvs3/MossDL/issues. Issues are public; do not include cookies, credentials, private URLs, or personal information. This policy will be updated when the extension's data handling changes.
