# README results graphic: specification

Input: `results/summary.csv` from `python -m harness.aggregate`. The graphic is generated
from that file by a script, never drawn by hand, so every mark traces to a record.

## Layout (one SVG, about 1200 x 900, readable in light and dark README themes)

1. **Panel A: Goodput, equalized policy, LAN setup.**
   - Horizontal dot-and-interval plot: one row per scenario (`lan-1g-single`,
     `many-100x10m`, `filehost-throttled`, `high-latency`, `flaky-drops`) and one
     colour per client.
   - Dot = median. Whisker = IQR. Faint dots = individual trials.
   - Axis in MiB/s starting at **0**, linear.
   - A labelled vertical line marks the link ceiling (e.g. "1 GbE = 112 MiB/s") and the
     harness ceiling if it is lower.
   - Clients that are unsupported or failed in a row get a grey text label ("not
     supported", "failed 3/5") instead of a dot.
2. **Panel B: Robustness grid.**
   - Rows: `filehost-strict`, `resume-kill`, `flaky-drops`, `no-range`, `hls-vod`.
     Columns: clients.
   - Cell: a check or cross with `n_ok/n`, plus the overhead ratio for `resume-kill`
     (e.g. "+3% re-sent").
   - Use text and shape, not colour alone.
3. **Panel C: Resources, `lan-1g-single`.**
   - Two small bar charts: CPU seconds per GiB and peak RAM (MiB). Bars start at 0.
   - Error ticks show the IQR.
   - A second, lighter bar per client gives the idle-footprint RAM, so a light engine with
     a heavy UI (or the reverse) is visible.

## Labelling rules

- Title states the policy and setup: "Equalized: 8 connections/file, 4 concurrent - wired 1 GbE LAN - 5 runs each".
- Subtitle: "median and interquartile range; SHA-256 verified; raw data: docs/benchmarks/results".
- Footer: the client versions/builds, test date, machine summary (CPU, disk), and "defaults-policy results: see link".
- MossDL gets no highlight colour, bold or ordering advantage. Clients are sorted
  alphabetically, or by median within each row, applied the same way to every client.
- Never use truncated axes, 3-D, area-scaled icons, or "x times faster" callouts unless
  the ratio's 95% CI excludes 1.
- If the data came from loopback, the title must say "loopback", and the graphic is not
  used in the README.
- Colour: one categorical palette, checked for colour-vision deficiency, with each series
  also labelled directly (no legend-only identification).

## Data contract (columns consumed from summary.csv)

`scenario, client, policy, mode, version, n, n_ok, statuses, goodput_mib_s_median,
goodput_mib_s_q1, goodput_mib_s_q3, cpu_s_per_gib_median, cpu_s_per_gib_q1,
cpu_s_per_gib_q3, peak_wset_mib_median, peak_wset_mib_q1, peak_wset_mib_q3,
server_overhead_ratio_median`. Per-trial dots come from the JSONL
(`throughput.goodput_bytes_per_s`, `warmup == false`, `status == "ok"`).

## Not allowed on the graphic

- Any number from the smoke runs, from a single trial, or from `mossdl` core mode
  labelled as "MossDL". Core mode may appear only as "MossDL transfer core (engineering
  reference)".
- Features comparison ticks for anything marked **Unknown** or **3rd-party** in RESEARCH.md.
