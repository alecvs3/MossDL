"""Process-tree resource sampler (Windows-first, psutil).

Tracks a set of root processes -- given by PID and/or executable name -- plus
all of their descendants, sampling every ``interval`` seconds:

* CPU: user+system seconds consumed during the window, summed over the tree.
  CPU time of processes that exit mid-window is kept (last value seen).
* Memory: tree working set (``rss`` == Windows working set) and tree private
  bytes (``vms`` == pagefile/private on Windows) at each sample; the peak of
  the sums is reported. Sampling can miss short spikes, so the sum of each
  process's OS-tracked ``peak_wset`` is also reported as an upper bound.
* Disk: bytes written by the tree (``io_counters().write_bytes``), which
  exposes write amplification (temp-file assembly, pre-allocation, journals).

Sampling itself costs CPU; keep interval >= 0.2 s and run the sampler in the
harness process, never inside a measured client.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import psutil


def _write_bytes(proc: psutil.Process, fallback: int) -> int:
    """Bytes written so far; ``fallback`` when the OS refuses (e.g. elevated process)."""
    try:
        return proc.io_counters().write_bytes
    except (psutil.Error, AttributeError):
        return fallback


@dataclass
class _Proc:
    proc: psutil.Process
    cpu0: float
    cpu_last: float
    write0: int
    write_last: int
    peak_wset: int = 0


@dataclass
class Sample:
    t: float
    procs: int
    cpu_s: float
    wset: int
    private: int


@dataclass
class ResourceReport:
    duration_s: float = 0.0
    cpu_seconds: float = 0.0
    cpu_avg_pct_one_core: float = 0.0     # 100 == one logical core fully busy
    cpu_avg_pct_machine: float = 0.0      # 100 == every logical core busy
    peak_tree_wset_bytes: int = 0
    peak_tree_private_bytes: int = 0
    sum_process_peak_wset_bytes: int = 0  # upper bound; peaks may not coincide
    disk_write_bytes: int = 0
    max_processes: int = 0
    process_names: list[str] = field(default_factory=list)
    samples: int = 0
    interval_s: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class TreeSampler:
    def __init__(self, pids: list[int] | None = None, names: list[str] | None = None,
                 interval: float = 0.25, keep_series: bool = False) -> None:
        self.root_pids = set(pids or [])
        self.names = {name.lower() for name in (names or [])}
        self.interval = max(0.05, interval)
        self.keep_series = keep_series
        self.series: list[Sample] = []
        self._tracked: dict[int, _Proc] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._report = ResourceReport(interval_s=self.interval)
        self._started = 0.0

    def add_pid(self, pid: int) -> None:
        self.root_pids.add(pid)

    # ---- discovery --------------------------------------------------------
    def _roots(self) -> list[psutil.Process]:
        roots = []
        for pid in list(self.root_pids):
            try:
                roots.append(psutil.Process(pid))
            except psutil.Error:
                continue
        if self.names:
            for proc in psutil.process_iter(["name"]):
                if (proc.info.get("name") or "").lower() in self.names:
                    roots.append(proc)
        return roots

    def _discover(self) -> None:
        for root in self._roots():
            try:
                members = [root, *root.children(recursive=True)]
            except psutil.Error:
                members = [root]
            for proc in members:
                if proc.pid in self._tracked:
                    continue
                try:
                    cpu = sum(proc.cpu_times()[:2])
                except psutil.Error:
                    continue
                written = _write_bytes(proc, 0)
                # A process that existed before the window opened (e.g. a GUI
                # app launched before the trial) is measured from now; one
                # created inside the window is measured from its birth.
                try:
                    born_inside = bool(self._started) and proc.create_time() >= self._started
                except psutil.Error:
                    born_inside = False
                base_cpu, base_write = (0.0, 0) if born_inside else (cpu, written)
                self._tracked[proc.pid] = _Proc(proc, base_cpu, cpu, base_write, written)
                try:
                    self._report.process_names.append(proc.name())
                except psutil.Error:
                    pass

    # ---- sampling ---------------------------------------------------------
    def _sample_once(self) -> None:
        self._discover()
        wset = private = alive = 0
        for tracked in self._tracked.values():
            try:
                with tracked.proc.oneshot():
                    tracked.cpu_last = sum(tracked.proc.cpu_times()[:2])
                    tracked.write_last = _write_bytes(tracked.proc, tracked.write_last)
                    memory = tracked.proc.memory_info()
            except psutil.Error:
                continue  # exited: keep its last CPU/IO values
            alive += 1
            wset += memory.rss
            private += memory.vms
            tracked.peak_wset = max(tracked.peak_wset, getattr(memory, "peak_wset", memory.rss))
        report = self._report
        report.samples += 1
        report.max_processes = max(report.max_processes, alive)
        report.peak_tree_wset_bytes = max(report.peak_tree_wset_bytes, wset)
        report.peak_tree_private_bytes = max(report.peak_tree_private_bytes, private)
        if self.keep_series:
            cpu = sum(t.cpu_last - t.cpu0 for t in self._tracked.values())
            self.series.append(Sample(time.time(), alive, cpu, wset, private))

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.perf_counter()
            self._sample_once()
            self._stop.wait(max(0.0, self.interval - (time.perf_counter() - started)))

    def start(self) -> "TreeSampler":
        self._discover()  # baseline for processes that already exist
        self._started = time.time()
        self._t0 = time.perf_counter()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tree-sampler")
        self._thread.start()
        return self

    def stop(self) -> ResourceReport:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._sample_once()
        report = self._report
        report.duration_s = time.perf_counter() - self._t0
        report.cpu_seconds = sum(max(0.0, t.cpu_last - t.cpu0) for t in self._tracked.values())
        report.disk_write_bytes = sum(max(0, t.write_last - t.write0) for t in self._tracked.values())
        report.sum_process_peak_wset_bytes = sum(t.peak_wset for t in self._tracked.values())
        if report.duration_s > 0:
            report.cpu_avg_pct_one_core = 100.0 * report.cpu_seconds / report.duration_s
            report.cpu_avg_pct_machine = report.cpu_avg_pct_one_core / (psutil.cpu_count() or 1)
        report.process_names = sorted(set(report.process_names))
        return report
