# Article outlines

Shared rules for every article:

- Every number links to its raw `results/*.jsonl` and the run's `env.json`.
- Always state the policy (defaults / equalized), the trial count and the success rate.
- Name the competitor's version and build.
- Give the scenarios where MossDL loses or ties the same prominence as the ones it wins.
- Offer a "reproduce this" box with the exact runner commands.
- Say so when a result is server-bound or within noise.
- Do not claim anything listed under "Claims needing measurement" until the aggregate
  supports it with non-overlapping 95% CIs.

Section skeleton reused by every competitor article:

1. TL;DR: three bullet findings, taken from measured medians only.
2. Who each tool is for (honest positioning).
3. Test setup: hardware, network (LAN or loopback), versions, policy, link to METHODOLOGY.
4. Results by scenario, in the charts listed below.
5. Features that are not about speed (table from RESEARCH.md section 6).
6. Where the competitor is better.
7. Reproduce it yourself.
8. Changelog of the article (re-runs after new versions).

---

## 1. MossDL vs Internet Download Manager

- **Angle:** "The paid Windows classic vs a free, open-source newcomer." Both use
  dynamic re-segmentation with 8 connections by default, which makes this the purest
  engine-against-engine comparison.
- **Charts:**
  - goodput by scenario, equalized policy;
  - CPU s/GiB;
  - disk write amplification (IDM temp directory on the same volume vs another volume, as
    a sidebar);
  - resume-after-kill: re-downloaded bytes and correctness.
- **Must-state caveats:**
  - HTTP/2 is only covered by the public-CDN run.
  - IDM's HLS works through the browser, so `hls-vod` is "not comparable" rather than a
    loss for IDM.
  - IDM is paid (USD 24.95 lifetime).
- **Claims needing measurement:** "faster on throttled hosts", "lower CPU", "resumes
  without re-downloading" (MossDL's core currently re-sent 41% in the smoke run, so check
  this before claiming anything).

## 2. MossDL vs JDownloader 2

- **Angle:** The file-host specialists. Speed is secondary; the story is hosts, captchas
  and automation.
- **Sections:**
  - host capability matrix (manual, own uploads);
  - captcha handling approach (Clearcote browser vs OCR, services and browser);
  - out-of-the-box single-connection default (JD chunks=1) vs 8;
  - JVM memory footprint (`idle-footprint`, `peak_wset`).
- **Charts:**
  - goodput under `defaults` and `equalized` side by side (this shows how much of any gap
    is only the default setting);
  - idle RAM;
  - `many-100x10m` total time.
- **Claims needing measurement:** memory advantage, many-file throughput (MossDL's engine
  currently serialises small files on a new host, so fix or report it honestly).

## 3. MossDL vs Free Download Manager

- **Angle:** Free all-rounders. FDM has BitTorrent and MossDL does not. Say so up front.
- **Method note:** FDM needs one click per URL, so compare on **wire throughput** and
  leave total time out. Many-file scenarios were run through a manual import list (if
  done at all).
- **Charts:** wire throughput by scenario, CPU s/GiB, idle footprint.
- **Claims needing measurement:** everything speed-related. FDM's default connection
  count must be recorded from the install.

## 4. MossDL vs AB Download Manager

- **Angle:** Two modern open-source newcomers (Kotlin/Compose vs Tauri/Rust/Python). Both
  are free and cross-platform.
- **Charts:**
  - equalized goodput, where thread count comes from ABDM's settings;
  - idle RAM (JVM vs WebView2);
  - `filehost-strict` success rate (backoff behaviour).
- **Caveats:** ABDM's HLS requires its browser extension, so it is "not comparable" on
  `hls-vod`. The ABDM API has no status endpoint, so completion is read from disk (same
  for all file-based clients).

## 5. MossDL vs Motrix

- **Angle:** aria2 under the hood. Motrix defaults to 64 connections per file with 1 MiB
  minimum splits. Explain what that means for small files and for servers that cap
  connections (`filehost-strict`).
- **Versions:** Test stable 1.8.19. The v2 beta is noted but not benchmarked unless the
  article is explicitly about the beta.
- **Charts:**
  - defaults vs equalized goodput (the 64-vs-8 difference);
  - server-observed peak connections (how hard each client hits a host);
  - `filehost-strict` outcome;
  - aria2 (neutral reference) as a dotted baseline.
- **Claims needing measurement:** "friendlier to hosts", "equal speed with fewer
  connections".

## 6. Combined article: "Six download managers, one reproducible benchmark"

- **Structure:**
  1. Why most download-manager benchmarks are not trustworthy (vendor tests, single runs,
     unknown servers; cite RESEARCH.md section 7).
  2. Our method in one screen: controlled server, two policies, SHA-256, 5 interleaved
     trials, median + IQR, raw data public.
  3. Results matrix: rows are scenarios, columns are clients. Each cell shows median
     goodput or "not supported" / "failed x/5".
  4. Resource use: CPU s/GiB and peak RAM (small multiples).
  5. Robustness: resume-kill, flaky-drops, filehost-strict, no-range (pass/fail grid with
     overhead ratio).
  6. Features beyond speed: the table from RESEARCH.md.
  7. Where MossDL is not the best choice (torrents: FDM/Motrix; FTP: IDM/JD/FDM/Motrix;
     mature host coverage: JD).
  8. Reproduce it, and how to contribute an adapter.
- **Headline rule:** the headline sentence may only summarise the equalized-policy
  median on the LAN setup plus the robustness grid. Loopback-only or single-run results
  never make a headline.
