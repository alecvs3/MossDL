"""AB Download Manager adapter via its local integration REST API.

From REST-API.yml in the project repository (see RESEARCH.md):
  GET  http://localhost:15151/queues                 -> [{id, name}]
  POST http://localhost:15151/start-headless-download
       {downloadSource: {link, headers, downloadPage}, folder, name, queueId}
  POST http://localhost:15151/add                    -> opens the add dialog
The port is configurable in the app (default 15151). v1.10.3 restricted the
integration server to local connections.

Completion is read from disk. The per-download thread count is a global
setting (Settings -> Download Engine -> thread count); its default/maximum is
not documented publicly -- record what your install shows.

TODO(user), once:
  1. Install AB Download Manager from GitHub releases or abdownloadmanager.com.
  2. Keep "browser integration" enabled (that is what starts the server).
  3. Set thread count (equalized: 8) and max concurrent downloads for the
     queue; confirm with ``--opt confirmed=1``.
  4. Disable completion popups; set duplicate handling to overwrite.
"""

from __future__ import annotations

import json
import urllib.request

from .base import Adapter, Job, Unavailable


class ABDMAdapter(Adapter):
    name = "abdm"
    display_name = "AB Download Manager"
    automation = "api"
    process_names = ["ABDownloadManager.exe"]  # TODO(user): verify the image name in Task Manager

    def __init__(self, options: dict | None = None) -> None:
        super().__init__(options)
        self.base = self.options.get("api", "http://127.0.0.1:15151")

    def _request(self, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data,
                                         headers={"Content-Type": "application/json"},
                                         method="POST" if data is not None else "GET")
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode("utf-8", "replace")

    def check(self) -> None:
        try:
            self._request("/queues")
        except Exception as exc:
            raise Unavailable(f"AB Download Manager API not reachable at {self.base} ({exc})") from exc

    def version(self) -> str | None:
        return self.options.get("version", "TODO: record ABDM version from About")

    def unsupported_reason(self, job: Job) -> str | None:
        if job.kind == "hls":
            return "ABDM handles HLS only when captured by its browser extension, not as a pasted .m3u8 (issue #1138)"
        return None

    def applied_settings(self, job: Job) -> dict:
        return {"thread_count": job.connections if job.policy == "equalized" else "default",
                "max_concurrent": job.max_concurrent if job.policy == "equalized" else "default",
                "confirmed_by": "operator" if self.options.get("confirmed") else "UNCONFIRMED"}

    def start(self, job: Job) -> None:
        queues = self._request("/queues")
        queue_id = int(self.options.get("queue_id", queues[0]["id"] if queues else 0))
        for spec in job.files:
            reply = self._request("/start-headless-download", {
                "downloadSource": {"link": spec.url, "headers": {}, "downloadPage": None},
                "folder": str(job.dest), "name": spec.name, "queueId": queue_id})
            if isinstance(reply, str) and reply.strip().upper() not in ("OK", ""):
                raise RuntimeError(f"ABDM refused {spec.name}: {reply[:200]}")

    def relaunch(self, job: Job) -> None:
        print("[abdm] Relaunch AB Download Manager and resume the unfinished download.", flush=True)

    def cleanup(self) -> None:
        print("[abdm] Remove finished downloads from the list before the next trial.", flush=True)
