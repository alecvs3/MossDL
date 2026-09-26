from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CaptureUiContractTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_api_exposes_engine_owned_capture_and_media_commands(self) -> None:
        source = self.read("src/api.ts")
        for method in (
            "listCaptureBatches",
            "importCaptureCandidate",
            "browserImportCandidate",
            '"media_plan"',
            '"media_assemble"',
        ):
            self.assertIn(method, source)
        self.assertIn("CaptureImportResult", source)

    def test_review_uses_canonical_selection_and_explicit_duplicate_action(self) -> None:
        # Captures are reviewed in Explore, alongside explored pages.
        page = self.read("src/figma/pages/explore/ExplorePage.tsx")
        for field in ("selectedIds", "focusedId", "anchorId"):
            self.assertIn(field, page)
        self.assertIn("candidateIndex", page)
        self.assertIn("addAnyway", page)
        self.assertIn('"duplicate"', page)
        self.assertIn("Add anyway", page)
        self.assertNotIn("isHighlighted", page)
        self.assertNotIn("browserQueue", page)

    def test_app_wires_durable_capture_inbox_to_review_surface(self) -> None:
        app = self.read("src/App.tsx")
        figma = self.read("src/figma/FigmaApp.tsx")
        router = self.read("src/figma/components/FigmaRouter.tsx")
        self.assertIn("listCaptureBatches", app)
        self.assertIn("captureBatches", app)
        self.assertTrue("onCaptureImport" in figma or "onCaptureImport" in router)
        self.assertIn('case "explore":', router)
        self.assertIn("ExplorePage", router)
        self.assertIn("captureBatches=", router)

    def test_media_overlay_has_narrow_eligibility_and_drm_boundary(self) -> None:
        overlay = self.read("src/components/MediaCaptureOverlay.tsx")
        for marker in ("audio/", "video/", "m3u8", "mpd", "encrypted", "DRM", "media_assemble"):
            self.assertIn(marker, overlay if marker != "media_assemble" else self.read("src/api.ts"))
        self.assertIn("onPlan", overlay)
        self.assertIn("onImport", overlay)
        self.assertNotIn("document.querySelector", overlay)
        self.assertNotIn("decrypt", overlay.lower())


if __name__ == "__main__":
    unittest.main()
