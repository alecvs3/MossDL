"""Internet Download Manager adapter via IDMan.exe command-line switches.

Documented switches (internetdownloadmanager.com/support/command_line.html):
  /d URL   download a file        /p path   local save path
  /f name  local file name        /n        silent mode (no questions)
  /a       add to queue, don't start       /s  start the main queue
  /q       exit IDM after successful download   /h  hang up after download
/a /h /n /q /f /p only work together with /d.

Completion: IDM keeps parts in its temporary directory (default
%APPDATA%\\IDM\\DwnlData\\<user>) and moves/assembles the final file into /p
at the end, so the final name appearing at full size means done. Put the IDM
temp directory on the SAME volume as the benchmark destination, otherwise the
final copy is timed as part of the download (record which you used).

TODO(user), once:
  1. Install IDM from internetdownloadmanager.com (trial is 30 days; a paid key
     for longer campaigns). Never use activation "scripts".
  2. Options -> Connection -> "Default max. conn. number": 8 for equalized
     runs (IDM's default is reported as 8 by third parties; verify on your
     install and record it). Confirm with ``--opt confirmed=1``.
  3. Options -> Downloads: disable "Show download complete dialog" and the
     start-download dialog; set duplicate-name handling to overwrite/rename.
  4. Options -> Downloads -> Virus checking: disabled (record it).
  5. Main queue: "Download N files at the same time" = equalized max_concurrent.
  6. Pass ``--opt idman=C:/Program Files (x86)/Internet Download Manager/IDMan.exe``.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .base import Adapter, Job, Unavailable

DEFAULT_IDMAN = Path(r"C:\Program Files (x86)\Internet Download Manager\IDMan.exe")


class IDMAdapter(Adapter):
    name = "idm"
    display_name = "Internet Download Manager"
    automation = "cli"
    # TODO(user): verify the resident image names in Task Manager and adjust.
    process_names = ["IDMan.exe", "IEMonitor.exe"]

    def __init__(self, options: dict | None = None) -> None:
        super().__init__(options)
        self.exe = Path(self.options.get("idman") or DEFAULT_IDMAN)

    def check(self) -> None:
        if not self.exe.is_file():
            raise Unavailable(f"IDMan.exe not found at {self.exe}; pass --opt idman=<path>")

    def version(self) -> str | None:
        return self.options.get("version", "TODO: record IDM version/build from Help -> About")

    def unsupported_reason(self, job: Job) -> str | None:
        if job.kind == "hls":
            return "IDM captures HLS through its browser integration; a bare .m3u8 via /d is untested -- run manually"
        return None

    def applied_settings(self, job: Job) -> dict:
        return {"max_conn_number": job.connections if job.policy == "equalized" else "default",
                "queue_concurrency": job.max_concurrent if job.policy == "equalized" else "default",
                "confirmed_by": "operator" if self.options.get("confirmed") else "UNCONFIRMED"}

    def _run(self, *args: str) -> None:
        # IDMan.exe hands the request to the running IDM instance and returns.
        subprocess.run([str(self.exe), *args], check=True, timeout=60)

    def start(self, job: Job) -> None:
        job.dest.mkdir(parents=True, exist_ok=True)
        if len(job.files) == 1:
            spec = job.files[0]
            self._run("/n", "/d", spec.url, "/p", str(job.dest), "/f", spec.name)
            return
        for spec in job.files:  # queue everything, then start the main queue once
            self._run("/n", "/a", "/d", spec.url, "/p", str(job.dest), "/f", spec.name)
            time.sleep(0.05)
        self._run("/s")

    def relaunch(self, job: Job) -> None:
        print("[idm] Relaunch IDM, select the unfinished download(s) and press Resume."
              " The harness keeps timing until the files are complete.", flush=True)

    def cleanup(self) -> None:
        print("[idm] Remove completed entries (Tasks -> Delete completed) before the next trial.", flush=True)
