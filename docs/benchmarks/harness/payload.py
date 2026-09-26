"""Deterministic, range-addressable, incompressible test payloads.

Every file is identified by ``(size, seed)``. Byte ``i`` of a file is a pure
function of ``(seed, i)``, so the server can serve any byte range without
storing the file, and the harness can compute the expected SHA-256 without
downloading anything.

Layout: the file is a sequence of 1 MiB blocks. Block ``k`` is a seeded random
1 MiB pattern rotated by a block-specific offset, with the first 16 bytes
overwritten by ``seed`` and ``k``. A segment written at the wrong offset, a
duplicated range, or a zero-filled gap therefore changes the digest, and a
transparently compressing proxy gains nothing because each block is random.
"""

from __future__ import annotations

import hashlib
import random
import struct
import threading
from functools import lru_cache

BLOCK = 1024 * 1024
_ROTATE_STEP = 7919  # prime, so consecutive blocks never share an alignment
_lock = threading.Lock()
_digests: dict[tuple[int, int], str] = {}


@lru_cache(maxsize=16)
def _pattern(seed: int) -> bytes:
    return random.Random(seed).randbytes(BLOCK)


@lru_cache(maxsize=64)  # 64 MiB: enough for 32 concurrent ranges, bounded server RAM
def block(seed: int, index: int) -> bytes:
    """Return the full 1 MiB block ``index`` for ``seed``."""
    pattern = _pattern(seed)
    shift = (index * _ROTATE_STEP) % BLOCK
    rotated = pattern[shift:] + pattern[:shift]
    return struct.pack("<QQ", seed, index) + rotated[16:]


def read_range(size: int, seed: int, start: int, end_inclusive: int):
    """Yield the bytes ``[start, end_inclusive]`` of the file in <=1 MiB pieces."""
    if start < 0 or end_inclusive >= size or start > end_inclusive:
        raise ValueError(f"range {start}-{end_inclusive} outside file of {size} bytes")
    position = start
    while position <= end_inclusive:
        index, offset = divmod(position, BLOCK)
        data = block(seed, index)
        take = min(BLOCK - offset, end_inclusive - position + 1)
        yield memoryview(data)[offset:offset + take]
        position += take


def sha256(size: int, seed: int) -> str:
    """Expected SHA-256 (hex) of the file; cached per process."""
    key = (size, seed)
    with _lock:
        if key in _digests:
            return _digests[key]
    digest = hashlib.sha256()
    if size:
        for piece in read_range(size, seed, 0, size - 1):
            digest.update(piece)
    value = digest.hexdigest()
    with _lock:
        _digests[key] = value
    return value


def file_sha256(path, chunk: int = 4 * BLOCK) -> str:
    """SHA-256 of a file on disk, streamed so large files stay out of RAM."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            data = handle.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def parse_size(text: str | int) -> int:
    """Parse ``1GiB``, ``10MiB``, ``512KiB``, ``1000000`` into bytes (binary units)."""
    if isinstance(text, int):
        return text
    value = str(text).strip()
    units = {"kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "b": 1}
    lowered = value.lower()
    for suffix in sorted(units, key=len, reverse=True):
        if lowered.endswith(suffix):
            return int(float(lowered[: -len(suffix)]) * units[suffix])
    return int(value)
