# Competitor research: MossDL vs JDownloader 2, IDM, FDM, AB Download Manager, Motrix

Research date: 2026-09-25/26. Every claim below either cites a source or is marked
**unverified**. Version numbers change often, so re-check them on the day you run the
benchmark and record the exact build in the results (`client.version`).

Status labels used in the tables:

- **Yes / No**: stated by the vendor, the source code or official docs (cited).
- **3rd-party**: reported only by third parties. Treat it as a lead, not a fact.
- **Unknown**: not found. The benchmark operator must check it in the app and record it.

---

## 0. MossDL: what the code actually does

These facts were read from this repository, not from marketing copy:

| Aspect | Finding | Where |
|---|---|---|
| Architecture | Tauri 2 shell (`MossDL.exe`, WebView2 UI) + Python engine sidecar (`transfer-engine`) + Rust `transfer-core` (reqwest 0.13, rustls, tokio). The core is long-lived: one JSON-RPC-over-stdio process that all transfers share. | `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`, `engine/rust_session.py` |
| Backend selection | Rust core preferred (`{"rust": 300, "custom": 100}`). The README still calls the Python backend the default, so the README is stale. | `engine/backend_selection.py:16` |
| Connections per file | Default **8** (`network.connectionsPerFile`), UI range 1-32. | `src/App.tsx:341`, `src/figma/settingsSchema.ts:30`, `engine/service.py:363` |
| Segmenting threshold | Segments only when `size > min_segment_size`. The production default is **16 MiB**, so files of 16 MiB or less use **one connection**. | `transfer-core.rs:1117`, `engine/limits.py:236` |
| Segmenting behaviour | Static split into N ranges plus lock-free **work stealing**: an idle worker takes the tail of the slowest range. Writes go to a sparse `.part` file with a `.part.ranges.json` checkpoint journal, and the file is renamed when complete. | `transfer-core.rs` (`RangeScheduler`, lines ~43-60, 1140-1180) |
| Concurrent downloads | Default **6** (`_DEFAULT_MAX_CONCURRENT`). | `engine/service.py:108` |
| HTTP client | UA `transfer-manager-rust-core/0.1`, `tcp_nodelay`, 64 idle conns/host, 15 s connect / 60 s read timeouts. No explicit HTTP/2 setting. | `transfer_core/session.rs:96-102` |
| Protocols | HTTP/HTTPS only. Magnet/torrent is explicitly unsupported and FTP is not downloadable. HLS/DASH (non-encrypted only) is handled by the Python `MediaAssembler`, which fetches segments **sequentially** on one connection, with optional ffmpeg remux. | `engine/intake_analysis.py:110-114`, `engine/media_pipeline.py`, `engine/providers/media.py` |
| File-host plugins | 81 provider plugin directories (e.g. mega, gofile, transfer.it, datanodes, mediafire, pixeldrain, 1fichier, google-drive). | `plugins/` |
| Captcha | Clearcote (bundled Chromium) solver flow. | `engine/clearcote_manager.py`, `engine/captcha.py` |
| Routes / VPN | Route kinds: direct, system_vpn, wireguard (boringtun + smoltcp inside the core), http_proxy, socks5, docker_socks5. | `engine/rust_backend.py:21`, `Cargo.toml` |
| Ad-blocking | Brave `adblock` crate 0.13 in the core (uBlock-format lists). | `Cargo.toml`, `transfer-core.rs` (`adblock_*` methods) |
| Resume | Core: range journal. Engine: after a relaunch, unfinished tasks come back **paused** and wait for the user to press Resume. | `engine/service.py:478-488` |
| Automation surface | Headless stdio JSON-RPC (`engine_entrypoint.py`), `python -m engine.cli download/add/rpc`, and an HTTP JSON-RPC adapter (`serve-http`, token). | `engine/cli.py`, `openapi.json` |
| License | GPL-3.0 (recent commit `b388353`). | git log |

Two engine behaviours found while building the harness were measured once and are
**not yet understood**. They affect the benchmark and are filed as follow-up tasks:

1. **Engine-path small files run serially on a new host.** Through the full engine
   (`add_task` + `download_task`), ten 4 MiB files from one host downloaded one at a time,
   with about 1 s between each completion and the next dispatch: about 12 s for 40 MiB.
   The same files through the core directly took about 0.2 s. Learned per-host ceilings
   are persisted to `<repo>/logs/host_concurrency_profiles.json`, which sits **outside
   the data dir**, so they leak between runs. Before this work that file held
   `127.0.0.1` with `verified_ceiling: 1`.
2. **Core resume and connection caps.** In one smoke trial the core seemed to re-download
   the pre-kill bytes after a hard kill: the server sent 1.41x the payload, although the
   SHA-256 was correct. It also failed outright against a host that allows 2 connections
   and answers 503 beyond that. See `results/smoke-20260926.jsonl`.

---

## 1. Internet Download Manager (IDM), Tonec Inc.

- **Architecture / platform:** Native Windows application, Windows 7 and later only
  ([Wikipedia](https://en.wikipedia.org/wiki/Internet_Download_Manager)). Proprietary.
- **Price:** 30-day trial. USD 11.95 for 1 year or 24.95 lifetime for 1 PC
  ([buy page](https://secure.internetdownloadmanager.com/buy_idm.html)).
- **Latest release:** 6.43 Build 11, 23 Sep 2026 (fixes 403 errors and MP4 assembly).
  Build 10 (20 Aug 2026) "enhanced HTTP/2 performance"
  ([news](https://www.internetdownloadmanager.com/news.html)).
- **Segmenting:** "Dynamic file segmentation": IDM re-splits during the download,
  finding "the largest segment ... and divide it in half" when a connection frees up
  ([features](https://www.internetdownloadmanager.com/features2.html)). This is the same
  idea as MossDL's work stealing.
- **Connections:** Configured in Options -> Connection -> "Default max. conn. number". The
  official FAQ recommends 16 or 32 for broadband
  ([FAQ](https://www.internetdownloadmanager.com/register/new_faq/functions8.html)).
  **Default 8 and max 32 are 3rd-party claims**
  ([helpdeskgeek](https://helpdeskgeek.com/free-tools-review/does-internet-download-manager-increase-download-speed/)),
  so verify on install.
- **Protocols:** HTTP, HTTPS, FTP, MMS
  ([features](https://www.internetdownloadmanager.com/features2.html)). HTTP/2 mentioned
  in 2026 release notes. No BitTorrent. Video/stream capture works through the browser
  integration. Whether a bare `.m3u8` passed to `/d` works is **unverified**.
- **Temp storage:** Parts live in `%APPDATA%\IDM\DwnlData\<user>` and the file is moved
  (same volume) or rebuilt (different volume) into the destination at the end
  ([FAQ problems5](https://www.internetdownloadmanager.com/register/new_faq/problems5.html)).
  This matters for disk I/O and completion timing.
- **Proxy:** Supports proxy servers and Basic/NTLM/Kerberos auth
  ([features](https://www.internetdownloadmanager.com/features2.html)). No VPN.
- **Captcha / file hosts:** No solver and no host plugins (N/A: IDM relies on the browser session).
- **Browser integration:** Edge, Chrome, Firefox, Opera and others.
- **Settings storage:** `HKCU\Software\DownloadManager`
  ([FAQ](https://www.internetdownloadmanager.com/register/new_faq/functions17.html)). The
  value name for the connection count (`MaxConnectionNumber`?) is **unverified**, so read
  the setting in the UI.
- **Automation:** `IDMan.exe /d URL /p path /f name /n /a /s /q /h`. `/a /h /n /q /f /p`
  only work with `/d`
  ([command line](https://www.internetdownloadmanager.com/support/command_line.html)).
  There is no status API, so completion is read from the filesystem.
- **Published benchmarks:** The vendor claims acceleration "up to 8 times"
  ([features](https://www.internetdownloadmanager.com/features2.html)). No method is published.

## 2. JDownloader 2, AppWork GmbH

- **Architecture:** Java (needs a JRE; the installer bundles one), cross-platform
  (Windows/macOS/Linux) ([Wikipedia](https://en.wikipedia.org/wiki/JDownloader)).
  GPLv3 with some sources not published (same source).
- **Latest:** Rolling self-updates. VideoHelp lists "2 r19136" (16 Sep 2026)
  ([VideoHelp](https://www.videohelp.com/software/JDownloader)). Record the revision from Help -> About.
- **Defaults (source code):** `MaxChunksPerFile` default **1**, range 1-20.
  `MaxSimultaneDownloads` default **3**. `MaxSimultaneDownloadsPerHost` default 1 (may
  only apply when the per-host limit is enabled, **unverified**). Hash check is enabled
  by default ([GeneralSettings.java](https://github.com/mirror/jdownloader/blob/master/src/org/jdownloader/settings/GeneralSettings.java)).
  Out of the box, JD downloads a direct file with **one connection**.
- **Protocols:** HTTP/HTTPS, FTP via plugin, HLS via `GenericM3u8` / `HLSDownloader`
  ([source](https://github.com/mirror/jdownloader/blob/master/src/jd/plugins/hoster/GenericM3u8.java)).
  No BitTorrent. Metalink and the DLC/RSDF/CCF containers are supported
  ([Wikipedia](https://en.wikipedia.org/wiki/JDownloader)).
- **File hosts:** "Hundreds" of host/decrypter plugins
  ([Wikipedia](https://en.wikipedia.org/wiki/JDownloader)). This is the closest
  competitor to MossDL's provider plugins.
- **Captcha:** Built-in OCR plus external solver services (e.g. 9kw, 2captcha)
  ([9kw guide](https://www.9kw.eu/hilfe_jdownloader.html)) and browser solving.
- **Proxy:** Connection Manager with HTTP/HTTPS/SOCKS4/SOCKS5 proxies and rotation
  (3rd-party guide: [ProxyCompass](https://proxycompass.com/knowledge-base/setting-up-proxies-in-jdownloader2/)).
  Reconnect scripts. No built-in WireGuard.
- **Extraction:** Automatic RAR extraction with password lists
  ([Wikipedia](https://en.wikipedia.org/wiki/JDownloader)).
- **Automation:**
  - **Folder Watch** extension. It watches `<JD>/folderwatch` every 1000 ms and accepts
    `.crawljob` files, plain or as a JSON array, with fields including `text`,
    `downloadFolder`, `filename`, `chunks`, `autoStart`, `forcedStart`, `autoConfirm`,
    `enabled`, `packageName`. Processed files move to `added/`
    ([support](https://support.jdownloader.org/en/knowledgebase/article/folder-watch-basic-usage)).
  - **Deprecated local API.** Enable it with Advanced Settings -> RemoteAPI ->
    `deprecatedapienabled`. It is served on `http://localhost:3128` and documents itself
    at `/help`. It is unauthenticated, so keep it local only
    ([titor.dev](https://titor.dev/guides/using-jdownloader2s-local-api/)).
  - **My.JDownloader API.** Namespaces include `linkgrabberv2/addLinks` (AddLinksQuery:
    `links`, `destinationFolder`, `autostart`, `packageName`, ...),
    `downloadsV2/queryLinks` (`bytesLoaded`, `bytesTotal`, `finished`, `speed`, `status`,
    `eta`) and `downloadcontroller/start`
    ([developer docs](https://my.jdownloader.org/developers/)).
- **Caution:** Search results surface third-party "JDownloader setup 2026" GitHub repos.
  Only use jdownloader.org. JD's installer has a bundleware history (2012/2014)
  ([Wikipedia](https://en.wikipedia.org/wiki/JDownloader)).

## 3. Free Download Manager (FDM), Softdeluxe

- **Architecture:** C++. Windows, macOS, Linux and Android. Freeware and proprietary since
  5.0, previously open source for 2.5-3.9.7
  ([Wikipedia](https://en.wikipedia.org/wiki/Free_Download_Manager)).
- **Latest:** 6.35.1.7021, 21 Sep 2026. 6.34.4 added "pause slow downloads" and 6.34.1
  improved the network proxy ([changelog](https://www.freedownloadmanager.org/changelog.htm)).
- **Segmenting:** "splits files into several sections and downloads them simultaneously"
  ([features](https://www.freedownloadmanager.org/features.htm)). The **default and
  maximum connections per download are unknown**; nothing official was found.
- **Protocols:** HTTP/HTTPS/FTP, BitTorrent (libtorrent) and magnet
  ([Wikipedia](https://en.wikipedia.org/wiki/Free_Download_Manager)). HLS is **unknown**.
  Video downloading uses the "Elephant" add-on
  ([download page](https://www.freedownloadmanager.org/download.htm)).
- **Captcha / file hosts:** None found.
- **Proxy:** A proxy setting exists (changelog 6.34.1). Supported types are **unverified**.
- **Remote control:** From an Android device
  ([features](https://www.freedownloadmanager.org/features.htm)).
- **Automation:** `fdm.exe --url URL`, `--hidden`, `--minimized` (FDM staff, 27 May 2020,
  [forum](https://www.freedownloadmanager.org/board/viewtopic.php?t=18253)). No save-path
  or file-name switch and **no silent add**: FDM opens a dialog. "Advanced -> Automation"
  can run an action after completion (same thread, 2025). **FDM needs a manual click per
  URL, so treat it as semi-manual.**
- **Security note:** The official site served a compromised Linux install script in
  2020-2022 ([Wikipedia](https://en.wikipedia.org/wiki/Free_Download_Manager)). Verify
  installer signatures.

## 4. AB Download Manager (amir1376)

- **Architecture:** Kotlin + Compose Multiplatform (JVM desktop). Windows, Linux, macOS
  and Android. Apache-2.0. About 18k GitHub stars
  ([GitHub](https://github.com/amir1376/ab-download-manager)).
- **Latest:** v1.10.4, 8 Sep 2026. v1.10.3 restricted the integration server to local
  connections ([releases API](https://api.github.com/repos/amir1376/ab-download-manager/releases?per_page=3)).
  An earlier issue reported the server bound to 0.0.0.0 without auth
  ([#1401](https://github.com/amir1376/ab-download-manager/issues/1401)).
- **Segmenting:** Multi-connection ("threads"). The **default and maximum thread count
  are unknown**; users ask for the max to be shown
  ([#578](https://github.com/amir1376/ab-download-manager/issues/578)). A 3rd-party
  summary says 1-8, which is **unverified**.
- **Protocols:** HTTP/HTTPS. HLS only when captured by the browser extension (from
  1.8.8). Manually pasted `.m3u8`/`.mpd` is not supported
  ([#1138](https://github.com/amir1376/ab-download-manager/issues/1138),
  [#685](https://github.com/amir1376/ab-download-manager/issues/685)). No torrent or FTP
  found. A 3rd-party wiki mentions proxy/PAC and DoH
  ([DeepWiki](https://deepwiki.com/amir1376/ab-download-manager)), **unverified**.
- **Captcha / file hosts:** None.
- **Automation:** Local REST API on port 15151
  ([REST-API.yml](https://github.com/amir1376/ab-download-manager/blob/master/REST-API.yml)):
  - `GET /queues`
  - `POST /add`: opens the dialog
  - `POST /start-headless-download` with `{downloadSource:{link,headers,downloadPage}, folder, name, queueId}`

  There is no status endpoint, so completion is read from disk.

## 5. Motrix (agalwood)

- **Architecture:** Electron UI over a bundled **aria2**. v1 is MIT-licensed on Windows,
  macOS and Linux ([GitHub](https://github.com/agalwood/Motrix)).
- **Versions:**
  - Latest **stable** is v1.8.19, published 2023-05-03
    ([API](https://api.github.com/repos/agalwood/Motrix/releases/latest)).
  - v2 "Motrix Turbo" is in **beta**. v2.0.0-beta.40 was published 2026-09-20
    ([API](https://api.github.com/repos/agalwood/Motrix/releases?per_page=5)). It uses
    Electron + React, a Motrix-maintained aria2 fork (`1.37.0-motrix.x`), and the "MDXP"
    JSON-RPC 2.0 protocol (port 16801 in Docker), a CLI `@motrix/cli` and a Docker headless server
    ([README](https://raw.githubusercontent.com/agalwood/Motrix/main/README.md)).
- **Defaults (v1.8.19 source):**
  - `ENGINE_RPC_PORT = 16800`.
  - `ENGINE_MAX_CONNECTION_PER_SERVER = 64`, used as the default for **both `split` and
    `max-connection-per-server`**
    ([constants.js](https://raw.githubusercontent.com/agalwood/Motrix/v1.8.19/src/shared/constants.js),
    [ConfigManager.js](https://raw.githubusercontent.com/agalwood/Motrix/v1.8.19/src/main/core/ConfigManager.js),
    [utils](https://raw.githubusercontent.com/agalwood/Motrix/v1.8.19/src/main/utils/index.js)).
  - `rpc-secret` is empty. `max-concurrent-downloads` is 5.
  - The bundled `aria2.conf` sets `min-split-size=1M`, `disk-cache=64M`,
    `file-allocation=falloc`, `rpc-listen-all=true`, `rpc-allow-origin-all=true`
    ([aria2.conf](https://raw.githubusercontent.com/agalwood/Motrix/v1.8.19/extra/win32/x64/engine/aria2.conf)).
    Motrix therefore opens **up to 64 connections even on small files** by default, while
    upstream aria2 allows at most 16 per server.
  - Upstream aria2 defaults are `split=5`, `max-connection-per-server=1`,
    `min-split-size=20M`, `file-allocation=prealloc`
    ([aria2 manual](https://aria2.github.io/manual/en/html/aria2c.html)).
- **Protocols:** HTTP/FTP, BitTorrent, magnet. No HLS: aria2 has none, and the v2 beta
  mentions "media downloads" but that is unverified.
- **Captcha / file hosts:** None in v1. v2 has a QuickJS plugin marketplace whose host
  coverage is **unknown**.
- **Proxy:** aria2 `all-proxy` (HTTP proxy) is supported in the config.
- **Automation:** aria2 JSON-RPC: `aria2.addUri([url], {dir, out, split, max-connection-per-server})`,
  `aria2.tellStatus(gid)` (status, completedLength, totalLength, downloadSpeed, connections)
  and `aria2.changeGlobalOption`, using `token:<secret>` when a secret is set. Completion is
  authoritative from `status == "complete"`.
- **Security note:** v1's `rpc-listen-all=true` with an empty secret exposes the RPC on
  the LAN. Run benchmarks on an isolated network or set a secret.

---

## 6. Feature comparison (verifiable facts only)

| | MossDL | IDM | JDownloader 2 | FDM 6 | AB DM | Motrix 1.8 |
|---|---|---|---|---|---|---|
| Platforms | Windows (Tauri; others untested) | Windows | Win/macOS/Linux | Win/macOS/Linux/Android | Win/macOS/Linux/Android | Win/macOS/Linux |
| License | GPL-3.0 | Proprietary, paid | GPLv3 (partly closed) | Freeware, proprietary | Apache-2.0 | MIT |
| Engine | Rust core + Python engine | Native | Java | C++ | Kotlin/JVM | aria2 (C++) |
| Default conns/file | 8 (files > 16 MiB only) | 8 (3rd-party) | 1 | Unknown | Unknown | 64 (split=64, min-split 1M) |
| Max conns/file | 32 | 32 (3rd-party) | 20 | Unknown | Unknown | 64 |
| Dynamic re-segmenting | Yes (work stealing) | Yes | Unknown | Unknown | Unknown | aria2 splits remaining pieces (not documented as work stealing) |
| HTTP/HTTPS | Yes | Yes | Yes | Yes | Yes | Yes |
| FTP | No | Yes | Yes | Yes | No | Yes |
| BitTorrent/magnet | No | No | No | Yes | No | Yes |
| HLS (pasted .m3u8) | Yes, sequential, unencrypted | Via browser; pasted unverified | Yes (GenericM3u8) | Unknown | No (browser capture only) | No |
| File-host plugins | Yes (81) | No | Yes (hundreds) | No | No | No (v2 plugins unknown) |
| Captcha handling | Yes (Clearcote browser) | No | Yes (OCR, services, browser) | No | No | No |
| Proxy | HTTP, SOCKS5 per route | Yes | HTTP/SOCKS4/5 | Yes (types unverified) | 3rd-party: yes | HTTP (`all-proxy`) |
| Built-in VPN | WireGuard in core | No | No | No | No | No |
| Ad-blocking (page intake) | Yes (adblock-rust) | No | No | No | No | No |
| Archive extraction | Yes | No | Yes | Unknown | No | No |
| Automation API | stdio/HTTP JSON-RPC, CLI | CLI switches | Folder Watch, local API, My.JD API | `--url` + dialog | REST :15151 | aria2 JSON-RPC :16800 |
| Status API | Yes | No | Yes (local/My.JD) | No | No | Yes |

"No" means no such feature was found in official material. Before publishing any "No",
confirm it in the current build.

---

## 7. Published benchmarks and what they are worth

- **Vendor comparisons with thin methodology** are common. Example: Hamax's blog reports
  228 vs 197 vs 187 MB/s (Hamax/IDM/Motrix) on "2 Gbps fiber, 32 connections, 10 GB file"
  with no trial count, variance or server description, published by the vendor of the
  winning product ([hamax.app](https://hamax.app/blog/best-idm-alternatives-free-fast-modern)).
  Do not cite its numbers. It is useful only as an example of what we must do better.
- **Review roundups** often state that the server, not the client, is usually the
  bottleneck, and that equal segment counts give near-equal times on large files. None of
  them publish measurements
  ([unstore.io](https://unstore.io/discover/best-internet-download-manager-alternatives-desktop/)).
  This matches our design: unshaped LAN runs will mostly tie, so the shaped scenarios are
  where clients differ.
- **Vendor acceleration claims:** IDM "up to 8 times"
  ([IDM](https://www.internetdownloadmanager.com/features2.html)). FDM and AB DM claim
  "faster download speed". None publish a method.
- **Malware-adjacent sources:** Several GitHub repos promise "pre-configured packs" or
  "full setup 2026" of these tools. Treat them as untrusted and never download from them.

## 8. Methodology references

- Repeated runs, warm-up and cache control: hyperfine documents warm-up runs, `--prepare`
  cache clearing, outlier detection, and at least 10 runs / 3 s by default
  ([hyperfine](https://github.com/sharkdp/hyperfine)).
- Dynamic run counts: Phoronix Test Suite adds runs while the standard deviation exceeds
  a threshold (3.5% by default, later tightened) and records system information
  ([PTS docs](https://github.com/phoronix-test-suite/phoronix-test-suite/blob/master/documentation/phoronix-test-suite.md)).
- JVM warm-up and statistics (relevant to JD and AB DM): Georges, Buytaert & Eeckhout,
  *Statistically Rigorous Java Performance Evaluation*, OOPSLA 2007
  ([ACM](https://dl.acm.org/doi/10.1145/1297027.1297033)).
- Measurement bias from environment changes: Mytkowicz et al., *Producing Wrong Data
  Without Doing Anything Obviously Wrong!*, ASPLOS 2009
  ([ACM](https://dl.acm.org/doi/10.1145/1508244.1508275)).
- Repetition design: Kalibera & Jones, *Rigorous Benchmarking in Reasonable Time*, ISMM
  2013 ([ACM](https://dl.acm.org/doi/10.1145/2464157.2464160)).
- Network shaping: toxiproxy toxics (latency+jitter ms, bandwidth KB/s, slicer,
  limit_data, reset_peer), a Windows binary and an HTTP API on :8474
  ([toxiproxy](https://github.com/Shopify/toxiproxy)).
- psutil on Windows, verified locally with psutil 7.2.1: `memory_info()` returns
  `rss == wset` (working set), `vms == pagefile == private`, plus `peak_wset`.
  `io_counters()` gives `write_bytes`.
- Public reference files, checked with a HEAD request on 2026-09-25:
  - `https://releases.ubuntu.com/24.04/*.iso`: ranges yes, SHA256SUMS published.
  - `https://proof.ovh.net/files/1Gb.dat`: ranges yes, ETag, no checksum, `Last-Modified`
    changes.
  - `http://speedtest.tele2.net/1GB.zip`: ranges yes, HTTP only.
  - `speed.cloudflare.com/__down`: no `Accept-Ranges`.
