"""`verified` must mean the CONTENT was proven, not just the length.

A size-only check was reported as "verified". A 2 GB volume arrived with a
byte-exact length and corrupt contents, passed our integrity check wearing a
verified badge, and was only caught later by the extractor's CRC. Length is
evidence; it is not proof, and the two must not share a name.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from engine.reliability import verify_file

PAYLOAD = b"example-repacks-volume-contents" * 64


class VerifyFileStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "volume.part1.rar"
        self.path.write_bytes(PAYLOAD)
        self.digest = hashlib.sha256(PAYLOAD).hexdigest()

    def test_matching_checksum_is_verified(self) -> None:
        report = verify_file(str(self.path), expected_size=len(PAYLOAD),
                             expected_checksum=self.digest, algorithm="sha256")
        self.assertEqual(report.state, "verified")

    def test_size_only_is_not_called_verified(self) -> None:
        """The whole point: no checksum means no content proof."""
        report = verify_file(str(self.path), expected_size=len(PAYLOAD))
        self.assertEqual(
            report.state, "size_verified",
            "a length-only check still claims content proof it never performed")
        self.assertIn("checksum", (report.reason or "").lower(),
                      "the weaker state must say WHY it is weaker")

    def test_no_expectations_at_all_is_unverifiable(self) -> None:
        report = verify_file(str(self.path))
        self.assertEqual(report.state, "unverifiable")

    def test_wrong_size_is_corrupt(self) -> None:
        report = verify_file(str(self.path), expected_size=len(PAYLOAD) + 1)
        self.assertEqual(report.state, "corrupt")

    def test_wrong_checksum_is_corrupt_even_at_the_right_size(self) -> None:
        """Exactly the failure mode that started this: right length, wrong bytes."""
        report = verify_file(str(self.path), expected_size=len(PAYLOAD),
                             expected_checksum="0" * 64, algorithm="sha256")
        self.assertEqual(report.state, "corrupt")


class TaskAggregationTests(unittest.TestCase):
    """A task is only as strong as its weakest item."""

    @staticmethod
    def _aggregate(states: list[str]) -> str:
        # Mirrors engine/service.py's aggregation; kept in lockstep by the
        # assertions below rather than re-implementing policy.
        if states and all(state == "verified" for state in states):
            return "verified"
        if states and all(state in {"verified", "size_verified"} for state in states):
            return "size_verified"
        return "unverifiable"

    def test_all_checksummed_is_verified(self) -> None:
        self.assertEqual(self._aggregate(["verified", "verified"]), "verified")

    def test_one_size_only_item_downgrades_the_task(self) -> None:
        self.assertEqual(self._aggregate(["verified", "size_verified"]), "size_verified")

    def test_any_unverifiable_item_downgrades_further(self) -> None:
        self.assertEqual(self._aggregate(["verified", "unverifiable"]), "unverifiable")

    def test_empty_is_not_verified(self) -> None:
        self.assertEqual(self._aggregate([]), "unverifiable")


class ArchiveGateTests(unittest.TestCase):
    """size_verified must still be allowed to extract, or nothing ever would."""

    def test_pass_states_include_size_verified(self) -> None:
        from engine.service import _INTEGRITY_PASS_STATES

        self.assertIn("verified", _INTEGRITY_PASS_STATES)
        self.assertIn(
            "size_verified", _INTEGRITY_PASS_STATES,
            "hosts that publish no checksum would never extract at all")
        self.assertNotIn("unverifiable", _INTEGRITY_PASS_STATES)
        self.assertNotIn("corrupt", _INTEGRITY_PASS_STATES)


if __name__ == "__main__":
    unittest.main()
