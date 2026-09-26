"""JDownloader 2 adapter: Folder Watch (.crawljob) submission + optional local API.

Submission (documented by JDownloader support, see RESEARCH.md):
  Folder Watch extension watches ``<JD install>/folderwatch`` every 1000 ms and
  accepts JSON crawljobs with fields such as text, downloadFolder, filename,
  chunks, autoStart, forcedStart, autoConfirm, enabled, packageName. Processed
  jobs are moved to ``folderwatch/added``.

Status (optional): the "Deprecated API" (Advanced Settings -> RemoteAPI ->
deprecatedapienabled=true) listens on http://127.0.0.1:3128 and documents
itself at /help. The adapter uses it only for cleanup; completion is decided
from the filesystem (JD writes ``<name>.part`` and renames on completion).

TODO(user), once:
  1. Install JDownloader 2 from jdownloader.org (not a third-party repack).
  2. Settings -> Extensions -> enable Folder Watch.
  3. Settings -> General: Max. Chunks per Download / Max. simultaneous downloads
     (defaults 1 and 3 per GeneralSettings.java). For "equalized" runs the
     crawljob ``chunks`` field sets per-link chunks; set simultaneous downloads
     in the UI and confirm with ``--opt confirmed=1``.
  4. Disable: auto-extract, hash check notification popups, "Linkgrabber
     auto-start" delays if any; set "If file exists" to overwrite.
  5. Pass ``--opt jd_home=C:/Users/<you>/AppData/Local/JDownloader 2.0``.
Processes are sampled by name (JDownloader2.exe launcher + javaw.exe/java.exe).
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .base import Adapter, Job, Unavailable


class JDownloaderAdapter(Adapter):
    name = "jdownloader"
    display_name = "JDownloader 2"
    automation = "folderwatch"
    process_names = ["JDownloader2.exe", "javaw.exe", "java.exe"]

    def __init__(self, options: dict | None = None) -> None:
        super().__init__(options)
        self.home = Path(self.options["jd_home"]) if self.options.get("jd_home") else None
        self.api = self.options.get("api", "http://127.0.0.1:3128")

    def _watch_dir(self) -> Path:
        assert self.home is not None
        return Path(self.options.get("watch_dir") or self.home / "folderwatch")

    def check(self) -> None:
        if not self.home or not self.home.is_dir():
            raise Unavailable("JDownloader 2 not configured: pass --opt jd_home=<install dir>")
        if not self._watch_dir().is_dir():
            raise Unavailable(f"Folder Watch dir {self._watch_dir()} missing: enable the Folder Watch extension")

    def version(self) -> str | None:
        return self.options.get("version", "TODO: record JD build (Help -> About -> revision)")

    def unsupported_reason(self, job: Job) -> str | None:
        return None  # HLS via the GenericM3u8 plugin; verify it needs no ffmpeg prompt on first use

    def applied_settings(self, job: Job) -> dict:
        settings = {"chunks": job.connections if job.policy == "equalized" else "default (1)",
                    "max_simultaneous": job.max_concurrent if job.policy == "equalized" else "default (3)"}
        settings["confirmed_by"] = "operator" if self.options.get("confirmed") else "UNCONFIRMED"
        return settings

    def start(self, job: Job) -> None:
        jobs = []
        for spec in job.files:
            entry = {"text": spec.url, "downloadFolder": str(job.dest), "filename": spec.name,
                     "packageName": f"bench-{job.dest.name}", "enabled": "TRUE", "autoStart": "TRUE",
                     "forcedStart": "TRUE", "autoConfirm": "TRUE", "overwritePackagizerEnabled": True}
            if job.policy == "equalized" and job.connections:
                entry["chunks"] = job.connections
            jobs.append(entry)
        name = f"bench-{int(time.time() * 1000)}.crawljob"
        staging = self._watch_dir() / (name + ".tmp")
        staging.write_text(json.dumps(jobs), encoding="utf-8")
        staging.replace(self._watch_dir() / name)  # atomic: JD never sees a half-written job

    def relaunch(self, job: Job) -> None:
        print("[jdownloader] Relaunch JDownloader 2; it restores its download list and resumes"
              " unfinished links on start if 'auto start downloads' is enabled.", flush=True)

    def _api(self, path: str, query: dict | None = None):
        url = self.api + path + ("?" + urllib.parse.quote(json.dumps(query)) if query is not None else "")
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read() or b"null")

    def cleanup(self) -> None:
        # TODO(user): confirm the removeLinks(linkIds, packageIds) call shape at
        # http://127.0.0.1:3128/help, then automate removal here. Until then the
        # finished-link count is printed and the list is cleared by hand.
        try:
            links = self._api("/downloadsV2/queryLinks", {"finished": True, "bytesTotal": True})
            count = len(links.get("data", [])) if isinstance(links, dict) else len(links or [])
            print(f"[jdownloader] {count} finished link(s) in the list; clear them before the next trial.")
        except Exception as exc:
            print(f"[jdownloader] local API unavailable ({type(exc).__name__}: {exc});"
                  " clear the download list manually between trials.", flush=True)
