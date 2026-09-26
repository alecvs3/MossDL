# Privacy policy

_Applies to MossDL, the desktop app and its browser extension. Last updated 2026-09-26._


MossDL has **no analytics, no crash reporting and no accounts**. Its logs are written to your own disk, with passwords, cookies, tokens and signed URLs removed before anything is recorded.

Apart from the downloads you ask for, this is every host MossDL contacts on its own, and when:

| When | Host | Why | Turn it off |
|---|---|---|---|
| Weekly, on your selected connection | `easylist.to`, `ublockorigin.github.io`, `pgl.yoyo.org` | Refresh the ad-blocking filter lists (about 4 MB) | Settings → **Full filter lists** (the built-in list is used instead) |
| When a site is shown in the list | That site itself | Fetch its icon. No third-party icon service is used. | n/a |
| When you sign in to Mullvad | `api.mullvad.net` | Register a device key and fetch the list of relays | Don't sign in |
| When you install the solver browser | `api.github.com` (Clearcote releases) | Download the browser used for Explore and captchas | Don't install it |
| When you click "Check for updates" | Update feed | Check for a newer version | Don't click it |
| Only if you enable automatic captcha solving **and** the audio solver | `api.wit.ai` | Speech-to-text for audio captchas: the captcha's audio clip is sent | Settings → Captcha → **Audio solver / speech-to-text** |
| Only if you enter your own API key | `2captcha.com` / `api.anti-captcha.com` | Paid captcha solving | Leave the key empty |

**Connections and leaks.** Every request MossDL makes goes out on the connection you selected, including page crawls, link resolving and icon fetches. If that connection is down, the request fails instead of going out on your normal connection. The one exception is quota rotation: by default, when a host's download quota is reached, MossDL may move that download to another healthy connection, **including your direct one**. If you depend on a VPN, turn off **Connections → Settings → Allow falling back to Direct**.

**Secrets.** Mullvad account numbers, WireGuard keys, proxy passwords and API keys are kept in the Windows Credential Manager, not in plain-text settings.

## Browser extension

The extension talks only to the MossDL app on your computer, over the browser's native messaging. It does not contact any other server.

## Questions

Open an issue at https://github.com/alecvs3/MossDL/issues.
