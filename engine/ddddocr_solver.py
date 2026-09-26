from __future__ import annotations

import asyncio
import base64
import importlib
import io
import logging
import os
import re
from pathlib import Path
from typing import Any

from .captcha import CaptchaChallenge, CaptchaSolver, CaptchaType
from .challenge_artifacts import ANSWER_TEXT
from .challenge_routing import IMAGE_INPUTS, Health, SolverCapability

logger = logging.getLogger(__name__)

_MATH_RE = re.compile(
    r"(\d+)\s*([\+\-\*\/xX]|plus|minus|times)\s*(\d+)",
    re.IGNORECASE
)


class DdddOcrSolver(CaptchaSolver):
    """
    Tier 1.5: Deep-learning Discrete Digit/Letter OCR Solver (analogous to sml2h3/ddddocr).
    Provides offline captcha recognition for alphanumeric images and math challenges.
    Uses ONNX Runtime when available; falls back gracefully when onnxruntime is absent.
    """

    def __init__(
        self,
        solver_id: str = "ddddocr",
        priority: int = 25,
        enabled: bool = True,
        model_path: str | Path | None = None,
        charset_path: str | Path | None = None,
    ) -> None:
        super().__init__(solver_id=solver_id, name="ddddocr Offline Neural OCR", priority=priority, enabled=enabled)
        self.model_path = Path(model_path) if model_path else self._discover_model_path()
        self.charset_path = Path(charset_path) if charset_path else self._discover_charset_path()
        self._session: Any = None
        self._charset: list[str] | None = None
        self._initialized = False

    def _discover_model_path(self) -> Path | None:
        candidates = [
            Path(__file__).resolve().parents[1] / "clone_reference" / "ddddocr" / "ddddocr" / "common.onnx",
            Path(__file__).resolve().parents[1] / "clone_reference" / "ddddocr" / "ddddocr" / "common_old.onnx",
            Path.home() / ".transfer_manager" / "models" / "ddddocr" / "common.onnx",
        ]
        for c in candidates:
            if c.is_file():
                return c
        return None

    def _discover_charset_path(self) -> Path | None:
        candidates = [
            Path(__file__).resolve().parents[1] / "clone_reference" / "ddddocr" / "ddddocr" / "charsets.py",
            Path.home() / ".transfer_manager" / "models" / "ddddocr" / "charsets.py",
        ]
        for c in candidates:
            if c.is_file():
                return c
        return None

    def is_model_available(self) -> bool:
        return self.model_path is not None and self.model_path.is_file()

    def _ensure_session(self) -> bool:
        if self._initialized:
            return self._session is not None

        self._initialized = True
        if not self.is_model_available():
            logger.info("ddddocr model not found on disk, running in lightweight heuristic mode")
            return False

        try:
            import onnxruntime as ort  # type: ignore
            sess_options = ort.SessionOptions()
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_options.intra_op_num_threads = 2
            self._session = ort.InferenceSession(str(self.model_path), sess_options, providers=["CPUExecutionProvider"])
            self._load_charset()
            logger.info("ddddocr ONNX session initialized successfully with %s", self.model_path)
            return True
        except ImportError:
            logger.warning("onnxruntime is not installed. ddddocr will operate in heuristic/fallback mode.")
            return False
        except Exception as exc:
            logger.warning("Failed to initialize ddddocr ONNX session: %s", exc)
            return False

    def _load_charset(self) -> None:
        if self.charset_path and self.charset_path.is_file():
            try:
                namespace: dict[str, Any] = {}
                with open(self.charset_path, "r", encoding="utf-8") as f:
                    code = f.read()
                exec(code, namespace)
                if "CHARSET_OLD" in namespace:
                    self._charset = namespace["CHARSET_OLD"]
                    return
                if "CHARSET_BETA" in namespace:
                    self._charset = namespace["CHARSET_BETA"]
                    return
            except Exception as exc:
                logger.warning("Could not parse charset from %s: %s", self.charset_path, exc)

        self._charset = [""] + [chr(i) for i in range(ord('0'), ord('9') + 1)] + \
                        [chr(i) for i in range(ord('A'), ord('Z') + 1)] + \
                        [chr(i) for i in range(ord('a'), ord('z') + 1)]

    def capability(self) -> SolverCapability:
        return SolverCapability(frozenset({CaptchaType.IMAGE_TEXT.value}), "text", ANSWER_TEXT,
                                inputs={"*": (IMAGE_INPUTS,)}, production=False)

    def health(self) -> Health:
        return Health(True) if self.model_path and Path(self.model_path).is_file() else Health(False, "ddddocr model not installed")

    def can_solve(self, challenge: CaptchaChallenge) -> bool:
        if not self.enabled:
            return False
        if challenge.captcha_type != CaptchaType.IMAGE_TEXT:
            return False

        params = challenge.params
        return bool(
            params.get("image_data") or
            params.get("image_bytes") or
            params.get("text_prompt") or
            params.get("raw_text") or
            params.get("mock_solution") or
            params.get("image_path")
        )

    async def solve(self, challenge: CaptchaChallenge) -> dict[str, Any] | None:
        params = challenge.params

        text_prompt = params.get("text_prompt") or params.get("raw_text") or ""
        math_res = self._solve_math(text_prompt)
        if math_res is not None:
            return {
                "text": str(math_res),
                "confidence": 0.98,
                "method": "ddddocr_math_eval",
                "solver": self.solver_id,
            }

        image_bytes = self._extract_image_bytes(params)
        if not image_bytes:
            mock_sol = params.get("mock_solution")
            if mock_sol:
                return {
                    "text": str(mock_sol),
                    "confidence": 0.99,
                    "method": "ddddocr_mock",
                    "solver": self.solver_id,
                }
            return None

        if await asyncio.to_thread(self._ensure_session) and self._session is not None:
            try:
                result_text = await asyncio.to_thread(self._infer_onnx, image_bytes)
                if result_text:
                    inner_math = self._solve_math(result_text)
                    if inner_math is not None:
                        return {
                            "text": str(inner_math),
                            "confidence": 0.95,
                            "method": "ddddocr_ocr_math",
                            "solver": self.solver_id,
                        }
                    return {
                        "text": result_text,
                        "confidence": 0.92,
                        "method": "ddddocr_onnx",
                        "solver": self.solver_id,
                    }
            except Exception as exc:
                logger.warning("ddddocr inference error: %s", exc)

        mock_sol = params.get("mock_solution")
        if mock_sol:
            return {
                "text": str(mock_sol),
                "confidence": 0.99,
                "method": "ddddocr_mock",
                "solver": self.solver_id,
            }

        return None

    def _extract_image_bytes(self, params: dict[str, Any]) -> bytes | None:
        if "image_bytes" in params and isinstance(params["image_bytes"], (bytes, bytearray)):
            return bytes(params["image_bytes"])

        data_uri = params.get("image_data")
        if data_uri and isinstance(data_uri, str):
            if "," in data_uri:
                data_uri = data_uri.split(",", 1)[1]
            try:
                return base64.b64decode(data_uri)
            except Exception:
                pass

        img_path = params.get("image_path")
        if img_path and os.path.isfile(img_path):
            try:
                return Path(img_path).read_bytes()
            except Exception:
                pass

        return None

    def _infer_onnx(self, image_bytes: bytes) -> str:
        try:
            np = importlib.import_module("numpy")  # type: ignore
            Image = importlib.import_module("PIL.Image")  # type: ignore
        except ImportError:
            return ""

        pil_image = Image.open(io.BytesIO(image_bytes))
        target_height = 64
        target_width = max(int(pil_image.size[0] * (target_height / max(pil_image.size[1], 1))), 32)
        pil_image = pil_image.resize((target_width, target_height), Image.Resampling.BILINEAR)
        pil_image = pil_image.convert("L")

        img_array = np.array(pil_image).astype(np.float32) / 255.0
        img_array = np.expand_dims(img_array, axis=0)
        img_array = np.expand_dims(img_array, axis=0)

        input_name = self._session.get_inputs()[0].name
        outputs = self._session.run(None, {input_name: img_array})
        raw_output = outputs[0]

        if len(raw_output.shape) == 3:
            if raw_output.shape[1] == 1:
                predicted_indices = np.argmax(raw_output[:, 0, :], axis=1)
            else:
                predicted_indices = np.argmax(raw_output[0, :, :], axis=1)
        else:
            predicted_indices = np.argmax(raw_output, axis=-1)

        decoded: list[int] = []
        last_idx = -1
        for idx in predicted_indices:
            idx_int = int(idx)
            if idx_int != last_idx and idx_int != 0:
                decoded.append(idx_int)
            last_idx = idx_int

        if not self._charset:
            return ""

        chars: list[str] = []
        for idx_int in decoded:
            if idx_int < len(self._charset):
                chars.append(self._charset[idx_int])

        return "".join(chars).strip()

    def _solve_math(self, text: str) -> int | None:
        if not text:
            return None
        m = _MATH_RE.search(text)
        if not m:
            return None
        try:
            a = int(m.group(1))
            op = m.group(2).lower()
            b = int(m.group(3))
            if op in ("+", "plus"):
                return a + b
            if op in ("-", "minus"):
                return a - b
            if op in ("*", "x", "times"):
                return a * b
            if op == "/":
                return a // b if b != 0 else None
        except Exception:
            pass
        return None
