from __future__ import annotations
import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import gzip
import io
import tarfile
import time
import unittest
import zipfile
from pathlib import Path

from engine.archive_backend import ArchiveBackend


class ArchiveWorkerTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).parent / ".test-artifacts" / "archive-worker"
        base.mkdir(parents=True, exist_ok=True)
        self.root = base / f"run-{time.time_ns()}"
        self.root.mkdir()
        self.backend = ArchiveBackend()
        if not self.backend.available():
            self.skipTest("archive-worker binary has not been built")

    def tearDown(self):
        pass

    def test_zip_probe_test_and_safe_extract(self):
        archive = self.root / "bundle.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("folder/", b"")
            handle.writestr("folder/hello.txt", b"hello archive")
        summary = self.backend.probe(archive)
        self.assertEqual(summary["format"], "zip")
        self.assertEqual(summary["expanded_bytes"], len(b"hello archive"))
        checked = self.backend.test(archive)
        self.assertTrue(checked["valid"])
        output = self.root / "extracted"
        result = self.backend.extract(archive, output)
        self.assertEqual(result["files"], 1)
        self.assertEqual((output / "folder" / "hello.txt").read_bytes(), b"hello archive")

    def test_extract_merges_into_existing_download_directory(self):
        archive = self.root / "bundle.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("payload/readme.txt", b"extracted archive content")
        output = self.root / "download-directory"
        output.mkdir()
        marker = output / "bundle.zip"
        marker.write_bytes(b"downloaded source must remain")

        result = self.backend.extract(archive, output)

        self.assertEqual(result["files"], 1)
        self.assertEqual((output / "payload" / "readme.txt").read_bytes(), b"extracted archive content")
        self.assertEqual(marker.read_bytes(), b"downloaded source must remain")

    def test_tar_gzip_and_gzip_extract(self):
        tar_gz = self.root / "bundle.tar.gz"
        with tarfile.open(tar_gz, "w:gz") as handle:
            data = b"tar data"
            info = tarfile.TarInfo("nested/data.txt")
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
        self.assertTrue(self.backend.test(tar_gz)["valid"])
        tar_output = self.root / "tar-out"
        self.backend.extract(tar_gz, tar_output)
        self.assertEqual((tar_output / "nested" / "data.txt").read_bytes(), b"tar data")

        gzip_path = self.root / "single.txt.gz"
        with gzip.open(gzip_path, "wb") as handle:
            handle.write(b"gzip data")
        gzip_output = self.root / "gzip-out"
        self.backend.extract(gzip_path, gzip_output)
        self.assertEqual((gzip_output / "single.txt").read_bytes(), b"gzip data")

    def test_traversal_duplicate_and_limits_are_rejected(self):
        archive = self.root / "unsafe.zip"
        with zipfile.ZipFile(archive, "w") as handle:
            handle.writestr("../escape.txt", b"bad")
        with self.assertRaises(RuntimeError):
            self.backend.list(archive)

        limited = self.root / "limited.zip"
        with zipfile.ZipFile(limited, "w") as handle:
            handle.writestr("one.txt", b"1")
            handle.writestr("two.txt", b"2")
        with self.assertRaises(RuntimeError):
            self.backend.probe(limited, policy={"max_file_count": 1})

    FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "archives"

    def _needs_7zip(self):
        import os
        import shutil
        roots = [os.environ.get(v, "") for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
        if not any(Path(r, "7-Zip", "7z.exe").is_file() for r in roots if r) and not shutil.which("7z"):
            self.skipTest("7-Zip is not installed")

    def test_rar_extracts_through_the_installed_7zip(self):
        self._needs_7zip()
        for name, member in (("solid.rar", ".gitignore"), ("unicode.rar", "te…―st✌")):
            summary = self.backend.probe(self.FIXTURES / name)
            self.assertEqual(summary["format"], "rar")
            output = self.root / name
            result = self.backend.extract(self.FIXTURES / name, output)
            self.assertEqual(result["files"], 1, name)
            self.assertTrue((output / member).is_file(), f"{name}: {sorted(p.name for p in output.iterdir())}")

    def test_encrypted_rar_needs_a_password_and_never_prompts(self):
        self._needs_7zip()
        from engine.errors import NeedsUser
        with self.assertRaisesRegex(NeedsUser, "(?i)password"):
            self.backend.extract(self.FIXTURES / "crypted.rar", self.root / "crypted")

    def test_without_7zip_rar_says_what_to_install(self):
        import os
        from unittest.mock import patch
        empty = self.root / "no-7zip"
        empty.mkdir()
        hidden = {"ProgramFiles": str(empty), "ProgramW6432": str(empty), "ProgramFiles(x86)": str(empty),
                  "PATH": str(empty), "MOSSDL_7ZIP": ""}
        with patch.dict(os.environ, hidden):
            with self.assertRaisesRegex(RuntimeError, "7-Zip"):
                self.backend.probe(self.FIXTURES / "solid.rar")

    def test_rar_is_supported_and_rejects_invalid_archive(self):
        archive = self.root / "archive.rar"
        archive.write_bytes(b"not a rar")
        with self.assertRaisesRegex(RuntimeError, "(?i)not a rar|unsupported archive|cannot open the file as archive|needs 7-Zip"):
            self.backend.probe(archive)


if __name__ == "__main__":
    unittest.main()
