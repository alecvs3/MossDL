"""Free Download Manager 6 adapter -- semi-manual.

What exists (FDM team on the official forum, see RESEARCH.md):
  fdm.exe --url URL     add a download (opens FDM's "new download" dialog)
  fdm.exe --hidden / --minimized
There is no documented switch for a save path or file name and no documented
silent add, so every submission needs one click in FDM's dialog. FDM's
"Advanced -> Automation" settings can run a command after a download
completes; it is not needed here because completion is read from disk.

Timing consequence: the clock starts when the harness issues the command, so
the dialog click is included. The adapter therefore records
``submission: manual-dialog`` and the runner reports FDM's TTFB separately
from its wire throughput (server first-byte -> last-byte), which excludes the
human click. Compare FDM on wire throughput, not total time.

TODO(user), once:
  1. Install FDM 6 from freedownloadmanager.org (verify the installer
     signature; the site served a compromised Linux script in 2020-2022).
  2. Settings: default download folder = the harness destination for the
     trial (the runner prints it), "when file exists" = overwrite/rename,
     disable completion notifications, traffic mode = High / unlimited.
  3. Record the per-download connection setting you used (``--opt
     connections_confirmed=N``); FDM's default is not documented publicly.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .base import Adapter, Job, Unavailable

DEFAULT_FDM = Path(r"C:\Program Files\Softdeluxe\Free Download Manager\fdm.exe")


class FDMAdapter(Adapter):
    name = "fdm"
    display_name = "Free Download Manager"
    automation = "manual"
    process_names = ["fdm.exe"]

    def __init__(self, options: dict | None = None) -> None:
        super().__init__(options)
        self.exe = Path(self.options.get("fdm") or DEFAULT_FDM)

    def check(self) -> None:
        if not self.exe.is_file():
            raise Unavailable(f"fdm.exe not found at {self.exe}; pass --opt fdm=<path>")

    def version(self) -> str | None:
        return self.options.get("version", "TODO: record FDM version from About")

    def unsupported_reason(self, job: Job) -> str | None:
        if len(job.files) > 20:
            return "FDM needs one dialog confirmation per URL; use an import list manually for many-file runs"
        if job.kind == "hls":
            return "HLS support not verified for FDM 6 -- run manually if it exists"
        return None

    def applied_settings(self, job: Job) -> dict:
        return {"connections_per_download": self.options.get("connections_confirmed", "UNCONFIRMED"),
                "submission": "manual-dialog", "confirmed_by": "operator"}

    def start(self, job: Job) -> None:
        print(f"[fdm] Confirm each dialog; save to: {job.dest}", flush=True)
        for spec in job.files:
            subprocess.Popen([str(self.exe), "--url", spec.url])

    def relaunch(self, job: Job) -> None:
        print("[fdm] Relaunch FDM and resume the unfinished download.", flush=True)

    def cleanup(self) -> None:
        print("[fdm] Remove finished downloads from the list before the next trial.", flush=True)
