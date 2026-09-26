from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable

from .captcha import CaptchaChallenge, CaptchaSolver, CaptchaType
from .challenge_artifacts import ANSWER_TEXT
from .challenge_routing import IMAGE_INPUTS, Health, SolverCapability

logger = logging.getLogger(__name__)

_SUPPORTED_HOSTS = {
    "keep2share.cc", "k2s.cc", "fileboom.me", "fboom.me",
    "filejoker.net", "tezfiles.com", "publish2.me", "depositfiles.com"
}


class DarknetYoloSolver(CaptchaSolver):
    """
    Tier 1 Native Offline Neural Net Solver.
    Uses cloned YOLOv4-tiny weights from JDownloader 2 CaptchaSolver package
    to solve 6-digit alphanumeric and geometric captchas in under 100ms.
    """

    def __init__(
        self,
        solver_id: str = "darknet_yolo",
        priority: int = 15,
        enabled: bool = True,
        tools_dir: Path | str | None = None,
    ) -> None:
        super().__init__(solver_id=solver_id, name="Darknet YOLOv4-Tiny (Offline)", priority=priority, enabled=enabled)
        if tools_dir is None:
            self.darknet_dir = Path(__file__).resolve().parents[1] / "clone_reference" / "CaptchaSolver" / "JDownloader 2.0" / "tools" / "offlineCaptchaSolver" / "darknet64"
        else:
            self.darknet_dir = Path(tools_dir)

        self.mock_evaluator: Callable[[bytes], str] | None = None  # Hook for test environments

    def capability(self) -> SolverCapability:
        # Neural OCR: measured in shadow, never submitted, until an approved gate.
        return SolverCapability(frozenset({CaptchaType.IMAGE_TEXT.value}), "text", ANSWER_TEXT,
                                inputs={"*": (IMAGE_INPUTS,)}, production=False)

    def health(self) -> Health:
        if self.mock_evaluator is not None or self.darknet_dir.is_dir():
            return Health(True)
        return Health(False, "YOLO model not installed")

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        if not self.enabled:
            return False
        if challenge.captcha_type != CaptchaType.IMAGE_TEXT:
            return False

        # Provider match or explicit image payload
        p_id = (challenge.provider_id or "").lower()
        is_supported_host = any(h in p_id for h in _SUPPORTED_HOSTS)
        has_image = bool(challenge.params.get("image_base64") or challenge.params.get("image_data") or challenge.params.get("image_path"))
        return is_supported_host or has_image

    async def solve(self, challenge: CaptchaChallenge) -> dict[str, Any] | None:
        params = challenge.params
        image_bytes: bytes | None = None

        if "image_base64" in params:
            try:
                image_bytes = base64.b64decode(params["image_base64"])
            except Exception as exc:
                logger.warning("Failed decoding base64 image: %s", exc)
        elif "image_data" in params:
            image_bytes = params["image_data"]
        elif "image_path" in params and Path(params["image_path"]).is_file():
            image_bytes = Path(params["image_path"]).read_bytes()

        if not image_bytes:
            return None

        # If mock hook is active:
        if self.mock_evaluator:
            res = self.mock_evaluator(image_bytes)
            if res:
                return {"text": res, "confidence": 0.95, "method": "darknet_yolo"}
            return None

        # Execute native Darknet binary (subprocess offloaded off the event loop)
        return await asyncio.to_thread(self._run_darknet_inference, image_bytes, challenge.provider_id)

    def _run_darknet_inference(self, image_bytes: bytes, provider_id: str) -> dict[str, Any] | None:
        exe_name = "darknet_no_gpu.exe" if sys.platform == "win32" else "darknet"
        darknet_bin = self.darknet_dir / exe_name
        cfg_file = self.darknet_dir / "yolov4-tiny-custom.cfg"
        weights_file = self.darknet_dir / "yolov4-tiny-custom_last.weights"
        data_file = self.darknet_dir / "data" / "obj.data"

        if not darknet_bin.is_file() or not cfg_file.is_file() or not weights_file.is_file():
            logger.info("Darknet binaries or weights not found at %s; skipping offline YOLO solver", self.darknet_dir)
            return None

        with NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(image_bytes)
            tmp_path = Path(tmp.name)

        try:
            cmd = [
                str(darknet_bin),
                "detector", "test",
                str(data_file),
                str(cfg_file),
                str(weights_file),
                "-dont_show",
                str(tmp_path),
            ]
            proc = subprocess.run(
                cmd,
                cwd=str(self.darknet_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
            return self.parse_darknet_output(proc.stdout)
        except Exception as exc:
            logger.warning("Darknet YOLO execution error: %s", exc)
            return None
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    @classmethod
    def parse_darknet_output(cls, output_text: str) -> dict[str, Any] | None:
        """
        Parses Darknet console output:
          e: 99%
          h: 74%
          C: 100%
        Applies JDownloader 'I' ambiguity filtering and takes the top 6 predictions.
        """
        detections: list[dict[str, Any]] = []
        for line in output_text.splitlines():
            line = line.strip()
            if ":" in line and "%" in line:
                parts = line.split(":")
                char = parts[0].strip()
                conf_str = parts[1].replace("%", "").strip()
                try:
                    conf = float(conf_str)
                    detections.append({"char": char, "conf": conf})
                except ValueError:
                    pass

        if not detections:
            return None

        # Remove "I" because capital "I" and lowercase "l" collide in this font (from ocr.js)
        detections = [d for d in detections if d["char"] != "I"]

        # Keep top 6 highest confidence characters
        if len(detections) > 6:
            detections.sort(key=lambda d: d["conf"], reverse=True)
            detections = detections[:6]

        solved_text = "".join(d["char"] for d in detections)
        avg_conf = sum(d["conf"] for d in detections) / max(len(detections), 1) / 100.0

        return {
            "text": solved_text,
            "confidence": round(avg_conf, 3),
            "method": "darknet_yolo",
            "characters": detections,
        }
