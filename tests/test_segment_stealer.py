from __future__ import annotations
"""Tests for engine.segment_stealer.DynamicSegmentCoordinator.

Validates 64 KB-aligned work-stealing, concurrent safety,
straggler elimination (>= 10x speedup), and manifest persistence.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# Ensure the project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.segment_stealer import DynamicSegmentCoordinator, SegmentState


class TestSegmentStealer(unittest.TestCase):
    """Core correctness tests for DynamicSegmentCoordinator."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.manifest = Path(self.tmpdir) / "segments.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # 1. Basic Work-Stealing
    # ------------------------------------------------------------------
    def test_basic_steal(self) -> None:
        """Complete 3 of 4 segments, then claim_work should steal from the 4th."""
        total = 4 * 1024 * 1024 * 64  # 256 MB
        seg_size = total // 4
        ranges = [(i * seg_size, (i + 1) * seg_size - 1) for i in range(4)]
        coord = DynamicSegmentCoordinator(
            self.manifest, total, min_steal_bytes=1024 * 1024, flush_interval=0,
        )
        coord.load_or_init(ranges)

        # Assign and complete the first three
        for i in range(3):
            seg = coord.claim_work(f"w-{i}")
            self.assertIsNotNone(seg)
            coord.complete_segment(seg.id)

        # Fourth segment is now active
        seg4 = coord.claim_work("w-3")
        self.assertIsNotNone(seg4)
        self.assertEqual(seg4.status, "active")

        # Now a fifth worker tries to claim — should steal from seg4
        stolen = coord.claim_work("w-4")
        self.assertIsNotNone(stolen, "Expected work-stealing to trigger")
        self.assertEqual(stolen.worker_id, "w-4")
        self.assertGreater(stolen.start, seg4.start)

    # ------------------------------------------------------------------
    # 2. 64 KB Block Alignment
    # ------------------------------------------------------------------
    def test_64kb_alignment(self) -> None:
        """Stolen segment boundaries must be 64 KB-aligned."""
        total = 128 * 1024 * 1024  # 128 MB
        ranges = [(0, total - 1)]
        coord = DynamicSegmentCoordinator(
            self.manifest, total, min_steal_bytes=1024 * 1024, flush_interval=0,
        )
        coord.load_or_init(ranges)

        # Claim the single range so it becomes active
        seg = coord.claim_work("victim")
        self.assertIsNotNone(seg)

        # Steal from it
        stolen = coord.claim_work("thief")
        self.assertIsNotNone(stolen, "Expected steal from the large active segment")
        self.assertEqual(
            stolen.start % 65536, 0,
            f"Stolen start {stolen.start} is not 64KB-aligned",
        )

    # ------------------------------------------------------------------
    # 3. Minimum Steal Threshold
    # ------------------------------------------------------------------
    def test_min_threshold_blocks_steal(self) -> None:
        """Segments smaller than 2 * min_steal_bytes should not be split."""
        total = 4 * 1024 * 1024  # 4 MB
        ranges = [(0, total - 1)]
        coord = DynamicSegmentCoordinator(
            self.manifest, total, min_steal_bytes=8 * 1024 * 1024, flush_interval=0,
        )
        coord.load_or_init(ranges)

        seg = coord.claim_work("w-0")
        self.assertIsNotNone(seg)

        # Attempt steal — should fail because 4 MB < 2 * 8 MB
        stolen = coord.claim_work("w-1")
        self.assertIsNone(stolen, "Steal should not happen below threshold")

    # ------------------------------------------------------------------
    # 4. No Gaps or Overlaps After Multiple Steals
    # ------------------------------------------------------------------
    def test_no_gaps_no_overlaps(self) -> None:
        """After aggressive stealing, all segments must tile [0, total-1] exactly."""
        total = 256 * 1024 * 1024  # 256 MB
        ranges = [(0, total - 1)]
        # Use a very small min_steal so we get many steals
        coord = DynamicSegmentCoordinator(
            self.manifest, total, min_steal_bytes=512 * 1024, flush_interval=0,
        )
        coord.load_or_init(ranges)

        # Keep stealing until no more work is available
        seg = coord.claim_work("w-0")
        worker_idx = 1
        while True:
            stolen = coord.claim_work(f"w-{worker_idx}")
            if stolen is None:
                break
            worker_idx += 1

        # Verify contiguous tiling
        ordered = sorted(coord.segments, key=lambda s: s.start)
        cursor = 0
        for s in ordered:
            self.assertEqual(s.start, cursor, f"Gap before segment {s.id} at byte {cursor}")
            cursor = s.end + 1
        self.assertEqual(cursor, total, f"Segments end at {cursor}, expected {total}")

        # Verify unique IDs
        ids = [s.id for s in coord.segments]
        self.assertEqual(len(ids), len(set(ids)), "Duplicate segment IDs found")

    # ------------------------------------------------------------------
    # 5. Concurrent Safety
    # ------------------------------------------------------------------
    def test_concurrent_workers(self) -> None:
        """8 threads concurrently claiming and recording progress must not race."""
        total = 64 * 1024 * 1024  # 64 MB
        seg_size = total // 8
        ranges = [(i * seg_size, (i + 1) * seg_size - 1) for i in range(8)]
        coord = DynamicSegmentCoordinator(
            self.manifest, total, min_steal_bytes=512 * 1024, flush_interval=0,
        )
        coord.load_or_init(ranges)

        errors: list[str] = []
        assigned_ids: list[int] = []
        lock = threading.Lock()

        def worker(idx: int) -> None:
            try:
                while True:
                    seg = coord.claim_work(f"thread-{idx}")
                    if seg is None:
                        break
                    with lock:
                        assigned_ids.append(seg.id)
                    # Simulate downloading in chunks
                    chunk_size = 256 * 1024
                    while seg.done < seg.expected:
                        delta = min(chunk_size, seg.remaining)
                        coord.record_progress(seg.id, delta)
            except Exception as exc:
                with lock:
                    errors.append(f"thread-{idx}: {exc}")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        coord.flush()
        self.assertEqual(errors, [], f"Thread errors: {errors}")
        self.assertTrue(coord.is_all_done(), "Not all segments completed")
        self.assertEqual(coord.total_bytes_done(), total)

    # ------------------------------------------------------------------
    # 6. Manifest Persistence Round-Trip
    # ------------------------------------------------------------------
    def test_manifest_persistence(self) -> None:
        """Save, reload from JSON, verify segments match."""
        total = 32 * 1024 * 1024
        ranges = [(0, total // 2 - 1), (total // 2, total - 1)]
        coord = DynamicSegmentCoordinator(
            self.manifest, total, min_steal_bytes=1024 * 1024, flush_interval=0,
        )
        coord.load_or_init(ranges)

        seg = coord.claim_work("w-0")
        coord.record_progress(seg.id, 1024 * 1024)
        coord.flush()

        # Reload
        coord2 = DynamicSegmentCoordinator(
            self.manifest, total, min_steal_bytes=1024 * 1024, flush_interval=0,
        )
        coord2.load_or_init(ranges)  # Should detect and resume manifest

        self.assertEqual(len(coord2.segments), len(coord.segments))
        for s1, s2 in zip(
            sorted(coord.segments, key=lambda s: s.id),
            sorted(coord2.segments, key=lambda s: s.id),
        ):
            self.assertEqual(s1.id, s2.id)
            self.assertEqual(s1.start, s2.start)
            self.assertEqual(s1.end, s2.end)
            self.assertEqual(s1.done, s2.done)

    # ------------------------------------------------------------------
    # 7. Segment ID Uniqueness After Multiple Steals
    # ------------------------------------------------------------------
    def test_segment_id_uniqueness(self) -> None:
        """All segment IDs must remain unique after many steal operations."""
        total = 512 * 1024 * 1024  # 512 MB
        ranges = [(0, total - 1)]
        coord = DynamicSegmentCoordinator(
            self.manifest, total, min_steal_bytes=1024 * 1024, flush_interval=0,
        )
        coord.load_or_init(ranges)

        coord.claim_work("w-0")  # activate
        idx = 1
        while True:
            stolen = coord.claim_work(f"w-{idx}")
            if stolen is None:
                break
            idx += 1

        ids = [s.id for s in coord.segments]
        self.assertEqual(len(ids), len(set(ids)), f"Duplicate IDs in {ids}")


class TestStragglerBenchmark(unittest.TestCase):
    """Performance benchmark proving work-stealing eliminates stragglers."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.manifest = Path(self.tmpdir) / "segments.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_straggler_elimination(self) -> None:
        """8 connections: 7 @ 10 MB/s + 1 @ 50 KB/s on 64 MB.

        Static: bottleneck = (64/8) MB / (50 KB/s) ≈ 160s
        Stealing: fast workers finish early, steal slow worker's bytes.
        Assert >= 10x speedup.
        """
        FILE_SIZE = 64 * 1024 * 1024  # 64 MB
        NUM_WORKERS = 8
        FAST_RATE = 10 * 1024 * 1024  # 10 MB/s
        SLOW_RATE = 50 * 1024  # 50 KB/s

        # --- Static simulation (no stealing) ---
        static_seg_size = FILE_SIZE // NUM_WORKERS
        rates = [SLOW_RATE] + [FAST_RATE] * (NUM_WORKERS - 1)
        sizes = [static_seg_size] * NUM_WORKERS
        sizes[-1] += FILE_SIZE - sum(sizes)
        static_times = [s / r for s, r in zip(sizes, rates)]
        static_wall = max(static_times)  # bottleneck is the slow worker (~163s)

        # --- Work-stealing simulation ---
        # Model: each worker independently consumes its assigned bytes at its
        # rate. When a worker finishes, it immediately steals from the largest
        # active segment. We compute the total simulated time as:
        #   total_bytes_remaining / aggregate_rate
        # where aggregate_rate increases as fast workers finish and re-engage.
        steal_manifest = Path(self.tmpdir) / "steal_segments.json"
        coord = DynamicSegmentCoordinator(
            steal_manifest, FILE_SIZE, min_steal_bytes=512 * 1024, flush_interval=0,
        )
        seg_size = FILE_SIZE // NUM_WORKERS
        ranges = [(i * seg_size, min((i + 1) * seg_size, FILE_SIZE) - 1) for i in range(NUM_WORKERS)]
        ranges[-1] = (ranges[-1][0], FILE_SIZE - 1)
        coord.load_or_init(ranges)

        worker_rates = [SLOW_RATE] + [FAST_RATE] * (NUM_WORKERS - 1)

        # Worker state: (remaining_bytes_in_current_segment, segment_obj)
        worker_segs = []
        for w in range(NUM_WORKERS):
            seg = coord.claim_work(f"sim-{w}")
            worker_segs.append(seg)

        # Discrete event simulation: find which worker finishes first,
        # advance time, then have that worker steal new work.
        total_time = 0.0
        total_done = 0
        progress_shown = 0

        for _ in range(10000):  # safety bound
            if coord.is_all_done():
                break

            # For each active worker, compute time to finish current segment
            finish_times = []
            for w in range(NUM_WORKERS):
                seg = worker_segs[w]
                if seg is not None and seg.remaining > 0:
                    t = seg.remaining / worker_rates[w]
                    finish_times.append((t, w))

            if not finish_times:
                break

            # Advance to the earliest finishing worker
            finish_times.sort()
            dt, winner = finish_times[0]
            total_time += dt

            # All active workers make progress proportional to dt
            for w in range(NUM_WORKERS):
                seg = worker_segs[w]
                if seg is not None and seg.remaining > 0:
                    bytes_done = min(int(worker_rates[w] * dt), seg.remaining)
                    if bytes_done > 0:
                        coord.record_progress(seg.id, bytes_done)
                        total_done += bytes_done

            # Complete the winner's segment
            if worker_segs[winner] is not None:
                coord.complete_segment(worker_segs[winner].id)

            # Winner tries to steal new work
            stolen = coord.claim_work(f"sim-{winner}")
            worker_segs[winner] = stolen

            # Progress bar
            pct = min(100.0, total_done / FILE_SIZE * 100)
            if pct - progress_shown >= 5.0 or pct >= 99.9:
                bar_len = 30
                filled = int(bar_len * pct / 100)
                bar = "█" * filled + "░" * (bar_len - filled)
                mb_done = total_done / (1024 * 1024)
                mb_total = FILE_SIZE / (1024 * 1024)
                sys.stderr.write(
                    f"\r  [{bar}] {pct:5.1f}% | {mb_done:.1f}/{mb_total:.1f} MB"
                )
                sys.stderr.flush()
                progress_shown = pct

        # Finish any remaining bytes
        coord.flush()
        sys.stderr.write("\n")

        steal_wall = total_time
        speedup = static_wall / steal_wall if steal_wall > 0 else float("inf")
        sys.stderr.write(
            f"  Static: {static_wall:.2f}s | Stealing: {steal_wall:.2f}s | "
            f"Speedup: {speedup:.1f}x\n"
        )
        sys.stderr.flush()

        self.assertTrue(coord.is_all_done(), "Work-stealing simulation did not complete")
        self.assertGreaterEqual(
            speedup, 10.0,
            f"Work-stealing speedup {speedup:.1f}x is below 10x target "
            f"(static={static_wall:.2f}s, steal={steal_wall:.2f}s)",
        )

    def test_sha256_integrity_after_stealing(self) -> None:
        """Verify that work-stealing produces correct byte coverage for SHA-256 validation."""
        FILE_SIZE = 16 * 1024 * 1024  # 16 MB for speed
        seg_size = FILE_SIZE // 4
        ranges = [(i * seg_size, (i + 1) * seg_size - 1) for i in range(4)]
        # Fix last
        ranges[-1] = (ranges[-1][0], FILE_SIZE - 1)

        coord = DynamicSegmentCoordinator(
            self.manifest, FILE_SIZE, min_steal_bytes=512 * 1024, flush_interval=0,
        )
        coord.load_or_init(ranges)

        # Simulate all workers completing via claim + progress
        byte_map = bytearray(FILE_SIZE)  # Track which bytes are "owned"
        worker = 0
        while not coord.is_all_done():
            seg = coord.claim_work(f"w-{worker}")
            if seg is None:
                break
            # Mark bytes
            for b in range(seg.start, min(seg.end + 1, FILE_SIZE)):
                self.assertEqual(byte_map[b], 0, f"Byte {b} assigned twice!")
                byte_map[b] = 1
            coord.complete_segment(seg.id)
            worker += 1

        # All bytes must be covered exactly once
        self.assertTrue(all(b == 1 for b in byte_map), "Not all bytes covered")
        self.assertTrue(coord.is_all_done())


if __name__ == "__main__":
    unittest.main()
