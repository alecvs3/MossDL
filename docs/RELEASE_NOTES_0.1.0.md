# MossDL v0.1.0 — Initial Public Release 🚀

Welcome to the initial public release of **MossDL (MossDownloader)**, a modern, blazing-fast Windows download manager engineered to discover and stream the true media files behind complex web pages and cyberlockers.

---

## ✨ Key Highlights

- **Hybrid Rust + Python Architecture**:
  - **Tauri v2 Native Shell**: Fluid, lightweight Windows desktop shell featuring native Windows 11 Mica blur materials, non-draggable IPC regions, and a responsive React 19 UI.
  - **`transfer-core` (Rust Engine)**: Turbo-charged multi-part concurrent downloader with dynamic chunk scheduling, direct zero-copy file assembly, and streaming SHA-256/MD5 validation.
  - **`transfer-engine` (Python Backend)**: Battle-tested extraction pipeline equipped with 72+ provider plugins, session management, and deep URL resolution.

- **🌐 MossDL Capture Browser Extension**:
  - Native integration for **Google Chrome**, **Mozilla Firefox**, and **Microsoft Edge**.
  - Intercept browser downloads directly into MossDL or sniff media batches from active tabs.
  - Chrome Web Store ID: `dknlgnaknfaoflbmobdknjfjdagieccm`
  - Firefox AMO Slug: `mossdl-capture`

- **⚡ Performance & Stability Fixes**:
  - **Loopback Stagger Elimination**: Resolved a 13.6-second delay on local domain resolution locks, bringing loopback test and benchmark completion down to under 2.0 seconds.
  - **Scheduler Adoption Guard**: Fixed task adoption logic with closed flags in strict-host multi-connection scenarios.
  - **Concurrency Persistence**: Restored persistence loading boundary for host concurrency profiles across restarts.
  - **Strict State Separation**: Zero UI simulation; UI strictly observes authoritative engine events via typed Tauri IPC.

---

## 📦 Downloads & Verification

| Asset | Description |
| :--- | :--- |
| `MossDL_0.1.0_x64-setup.exe` | Standard Windows NSIS Installer (recommended) |
| `MossDL_0.1.0_x64_en-US.msi` | Windows MSI Installer |
| `chrome-extension.zip` | MossDL Capture for Google Chrome (Developer mode) |
| `firefox-extension.zip` | MossDL Capture for Mozilla Firefox |
| `edge-extension.zip` | MossDL Capture for Microsoft Edge |
| `SHA256SUMS.txt` | Cryptographic SHA-256 checksums for all release binaries |

### Verifying Checksums
In PowerShell:
```powershell
Get-FileHash -Algorithm SHA256 .\MossDL_0.1.0_x64-setup.exe
```
Compare the output against the values published in `SHA256SUMS.txt`.

---

## 🔒 Code Signing Notice
Binaries in this initial release are built using our automated release pipeline. Windows SmartScreen may display an unknown publisher notification until reputation is established. You may proceed by clicking **More info → Run anyway**.

---

## 💬 Support & Links
- **Website**: [mossdownloader.com](https://mossdownloader.com) / [mossdl.com](https://mossdl.com)
- **Repository**: [github.com/alecvs3/MossDL](https://github.com/alecvs3/MossDL)
- **Support**: `support@mossdl.com`
