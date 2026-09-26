# Benchmark methodology: MossDL vs JDownloader 2, IDM, FDM, AB Download Manager, Motrix

Goal: numbers that a sceptical reader, or a competitor's developer, could reproduce and
would accept as fair. Rule zero: **no number is published unless it was measured by this
harness (or by the documented manual protocol) and its raw record is kept in `results/`.**

## 1. Principles

1. **Controlled first, real-world second.** Most scenarios use the harness server
   (`harness/server.py`). It serves deterministic, incompressible content with exact
   network conditions, so differences come from the clients. The public-CDN scenario
   corroborates the controlled results but is never the headline. This mirrors the
   repo's own `engine/benchmarking.py` ("fixture is release gate, live is corroborating only").
2. **Two policies, always labelled.**
   - `defaults`: each app exactly as installed. This is what users get, and connection
     defaults differ a lot: JD 1, MossDL and IDM 8, Motrix 64.
   - `equalized`: the same connections per file (8) and concurrent downloads (4) for
     everyone, so the comparison is engine against engine.

   Charts must say which policy they show. Never mix the two in one bar group.
3. **Correctness gates speed.** Each file's SHA-256 is compared with the expected value
   (computed from the generator, or published for public files). A trial whose output is
   wrong counts as failed and is excluded from speed statistics. The success rate is
   shown next to every median.
4. **What a feature a competitor lacks counts for.** Scenarios a client cannot run (IDM
   and a pasted HLS URL, AB DM and HLS, Motrix and HLS, file-host plugins) are recorded
   as `unsupported`, with the reason, and shown as "not supported" in the charts. They are
   **never** scored as zero speed and never silently omitted.
5. **The harness must not be the bottleneck.** Run `python -m harness.selftest` on the
   test machine. On the development machine (loopback, in-process Python server) it
   measured about **361 MiB/s with 1 connection, 488 MiB/s with 8, and 466 MiB/s with 32**
   for 1 GiB. Any result at 90% or more of the ceiling is *server-bound* and must be
   labelled that way. For headline "fast link" numbers, run the server on a second machine
   over wired gigabit (`python -m harness.server --host 0.0.0.0` on a trusted LAN) so the
   link, not the harness, is the cap.

## 2. Scenarios (`harness/scenarios.py`)

| Name | Payload | Server profile | What it shows |
|---|---|---|---|
| `lan-1g-single` | 1 x 1 GiB | unshaped | Client ceiling, CPU/RAM per GiB, disk behaviour (pre-allocation, temp-file assembly) |
| `many-100x10m` | 100 x 10 MiB | unshaped | Queueing, per-task overhead, concurrency policy. **Note:** MossDL does not segment files of 16 MiB or less |
| `tiny-1000x64k` | 1000 x 64 KiB | unshaped | Pure per-task overhead (optional, 3 trials) |
| `filehost-throttled` | 256 MiB | 1 MiB/s per connection, max 8 connections, 503 beyond | The classic file-host case: throughput scales with connections up to the cap |
| `filehost-strict` | 128 MiB | 1 MiB/s per connection, **max 2**, 503 + Retry-After beyond | Does the client back off and finish, or fail? |
| `high-latency` | 256 MiB | 150 +/- 20 ms before each response | Request-latency sensitivity. For true RTT, also run with a packet shaper (section 5) |
| `flaky-drops` | 256 MiB | every response cut after 16 MiB | Retry and range-resume inside a session |
| `no-range` | 256 MiB | Range ignored, not advertised | Correct fallback to a single stream |
| `resume-kill` | 512 MiB | 4 MiB/s per connection | Process tree hard-killed at 40% of bytes, relaunched. Metrics: correctness, re-downloaded bytes (`server.overhead_ratio`), time to finish |
| `hls-vod` | 300 x 1 MiB segments | unshaped | Playlist handling, byte-exact concatenation (synthetic segments, see section 7) |
| `public-cdn` | e.g. an Ubuntu ISO with its published SHA-256 | real internet, TLS | Corroboration only: interleaved, at least 5 rounds, time of day recorded |
| `idle-footprint` | none | none | App launched and idle for 60 s: background CPU and RAM of the full app (MossDL: `MossDL.exe` + WebView2 + engine + core) |
| `smoke` | 64 MiB + 10 x 4 MiB | unshaped | Harness self-check only. Never published |

File-host plugin coverage (MEGA, Gofile, and so on) is **not** a speed benchmark. It is a
capability matrix: for each host, can the client fetch a public test link without manual
steps? It is run by hand with the authors' own uploads, recorded as yes/no/manual, and
compared only with JDownloader, the one competitor with host plugins. Never benchmark
against third-party uploads or hammer real hosts.

## 3. Metrics (`harness/result.schema.json`)

| Metric | Definition | Source |
|---|---|---|
| Total time | `completed_at - submitted_at`. Submission is the moment the harness issues the add/start command to an already-running, idle client | runner clock |
| Goodput | payload bytes / total time | derived |
| TTFB | first payload byte sent by the server minus `submitted_at` | server, clock-offset corrected |
| Wire throughput | payload / (last byte - first byte), which excludes submission overhead. This is the fair metric for semi-manual clients such as FDM | server |
| CPU seconds, CPU s/GiB | user+system CPU of the client's whole process tree during the trial | psutil sampler, 250 ms |
| Peak RAM | peak sum of working sets over the process tree. `sum_process_peak_wset_bytes` is an upper bound | psutil |
| Disk write amplification | bytes the tree wrote / payload. This exposes temp-file assembly (IDM on another volume), pre-allocation, journals | psutil `io_counters` |
| Connections used | server-observed peak concurrent payload connections | server |
| Overhead ratio | bytes the server sent / payload - 1. This exposes overlapping ranges and restart-from-zero | server |
| Correctness | SHA-256 per file. Leftover temp files and unexpected files are listed | harness |

Completion is decided from evidence the harness controls, not from the app's UI:

- every file is present under its final name at the expected size;
- no temp sibling exists (`.part`, `.aria2`, `.crdownload`, ...);
- the harness server reports no active payload connections;
- the client's own status API, where one exists, agrees;
- sizes and mtimes stay unchanged for 0.5 s.

`completed_at` is the moment these first held.

## 4. Trials and statistics

- **1 warm-up round** per client, scenario and policy, recorded with `warmup: true` and
  excluded. JVM clients (JD, AB DM) especially need this.
- **5 measured rounds** by default (3 for `tiny-1000x64k` and `idle-footprint`). Raise
  this to 10 if the IQR is more than 10% of the median. That is the same spirit as the
  Phoronix dynamic run count.
- **Interleaving:** in each round the client order is shuffled with a fixed seed
  (`--seed`), so thermal drift, background activity and CDN time-of-day affect every
  client alike.
- **Reporting:** median, IQR, min/max, n_ok/n, and a seeded bootstrap 95% CI of the
  median (`harness/aggregate.py`). Means are not headlined.
- **Claims:** "A is faster than B" only when the two 95% CIs do not overlap. Otherwise
  say "within measurement noise".
- **Cold runs:** "first run after app launch" is a separate, clearly named variant. Record
  it by relaunching the app before a round, not by mixing it into warm results.

## 5. Environment and fairness rules

Recorded automatically (`results/<run>.env.json`, via `harness/env.py`, read-only): OS
build, CPU, core counts, RAM, destination volume and filesystem, disk model, power
scheme, Defender real-time state, NIC link speeds, CPU load at start, Python and psutil
versions, and all CLI arguments.

The operator must:

1. Use a machine that is otherwise idle: no browser, no sync clients, no Windows Update.
   Use AC power and the High performance plan, and record it.
2. **Antivirus:** real-time scanning inspects every written file and costs throughput
   unevenly. Either (a) run with the destination folder excluded, for all clients, or (b)
   leave Defender on for all clients. Record which. The harness never changes security
   settings; the operator does it by hand.
3. Use one destination volume for every client, an NVMe/SSD with at least 3x the payload
   free. Place IDM's temporary directory on the **same volume** (otherwise IDM pays for a
   cross-volume copy) and record it.
4. Turn off anything that is not downloading: auto-extract, virus-scan-after-download,
   completion popups and sounds, "open folder when done", speed limits, clipboard
   monitoring and scheduled queues.
5. Clear finished entries between trials (the adapters print a reminder where they
   cannot do it themselves). The harness deletes each destination between trials.
6. **MossDL-specific:** the engine persists learned per-host concurrency ceilings in
   `<install>/logs/host_concurrency_profiles.json`, outside the data dir. That state is
   recorded in each engine-mode record (`client.settings.learned_host_profile`). For
   fresh-install parity, start from a file without the test host and say so.
7. Do not run the benchmark against real file hosts or other people's servers at scale.
   The public-CDN scenario uses mirrors that publish speed-test or ISO files, and 5
   rounds at most.
8. Packet-level conditions (true RTT, loss) come from a shaper, not the harness:
   toxiproxy (latency/bandwidth toxics) or clumsy on Windows, or `tc netem` on a Linux
   router between two machines. Record the tool and its parameters.
9. HTTP/1.1 only in the controlled scenarios. IDM 6.43 advertises HTTP/2 improvements and
   several clients use TLS stacks with different costs, so HTTP/2 and TLS are covered only
   by the public-CDN scenario. Say so in the article.

## 6. Driving each client

| Client | Adapter | Submission | Completion | Status |
|---|---|---|---|---|
| MossDL | `mossdl` (`mode=core` or `engine`) | Child process: warm, idle, then "go" | Driver events + files | **Runnable now** |
| Motrix 1.8 | `motrix` | aria2 JSON-RPC `aria2.addUri` on :16800 | `tellStatus == complete` + files | Code ready; needs Motrix installed |
| aria2 (neutral reference) | `aria2` | aria2 JSON-RPC :6800 | same | Needs `aria2c --enable-rpc` |
| JDownloader 2 | `jdownloader` | `.crawljob` JSON into `folderwatch/` | Files (`.part` renamed at the end) | Needs JD + Folder Watch; local API optional |
| IDM | `idm` | `IDMan.exe /n /d URL /p DIR /f NAME` (`/a`... then `/s` for many files) | Files (moved from DwnlData at the end) | Needs IDM; settings by hand |
| AB DM | `abdm` | `POST :15151/start-headless-download` | Files | Needs AB DM running |
| FDM 6 | `fdm` | `fdm.exe --url URL` + **one click per URL** | Files | Semi-manual; compare on wire throughput |

"Settings by hand" means the operator sets connections and concurrency in the app and
passes `--opt <client>.confirmed=1`. Records without that option carry
`confirmed_by: UNCONFIRMED` and must not be published under the `equalized` policy.

MossDL modes:

- **core** measures the shipped Rust core through `RustTransferBackend`, with production
  defaults (8 connections, 16 MiB minimum segment, 6 concurrent).
- **engine** runs a private `EngineService` (queueing, plugin resolution, host
  admission) exactly as the UI does.

Neither mode includes the WebView UI. The UI's cost is measured by `idle-footprint`
against the installed app. **Publish engine-mode numbers as "MossDL". Core-mode numbers
are an engineering reference, labelled "MossDL transfer core".**

## 7. Scenario caveats to state in every write-up

- `hls-vod` segments are synthetic bytes, not real MPEG-TS. That is fine for clients that
  concatenate, but a client that remuxes to MP4 will not match byte for byte. For those
  clients, serve a real ffmpeg-generated HLS folder with `--static-dir` and check
  duration and stream count with ffprobe instead of SHA-256.
- `high-latency` adds per-request delay only (section 5.8).
- The harness server is Python. Treat results near the self-test ceiling as server-bound.
- Loopback is not a network: no NIC offload, no real congestion. The LAN setup is the
  one to quote for "fast link" claims.

## 8. Running it

From `docs/benchmarks` (Python 3.10+, `psutil`). **psutil is not in the repo's
`requirements.txt`; add it there or install it in a separate venv. The harness does not
install anything.**

```text
python -m harness.selftest                                   # server correctness + ceiling
python -m harness.runner --scenario smoke --client mossdl    # 1-minute smoke
python -m harness.runner --scenario lan-1g-single --scenario many-100x10m --scenario filehost-throttled \
    --client mossdl --opt mossdl.mode=engine --client motrix --client jdownloader \
    --opt jdownloader.jd_home="C:/Users/<you>/AppData/Local/JDownloader 2.0" --opt jdownloader.confirmed=1 \
    --policy both
python -m harness.runner --scenario public-cdn --client mossdl --public-url <iso-url> \
    --public-sha256 <hex> --public-size <bytes>
python -m harness.aggregate "results/*.jsonl" --csv results/summary.csv --md results/summary.md
```

LAN setup: on the server machine run `python -m harness.server --host 0.0.0.0 --port
8765 [--conn-rate ...]`, then run the runner with `--server-url http://<server-ip>:8765`.
The server's profile is set by its own flags, so start one server per shaped scenario.

## 9. Smoke run status (2026-09-26)

`results/smoke-20260926.jsonl` holds single-trial runs of MossDL core mode on the
development machine (loopback). **These are harness validation runs, not benchmark
results, and must not be quoted.** They exercised every code path and showed:

- `filehost-strict` fails in core mode (503 cap);
- `resume-kill` finished correctly but the server sent 1.41x the payload.

Both are filed for investigation (RESEARCH.md section 0).
