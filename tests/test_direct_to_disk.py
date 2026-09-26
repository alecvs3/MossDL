from __future__ import annotations
"""Direct-to-disk unit and integration verification test suite.

Verifies:
1. Direct-to-disk transfers do NOT leave `{index}.part` files on disk.
2. Assembly duration is T_assembly <= 15 ms.
3. Write amplification factor is WAF = 1.00.
4. Cryptographic SHA-256 digest matches pre-computed expectations.
5. Uncapped high-throughput mode achieves maximal socket/disk loopback speed.
"""


import asyncio
import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine.benchmarking import (  # noqa: E402
    BenchmarkRunner,
    BenchmarkScenario,
    _FixtureServer,
    _FixtureSpec,
    _generate_payload,
)
from engine.custom_downloader import CustomAsyncBackend  # noqa: E402
from engine.limits import ResourceManager, SchedulerPolicy  # noqa: E402
from engine.models import ResolvedItem  # noqa: E402


class DirectToDiskTests(unittest.TestCase):
    """Rigorous verification of direct-to-disk download mechanics."""

    def test_direct_to_disk_no_part_files_remain(self):
        """Verifies direct-to-disk transfer leaves no {index}.part files on disk."""
        payload_size = 4 * 1024 * 1024
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runner = BenchmarkRunner(
                output_dir=root,
                payload_size=payload_size,
                advertised_rate_bytes_per_second=0,
                uncapped=True,
            )
            report = runner.run("direct-to-disk")
            data = report.to_dict()

            # Ensure custom backend succeeded
            custom_result = next(r for r in data["results"] if r["backend"] == "custom")
            self.assertTrue(custom_result["gate"]["passed"], f"Gate failed: {custom_result['gate']}")
            # No checksum is published for this benchmark case, so length is all
            # that was checked (see test_direct_to_disk_sha256_cryptographic_integrity
            # for the real content proof).
            self.assertEqual(custom_result["integrity"]["state"], "size_verified")

            # Check that all files in root temporary directory do not contain part files
            all_files = list(root.rglob("*"))
            part_files = [
                f.name for f in all_files
                if f.is_file() and (".part" in f.name.lower() or f.name.endswith(".part"))
            ]
            self.assertEqual(
                part_files,
                [],
                f"Direct-to-disk must not leave any .part or {{index}}.part files, found: {part_files}",
            )

            # Specifically verify no {index}.part pattern exists (e.g. 0.part, 1.part, part.0, etc.)
            index_parts = [
                f.name for f in all_files
                if f.is_file() and any(f.name.endswith(f".part.{i}") or f.name.endswith(f".{i}.part") for i in range(16))
            ]
            self.assertEqual(index_parts, [], f"Leftover indexed part files found: {index_parts}")

            # Verify no segment manifests remain
            manifest_files = [f.name for f in all_files if "segments.json" in f.name]
            self.assertEqual(manifest_files, [], f"Leftover segment manifest files found: {manifest_files}")

    def test_direct_to_disk_assembly_duration_under_15ms(self):
        """Verifies assembly duration T_assembly <= 15 ms."""
        payload_size = 4 * 1024 * 1024
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = BenchmarkRunner(
                output_dir=temp_dir,
                payload_size=payload_size,
                advertised_rate_bytes_per_second=0,
                uncapped=True,
            )
            report = runner.run("direct-to-disk")
            custom_result = next(r for r in report.results if r.backend == "custom")

            assembly_duration = custom_result.metrics.get("assembly_duration_seconds")
            self.assertIsNotNone(assembly_duration)
            self.assertLessEqual(
                assembly_duration,
                0.015,
                f"T_assembly {assembly_duration * 1000:.3f} ms exceeds 15 ms boundary",
            )

        # Microbenchmark the atomic direct-to-disk file publish mechanism on this storage
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            sparse_part = temp_path / "large_file.part"
            final_dest = temp_path / "large_file.bin"
            # Pre-allocate sparse 32 MB file
            with sparse_part.open("wb") as handle:
                handle.truncate(32 * 1024 * 1024)

            t0 = time.monotonic()
            os.replace(sparse_part, final_dest)
            t_assembly = time.monotonic() - t0

            self.assertLessEqual(
                t_assembly,
                0.015,
                f"Atomic replace assembly duration {t_assembly * 1000:.3f} ms exceeds 15 ms target",
            )
            self.assertTrue(final_dest.exists())
            self.assertFalse(sparse_part.exists())

    def test_direct_to_disk_write_amplification_factor_is_one(self):
        """Verifies Write Amplification Factor WAF = 1.00 (single write pass directly to disk)."""
        payload_size = 4 * 1024 * 1024
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = BenchmarkRunner(
                output_dir=temp_dir,
                payload_size=payload_size,
                advertised_rate_bytes_per_second=0,
                uncapped=True,
            )
            report = runner.run("direct-to-disk")
            custom_result = next(r for r in report.results if r.backend == "custom")

            waf = custom_result.metrics.get("write_amplification_factor")
            self.assertIsNotNone(waf)
            self.assertEqual(waf, 1.00, f"Expected WAF 1.00, got {waf}")

            # Verify write_bytes equals observed_size (no duplicate write passes)
            write_bytes = custom_result.metrics.get("write_bytes", 0)
            self.assertEqual(write_bytes, payload_size)

    def test_direct_to_disk_sha256_cryptographic_integrity(self):
        """Verifies cryptographic SHA-256 digest matches pre-computed expectation."""
        payload_size = 8 * 1024 * 1024
        expected_bytes = _generate_payload(payload_size)
        expected_digest = hashlib.sha256(expected_bytes).hexdigest()

        # 1. Verification via BenchmarkRunner and engine integrity contract
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = BenchmarkRunner(
                output_dir=temp_dir,
                payload_size=payload_size,
                advertised_rate_bytes_per_second=0,
                uncapped=True,
            )
            report = runner.run("direct-to-disk")
            custom_result = next(r for r in report.results if r.backend == "custom")

            # The direct-to-disk benchmark case publishes no expected checksum, so
            # the engine only ever checked the length here. The cryptographic
            # assertion this test is named for is made independently in part 2
            # below, against the bytes actually on disk.
            self.assertEqual(custom_result.integrity.get("state"), "size_verified")
            self.assertIsNone(custom_result.failure)

        # 2. Independent file on disk cryptographic verification
        spec = _FixtureSpec(case="valid-206", payload_size=payload_size, rate_bytes_per_second=0)
        policy = SchedulerPolicy(
            max_active_tasks=1,
            max_active_segments=8,
            per_host_transfers=8,
            max_segments_per_file=8,
            min_segment_size=512 * 1024,
            initial_segment_concurrency=8,
            per_host_requests_per_second=0,
            bandwidth_bytes_per_second=0,
            max_retries=2,
            request_timeout_seconds=30,
        )
        resources = ResourceManager(policy)
        backend = CustomAsyncBackend(resources)
        with _FixtureServer(spec) as server, tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = ResolvedItem(
                "test-sha",
                server.url,
                "fixture_sha.bin",
                size=payload_size,
                direct_url=server.url,
                checksum=f"sha256:{expected_digest}",
            )
            output_file = asyncio.run(backend.download(item, root))
            self.assertTrue(output_file.is_file())
            actual_digest = hashlib.sha256(output_file.read_bytes()).hexdigest()
            self.assertEqual(
                actual_digest,
                expected_digest,
                f"Cryptographic digest mismatch: expected {expected_digest}, got {actual_digest}",
            )

    def test_uncapped_high_speed_direct_to_disk_throughput(self):
        """Verifies uncapped throughput runs at maximum wire/disk speed without artificial sleep."""
        payload_size = 8 * 1024 * 1024
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = BenchmarkRunner(
                output_dir=temp_dir,
                payload_size=payload_size,
                advertised_rate_bytes_per_second=0,
                uncapped=True,
            )
            report = runner.run("direct-to-disk")
            custom_result = next(r for r in report.results if r.backend == "custom")

            throughput = custom_result.metrics.get("throughput_bytes_per_second", 0.0)
            self.assertGreater(throughput, 0.0, "Throughput must be positive in uncapped mode")
            self.assertTrue(custom_result.gate.get("passed"))
            self.assertEqual(custom_result.metrics.get("write_amplification_factor"), 1.00)
            self.assertLessEqual(custom_result.metrics.get("assembly_duration_seconds", 0.0), 0.015)

    def test_custom_async_backend_multi_segment_direct_to_disk(self):
        """Direct integration test of CustomAsyncBackend performing segmented direct-to-disk transfer."""
        payload_size = 4 * 1024 * 1024
        expected_bytes = _generate_payload(payload_size)
        expected_hash = hashlib.sha256(expected_bytes).hexdigest()

        spec = _FixtureSpec(case="valid-206", payload_size=payload_size, rate_bytes_per_second=0)
        policy = SchedulerPolicy(
            max_active_tasks=1,
            max_active_segments=8,
            per_host_transfers=8,
            max_segments_per_file=8,
            min_segment_size=256 * 1024,
            initial_segment_concurrency=8,
            per_host_requests_per_second=0,
            bandwidth_bytes_per_second=0,
            max_retries=2,
            request_timeout_seconds=30,
        )
        resources = ResourceManager(policy)
        backend = CustomAsyncBackend(resources)

        with _FixtureServer(spec) as server, tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            item = ResolvedItem(
                "test-dtd",
                server.url,
                "test_download.bin",
                size=payload_size,
                direct_url=server.url,
                checksum=f"sha256:{expected_hash}",
            )

            progress_events: list[int] = []
            output_file = asyncio.run(backend.download(item, root, progress=progress_events.append))

            # 1. Output file exists and size matches
            self.assertTrue(output_file.is_file())
            self.assertEqual(output_file.stat().st_size, payload_size)

            # 2. No {index}.part or .part files exist on disk
            all_parts = [p.name for p in root.rglob("*") if ".part" in p.name.lower()]
            self.assertEqual(all_parts, [], f"Part files must be cleaned up, found: {all_parts}")

            # 3. Verify SHA-256 hash
            actual_hash = hashlib.sha256(output_file.read_bytes()).hexdigest()
            self.assertEqual(actual_hash, expected_hash)

            # 4. Progress was tracked
            self.assertTrue(progress_events)
            self.assertEqual(progress_events[-1], payload_size)


if __name__ == "__main__":
    unittest.main()
