from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .archive_backend import ARCHIVE_COMPLETE_PREFIX, ARCHIVE_STAGING_PREFIX, ArchiveBackend


def produced_file_manifest(output_dir: str | Path, exclude_paths: Iterable[str | Path] | None = None) -> list[dict[str, Any]]:
    """Payload files produced under output_dir, excluding protocol artifacts and sources.

    The service must verify this manifest is non-empty before any source deletion:
    staging trees and completion sentinels are worker bookkeeping, not payload.
    """
    root = Path(output_dir)
    manifest: list[dict[str, Any]] = []
    if not root.is_dir():
        return manifest
    excluded = {os.path.normcase(str(Path(path).resolve())) for path in (exclude_paths or ()) if path}
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if not name.startswith(ARCHIVE_STAGING_PREFIX)]
        for name in files:
            if name.startswith(ARCHIVE_STAGING_PREFIX) or name.startswith(ARCHIVE_COMPLETE_PREFIX):
                continue
            candidate = Path(current) / name
            if candidate.is_symlink():
                continue
            if os.path.normcase(str(candidate.resolve())) in excluded:
                continue
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            manifest.append({"path": str(candidate), "size": size})
    manifest.sort(key=lambda entry: entry["path"])
    return manifest


@dataclass
class MultiPartInfo:
    is_multipart: bool
    base_name: str
    part_number: int
    format_type: str  # "part_archive", "split_binary", "numbered_rar", "multipart_zip"
    extension: str
    original_name: str


class MultiPartDetector:
    """Detects and groups multi-part archives and split binary files."""

    # Patterns
    # 1. Game.part1.rar, Game_part02.7z, Game.part001.zip
    PATTERN_PART = re.compile(r"^(?P<base>.+?)[._-]part(?P<num>\d+)\.(?P<ext>rar|7z|zip)$", re.IGNORECASE)
    # 2. Movie.001, Movie.002, Movie.003
    PATTERN_SPLIT = re.compile(r"^(?P<base>.+?)\.(?P<num>\d{3})$", re.IGNORECASE)
    # 3. Game.r00, Game.r01, Game.r02 (legacy RAR sequence where .rar is part 1 or header, .r00 is part 2)
    PATTERN_R_NUM = re.compile(r"^(?P<base>.+?)\.r(?P<num>\d{2,})$", re.IGNORECASE)
    # 4. Backup.z01, Backup.z02, Backup.zip
    PATTERN_Z_NUM = re.compile(r"^(?P<base>.+?)\.z(?P<num>\d{2,})$", re.IGNORECASE)

    @classmethod
    def detect(cls, filename_or_path: str | Path) -> MultiPartInfo | None:
        filename = Path(filename_or_path).name

        # 1. Standard .partX.ext
        m = cls.PATTERN_PART.match(filename)
        if m:
            return MultiPartInfo(
                is_multipart=True,
                base_name=m.group("base"),
                part_number=int(m.group("num")),
                format_type="part_archive",
                extension=m.group("ext").lower(),
                original_name=filename,
            )

        # 2. Split binary .001
        m = cls.PATTERN_SPLIT.match(filename)
        if m:
            return MultiPartInfo(
                is_multipart=True,
                base_name=m.group("base"),
                part_number=int(m.group("num")),
                format_type="split_binary",
                extension=m.group("num"),
                original_name=filename,
            )

        # 3. Legacy RAR .r00, .r01
        m = cls.PATTERN_R_NUM.match(filename)
        if m:
            return MultiPartInfo(
                is_multipart=True,
                base_name=m.group("base"),
                part_number=int(m.group("num")) + 2,  # .rar is part 1, .r00 is part 2
                format_type="numbered_rar",
                extension="r" + m.group("num"),
                original_name=filename,
            )

        # 4. Multi-part zip .z01, .z02
        m = cls.PATTERN_Z_NUM.match(filename)
        if m:
            return MultiPartInfo(
                is_multipart=True,
                base_name=m.group("base"),
                part_number=int(m.group("num")),
                format_type="multipart_zip",
                extension="z" + m.group("num"),
                original_name=filename,
            )

        return None

    @classmethod
    def find_package_siblings(cls, directory: str | Path, base_name: str, format_type: str) -> list[tuple[int, Path]]:
        """Scans directory and returns all parts for this base_name sorted by part number."""
        dir_path = Path(directory)
        if not dir_path.is_dir():
            return []

        results: list[tuple[int, Path]] = []
        for entry in dir_path.iterdir():
            if not entry.is_file():
                continue
            info = cls.detect(entry.name)
            if info and info.base_name.lower() == base_name.lower() and info.format_type == format_type:
                results.append((info.part_number, entry))

        # Handle special legacy headers: .rar for .r00 sequences, or .zip for .z01 sequences
        if format_type == "numbered_rar":
            rar_header = dir_path / f"{base_name}.rar"
            if rar_header.is_file() and not any(p[0] == 1 for p in results):
                results.append((1, rar_header))
        elif format_type == "multipart_zip":
            zip_header = dir_path / f"{base_name}.zip"
            if zip_header.is_file():
                # .zip is the terminal volume in multipart zip
                results.append((999999, zip_header))

        results.sort(key=lambda item: item[0])
        return results

    @classmethod
    def is_package_complete(cls, directory: str | Path, base_name: str, format_type: str) -> tuple[bool, list[Path]]:
        """Verifies if parts are contiguous starting from 1 with no gaps."""
        siblings = cls.find_package_siblings(directory, base_name, format_type)
        if not siblings:
            return False, []

        part_numbers = [p[0] for p in siblings]
        part_paths = [p[1] for p in siblings]

        if format_type == "multipart_zip":
            # Must have at least one .z01 and the final .zip
            if not any(p == 999999 for p in part_numbers):
                return False, part_paths
            z_nums = [p for p in part_numbers if p != 999999]
            expected = list(range(1, len(z_nums) + 1))
            return z_nums == expected, part_paths

        # Contiguous check: 1, 2, 3...
        expected = list(range(1, len(part_numbers) + 1))
        is_contiguous = (part_numbers == expected)
        return is_contiguous, part_paths


class BinaryPartJoiner:
    """Combines raw split files (.001, .002...) into a unified file."""

    @staticmethod
    def join(
        parts: list[Path | str],
        output_path: Path | str,
        chunk_size: int = 65536,
        progress_callback: Callable[[int, int], None] | None = None,
        promote: bool = True,
    ) -> tuple[Path, str, int]:
        """
        Sequentially streams parts into output_path.
        Returns (output_path, sha256_hex, total_bytes).
        """
        out_file = Path(output_path).resolve()
        out_file.parent.mkdir(parents=True, exist_ok=True)

        part_paths = [Path(p).resolve() for p in parts]
        for p in part_paths:
            if not p.is_file():
                raise FileNotFoundError(f"Missing split part: {p}")

        total_bytes_expected = sum(p.stat().st_size for p in part_paths)
        written_bytes = 0
        hasher = hashlib.sha256()

        temp_out = out_file.with_name(f".{out_file.name}.joining_tmp")
        completed = False
        try:
            with open(temp_out, "wb") as dest:
                for part_path in part_paths:
                    with open(part_path, "rb") as src:
                        while True:
                            chunk = src.read(chunk_size)
                            if not chunk:
                                break
                            dest.write(chunk)
                            hasher.update(chunk)
                            written_bytes += len(chunk)
                            if progress_callback:
                                progress_callback(written_bytes, total_bytes_expected)
                dest.flush()
                os.fsync(dest.fileno())

            completed = True
            if promote and temp_out.exists():
                os.replace(temp_out, out_file)
        finally:
            if temp_out.exists() and (promote or not completed):
                try:
                    temp_out.unlink()
                except OSError:
                    pass

        return (out_file if promote else temp_out), hasher.hexdigest(), written_bytes


class ArchiveOrchestrator:
    """Coordinates detection, multi-part combining, and safe staged extraction."""

    def __init__(self, archive_backend: ArchiveBackend | None = None) -> None:
        self.backend = archive_backend or ArchiveBackend()

    def inspect_file(self, file_path: str | Path) -> dict[str, Any]:
        """Inspects if file is single archive, split volume, or non-archive."""
        path = Path(file_path)
        info = MultiPartDetector.detect(path.name)
        if info:
            complete, siblings = MultiPartDetector.is_package_complete(path.parent, info.base_name, info.format_type)
            return {
                "is_multipart": True,
                "base_name": info.base_name,
                "part_number": info.part_number,
                "format_type": info.format_type,
                "complete": complete,
                "part_count": len(siblings),
                "parts": [str(p) for p in siblings],
            }
        return {"is_multipart": False, "complete": True, "parts": [str(path)]}

    def process_package(
        self,
        directory: str | Path,
        base_name: str,
        format_type: str,
        output_directory: str | Path | None = None,
        delete_parts_on_success: bool = False,
        password_ref: str | None = None,
    ) -> dict[str, Any]:
        """Processes a completed multi-part package (joining if split, or extracting volume 1)."""
        dir_path = Path(directory).resolve()
        complete, parts = MultiPartDetector.is_package_complete(dir_path, base_name, format_type)
        if not complete or not parts:
            return {"success": False, "error": f"Package {base_name} is incomplete or missing parts"}

        out_dir = Path(output_directory).resolve() if output_directory else dir_path
        out_dir.mkdir(parents=True, exist_ok=True)

        if format_type == "split_binary":
            # Raw split parts: combine into single file
            combined_file = dir_path / base_name
            joined_path, digest, total_size = BinaryPartJoiner.join(parts, combined_file)
            result: dict[str, Any] = {
                "success": True,
                "action": "joined_binary",
                "output_file": str(joined_path),
                "sha256": digest,
                "total_bytes": total_size,
                "parts_removed": False,
            }

            # If combined file is itself an archive (e.g. game.iso.001 -> game.iso, backup.7z.001 -> backup.7z, file.rar.001 -> file.rar)
            if self.backend.available() and joined_path.suffix.lower() in (".rar", ".7z", ".zip", ".tar", ".gz"):
                try:
                    ext_res = self.backend.extract(str(joined_path), str(out_dir), password_ref)
                    result["extracted"] = ext_res
                    result["action"] = "joined_and_extracted"
                except Exception as exc:
                    result["success"] = False
                    result["extract_error"] = str(exc)

            if delete_parts_on_success and result.get("success"):
                for p in parts:
                    try:
                        p.unlink()
                    except OSError:
                        pass
                result["parts_removed"] = True

            return result

        # Multi-volume archive: Volume 1 extraction
        primary_volume = parts[0]
        if not self.backend.available():
            return {
                "success": False,
                "error": "archive-worker unavailable for multi-volume extraction",
                "primary_volume": str(primary_volume),
                "parts": [str(p) for p in parts],
            }

        try:
            ext_res = self.backend.extract(str(primary_volume), str(out_dir), password_ref)
            result = {
                "success": True,
                "action": "extracted_multivolume",
                "primary_volume": str(primary_volume),
                "parts_count": len(parts),
                "extracted": ext_res,
                "parts_removed": False,
            }
            if delete_parts_on_success:
                for p in parts:
                    try:
                        p.unlink()
                    except OSError:
                        pass
                result["parts_removed"] = True
            return result
        except Exception as exc:
            return {"success": False, "error": str(exc), "primary_volume": str(primary_volume)}
