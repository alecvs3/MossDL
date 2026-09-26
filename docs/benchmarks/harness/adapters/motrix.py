"""Motrix adapter via the bundled aria2 JSON-RPC interface.

Facts this relies on (see RESEARCH.md for sources):
  * Motrix 1.x starts a bundled aria2c with RPC on port 16800 (ENGINE_RPC_PORT)
    and, by default, split = max-connection-per-server = 64 (a patched aria2;
    upstream caps max-connection-per-server at 16) and min-split-size=1M.
  * The RPC secret defaults to empty; if you set one in Motrix's Advanced
    preferences pass it as ``--opt secret=...``.
  * Motrix 2 (beta, "Motrix Turbo") still bundles an aria2 fork but fronts it
    with MDXP (JSON-RPC 2.0, port 16801 in Docker). Whether raw aria2 RPC stays
    reachable in v2 is UNVERIFIED -- use Motrix 1.8.x stable for this adapter,
    or verify and set ``--opt port=...``.

The same adapter drives a plain ``aria2c --enable-rpc`` as a neutral reference
(``--client aria2``); install aria2 yourself, the harness never downloads it.

TODO(user): install Motrix; start it; confirm RPC port/secret in
Preferences > Advanced; disable "notify on completion"; set the download
folder is NOT needed (the adapter passes ``dir`` per task).
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid

from .base import Adapter, Job, Unavailable


class Aria2RpcAdapter(Adapter):
    name = "aria2"
    display_name = "aria2 (reference)"
    automation = "api"
    process_names = ["aria2c.exe"]
    default_port = 6800

    def __init__(self, options: dict | None = None) -> None:
        super().__init__(options)
        self.url = f"http://127.0.0.1:{int(self.options.get('port', self.default_port))}/jsonrpc"
        self.secret = self.options.get("secret", "")
        self.gids: dict[str, str] = {}

    def _call(self, method: str, *params):
        args = ([f"token:{self.secret}"] if self.secret else []) + list(params)
        body = json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": args}).encode()
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            reply = json.loads(response.read())
        if "error" in reply:
            raise RuntimeError(f"aria2 {method}: {reply['error']}")
        return reply["result"]

    def version(self) -> str | None:
        try:
            return "aria2 " + self._call("aria2.getVersion")["version"]
        except Exception as exc:  # reported, not hidden: version is part of the record
            return f"unknown ({type(exc).__name__})"

    def check(self) -> None:
        try:
            self._call("aria2.getVersion")
        except Exception as exc:
            raise Unavailable(f"{self.display_name}: RPC not reachable at {self.url} ({exc})") from exc

    def unsupported_reason(self, job: Job) -> str | None:
        if job.kind == "hls":
            return "aria2 has no HLS playlist support"
        return None

    def applied_settings(self, job: Job) -> dict:
        if job.policy == "equalized":
            return {"split": job.connections, "max-connection-per-server": job.connections,
                    "max-concurrent-downloads": job.max_concurrent, "confirmed_by": "adapter"}
        return {"note": "client defaults (Motrix 1.x: split=64, max-connection-per-server=64, min-split-size=1M)",
                "confirmed_by": "defaults"}

    def start(self, job: Job) -> None:
        self.gids = {}
        if job.policy == "equalized" and job.max_concurrent:
            self._call("aria2.changeGlobalOption", {"max-concurrent-downloads": str(job.max_concurrent)})
        for spec in job.files:
            options = {"dir": str(job.dest), "out": spec.name}
            if job.policy == "equalized" and job.connections:
                options.update({"split": str(job.connections), "max-connection-per-server": str(job.connections)})
            self.gids[self._call("aria2.addUri", [spec.url], options)] = spec.name

    def relaunch(self, job: Job) -> None:
        # aria2 resumes from its .aria2 control file when the same file is re-added.
        deadline = time.time() + 60
        while True:
            try:
                self._call("aria2.getVersion")
                break
            except Exception:
                if time.time() > deadline:
                    raise Unavailable("relaunch the client manually; RPC did not come back within 60 s")
                time.sleep(1)
        self.start(job)

    def poll(self) -> dict | None:
        failed = []
        done = 0
        for gid, name in self.gids.items():
            status = self._call("aria2.tellStatus", gid, ["status", "errorMessage"])
            if status["status"] == "complete":
                done += 1
            elif status["status"] in ("error", "removed"):
                failed.append(f"{name}: {status.get('errorMessage')}")
        if failed:
            return {"failed": "; ".join(failed)[:800]}
        return {"done": done == len(self.gids)}

    def cleanup(self) -> None:
        try:
            self._call("aria2.purgeDownloadResult")
        except Exception as exc:
            print(f"[aria2] purgeDownloadResult failed: {exc}")


class MotrixAdapter(Aria2RpcAdapter):
    name = "motrix"
    display_name = "Motrix"
    process_names = ["Motrix.exe", "aria2c.exe"]
    default_port = 16800

    def version(self) -> str | None:
        engine = super().version()
        app = self.options.get("version", "TODO: record Motrix version from About")
        return f"{app} / {engine}"

    def relaunch(self, job: Job) -> None:
        print("[motrix] Relaunch Motrix now (Start menu). Waiting for its aria2 RPC...", flush=True)
        super().relaunch(job)
